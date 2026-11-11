"use client";

/**
 * 角色工坊（`#/personas/studio`）：角色列表 + 单角色属性编辑 + 批量工具。
 *
 * 从旧管理台（`backend/app/admin/page.py` 的 `renderPersonaStudioView`）平移，行为保持一致：
 *
 * - **左侧列表**：`GET /api/admin/personas?query=&limit=` → `{ok, personas, counts, users}`；
 *   列表行里的音色字段叫 `fish_voice`（后端 `list_personas` 把 `fish_voice_id` 改名了），
 *   而详情（`GET /api/admin/personas/{id}`）里是原字段名 `fish_voice_id`。
 * - **右侧编辑器**：`PUT /api/admin/personas/{id}`（保存）/ `POST /api/admin/personas`（新建）/
 *   `POST /api/admin/personas/{id}/reroll`（重捏）/ `DELETE /api/admin/personas/{id}`（删除）。
 * - **批量工具**：`POST /api/admin/personas/{action}`，action ∈
 *   `rebuild-preview|rematch-media|rebalance-genders|redistribute-voices|regenerate-posts|init-life|rebuild`。
 *
 * 危险操作一律走 `ConfirmDialog`：
 * - 删除单个角色：说明会删掉动态/示例/关系/好友关系与相关聊天，并先备份数据库；
 * - 彻底重建（`rebuild` + `mode=recreate`）：说明会删除**全部**角色及全部关联数据、清空聊天，
 *   并要求手输 `RESET` —— 后端 `rebuild_all` 就是要求 `confirm` 等于 `RESET` 才执行
 *   （大小写不敏感），前端先挡一层，避免误点后直接打到后端；
 * - 全量原地重捏 / 重新生成动态 / 平衡男女比例 / 重新分配音色 / 修正图文 / 补齐生活层：
 *   都先展示影响范围再确认。
 *
 * 写操作的 HTTP 状态码都是 200，失败信息在响应体的 `ok:false + issues[]` 里，
 * 所以统一用 `ensureOk()` 把"业务失败"转成异常，交给 react-query 的 onError 处理。
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  AdminApiError,
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  requestBlobPost,
  withQuery,
} from "../api";
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
  TagCloud,
  TextArea,
  TextInput,
  ToastStack,
  useToasts,
} from "../ui";
import type { Column } from "../ui";
import { SpeechDnaEditor } from "../speechDna";

/* -------------------------------------------------------------------------- */
/* 后端数据形状（shape 来源：backend/app/admin/persona_studio.py）              */
/* -------------------------------------------------------------------------- */

/** `GET /api/admin/personas` 的行：音色字段是 `fish_voice`（不是 `fish_voice_id`）。 */
interface PersonaRow {
  id: string;
  nickname?: string;
  gender?: string;
  age?: number;
  language?: string;
  country?: string;
  city?: string;
  occupation?: string;
  fish_voice?: string;
  fish_voice_name?: string;
  voice_id?: string;
  status?: string;
  lifecycle?: string;
  created_at?: string;
  friend_count?: number;
  post_count?: number;
  example_count?: number;
}

/** `rebuild_preview()` 的 counts（`-1` 表示该表不存在/读取失败）。 */
interface PersonaCounts {
  personas?: number;
  persona_posts?: number;
  persona_example_dialogs?: number;
  persona_events?: number;
  persona_relationships?: number;
  friendships?: number;
  user_post_likes?: number;
  user_post_comments?: number;
  social_events?: number;
  v3_users?: number;
  friends?: number;
  [key: string]: number | undefined;
}

interface PersonaUser {
  id: string;
  display_name?: string;
  device_key_masked?: string;
  friend_count?: number;
  last_seen_at?: string;
}

interface PersonaListPayload {
  ok?: boolean;
  issues?: string[];
  personas?: PersonaRow[];
  counts?: PersonaCounts;
  users?: PersonaUser[];
}

/**
 * `persona_detail()`：`SELECT *` 的整行 + JSON 字段已解析 + `fish_voice_name` /
 * `_counts` / `_recent_posts`。
 */
interface PersonaDetail extends PersonaRow {
  bio?: string;
  personality?: string;
  interests?: unknown;
  avatar_seed?: string;
  avatar_style?: string;
  avatar_options?: unknown;
  ai_config?: unknown;
  speech_dna?: unknown;
  emotion_state?: unknown;
  timezone?: string;
  archetype_id?: string;
  expires_at?: string;
  last_interaction_at?: string;
  interaction_count?: number;
  fish_voice_id?: string;
  _counts?: { friends?: number; posts?: number; examples?: number };
  _recent_posts?: Array<{
    id: string;
    content?: string;
    created_at?: string;
    like_count?: number;
    comment_count?: number;
  }>;
  [key: string]: unknown;
}

interface PersonaDetailPayload {
  ok?: boolean;
  issues?: string[];
  persona?: PersonaDetail;
}

/** 音色池（角色工坊里只用来做"从素材池选择"和试听）。 */
interface PoolVoice {
  reference_id: string;
  name?: string;
  gender?: string;
  language?: string;
  assigned?: number;
}

interface VoicePoolPayload {
  fish?: { configured?: boolean; usable?: boolean };
  voices?: PoolVoice[];
}

/* -------------------------------------------------------------------------- */
/* 小工具                                                                      */
/* -------------------------------------------------------------------------- */

type ApiRecord = Record<string, unknown>;

