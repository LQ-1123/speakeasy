"use client";

/**
 * 表达 DNA：全局词库（13 个字段 × 若干取值）。
 *
 * 设计要点
 * --------
 * 1. **本地草稿 + 显式保存**：改滑杆/开关不会立刻发请求（否则拖一次滑杆就是一串 PUT），
 *    而是改本地 draft，点「保存并生效」才校验 + 落盘。
 * 2. **先校验再保存**：先 `POST /lexicons/dna/validate`，不通过就只显示 issues 不发 PUT——
 *    让运营看到"哪里不对"，而不是吃一个 422。
 * 3. **使用人数来自 /api/admin/usage**：`dna: {field: {value: count}}`。这个接口失败不应该
 *    让整页挂掉，所以和词库并行取、失败降级为 null（只少个数字）。
 * 4. 中文标签优先用词库里的 `label`，没有就回落到旧管理台的两张映射表（FIELD_ZH/VALUE_ZH）。
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
  TextInput,
  ToastStack,
  useToasts,
} from "../ui";

/* -------------------------------------------------------------------------- */
/* 数据形状（与 backend/app/v3/lexicon.py 的 _validate_dna 对齐）               */
/* -------------------------------------------------------------------------- */

interface DnaValueMeta {
  /** 中文名。旧管理台只收集不落盘，这里真的存下来。 */
  label?: string;
  /** 行为影响强度 0..1；缺省表示"不参与数值映射"。 */
  trait?: number;
  enabled?: boolean;
}

interface DnaFieldSpec {
  label?: string;
  values: Record<string, DnaValueMeta>;
  default?: string;
  enabled?: boolean;
}

