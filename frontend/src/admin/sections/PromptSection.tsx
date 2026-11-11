"use client";

/**
 * Prompt 控制台：按「角色 × 模式」改写固定模块，并预览组装后的完整 prompt。
 *
 * 三条设计决定（写在代码最前面，免得后来的人又"改回去"）
 * ------------------------------------------------------
 * 1. **可改 / 只读由后端的 `editable` 决定**，前端绝不按 key 猜。
 *    判断写的是 `module.editable !== true` 才算只读——即"后端没明确允许就不允许"，
 *    默认拒绝比默认放行安全：漏一个只读模块会导致运行时状态被写死。
 * 2. **只读模块不隐藏，而是解释清楚为什么只读**。
 *    plan / profile / memory 这三层是运行时数据（今天的计划、档案、真实记忆），
 *    写死一段会让角色对谁都演同一出——这是产品设计决定，不是"技术上做不到"。
 *    所以只读模块照样展示原文，并挂一个明确的徽章说明原因。
 * 3. **保存后立即生效**，界面就这么写。本项目刚修掉一个"改了不生效、还标着重启生效"的
 *    老 bug（见 RhythmSection 顶部注释），这里不再制造同类误解。
 *
 * 其它实现要点
 * ------------
 * - 改写是"整体替换"语义（PUT 一次提交整张 map），所以提交前要把"被清空的模块"也考虑进去：
 *   草稿清空 = 取消该模块的改写，不能当成"保存空字符串"。
 * - 模板套用同样遵守 `editable`：模板里若含只读模块，跳过并告诉用户跳过了几个。
 * - `saved` 是"已生效的改写"，`modules[].text` 是原文；编辑框预填 `saved[key] ?? 原文`，
 *   这样运营看到的就是"当前真正生效的内容"。
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AdminApiError, apiDelete, apiGet, apiPost, apiPut, withQuery } from "../api";
import { adminKeys } from "../queryKeys";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Segmented,
  Select,
  TextArea,
  TextInput,
  ToastStack,
  useToasts,
} from "../ui";

/* -------------------------------------------------------------------------- */
/* 数据形状（task-9 契约；后端并行实现，形状差异由 Lead 整合时统一）            */
/* -------------------------------------------------------------------------- */

type PromptMode = "text" | "image" | "call";

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

interface PromptModule {
  key: string;
  layer?: string;
  title?: string;
  source?: string;
  /** **唯一**的可写判定依据；只有 true 才允许编辑。 */
  editable?: boolean;
  /** 原文可能挂在 text / content / body / original 之一（契约未钉死字段名，逐个兜底）。 */
  text?: string;
  content?: string;
  body?: string;
  original?: string;
  chars?: number;
}

interface ModulesPayload {
  ok?: boolean;
  persona_id?: string;
  mode?: string;
  modules?: PromptModule[];
  /** 已生效的改写：key → text。 */
  saved?: Record<string, string>;
  has_overrides?: boolean;
  max_chars?: number;
}

interface PreviewModuleRow {
  key?: string;
  layer?: string;
  title?: string;
  chars?: number;
  included?: boolean;
  trimmed?: boolean;
}

interface PreviewPayload {
  ok?: boolean;
  full_text?: string;
  text?: string;
  modules?: PreviewModuleRow[];
  plan?: unknown;
  trimmed?: unknown;
  total_chars?: number;
  max_chars?: number;
}

interface PromptTemplate {
  id: string;
  name?: string;
  title?: string;
  mode?: string;
  persona_id?: string;
  modules?: Record<string, string>;
  created_at?: string | number;
}

interface TemplatePayload {
  ok?: boolean;
  items?: PromptTemplate[];
  templates?: PromptTemplate[];
}

/* -------------------------------------------------------------------------- */
/* 中文标签                                                                    */
/* -------------------------------------------------------------------------- */

const MODE_LABEL: Record<PromptMode, string> = {
  text: "文本对话",
  image: "图片生成",
  call: "语音通话",
};

const MODE_OPTIONS: Array<{ value: PromptMode; label: string }> = [
  { value: "text", label: "文本" },
  { value: "image", label: "图片" },
  { value: "call", label: "通话" },
];

/**
 * 只读的**原因**（只是文案；是否只读由 `editable` 决定）。
 *
 * 为什么要把原因写出来：运营看到"只读"第一反应是"你们没做完"。
 * 写清楚"这段是运行时生成的、写死会让角色失真"，它才是一个可接受的产品决定。
 */
const READONLY_REASON: Record<string, string> = {
  plan: "运行时计划：今天的作息、待办、当前情绪目标，由调度器按时间和状态实时生成。",
  profile: "角色档案：职业、城市、性格、兴趣，由捏人流程写入数据库。",
  memory: "角色记忆：最近的对话、关系进度、真实发生过的事，由运行时写入。",
};