function asText(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function asNumber(value: unknown): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function asStringArray(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => asText(item)).filter((item) => item.trim() !== "");
  }
  if (typeof value === "string") {
    return value
      .split(/[,，]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
}

/** JSON 字段转编辑框文本（详情里已是对象；坏数据退化成原文本，不阻断编辑）。 */
function jsonText(value: unknown): string {
  if (value === null || value === undefined) return "{}";
  if (typeof value === "string") {
    const raw = value.trim();
    if (!raw) return "{}";
    try {
      return JSON.stringify(JSON.parse(raw), null, 2);
    } catch {
      return raw;
    }
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "{}";
  }
}

/**
 * 把编辑框里的 JSON 文本解析成对象，喂给结构化编辑器。
 *
 * 坏数据不抛错：退回空对象，让运营看到"全是默认值"而不是整页崩掉——
 * 原始文本仍保留在「高级」折叠区里，可以手工修。
 */
function speechDnaObject(text: string): Record<string, unknown> {
  const raw = String(text || "").trim();
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function parseJsonField(label: string, text: string): unknown {
  const raw = text.trim();
  if (!raw) return {};
  try {
    return JSON.parse(raw) as unknown;
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`${label} 不是合法 JSON：${detail}`);
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof AdminApiError) {
    return error.issues.join("；") || error.detail || error.message;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

function issueText(issues: unknown): string {
  const list = Array.isArray(issues) ? issues.map((item) => String(item)) : [];
  return list.length ? list.join("；") : "操作失败（后端未返回原因）";
}

/** 业务失败（HTTP 200 + `ok:false`）也当异常抛，统一走 onError。 */
function ensureOk<T extends ApiRecord>(result: T): T {
  if (result.ok === false) throw new Error(issueText(result.issues));
  return result;
}

/**
 * 「试听音色」要 POST 拿 mp3：`/api/admin/voice-pool/preview` 只接受 POST + JSON body。
 * POST-blob 的鉴权语义（CSRF 头 + 403 重试）统一由 `api.ts::requestBlobPost` 提供，
 * 这里只绑定路径与参数——不要在组件里重写一遍 CSRF 逻辑。
 */
function requestPreviewBlob(referenceId: string): Promise<Blob> {
  return requestBlobPost("/api/admin/voice-pool/preview", { reference_id: referenceId });
}

/* -------------------------------------------------------------------------- */
/* 表单状态                                                                    */
/* -------------------------------------------------------------------------- */

interface PersonaForm {
  nickname: string;
  gender: string;
  age: string;
  language: string;
  country: string;
  city: string;
  timezone: string;
  occupation: string;
  bio: string;
  personality: string;
  interests: string[];
  fish_voice_id: string;
  voice_id: string;
  archetype_id: string;
  status: string;
  lifecycle: string;
  expires_at: string;
  avatar_seed: string;
  avatar_style: string;
  avatar_options_text: string;
  ai_config_text: string;
  speech_dna_text: string;
}

const LANGUAGE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "en", label: "en · 英语" },
  { value: "zh", label: "zh · 中文" },
  { value: "ja", label: "ja · 日语" },
  { value: "ko", label: "ko · 韩语" },
  { value: "de", label: "de · 德语" },
  { value: "es", label: "es · 西班牙语" },
  { value: "it", label: "it · 意大利语" },
  { value: "pt", label: "pt · 葡萄牙语" },
  { value: "fr", label: "fr · 法语" },
];

const GENDER_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "female", label: "女" },
  { value: "male", label: "男" },
  { value: "", label: "未知" },
];

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "active", label: "active · 启用" },
  { value: "paused", label: "paused · 暂停" },
  { value: "inactive", label: "inactive · 停用" },
];

const LIFECYCLE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "permanent", label: "permanent · 永久" },
  { value: "guest", label: "guest · 访客（到期消失）" },
];

function emptyForm(): PersonaForm {
  return {
    nickname: "",
    gender: "female",
    age: "25",
    language: "en",
    country: "",
    city: "",
    timezone: "UTC",
    occupation: "",
    bio: "",
    personality: "",
    interests: [],
    fish_voice_id: "",
    voice_id: "",
    archetype_id: "",
    status: "active",
    lifecycle: "permanent",
    expires_at: "",
    avatar_seed: "persona-preview",
    avatar_style: "avataaars",
    avatar_options_text: "{}",
    ai_config_text: jsonText({ source: "admin-test" }),
    speech_dna_text: "{}",
  };
}

function formFromDetail(detail: PersonaDetail): PersonaForm {
  return {
    nickname: asText(detail.nickname),
    gender: asText(detail.gender, "female"),
    age: detail.age === undefined || detail.age === null ? "" : String(detail.age),
    language: asText(detail.language, "en"),
    country: asText(detail.country),
    city: asText(detail.city),
    timezone: asText(detail.timezone, "UTC"),
    occupation: asText(detail.occupation),
    bio: asText(detail.bio),
    personality: asText(detail.personality),
    interests: asStringArray(detail.interests),
    fish_voice_id: asText(detail.fish_voice_id) || asText(detail.fish_voice),
    voice_id: asText(detail.voice_id),
    archetype_id: asText(detail.archetype_id),
    status: asText(detail.status, "active"),
    lifecycle: asText(detail.lifecycle, "permanent"),
    expires_at: asText(detail.expires_at),
    avatar_seed: asText(detail.avatar_seed),
    avatar_style: asText(detail.avatar_style, "avataaars"),
    avatar_options_text: jsonText(detail.avatar_options),
    ai_config_text: jsonText(detail.ai_config),
    speech_dna_text: jsonText(detail.speech_dna),
  };
}

/** 只提交后端 `EDITABLE_FIELDS` 里的字段；JSON 字段解析后交给后端重新序列化。 */
function payloadFromForm(form: PersonaForm): ApiRecord {
  const nickname = form.nickname.trim();
  if (!nickname) throw new Error("昵称不能为空");

  const payload: ApiRecord = {
    nickname,
    gender: form.gender,
    language: form.language,
    country: form.country.trim(),
    city: form.city.trim(),
    timezone: form.timezone.trim() || "UTC",
    occupation: form.occupation.trim(),
    bio: form.bio,
    personality: form.personality,
    interests: form.interests,
    fish_voice_id: form.fish_voice_id.trim(),
    voice_id: form.voice_id.trim(),
    archetype_id: form.archetype_id.trim(),
    status: form.status,
    lifecycle: form.lifecycle,
    expires_at: form.expires_at.trim(),
    avatar_seed: form.avatar_seed.trim(),
    avatar_style: form.avatar_style.trim(),
    avatar_options: parseJsonField("avatar_options", form.avatar_options_text),
    ai_config: parseJsonField("ai_config", form.ai_config_text),
    speech_dna: parseJsonField("speech_dna", form.speech_dna_text),
  };

  const age = form.age.trim();
  if (age) {
    const numeric = Number(age);
    if (!Number.isFinite(numeric)) throw new Error("年龄必须是数字");
    payload.age = Math.trunc(numeric);
  }
  return payload;
}

/* -------------------------------------------------------------------------- */
/* 批量工具                                                                    */
/* -------------------------------------------------------------------------- */

type BatchKind =
  | "reroll-all"
  | "recreate"
  | "rematch-media"
  | "rebalance-genders"
  | "redistribute-voices"
  | "regenerate-posts"
  | "init-life";

interface BatchAction {
  kind: BatchKind;
  label: string;
  hint: string;
  danger?: boolean;
}

