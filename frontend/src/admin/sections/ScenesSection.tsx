"use client";

/**
 * 场景与口头禅：两个词库合并成一个分区（旧管理台是两页）。
 *
 * - `scenes`：示例对话场景，每个场景一个 key（寒暄、分享生活、安慰……）。校验要求 ≥ 8 个。
 * - `phrases`：`banned_global` 是注入每条 prompt 的红线词；`signature_pools` 按语言分池的
 *   口头禅，供生成新角色时参考（每种语言上限 80 条，容易撞梗）。
 *
 * 实现要点
 * --------
 * 1. 两个词库互不依赖，各自独立取数、独立草稿、独立保存（改口头禅不会把场景一起 PUT）。
 * 2. 标签云直接复用 `ui.tsx` 的 `TagCloud`；场景条目在词库里既可能是字符串也可能是
 *    `{key}` 对象（旧数据两种都有），统一按 key 读写，新增一律写 `{key}` 对象形式。
 * 3. 保存前先 validate，把 issues 原样展示——场景数 < 8、重复 key 都能在这里看到。
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
  Table,
  TagCloud,
  TextInput,
  ToastStack,
  useToasts,
} from "../ui";

/* -------------------------------------------------------------------------- */
/* 数据形状（与 backend/app/v3/lexicon.py 的 _validate_scenes/_validate_phrases 对齐） */
/* -------------------------------------------------------------------------- */

type SceneEntry = string | { key: string; [extra: string]: unknown };

interface ScenesData {
  version?: number;
  scenes: SceneEntry[];
  note?: string;
}

interface PhrasesData {
  version?: number;
  banned_global: string[];
  signature_pools: Record<string, string[]>;
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
  scenes?: Record<string, number>;
  dna?: Record<string, Record<string, number>>;
}

interface SaveOutcome {
  ok: boolean;
  issues: string[];
  message: string;
  applied?: Record<string, unknown> | null;
}

const LANG_ZH: Record<string, string> = {
  en: "英语",
  ja: "日语",
  ko: "韩语",
  fr: "法语",
  de: "德语",
  es: "西班牙语",
  it: "意大利语",
  pt: "葡萄牙语",
  zh: "中文",
};

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

function sceneKey(entry: SceneEntry): string {
  return typeof entry === "string" ? entry : String(entry?.key ?? "");
}

/** 校验 → 保存。校验失败时不发 PUT（避免吃 422，也避免把半成品写坏）。 */
async function validateThenSave<T>(name: string, data: T): Promise<SaveOutcome> {
  const check = await apiPost<{ ok: boolean; issues: string[] }>(`/api/admin/lexicons/${name}/validate`, {
    data,
  });
  if (!check.ok) return { ok: false, issues: check.issues ?? [], message: "校验未通过" };
  const saved = await apiPut<{
    ok: boolean;
    issues?: string[];
    message?: string;
    applied?: Record<string, unknown>;
  }>(`/api/admin/lexicons/${name}`, { data });
  if (saved.ok === false) {
    return { ok: false, issues: saved.issues ?? [], message: saved.message || "保存被拒绝" };
  }
  return { ok: true, issues: [], message: "", applied: saved.applied ?? null };
}

