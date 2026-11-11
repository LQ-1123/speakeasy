"use client";

/**
 * 世界节奏（rhythm 词库）+ 事件类型（events 词库）+ 「模拟今天」。
 *
 * 关键修正（2026-11）
 * -------------------
 * `life_tick_s` / `posts_tick_s` / `posts_max_events` / `posts_since_hours` 以前**改了不生效**
 * （旧实现从 `os.getenv` 直接读，根本没读词库），旧页面标的「重启后生效」也是错的——
 * 重启同样不生效。现在这 4 个参数已接入 `lexicon.rhythm_params()`，维护循环每轮重新读取，
 * 所以**保存后下一轮就生效（约 1 分钟内）**，这里不再出现「重启生效」字样。
 *
 * 实现要点
 * --------
 * 1. 两个词库各自独立草稿 / 独立保存：调节奏不会顺手把事件类型一起 PUT。
 * 2. 滑杆实时改本地值，点保存才校验 + 落盘；校验失败原样展示 issues。
 * 3. 「模拟今天」是只读估算，读的是**已保存**的词库，所以提示先保存再看效果。
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AdminApiError, apiGet, apiPost, apiPut } from "../api";
import { adminKeys } from "../queryKeys";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Segmented,
  Slider,
  TagCloud,
  ToastStack,
  useToasts,
} from "../ui";

/* -------------------------------------------------------------------------- */
/* 数据形状                                                                    */
/* -------------------------------------------------------------------------- */

interface RhythmData {
  version?: number;
  params: Record<string, number>;
  note?: string;
}

type EventSource = "rule" | "narrative" | "both";
type Tier = "0" | "1" | "2" | "3";

interface EventTypeSpec {
  zh?: string;
  label?: string;
  value_tier?: number;
  source?: EventSource;
  share_weight?: number;
  show_title?: boolean;
  /** 旧字段名（部分历史数据/文档用它）；存在时和 show_title 同步写。 */
  show_content?: boolean;
  tags?: string[];
  enabled?: boolean;
}

interface EventsData {
  version?: number;
  types: Record<string, EventTypeSpec>;
  note?: string;
}

interface LexiconPayload<T> {
  name: string;
  title: string;
  exists: boolean;
  data: T;
  issues: string[];
  path: string;
}

interface SimulateResult {
  params: Record<string, number>;
  types: { total?: number; enabled?: number };
  world: { personas?: number; rule_types?: number; narrative_types?: number };
  today: { events?: Record<string, number>; posts?: number; comments?: number };
  pending: {
    eligible_events?: number;
    avg_share_prob?: number;
    quota?: number;
    expected_new_posts?: number;
    expected_new_comments?: number;
  };
  next_day: {
    rule_events_est?: number;
    narrative_events_est?: number;
    posts_quota?: number;
    events_total_est?: number;
  };
  assumptions: string[];
}

interface SaveOutcome {
  ok: boolean;
  issues: string[];
  message: string;
}

/* -------------------------------------------------------------------------- */
/* 节奏参数元数据                                                              */
/*                                                                             */
/* 范围逐条照抄 backend/app/v3/lexicon.py 的 RHYTHM_BOUNDS；                    */
/* 中文标签 / hint 照抄旧管理台 page.py 的 RHYTHM_META（只改了 tick 那两行的单位）*/
/* -------------------------------------------------------------------------- */

interface RhythmMeta {
  key: string;
  label: string;
  hint: string;
  min: number;
  max: number;
  step: number;
  unit?: string;
  /** 词库文件缺这个键时的回落值（与 lexicon.RHYTHM_DEFAULTS 一致）。 */
  fallback: number;
}