interface DnaData {
  version?: number;
  fields: Record<string, DnaFieldSpec>;
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

interface UsagePayload {
  personas: number;
  dna: Record<string, Record<string, number>>;
  world?: Record<string, Record<string, number>>;
  scenes?: Record<string, number>;
}

/** 一次保存的结果（校验失败走 200 + ok:false，也要当失败处理）。 */
interface SaveOutcome {
  ok: boolean;
  issues: string[];
  message: string;
  applied?: Record<string, unknown> | null;
}

/* -------------------------------------------------------------------------- */
/* 中文标签：回落到旧管理台的映射（词库 data 里没有 label 时用）                */
/* -------------------------------------------------------------------------- */

const FIELD_ZH: Record<string, [string, string]> = {
  sentence_rhythm: ["句子节奏", "说话是短促、平稳、流畅还是跳跃"],
  directness: ["直接程度", "有话直说还是先绕一下"],
  humor_style: ["幽默风格", "冷幽默 / 讽刺 / 俏皮 / 荒诞"],
  emotional_expression: ["情绪外露", "情绪是收着还是写在脸上"],
  vocabulary_style: ["用词风格", "朴素 / 精准 / 文雅 / 街头 / 专业"],
  slang_usage: ["俚语使用", "口语化程度"],
  emoji_usage: ["表情符号", "是否用 emoji、多不多"],
  verbosity: ["回复长度", "一句话 / 短 / 中 / 长"],
  conversation_initiative: ["主动性", "会不会主动起话题"],
  self_disclosure: ["自我暴露", "愿不愿意讲自己的事"],
  social_distance: ["社交距离", "疏远 / 中性 / 熟络 / 亲密"],
  disagreement_style: ["反对方式", "回避 / 缓和 / 直说 / 挑战"],
  topic_transition: ["话题转换", "留在原题 / 自然转 / 突然转 / 回扣"],
};

const VALUE_ZH: Record<string, string> = {
  clipped: "短促",
  measured: "平稳",
  flowing: "流畅",
  fragmented: "跳跃",
  blunt: "直白",
  direct: "直接",
  diplomatic: "委婉",
  indirect: "含蓄",
  none: "不用",
  dry: "冷幽默",
  sarcastic: "讽刺",
  playful: "俏皮",
  absurd: "荒诞",
  reserved: "克制",
  warm: "温暖",
  expressive: "外放",
  volatile: "情绪化",
  plain: "朴素",
  precise: "精准",
  literary: "文雅",
  street: "街头",
  technical: "专业",
  rare: "少",
  moderate: "适中",
  heavy: "多",
  never: "不用",
  sometimes: "有时",
  often: "常用",
  one_line: "一句话",
  short: "短",
  medium: "中",
  long: "长",
  low: "低",
  high: "高",
  guarded: "戒备",
  open: "开放",
  neutral: "中性",
  familiar: "熟络",
  intimate: "亲密",
  distant: "疏远",
  avoid: "回避",
  soften: "缓和",
  state_plainly: "直说",
  challenge: "挑战",
  stay: "留在原题",
  gentle_shift: "自然转",
  abrupt_shift: "突然转",
  callback: "回扣",
};

/* -------------------------------------------------------------------------- */
/* 小工具                                                                      */
/* -------------------------------------------------------------------------- */

/** 词库都是纯 JSON，用 JSON 深拷贝足够，且不依赖浏览器实现差异。 */
function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function errorOf(error: unknown): { message: string; issues: string[] } {
  if (error instanceof AdminApiError) {
    return { message: error.detail || error.message, issues: error.issues };
  }
  return { message: error instanceof Error ? error.message : String(error), issues: [] };
}

/* -------------------------------------------------------------------------- */
/* 新增取值：每个字段一份本地输入状态                                          */
/* -------------------------------------------------------------------------- */

function AddValueForm({
  existing,
  onAdd,
}: {
  existing: string[];
  onAdd: (key: string, label: string, trait: number | null) => void;
}) {
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [trait, setTrait] = useState("");
  const [error, setError] = useState("");

  const submit = () => {
    const raw = key.trim().toLowerCase();
    if (!raw) {
      setError("请先填 key");
      return;
    }
    if (!/^[a-z0-9_]+$/.test(raw)) {
      setError("key 只能包含小写字母、数字、下划线");
      return;
    }
    if (existing.includes(raw)) {
      setError(`该取值已存在：${raw}`);
      return;
    }
    let parsed: number | null = null;
    const rawTrait = trait.trim();
    if (rawTrait !== "") {
      const value = Number(rawTrait);
      if (Number.isNaN(value) || value < 0 || value > 1) {
        setError("数值必须在 0..1");
        return;
      }
      parsed = Math.round(value * 100) / 100;
    }
    setError("");
    onAdd(raw, label.trim(), parsed);
    setKey("");
    setLabel("");
    setTrait("");
  };

  return (
    <div className="admin-col" style={{ gap: 6 }}>
      <div className="admin-row">
        <div style={{ flex: "1 1 130px" }}>
          <TextInput value={key} onChange={setKey} placeholder="key，如 long" onEnter={submit} />
        </div>
        <div style={{ flex: "1 1 130px" }}>
          <TextInput value={label} onChange={setLabel} placeholder="中文名，如 长" onEnter={submit} />
        </div>
        <div style={{ flex: "0 1 110px" }}>
          <TextInput
            value={trait}
            onChange={setTrait}
            placeholder="0..1（可空）"
            type="number"
            onEnter={submit}
          />
        </div>
        <Button onClick={submit} disabled={!key.trim()}>
          ＋ 新增取值
        </Button>
      </div>
      {error ? <div className="admin-field-error">{error}</div> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function DnaSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();
  const [draft, setDraft] = useState<DnaData | null>(null);
  const [issues, setIssues] = useState<string[]>([]);
  const [resetOpen, setResetOpen] = useState(false);

  const query = useQuery({
    queryKey: adminKeys.lexicon("dna"),
    queryFn: async () => {
      const lib = await apiGet<LexiconPayload<DnaData>>("/api/admin/lexicons/dna");
      // 使用统计是"锦上添花"：数据库没就绪时它可能整体失败，这里降级即可。
      const usage = await apiGet<UsagePayload>("/api/admin/usage").catch(() => null);
      return { lib, usage };
    },
  });

  const serverData = query.data?.lib.data ?? null;
  const usage = query.data?.usage ?? null;

  // 服务端数据变化（首次加载、保存后重取、恢复默认）→ 重置草稿。
  useEffect(() => {
    if (!serverData) return;
    setDraft(clone(serverData));
  }, [serverData]);

  const dirty = useMemo(() => {
    if (!draft || !serverData) return false;
    return JSON.stringify(draft) !== JSON.stringify(serverData);
  }, [draft, serverData]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: adminKeys.lexicon("dna") });
    void queryClient.invalidateQueries({ queryKey: adminKeys.lexicons() });
  };

  /**
   * 刷新 = 丢弃本地改动并重新读盘。
   * 不能只 `refetch()`：react-query 的结构共享会让内容相同的响应保持同一引用，
   * 上面的同步 effect 就不会触发，草稿会一直停在"有未保存改动"。
   */
  const reload = async () => {
    setIssues([]);
    const result = await query.refetch();
    if (result.data) setDraft(clone(result.data.lib.data));
  };

  const saveMutation = useMutation({
    mutationFn: async (data: DnaData): Promise<SaveOutcome> => {
      const check = await apiPost<{ ok: boolean; issues: string[] }>(
        "/api/admin/lexicons/dna/validate",
        { data },
      );
      if (!check.ok) {
        return { ok: false, issues: check.issues ?? [], message: "校验未通过" };
      }
      const saved = await apiPut<{
        ok: boolean;
        issues?: string[];
        message?: string;
        applied?: Record<string, unknown>;
      }>("/api/admin/lexicons/dna", { data });
      if (saved.ok === false) {
        return { ok: false, issues: saved.issues ?? [], message: saved.message || "保存被拒绝" };
      }
      return { ok: true, issues: [], message: "", applied: saved.applied ?? null };
    },
    onSuccess: (result) => {
      if (!result.ok) {
        setIssues(result.issues);
        push(result.message || "保存被拒绝", "error");
        return;
      }
      setIssues([]);
      const fields = result.applied?.dna_fields;
      push(typeof fields === "number" ? `已保存并生效：${fields} 个字段` : "已保存并生效");
      invalidate();
    },
    onError: (error) => {
      const info = errorOf(error);
      setIssues(info.issues);
      push(info.message, "error");
    },
  });

  const resetMutation = useMutation({
    mutationFn: () => apiPost<{ ok: boolean; restored: string }>("/api/admin/lexicons/dna/reset"),
    onSuccess: (result) => {
      setResetOpen(false);
      setIssues([]);
      push(
        result.restored === "backup"
          ? "已回滚到上一版备份"
          : result.restored === "code-default"
            ? "已恢复代码默认值"
            : "没有可恢复的内容",
      );
      invalidate();
    },
    onError: (error) => {
      setResetOpen(false);
      push(errorOf(error).message, "error");
    },
  });

  /* ------------------------------ 局部更新 ------------------------------ */

  const updateField = (field: string, mutate: (spec: DnaFieldSpec) => void) => {
    setDraft((current) => {
      if (!current) return current;
      const next = clone(current);
      const spec = next.fields?.[field];
      if (!spec) return current;
      mutate(spec);
      return next;
    });
  };

  const updateValue = (field: string, value: string, mutate: (meta: DnaValueMeta) => void) => {
    updateField(field, (spec) => {
      const meta = spec.values?.[value];
      if (!meta) return;
      mutate(meta);
    });
  };

  /* ------------------------------ 渲染 ------------------------------ */

  if (query.isPending) return <LoadingState label="正在读取表达 DNA 词库…" />;
  if (query.isError) {
    const info = errorOf(query.error);
    return (
      <ErrorState
        title="读取 DNA 词库失败"
        message={info.message}
        issues={info.issues}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const lib = query.data.lib;
  const fields = draft?.fields ?? {};
  const fieldKeys = Object.keys(fields);

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">表达 DNA</h1>
          <p className="admin-section-desc">
            每个字段一张卡片，取值就是标签。拖动滑杆调整它对行为的影响强度（0..1），点 ★
            设为该字段默认值，停用后新角色不再分到该取值（已有角色不受影响）。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button variant="primary" onClick={() => draft && saveMutation.mutate(draft)} loading={saveMutation.isPending} disabled={!dirty}>
            保存并生效
          </Button>
          <Button onClick={() => void reload()} disabled={query.isFetching}>
            {query.isFetching ? "刷新中…" : "刷新"}
          </Button>
          <Button variant="danger" onClick={() => setResetOpen(true)}>
            恢复默认
          </Button>
        </div>
      </div>

      <div className="admin-row">
        {dirty ? <Badge tone="warn">有未保存改动</Badge> : <Badge tone="success">已与磁盘一致</Badge>}
        <span className="admin-muted">
          {lib.exists ? lib.path : "词库文件不存在（保存后会用当前内容生成）"}
        </span>
        {usage ? <span className="admin-muted">· 已统计 {usage.personas} 个角色</span> : null}
      </div>

      {lib.issues.length ? (
        <ErrorState title="当前词库未通过校验（保存前请先修正）" issues={lib.issues} />
      ) : null}

      {issues.length ? <ErrorState title="校验未通过，未写入磁盘" issues={issues} /> : null}

      {fieldKeys.length === 0 ? (
        <EmptyState
          title="词库为空"
          description="词库文件缺失或结构不完整。可以点「恢复默认」回滚到上一版备份；字段本身需要后端代码支持，管理台只能给已有字段增删取值。"
        />
      ) : (
        <div className="admin-col">
          {fieldKeys.map((field) => {
            const spec = fields[field];
            const labels = FIELD_ZH[field];
            const fieldLabel = spec.label || labels?.[0] || field;
            const fieldHint = labels?.[1] || "";
            const values = spec.values ?? {};
            const valueKeys = Object.keys(values);
            const usageForField = usage?.dna?.[field] ?? {};

            return (
              <div className="admin-card" key={field}>
                <div className="admin-card-head">
                  <h2 className="admin-card-title">
                    {fieldLabel} <span className="admin-mono admin-muted">{field}</span>
                  </h2>
                  <span className="admin-card-sub">{fieldHint}</span>
                </div>

                <div className="admin-col" style={{ gap: 2 }}>
                  {valueKeys.map((value) => {
                    const meta = values[value];
                    const on = meta.enabled !== false;
                    const isDefault = spec.default === value;
                    const hasTrait = typeof meta.trait === "number";
                    const count = usageForField[value] ?? 0;
                    const valueLabel = meta.label || VALUE_ZH[value] || value;

                    return (
                      <div
                        key={value}
                        className="admin-row"
                        style={{ alignItems: "center", opacity: on ? 1 : 0.55, padding: "4px 0" }}
                      >
                        <div style={{ flex: "1 1 190px", minWidth: 150 }}>
                          <span style={{ fontWeight: 500 }}>{valueLabel}</span>{" "}
                          <span className="admin-mono admin-muted">{value}</span>
                          {isDefault ? (
                            <span style={{ marginLeft: 6 }}>
                              <Badge tone="info">默认</Badge>
                            </span>
                          ) : null}
                        </div>

                        {count > 0 ? (
                          <Badge tone="info">{count} 人</Badge>
                        ) : (
                          <span className="admin-muted" style={{ fontSize: 12 }}>
                            未使用
                          </span>
                        )}

                        {hasTrait ? (
                          <>
                            <input
                              type="range"
                              min={0}
                              max={1}
                              step={0.01}
                              value={meta.trait ?? 0}
                              aria-label={`${valueLabel} 的影响强度`}
                              onChange={(event) =>
                                updateValue(field, value, (target) => {
                                  target.trait = Number(event.target.value);
                                })
                              }
                              style={{ flex: "1 1 120px", minWidth: 90 }}
                            />
                            <span className="admin-mono" style={{ width: 42, textAlign: "right" }}>
                              {(meta.trait ?? 0).toFixed(2)}
                            </span>
                          </>
                        ) : (
                          <span className="admin-muted" style={{ flex: "1 1 120px", fontSize: 12 }}>
                            不参与数值映射
                          </span>
                        )}

                        <Button
                          size="sm"
                          variant={isDefault ? "primary" : "ghost"}
                          title="设为该字段的默认取值"
                          onClick={() =>
                            updateField(field, (target) => {
                              target.default = value;
                            })
                          }
                        >
                          {isDefault ? "★ 默认" : "☆ 设为默认"}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          title={on ? "停用后新角色不再分配该取值" : "重新启用该取值"}
                          onClick={() =>
                            updateValue(field, value, (target) => {
                              target.enabled = !on;
                            })
                          }
                        >
                          {on ? "停用" : "启用"}
                        </Button>
                      </div>
                    );
                  })}
                </div>

                <div className="admin-divider" />
                <AddValueForm
                  existing={valueKeys}
                  onAdd={(key, label, trait) =>
                    updateField(field, (target) => {
                      target.values = target.values ?? {};
                      const meta: DnaValueMeta = { enabled: true };
                      if (label) meta.label = label;
                      if (trait !== null) meta.trait = trait;
                      target.values[key] = meta;
                      // 该字段还没有默认值就顺手把新取值设为默认（旧页面的行为）。
                      if (!target.default) target.default = key;
                    })
                  }
                />
              </div>
            );
          })}
        </div>
      )}

      <ConfirmDialog
        open={resetOpen}
        title="恢复默认？"
        body={
          <div className="admin-col">
            <span>
              会回滚到上一版备份（<span className="admin-mono">dna.bak.json</span>）；没有备份时删除
              当前文件，回到代码默认值。
            </span>
            <span className="admin-muted">未保存的改动会一并丢失。</span>
          </div>
        }
        confirmLabel="恢复"
        danger
        busy={resetMutation.isPending}
        onConfirm={() => resetMutation.mutate()}
        onCancel={() => setResetOpen(false)}
      />
    </section>
  );
}
