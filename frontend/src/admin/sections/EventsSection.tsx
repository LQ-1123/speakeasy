"use client";

/**
 * 事件：角色事件（persona_events）+ 公共事件池（event_pool + event_pool_claims）。
 *
 * 为什么这个分区重要
 * ------------------
 * 公共事件池是本期唯一能让运营**影响世界走向**的入口：写一条事件，世界里的角色按契合度来抢。
 * 但"谁会领"以前是黑盒——运营只能改条件、看结果，无法解释原因。
 * 所以「预演」必须给出 **分项得分明细**：职业 / 兴趣 / 城市 / 表达 DNA 各贡献多少、
 * 占全部贡献的比例是多少。只给一个总分等于没做：运营没法判断"改一下条件会不会换人"。
 *
 * 实现要点
 * --------
 * 1. 预演走 `useQuery`（key = `adminKeys.eventPoolPreview`），点开哪张卡就只算哪张；
 *    它是 POST（不落库），用 query 是为了拿到缓存、以及在领取后自动失效重算。
 * 2. `score_detail` 的键名由后端决定，前端**不硬编码维度集合**：遍历返回的键，
 *    已知键映射成中文（职业契合 / 兴趣匹配 / 城市匹配 / 表达 DNA），未知键原样显示。
 *    后端将来加一个维度，页面不用改、也不会悄悄漏掉。
 * 3. 领取是"批量 + 幂等"：后端跳过已领过的角色，前端只负责把勾选的 id 交上去。
 * 4. 名额满了（status === "filled" 或 claims 数 >= slots）状态直接显示 `filled`。
 * 5. 事件类型下拉读 `lexicons/events` 词库（在「世界节奏」里可维护），
 *    读不到就回落到内置的 12 种，不让页面挂掉。
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AdminApiError, apiDelete, apiGet, apiPost, apiPut, withQuery } from "../api";
import { adminKeys } from "../queryKeys";
import {
  Badge,
  Button,
  ConfirmDialog,
  Drawer,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Pager,
  Segmented,
  Select,
  Table,
  TextArea,
  TextInput,
  ToastStack,
  useToasts,
} from "../ui";
import type { Column } from "../ui";

/* -------------------------------------------------------------------------- */
/* 数据形状（以 task-9 契约为准；后端并行实现，形状差异由 Lead 整合时统一）      */
/* -------------------------------------------------------------------------- */

interface PersonaOption {
  id: string;
  nickname?: string;
  city?: string;
  occupation?: string;
}

interface PersonaListPayload {
  ok?: boolean;
  personas?: PersonaOption[];
}

interface EventRow {
  id: string;
  persona_id?: string;
  persona_nickname?: string;
  event_type?: string;
  title?: string;
  source_type?: string;
  event_status?: string;
  day?: string;
  created_at?: string | number;
}

interface EventListPayload {
  ok?: boolean;
  total?: number;
  items?: EventRow[];
}

interface EventPoolClaim {
  persona_id: string;
  persona_nickname?: string;
  score?: number;
  claimed_at?: string | number;
}

interface EventPoolItem {
  id: string;
  title?: string;
  description?: string;
  event_type?: string;
  start_at?: string | number | null;
  location?: string;
  slots?: number;
  conditions?: unknown;
  status?: string;
  created_at?: string | number;
  claims?: EventPoolClaim[];
}

interface EventPoolPayload {
  ok?: boolean;
  items?: EventPoolItem[];
}

/** 预演候选：后端必须回 `score_detail`（分项），否则页面会明说"只有总分"。 */
interface PreviewCandidate {
  persona_id: string;
  nickname?: string;
  score?: number;
  score_detail?: unknown;
}

interface PreviewPayload {
  ok?: boolean;
  event_id?: string;
  limit?: number;
  items?: PreviewCandidate[];
}

/** 词库 events 的形状（与 RhythmSection 共用同一个 queryKey，天然共享缓存）。 */
interface EventTypeSpec {
  zh?: string;
  label?: string;
}

interface EventsLexiconPayload {
  data?: { types?: Record<string, EventTypeSpec> };
}

type SectionTab = "persona" | "pool";

const PAGE_SIZE = 20;
const PREVIEW_LIMIT = 12;

/* -------------------------------------------------------------------------- */
/* 中文标签                                                                    */
/* -------------------------------------------------------------------------- */

const EVENT_ZH: Record<string, string> = {
  achievement: "个人成就",
  conflict: "冲突",
  personal_milestone: "个人里程碑",
  relationship: "关系变化",
  social: "社交",
  entertainment: "娱乐",
  travel: "旅行",
  work: "工作",
  study: "学习",
  unexpected_event: "意外",
  meal: "吃饭",
  daily_routine: "日常",
};

/** 词库读不到时的回落（顺序即下拉顺序）。 */
const EVENT_TYPE_FALLBACK: Array<{ value: string; label: string }> = [
  "meal",
  "daily_routine",
  "work",
  "study",
  "social",
  "entertainment",
  "travel",
  "relationship",
  "achievement",
  "personal_milestone",
  "unexpected_event",
  "conflict",
].map((value) => ({ value, label: `${EVENT_ZH[value]}（${value}）` }));

const SOURCE_TYPE_LABEL: Record<string, string> = {
  rule: "规则事件",
  narrative: "叙事事件",
  manual: "手动补录",
  pool: "公共池",
  import: "导入",
};

const EVENT_STATUS_LABEL: Record<string, string> = {
  pending: "待发",
  scheduled: "已排期",
  published: "已发布",
  shared: "已分享",
  done: "完成",
  skipped: "跳过",
  hidden: "隐藏",
  archived: "归档",
};

const EVENT_STATUS_TONE: Record<string, "success" | "error" | "warn" | "info" | undefined> = {
  published: "success",
  shared: "success",
  done: "success",
  pending: "info",
  scheduled: "info",
  skipped: "warn",
  hidden: "warn",
};