const RHYTHM_META: RhythmMeta[] = [
  {
    key: "rule_events_per_day",
    label: "每天每人规则事件",
    hint: "按作息自动产生，1–8 条",
    min: 1,
    max: 8,
    step: 1,
    unit: " 条",
    fallback: 4,
  },
  {
    key: "narrative_events_per_day",
    label: "每天每人叙事事件",
    hint: "0 = 关闭 LLM 叙事（省钱），最多 6",
    min: 0,
    max: 6,
    step: 1,
    unit: " 条",
    fallback: 2,
  },
  {
    key: "event_posts_per_day",
    label: "每日事件动态配额",
    hint: "全体总闸，1–30 条",
    min: 1,
    max: 30,
    step: 1,
    unit: " 条",
    fallback: 8,
  },
  {
    key: "tension_penalty_step",
    label: "张力惩罚步长",
    hint: "越大越记仇，0–0.5",
    min: 0,
    max: 0.5,
    step: 0.01,
    fallback: 0.18,
  },
  {
    key: "tension_penalty_floor",
    label: "张力惩罚下限",
    hint: "最低保留多少互动意愿，0–0.8",
    min: 0,
    max: 0.8,
    step: 0.01,
    fallback: 0.15,
  },
  {
    key: "comment_backfill_hours",
    label: "评论补位窗口",
    hint: "只给最近这么多小时的动态补评论",
    min: 0,
    max: 24,
    step: 0.5,
    unit: " 小时",
    fallback: 6,
  },
  {
    key: "life_tick_s",
    label: "生活事件 tick",
    hint: "多久跑一轮生活事件 · 保存后下一轮生效（约 1 分钟内）",
    min: 60,
    max: 3600,
    step: 30,
    unit: " 秒",
    fallback: 300,
  },
  {
    key: "posts_tick_s",
    label: "事件动态 tick",
    hint: "多久扫一轮待发事件 · 保存后下一轮生效（约 1 分钟内）",
    min: 60,
    max: 3600,
    step: 30,
    unit: " 秒",
    fallback: 240,
  },
  {
    key: "posts_max_events",
    label: "每轮最多处理事件",
    hint: "单轮批量上限",
    min: 1,
    max: 20,
    step: 1,
    unit: " 条",
    fallback: 4,
  },
  {
    key: "posts_since_hours",
    label: "只看最近几小时事件",
    hint: "避免把很旧的事件翻出来发",
    min: 1,
    max: 48,
    step: 1,
    unit: " 小时",
    fallback: 6,
  },
];

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

const SOURCE_ZH: Record<EventSource, string> = {
  rule: "规则事件",
  narrative: "叙事事件",
  both: "两者",
};

const SOURCE_TITLE: Record<EventSource, string> = {
  rule: "按作息自动产生（不需要 LLM）",
  narrative: "由 LLM 写故事性事件",
  both: "两者都会产生",
};

const TIER_OPTIONS: Array<{ value: Tier; label: string }> = [
  { value: "0", label: "低" },
  { value: "1", label: "★" },
  { value: "2", label: "★★" },
  { value: "3", label: "★★★" },
];

/* -------------------------------------------------------------------------- */
/* 小工具                                                                      */
/* -------------------------------------------------------------------------- */

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function errorOf(error: unknown): { message: string; issues: string[] } {
  if (error instanceof AdminApiError) {
    return { message: error.detail || error.message, issues: error.issues };
  }
  return { message: error instanceof Error ? error.message : String(error), issues: [] };
}

async function validateThenSave<T>(name: string, data: T): Promise<SaveOutcome> {
  const check = await apiPost<{ ok: boolean; issues: string[] }>(`/api/admin/lexicons/${name}/validate`, {
    data,
  });
  if (!check.ok) return { ok: false, issues: check.issues ?? [], message: "校验未通过" };
  const saved = await apiPut<{ ok: boolean; issues?: string[]; message?: string }>(
    `/api/admin/lexicons/${name}`,
    { data },
  );
  if (saved.ok === false) {
    return { ok: false, issues: saved.issues ?? [], message: saved.message || "保存被拒绝" };
  }
  return { ok: true, issues: [], message: "" };
}

function paramValue(
  params: Record<string, number> | undefined,
  meta: RhythmMeta,
): number {
  const raw = params?.[meta.key];
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  return meta.fallback;
}

function formatParam(meta: RhythmMeta) {
  return (value: number) => `${Number(value.toFixed(2))}${meta.unit ?? ""}`;
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div className="admin-stat">
      <div className="admin-stat-label">{label}</div>
      <div className="admin-stat-value">{value}</div>
      {hint ? <div className="admin-stat-hint">{hint}</div> : null}
    </div>
  );
}

