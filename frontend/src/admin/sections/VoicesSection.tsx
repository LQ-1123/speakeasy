"use client";

/**
 * 音色池（`#/personas/voices`）：Fish 音色素材池的查看 / 检测 / 加入 / 试听 / 移除。
 *
 * 从旧管理台（`backend/app/admin/page.py` 的 `renderVoicePoolView`）平移，并修掉旧版的体验问题：
 *
 * - **移除前提示受影响角色数**。旧版 `vpRemove` 只弹一句"已分配给人物角色的声音不会变"，
 *   运营根本不知道有多少角色在用这个音色。现在确认框里会明确写出"N 个角色正在使用"，
 *   并说清移除后的真实后果（素材池不再包含它；重新分配音色时这些角色会被换走）。
 *
 * 接口清单（权威形状见 `backend/app/admin/voice_pool.py`）：
 * - `GET  /api/admin/voice-pool` → `{fish:{configured,usable,status}, voices:[{reference_id,name,gender,language,source,added_at,assigned}], assigned:{rid:{count}}, voice_gaps:{waiting_voice,gaps}}`
 * - `GET  /api/admin/voice-pool/models?refresh=1` → `{ok, models:[{id,title,description,languages,tags,cover}], count}`
 * - `POST /api/admin/voice-pool/validate` `{reference_id}` → `{ok, reference_id, bytes, media, message}`
 * - `POST /api/admin/voice-pool/add` `{reference_id, name, gender, language, source?, skip_validate?}`
 * - `POST /api/admin/voice-pool/remove` `{reference_id}` → `{ok, voices}`
 * - `POST /api/admin/voice-pool/resolve` `{q}` → `{ok, match, resolved_id}`（把"名称"解析成 reference_id）
 * - `POST /api/admin/voice-pool/preview` `{reference_id}` → **mp3 二进制**
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AdminApiError, apiGet, apiPost, requestBlobPost, withQuery } from "../api";
import { adminKeys } from "../queryKeys";
import {
  AudioPreview,
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Select,
  Table,
  TextInput,
  ToastStack,
  useToasts,
} from "../ui";
import type { Column } from "../ui";

/* -------------------------------------------------------------------------- */
/* 后端数据形状                                                                */
/* -------------------------------------------------------------------------- */

interface PoolVoice {
  reference_id: string;
  name?: string;
  gender?: string;
  language?: string;
  source?: string;
  added_at?: string;
  /** `status()` 里逐个补上的"被几个角色使用"。 */
  assigned?: number;
}

/** `voice_registry.fish_status()` 的诊断快照（不含密钥）。 */
interface FishStatus {
  reachable?: boolean;
  retry_in_s?: number;
  last_ok_at?: number;
  last_error?: string;
  attempt_ok?: boolean | null;
}

interface VoicePoolPayload {
  fish?: { configured?: boolean; usable?: boolean; status?: FishStatus };
  voices?: PoolVoice[];
  assigned?: Record<string, { count?: number }>;
  voice_gaps?: { waiting_voice?: number; gaps?: unknown[] };
}

/** `fish_tts.list_models()` 的行。 */
interface FishModel {
  id: string;
  title?: string;
  description?: string;
  languages?: string[];
  tags?: string[];
  cover?: string;
  total?: number;
}

interface ModelsPayload {
  ok?: boolean;
  issues?: string[];
  models?: FishModel[];
  count?: number;
}

/* -------------------------------------------------------------------------- */
/* 小工具                                                                      */
/* -------------------------------------------------------------------------- */

type ApiRecord = Record<string, unknown>;

/**
 * queryKeys.ts 里没有「Fish 账号全量音色」的 key（该文件只读），这里就地定义。
 * 故意**不**复用 `adminKeys.voices()` 前缀：音色池增删后要失效的是池数据，
 * 不应连带把 Fish 全量列表（一次最多 10 页外部请求）重新拉一遍。
 */
const FISH_MODELS_KEY = ["admin", "voice-pool", "fish-models"] as const;