const POOL_STATUS_LABEL: Record<string, string> = {
  open: "开放中",
  filled: "已领满",
  closed: "已关闭",
  draft: "草稿",
  cancelled: "已取消",
};

const POOL_STATUS_TONE: Record<string, "success" | "error" | "warn" | "info" | undefined> = {
  open: "info",
  filled: "success",
  closed: undefined,
  draft: "warn",
  cancelled: "error",
};

/**
 * 分项键名 → 中文。
 *
 * 同一维度后端可能用不同命名（occupation / occupation_match / job …），这里都收，
 * 未收录的键直接用原样显示——宁可露出原始键，也不要因为猜错而漏掉一个维度。
 */
const DIMENSION_LABEL: Record<string, string> = {
  occupation: "职业契合",
  occupation_match: "职业契合",
  occupation_score: "职业契合",
  job: "职业契合",
  job_match: "职业契合",
  interests: "兴趣匹配",
  interest: "兴趣匹配",
  interest_match: "兴趣匹配",
  interests_match: "兴趣匹配",
  interest_score: "兴趣匹配",
  city: "城市匹配",
  city_match: "城市匹配",
  city_score: "城市匹配",
  location: "城市匹配",
  location_match: "城市匹配",
  dna: "表达 DNA",
  dna_match: "表达 DNA",
  dna_score: "表达 DNA",
  speech_dna: "表达 DNA",
  expression_dna: "表达 DNA",
  persona_dna: "表达 DNA",
  base: "基础分",
  base_score: "基础分",
  random: "随机扰动",
  jitter: "随机扰动",
  noise: "随机扰动",
};

/* -------------------------------------------------------------------------- */
/* 小工具                                                                      */
/* -------------------------------------------------------------------------- */