export default function RhythmSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();
  const [resetTarget, setResetTarget] = useState<null | "rhythm" | "events">(null);
  const [issues, setIssues] = useState<string[]>([]);

  const rhythmQuery = useQuery({
    queryKey: adminKeys.lexicon("rhythm"),
    queryFn: () => apiGet<LexiconPayload<RhythmData>>("/api/admin/lexicons/rhythm"),
  });

  const eventsQuery = useQuery({
    queryKey: adminKeys.lexicon("events"),
    queryFn: () => apiGet<LexiconPayload<EventsData>>("/api/admin/lexicons/events"),
  });

  const rhythmServer = rhythmQuery.data?.data ?? null;
  const eventsServer = eventsQuery.data?.data ?? null;

  const [rhythmDraft, setRhythmDraft] = useState<RhythmData | null>(null);
  const [eventsDraft, setEventsDraft] = useState<EventsData | null>(null);

  useEffect(() => {
    if (rhythmServer) setRhythmDraft(clone(rhythmServer));
  }, [rhythmServer]);

  useEffect(() => {
    if (eventsServer) setEventsDraft(clone(eventsServer));
  }, [eventsServer]);

  // 只比较 10 个受管参数：词库里多出来的键不该把页面标成「有改动」。
  const rhythmDirty = useMemo(() => {
    if (!rhythmServer) return false;
    return RHYTHM_META.some(
      (meta) => paramValue(rhythmDraft?.params, meta) !== paramValue(rhythmServer.params, meta),
    );
  }, [rhythmDraft, rhythmServer]);

  const eventsDirty = useMemo(() => {
    if (!eventsServer || !eventsDraft) return false;
    return JSON.stringify(eventsDraft) !== JSON.stringify(eventsServer);
  }, [eventsDraft, eventsServer]);

  const invalidate = (name: string) => {
    void queryClient.invalidateQueries({ queryKey: adminKeys.lexicon(name) });
    void queryClient.invalidateQueries({ queryKey: adminKeys.lexicons() });
  };

  /** 刷新 = 丢弃本地改动重新读盘（结构共享下单纯 refetch 不会重置草稿）。 */
  const reload = async () => {
    setIssues([]);
    const [rhythm, events] = await Promise.all([rhythmQuery.refetch(), eventsQuery.refetch()]);
    if (rhythm.data) setRhythmDraft(clone(rhythm.data.data));
    if (events.data) setEventsDraft(clone(events.data.data));
  };

  /* ------------------------------ 保存 ------------------------------ */

  const saveRhythm = useMutation({
    mutationFn: () =>
      validateThenSave("rhythm", {
        ...(rhythmServer ?? {}),
        params: {
          // 保留词库里可能存在的额外参数，只覆盖这 10 个受管键。
          ...(rhythmServer?.params ?? {}),
          ...RHYTHM_META.reduce<Record<string, number>>((acc, meta) => {
            acc[meta.key] = paramValue(rhythmDraft?.params, meta);
            return acc;
          }, {}),
        },
      }),
    onSuccess: (result) => {
      if (!result.ok) {
        setIssues(result.issues);
        push(result.message, "error");
        return;
      }
      setIssues([]);
      push("节奏参数已保存：tick / 批量参数会在下一轮维护循环生效（约 1 分钟内）");
      invalidate("rhythm");
    },
    onError: (error) => {
      const info = errorOf(error);
      setIssues(info.issues);
      push(info.message, "error");
    },
  });

  const saveEvents = useMutation({
    mutationFn: () => validateThenSave("events", eventsDraft ?? {}),
    onSuccess: (result) => {
      if (!result.ok) {
        setIssues(result.issues);
        push(result.message, "error");
        return;
      }
      setIssues([]);
      push("事件类型已保存并生效");
      invalidate("events");
    },
    onError: (error) => {
      const info = errorOf(error);
      setIssues(info.issues);
      push(info.message, "error");
    },
  });

  const resetMutation = useMutation({
    mutationFn: (name: string) =>
      apiPost<{ ok: boolean; restored: string }>(`/api/admin/lexicons/${name}/reset`),
    onSuccess: (result, name) => {
      setResetTarget(null);
      setIssues([]);
      push(
        result.restored === "backup"
          ? `${name} 已回滚到上一版备份`
          : result.restored === "code-default"
            ? `${name} 已恢复代码默认值`
            : `${name} 没有可恢复的内容`,
      );
      invalidate(name);
    },
    onError: (error) => {
      setResetTarget(null);
      push(errorOf(error).message, "error");
    },
  });

  const simulate = useMutation({
    mutationFn: () => apiGet<SimulateResult>("/api/admin/simulate"),
    onError: (error) => push(`模拟失败：${errorOf(error).message}`, "error"),
  });

  /* ------------------------------ 局部更新 ------------------------------ */

  const setParam = (key: string, value: number) => {
    setRhythmDraft((current) => {
      if (!current) return current;
      const next = clone(current);
      next.params = next.params ?? {};
      next.params[key] = value;
      return next;
    });
  };

  const updateType = (key: string, mutate: (spec: EventTypeSpec) => void) => {
    setEventsDraft((current) => {
      if (!current) return current;
      const next = clone(current);
      const spec = next.types?.[key];
      if (!spec) return current;
      mutate(spec);
      return next;
    });
  };

  /* ------------------------------ 渲染 ------------------------------ */

  if (rhythmQuery.isPending || eventsQuery.isPending) {
    return <LoadingState label="正在读取世界节奏与事件类型…" />;
  }

  // 两个词库任一失败都算整页失败（节奏和事件在这里是一体两面）；
  // 用 `isError` 联合判断而不是 `!error`，这样 TS 能把 data 收窄成非空。
  if (rhythmQuery.isError || eventsQuery.isError) {
    const info = errorOf(rhythmQuery.error ?? eventsQuery.error);
    return (
      <ErrorState
        title="读取词库失败"
        message={info.message}
        issues={info.issues}
        onRetry={() => {
          void rhythmQuery.refetch();
          void eventsQuery.refetch();
        }}
      />
    );
  }

  const rhythmLib = rhythmQuery.data;
  const eventsLib = eventsQuery.data;
  const types = eventsDraft?.types ?? {};
  const typeKeys = Object.keys(types);
  const enabledTypes = typeKeys.filter((key) => types[key].enabled !== false);
  const hasRule = enabledTypes.some((key) => ["rule", "both"].includes(types[key].source ?? ""));
  const hasNarrative = enabledTypes.some((key) =>
    ["narrative", "both"].includes(types[key].source ?? ""),
  );
  const sim = simulate.data;

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">世界节奏</h1>
          <p className="admin-section-desc">
            这些参数决定世界活不活跃：每天多少事件、多少条动态、多久跑一轮。
            所有参数**保存后即生效**；tick 与批量参数在下一轮维护循环读取（约 1 分钟内），不需要重启。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button
            variant="primary"
            loading={simulate.isPending}
            onClick={() => simulate.mutate()}
          >
            {simulate.isPending ? "模拟中…" : "模拟今天"}
          </Button>
          <Button
            onClick={() => void reload()}
            disabled={rhythmQuery.isFetching || eventsQuery.isFetching}
          >
            {rhythmQuery.isFetching || eventsQuery.isFetching ? "刷新中…" : "刷新"}
          </Button>
        </div>
      </div>

      {issues.length ? <ErrorState title="校验未通过，未写入磁盘" issues={issues} /> : null}

      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">节奏参数</h2>
          <span className="admin-card-sub">
            {rhythmDirty ? "有未保存改动" : "已与磁盘一致"} · 拖动滑杆即可调整
          </span>
        </div>

        <div className="admin-col">
          {RHYTHM_META.map((meta) => (
            <Slider
              key={meta.key}
              label={meta.label}
              hint={meta.hint}
              min={meta.min}
              max={meta.max}
              step={meta.step}
              value={paramValue(rhythmDraft?.params, meta)}
              onChange={(value) => setParam(meta.key, value)}
              format={formatParam(meta)}
              disabled={!rhythmDraft}
            />
          ))}
        </div>

        <div className="admin-row" style={{ marginTop: 12 }}>
          <Button
            variant="primary"
            disabled={!rhythmDirty}
            loading={saveRhythm.isPending}
            onClick={() => saveRhythm.mutate()}
          >
            保存节奏
          </Button>
          <Button variant="danger" onClick={() => setResetTarget("rhythm")}>
            恢复默认
          </Button>
          <span className="admin-muted">
            {rhythmLib.exists ? rhythmLib.path : "词库文件不存在（保存后生成）"}
          </span>
        </div>
      </div>

      {sim ? (
        <div className="admin-card">
          <div className="admin-card-head">
            <h2 className="admin-card-title">模拟结果（只读估算）</h2>
            <span className="admin-card-sub">基于已保存的词库，不写库、不调模型</span>
          </div>

          <div className="admin-stat-grid">
            <Stat label="人物数" value={sim.world.personas ?? "?"} />
            <Stat
              label="启用事件类型"
              value={sim.types.enabled ?? "?"}
              hint={`共 ${sim.types.total ?? "?"} 种`}
            />
            <Stat label="今天已产生事件" value={Object.values(sim.today.events ?? {}).reduce((a, b) => a + b, 0)} />
            <Stat label="今天动态" value={sim.today.posts ?? 0} />
            <Stat label="今天评论" value={sim.today.comments ?? 0} />
            <Stat
              label="待发事件"
              value={sim.pending.eligible_events ?? 0}
              hint={`平均分享意愿 ${sim.pending.avg_share_prob ?? "?"}`}
            />
            <Stat
              label="预计还能发"
              value={sim.pending.expected_new_posts ?? 0}
              hint={`评论约 ${sim.pending.expected_new_comments ?? 0} 条 · 配额 ${sim.pending.quota ?? "?"}`}
            />
            <Stat
              label="按当前参数跑一天"
              value={sim.next_day.events_total_est ?? "?"}
              hint={`规则 ${sim.next_day.rule_events_est ?? "?"} + 叙事 ${sim.next_day.narrative_events_est ?? "?"}，动态配额 ${sim.next_day.posts_quota ?? "?"}`}
            />
          </div>

          {Object.keys(sim.today.events ?? {}).length ? (
            <div className="admin-row">
              <span className="admin-muted">今天事件来源分布</span>
              {Object.entries(sim.today.events ?? {}).map(([source, count]) => (
                <Badge key={source} tone="info">
                  {source}：{count}
                </Badge>
              ))}
            </div>
          ) : null}

          {sim.assumptions.length ? (
            <div className="admin-col" style={{ gap: 4 }}>
              <span className="admin-muted">估算假设（不要当成承诺）</span>
              {sim.assumptions.map((line) => (
                <span key={line} className="admin-muted" style={{ fontSize: 12 }}>
                  · {line}
                </span>
              ))}
            </div>
          ) : null}

          <div className="admin-row">
            <Button size="sm" onClick={() => simulate.reset()}>
              收起
            </Button>
          </div>
        </div>
      ) : null}

      <div className="admin-card">
        <div className="admin-card-head">
          <h2 className="admin-card-title">AI 事件与动态</h2>
          <span className="admin-card-sub">
            {typeKeys.length} 种类型 · 启用 {enabledTypes.length} 种
            {eventsDirty ? " · 有未保存改动" : ""}
          </span>
        </div>

        <p className="admin-muted" style={{ marginTop: 0 }}>
          事件完成后按价值档和分享意愿发成动态。价值档越高越先出现在信息流；分享权重是倍率（0..2），
          不是概率。停用后不再产生新事件，历史事件保留。
        </p>

        {!hasRule || !hasNarrative ? (
          <div className="admin-row">
            {!hasRule ? <Badge tone="warn">缺少「规则来源」的启用类型（世界不会自动产生日常）</Badge> : null}
            {!hasNarrative ? <Badge tone="warn">缺少「叙事来源」的启用类型（没有故事性事件）</Badge> : null}
          </div>
        ) : null}

        {eventsLib && !eventsLib.exists ? (
          <p className="admin-muted">词库文件不存在；保存后会用当前内容生成。</p>
        ) : null}

        {typeKeys.length === 0 ? (
          <EmptyState
            title="没有事件类型"
            description="事件类型需要后端代码支持（叙事 prompt 与校验要认识它），无法在管理台新建。请检查 events.json。"
          />
        ) : (
          <div className="admin-col">
            {typeKeys.map((key) => {
              const spec = types[key];
              const on = spec.enabled !== false;
              const tier = String(Math.max(0, Math.min(3, Number(spec.value_tier ?? 0)))) as Tier;
              const source = (spec.source ?? "rule") as EventSource;
              const weight = typeof spec.share_weight === "number" ? spec.share_weight : 0.7;
              const showTitle = spec.show_title ?? spec.show_content ?? true;
              const tags = Array.isArray(spec.tags) ? spec.tags : [];

              return (
                <div className="admin-card" key={key} style={{ opacity: on ? 1 : 0.6 }}>
                  <div className="admin-card-head">
                    <h2 className="admin-card-title">
                      {spec.zh || spec.label || EVENT_ZH[key] || key}{" "}
                      <span className="admin-mono admin-muted">{key}</span>
                    </h2>
                    <div className="admin-row">
                      {on ? null : <Badge tone="warn">已停用</Badge>}
                      <Button
                        size="sm"
                        variant={on ? "ghost" : "primary"}
                        title={on ? "停用后不再产生新事件，历史保留" : "重新启用"}
                        onClick={() =>
                          updateType(key, (target) => {
                            target.enabled = !on;
                          })
                        }
                      >
                        {on ? "停用" : "启用"}
                      </Button>
                    </div>
                  </div>

                  <div className="admin-col">
                    <div className="admin-row">
                      <span className="admin-slider-label">价值档</span>
                      <Segmented<Tier>
                        value={tier}
                        onChange={(value) =>
                          updateType(key, (target) => {
                            target.value_tier = Number(value);
                          })
                        }
                        options={TIER_OPTIONS}
                      />
                      <span className="admin-muted" style={{ fontSize: 12 }}>
                        越高越靠前
                      </span>
                    </div>

                    <div className="admin-row">
                      <span className="admin-slider-label">来源</span>
                      <Segmented<EventSource>
                        value={source}
                        onChange={(value) =>
                          updateType(key, (target) => {
                            target.source = value;
                          })
                        }
                        options={(["rule", "narrative", "both"] as EventSource[]).map((value) => ({
                          value,
                          label: SOURCE_ZH[value],
                        }))}
                      />
                      <span className="admin-muted" style={{ fontSize: 12 }}>
                        {SOURCE_TITLE[source]}
                      </span>
                    </div>

                    <Slider
                      label="分享权重"
                      hint="倍率 0..2：越高越容易发成动态"
                      min={0}
                      max={2}
                      step={0.01}
                      value={weight}
                      onChange={(value) =>
                        updateType(key, (target) => {
                          target.share_weight = value;
                        })
                      }
                      format={(value) => value.toFixed(2)}
                    />

                    <div className="admin-row">
                      <label className="admin-row" style={{ gap: 6 }}>
                        <input
                          type="checkbox"
                          checked={showTitle}
                          onChange={(event) =>
                            updateType(key, (target) => {
                              const checked = event.target.checked;
                              target.show_title = checked;
                              // 历史数据里也存在 show_content 字段，存在时一起同步，避免两个开关打架。
                              if (target.show_content !== undefined) target.show_content = checked;
                            })
                          }
                        />
                        <span className="admin-slider-label">对用户展示具体内容</span>
                      </label>
                      <span className="admin-muted" style={{ fontSize: 12 }}>
                        红线：冲突类默认不展示
                      </span>
                    </div>

                    <div className="admin-col" style={{ gap: 6 }}>
                      <span className="admin-slider-label">可见 tag</span>
                      <TagCloud
                        tags={Array.from(new Set(tags))}
                        placeholder="新增 tag，例如 daily"
                        addLabel="＋ 添加"
                        onAdd={(tag) => {
                          const value = tag.trim();
                          if (!value) return;
                          updateType(key, (target) => {
                            const current = Array.isArray(target.tags) ? target.tags : [];
                            if (current.includes(value)) return;
                            target.tags = [...current, value];
                          });
                        }}
                        onRemove={(tag) =>
                          updateType(key, (target) => {
                            target.tags = (Array.isArray(target.tags) ? target.tags : []).filter(
                              (item) => item !== tag,
                            );
                          })
                        }
                      />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div className="admin-divider" />
        <div className="admin-row">
          <Button
            variant="primary"
            disabled={!eventsDirty}
            loading={saveEvents.isPending}
            onClick={() => saveEvents.mutate()}
          >
            保存事件类型
          </Button>
          <Button variant="danger" onClick={() => setResetTarget("events")}>
            恢复默认
          </Button>
          <span className="admin-muted">
            {eventsLib.exists ? eventsLib.path : "词库文件不存在（保存后生成）"}
          </span>
        </div>
      </div>

      <ConfirmDialog
        open={resetTarget !== null}
        title={`恢复默认：${resetTarget === "rhythm" ? "世界节奏" : "AI 事件与动态"}？`}
        body={
          <div className="admin-col">
            <span>
              会回滚到上一版备份（<span className="admin-mono">{resetTarget}.bak.json</span>
              ）；没有备份时删除当前文件，回到代码默认值。
            </span>
            <span className="admin-muted">未保存的改动会一并丢失。</span>
          </div>
        }
        confirmLabel="恢复"
        danger
        busy={resetMutation.isPending}
        onConfirm={() => {
          if (resetTarget) resetMutation.mutate(resetTarget);
        }}
        onCancel={() => setResetTarget(null)}
      />
    </section>
  );
}