/** 去重后的标签（TagCloud 用 tag 当 React key，重复会告警）。 */
function uniqueTags(tags: string[]): string[] {
  return Array.from(new Set(tags.map((tag) => tag.trim()).filter(Boolean)));
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

type Tab = "scenes" | "phrases";

export default function ScenesSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();
  const [tab, setTab] = useState<Tab>("scenes");
  const [resetTarget, setResetTarget] = useState<null | "scenes" | "phrases">(null);
  const [issues, setIssues] = useState<string[]>([]);

  const scenesQuery = useQuery({
    queryKey: adminKeys.lexicon("scenes"),
    queryFn: () => apiGet<LexiconPayload<ScenesData>>("/api/admin/lexicons/scenes"),
  });

  const phrasesQuery = useQuery({
    queryKey: adminKeys.lexicon("phrases"),
    queryFn: () => apiGet<LexiconPayload<PhrasesData>>("/api/admin/lexicons/phrases"),
  });

  const usageQuery = useQuery({
    queryKey: adminKeys.usage(),
    queryFn: () => apiGet<UsagePayload>("/api/admin/usage"),
    // 只是覆盖率参考，失败就当没数据。
    retry: false,
    staleTime: 60_000,
  });

  /* ------------------------------ 草稿 ------------------------------ */

  const scenesServer = scenesQuery.data?.data ?? null;
  const phrasesServer = phrasesQuery.data?.data ?? null;

  const [scenesDraft, setScenesDraft] = useState<SceneEntry[]>([]);
  const [phrasesDraft, setPhrasesDraft] = useState<PhrasesData | null>(null);

  useEffect(() => {
    if (!scenesServer) return;
    setScenesDraft(clone(scenesServer.scenes ?? []));
  }, [scenesServer]);

  useEffect(() => {
    if (!phrasesServer) return;
    setPhrasesDraft(clone(phrasesServer));
  }, [phrasesServer]);

  const scenesDirty = useMemo(() => {
    if (!scenesServer) return false;
    return JSON.stringify(scenesDraft) !== JSON.stringify(scenesServer.scenes ?? []);
  }, [scenesDraft, scenesServer]);

  const phrasesDirty = useMemo(() => {
    if (!phrasesServer || !phrasesDraft) return false;
    return JSON.stringify(phrasesDraft) !== JSON.stringify(phrasesServer);
  }, [phrasesDraft, phrasesServer]);

  const invalidate = (name: string) => {
    void queryClient.invalidateQueries({ queryKey: adminKeys.lexicon(name) });
    void queryClient.invalidateQueries({ queryKey: adminKeys.lexicons() });
    void queryClient.invalidateQueries({ queryKey: adminKeys.usage() });
  };

  /** 刷新 = 丢弃本地改动重新读盘（结构共享下单纯 refetch 不会重置草稿）。 */
  const reload = async () => {
    setIssues([]);
    const [scenes, phrases] = await Promise.all([scenesQuery.refetch(), phrasesQuery.refetch()]);
    if (scenes.data) setScenesDraft(clone(scenes.data.data.scenes ?? []));
    if (phrases.data) setPhrasesDraft(clone(phrases.data.data));
  };

  /* ------------------------------ 保存 / 重置 ------------------------------ */

  const saveScenes = useMutation({
    mutationFn: () =>
      validateThenSave("scenes", {
        ...(scenesServer ?? {}),
        scenes: scenesDraft,
      }),
    onSuccess: (result) => {
      if (!result.ok) {
        setIssues(result.issues);
        push(result.message, "error");
        return;
      }
      setIssues([]);
      push("场景词库已保存并生效");
      invalidate("scenes");
    },
    onError: (error) => {
      const info = errorOf(error);
      setIssues(info.issues);
      push(info.message, "error");
    },
  });

  const savePhrases = useMutation({
    mutationFn: () => validateThenSave("phrases", phrasesDraft ?? {}),
    onSuccess: (result) => {
      if (!result.ok) {
        setIssues(result.issues);
        push(result.message, "error");
        return;
      }
      setIssues([]);
      push("口头禅 / 禁用词已保存并生效");
      invalidate("phrases");
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

  /* ------------------------------ 渲染 ------------------------------ */

  if (scenesQuery.isPending || phrasesQuery.isPending) {
    return <LoadingState label="正在读取场景与口头禅词库…" />;
  }

  const loadError = scenesQuery.error ?? phrasesQuery.error;
  if (loadError) {
    const info = errorOf(loadError);
    return (
      <ErrorState
        title="读取词库失败"
        message={info.message}
        issues={info.issues}
        onRetry={() => {
          void scenesQuery.refetch();
          void phrasesQuery.refetch();
        }}
      />
    );
  }

  const scenesLib = scenesQuery.data;
  const phrasesLib = phrasesQuery.data;
  const sceneKeys = uniqueTags(scenesDraft.map(sceneKey));
  const usageScenes = usageQuery.data?.scenes ?? null;

  const pools = phrasesDraft?.signature_pools ?? {};
  const banned = phrasesDraft?.banned_global ?? [];

  const patchPhrases = (mutate: (draft: PhrasesData) => void) => {
    setPhrasesDraft((current) => {
      if (!current) return current;
      const next = clone(current);
      next.banned_global = next.banned_global ?? [];
      next.signature_pools = next.signature_pools ?? {};
      mutate(next);
      return next;
    });
  };

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">场景与口头禅</h1>
          <p className="admin-section-desc">
            场景决定示例语料的覆盖面（至少 8 个）；禁用词会注入每个角色的 prompt（红线，越少越精确）；
            口头禅按语言分池，供生成新角色时参考。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button
            onClick={() => void reload()}
            disabled={scenesQuery.isFetching || phrasesQuery.isFetching}
          >
            {scenesQuery.isFetching || phrasesQuery.isFetching ? "刷新中…" : "刷新"}
          </Button>
          <Button variant="danger" onClick={() => setResetTarget(tab)}>
            恢复默认（当前标签）
          </Button>
        </div>
      </div>

      <div className="admin-row">
        <Segmented<Tab>
          value={tab}
          onChange={setTab}
          options={[
            { value: "scenes", label: "示例场景" },
            { value: "phrases", label: "口头禅与禁用词" },
          ]}
        />
        {tab === "scenes" ? (
          <Badge tone={scenesDirty ? "warn" : "success"}>
            {scenesDirty ? "有未保存改动" : "已与磁盘一致"}
          </Badge>
        ) : (
          <Badge tone={phrasesDirty ? "warn" : "success"}>
            {phrasesDirty ? "有未保存改动" : "已与磁盘一致"}
          </Badge>
        )}
      </div>

      {issues.length ? <ErrorState title="校验未通过，未写入磁盘" issues={issues} /> : null}

      {tab === "scenes" ? (
        <>
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">示例场景</h2>
              <span className="admin-card-sub">
                当前 {sceneKeys.length} 个
                {sceneKeys.length < 8 ? " · 少于 8 个会被校验拒绝" : " · 覆盖良好"}
              </span>
            </div>

            {scenesLib && !scenesLib.exists ? (
              <p className="admin-muted" style={{ marginTop: 0 }}>
                词库文件不存在；保存后会用当前内容生成。
              </p>
            ) : null}

            {sceneKeys.length === 0 ? (
              <EmptyState
                title="还没有场景"
                description="先加几个：small_talk / life_share / comfort / disagree 等。至少 8 个才能保存。"
              />
            ) : (
              <TagCloud
                tags={sceneKeys}
                placeholder="新场景 key，例如 weekend_plan"
                addLabel="＋ 添加场景"
                onAdd={(tag) => {
                  const key = tag.trim();
                  if (!key) return;
                  if (sceneKeys.includes(key)) {
                    push(`场景已存在：${key}`, "error");
                    return;
                  }
                  setScenesDraft((current) => [...current, { key }]);
                }}
                onRemove={(tag) => {
                  setScenesDraft((current) => current.filter((entry) => sceneKey(entry) !== tag));
                }}
              />
            )}

            <div className="admin-row" style={{ marginTop: 12 }}>
              <Button
                variant="primary"
                disabled={!scenesDirty}
                loading={saveScenes.isPending}
                onClick={() => saveScenes.mutate()}
              >
                保存场景
              </Button>
              <span className="admin-muted">
                场景 key 会写进示例语料生成 prompt，改名等于新增一个场景（旧语料仍挂旧 key）。
              </span>
            </div>
          </div>

          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">示例语料覆盖</h2>
              <span className="admin-card-sub">每个场景已有多少条示例对话</span>
            </div>
            {usageScenes === null ? (
              <p className="admin-muted" style={{ margin: 0 }}>
                使用统计暂不可用（数据库未就绪时会出现）。
              </p>
            ) : (
              <Table<{ scene: string; count: number }>
                columns={[
                  { key: "scene", header: "场景", render: (row) => <span className="admin-mono">{row.scene}</span> },
                  {
                    key: "count",
                    header: "示例条数",
                    numeric: true,
                    render: (row) =>
                      row.count > 0 ? row.count : <Badge tone="warn">缺语料</Badge>,
                  },
                ]}
                rows={sceneKeys.map((scene) => ({ scene, count: usageScenes[scene] ?? 0 }))}
                rowKey={(row) => row.scene}
                empty={<div className="admin-table-empty">还没有场景。</div>}
              />
            )}
          </div>
        </>
      ) : (
        <>
          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">全局禁用词（红线）</h2>
              <span className="admin-card-sub">注入每个角色的 prompt，越少越精确</span>
            </div>
            <TagCloud
              tags={uniqueTags(banned)}
              placeholder="新增禁用词，例如 as an AI"
              addLabel="＋ 添加"
              onAdd={(word) => {
                const value = word.trim();
                if (!value) return;
                if (banned.includes(value)) {
                  push(`禁用词已存在：${value}`, "error");
                  return;
                }
                patchPhrases((draft) => {
                  draft.banned_global = [...(draft.banned_global ?? []), value];
                });
              }}
              onRemove={(word) => {
                patchPhrases((draft) => {
                  draft.banned_global = (draft.banned_global ?? []).filter((item) => item !== word);
                });
              }}
            />
          </div>

          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">口头禅池（按语言）</h2>
              <span className="admin-card-sub">
                {Object.keys(pools).length} 种语言 · 单池超过 80 条会撞梗
              </span>
            </div>

            {Object.keys(pools).length === 0 ? (
              <EmptyState title="还没有口头禅池" description="在下方新增语言池，例如 en / ja / de。" />
            ) : null}

            {Object.entries(pools).map(([lang, rawItems]) => {
              // 手改坏的文件里 pool 可能不是数组；渲染层兜一层，不让整页白屏（保存时校验会报出来）。
              const items = Array.isArray(rawItems) ? rawItems : [];
              return (
                <div key={lang} className="admin-col" style={{ gap: 6, marginBottom: 14 }}>
                  <div className="admin-row">
                    <span style={{ fontWeight: 500 }}>
                      {(LANG_ZH[lang] || lang) + " · " + lang}
                    </span>
                    <Badge tone={items.length > 80 ? "warn" : undefined}>{items.length} 条</Badge>
                    <span style={{ flex: 1 }} />
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => {
                        patchPhrases((draft) => {
                          delete draft.signature_pools[lang];
                        });
                      }}
                    >
                      删除该语言池
                    </Button>
                  </div>
                  <TagCloud
                    tags={uniqueTags(items)}
                    placeholder={`新增口头禅（${LANG_ZH[lang] || lang}）`}
                    addLabel="＋ 添加"
                    onAdd={(phrase) => {
                      const value = phrase.trim();
                      if (!value) return;
                      patchPhrases((draft) => {
                        const pool = draft.signature_pools[lang] ?? [];
                        if (pool.includes(value)) return;
                        draft.signature_pools[lang] = [...pool, value];
                      });
                    }}
                    onRemove={(phrase) => {
                      patchPhrases((draft) => {
                        draft.signature_pools[lang] = (draft.signature_pools[lang] ?? []).filter(
                          (item) => item !== phrase,
                        );
                      });
                    }}
                  />
                </div>
              );
            })}

            <div className="admin-divider" />
            <NewPoolForm
              existing={Object.keys(pools)}
              onAdd={(lang) => {
                patchPhrases((draft) => {
                  draft.signature_pools[lang] = [];
                });
              }}
            />

            <div className="admin-row" style={{ marginTop: 12 }}>
              <Button
                variant="primary"
                disabled={!phrasesDirty}
                loading={savePhrases.isPending}
                onClick={() => savePhrases.mutate()}
              >
                保存口头禅 / 禁用词
              </Button>
              <span className="admin-muted">
                {phrasesLib?.exists ? phrasesLib.path : "词库文件不存在（保存后生成）"}
              </span>
            </div>
          </div>
        </>
      )}

      <ConfirmDialog
        open={resetTarget !== null}
        title={`恢复默认：${resetTarget === "scenes" ? "示例场景" : "口头禅与禁用词"}？`}
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

/* -------------------------------------------------------------------------- */
/* 新增语言池                                                                  */
/* -------------------------------------------------------------------------- */

function NewPoolForm({
  existing,
  onAdd,
}: {
  existing: string[];
  onAdd: (lang: string) => void;
}) {
  const [lang, setLang] = useState("");
  const [error, setError] = useState("");

  const submit = () => {
    const value = lang.trim().toLowerCase();
    if (!value) {
      setError("请填语言码");
      return;
    }
    if (!/^[a-z]{2,3}(-[a-z0-9]{2,8})?$/.test(value)) {
      setError("语言码形如 en / ja / pt-br");
      return;
    }
    if (existing.includes(value)) {
      setError("该语言池已存在");
      return;
    }
    setError("");
    onAdd(value);
    setLang("");
  };

  return (
    <div className="admin-col" style={{ gap: 6 }}>
      <div className="admin-row">
        <div style={{ flex: "0 1 160px" }}>
          <TextInput value={lang} onChange={setLang} placeholder="语言码，如 fr" onEnter={submit} />
        </div>
        <Button onClick={submit} disabled={!lang.trim()}>
          ＋ 新增语言池
        </Button>
      </div>
      {error ? <div className="admin-field-error">{error}</div> : null}
    </div>
  );
}