function errorOf(error: unknown): { message: string; issues: string[] } {
  if (error instanceof AdminApiError) {
    return { message: error.detail || error.message, issues: error.issues };
  }
  return { message: error instanceof Error ? error.message : String(error), issues: [] };
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function formatDate(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/** 时间字段可能是 ISO 字符串、unix 秒或毫秒；无法识别就原样显示，不做静默猜测。 */
function formatWhen(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number" && Number.isFinite(value)) {
    const date = new Date(value < 1e12 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : formatDate(date);
  }
  const text = String(value);
  if (/^\d+$/.test(text)) {
    const numeric = Number(text);
    const date = new Date(numeric < 1e12 ? numeric * 1000 : numeric);
    if (!Number.isNaN(date.getTime())) return formatDate(date);
  }
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? text : formatDate(parsed);
}

/** 分数统一两位小数：明细里 0.30000000000000004 这种会让人怀疑数据坏了。 */
function formatScore(value: number | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return String(Number(value.toFixed(2)));
}

function describeConditions(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) {
    return value
      .map((entry) => (typeof entry === "string" ? entry : JSON.stringify(entry)))
      .join(" / ");
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/* -------------------------------------------------------------------------- */
/* 分项得分：把 score_detail 变成"看得懂为什么"的进度条                         */
/* -------------------------------------------------------------------------- */

interface ScoreDimension {
  key: string;
  label: string;
  /** 数值型贡献；null 表示该键不是数值（下面用 note 展示原始内容）。 */
  value: number | null;
  note: string;
}

function dimensionLabel(key: string): string {
  return DIMENSION_LABEL[key] ?? DIMENSION_LABEL[key.toLowerCase()] ?? key;
}

/**
 * 归一化 `score_detail`。
 *
 * 后端可能给：
 * - 扁平数值：`{occupation: 0.32, interests: 0.21, ...}`
 * - 带原因的嵌套：`{occupation: {score: 0.32, reason: "同职业"}}`
 * - 字符串/数组说明：`{city: "同城加成"}`
 * 三种都要能显示，且不丢信息（看不懂的键 + 原始值也照实展示）。
 */
function scoreDimensions(detail: unknown): ScoreDimension[] {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return [];
  const rows: ScoreDimension[] = [];
  for (const [key, raw] of Object.entries(detail as Record<string, unknown>)) {
    if (typeof raw === "number" && Number.isFinite(raw)) {
      rows.push({ key, label: dimensionLabel(key), value: raw, note: "" });
      continue;
    }
    if (typeof raw === "string") {
      rows.push({ key, label: dimensionLabel(key), value: null, note: raw });
      continue;
    }
    if (typeof raw === "boolean") {
      rows.push({ key, label: dimensionLabel(key), value: null, note: raw ? "是" : "否" });
      continue;
    }
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      const record = raw as Record<string, unknown>;
      const numericKey = ["score", "value", "points", "contribution", "weight"].find(
        (candidate) => typeof record[candidate] === "number",
      );
      const noteKey = ["reason", "note", "detail", "label", "explain"].find(
        (candidate) => typeof record[candidate] === "string",
      );
      rows.push({
        key,
        label: dimensionLabel(key),
        value: typeof numericKey === "string" ? Number(record[numericKey]) : null,
        note: typeof noteKey === "string" ? String(record[noteKey]) : "",
      });
      continue;
    }
    if (Array.isArray(raw)) {
      rows.push({
        key,
        label: dimensionLabel(key),
        value: null,
        note: raw.map((entry) => (typeof entry === "string" ? entry : JSON.stringify(entry))).join(" / "),
      });
      continue;
    }
    rows.push({ key, label: dimensionLabel(key), value: null, note: String(raw) });
  }
  return rows;
}

/**
 * 分项得分明细。
 *
 * 呈现方式：每行一个维度 = 中文名 + 进度条 + `数值 · 占比%`。
 * 占比 = 该维度 |贡献| / 全部 |贡献| 之和 —— 回答的是"这一分里它出了多少力"，
 * 而不是"满分是多少"（满分口径后端没给，不猜）。
 */
function ScoreBreakdown({ detail }: { detail: unknown }) {
  const rows = scoreDimensions(detail);
  if (!rows.length) {
    return (
      <div className="admin-muted" style={{ fontSize: 12 }}>
        后端这次没有返回分项明细（只有总分），无法解释排名原因。请让 backend 在
        <span className="admin-mono"> score_detail </span>里给出各维度贡献。
      </div>
    );
  }
  const totalAbs = rows.reduce((sum, row) => sum + Math.abs(row.value ?? 0), 0);
  return (
    <div className="admin-col" style={{ gap: 3 }}>
      {rows.map((row) => {
        const share =
          row.value !== null && totalAbs > 0 ? Math.round((Math.abs(row.value) / totalAbs) * 100) : 0;
        return (
          <div className="admin-row" key={row.key} style={{ gap: 8 }}>
            <span style={{ flex: "0 0 96px", fontSize: 12 }} title={row.key}>
              {row.label}
            </span>
            <div
              style={{
                flex: "1 1 120px",
                minWidth: 80,
                height: 8,
                borderRadius: 999,
                background: "var(--md-surface-container-high, #e6e5ee)",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  width: `${share}%`,
                  height: "100%",
                  background: "var(--md-primary, #4c5e8b)",
                }}
              />
            </div>
            <span className="admin-mono" style={{ flex: "0 0 118px", textAlign: "right" }}>
              {row.value === null ? "—" : `${formatScore(row.value)} · ${share}%`}
            </span>
            {row.note ? (
              <span
                className="admin-muted admin-truncate"
                style={{ flex: "1 1 140px", fontSize: 11 }}
                title={row.note}
              >
                {row.note}
              </span>
            ) : null}
          </div>
        );
      })}
      <div className="admin-muted" style={{ fontSize: 11 }}>
        分项 = 各维度对总分的贡献；百分比 = 该维度占全部贡献的比例。榜单个位按总分降序。
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 抽屉表单的本地草稿                                                          */
/* -------------------------------------------------------------------------- */

interface PoolForm {
  /** 空字符串 = 新建；有 id = 编辑。 */
  id: string;
  title: string;
  description: string;
  event_type: string;
  start_at: string;
  location: string;
  slots: string;
  status: string;
  /** conditions 形状后端未定，这里当 JSON 文本编辑、原样往返，不替后端发明结构。 */
  conditionsText: string;
}

const EMPTY_POOL_FORM: PoolForm = {
  id: "",
  title: "",
  description: "",
  event_type: "",
  start_at: "",
  location: "",
  slots: "3",
  status: "open",
  conditionsText: "",
};

function poolToForm(item: EventPoolItem): PoolForm {
  return {
    id: item.id,
    title: item.title ?? "",
    description: item.description ?? "",
    event_type: item.event_type ?? "",
    start_at: item.start_at === null || item.start_at === undefined ? "" : String(item.start_at),
    location: item.location ?? "",
    slots: typeof item.slots === "number" ? String(item.slots) : "",
    status: item.status ?? "open",
    conditionsText:
      item.conditions === null || item.conditions === undefined
        ? ""
        : JSON.stringify(item.conditions, null, 2),
  };
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function EventsSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();

  const [tab, setTab] = useState<SectionTab>("persona");

  /* ------------------------------ 角色事件 ------------------------------ */

  const [personaFilter, setPersonaFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [eventForm, setEventForm] = useState({
    persona_id: "",
    event_type: "",
    title: "",
    day: "",
    visibility: "",
  });

  /* ------------------------------ 公共事件池 ------------------------------ */

  const [poolStatus, setPoolStatus] = useState("");
  const [form, setForm] = useState<PoolForm | null>(null);
  const [formError, setFormError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<EventPoolItem | null>(null);
  const [openPreviewId, setOpenPreviewId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  /* ------------------------------ 基础数据 ------------------------------ */

  // 角色下拉：两个 Tab 共用，300 条足够（超过就该用搜索了，而不是一个下拉）。
  const personasQuery = useQuery({
    queryKey: adminKeys.personas({ limit: 300 }),
    queryFn: () => apiGet<PersonaListPayload>(withQuery("/api/admin/personas", { limit: 300 })),
    staleTime: 5 * 60_000,
  });

  // 事件类型来自词库（「世界节奏」里可维护）；与 RhythmSection 共用同一个 key。
  const lexiconQuery = useQuery({
    queryKey: adminKeys.lexicon("events"),
    queryFn: () => apiGet<EventsLexiconPayload>("/api/admin/lexicons/events"),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const personas = personasQuery.data?.personas ?? [];

  const personaChoices = useMemo(
    () =>
      personas.map((persona) => ({
        value: persona.id,
        label: persona.nickname ? `${persona.nickname} · ${persona.id}` : persona.id,
      })),
    [personas],
  );

  const filterPersonaOptions = useMemo(
    () => [{ value: "", label: "全部角色" }, ...personaChoices],
    [personaChoices],
  );

  const formPersonaOptions = useMemo(
    () => [{ value: "", label: "选择角色…" }, ...personaChoices],
    [personaChoices],
  );

  /** 类型 → 中文：词库优先，内置兜底。 */
  const typeLabelMap = useMemo(() => {
    const map: Record<string, string> = { ...EVENT_ZH };
    const types = lexiconQuery.data?.data?.types;
    if (types && typeof types === "object") {
      for (const [key, spec] of Object.entries(types)) {
        const label = spec?.zh || spec?.label;
        if (label) map[key] = label;
      }
    }
    return map;
  }, [lexiconQuery.data]);

  const eventTypeOptions = useMemo(() => {
    const types = lexiconQuery.data?.data?.types;
    if (!types || typeof types !== "object") return EVENT_TYPE_FALLBACK;
    const options = Object.entries(types).map(([key, spec]) => ({
      value: key,
      label: `${spec?.zh || spec?.label || EVENT_ZH[key] || key}（${key}）`,
    }));
    return options.length ? options : EVENT_TYPE_FALLBACK;
  }, [lexiconQuery.data]);

  /* ------------------------------ 查询 ------------------------------ */

  const eventsQuery = useQuery({
    queryKey: adminKeys.events({
      persona_id: personaFilter,
      source_type: sourceFilter,
      offset,
      limit: PAGE_SIZE,
    }),
    queryFn: () =>
      apiGet<EventListPayload>(
        withQuery("/api/admin/events", {
          persona_id: personaFilter,
          source_type: sourceFilter,
          offset,
          limit: PAGE_SIZE,
        }),
      ),
    enabled: tab === "persona",
  });

  const poolQuery = useQuery({
    queryKey: adminKeys.eventPool({ status: poolStatus }),
    queryFn: () =>
      apiGet<EventPoolPayload>(withQuery("/api/admin/event-pool", { status: poolStatus })),
    enabled: tab === "pool",
  });

  /**
   * 预演：点哪张卡算哪张。
   *
   * 用 query 而不是 mutation：一来 `adminKeys.eventPoolPreview` 就是为它准备的，
   * 二来领取后只要 invalidate 前缀 `["admin","eventPool"]`，预演结果会自动重算。
   */
  const previewQuery = useQuery({
    queryKey: adminKeys.eventPoolPreview({ pool_id: openPreviewId, limit: PREVIEW_LIMIT }),
    queryFn: () => {
      const poolId = openPreviewId as string;
      return apiPost<PreviewPayload>(
        `/api/admin/event-pool/${encodeURIComponent(poolId)}/preview`,
        { limit: PREVIEW_LIMIT },
      );
    },
    enabled: Boolean(openPreviewId),
    staleTime: 30_000,
    retry: false,
  });

  /* ------------------------------ 失效与刷新 ------------------------------ */

  const invalidateEvents = () => {
    void queryClient.invalidateQueries({ queryKey: ["admin", "events"] });
  };

  const invalidatePool = () => {
    // 前缀同时命中 eventPool 列表与 eventPoolPreview（预演依赖池子里的 claims）。
    void queryClient.invalidateQueries({ queryKey: ["admin", "eventPool"] });
  };

  /* ------------------------------ 变更 ------------------------------ */

  const createEvent = useMutation({
    mutationFn: () =>
      apiPost<{ ok?: boolean; issues?: string[]; id?: string }>("/api/admin/events", {
        persona_id: eventForm.persona_id,
        event_type: eventForm.event_type,
        ...(eventForm.title.trim() ? { title: eventForm.title.trim() } : {}),
        ...(eventForm.day.trim() ? { day: eventForm.day.trim() } : {}),
        ...(eventForm.visibility ? { visibility: eventForm.visibility } : {}),
      }),
    onSuccess: (data) => {
      if (data?.ok === false) {
        push((data.issues ?? ["补录被拒绝"]).join("；"), "error");
        return;
      }
      push("已补录一条角色事件");
      setEventForm((current) => ({ ...current, title: "", day: "" }));
      invalidateEvents();
    },
    onError: (error) => push(`补录失败：${errorOf(error).message}`, "error"),
  });

  const createPool = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiPost<{ ok?: boolean; issues?: string[]; id?: string }>("/api/admin/event-pool", body),
    onSuccess: (data) => {
      if (data?.ok === false) {
        setFormError((data.issues ?? ["创建被拒绝"]).join("；"));
        return;
      }
      setForm(null);
      setFormError("");
      push("公共事件已创建");
      invalidatePool();
    },
    onError: (error) => setFormError(errorOf(error).message),
  });

  const savePool = useMutation({
    mutationFn: (args: { id: string; body: Record<string, unknown> }) =>
      apiPut<{ ok?: boolean; issues?: string[] }>(
        `/api/admin/event-pool/${encodeURIComponent(args.id)}`,
        args.body,
      ),
    onSuccess: (data) => {
      if (data?.ok === false) {
        setFormError((data.issues ?? ["保存被拒绝"]).join("；"));
        return;
      }
      setForm(null);
      setFormError("");
      push("公共事件已保存");
      invalidatePool();
    },
    onError: (error) => setFormError(errorOf(error).message),
  });

  const deletePool = useMutation({
    mutationFn: (id: string) =>
      apiDelete<{ ok?: boolean; issues?: string[] }>(`/api/admin/event-pool/${encodeURIComponent(id)}`),
    onSuccess: (data) => {
      setDeleteTarget(null);
      if (data?.ok === false) {
        push((data.issues ?? ["删除被拒绝"]).join("；"), "error");
        return;
      }
      push("公共事件已删除（对应的领取记录一并删除）");
      invalidatePool();
    },
    onError: (error) => {
      setDeleteTarget(null);
      push(`删除失败：${errorOf(error).message}`, "error");
    },
  });

  const claimPool = useMutation({
    mutationFn: (args: { id: string; personaIds: string[] }) =>
      apiPost<{ ok?: boolean; issues?: string[]; claimed?: unknown[]; skipped?: unknown[] }>(
        `/api/admin/event-pool/${encodeURIComponent(args.id)}/claim`,
        args.personaIds.length ? { persona_ids: args.personaIds } : {},
      ),
    onSuccess: (data, args) => {
      if (data?.ok === false) {
        push((data.issues ?? ["领取被拒绝"]).join("；"), "error");
        return;
      }
      const claimed = Array.isArray(data?.claimed) ? data.claimed.length : args.personaIds.length;
      const skipped = Array.isArray(data?.skipped) ? data.skipped.length : 0;
      push(
        args.personaIds.length
          ? `已让 ${claimed} 个角色领取${skipped ? `（${skipped} 个已领过，跳过）` : ""}`
          : `已按系统排名自动领取${claimed ? ` ${claimed} 个角色` : ""}`,
      );
      setSelectedIds([]);
      invalidatePool();
    },
    onError: (error) => push(`领取失败：${errorOf(error).message}`, "error"),
  });

  /* ------------------------------ 提交 ------------------------------ */

  const submitPool = (current: PoolForm) => {
    setFormError("");
    const title = current.title.trim();
    if (!title) {
      setFormError("标题不能为空");
      return;
    }
    const eventType = current.event_type.trim();
    if (!eventType) {
      setFormError("事件类型不能为空");
      return;
    }
    const slots = Number(current.slots);
    if (!Number.isFinite(slots) || slots <= 0) {
      setFormError("名额必须是大于 0 的整数");
      return;
    }
    let conditions: unknown;
    if (current.conditionsText.trim()) {
      try {
        conditions = JSON.parse(current.conditionsText);
      } catch {
        setFormError("conditions 不是合法 JSON（留空表示没有额外条件）");
        return;
      }
    }

    // 空值一律不发送：让后端用它自己的默认值，比发 null/"" 更安全。
    const body: Record<string, unknown> = {
      title,
      description: current.description.trim(),
      event_type: eventType,
      slots: Math.round(slots),
    };
    if (current.start_at.trim()) body.start_at = current.start_at.trim();
    if (current.location.trim()) body.location = current.location.trim();
    if (conditions !== undefined) body.conditions = conditions;
    if (current.id) {
      body.status = current.status;
      savePool.mutate({ id: current.id, body });
    } else {
      createPool.mutate(body);
    }
  };

  /* ------------------------------ 渲染：角色事件 ------------------------------ */

  const eventColumns: Array<Column<EventRow>> = [
    {
      key: "when",
      header: "日期",
      width: "150px",
      render: (row) => (
        <div className="admin-col" style={{ gap: 0 }}>
          <span>{row.day || formatWhen(row.created_at)}</span>
          <span className="admin-muted admin-mono">{formatWhen(row.created_at)}</span>
        </div>
      ),
    },
    {
      key: "persona",
      header: "角色",
      render: (row) =>
        row.persona_nickname ? (
          <div className="admin-col" style={{ gap: 0 }}>
            <span>{row.persona_nickname}</span>
            <span className="admin-muted admin-mono">{row.persona_id}</span>
          </div>
        ) : (
          <span className="admin-mono">{row.persona_id}</span>
        ),
    },
    {
      key: "type",
      header: "类型",
      render: (row) =>
        row.event_type ? (
          <span title={row.event_type}>{typeLabelMap[row.event_type] || row.event_type}</span>
        ) : null,
    },
    {
      key: "title",
      header: "标题",
      render: (row) =>
        row.title ? (
          <span className="admin-truncate" style={{ display: "block", maxWidth: 320 }} title={row.title}>
            {row.title}
          </span>
        ) : null,
    },
    {
      key: "source",
      header: "来源",
      render: (row) =>
        row.source_type ? (
          <Badge tone="info">{SOURCE_TYPE_LABEL[row.source_type] || row.source_type}</Badge>
        ) : null,
    },
    {
      key: "status",
      header: "状态",
      render: (row) =>
        row.event_status ? (
          <Badge tone={EVENT_STATUS_TONE[row.event_status]}>
            {EVENT_STATUS_LABEL[row.event_status] || row.event_status}
          </Badge>
        ) : null,
    },
  ];

  const eventTotal = eventsQuery.data?.total ?? 0;
  const eventItems = eventsQuery.data?.items ?? [];

  /* ------------------------------ 渲染：公共池 ------------------------------ */

  const pools = poolQuery.data?.items ?? [];
  const previewCandidates = previewQuery.data?.items ?? [];
  const previewSelected = selectedIds.filter((id) =>
    previewCandidates.some((candidate) => candidate.persona_id === id),
  );

  const toggleCandidate = (personaId: string) => {
    setSelectedIds((current) =>
      current.includes(personaId)
        ? current.filter((id) => id !== personaId)
        : [...current, personaId],
    );
  };

  const openPreview = (poolId: string) => {
    setSelectedIds([]);
    setOpenPreviewId(poolId);
  };

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">事件</h1>
          <p className="admin-section-desc">
            左边是角色身上真实发生过的生活事件，可以筛选回看、也可以手动补一条；
            右边是「公共事件池」——写一条世界里的事件，让角色按契合度来领。
            点「预演谁会领」会拿到一份<strong>不写库</strong>的评分排序，并逐项拆开告诉你为什么是 TA。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button
            onClick={() => {
              if (tab === "persona") void eventsQuery.refetch();
              else void poolQuery.refetch();
            }}
            disabled={eventsQuery.isFetching || poolQuery.isFetching}
          >
            {eventsQuery.isFetching || poolQuery.isFetching ? "刷新中…" : "刷新"}
          </Button>
        </div>
      </div>

      <div className="admin-row">
        <Segmented<SectionTab>
          value={tab}
          onChange={setTab}
          options={[
            { value: "persona", label: "角色事件" },
            { value: "pool", label: "公共事件池" },
          ]}
        />
        <span className="admin-muted">
          {tab === "persona"
            ? "角色事件的读写走 persona_events"
            : "公共池的「预演」只做评分，不会写库；「领取」才真正落库"}
        </span>
      </div>

      {tab === "persona" ? (
        <>
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">手动补一条事件</h2>
              <span className="admin-card-sub">补历史 / 补漏；来源会被记为手动补录</span>
            </div>

            <div className="admin-form-grid">
              <Field label="角色">
                <Select<string>
                  value={eventForm.persona_id}
                  onChange={(value) => setEventForm((current) => ({ ...current, persona_id: value }))}
                  options={formPersonaOptions}
                  disabled={personasQuery.isPending}
                />
              </Field>

              <Field label="事件类型" hint="取值来自「世界节奏 → AI 事件与动态」词库">
                <Select<string>
                  value={eventForm.event_type}
                  onChange={(value) => setEventForm((current) => ({ ...current, event_type: value }))}
                  options={[{ value: "", label: "选择类型…" }, ...eventTypeOptions]}
                />
              </Field>

              <Field label="标题" hint="可留空，由后端按类型生成">
                <TextInput
                  value={eventForm.title}
                  onChange={(value) => setEventForm((current) => ({ ...current, title: value }))}
                  placeholder="例如：在便利店遇到老朋友"
                />
              </Field>

              <Field label="日期" hint="留空 = 今天；格式 YYYY-MM-DD">
                <TextInput
                  value={eventForm.day}
                  onChange={(value) => setEventForm((current) => ({ ...current, day: value }))}
                  placeholder="2026-11-11"
                />
              </Field>

              <Field label="可见性" hint="留空 = 由后端按默认策略决定">
                <Select<string>
                  value={eventForm.visibility}
                  onChange={(value) => setEventForm((current) => ({ ...current, visibility: value }))}
                  options={[
                    { value: "", label: "默认" },
                    { value: "public", label: "公开" },
                    { value: "friends", label: "好友可见" },
                    { value: "private", label: "仅自己" },
                  ]}
                />
              </Field>
            </div>

            <div className="admin-row" style={{ marginTop: 12 }}>
              <Button
                variant="primary"
                loading={createEvent.isPending}
                disabled={!eventForm.persona_id || !eventForm.event_type}
                onClick={() => createEvent.mutate()}
              >
                补录事件
              </Button>
              {!personas.length && !personasQuery.isPending ? (
                <span className="admin-muted">还没有角色，先去「角色工坊」捏一个。</span>
              ) : null}
            </div>
          </div>

          <div className="admin-row">
            <span className="admin-muted">角色</span>
            <div style={{ minWidth: 200 }}>
              <Select<string>
                value={personaFilter}
                onChange={(value) => {
                  setPersonaFilter(value);
                  setOffset(0);
                }}
                options={filterPersonaOptions}
              />
            </div>
            <span className="admin-muted">来源</span>
            <Segmented<string>
              value={sourceFilter}
              onChange={(value) => {
                setSourceFilter(value);
                setOffset(0);
              }}
              options={[
                { value: "", label: "全部" },
                { value: "rule", label: "规则" },
                { value: "narrative", label: "叙事" },
                { value: "manual", label: "手动" },
              ]}
            />
            <span className="admin-muted">
              共 {eventTotal} 条{eventsQuery.isFetching ? " · 刷新中…" : ""}
            </span>
          </div>

          {eventsQuery.isError ? (
            <ErrorState
              title="读取角色事件失败"
              message={errorOf(eventsQuery.error).message}
              issues={errorOf(eventsQuery.error).issues}
              onRetry={() => void eventsQuery.refetch()}
            />
          ) : eventsQuery.isPending ? (
            <LoadingState label="正在读取角色事件…" />
          ) : (
            <>
              <Table<EventRow>
                columns={eventColumns}
                rows={eventItems}
                rowKey={(row) => row.id}
                empty={
                  <div className="admin-table-empty">
                    没有符合条件的事件。世界还没跑过，或者筛选太窄——试试清掉筛选。
                  </div>
                }
              />
              <Pager
                total={eventTotal}
                offset={offset}
                limit={PAGE_SIZE}
                onChange={setOffset}
              />
            </>
          )}
        </>
      ) : (
        <>
          <div className="admin-row">
            <span className="admin-muted">状态</span>
            <Segmented<string>
              value={poolStatus}
              onChange={setPoolStatus}
              options={[
                { value: "", label: "全部" },
                { value: "open", label: "开放中" },
                { value: "filled", label: "已领满" },
                { value: "closed", label: "已关闭" },
              ]}
            />
            <span className="admin-muted">共 {pools.length} 个公共事件</span>
            <div style={{ marginLeft: "auto" }} className="admin-row">
              <Button
                variant="primary"
                onClick={() => {
                  setFormError("");
                  setForm({ ...EMPTY_POOL_FORM });
                }}
              >
                ＋ 新建公共事件
              </Button>
            </div>
          </div>

          {poolQuery.isError ? (
            <ErrorState
              title="读取公共事件池失败"
              message={errorOf(poolQuery.error).message}
              issues={errorOf(poolQuery.error).issues}
              onRetry={() => void poolQuery.refetch()}
            />
          ) : poolQuery.isPending ? (
            <LoadingState label="正在读取公共事件池…" />
          ) : pools.length === 0 ? (
            <EmptyState
              title="公共事件池是空的"
              description="公共事件是世界级的诱因：写一条（比如「城市半程马拉松」），角色会按职业 / 兴趣 / 城市 / 表达 DNA 的契合度来抢。先建一条试试预演。"
              action={
                <Button
                  variant="primary"
                  onClick={() => {
                    setFormError("");
                    setForm({ ...EMPTY_POOL_FORM });
                  }}
                >
                  ＋ 新建公共事件
                </Button>
              }
            />
          ) : (
            <div className="admin-col" style={{ gap: "var(--admin-gap-lg)" }}>
              {pools.map((item) => {
                const claims = item.claims ?? [];
                const slots = typeof item.slots === "number" ? item.slots : 0;
                const filled = item.status === "filled" || (slots > 0 && claims.length >= slots);
                const status = filled ? "filled" : item.status ?? "open";
                const ratio = slots > 0 ? Math.min(100, Math.round((claims.length / slots) * 100)) : 0;
                const previewOpen = openPreviewId === item.id;

                return (
                  <div className="admin-card" key={item.id}>
                    <div className="admin-card-head" style={{ flexWrap: "wrap" }}>
                      <h2 className="admin-card-title">{item.title || item.id}</h2>
                      {item.event_type ? (
                        <Badge tone="info">
                          {typeLabelMap[item.event_type] || item.event_type}
                        </Badge>
                      ) : null}
                      <Badge tone={POOL_STATUS_TONE[status]}>
                        {POOL_STATUS_LABEL[status] || status}
                      </Badge>
                      {/* 同时露出状态码：运营和客服沟通时说的是 filled/open，不是中文标签 */}
                      <span className="admin-mono admin-muted">{status}</span>
                      <span className="admin-card-sub admin-mono">{item.id}</span>
                      <div style={{ marginLeft: "auto" }} className="admin-row">
                        <Button
                          size="sm"
                          variant={previewOpen ? "default" : "primary"}
                          onClick={() => openPreview(item.id)}
                        >
                          {previewOpen ? "重新预演" : "预演谁会领"}
                        </Button>
                        <Button
                          size="sm"
                          onClick={() => {
                            setFormError("");
                            setForm(poolToForm(item));
                          }}
                        >
                          编辑
                        </Button>
                        <Button size="sm" variant="danger" onClick={() => setDeleteTarget(item)}>
                          删除
                        </Button>
                      </div>
                    </div>

                    {item.description ? (
                      <p className="admin-muted" style={{ marginTop: 0 }}>
                        {item.description}
                      </p>
                    ) : null}

                    <div className="admin-row">
                      <span className="admin-muted">名额</span>
                      <span className="admin-mono">
                        {claims.length} / {slots || "不限"}
                      </span>
                      <div
                        style={{
                          flex: "1 1 120px",
                          minWidth: 80,
                          height: 6,
                          borderRadius: 999,
                          background: "var(--md-surface-container-high, #e6e5ee)",
                          overflow: "hidden",
                        }}
                      >
                        <div
                          style={{
                            width: `${ratio}%`,
                            height: "100%",
                            background: filled
                              ? "var(--md-primary, #4c5e8b)"
                              : "var(--md-secondary, #dce2f9)",
                          }}
                        />
                      </div>
                      {filled ? <Badge tone="success">已领满</Badge> : null}
                    </div>

                    <div className="admin-row">
                      <span className="admin-muted">时间</span>
                      <span className="admin-mono">{formatWhen(item.start_at)}</span>
                      <span className="admin-muted">地点</span>
                      <span>{item.location || "—"}</span>
                      <span className="admin-muted">创建</span>
                      <span className="admin-mono">{formatWhen(item.created_at)}</span>
                      {describeConditions(item.conditions) ? (
                        <>
                          <span className="admin-muted">条件</span>
                          <span
                            className="admin-mono admin-truncate"
                            style={{ maxWidth: 420 }}
                            title={describeConditions(item.conditions)}
                          >
                            {describeConditions(item.conditions)}
                          </span>
                        </>
                      ) : null}
                    </div>

                    {claims.length ? (
                      <div className="admin-row">
                        <span className="admin-muted">已领取</span>
                        {claims.map((claim) => (
                          <span
                            className="admin-tag"
                            key={`${claim.persona_id}-${String(claim.claimed_at ?? "")}`}
                            title={claim.persona_id}
                          >
                            {claim.persona_nickname || claim.persona_id}
                            {typeof claim.score === "number" ? ` · ${formatScore(claim.score)}` : ""}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <div className="admin-muted" style={{ fontSize: 12 }}>
                        还没有角色领取。点「预演谁会领」看看这份条件会吸引谁。
                      </div>
                    )}

                    {previewOpen ? (
                      <div style={{ marginTop: "var(--admin-gap)" }}>
                        <div className="admin-divider" />
                        <div className="admin-card-head" style={{ marginTop: "var(--admin-gap)" }}>
                          <h3 className="admin-card-title">预演结果</h3>
                          <span className="admin-card-sub">
                            取前 {PREVIEW_LIMIT} 名 · 只做评分、不写库
                          </span>
                          <div style={{ marginLeft: "auto" }} className="admin-row">
                            <Button
                              size="sm"
                              loading={previewQuery.isFetching}
                              onClick={() => void previewQuery.refetch()}
                            >
                              重新计算
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => {
                                setOpenPreviewId(null);
                                setSelectedIds([]);
                              }}
                            >
                              收起
                            </Button>
                          </div>
                        </div>

                        {previewQuery.isError ? (
                          <ErrorState
                            title="预演失败"
                            message={errorOf(previewQuery.error).message}
                            issues={errorOf(previewQuery.error).issues}
                            onRetry={() => void previewQuery.refetch()}
                          />
                        ) : previewQuery.isPending ? (
                          <LoadingState label="正在计算谁会领…" />
                        ) : previewCandidates.length === 0 ? (
                          <EmptyState
                            title="没有角色会领这条事件"
                            description="可能是名额为 0、池子已关闭，或者当前没有可用角色。先确认池子状态与角色数量。"
                          />
                        ) : (
                          <>
                            <div className="admin-col" style={{ gap: 6 }}>
                              {previewCandidates.map((candidate, index) => (
                                <div
                                  key={candidate.persona_id}
                                  style={{
                                    border: "1px solid var(--md-outline-variant, #c6c6d0)",
                                    borderRadius: "var(--admin-radius-sm, 6px)",
                                    padding: "8px 12px",
                                    background: "var(--md-surface-container-low, #f4f3fa)",
                                  }}
                                >
                                  <div className="admin-row">
                                    <label className="admin-row" style={{ gap: 6, cursor: "pointer" }}>
                                      <input
                                        type="checkbox"
                                        checked={selectedIds.includes(candidate.persona_id)}
                                        onChange={() => toggleCandidate(candidate.persona_id)}
                                      />
                                      <span style={{ fontWeight: 550 }}>
                                        #{index + 1} {candidate.nickname || candidate.persona_id}
                                      </span>
                                    </label>
                                    <span className="admin-mono admin-muted">
                                      {candidate.persona_id}
                                    </span>
                                    <span
                                      className="admin-mono"
                                      style={{ marginLeft: "auto", fontWeight: 600 }}
                                    >
                                      总分 {formatScore(candidate.score)}
                                    </span>
                                  </div>
                                  <div style={{ marginTop: 6 }}>
                                    <ScoreBreakdown detail={candidate.score_detail} />
                                  </div>
                                </div>
                              ))}
                            </div>

                            <div className="admin-row" style={{ marginTop: 12 }}>
                              <Button
                                variant="primary"
                                loading={claimPool.isPending}
                                disabled={previewSelected.length === 0}
                                onClick={() =>
                                  claimPool.mutate({
                                    id: item.id,
                                    personaIds: previewSelected,
                                  })
                                }
                              >
                                让选中的领取（{previewSelected.length}）
                              </Button>
                              <Button
                                loading={claimPool.isPending}
                                title="不指定角色，由后端按评分自行挑选"
                                onClick={() => claimPool.mutate({ id: item.id, personaIds: [] })}
                              >
                                按排名自动领取
                              </Button>
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => setSelectedIds([])}
                                disabled={!previewSelected.length}
                              >
                                清空勾选
                              </Button>
                              <span className="admin-muted">
                                领取会写{" "}
                                <span className="admin-mono">event_pool_claims</span> 并给每个角色生成一条事件；
                                已领过的角色会被跳过（幂等）。
                              </span>
                            </div>
                          </>
                        )}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      {form ? (
        <Drawer
          open
          title={form.id ? "编辑公共事件" : "新建公共事件"}
          onClose={() => {
            setForm(null);
            setFormError("");
          }}
          footer={
            <>
              <Button
                onClick={() => {
                  setForm(null);
                  setFormError("");
                }}
                disabled={createPool.isPending || savePool.isPending}
              >
                取消
              </Button>
              <Button
                variant="primary"
                loading={createPool.isPending || savePool.isPending}
                onClick={() => submitPool(form)}
              >
                {form.id ? "保存修改" : "创建"}
              </Button>
            </>
          }
        >
          {formError ? <ErrorState title="保存失败" message={formError} /> : null}

          <Field label="标题" hint="运营一眼能看懂的事件名">
            <TextInput
              value={form.title}
              onChange={(value) => setForm((current) => (current ? { ...current, title: value } : current))}
              placeholder="例如：城市半程马拉松"
            />
          </Field>

          <Field label="描述" hint="会写进角色看到的事件上下文">
            <TextArea
              value={form.description}
              onChange={(value) =>
                setForm((current) => (current ? { ...current, description: value } : current))
              }
              rows={4}
              placeholder="周末清晨，沿江步道封路办半马，报名的人不少。"
            />
          </Field>

          <div className="admin-form-grid">
            <Field label="事件类型">
              <Select<string>
                value={form.event_type}
                onChange={(value) =>
                  setForm((current) => (current ? { ...current, event_type: value } : current))
                }
                options={[{ value: "", label: "选择类型…" }, ...eventTypeOptions]}
              />
            </Field>

            <Field label="名额" hint="最多几个角色能领到（决定 filled 的时机）">
              <TextInput
                value={form.slots}
                onChange={(value) =>
                  setForm((current) => (current ? { ...current, slots: value } : current))
                }
                placeholder="3"
                type="number"
              />
            </Field>

            <Field label="开始时间" hint="ISO 时间或 YYYY-MM-DD HH:mm；留空=不限">
              <TextInput
                value={form.start_at}
                onChange={(value) =>
                  setForm((current) => (current ? { ...current, start_at: value } : current))
                }
                placeholder="2026-11-15 07:30"
              />
            </Field>

            <Field label="地点" hint="城市 / 场所">
              <TextInput
                value={form.location}
                onChange={(value) =>
                  setForm((current) => (current ? { ...current, location: value } : current))
                }
                placeholder="杭州 · 滨江步道"
              />
            </Field>

            {form.id ? (
              <Field label="状态" hint="filled 由名额自动决定，不用手动设">
                <Select<string>
                  value={form.status}
                  onChange={(value) =>
                    setForm((current) => (current ? { ...current, status: value } : current))
                  }
                  options={[
                    { value: "open", label: "开放中" },
                    { value: "closed", label: "已关闭" },
                    { value: "filled", label: "已领满" },
                  ]}
                />
              </Field>
            ) : null}
          </div>

          <Field
            label="条件（JSON，可留空）"
            hint="评分维度从角色数据取；这里只放额外的硬条件。原样往返，不在前端发明结构。"
          >
            <TextArea
              value={form.conditionsText}
              onChange={(value) =>
                setForm((current) => (current ? { ...current, conditionsText: value } : current))
              }
              rows={5}
              mono
              placeholder={'{\n  "city": "杭州"\n}'}
            />
          </Field>
        </Drawer>
      ) : null}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="删除这条公共事件？"
        body={
          <div className="admin-col">
            <span>
              会删除 <span className="admin-mono">{deleteTarget?.title || deleteTarget?.id}</span>
              ，其领取记录一并删除（外键 CASCADE）。
            </span>
            <span className="admin-muted">已经生成的角色事件不会被回收——那是真实发生过的事。</span>
          </div>
        }
        confirmLabel="删除"
        danger
        busy={deletePool.isPending}
        onConfirm={() => {
          if (deleteTarget) deletePool.mutate(deleteTarget.id);
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </section>
  );
}