function asText(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function asNumber(value: unknown): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function issueText(issues: unknown): string {
  const list = Array.isArray(issues) ? issues.map((item) => String(item)) : [];
  return list.length ? list.join("；") : "操作失败（后端未返回原因）";
}

function errorMessage(error: unknown): string {
  if (error instanceof AdminApiError) {
    return error.issues.join("；") || error.detail || error.message;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/** 业务失败（HTTP 200 + `ok:false`）也当异常抛，统一走 onError。 */
function ensureOk<T extends ApiRecord>(result: T): T {
  if (result.ok === false) throw new Error(issueText(result.issues));
  return result;
}

function formatBytes(value: unknown): string {
  const bytes = asNumber(value);
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

/**
 * 试听要 POST 拿 mp3：`/api/admin/voice-pool/preview` 只接受 POST + JSON body。
 * POST-blob 的鉴权语义（CSRF 头 + 403 重试）统一由 `api.ts::requestBlobPost` 提供，
 * 这里只是绑定路径与参数——不要在组件里重写一遍 CSRF 逻辑。
 */
function requestPreviewBlob(referenceId: string): Promise<Blob> {
  return requestBlobPost("/api/admin/voice-pool/preview", { reference_id: referenceId });
}

/* -------------------------------------------------------------------------- */
/* 选项                                                                        */
/* -------------------------------------------------------------------------- */

const GENDER_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "不限" },
  { value: "female", label: "女声" },
  { value: "male", label: "男声" },
];

/** 与 `voice_pool._SAMPLES` 覆盖的语言保持一致（检测/试听按语言选例句）。 */
const LANGUAGE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "不限" },
  { value: "en", label: "en · 英语" },
  { value: "zh", label: "zh · 中文" },
  { value: "ja", label: "ja · 日语" },
  { value: "ko", label: "ko · 韩语" },
  { value: "de", label: "de · 德语" },
  { value: "es", label: "es · 西班牙语" },
  { value: "fr", label: "fr · 法语" },
  { value: "pt", label: "pt · 葡萄牙语" },
  { value: "it", label: "it · 意大利语" },
  { value: "ar", label: "ar · 阿拉伯语" },
  { value: "ru", label: "ru · 俄语" },
];

/* -------------------------------------------------------------------------- */
/* 局部展示组件                                                                */
/* -------------------------------------------------------------------------- */

function Stat({ label, value, hint }: { label: string; value: number | string; hint?: string }) {
  return (
    <div className="admin-stat">
      <div className="admin-stat-label">{label}</div>
      <div className="admin-stat-value">{value}</div>
      {hint ? <div className="admin-stat-hint">{hint}</div> : null}
    </div>
  );
}

function genderLabel(gender: string): string {
  if (gender === "female") return "女";
  if (gender === "male") return "男";
  return "—";
}