const BATCH_ACTIONS: BatchAction[] = [
  {
    kind: "reroll-all",
    label: "全量原地重捏",
    hint: "保留角色 ID/好友/聊天，只重掷人设、头像、音色；先备份数据库",
  },
  {
    kind: "recreate",
    label: "彻底重建",
    hint: "删除全部角色及其动态/示例/关系/好友关系与聊天后，按素材池重新生成；需输入 RESET 确认",
    danger: true,
  },
  {
    kind: "rematch-media",
    label: "修正图文匹配",
    hint: "用 LLM 按图片内容重写图文动态的文案（每次 20 条，图片不变）",
  },
  {
    kind: "rebalance-genders",
    label: "平衡男女比例",
    hint: "把角色性别调整到约 1:1，会更换部分角色的名字/头像/音色",
  },
  {
    kind: "redistribute-voices",
    label: "重新分配音色",
    hint: "把素材池音色均匀重分配给全部角色（只换声音，ID/好友/聊天不变）",
  },
  {
    kind: "regenerate-posts",
    label: "重新生成动态",
    hint: "删除现有全部角色动态，按语言素材库重新生成",
  },
  {
    kind: "init-life",
    label: "补齐生活层",
    hint: "补齐作息表 / Speech DNA / 示例对话 / 人物关系（0 次 LLM）",
  },
];

const BATCH_PATH: Record<BatchKind, string> = {
  "reroll-all": "/api/admin/personas/rebuild",
  recreate: "/api/admin/personas/rebuild",
  "rematch-media": "/api/admin/personas/rematch-media",
  "rebalance-genders": "/api/admin/personas/rebalance-genders",
  "redistribute-voices": "/api/admin/personas/redistribute-voices",
  "regenerate-posts": "/api/admin/personas/regenerate-posts",
  "init-life": "/api/admin/personas/init-life",
};

const BATCH_TITLE: Record<BatchKind, string> = {
  "reroll-all": "原地重捏全部角色？",
  recreate: "彻底重建全部角色？",
  "rematch-media": "修正图文匹配？",
  "rebalance-genders": "平衡男女比例？",
  "redistribute-voices": "重新分配全部角色的音色？",
  "regenerate-posts": "重新生成全部角色动态？",
  "init-life": "补齐生活层？",
};

/** 把批量接口的返回压成一句人话（各 action 返回结构不同，见 persona_studio.py）。 */
function describeBatchResult(kind: BatchKind, result: ApiRecord): string {
  switch (kind) {
    case "reroll-all":
      return `已原地重捏 ${asNumber(result.rerolled)} 个角色；数据库备份：${asText(result.backup) || "（无）"}`;
    case "recreate": {
      const deleted = result.deleted;
      const summary =
        deleted && typeof deleted === "object"
          ? Object.entries(deleted as Record<string, unknown>)
              .filter(([, value]) => asNumber(value) > 0)
              .map(([key, value]) => `${key} ${asNumber(value)}`)
              .join(" / ")
          : "";
      return `已重建 ${asNumber(result.created)} 个角色；删除：${summary || "—"}；备份：${
        asText(result.backup) || "（无）"
      }`;
    }
    case "rematch-media":
      return `已修正 ${asNumber(result.fixed)} 条（失败 ${asNumber(result.failed)}，剩余 ${asNumber(
        result.remaining,
      )}）；可再点一次继续`;
    case "rebalance-genders":
      return `性别已平衡：女 ${asNumber(result.female)} / 男 ${asNumber(result.male)}；调整 ${asNumber(
        result.changed,
      )} 人，音色已按性别重配`;
    case "redistribute-voices": {
      const distribution = Array.isArray(result.distribution) ? result.distribution : [];
      const text = distribution
        .slice(0, 6)
        .map((item) => {
          const row = (item || {}) as ApiRecord;
          return `${asText(row.name) || asText(row.reference_id)}×${asNumber(row.count)}`;
        })
        .join("，");
      return `已重新分配 ${asNumber(result.updated)} 个角色（未分配 ${asNumber(
        result.unassigned,
      )}）${text ? ` · ${text}` : ""}`;
    }
    case "regenerate-posts":
      return `已为 ${asNumber(result.personas)} 个角色生成 ${asNumber(result.posts)} 条动态（${
        asNumber(result.posts_with_media)
      } 条带图）`;
    case "init-life":
      return `生活层补齐：角色 ${asNumber(result.personas)} · 作息 ${asNumber(
        result.schedules,
      )} · 关系 ${asNumber(result.relationships)}`;
  }
}

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
  return "?";
}

/* -------------------------------------------------------------------------- */
/* 分区                                                                        */
/* -------------------------------------------------------------------------- */

