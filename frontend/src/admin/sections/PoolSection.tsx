"use client";

/**
 * 捏人素材池（world 词库）：名字 / 职业 / 城市 / 兴趣。
 *
 * 结构要点（以 backend/app/v3/lexicon.py 的 _validate_world 为准）
 * ---------------------------------------------------------------
 * - `names` 是 `语言码 → 条目数组`，条目可以是字符串，也可以是
 *   `{name, gender: "male"|"female"|"", country}`；渲染时两种都要认。
 * - `occupations` / `interests` 是 `分组 → 字符串数组`（现在只有 `any` 一组）。
 * - `cities` 是 `国家 → 城市数组`，国家和名字条目的 country 会对齐，
 *   避免出现「英国名字 + 德国城市」。
 *
 * 预览捏人走 `GET /api/admin/preview-personas?count=&language=`，和真实兜底捏人
 * 用同一套取池逻辑，所以这里改完素材（保存后）立刻能看出效果；预览本身不写库、不调 LLM。
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AdminApiError, apiGet, apiPost, apiPut, withQuery } from "../api";
import { adminKeys } from "../queryKeys";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Select,
  TagCloud,
  TextInput,
  ToastStack,
  useToasts,
} from "../ui";

/* -------------------------------------------------------------------------- */
/* 数据形状                                                                    */
/* -------------------------------------------------------------------------- */

interface NameEntry {
  name: string;
  gender?: string;
  country?: string;
}

type NameItem = string | NameEntry;

interface WorldData {
  version?: number;
  names?: Record<string, NameItem[]>;
  occupations?: Record<string, string[]>;
  cities?: Record<string, string[]>;
  interests?: Record<string, string[]>;
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

interface PreviewPersona {
  nickname?: string;
  gender?: string;
  age?: number;
  country?: string;
  city?: string;
  occupation?: string;
  language?: string;
  interests?: string[];
  bio?: string;
}

interface PreviewResult {
  day?: string;
  language?: string;
  count?: number;
  warnings?: string[];
  pools?: Record<string, number>;
  personas?: PreviewPersona[];
  error?: string;
}

interface SaveOutcome {
  ok: boolean;
  issues: string[];
  message: string;
}

/* -------------------------------------------------------------------------- */
/* 常量                                                                        */
/* -------------------------------------------------------------------------- */

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
  ar: "阿拉伯语",
  ru: "俄语",
};

type GroupKey = "names" | "occupations" | "cities" | "interests";

const GROUPS: Array<{ key: GroupKey; title: string; hint: string; groupPlaceholder: string }> = [
  {
    key: "names",
    title: "名字（按语言，带性别/国家）",
    hint: "有 LLM 时把池子写进 prompt 作为取材边界；没有 LLM 时直接取池子",
    groupPlaceholder: "语言码，如 en",
  },
  {
    key: "occupations",
    title: "职业",
    hint: "职业池决定人物的社会身份",
    groupPlaceholder: "分组，如 any",
  },
  {
    key: "cities",
    title: "城市（按国家分组）",
    hint: "国家分组会和名字条目里的 country 对齐",
    groupPlaceholder: "国家名，如 Germany",
  },
  {
    key: "interests",
    title: "兴趣",
    hint: "兴趣池太小时人物标签会比较雷同（建议 8 个以上）",
    groupPlaceholder: "分组，如 any",
  },
];