/** 从 Fish 模型行推断"首个语言 + 标签里的性别"（与旧页 `vpAddFromModel` 同规则）。 */
function modelHints(model: FishModel): { language: string; gender: string } {
  const first = (model.languages ?? [])[0];
  const language = asText(first).toLowerCase().slice(0, 2);
  const tags = (model.tags ?? []).map((tag) => asText(tag).toLowerCase());
  if (tags.includes("female")) return { language, gender: "female" };
  if (tags.includes("male")) return { language, gender: "male" };
  return { language, gender: "" };
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function VoicesSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();

  const [refId, setRefId] = useState("");
  const [name, setName] = useState("");
  const [gender, setGender] = useState("");
  const [language, setLanguage] = useState("");
  /** 已通过真实合成检测的 reference_id；用它决定加入时是否还能省掉二次检测。 */
  const [validatedRef, setValidatedRef] = useState("");
  const [validatedInfo, setValidatedInfo] = useState<{
    reference_id: string;
    bytes: number;
    media: string;
    message: string;
  } | null>(null);

  const [modelsWanted, setModelsWanted] = useState(false);
  const [modelFilter, setModelFilter] = useState("");
  const [removeTarget, setRemoveTarget] = useState<PoolVoice | null>(null);

  /* ------------------------------ 查询 ------------------------------ */

  const poolQuery = useQuery({
    queryKey: adminKeys.voices(),
    queryFn: () => apiGet<VoicePoolPayload>("/api/admin/voice-pool"),
  });

  const modelsQuery = useQuery({
    queryKey: FISH_MODELS_KEY,
    queryFn: () => apiGet<ModelsPayload>(withQuery("/api/admin/voice-pool/models", { refresh: 1 })),
    enabled: modelsWanted,
    staleTime: 5 * 60_000,
  });

  /* ------------------------------ 写操作 ------------------------------ */

  /**
   * 检测并试听：先 `resolve` 把"名称"解析成 reference_id（能顺手显示池里匹配到哪一条），
   * 再 `validate` 用该音色真实合成一句。
   */
  const validateMutation = useMutation({
    mutationFn: async (query: string) => {
      const resolved = await apiPost<ApiRecord>("/api/admin/voice-pool/resolve", { q: query });
      const resolvedId = asText(resolved.resolved_id) || query;
      const match =
        resolved.match && typeof resolved.match === "object"
          ? (resolved.match as ApiRecord)
          : null;
      const result = ensureOk(
        await apiPost<ApiRecord>("/api/admin/voice-pool/validate", { reference_id: resolvedId }),
      );
      return { resolvedId, match, result };
    },
    onSuccess: ({ resolvedId, match, result }) => {
      setValidatedRef(resolvedId);
      setValidatedInfo({
        reference_id: asText(result.reference_id) || resolvedId,
        bytes: asNumber(result.bytes),
        media: asText(result.media, "audio/mpeg"),
        message: asText(result.message, "检测成功"),
      });
      // 解析到池里已有条目时，顺手把名称/性别/语言补上（不覆盖用户已填的内容）。
      if (match) {
        setRefId(resolvedId);
        setName((current) => current.trim() || asText(match.name));
        setGender((current) => current || asText(match.gender).toLowerCase());
        setLanguage((current) => current || asText(match.language).toLowerCase());
      }
      push(`检测通过：${asText(result.reference_id) || resolvedId}（${formatBytes(result.bytes)}）`);
    },
    onError: (error) => {
      setValidatedRef("");
      setValidatedInfo(null);
      push(`检测失败：${errorMessage(error)}`, "error");
    },
  });

  interface AddVoiceInput {
    reference_id: string;
    name: string;
    gender: string;
    language: string;
    source: string;
    skip_validate?: boolean;
  }

  const addMutation = useMutation({
    mutationFn: async (input: AddVoiceInput) =>
      ensureOk(await apiPost<ApiRecord>("/api/admin/voice-pool/add", input)),
    onSuccess: (result, input) => {
      void queryClient.invalidateQueries({ queryKey: adminKeys.voices() });
      const updated = result.updated === true;
      push(
        `${updated ? "已更新池内已有音色" : "已加入素材池"}：${
          input.name || input.reference_id
        }（新捏的角色会优先从这里挑音色）`,
      );
      setValidatedInfo(null);
      setValidatedRef("");
      setRefId("");
      setName("");
      setGender("");
      setLanguage("");
    },
    onError: (error) => push(`加入失败：${errorMessage(error)}`, "error"),
  });

  const removeMutation = useMutation({
    mutationFn: async (referenceId: string) =>
      ensureOk(
        await apiPost<ApiRecord>("/api/admin/voice-pool/remove", { reference_id: referenceId }),
      ),
    onSuccess: (_result, referenceId) => {
      setRemoveTarget(null);
      void queryClient.invalidateQueries({ queryKey: adminKeys.voices() });
      push(`已从素材池移除：${referenceId}`);
    },
    onError: (error) => {
      setRemoveTarget(null);
      push(`移除失败：${errorMessage(error)}`, "error");
    },
  });

  /* ------------------------------ 交互 ------------------------------ */

  const submitValidate = () => {
    const query = refId.trim();
    if (!query) {
      push("先填写 Fish 音色 id 或素材池里的名称", "error");
      return;
    }
    validateMutation.mutate(query);
  };

  const submitAdd = () => {
    const query = refId.trim();
    if (!query) {
      push("先填写 Fish 音色 id 或素材池里的名称", "error");
      return;
    }
    const effectiveId = validatedInfo ? validatedInfo.reference_id : query;
    addMutation.mutate({
      reference_id: effectiveId,
      name: name.trim(),
      gender,
      language,
      source: "manual",
      // 刚用同一个 id 检测通过 → 让后端跳过二次合成（省一次 Fish 调用）。
      skip_validate: Boolean(validatedRef) && validatedRef === effectiveId,
    });
  };

  const addFromModel = (model: FishModel) => {
    const hints = modelHints(model);
    addMutation.mutate({
      reference_id: model.id,
      name: asText(model.title) || model.id.slice(0, 12),
      gender: hints.gender,
      language: hints.language,
      source: "fish-model",
    });
  };

  /* ------------------------------ 派生数据 ------------------------------ */

  const poolData = poolQuery.data;
  const voices = poolData?.voices ?? [];
  const assigned = poolData?.assigned ?? {};
  const fish = poolData?.fish ?? {};
  const fishStatus: FishStatus = fish.status ?? {};
  const waitingVoice = asNumber(poolData?.voice_gaps?.waiting_voice);

  const poolIds = new Set(voices.map((voice) => voice.reference_id));
  const assignedTotal = Object.values(assigned).reduce(
    (total, entry) => total + asNumber((entry || {}).count),
    0,
  );
  const poolInUse = voices.filter(
    (voice) => asNumber(voice.assigned) > 0 || asNumber((assigned[voice.reference_id] || {}).count) > 0,
  ).length;
  /** 仍被角色使用、但已经不在池里的音色（旧版静默移除留下的「孤儿」）。 */
  const orphanIds = Object.keys(assigned).filter(
    (referenceId) => referenceId && !poolIds.has(referenceId) && asNumber((assigned[referenceId] || {}).count) > 0,
  );

  const fishStateLabel = fish.usable
    ? "可用"
    : fish.configured
      ? `冷却中${fishStatus.retry_in_s ? `（约 ${Math.ceil(asNumber(fishStatus.retry_in_s))}s 后重试）` : ""}`
      : "未配置 / provider 不是 fish";

  const allModels = modelsQuery.data?.models ?? [];
  const keyword = modelFilter.trim().toLowerCase();
  const filteredModels = keyword
    ? allModels.filter((model) =>
        [model.title, model.id, ...(model.languages ?? []), ...(model.tags ?? [])]
          .map((item) => asText(item))
          .join(" ")
          .toLowerCase()
          .includes(keyword),
      )
    : allModels;
  const visibleModels = filteredModels.slice(0, 200);

  const affectedCount = removeTarget
    ? asNumber(removeTarget.assigned) ||
      asNumber((assigned[removeTarget.reference_id] || {}).count)
    : 0;

  const poolColumns: Array<Column<PoolVoice>> = [
    {
      key: "name",
      header: "音色",
      render: (voice) => (
        <div className="admin-col" style={{ gap: 2 }}>
          <span>{voice.name || voice.reference_id}</span>
          <span className="admin-mono admin-muted">
            {voice.reference_id}
            {voice.source ? ` · ${voice.source}` : ""}
          </span>
        </div>
      ),
    },
    { key: "gender", header: "性别", render: (voice) => genderLabel(asText(voice.gender)) },
    { key: "language", header: "语言", render: (voice) => asText(voice.language) },
    {
      key: "assigned",
      header: "被角色使用",
      numeric: true,
      render: (voice) =>
        asNumber(voice.assigned) || asNumber((assigned[voice.reference_id] || {}).count),
    },
    {
      key: "actions",
      header: "操作",
      render: (voice) => (
        <div className="admin-row">
          <AudioPreview
            label="试听"
            load={() => requestPreviewBlob(voice.reference_id)}
          />
          <Button size="sm" variant="danger" onClick={() => setRemoveTarget(voice)}>
            移除
          </Button>
        </div>
      ),
    },
  ];

  const modelColumns: Array<Column<FishModel>> = [
    {
      key: "title",
      header: "音色",
      render: (model) => (
        <div className="admin-col" style={{ gap: 2 }}>
          <span>{model.title || "（未命名）"}</span>
          <span className="admin-mono admin-muted">{model.id}</span>
          {model.description ? (
            <span className="admin-muted">{model.description}</span>
          ) : null}
        </div>
      ),
    },
    {
      key: "languages",
      header: "语言",
      render: (model) => (model.languages ?? []).map((item) => asText(item)).join(" / "),
    },
    {
      key: "tags",
      header: "标签",
      render: (model) => (model.tags ?? []).map((item) => asText(item)).join(","),
    },
    {
      key: "actions",
      header: "操作",
      render: (model) => (
        <div className="admin-row">
          <AudioPreview label="试听" load={() => requestPreviewBlob(model.id)} />
          <Button
            size="sm"
            variant={poolIds.has(model.id) ? "default" : "primary"}
            loading={addMutation.isPending && addMutation.variables?.reference_id === model.id}
            onClick={() => addFromModel(model)}
          >
            {poolIds.has(model.id) ? "更新池内信息" : "加入池"}
          </Button>
        </div>
      ),
    },
  ];

  /* ------------------------------ 渲染 ------------------------------ */

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">音色池</h1>
          <p className="admin-section-desc">
            捏人时优先从这里挑音色：按语言 + 性别匹配。加入池前会用 Fish 真实合成一句做检测，
            池里的每个音色都能试听。
          </p>
        </div>
        <div className="admin-section-actions">
          <Badge tone={fish.usable ? "success" : fish.configured ? "warn" : "error"}>
            Fish {fishStateLabel}
          </Badge>
          <Button onClick={() => void poolQuery.refetch()} disabled={poolQuery.isFetching}>
            {poolQuery.isFetching ? "刷新中…" : "刷新"}
          </Button>
        </div>
      </div>

      {poolQuery.isError ? (
        <ErrorState
          title="读取音色池失败"
          message={errorMessage(poolQuery.error)}
          issues={poolQuery.error instanceof AdminApiError ? poolQuery.error.issues : undefined}
          onRetry={() => void poolQuery.refetch()}
        />
      ) : poolQuery.isPending ? (
        <LoadingState label="正在读取音色池…" />
      ) : (
        <>
          <div className="admin-stat-grid">
            <Stat label="池内音色" value={voices.length} hint="捏人时优先使用" />
            <Stat label="池内使用中" value={poolInUse} hint="至少被 1 个角色使用" />
            <Stat label="已分配 Fish 音色的角色" value={assignedTotal} />
            <Stat
              label="待验证角色"
              value={waitingVoice}
              hint="status=waiting_voice，音色还没通过验证"
            />
            <Stat
              label="池外仍在用的音色"
              value={orphanIds.length}
              hint={
                orphanIds.length
                  ? "这些音色已被移出池但仍被角色引用，建议加回或重新分配"
                  : "没有孤儿音色"
              }
            />
          </div>

          {orphanIds.length ? (
            <div className="admin-card">
              <div className="admin-card-head">
                <Badge tone="warn">需处理</Badge>
                <span className="admin-card-sub">
                  有 {orphanIds.length} 个音色不在池里，但仍被角色使用
                </span>
              </div>
              <p className="admin-mono admin-muted" style={{ margin: 0 }}>
                {orphanIds
                  .map((referenceId) => `${referenceId}（${asNumber((assigned[referenceId] || {}).count)} 个角色）`)
                  .join(" · ")}
              </p>
              <p className="admin-muted" style={{ margin: 0 }}>
                把它们重新加入池子（填 id → 检测 → 加入），或者在「角色工坊 → 重新分配音色」里
                把这些角色换到池内音色。
              </p>
            </div>
          ) : null}

          {/* ---------- 手动添加 ---------- */}
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">手动添加音色</h2>
              <span className="admin-card-sub">
                输入 Fish 的 reference_id，或素材池里已有的名称
              </span>
            </div>
            <div className="admin-form-grid">
              <Field
                label="音色 id"
                htmlFor="vp-ref"
                hint="Fish 平台的音色 model id（reference_id）"
              >
                <TextInput
                  id="vp-ref"
                  value={refId}
                  onChange={(value) => {
                    setRefId(value);
                    // 换了 id 之后，之前的检测结果就不再代表它了。
                    if (validatedRef && value.trim() !== validatedRef) {
                      setValidatedRef("");
                      setValidatedInfo(null);
                    }
                  }}
                  placeholder="例如 7f92c1ab…"
                  onEnter={submitValidate}
                />
              </Field>
              <Field label="名称" htmlFor="vp-name" hint="给这个音色起个名字（可选）">
                <TextInput
                  id="vp-name"
                  value={name}
                  onChange={setName}
                  placeholder="例如 Emma · 温暖女声"
                />
              </Field>
              <Field label="性别" htmlFor="vp-gender" hint="影响捏人时按性别匹配">
                <Select
                  id="vp-gender"
                  value={gender}
                  onChange={setGender}
                  options={GENDER_OPTIONS}
                />
              </Field>
              <Field label="语言" htmlFor="vp-language" hint="影响捏人时按语言匹配">
                <Select
                  id="vp-language"
                  value={language}
                  onChange={setLanguage}
                  options={LANGUAGE_OPTIONS}
                />
              </Field>
            </div>

            <div className="admin-row">
              <Button
                variant="primary"
                loading={validateMutation.isPending}
                onClick={submitValidate}
              >
                检测音色
              </Button>
              <Button loading={addMutation.isPending} onClick={submitAdd}>
                加入素材池
              </Button>
              <span className="admin-muted">
                检测会用该音色合成一句（需几十秒）；检测通过后再加入可省掉一次合成。
              </span>
            </div>

            {validatedInfo ? (
              <div className="admin-col">
                <hr className="admin-divider" />
                <div className="admin-row">
                  <Badge tone="success">检测通过</Badge>
                  <span className="admin-muted">{validatedInfo.message}</span>
                </div>
                <p className="admin-mono admin-muted" style={{ margin: 0 }}>
                  {validatedInfo.reference_id} · {formatBytes(validatedInfo.bytes)} ·{" "}
                  {validatedInfo.media}
                </p>
                <AudioPreview
                  label="试听检测音色"
                  load={() => requestPreviewBlob(validatedInfo.reference_id)}
                />
              </div>
            ) : null}
          </div>

          {/* ---------- 素材池 ---------- */}
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">素材池</h2>
              <span className="admin-card-sub">已加入 {voices.length} 个音色</span>
            </div>
            <Table<PoolVoice>
              columns={poolColumns}
              rows={voices}
              rowKey={(voice) => voice.reference_id}
              empty={
                <EmptyState
                  title="池里还没有音色"
                  description="上面填 reference_id 检测通过后加入，或从下面 Fish 账号的完整音色列表里挑一个「加入池」。"
                />
              }
            />
          </div>

          {/* ---------- Fish 账号全部音色 ---------- */}
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">Fish 账号全部音色</h2>
              <span className="admin-card-sub">
                {modelsWanted && modelsQuery.data
                  ? `已加载 ${asNumber(modelsQuery.data.count) || allModels.length} 个`
                  : "按需拉取（分页最多 1000 条）"}
              </span>
            </div>

            <div className="admin-row">
              <Button
                loading={modelsQuery.isFetching}
                onClick={() => {
                  if (!modelsWanted) {
                    setModelsWanted(true);
                    return;
                  }
                  void modelsQuery.refetch();
                }}
              >
                {modelsWanted ? "重新拉取 Fish 音色" : "加载 Fish 全部音色"}
              </Button>
              {modelsWanted ? (
                <div style={{ flex: "1 1 220px" }}>
                  <TextInput
                    value={modelFilter}
                    onChange={setModelFilter}
                    type="search"
                    placeholder="过滤：名称 / id / 语言 / 标签"
                  />
                </div>
              ) : null}
            </div>

            {!modelsWanted ? null : modelsQuery.isPending ? (
              <LoadingState label="正在拉取 Fish 账号下的音色…" />
            ) : modelsQuery.isError ? (
              <ErrorState
                title="拉取 Fish 音色失败"
                message={errorMessage(modelsQuery.error)}
                onRetry={() => void modelsQuery.refetch()}
              />
            ) : modelsQuery.data && modelsQuery.data.ok === false ? (
              <ErrorState
                title="拉取 Fish 音色失败"
                message={issueText(modelsQuery.data.issues)}
                onRetry={() => void modelsQuery.refetch()}
              />
            ) : (
              <>
                <Table<FishModel>
                  columns={modelColumns}
                  rows={visibleModels}
                  rowKey={(model) => model.id}
                  empty={
                    <EmptyState
                      title={keyword ? "没有匹配的音色" : "没有可用的音色"}
                      description={
                        keyword
                          ? "换个关键词，或清空过滤框。"
                          : "Fish 账号下没有返回任何音色；先到「API 与密钥」确认 Fish 已配置。"
                      }
                    />
                  }
                />
                {filteredModels.length > visibleModels.length ? (
                  <p className="admin-muted" style={{ margin: 0 }}>
                    仅显示前 {visibleModels.length} 个，用过滤框缩小范围后再挑选。
                  </p>
                ) : null}
              </>
            )}
          </div>
        </>
      )}

      {/* ---------- 移除确认：必须提示受影响角色数 ---------- */}
      <ConfirmDialog
        open={removeTarget !== null}
        title="从素材池移除这个音色？"
        danger
        confirmLabel="确认移除"
        busy={removeMutation.isPending}
        onConfirm={() => {
          if (removeTarget) removeMutation.mutate(removeTarget.reference_id);
        }}
        onCancel={() => {
          if (removeMutation.isPending) return;
          setRemoveTarget(null);
        }}
        body={
          removeTarget ? (
            <div className="admin-col">
              <p style={{ margin: 0 }}>
                音色 <strong>{removeTarget.name || removeTarget.reference_id}</strong>
              </p>
              <p className="admin-mono admin-muted" style={{ margin: 0 }}>
                {removeTarget.reference_id}
              </p>
              <p style={{ margin: 0 }}>
                该音色正被 <strong>{affectedCount}</strong> 个角色使用。
              </p>
              {affectedCount > 0 ? (
                <ul className="admin-muted" style={{ margin: 0, paddingLeft: 18 }}>
                  <li>这 {affectedCount} 个角色当前已设置的音色不会立刻改变（不会马上失声）</li>
                  <li>
                    但素材池里不再有它：新捏的角色不会再挑到它；
                    「角色工坊 → 重新分配音色」时这些角色会被换到池内其它音色
                  </li>
                  <li>以后要恢复，只能重新把它的 reference_id 加回池子</li>
                </ul>
              ) : (
                <p className="admin-muted" style={{ margin: 0 }}>
                  目前没有角色使用它，移除不会影响任何角色。
                </p>
              )}
              {removeTarget.assigned === undefined && affectedCount === 0 ? (
                <p className="admin-muted" style={{ margin: 0 }}>
                  注意：使用统计来自角色表的 fish_voice_id，若刚有角色换过音色，请先点右上角「刷新」再确认。
                </p>
              ) : null}
            </div>
          ) : null
        }
      />
    </section>
  );
}