export default function StudioSection() {
  const queryClient = useQueryClient();
  const { toasts, push } = useToasts();

  const [search, setSearch] = useState("");
  const [queryText, setQueryText] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [mode, setMode] = useState<"none" | "edit" | "create">("none");
  const [form, setForm] = useState<PersonaForm>(() => emptyForm());
  const [bindUserId, setBindUserId] = useState("");
  const [initialPosts, setInitialPosts] = useState("");

  const [personaDialog, setPersonaDialog] = useState<null | "delete" | "reroll">(null);
  const [batch, setBatch] = useState<BatchKind | null>(null);
  const [batchError, setBatchError] = useState("");
  const [recreateCount, setRecreateCount] = useState("");
  const [recreateConfirm, setRecreateConfirm] = useState("");
  const [autoFriend, setAutoFriend] = useState(false);
  const [postsPerPersona, setPostsPerPersona] = useState("5");

  /** 已经应用到表单的详情 id：避免后台 refetch 把用户正在编辑的内容冲掉。 */
  const appliedDetailId = useRef("");

  /* ------------------------------ 查询 ------------------------------ */

  const listParams = { query: queryText, limit: 300 };
  const listKey = adminKeys.personas(listParams);

  const listQuery = useQuery({
    queryKey: listKey,
    queryFn: () =>
      apiGet<PersonaListPayload>(
        withQuery("/api/admin/personas", { query: queryText, limit: 300 }),
      ),
  });

  const detailQuery = useQuery({
    queryKey: adminKeys.persona(selectedId),
    queryFn: () => apiGet<PersonaDetailPayload>(`/api/admin/personas/${encodeURIComponent(selectedId)}`),
    enabled: mode === "edit" && Boolean(selectedId),
  });

  const voicesQuery = useQuery({
    queryKey: adminKeys.voices(),
    queryFn: () => apiGet<VoicePoolPayload>("/api/admin/voice-pool"),
    staleTime: 60_000,
  });

  /* 详情 → 表单。只在「换了一个角色」时灌入，保存/重掷后由响应直接回填。 */
  useEffect(() => {
    if (mode !== "edit") return;
    const detail = detailQuery.data?.persona;
    if (!detail) return;
    if (appliedDetailId.current === detail.id) return;
    appliedDetailId.current = detail.id;
    setForm(formFromDetail(detail));
  }, [mode, detailQuery.data]);

  /* ------------------------------ 失效 ------------------------------ */

  const invalidatePersona = () => {
    if (selectedId) {
      void queryClient.invalidateQueries({ queryKey: adminKeys.persona(selectedId) });
    }
  };

  const invalidateAfterBulk = () => {
    void queryClient.invalidateQueries({ queryKey: listKey });
    void queryClient.invalidateQueries({ queryKey: adminKeys.voices() });
    // 批量操作会改到「当前正在编辑的那个人」，让详情重新灌进表单
    appliedDetailId.current = "";
    invalidatePersona();
  };

  /* ------------------------------ 写操作 ------------------------------ */

  const saveMutation = useMutation({
    mutationFn: async (payload: ApiRecord) => {
      if (mode === "create") {
        const body: ApiRecord = {
          ...payload,
          bind_user_id: bindUserId,
          posts: initialPosts
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean)
            .slice(0, 5),
        };
        return ensureOk(await apiPost<ApiRecord>("/api/admin/personas", body));
      }
      return ensureOk(
        await apiPut<ApiRecord>(`/api/admin/personas/${encodeURIComponent(selectedId)}`, payload),
      );
    },
    onSuccess: (result) => {
      const persona = (result.persona || {}) as PersonaDetail;
      const created = mode === "create";
      if (persona && persona.id) {
        appliedDetailId.current = persona.id;
        setForm(formFromDetail(persona));
        setSelectedId(persona.id);
      }
      setMode("edit");
      setBindUserId("");
      setInitialPosts("");
      void queryClient.invalidateQueries({ queryKey: listKey });
      void queryClient.invalidateQueries({ queryKey: adminKeys.voices() });
      if (persona && persona.id) {
        void queryClient.invalidateQueries({ queryKey: adminKeys.persona(persona.id) });
      }
      if (created) {
        push(`已新建角色：${asText(persona.nickname) || persona.id}（绑定用户 ${asNumber(result.bound)} 个）`);
      } else {
        push(`已保存（同步好友行 ${asNumber(result.friends_synced)} 条）`);
      }
    },
    onError: (error) => push(errorMessage(error), "error"),
  });

  const rerollMutation = useMutation({
    mutationFn: async () =>
      ensureOk(
        await apiPost<ApiRecord>(`/api/admin/personas/${encodeURIComponent(selectedId)}/reroll`, {
          regen_voice: true,
          regen_avatar: true,
        }),
      ),
    onSuccess: (result) => {
      const persona = (result.persona || {}) as PersonaDetail;
      const before = (result.before || {}) as ApiRecord;
      if (persona && persona.id) {
        appliedDetailId.current = persona.id;
        setForm(formFromDetail(persona));
      }
      setPersonaDialog(null);
      void queryClient.invalidateQueries({ queryKey: listKey });
      void queryClient.invalidateQueries({ queryKey: adminKeys.persona(selectedId) });
      push(
        `已重捏：${asText(before.nickname) ? `${asText(before.nickname)} → ` : ""}${
          asText(persona.nickname) || "（未知）"
        }（${asText(persona.city, "城市未变")}）`,
      );
    },
    onError: (error) => {
      setPersonaDialog(null);
      push(errorMessage(error), "error");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async () =>
      ensureOk(await apiDelete<ApiRecord>(`/api/admin/personas/${encodeURIComponent(selectedId)}`)),
    onSuccess: (result) => {
      setPersonaDialog(null);
      setMode("none");
      setSelectedId("");
      appliedDetailId.current = "";
      setForm(emptyForm());
      void queryClient.invalidateQueries({ queryKey: listKey });
      // 删掉角色会改变音色的"被几个角色使用"统计
      void queryClient.invalidateQueries({ queryKey: adminKeys.voices() });
      push(`已删除。数据库备份：${asText(result.backup) || "（无）"}`);
    },
    onError: (error) => {
      setPersonaDialog(null);
      push(errorMessage(error), "error");
    },
  });

  const batchMutation = useMutation({
    mutationFn: async (request: { kind: BatchKind; body: ApiRecord }) =>
      ensureOk(await apiPost<ApiRecord>(BATCH_PATH[request.kind], request.body)),
    onSuccess: (result, request) => {
      setBatch(null);
      setBatchError("");
      push(describeBatchResult(request.kind, result));
      if (request.kind === "recreate") {
        // 全部角色都被换掉了：原来的选中 id 已经不存在，直接收起编辑器。
        setMode("none");
        setSelectedId("");
        setForm(emptyForm());
        appliedDetailId.current = "";
      }
      invalidateAfterBulk();
    },
    onError: (error) => {
      // 对话框保持打开，把后端原因显示在原地，方便直接改参数重试。
      setBatchError(errorMessage(error));
    },
  });

  /* ------------------------------ 交互 ------------------------------ */

  const applySearch = () => setQueryText(search.trim());

  const openCreate = () => {
    appliedDetailId.current = "__new__";
    setSelectedId("");
    setBindUserId("");
    setInitialPosts("");
    setForm(emptyForm());
    setMode("create");
  };

  const openPersona = (persona: PersonaRow) => {
    if (mode === "edit" && persona.id === selectedId) return;
    appliedDetailId.current = "";
    setSelectedId(persona.id);
    setMode("edit");
  };

  const closeEditor = () => {
    setMode("none");
    setSelectedId("");
    appliedDetailId.current = "";
    setForm(emptyForm());
  };

  const save = () => {
    if (mode === "none") return;
    try {
      saveMutation.mutate(payloadFromForm(form));
    } catch (error) {
      push(errorMessage(error), "error");
    }
  };

  const openBatch = (kind: BatchKind) => {
    setBatchError("");
    setBatch(kind);
    if (kind === "recreate") {
      const total = listQuery.data?.counts?.personas ?? 0;
      setRecreateCount(String(total || 11));
      setRecreateConfirm("");
      setAutoFriend(false);
    }
    if (kind === "regenerate-posts") setPostsPerPersona("5");
  };

  const submitBatch = () => {
    if (!batch) return;
    setBatchError("");
    switch (batch) {
      case "reroll-all":
        batchMutation.mutate({ kind: batch, body: { mode: "reroll" } });
        return;
      case "recreate": {
        // 后端 rebuild_all 要求 confirm 等于 RESET（strip + upper 后比较），前端先挡一层。
        if (recreateConfirm.trim().toUpperCase() !== "RESET") {
          setBatchError("请在下方的确认框里输入 RESET（全大写）后再执行");
          return;
        }
        batchMutation.mutate({
          kind: batch,
          body: {
            mode: "recreate",
            count: Number(recreateCount) || 0,
            auto_friend_users: autoFriend,
            confirm: "RESET",
          },
        });
        return;
      }
      case "rematch-media":
        batchMutation.mutate({ kind: batch, body: { limit: 20 } });
        return;
      case "rebalance-genders":
        batchMutation.mutate({ kind: batch, body: { female_ratio: 0.5 } });
        return;
      case "redistribute-voices":
        batchMutation.mutate({ kind: batch, body: {} });
        return;
      case "regenerate-posts":
        batchMutation.mutate({
          kind: batch,
          body: { per_persona: Number(postsPerPersona) || 5 },
        });
        return;
      case "init-life":
        batchMutation.mutate({ kind: batch, body: {} });
        return;
    }
  };

  /* ------------------------------ 派生数据 ------------------------------ */

  const listData = listQuery.data;
  const rows = listData?.personas ?? [];
  const counts = listData?.counts ?? {};
  const users = listData?.users ?? [];
  const detail = detailQuery.data?.persona;
  const detailCounts = detail?._counts ?? {};
  const poolVoices = voicesQuery.data?.voices ?? [];
  const personaTotal = counts.personas ?? rows.length;

  const fishUsable = Boolean(voicesQuery.data?.fish?.usable);
  const editingCountLabel = detail
    ? `好友 ${asNumber(detailCounts.friends)} · 动态 ${asNumber(detailCounts.posts)} · 示例 ${asNumber(
        detailCounts.examples,
      )}`
    : "";

  const voiceOptions: Array<{ value: string; label: string }> = [
    { value: "", label: "从素材池选择…" },
    ...poolVoices.map((voice) => ({
      value: voice.reference_id,
      label: `${voice.name || voice.reference_id} · ${voice.reference_id.slice(0, 8)}${
        voice.assigned ? `（${voice.assigned} 人在用）` : ""
      }`,
    })),
  ];

  const columns: Array<Column<PersonaRow>> = [
    {
      key: "nickname",
      header: "角色",
      render: (row) => (
        <div className="admin-col" style={{ gap: 2 }}>
          <span>
            {row.nickname || "（无名）"}
            {mode === "edit" && row.id === selectedId ? (
              <>
                {" "}
                <Badge tone="info">编辑中</Badge>
              </>
            ) : null}
          </span>
          <span className="admin-mono admin-muted">
            {genderLabel(asText(row.gender))}
            {row.age ? ` · ${row.age} 岁` : ""} · {row.id}
          </span>
        </div>
      ),
    },
    { key: "occupation", header: "职业", render: (row) => row.occupation },
    { key: "city", header: "城市", render: (row) => row.city },
    { key: "language", header: "语言", render: (row) => row.language },
    {
      key: "friend_count",
      header: "好友",
      numeric: true,
      render: (row) => asNumber(row.friend_count),
    },
    {
      key: "fish_voice",
      header: "音色",
      render: (row) => row.fish_voice_name || asText(row.fish_voice).slice(0, 10),
    },
  ];

  /* ------------------------------ 渲染 ------------------------------ */

  return (
    <section className="admin-section">
      <ToastStack toasts={toasts} />

      <div className="admin-section-head">
        <div>
          <h1 className="admin-section-title">角色工坊</h1>
          <p className="admin-section-desc">
            编辑单个角色的全部属性（改动会同步到聊天 / 朗读 / 通话），或对全部角色做重捏与重建。
            所有批量操作执行前都会先备份数据库。
          </p>
        </div>
        <div className="admin-section-actions">
          <Button onClick={() => void listQuery.refetch()} disabled={listQuery.isFetching}>
            {listQuery.isFetching ? "刷新中…" : "刷新"}
          </Button>
          <Button variant="primary" onClick={openCreate}>
            新建测试角色
          </Button>
        </div>
      </div>

      {listQuery.isError ? (
        <ErrorState
          title="读取角色列表失败"
          message={errorMessage(listQuery.error)}
          issues={listQuery.error instanceof AdminApiError ? listQuery.error.issues : undefined}
          onRetry={() => void listQuery.refetch()}
        />
      ) : listData && listData.ok === false ? (
        <ErrorState title="读取角色列表失败" message={issueText(listData.issues)} />
      ) : (
        <>
          <div className="admin-stat-grid">
            <Stat label="角色总数" value={personaTotal} />
            <Stat label="角色动态" value={asNumber(counts.persona_posts)} hint="含图文与纯文字" />
            <Stat label="示例对话" value={asNumber(counts.persona_example_dialogs)} />
            <Stat label="好友关系" value={asNumber(counts.friendships)} />
            <Stat label="用户账号" value={asNumber(counts.v3_users)} hint="重建不会动用户" />
            <Stat
              label="池内音色"
              value={poolVoices.length}
              hint={fishUsable ? "Fish 可用" : "Fish 不可用 / 未配置"}
            />
          </div>

          <div className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">批量工具</h2>
              <span className="admin-card-sub">点击后先在确认框里看清影响范围再执行</span>
            </div>
            <div className="admin-row">
              {BATCH_ACTIONS.map((action) => (
                <Button
                  key={action.kind}
                  size="sm"
                  variant={action.danger ? "danger" : "default"}
                  title={action.hint}
                  onClick={() => openBatch(action.kind)}
                >
                  {action.label}
                </Button>
              ))}
            </div>
            <p className="admin-muted" style={{ margin: 0 }}>
              「彻底重建」会删除全部 AI 角色及其动态、示例对话、人物关系、好友关系与相关聊天记录
              （用户账号保留），再按素材池重新生成，必须输入 <span className="admin-mono">RESET</span> 才会执行。
            </p>
          </div>

          <div className="admin-grid-2">
            {/* ---------- 左：列表 ---------- */}
            <div className="admin-card">
              <div className="admin-card-head">
                <h2 className="admin-card-title">全部角色</h2>
                <span className="admin-card-sub">共 {rows.length} 个</span>
              </div>
              <div className="admin-row">
                <div style={{ flex: "1 1 180px" }}>
                  <TextInput
                    value={search}
                    onChange={setSearch}
                    placeholder="搜索昵称 / id / 城市 / 国家"
                    type="search"
                    onEnter={applySearch}
                  />
                </div>
                <Button size="sm" onClick={applySearch}>
                  搜索
                </Button>
                {queryText ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setSearch("");
                      setQueryText("");
                    }}
                  >
                    清除
                  </Button>
                ) : null}
              </div>
              {listQuery.isPending ? (
                <LoadingState label="正在读取角色…" />
              ) : (
                <Table<PersonaRow>
                  columns={columns}
                  rows={rows}
                  rowKey={(row) => row.id}
                  onRowClick={openPersona}
                  empty={
                    <EmptyState
                      title={queryText ? "没有匹配的角色" : "还没有角色"}
                      description={
                        queryText
                          ? "换个关键词，或点「清除」看全部。"
                          : "可以用右上角「新建测试角色」，或用批量工具「彻底重建」生成一批。"
                      }
                    />
                  }
                />
              )}
            </div>

            {/* ---------- 右：详情编辑 ---------- */}
            <div className="admin-card">
              <div className="admin-card-head">
                <h2 className="admin-card-title">
                  {mode === "create"
                    ? "新建测试角色"
                    : mode === "edit"
                      ? `编辑角色：${form.nickname || selectedId}`
                      : "角色详情"}
                </h2>
                <span className="admin-card-sub">
                  {mode === "edit" && detail
                    ? `${editingCountLabel}${detail.fish_voice_name ? ` · 音色 ${detail.fish_voice_name}` : ""}`
                    : mode === "create"
                      ? "不绑定用户时只入库"
                      : ""}
                </span>
              </div>

              {mode === "none" ? (
                <EmptyState
                  title="未选择角色"
                  description="从左侧点一个角色开始编辑，或点右上角「新建测试角色」。"
                />
              ) : mode === "edit" && detailQuery.isPending ? (
                <LoadingState label="正在读取角色详情…" />
              ) : mode === "edit" && detailQuery.isError ? (
                <ErrorState
                  title="读取角色详情失败"
                  message={errorMessage(detailQuery.error)}
                  issues={
                    detailQuery.error instanceof AdminApiError ? detailQuery.error.issues : undefined
                  }
                  onRetry={() => void detailQuery.refetch()}
                />
              ) : (
                <div className="admin-col">
                  <div className="admin-form-grid">
                    <Field label="昵称" htmlFor="ps-nickname">
                      <TextInput
                        id="ps-nickname"
                        value={form.nickname}
                        onChange={(value) => setForm({ ...form, nickname: value })}
                        placeholder="角色昵称"
                      />
                    </Field>
                    <Field label="性别" htmlFor="ps-gender">
                      <Select
                        id="ps-gender"
                        value={form.gender}
                        onChange={(value) => setForm({ ...form, gender: value })}
                        options={GENDER_OPTIONS}
                      />
                    </Field>
                    <Field label="年龄" htmlFor="ps-age" hint="留空表示不修改">
                      <TextInput
                        id="ps-age"
                        type="number"
                        value={form.age}
                        onChange={(value) => setForm({ ...form, age: value })}
                      />
                    </Field>
                    <Field label="语言" htmlFor="ps-language">
                      <Select
                        id="ps-language"
                        value={form.language}
                        onChange={(value) => setForm({ ...form, language: value })}
                        options={LANGUAGE_OPTIONS}
                      />
                    </Field>
                    <Field label="国家" htmlFor="ps-country">
                      <TextInput
                        id="ps-country"
                        value={form.country}
                        onChange={(value) => setForm({ ...form, country: value })}
                      />
                    </Field>
                    <Field label="城市" htmlFor="ps-city">
                      <TextInput
                        id="ps-city"
                        value={form.city}
                        onChange={(value) => setForm({ ...form, city: value })}
                      />
                    </Field>
                    <Field label="时区" htmlFor="ps-timezone" hint="例如 Asia/Shanghai">
                      <TextInput
                        id="ps-timezone"
                        value={form.timezone}
                        onChange={(value) => setForm({ ...form, timezone: value })}
                      />
                    </Field>
                    <Field label="职业" htmlFor="ps-occupation">
                      <TextInput
                        id="ps-occupation"
                        value={form.occupation}
                        onChange={(value) => setForm({ ...form, occupation: value })}
                      />
                    </Field>
                    <Field label="状态" htmlFor="ps-status">
                      <Select
                        id="ps-status"
                        value={form.status}
                        onChange={(value) => setForm({ ...form, status: value })}
                        options={STATUS_OPTIONS}
                      />
                    </Field>
                    <Field label="生命周期" htmlFor="ps-lifecycle">
                      <Select
                        id="ps-lifecycle"
                        value={form.lifecycle}
                        onChange={(value) => setForm({ ...form, lifecycle: value })}
                        options={LIFECYCLE_OPTIONS}
                      />
                    </Field>
                    <Field
                      label="过期时间"
                      htmlFor="ps-expires-at"
                      hint="guest 角色用；permanent 留空"
                    >
                      <TextInput
                        id="ps-expires-at"
                        value={form.expires_at}
                        onChange={(value) => setForm({ ...form, expires_at: value })}
                      />
                    </Field>
                    <Field label="原型 archetype_id" htmlFor="ps-archetype" hint="留空表示不绑定原型">
                      <TextInput
                        id="ps-archetype"
                        value={form.archetype_id}
                        onChange={(value) => setForm({ ...form, archetype_id: value })}
                      />
                    </Field>
                    <Field label="头像 seed" htmlFor="ps-avatar-seed">
                      <TextInput
                        id="ps-avatar-seed"
                        value={form.avatar_seed}
                        onChange={(value) => setForm({ ...form, avatar_seed: value })}
                      />
                    </Field>
                    <Field label="头像风格" htmlFor="ps-avatar-style" hint="例如 avataaars">
                      <TextInput
                        id="ps-avatar-style"
                        value={form.avatar_style}
                        onChange={(value) => setForm({ ...form, avatar_style: value })}
                      />
                    </Field>
                  </div>

                  <Field label="简介 bio" htmlFor="ps-bio">
                    <TextArea
                      id="ps-bio"
                      rows={3}
                      value={form.bio}
                      onChange={(value) => setForm({ ...form, bio: value })}
                    />
                  </Field>

                  <Field label="性格 personality" htmlFor="ps-personality">
                    <TextArea
                      id="ps-personality"
                      rows={2}
                      value={form.personality}
                      onChange={(value) => setForm({ ...form, personality: value })}
                    />
                  </Field>

                  <Field label="兴趣 interests" hint="回车或点「添加」追加，× 删除">
                    <TagCloud
                      tags={form.interests}
                      onAdd={(tag) =>
                        setForm({ ...form, interests: [...form.interests, tag] })
                      }
                      onRemove={(tag) =>
                        setForm({
                          ...form,
                          interests: form.interests.filter((item) => item !== tag),
                        })
                      }
                    />
                  </Field>

                  <Field
                    label="Fish 音色"
                    hint={
                      detail?.fish_voice_name
                        ? `当前：${detail.fish_voice_name}（可填素材池名称，或 reference_id）`
                        : "可填素材池里的名称，或 Fish 的 reference_id；留空表示不用 Fish 音色"
                    }
                    htmlFor="ps-fish-voice"
                  >
                    <div className="admin-col">
                      <Select
                        id="ps-fish-voice-select"
                        value={
                          voiceOptions.some((option) => option.value === form.fish_voice_id)
                            ? form.fish_voice_id
                            : ""
                        }
                        onChange={(value) => {
                          if (!value) return;
                          setForm({ ...form, fish_voice_id: value });
                        }}
                        options={voiceOptions}
                      />
                      <TextInput
                        id="ps-fish-voice"
                        value={form.fish_voice_id}
                        onChange={(value) => setForm({ ...form, fish_voice_id: value })}
                        placeholder="名称或 reference_id"
                      />
                      <AudioPreview
                        label="试听音色"
                        disabled={!form.fish_voice_id.trim()}
                        load={() => requestPreviewBlob(form.fish_voice_id.trim())}
                      />
                    </div>
                  </Field>

                  <Field
                    label="Edge 兜底音色 voice_id"
                    htmlFor="ps-voice-id"
                    hint="Fish 不可用时用；留空自动按语言选"
                  >
                    <TextInput
                      id="ps-voice-id"
                      value={form.voice_id}
                      onChange={(value) => setForm({ ...form, voice_id: value })}
                    />
                  </Field>

                  <Field label="avatar_options（JSON）" hint="头像生成参数，一般不用手改">
                    <TextArea
                      mono
                      rows={4}
                      value={form.avatar_options_text}
                      onChange={(value) => setForm({ ...form, avatar_options_text: value })}
                    />
                  </Field>

                  <Field label="ai_config（JSON）" hint="生成来源等信息">
                    <TextArea
                      mono
                      rows={3}
                      value={form.ai_config_text}
                      onChange={(value) => setForm({ ...form, ai_config_text: value })}
                    />
                  </Field>

                  {/* 表达 DNA：结构化编辑器（原来是一个裸 JSON 文本框，运营改不动）。
                      仍然以 speech_dna_text 为唯一来源，只是把它渲染成可操作的控件，
                      这样保存链路完全不用改，也不会和「重捏此人」等其它入口产生两套数据。 */}
                  <div style={{ gridColumn: "1 / -1" }}>
                    <Field
                      label="表达 DNA"
                      hint="决定角色说话像不像一个人：断句、直白度、幽默、话多话少…留空 = 用词库默认值"
                    >
                      <SpeechDnaEditor
                        value={speechDnaObject(form.speech_dna_text)}
                        onChange={(next) =>
                          setForm({ ...form, speech_dna_text: JSON.stringify(next, null, 2) })
                        }
                      />
                    </Field>
                    <details style={{ marginTop: 8 }}>
                      <summary className="admin-muted" style={{ fontSize: 12, cursor: "pointer" }}>
                        高级：查看/直接编辑 JSON（排查用）
                      </summary>
                      <TextArea
                        mono
                        rows={6}
                        value={form.speech_dna_text}
                        onChange={(value) => setForm({ ...form, speech_dna_text: value })}
                      />
                    </details>
                  </div>

                  {mode === "create" ? (
                    <>
                      <Field
                        label="绑定用户（可选）"
                        htmlFor="ps-bind-user"
                        hint="选一个用户，角色会直接出现在 TA 的消息列表里"
                      >
                        <Select
                          id="ps-bind-user"
                          value={bindUserId}
                          onChange={setBindUserId}
                          options={[
                            { value: "", label: "不绑定" },
                            ...users.map((user) => ({
                              value: user.id,
                              label: `${user.display_name || user.device_key_masked || user.id} · 好友 ${asNumber(
                                user.friend_count,
                              )}`,
                            })),
                          ]}
                        />
                      </Field>
                      <Field label="初始动态（可选）" htmlFor="ps-initial-posts" hint="每行一条，最多 5 条">
                        <TextArea
                          id="ps-initial-posts"
                          rows={3}
                          value={initialPosts}
                          onChange={setInitialPosts}
                          placeholder={"今天去看了海\n新开的咖啡店不错"}
                        />
                      </Field>
                    </>
                  ) : null}

                  <div className="admin-row">
                    <Button variant="primary" loading={saveMutation.isPending} onClick={save}>
                      保存
                    </Button>
                    {mode === "edit" ? (
                      <>
                        <Button onClick={() => setPersonaDialog("reroll")}>重捏此人</Button>
                        <Button variant="danger" onClick={() => setPersonaDialog("delete")}>
                          删除此人
                        </Button>
                      </>
                    ) : null}
                    <Button variant="ghost" onClick={closeEditor} disabled={saveMutation.isPending}>
                      取消
                    </Button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* ---------- 单个角色：重捏确认 ---------- */}
      <ConfirmDialog
        open={personaDialog === "reroll"}
        title="重捏这个角色？"
        confirmLabel="确认重捏"
        busy={rerollMutation.isPending}
        onConfirm={() => rerollMutation.mutate()}
        onCancel={() => setPersonaDialog(null)}
        body={
          <div className="admin-col">
            <p style={{ margin: 0 }}>
              昵称、性别、年龄、城市、职业、兴趣、人设、头像和音色都会重新随机生成。
            </p>
            <p style={{ margin: 0 }}>
              角色 ID、好友关系、聊天记录都会保留；执行前会先备份数据库。此操作不可撤销。
            </p>
          </div>
        }
      />

      {/* ---------- 单个角色：删除确认 ---------- */}
      <ConfirmDialog
        open={personaDialog === "delete"}
        title="删除这个角色？"
        danger
        confirmLabel="确认删除"
        busy={deleteMutation.isPending}
        onConfirm={() => deleteMutation.mutate()}
        onCancel={() => setPersonaDialog(null)}
        body={
          <div className="admin-col">
            <p style={{ margin: 0 }}>
              会删除角色 <strong>{asText(detail?.nickname) || selectedId}</strong>{" "}
              及其全部关联数据，并先自动备份数据库：
            </p>
            <ul className="admin-muted" style={{ margin: 0, paddingLeft: 18 }}>
              <li>
                动态 {asNumber(detailCounts.posts)} 条（含点赞/评论）、示例对话{" "}
                {asNumber(detailCounts.examples)} 条
              </li>
              <li>好友关系 {asNumber(detailCounts.friends)} 个，以及相关的聊天记录</li>
              <li>人物关系、日程、社交事件等也会一并清理</li>
            </ul>
            <p style={{ margin: 0 }}>删除后不可恢复（只能从数据库备份里找回）。</p>
          </div>
        }
      />

      {/* ---------- 批量操作：统一确认 ---------- */}
      <ConfirmDialog
        open={batch !== null}
        title={batch ? BATCH_TITLE[batch] : ""}
        danger={batch === "recreate"}
        confirmLabel={batch === "recreate" ? "确认彻底重建" : "确认执行"}
        busy={batchMutation.isPending}
        onConfirm={submitBatch}
        onCancel={() => {
          if (batchMutation.isPending) return;
          setBatch(null);
          setBatchError("");
        }}
        body={
          batch ? (
            <div className="admin-col">
              {batchError ? <div className="admin-field-error">{batchError}</div> : null}

              {batch === "reroll-all" ? (
                <>
                  <p style={{ margin: 0 }}>
                    将对全部 {personaTotal} 个角色原地重捏：保留角色 ID、好友关系、聊天记录，
                    只重掷人设 / 头像 / 音色。执行前自动备份数据库。
                  </p>
                  <p className="admin-muted" style={{ margin: 0 }}>
                    当前：动态 {asNumber(counts.persona_posts)} 条 · 示例{" "}
                    {asNumber(counts.persona_example_dialogs)} 条 · 好友关系{" "}
                    {asNumber(counts.friendships)} 条 · 用户 {asNumber(counts.v3_users)} 个
                  </p>
                </>
              ) : null}

              {batch === "recreate" ? (
                <>
                  <p style={{ margin: 0 }}>
                    会<strong>删除全部 AI 角色</strong>（共 {personaTotal} 个）及其动态、示例对话、
                    人物关系、好友关系，并清空这些角色的<strong>聊天记录</strong>；
                    用户账号不受影响。执行前自动备份数据库。
                  </p>
                  <p className="admin-muted" style={{ margin: 0 }}>
                    将删除：动态 {asNumber(counts.persona_posts)} 条 · 示例{" "}
                    {asNumber(counts.persona_example_dialogs)} 条 · 好友关系{" "}
                    {asNumber(counts.friendships)} 条 · 关系{" "}
                    {asNumber(counts.persona_relationships)} 条；{asNumber(counts.v3_users)} 个用户保留。
                  </p>
                  <Field label="重建角色数量" htmlFor="ps-recreate-count" hint="填 0 表示沿用当前角色数">
                    <TextInput
                      id="ps-recreate-count"
                      type="number"
                      value={recreateCount}
                      onChange={setRecreateCount}
                    />
                  </Field>
                  <div className="admin-row">
                    <input
                      id="ps-recreate-auto-friend"
                      type="checkbox"
                      checked={autoFriend}
                      onChange={(event) => setAutoFriend(event.target.checked)}
                    />
                    <label htmlFor="ps-recreate-auto-friend">
                      重建后把所有新角色自动加为所有用户的好友
                    </label>
                  </div>
                  <Field
                    label="输入 RESET 确认"
                    htmlFor="ps-recreate-confirm"
                    hint="必须输入 RESET（全大写），后端同样校验该字符串"
                  >
                    <TextInput
                      id="ps-recreate-confirm"
                      value={recreateConfirm}
                      onChange={setRecreateConfirm}
                      placeholder="RESET"
                    />
                  </Field>
                  <p className="admin-muted" style={{ margin: 0 }}>
                    旧对话会随角色一起被清除，用户会看到好友列表里这些角色消失。此操作不可撤销。
                  </p>
                </>
              ) : null}

              {batch === "rematch-media" ? (
                <p style={{ margin: 0 }}>
                  用 LLM 按图片内容重写图文动态的文案：图片不变，只改文字，每点一次修 20 条，
                  可重复执行直到修完。会消耗 LLM 额度。
                </p>
              ) : null}

              {batch === "rebalance-genders" ? (
                <p style={{ margin: 0 }}>
                  把现有 {personaTotal} 个角色的性别调整到约 1:1，会更换部分角色的
                  名字 / 头像 / 音色；角色 ID、好友、聊天记录保留，执行前自动备份数据库。
                </p>
              ) : null}

              {batch === "redistribute-voices" ? (
                <p style={{ margin: 0 }}>
                  把素材池里的 {poolVoices.length} 个音色重新均匀分配给全部 {personaTotal} 个角色
                  （同一角色 ID / 好友 / 聊天不变，只换声音）。执行前自动备份数据库。
                </p>
              ) : null}

              {batch === "regenerate-posts" ? (
                <>
                  <p style={{ margin: 0 }}>
                    会<strong>删除现有全部角色动态</strong>（含点赞/评论），再按语言素材库重新生成，
                    时间错峰、约 1/3 带图。
                  </p>
                  <Field
                    label="每个角色生成几条"
                    htmlFor="ps-posts-per-persona"
                    hint="默认 5 条"
                  >
                    <TextInput
                      id="ps-posts-per-persona"
                      type="number"
                      value={postsPerPersona}
                      onChange={setPostsPerPersona}
                    />
                  </Field>
                </>
              ) : null}

              {batch === "init-life" ? (
                <p style={{ margin: 0 }}>
                  给全部角色补齐作息表、Speech DNA + 示例对话、人物关系（0 次 LLM）。
                  之后生活事件和事件动态才会由后台任务自动生成。不会删除任何数据。
                </p>
              ) : null}
            </div>
          ) : null
        }
      />
    </section>
  );
}