const SOURCE_LABEL: Record<string, string> = {
  plan: "运行时计划",
  profile: "角色档案",
  memory: "角色记忆",
  fixed: "固定文案",
  static: "固定文案",
  template: "固定文案",
  lexicon: "词库",
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

function formatWhen(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number" && Number.isFinite(value)) {
    const date = new Date(value < 1e12 ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return String(value);
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
      date.getHours(),
    )}:${pad(date.getMinutes())}`;
  }
  const text = String(value);
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text;
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`;
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map((entry) => asText(entry)).join(" / ");
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

/** 模块原文：契约没钉死字段名，按可能的名字依次取。 */
function moduleText(module: PromptModule): string {
  for (const value of [module.text, module.content, module.body, module.original]) {
    if (typeof value === "string") return value;
  }
  return "";
}

function templateNameOf(template: PromptTemplate): string {
  return template.name || template.title || template.id;
}

function templateModulesOf(template: PromptTemplate): Record<string, string> {
  const modules = template.modules;
  if (!modules || typeof modules !== "object") return {};
  const result: Record<string, string> = {};
  for (const [key, value] of Object.entries(modules)) {
    if (typeof value === "string") result[key] = value;
  }
  return result;
}

/** `plan` 摘要：可能是对象（最常见）、字符串或数组，统一成 key/value 行。 */
function planRows(plan: unknown): Array<{ key: string; value: string }> {
  if (!plan) return [];
  if (typeof plan === "string") return [{ key: "plan", value: plan }];
  if (Array.isArray(plan)) {
    return plan.map((entry, index) => ({ key: `#${index + 1}`, value: asText(entry) }));
  }
  if (typeof plan === "object") {
    return Object.entries(plan as Record<string, unknown>).map(([key, value]) => ({
      key,
      value: asText(value),
    }));
  }
  return [{ key: "plan", value: String(plan) }];
}

/** `trimmed`（被预算裁掉的部分）：字符串 / 数组 / 对象都归一成一行一条。 */
function trimmedLines(trimmed: unknown): string[] {
  if (trimmed === null || trimmed === undefined || trimmed === "") return [];
  if (typeof trimmed === "string") return [trimmed];
  if (Array.isArray(trimmed)) {
    return trimmed.map((entry) => {
      if (typeof entry === "string") return entry;
      if (entry && typeof entry === "object") {
        const record = entry as Record<string, unknown>;
        const name = record.title ?? record.key ?? record.module ?? record.name;
        const reason = record.reason ?? record.note;
        const parts: string[] = [];
        if (name !== undefined && name !== null) parts.push(String(name));
        if (reason !== undefined && reason !== null) parts.push(String(reason));
        if (record.chars !== undefined) parts.push(`${String(record.chars)} 字符`);
        return parts.length ? parts.join(" · ") : asText(entry);
      }
      return asText(entry);
    });
  }
  if (typeof trimmed === "object") {
    return Object.entries(trimmed as Record<string, unknown>).map(
      ([key, value]) => `${key}：${asText(value)}`,
    );
  }
  return [String(trimmed)];
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function PromptSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();

  /** 空字符串表示"还没手动选"，此时回落到第一个角色。 */
  const [personaId, setPersonaId] = useState("");
  const [mode, setMode] = useState<PromptMode>("text");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [selectedKey, setSelectedKey] = useState("");
  const [preview, setPreview] = useState<PreviewPayload | null>(null);
  const [saveIssues, setSaveIssues] = useState<string[]>([]);
  const [clearOpen, setClearOpen] = useState(false);
  const [deleteTemplateTarget, setDeleteTemplateTarget] = useState<PromptTemplate | null>(null);
  const [templateName, setTemplateName] = useState("");

  /* ------------------------------ 基础数据 ------------------------------ */

  const personasQuery = useQuery({
    queryKey: adminKeys.personas({ limit: 300 }),
    queryFn: () => apiGet<PersonaListPayload>(withQuery("/api/admin/personas", { limit: 300 })),
    staleTime: 5 * 60_000,
  });

  const personaChoices = useMemo(
    () =>
      (personasQuery.data?.personas ?? []).map((persona) => ({
        value: persona.id,
        label: persona.nickname ? `${persona.nickname} · ${persona.id}` : persona.id,
      })),
    [personasQuery.data],
  );

  // 不写 effect 去"自动选中第一个"：派生值更简单，也不会多渲染一轮空状态。
  const activePersonaId = personaId || personaChoices[0]?.value || "";

  /**
   * 模块清单。
   *
   * `refetchOnWindowFocus: false` 是刻意的：这个接口回来后会把草稿重置成服务端内容，
   * 如果切个标签页回来就重置，正在改的东西会莫名其妙消失。
   */
  const modulesQuery = useQuery({
    queryKey: adminKeys.personaPrompt(activePersonaId, mode),
    queryFn: () =>
      apiGet<ModulesPayload>(
        withQuery("/api/admin/prompt/modules", { persona_id: activePersonaId, mode }),
      ),
    enabled: Boolean(activePersonaId),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  const templatesQuery = useQuery({
    queryKey: adminKeys.promptTemplates(),
    queryFn: () => apiGet<TemplatePayload | PromptTemplate[]>("/api/admin/prompt/templates"),
    staleTime: 60_000,
  });

  const moduleList = useMemo(() => modulesQuery.data?.modules ?? [], [modulesQuery.data]);

  /**
   * 服务端数据到了（首次 / 切换角色模式 / 保存后重取）→ 重建草稿。
   *
   * 草稿预填 `saved[key] ?? 原文`：运营看到的编辑框内容 == 当前真正生效的内容。
   * 只读模块不建草稿（永远不会被提交）。
   */
  useEffect(() => {
    const payload = modulesQuery.data;
    if (!payload) return;
    const next: Record<string, string> = {};
    for (const module of payload.modules ?? []) {
      if (module.editable !== true) continue;
      const saved = payload.saved?.[module.key];
      next[module.key] = typeof saved === "string" ? saved : moduleText(module);
    }
    setDrafts(next);
    setPreview(null);
    setSaveIssues([]);
    setSelectedKey((current) => {
      const keys = (payload.modules ?? []).map((module) => module.key);
      return current && keys.includes(current) ? current : keys[0] ?? "";
    });
  }, [modulesQuery.data]);

  /* ------------------------------ 差异计算 ------------------------------ */

  /**
   * 待提交的改写集合。
   *
   * 整体替换语义下，这里要同时算出两件事：
   * - `save`：要写入的模块（草稿非空且与基线不同）
   * - `remove`：之前有改写、现在草稿被清空的模块（提交时"不出现"即等于删除）
   */
  const overrideDiff = useMemo(() => {
    const payload = modulesQuery.data;
    const save: Record<string, string> = {};
    const remove: string[] = [];
    let dirty = 0;
    if (payload) {
      for (const module of payload.modules ?? []) {
        if (module.editable !== true) continue;
        const draft = drafts[module.key];
        if (typeof draft !== "string") continue;
        const saved = payload.saved?.[module.key];
        const baseline = typeof saved === "string" ? saved : moduleText(module);
        if (draft === baseline) continue;
        dirty += 1;
        if (draft.trim()) save[module.key] = draft;
        else remove.push(module.key);
      }
    }
    return { save, remove, dirty };
  }, [drafts, modulesQuery.data]);

  const hasOverrides = modulesQuery.data?.has_overrides === true;
  const maxChars = modulesQuery.data?.max_chars;

  const templateList = useMemo(() => {
    const data = templatesQuery.data;
    if (!data) return [];
    if (Array.isArray(data)) return data;
    return data.items ?? data.templates ?? [];
  }, [templatesQuery.data]);

  /** 存模板时用"当前改写"；没有未保存的改写就退回"已生效的改写"。 */
  const templateModules = useMemo(() => {
    if (Object.keys(overrideDiff.save).length) return overrideDiff.save;
    return modulesQuery.data?.saved ?? {};
  }, [overrideDiff.save, modulesQuery.data]);

  /* ------------------------------ 变更 ------------------------------ */

  const invalidatePrompt = () => {
    if (activePersonaId) {
      void queryClient.invalidateQueries({
        queryKey: adminKeys.personaPrompt(activePersonaId, mode),
      });
    }
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      apiPut<{ ok?: boolean; issues?: string[]; saved?: number }>("/api/admin/prompt/overrides", {
        persona_id: activePersonaId,
        mode,
        modules: overrideDiff.save,
      }),
    onSuccess: (data) => {
      if (data?.ok === false) {
        setSaveIssues(data.issues ?? ["保存被拒绝"]);
        push("保存被拒绝，详见下方提示", "error");
        return;
      }
      setSaveIssues([]);
      const count = Object.keys(overrideDiff.save).length;
      const removed = overrideDiff.remove.length;
      push(
        `已保存 ${count} 个模块的改写${removed ? `，并清除 ${removed} 个` : ""} · 立即生效`,
      );
      invalidatePrompt();
    },
    onError: (error) => {
      const info = errorOf(error);
      setSaveIssues(info.issues.length ? info.issues : [info.message]);
      push(info.message, "error");
    },
  });

  const clearMutation = useMutation({
    mutationFn: () =>
      apiDelete<{ ok?: boolean; issues?: string[]; removed?: number }>(
        withQuery("/api/admin/prompt/overrides", { persona_id: activePersonaId, mode }),
      ),
    onSuccess: (data) => {
      setClearOpen(false);
      if (data?.ok === false) {
        push((data.issues ?? ["清空被拒绝"]).join("；"), "error");
        return;
      }
      push("已清空该角色在这个模式下的全部改写，回到默认文案（立即生效）");
      invalidatePrompt();
    },
    onError: (error) => {
      setClearOpen(false);
      push(`清空失败：${errorOf(error).message}`, "error");
    },
  });

  const previewMutation = useMutation({
    mutationFn: () =>
      apiPost<PreviewPayload>("/api/admin/prompt/preview", {
        persona_id: activePersonaId,
        mode,
        module_overrides: overrideDiff.save,
      }),
    onSuccess: (data) => setPreview(data ?? null),
    onError: (error) => push(`预览失败：${errorOf(error).message}`, "error"),
  });

  const createTemplate = useMutation({
    mutationFn: () =>
      apiPost<{ ok?: boolean; issues?: string[] }>("/api/admin/prompt/templates", {
        name: templateName.trim(),
        persona_id: activePersonaId,
        mode,
        modules: templateModules,
      }),
    onSuccess: (data) => {
      if (data?.ok === false) {
        push((data.issues ?? ["保存模板被拒绝"]).join("；"), "error");
        return;
      }
      push("模板已保存");
      setTemplateName("");
      void queryClient.invalidateQueries({ queryKey: adminKeys.promptTemplates() });
    },
    onError: (error) => push(`保存模板失败：${errorOf(error).message}`, "error"),
  });

  const removeTemplate = useMutation({
    mutationFn: (id: string) =>
      apiDelete<{ ok?: boolean; issues?: string[] }>(
        `/api/admin/prompt/templates/${encodeURIComponent(id)}`,
      ),
    onSuccess: (data) => {
      setDeleteTemplateTarget(null);
      if (data?.ok === false) {
        push((data.issues ?? ["删除被拒绝"]).join("；"), "error");
        return;
      }
      push("模板已删除");
      void queryClient.invalidateQueries({ queryKey: adminKeys.promptTemplates() });
    },
    onError: (error) => {
      setDeleteTemplateTarget(null);
      push(`删除模板失败：${errorOf(error).message}`, "error");
    },
  });

  /* ------------------------------ 交互 ------------------------------ */

  const setDraft = (key: string, value: string) => {
    setDrafts((current) => ({ ...current, [key]: value }));
  };

  const saveTemplate = () => {
    if (!activePersonaId) {
      push("先选一个角色", "error");
      return;
    }
    if (!templateName.trim()) {
      push("先给模板起个名字", "error");
      return;
    }
    if (!Object.keys(templateModules).length) {
      push("当前没有改写内容可存为模板；先改几段再存", "error");
      return;
    }
    createTemplate.mutate();
  };

  /**
   * 套用模板：只覆盖"后端声明可改写"的模块。
   * 模板是历史产物，可能包含现在已经变成只读的模块——直接写进去会绕过 editable 边界。
   */
  const applyTemplate = (template: PromptTemplate) => {
    const source = templateModulesOf(template);
    const keys = Object.keys(source);
    if (!keys.length) {
      push("这个模板里没有模块内容", "error");
      return;
    }
    const applied: string[] = [];
    const skipped: string[] = [];
    for (const key of keys) {
      const module = moduleList.find((item) => item.key === key);
      if (!module || module.editable !== true) {
        skipped.push(key);
        continue;
      }
      applied.push(key);
    }
    if (applied.length) {
      setDrafts((current) => {
        const next = { ...current };
        for (const key of applied) next[key] = source[key];
        return next;
      });
    }
    push(
      `已套用 ${applied.length} 个模块到编辑器${
        skipped.length ? `（${skipped.length} 个只读模块已跳过：${skipped.join("、")}）` : ""
      }；点「保存改写」后才生效`,
      applied.length ? "info" : "error",
    );
  };

  /* ------------------------------ 派生展示值 ------------------------------ */

  /** 按 layer 分组：后端给的层顺序即展示顺序（A/B/C…），不重排。 */
  const layerGroups = useMemo(() => {
    const groups = new Map<string, PromptModule[]>();
    for (const module of moduleList) {
      const layer = module.layer || "未分层";
      const bucket = groups.get(layer);
      if (bucket) bucket.push(module);
      else groups.set(layer, [module]);
    }
    return Array.from(groups.entries());
  }, [moduleList]);

  const selectedModule = moduleList.find((module) => module.key === selectedKey) ?? moduleList[0];
  const selectedEditable = selectedModule?.editable === true;
  const selectedSource = selectedModule?.source ?? "";
  /** 只有真正来自运行时的那三层才用"动态模块"的说法；其它只读原因不硬套这个解释。 */
  const selectedDynamic = ["plan", "profile", "memory"].includes(selectedSource);
  const selectedSaved = selectedModule ? modulesQuery.data?.saved?.[selectedModule.key] : undefined;
  const selectedOriginal = selectedModule ? moduleText(selectedModule) : "";
  const selectedBaseline = typeof selectedSaved === "string" ? selectedSaved : selectedOriginal;
  const selectedDraft = selectedModule ? drafts[selectedModule.key] ?? "" : "";
  const selectedChanged = Boolean(selectedModule) && selectedEditable && selectedDraft !== selectedBaseline;

  const fullText = preview ? preview.full_text ?? preview.text ?? "" : "";
  const previewTrimmed = preview ? trimmedLines(preview.trimmed) : [];
  const previewPlan = preview ? planRows(preview.plan) : [];

  /* ------------------------------ 早退 ------------------------------ */

  if (personasQuery.isPending) return <LoadingState label="正在读取角色…" />;

  if (personasQuery.isError) {
    const info = errorOf(personasQuery.error);
    return (
      <ErrorState
        title="读取角色列表失败"
        message={info.message}
        issues={info.issues}
        onRetry={() => void personasQuery.refetch()}
      />
    );
  }

  if (!personaChoices.length) {
    return (
      <EmptyState
        title="还没有角色"
        description="Prompt 按角色改写，先把角色捏出来（「角色工坊」），再回来调文案。"
      />
    );
  }

  /* ------------------------------ 渲染 ------------------------------ */

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">Prompt 控制台</h1>
          <p className="admin-section-desc">
            按角色改写 prompt 里<strong>固定的</strong>模块。改写保存后立即生效（下一次生成就用新文案），
            不需要重启。只读模块（运行时计划 / 角色档案 / 角色记忆）不提供改写，
            <strong>为什么只读</strong>在与模块原文一起写清楚了。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button
            variant="primary"
            loading={saveMutation.isPending}
            disabled={overrideDiff.dirty === 0}
            onClick={() => saveMutation.mutate()}
          >
            保存改写（{Object.keys(overrideDiff.save).length} 个模块）
          </Button>
          <Button
            onClick={() => {
              setSaveIssues([]);
              invalidatePrompt();
            }}
            disabled={modulesQuery.isFetching}
            title="从后端重新读取模块与已保存的改写；编辑器里未保存的改动会丢失"
          >
            {modulesQuery.isFetching ? "读取中…" : "重新读取"}
          </Button>
        </div>
      </div>

      <div className="admin-row">
        <span className="admin-muted">角色</span>
        <div style={{ minWidth: 260 }}>
          <Select<string>
            value={activePersonaId}
            onChange={(value) => {
              setPersonaId(value);
              setSelectedKey("");
            }}
            options={personaChoices}
          />
        </div>
        <span className="admin-muted">模式</span>
        <Segmented<PromptMode> value={mode} onChange={setMode} options={MODE_OPTIONS} />
        {modulesQuery.data ? (
          <Badge tone={hasOverrides ? "warn" : "success"}>
            {hasOverrides ? "有改写（自定义文案生效中）" : "使用默认文案"}
          </Badge>
        ) : null}
        {overrideDiff.dirty > 0 ? (
          <>
            <Badge tone="warn">有 {overrideDiff.dirty} 处未保存改动</Badge>
            <span className="admin-muted" style={{ fontSize: 11 }}>
              切换角色 / 模式会丢弃这些改动
            </span>
          </>
        ) : null}
      </div>

      {modulesQuery.isError ? (
        <ErrorState
          title="读取 prompt 模块失败"
          message={errorOf(modulesQuery.error).message}
          issues={errorOf(modulesQuery.error).issues}
          onRetry={() => void modulesQuery.refetch()}
        />
      ) : modulesQuery.isPending ? (
        <LoadingState label="正在读取 prompt 模块…" />
      ) : moduleList.length === 0 ? (
        <EmptyState
          title="这个模式下没有模块"
          description="后端没有为「角色 × 模式」返回任何模块。换一个模式看看；如果三种模式都为空，是后端还没接这套模块定义。"
        />
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(220px, 320px) minmax(0, 1fr)",
            gap: "var(--admin-gap-lg)",
            alignItems: "start",
          }}
        >
          {/* ---------------- 左栏：模块列表（按 layer 分组） ---------------- */}
          <div className="admin-card" style={{ padding: "var(--admin-gap-sm)" }}>
            <div className="admin-card-head" style={{ marginBottom: 6, padding: "0 var(--admin-gap-xs)" }}>
              <h2 className="admin-card-title">模块</h2>
              <span className="admin-card-sub">{moduleList.length} 个</span>
            </div>

            {layerGroups.map(([layer, items]) => (
              <div key={layer} className="admin-col" style={{ gap: 2 }}>
                <div className="admin-nav-group-label" style={{ padding: "6px 8px 2px" }}>
                  层 {layer}
                </div>
                {items.map((module) => {
                  const editable = module.editable === true;
                  const overridden = typeof modulesQuery.data?.saved?.[module.key] === "string";
                  const selected = module.key === (selectedModule?.key ?? "");
                  return (
                    <button
                      key={module.key}
                      type="button"
                      className="admin-nav-item"
                      aria-current={selected ? "page" : undefined}
                      onClick={() => setSelectedKey(module.key)}
                      style={{ alignItems: "flex-start", gap: 6 }}
                    >
                      <span
                        className="admin-col"
                        style={{ gap: 1, minWidth: 0, flex: "1 1 auto", alignItems: "flex-start" }}
                      >
                        <span
                          className="admin-truncate"
                          style={{ width: "100%", fontWeight: selected ? 650 : 500 }}
                        >
                          {module.title || module.key}
                        </span>
                        <span className="admin-mono admin-muted admin-truncate" style={{ width: "100%" }}>
                          {module.key}
                        </span>
                      </span>
                      <span className="admin-row" style={{ gap: 4, flexWrap: "nowrap", flex: "0 0 auto" }}>
                        {overridden ? <Badge tone="info">已改写</Badge> : null}
                        <Badge tone={editable ? "success" : "warn"}>{editable ? "可改写" : "只读"}</Badge>
                      </span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>

          {/* ---------------- 右栏：编辑区 / 只读说明 ---------------- */}
          <div className="admin-col" style={{ gap: "var(--admin-gap)" }}>
            {selectedModule ? (
              <div className="admin-card">
                <div className="admin-card-head" style={{ flexWrap: "wrap" }}>
                  <h2 className="admin-card-title">{selectedModule.title || selectedModule.key}</h2>
                  <span className="admin-mono admin-muted">{selectedModule.key}</span>
                  <Badge tone={selectedEditable ? "success" : "warn"}>
                    {selectedEditable ? "可改写" : "只读"}
                  </Badge>
                  {typeof selectedSaved === "string" ? <Badge tone="info">改写生效中</Badge> : null}
                  <span className="admin-card-sub" style={{ marginLeft: "auto" }}>
                    {MODE_LABEL[mode]} · {SOURCE_LABEL[selectedModule.source ?? ""] ?? selectedModule.source ?? "来源未标注"}
                  </span>
                </div>

                {selectedEditable ? (
                  <>
                    <TextArea
                      value={selectedDraft}
                      onChange={(value) => setDraft(selectedModule.key, value)}
                      rows={14}
                      mono
                      placeholder="留空 = 不覆盖（回到原文）；这里写的就是这个模块最终会注入的文案"
                    />

                    <div className="admin-row" style={{ marginTop: 8 }}>
                      <span className="admin-muted">
                        {selectedDraft.length} 字符
                        {typeof maxChars === "number" ? ` · 全局上限 ${maxChars}` : ""}
                      </span>
                      <Button
                        size="sm"
                        onClick={() => setDraft(selectedModule.key, selectedOriginal)}
                        disabled={selectedDraft === selectedOriginal}
                      >
                        恢复原文
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setDraft(selectedModule.key, "")}
                        disabled={selectedDraft === ""}
                        title="留空提交 = 取消该模块的改写"
                      >
                        清除该模块改写
                      </Button>
                      {selectedChanged ? <Badge tone="warn">有未保存改动</Badge> : null}
                      {typeof selectedSaved === "string" && !selectedChanged ? (
                        <Badge tone="info">当前生效的是改写</Badge>
                      ) : null}
                    </div>

                    <details style={{ marginTop: 8 }}>
                      <summary className="admin-muted" style={{ cursor: "pointer", fontSize: 12 }}>
                        查看原文（默认注入的固定文案）
                      </summary>
                      <pre
                        className="admin-mono"
                        style={{
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                          margin: "6px 0 0",
                          padding: 10,
                          borderRadius: 6,
                          background: "var(--md-surface-container-low, #f4f3fa)",
                          maxHeight: 220,
                          overflow: "auto",
                        }}
                      >
                        {selectedOriginal || "（后端未返回该模块原文）"}
                      </pre>
                    </details>
                  </>
                ) : (
                  <>
                    {/* 只读不是"没做完"，是设计决定——必须讲清楚，否则每次都要解释一遍 */}
                    <div
                      className="admin-card"
                      style={{
                        background: "var(--md-surface-container-low, #f4f3fa)",
                        padding: "var(--admin-gap)",
                      }}
                    >
                      <div className="admin-row">
                        <Badge tone="warn">{selectedDynamic ? "只读 · 动态模块" : "只读 · 后端声明"}</Badge>
                        <strong>
                          {selectedDynamic
                            ? "动态模块：内容由运行时数据生成，改写会让角色此刻的状态失真。"
                            : "这个模块由后端标记为只读，前端不提供改写。"}
                        </strong>
                      </div>
                      <p className="admin-muted" style={{ margin: "8px 0 0" }}>
                        {READONLY_REASON[selectedSource] ?? "后端没有给出只读的具体原因。"}
                      </p>
                      {selectedDynamic ? (
                        <p className="admin-muted" style={{ margin: "4px 0 0", fontSize: 12 }}>
                          这是设计决定而不是技术限制：同一角色在不同时刻这段内容本来就不同，
                          写死一段等于让她对谁都演同一出。
                        </p>
                      ) : null}
                    </div>

                    <pre
                      className="admin-mono"
                      style={{
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                        margin: 0,
                        padding: 12,
                        borderRadius: 6,
                        background: "var(--md-surface-container-high, #e6e5ee)",
                        maxHeight: 380,
                        overflow: "auto",
                      }}
                    >
                      {selectedOriginal || "（后端未返回该模块原文；可以点下面的「预览组装结果」看它实际拼成了什么）"}
                    </pre>
                  </>
                )}
              </div>
            ) : null}

            {/* ---------------- 保存 / 清空 / 预览 ---------------- */}
            <div className="admin-card">
              <div className="admin-card-head" style={{ flexWrap: "wrap" }}>
                <h2 className="admin-card-title">保存与预览</h2>
                <span className="admin-card-sub">保存后立即生效，不需要重启</span>
              </div>

              {saveIssues.length ? <ErrorState title="保存失败，未写入" issues={saveIssues} /> : null}

              <div className="admin-row" style={{ marginTop: saveIssues.length ? 12 : 0 }}>
                <Button
                  variant="primary"
                  loading={saveMutation.isPending}
                  disabled={overrideDiff.dirty === 0}
                  onClick={() => saveMutation.mutate()}
                >
                  保存改写（{Object.keys(overrideDiff.save).length} 个模块）
                </Button>
                <Button
                  variant="danger"
                  onClick={() => setClearOpen(true)}
                  disabled={!hasOverrides && overrideDiff.dirty === 0}
                >
                  清空改写
                </Button>
                <Button loading={previewMutation.isPending} onClick={() => previewMutation.mutate()}>
                  预览组装结果
                </Button>
                <span className="admin-muted">
                  {overrideDiff.remove.length
                    ? `另有 ${overrideDiff.remove.length} 个模块的改写会被清除`
                    : "改写只覆盖「当前角色 × 当前模式」，不影响别的角色"}
                </span>
              </div>
            </div>

            {/* ---------------- 预览结果 ---------------- */}
            {preview ? (
              <div className="admin-card">
                <div className="admin-card-head" style={{ flexWrap: "wrap" }}>
                  <h2 className="admin-card-title">组装结果</h2>
                  <span className="admin-card-sub">
                    {fullText.length} 字符
                    {typeof preview.max_chars === "number"
                      ? ` · 预算上限 ${preview.max_chars}`
                      : typeof maxChars === "number"
                        ? ` · 预算上限 ${maxChars}`
                        : ""}
                    {typeof preview.total_chars === "number" && preview.total_chars !== fullText.length
                      ? ` · 后端计 ${preview.total_chars}`
                      : ""}
                  </span>
                  <div style={{ marginLeft: "auto" }}>
                    <Button size="sm" variant="ghost" onClick={() => setPreview(null)}>
                      关闭
                    </Button>
                  </div>
                </div>

                <div className="admin-grid-2">
                  <div className="admin-col">
                    <span className="admin-slider-label">完整 prompt</span>
                    <pre
                      className="admin-mono"
                      style={{
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                        margin: 0,
                        padding: 12,
                        borderRadius: 6,
                        background: "var(--md-surface-container-high, #e6e5ee)",
                        maxHeight: 420,
                        overflow: "auto",
                      }}
                    >
                      {fullText || "（后端没有返回 full_text）"}
                    </pre>
                  </div>

                  <div className="admin-col">
                    <span className="admin-slider-label">plan 摘要</span>
                    {previewPlan.length ? (
                      <div className="admin-col" style={{ gap: 2 }}>
                        {previewPlan.map((row) => (
                          <div className="admin-row" key={row.key} style={{ gap: 8 }}>
                            <span className="admin-muted admin-truncate" style={{ flex: "0 0 132px" }}>
                              {row.key}
                            </span>
                            <span className="admin-mono" style={{ flex: "1 1 auto", wordBreak: "break-word" }}>
                              {row.value}
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <span className="admin-muted">后端没有返回 plan 摘要。</span>
                    )}

                    <span className="admin-slider-label" style={{ marginTop: 8 }}>
                      被预算裁掉的部分（trimmed）
                    </span>
                    {previewTrimmed.length ? (
                      <ul className="admin-error-issues" style={{ color: "inherit" }}>
                        {previewTrimmed.map((line, index) => (
                          <li key={`${index}-${line}`}>
                            <span className="admin-mono">{line}</span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <span className="admin-muted">没有被裁掉的内容（预算够用）。</span>
                    )}

                    {Array.isArray(preview.modules) && preview.modules.length ? (
                      <>
                        <span className="admin-slider-label" style={{ marginTop: 8 }}>
                          各模块字符数
                        </span>
                        <div className="admin-tagcloud">
                          {preview.modules.map((row, index) => (
                            <span
                              className="admin-tag"
                              key={row.key ?? `${index}`}
                              title={row.trimmed ? "被预算裁掉了" : undefined}
                            >
                              {row.title || row.key || `#${index + 1}`}
                              {typeof row.chars === "number" ? ` · ${row.chars} 字` : ""}
                              {row.trimmed ? " · 已裁" : row.included === false ? " · 未包含" : ""}
                            </span>
                          ))}
                        </div>
                      </>
                    ) : null}
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      )}

      {/* ---------------- 模板 ---------------- */}
      <div className="admin-card">
        <div className="admin-card-head" style={{ flexWrap: "wrap" }}>
          <h2 className="admin-card-title">改写模板</h2>
          <span className="admin-card-sub">
            把一套改写存下来，之后套用到别的角色（只套用可改写的模块，只读模块自动跳过）
          </span>
        </div>

        <div className="admin-row">
          <div style={{ flex: "1 1 220px", maxWidth: 360 }}>
            <Field label="模板名">
              <TextInput
                value={templateName}
                onChange={setTemplateName}
                placeholder="例如：冷淡短句版"
                onEnter={saveTemplate}
              />
            </Field>
          </div>
          <Button
            variant="primary"
            loading={createTemplate.isPending}
            onClick={saveTemplate}
            disabled={!activePersonaId}
          >
            把当前改写存为模板
          </Button>
          <span className="admin-muted">将保存 {Object.keys(templateModules).length} 个模块</span>
        </div>

        <div className="admin-divider" style={{ margin: "12px 0" }} />

        {templatesQuery.isError ? (
          <ErrorState
            title="读取模板失败"
            message={errorOf(templatesQuery.error).message}
            issues={errorOf(templatesQuery.error).issues}
            onRetry={() => void templatesQuery.refetch()}
          />
        ) : templatesQuery.isPending ? (
          <LoadingState label="正在读取模板…" />
        ) : templateList.length === 0 ? (
          <EmptyState
            title="还没有模板"
            description="把常用的改写存成模板，下次给别的角色调文案就不用重敲一遍。"
          />
        ) : (
          <div className="admin-col" style={{ gap: 6 }}>
            {templateList.map((template) => {
              const count = Object.keys(templateModulesOf(template)).length;
              return (
                <div
                  className="admin-row"
                  key={template.id}
                  style={{
                    justifyContent: "space-between",
                    border: "1px solid var(--md-outline-variant, #c6c6d0)",
                    borderRadius: "var(--admin-radius-sm, 6px)",
                    padding: "8px 12px",
                  }}
                >
                  <div className="admin-col" style={{ gap: 0, minWidth: 0 }}>
                    <span style={{ fontWeight: 550 }} className="admin-truncate">
                      {templateNameOf(template)}
                    </span>
                    <span className="admin-muted" style={{ fontSize: 11 }}>
                      {template.mode || "模式未标注"} · {count} 个模块 ·{" "}
                      {formatWhen(template.created_at)}
                      {template.persona_id ? ` · 来自 ${template.persona_id}` : ""}
                    </span>
                  </div>
                  <div className="admin-row">
                    <Button size="sm" onClick={() => applyTemplate(template)} disabled={!moduleList.length}>
                      套用到这个角色
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => setDeleteTemplateTarget(template)}
                    >
                      删除
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={clearOpen}
        title="清空这个角色在这个模式下的改写？"
        body={
          <div className="admin-col">
            <span>
              会回到默认文案（删除{" "}
              <span className="admin-mono">
                prompt overrides · {activePersonaId} · {mode}
              </span>
              ）。
            </span>
            <span className="admin-muted">立即生效，不需要重启。</span>
          </div>
        }
        confirmLabel="清空"
        danger
        busy={clearMutation.isPending}
        onConfirm={() => clearMutation.mutate()}
        onCancel={() => setClearOpen(false)}
      />

      <ConfirmDialog
        open={deleteTemplateTarget !== null}
        title="删除这个模板？"
        body={
          <div className="admin-col">
            <span>
              删除 <span className="admin-mono">{deleteTemplateTarget ? templateNameOf(deleteTemplateTarget) : ""}</span>
              ；已经套用过的角色不受影响（套用只是填进了编辑器）。
            </span>
          </div>
        }
        confirmLabel="删除"
        danger
        busy={removeTemplate.isPending}
        onConfirm={() => {
          if (deleteTemplateTarget) removeTemplate.mutate(deleteTemplateTarget.id);
        }}
        onCancel={() => setDeleteTemplateTarget(null)}
      />
    </section>
  );
}