const GENDER_OPTIONS = [
  { value: "", label: "性别（不限）" },
  { value: "female", label: "女" },
  { value: "male", label: "男" },
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

function nameOf(item: NameItem): string {
  return typeof item === "string" ? item : String(item?.name ?? "");
}

function genderOf(item: NameItem): string {
  return typeof item === "string" ? "" : String(item?.gender ?? "");
}

function countryOf(item: NameItem): string {
  return typeof item === "string" ? "" : String(item?.country ?? "");
}

/** 池子总条数（用于"池子过小"提示）。 */
function totalCount(pools: Record<string, Array<unknown>> | undefined): number {
  if (!pools) return 0;
  return Object.values(pools).reduce((sum, items) => sum + (Array.isArray(items) ? items.length : 0), 0);
}

/* -------------------------------------------------------------------------- */
/* 局部组件：名字标签（带性别/国家，不能直接用 TagCloud）                        */
/* -------------------------------------------------------------------------- */

function NameChips({
  items,
  onRemove,
}: {
  items: NameItem[];
  onRemove: (index: number) => void;
}) {
  return (
    <div className="admin-tagcloud">
      {items.length === 0 ? <span className="admin-muted">（空）</span> : null}
      {items.map((item, index) => {
        const name = nameOf(item);
        const gender = genderOf(item);
        const country = countryOf(item);
        return (
          <span className="admin-tag" key={`${name}-${index}`}>
            {name}
            {gender === "male" ? " ♂" : gender === "female" ? " ♀" : ""}
            {country ? <span className="admin-muted"> · {country}</span> : null}
            <button
              type="button"
              className="admin-tag-x"
              aria-label={`移除 ${name}`}
              onClick={() => onRemove(index)}
            >
              ×
            </button>
          </span>
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function PoolSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();
  const [issues, setIssues] = useState<string[]>([]);
  const [resetTarget, setResetTarget] = useState<null | "world">(null);
  const [previewLang, setPreviewLang] = useState("en");

  const query = useQuery({
    queryKey: adminKeys.lexicon("world"),
    queryFn: () => apiGet<LexiconPayload<WorldData>>("/api/admin/lexicons/world"),
  });

  const serverData = query.data?.data ?? null;
  const [draft, setDraft] = useState<WorldData | null>(null);

  useEffect(() => {
    if (!serverData) return;
    setDraft(clone(serverData));
  }, [serverData]);

  const dirty = useMemo(() => {
    if (!draft || !serverData) return false;
    return JSON.stringify(draft) !== JSON.stringify(serverData);
  }, [draft, serverData]);

  const namesPools = draft?.names ?? {};
  const nameLangs = Object.keys(namesPools);
  // 预览语言优先用名字池里真实存在的语言，避免选一个空池看不到人。
  const langOptions = (nameLangs.length ? nameLangs : Object.keys(LANG_ZH)).map((lang) => ({
    value: lang,
    label: `${LANG_ZH[lang] || lang} · ${lang}`,
  }));
  const effectiveLang = langOptions.some((option) => option.value === previewLang)
    ? previewLang
    : langOptions[0]?.value ?? "en";

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: adminKeys.lexicon("world") });
    void queryClient.invalidateQueries({ queryKey: adminKeys.lexicons() });
  };

  /** 刷新 = 丢弃本地改动重新读盘（结构共享下单纯 refetch 不会重置草稿）。 */
  const reload = async () => {
    setIssues([]);
    const result = await query.refetch();
    if (result.data) setDraft(clone(result.data.data));
  };

  /* ------------------------------ 保存 / 重置 ------------------------------ */

  const saveMutation = useMutation({
    mutationFn: () => validateThenSave("world", draft ?? {}),
    onSuccess: (result) => {
      if (!result.ok) {
        setIssues(result.issues);
        push(result.message, "error");
        return;
      }
      setIssues([]);
      setResetTarget(null);
      push("素材池已保存：下次捏人就会用新素材");
      invalidate();
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
    onSuccess: (result) => {
      setResetTarget(null);
      setIssues([]);
      push(
        result.restored === "backup"
          ? "素材池已回滚到上一版备份"
          : result.restored === "code-default"
            ? "素材池已恢复代码默认值"
            : "没有可恢复的内容",
      );
      invalidate();
    },
    onError: (error) => {
      setResetTarget(null);
      push(errorOf(error).message, "error");
    },
  });

  const preview = useMutation({
    mutationFn: (language: string) =>
      apiGet<PreviewResult>(
        withQuery("/api/admin/preview-personas", { count: 3, language }),
      ),
    onError: (error) => push(`预览失败：${errorOf(error).message}`, "error"),
  });

  /* ------------------------------ 局部更新 ------------------------------ */

  const updateWorld = (mutate: (draft: WorldData) => void) => {
    setDraft((current) => {
      if (!current) return current;
      const next = clone(current);
      mutate(next);
      return next;
    });
  };

  /* ------------------------------ 渲染 ------------------------------ */

  if (query.isPending) return <LoadingState label="正在读取捏人素材池…" />;
  if (query.isError) {
    const info = errorOf(query.error);
    return (
      <ErrorState
        title="读取素材池失败"
        message={info.message}
        issues={info.issues}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const lib = query.data;
  const nameCount = namesPools[effectiveLang]?.length ?? 0;
  const occupationCount = totalCount(draft?.occupations);
  const cityCount = totalCount(draft?.cities);
  const interestCount = totalCount(draft?.interests);

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">捏人素材池</h1>
          <p className="admin-section-desc">
            名字（按语言，带性别/国家）、职业、城市（按国家分组）、兴趣。池子已接入真实捏人：
            保存后下次捏人就会用新素材；名字的性别/国家会和城市分组对齐，避免「英国名字 + 德国城市」。
          </p>
        </div>
        <div className="admin-section-actions">
          <Select
            value={effectiveLang}
            onChange={setPreviewLang}
            options={langOptions}
          />
          <Button
            variant="primary"
            loading={preview.isPending}
            onClick={() => preview.mutate(effectiveLang)}
          >
            {preview.isPending ? "推理中…" : "预览捏人"}
          </Button>
          <Button onClick={() => void reload()} disabled={query.isFetching}>
            {query.isFetching ? "刷新中…" : "刷新"}
          </Button>
        </div>
      </div>

      <div className="admin-row">
        {dirty ? <Badge tone="warn">有未保存改动</Badge> : <Badge tone="success">已与磁盘一致</Badge>}
        <Badge tone={nameCount < 3 ? "warn" : "info"}>名字（{effectiveLang}）{nameCount}</Badge>
        <Badge tone={occupationCount < 3 ? "warn" : "info"}>职业 {occupationCount}</Badge>
        <Badge tone={cityCount === 0 ? "warn" : "info"}>城市 {cityCount}</Badge>
        <Badge tone={interestCount < 8 ? "warn" : "info"}>兴趣 {interestCount}</Badge>
        <span className="admin-muted">{lib.exists ? lib.path : "词库文件不存在（保存后生成）"}</span>
      </div>

      {nameCount < 3 || occupationCount < 3 || cityCount === 0 || interestCount < 8 ? (
        <div className="admin-row">
          <Badge tone="warn">池子偏小</Badge>
          <span className="admin-muted">
            建议：名字 ≥ 3（当前 {effectiveLang} 池 {nameCount}）、职业 ≥ 3（当前 {occupationCount}）、
            城市 ≥ 1（当前 {cityCount}）、兴趣 ≥ 8（当前 {interestCount}）——否则同一天捏出来的人物
            容易重名、重职业或标签雷同。
          </span>
        </div>
      ) : null}

      {lib.issues.length ? (
        <ErrorState title="当前素材池未通过校验（保存前请先修正）" issues={lib.issues} />
      ) : null}
      {issues.length ? <ErrorState title="校验未通过，未写入磁盘" issues={issues} /> : null}

      {preview.data ? (
        <div className="admin-card">
          <div className="admin-card-head">
            <h2 className="admin-card-title">预览捏人（{preview.data.language ?? effectiveLang}）</h2>
            <span className="admin-card-sub">
              {preview.data.error
                ? "预览失败"
                : `${preview.data.count ?? 0} 个 · 不写库、不调模型，保存后才影响真实捏人`}
            </span>
          </div>

          <div className="admin-col">
            {preview.data.error ? (
              <ErrorState title="预览失败" message={preview.data.error} />
            ) : null}

            {preview.data.pools ? (
              <div className="admin-row">
                <span className="admin-muted">当前池子</span>
                {Object.entries(preview.data.pools).map(([key, count]) => (
                  <Badge key={key} tone="info">
                    {key}：{count}
                  </Badge>
                ))}
              </div>
            ) : null}

            {(preview.data.warnings ?? []).length ? (
              <div className="admin-col" style={{ gap: 4 }}>
                {preview.data.warnings?.map((warning) => (
                  <span key={warning} className="admin-muted" style={{ fontSize: 12 }}>
                    ⚠ {warning}
                  </span>
                ))}
              </div>
            ) : null}

            {(preview.data.personas ?? []).length === 0 ? (
              <EmptyState title="没有可预览的人物" description="先补一些名字 / 职业 / 兴趣素材。" />
            ) : (
              <div className="admin-grid-2">
                {(preview.data.personas ?? []).map((persona, index) => (
                  <div className="admin-card" key={`${persona.nickname ?? "persona"}-${index}`}>
                    <div className="admin-card-head">
                      <h2 className="admin-card-title">{persona.nickname || "（无名）"}</h2>
                      <span className="admin-card-sub">
                        {persona.gender === "male"
                          ? "男"
                          : persona.gender === "female"
                            ? "女"
                            : "未知"}
                        {persona.age ? ` · ${persona.age} 岁` : ""}
                      </span>
                    </div>
                    <div className="admin-col" style={{ gap: 2 }}>
                      <span>
                        {persona.city || "?"} · {persona.country || "?"}
                      </span>
                      <span className="admin-muted">{persona.occupation || "?"}</span>
                      {(persona.interests ?? []).length ? (
                        <div className="admin-tagcloud">
                          {(persona.interests ?? []).map((interest) => (
                            <span className="admin-tag" key={interest}>
                              {interest}
                            </span>
                          ))}
                        </div>
                      ) : null}
                      {persona.bio ? (
                        <span className="admin-muted" style={{ fontSize: 12 }}>
                          {persona.bio}
                        </span>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div className="admin-row">
              <Button size="sm" onClick={() => preview.reset()}>
                收起
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {GROUPS.map((group) => {
        const isNames = group.key === "names";
        const pools: Record<string, Array<NameItem | string>> = isNames
          ? (draft?.names ?? {})
          : ((draft?.[group.key] as Record<string, string[]> | undefined) ?? {});
        const groupKeys = Object.keys(pools);

        return (
          <div className="admin-card" key={group.key}>
            <div className="admin-card-head">
              <h2 className="admin-card-title">{group.title}</h2>
              <span className="admin-card-sub">
                {group.hint} · 共 {totalCount(pools)} 条 / {groupKeys.length} 组
              </span>
            </div>

            {groupKeys.length === 0 ? (
              <EmptyState
                title="还没有素材"
                description={`新增一个分组（${group.groupPlaceholder}）后就能往里加条目。`}
              />
            ) : null}

            {groupKeys.map((groupKey) => {
              const rawItems = (pools[groupKey] ?? []) as Array<NameItem | string>;
              const label = isNames ? LANG_ZH[groupKey] || groupKey : groupKey;

              return (
                <div key={groupKey} className="admin-col" style={{ gap: 6, marginBottom: 14 }}>
                  <div className="admin-row">
                    <span style={{ fontWeight: 500 }}>
                      {label}
                      {isNames ? <span className="admin-mono admin-muted"> · {groupKey}</span> : null}
                    </span>
                    <Badge>{rawItems.length} 条</Badge>
                    <span style={{ flex: 1 }} />
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={groupKeys.length <= 1}
                      title={groupKeys.length <= 1 ? "至少保留一个分组" : "删除该分组"}
                      onClick={() => {
                        updateWorld((next) => {
                          const target = next[group.key] as
                            | Record<string, Array<NameItem | string>>
                            | undefined;
                          if (!target) return;
                          delete target[groupKey];
                        });
                      }}
                    >
                      删除该分组
                    </Button>
                  </div>

                  {isNames ? (
                    <>
                      <NameChips
                        items={rawItems as NameItem[]}
                        onRemove={(index) =>
                          updateWorld((next) => {
                            const target = next.names?.[groupKey];
                            if (!target) return;
                            target.splice(index, 1);
                          })
                        }
                      />
                      <NameAddForm
                        langLabel={label}
                        onAdd={(name, gender, country) =>
                          updateWorld((next) => {
                            next.names = next.names ?? {};
                            const pool = next.names[groupKey] ?? [];
                            next.names[groupKey] = [
                              ...pool,
                              { name, gender, country } as NameEntry,
                            ];
                          })
                        }
                      />
                    </>
                  ) : (
                    <>
                      <StringChips
                        items={rawItems as string[]}
                        onAdd={(value) =>
                          updateWorld((next) => {
                            const target = next[group.key] as Record<string, string[]> | undefined;
                            if (!target) return;
                            const pool = target[groupKey] ?? [];
                            if (pool.includes(value)) return;
                            target[groupKey] = [...pool, value];
                          })
                        }
                        onRemove={(value) =>
                          updateWorld((next) => {
                            const target = next[group.key] as Record<string, string[]> | undefined;
                            if (!target) return;
                            target[groupKey] = (target[groupKey] ?? []).filter(
                              (item) => item !== value,
                            );
                          })
                        }
                        placeholder={`新增一条（${label}）`}
                      />
                    </>
                  )}
                </div>
              );
            })}

            <div className="admin-divider" />
            <AddGroupForm
              placeholder={group.groupPlaceholder}
              existing={groupKeys}
              onAdd={(groupKey) =>
                updateWorld((next) => {
                  if (group.key === "names") {
                    next.names = next.names ?? {};
                    if (!next.names[groupKey]) next.names[groupKey] = [];
                    return;
                  }
                  next[group.key] = next[group.key] ?? {};
                  const target = next[group.key] as Record<string, string[]>;
                  if (!target[groupKey]) target[groupKey] = [];
                })
              }
            />
          </div>
        );
      })}

      <div className="admin-row">
        <Button
          variant="primary"
          disabled={!dirty}
          loading={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          保存素材池
        </Button>
        <Button variant="danger" onClick={() => setResetTarget("world")}>
          恢复默认
        </Button>
        <span className="admin-muted">保存后下次捏人就会用新素材；已有角色不会被重捏。</span>
      </div>

      <ConfirmDialog
        open={resetTarget !== null}
        title="恢复默认：捏人素材池？"
        body={
          <div className="admin-col">
            <span>
              会回滚到上一版备份（<span className="admin-mono">world.bak.json</span>
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
/* 局部组件：字符串标签云                                                       */
/* -------------------------------------------------------------------------- */

function StringChips({
  items,
  onAdd,
  onRemove,
  placeholder,
}: {
  items: string[];
  onAdd: (value: string) => void;
  onRemove: (value: string) => void;
  placeholder: string;
}) {
  // 直接复用 TagCloud：它内部已经处理了"输入 + 回车/按钮 + × 删除"。
  return (
    <TagCloud
      tags={items}
      onAdd={onAdd}
      onRemove={onRemove}
      placeholder={placeholder}
      addLabel="＋ 添加"
    />
  );
}

/* -------------------------------------------------------------------------- */
/* 局部组件：新增条目 / 分组                                                    */
/* -------------------------------------------------------------------------- */

function NameAddForm({
  langLabel,
  onAdd,
}: {
  langLabel: string;
  onAdd: (name: string, gender: string, country: string) => void;
}) {
  const [name, setName] = useState("");
  const [gender, setGender] = useState("");
  const [country, setCountry] = useState("");

  const submit = () => {
    const value = name.trim();
    if (!value) return;
    onAdd(value, gender, country.trim());
    setName("");
    setCountry("");
  };

  return (
    <div className="admin-row">
      <div style={{ flex: "1 1 150px" }}>
        <TextInput value={name} onChange={setName} placeholder="名字，如 Jonas" onEnter={submit} />
      </div>
      <Select value={gender} onChange={setGender} options={GENDER_OPTIONS} />
      <div style={{ flex: "1 1 140px" }}>
        <TextInput
          value={country}
          onChange={setCountry}
          placeholder="国家，如 Germany"
          onEnter={submit}
        />
      </div>
      <Button onClick={submit} disabled={!name.trim()}>
        ＋ 添加（{langLabel}）
      </Button>
    </div>
  );
}

function AddGroupForm({
  placeholder,
  existing,
  onAdd,
}: {
  placeholder: string;
  existing: string[];
  onAdd: (group: string) => void;
}) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");

  const submit = () => {
    const group = value.trim();
    if (!group) {
      setError("请填分组名");
      return;
    }
    if (existing.includes(group)) {
      setError("该分组已存在");
      return;
    }
    setError("");
    onAdd(group);
    setValue("");
  };

  return (
    <div className="admin-col" style={{ gap: 6 }}>
      <div className="admin-row">
        <div style={{ flex: "0 1 200px" }}>
          <TextInput value={value} onChange={setValue} placeholder={placeholder} onEnter={submit} />
        </div>
        <Button onClick={submit} disabled={!value.trim()}>
          ＋ 新增分组
        </Button>
      </div>
      {error ? <div className="admin-field-error">{error}</div> : null}
    </div>
  );
}
