"use client";

/**
 * `/api/v2` 客户端。
 *
 * 静态导出：所有请求必须走**相对路径**，构建期不读任何 host / env。
 * 每个调用都带超时；失败抛 `ApiError`，由 UI 转成中文提示 + 空态。
 */

import { asStringArray, isRecord, num, str } from "@/lib/guards";
import {
  parseBootstrap,
  parseChatTurn,
  parseExpressions,
  parseFriend,
  parseFriendsPayload,
  parseLlmSaveResult,
  parseLlmSettings,
  parseLlmVerify,
  parseMemoryPayload,
  parseMessage,
  parseMessages,
  parseMoments,
  parsePostedMoment,
  parseBatchTranslationResponse,
  parseSttSaveResult,
  parseSttSettings,
  parseSttVerify,
  parseTranslationResponse,
  type BootstrapPayload,
  type ChatTurnResponse,
  type Expression,
  type FactSheet,
  type Friend,
  type FriendsPayload,
  type LlmSaveInput,
  type LlmSaveResult,
  type LlmSettingsPayload,
  type LlmTestInput,
  type LlmVerifyResult,
  type MemoryPayload,
  type Message,
  type Moment,
  type PostedMoment,
  type SttSaveInput,
  type SttSaveResult,
  type SttSettingsPayload,
  type BatchTranslationResponse,
  type SttTestInput,
  type SttVerifyResult,
  type TranslationResult,
  type UserProfile,
} from "@/lib/v2/types";

const BASE = "/api/v2";
const DEFAULT_TIMEOUT = 9000;

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

interface RequestOptions {
  method?: string;
  body?: BodyInit | null;
  timeoutMs?: number;
  headers?: Record<string, string>;
}

async function detailOf(res: Response): Promise<string> {
  try {
    const raw = (await res.json()) as unknown;
    if (typeof raw === "object" && raw !== null) {
      const detail = (raw as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail;
      if (Array.isArray(detail) && detail.length > 0) {
        const first = detail[0] as { msg?: unknown } | undefined;
        if (first && typeof first.msg === "string") return first.msg;
      }
    }
  } catch {
    /* 非 JSON 错误体 */
  }
  if (res.status === 404) return "接口不存在（后端 2.0 服务可能尚未启动）";
  if (res.status >= 500) return "服务器内部错误，请稍后再试";
  return `请求失败（HTTP ${res.status}）`;
}

/** 任意 `/api/...` 绝对路径请求（设置页的 LLM 接口在 `/api/llm/*`）。 */
async function requestAt<T>(
  path: string,
  parse: (raw: unknown) => T,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body = null, timeoutMs = DEFAULT_TIMEOUT, headers } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let res: Response;
  try {
    res = await fetch(path, {
      method,
      body,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(body && !(body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
        ...headers,
      },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError("请求超时，请检查网络或后端服务", 0);
    }
    throw new ApiError("无法连接后端服务", 0);
  } finally {
    clearTimeout(timer);
  }

  if (!res.ok) throw new ApiError(await detailOf(res), res.status);

  if (res.status === 204) return parse(null);
  const text = await res.text();
  if (!text.trim()) return parse(null);
  try {
    return parse(JSON.parse(text) as unknown);
  } catch {
    throw new ApiError("响应格式异常", res.status);
  }
}

/** `/api/v2/...` 相对路径请求。 */
function request<T>(
  path: string,
  parse: (raw: unknown) => T,
  options: RequestOptions = {},
): Promise<T> {
  return requestAt(`${BASE}${path}`, parse, options);
}

function parseFriendEnvelope(raw: unknown): Friend | null {
  if (typeof raw === "object" && raw !== null && "friend" in raw) {
    return parseFriend((raw as { friend?: unknown }).friend);
  }
  return parseFriend(raw);
}

function parseExpressionEnvelope(raw: unknown): Expression {
  const inner =
    typeof raw === "object" && raw !== null && "expression" in raw
      ? (raw as { expression?: unknown }).expression
      : raw;
  const list = parseExpressions([inner]);
  const item = list[0];
  if (!item) throw new ApiError("表达本响应格式异常", 500);
  return item;
}

export const api = {
  /** 首屏聚合数据。后端未实现时退回 friends 列表。 */
  async bootstrap(): Promise<BootstrapPayload> {
    try {
      return await request("/bootstrap", parseBootstrap);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
        const friends = await api.friends();
        return {
          profile: {
            nickname: "",
            native_language: "zh",
            target_languages: ["en"],
            fact_sheet: {
              profile: { nickname: "", city: "", major_or_job: "", routine: "" },
              interests: [],
              language_profile: { target: "en", weaknesses: [] },
              recent_events: [],
            },
          },
          friend_count: friends.friends.length,
          expressions_total: 0,
          expressions_learning: 0,
          llm_mock: false,
          pending_onboarding: false,
          friends: friends.friends,
          requests: friends.requests,
        };
      }
      throw err;
    }
  },

  friends(): Promise<FriendsPayload> {
    return request("/friends", parseFriendsPayload);
  },

  async friend(id: string): Promise<Friend> {
    const friend = await request(`/friends/${encodeURIComponent(id)}`, parseFriendEnvelope);
    if (!friend) throw new ApiError("好友不存在", 404);
    return friend;
  },

  friendStatus(id: string): Promise<{ replying: boolean; started_at: string | null }> {
    return request(`/friends/${encodeURIComponent(id)}/status`, (raw) => {
      const data = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
      return {
        replying: data.replying === true,
        started_at: typeof data.started_at === "string" ? data.started_at : null,
      };
    });
  },

  discover(filters: { nationality?: string; language?: string; gender?: string }): Promise<{
    friend: Friend;
    message: Message | null;
  }> {
    const body: Record<string, string> = {};
    if (filters.nationality) body.nationality = filters.nationality;
    if (filters.language) body.language = filters.language;
    if (filters.gender) body.gender = filters.gender;
    return request(
      "/friends/discover",
      (raw) => {
        const r = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
        const friend = parseFriend(r.friend);
        if (!friend) throw new ApiError("偶遇响应缺少好友信息", 500);
        const msg = r.message;
        return {
          friend,
          message: msg ? parseMessages([msg])[0] ?? null : null,
        };
      },
      { method: "POST", body: JSON.stringify(body), timeoutMs: 30000 },
    );
  },

  acceptFriend(id: string): Promise<{ friend: Friend; message: Message | null }> {
    return request(
      `/friends/${encodeURIComponent(id)}/accept`,
      (raw) => {
        const r = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
        const friend = parseFriend(r.friend);
        if (!friend) throw new ApiError("响应缺少好友信息", 500);
        const msg = r.message;
        return { friend, message: msg ? parseMessages([msg])[0] ?? null : null };
      },
      { method: "POST", timeoutMs: 30000 },
    );
  },

  declineFriend(id: string): Promise<void> {
    return request(`/friends/${encodeURIComponent(id)}/decline`, () => undefined, { method: "POST" });
  },

  messages(friendId: string, limit = 30, before?: string): Promise<Message[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (before) params.set("before", before);
    return request(
      `/friends/${encodeURIComponent(friendId)}/messages?${params.toString()}`,
      parseMessages,
    );
  },

  /** 消息列表：优先 /friends/{id}/messages（后端实现），旧文档路径 /chat 兜底。 */
  async chatMessages(friendId: string, limit = 30): Promise<Message[]> {
    try {
      return await api.messages(friendId, limit);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        return request(
          `/chat/${encodeURIComponent(friendId)}/messages?limit=${limit}`,
          parseMessages,
        );
      }
      throw err;
    }
  },

  markRead(friendId: string): Promise<void> {
    return request(`/friends/${encodeURIComponent(friendId)}/read`, () => undefined, { method: "POST" });
  },

  /** 用户离开聊天窗：触发后台记忆提炼（异步，立即返回）。 */
  idle(friendId: string): Promise<void> {
    return request(
      `/chat/${encodeURIComponent(friendId)}/idle`,
      () => undefined,
      { method: "POST", timeoutMs: 4000 },
    ).catch(() => undefined);
  },

  sendVoice(
    friendId: string,
    blob: Blob,
    durationMs: number,
    filename = "voice.webm",
  ): Promise<ChatTurnResponse> {
    const form = new FormData();
    form.append("audio", blob, filename);
    form.append("duration_ms", String(Math.max(0, Math.round(durationMs))));
    return request(`/chat/${encodeURIComponent(friendId)}/voice`, parseChatTurn, {
      method: "POST",
      body: form,
      timeoutMs: 120000,
    });
  },

  /** 发送图片消息（多模态）：multipart image + 可选 caption。 */
  sendImage(friendId: string, file: Blob, caption = "", filename = "image.png"): Promise<ChatTurnResponse> {
    const form = new FormData();
    form.append("image", file, filename);
    form.append("text", caption.trim());
    return request(`/chat/${encodeURIComponent(friendId)}/image`, parseChatTurn, {
      method: "POST",
      body: form,
      timeoutMs: 180000,
    });
  },

  /** 结束通话：写入一条 call_summary 聊天记录。 */
  callSummary(
    friendId: string,
    input: { duration_s: number; turns: number },
  ): Promise<Message | null> {
    return request(
      `/chat/${encodeURIComponent(friendId)}/call/summary`,
      (raw) => {
        const r = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
        return parseMessage(r.message);
      },
      { method: "POST", body: JSON.stringify(input), timeoutMs: 20000 },
    );
  },

  sendText(friendId: string, text: string): Promise<ChatTurnResponse> {
    return request(`/chat/${encodeURIComponent(friendId)}/text`, parseChatTurn, {
      method: "POST",
      body: JSON.stringify({ text, context: null }),
      timeoutMs: 120000,
    });
  },

  moments(): Promise<Moment[]> {
    return request("/moments", parseMoments);
  },

  generateMoments(): Promise<Moment[]> {
    return request("/moments/generate", parseMoments, { method: "POST", timeoutMs: 60000 });
  },

  /** 发布我的动态（AI 好友评论异步生成，前端轮询 /moments 取评论）。 */
  postMoment(input: { content: string; mood_tag?: string }): Promise<PostedMoment> {
    return request("/moments", parsePostedMoment, {
      method: "POST",
      body: JSON.stringify(input),
      timeoutMs: 30000,
    });
  },

  /** 发布带图动态：multipart content + mood_tag + image。 */
  postMomentPhoto(input: {
    image: Blob;
    content?: string;
    mood_tag?: string;
    filename?: string;
  }): Promise<PostedMoment> {
    const form = new FormData();
    form.append("image", input.image, input.filename ?? "moment.png");
    form.append("content", (input.content ?? "").trim());
    form.append("mood_tag", input.mood_tag ?? "");
    return request("/moments/photo", parsePostedMoment, {
      method: "POST",
      body: form,
      timeoutMs: 60000,
    });
  },

  commentMomentVoice(momentId: string, blob: Blob, durationMs: number, filename = "comment.webm"): Promise<{
    friend_id: string;
    moment: Moment | null;
  }> {
    const form = new FormData();
    form.append("audio", blob, filename);
    form.append("duration_ms", String(Math.max(0, Math.round(durationMs))));
    return request(
      `/moments/${encodeURIComponent(momentId)}/comment`,
      (raw) => {
        const r = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
        const moment = r.moment ? parseMoments([r.moment])[0] ?? null : null;
        return { friend_id: typeof r.friend_id === "string" ? r.friend_id : "", moment };
      },
      { method: "POST", body: form, timeoutMs: 120000 },
    );
  },

  commentMomentText(momentId: string, text: string): Promise<{ friend_id: string; moment: Moment | null }> {
    return request(
      `/moments/${encodeURIComponent(momentId)}/comment`,
      (raw) => {
        const r = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
        const moment = r.moment ? parseMoments([r.moment])[0] ?? null : null;
        return { friend_id: typeof r.friend_id === "string" ? r.friend_id : "", moment };
      },
      { method: "POST", body: JSON.stringify({ text }), timeoutMs: 120000 },
    );
  },

  expressions(status?: "learning" | "mastered"): Promise<Expression[]> {
    const qs = status ? `?status=${status}` : "";
    return request(`/expressions${qs}`, parseExpressions);
  },

  async createExpression(input: {
    original_text: string;
    better_expression: string;
    explanation?: string;
    source_friend_id?: string;
  }): Promise<Expression> {
    return request("/expressions", parseExpressionEnvelope, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  async patchExpression(id: string, patch: { status?: string }): Promise<Expression> {
    return request(`/expressions/${encodeURIComponent(id)}`, parseExpressionEnvelope, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
  },

  deleteExpression(id: string): Promise<void> {
    return request(`/expressions/${encodeURIComponent(id)}`, () => undefined, { method: "DELETE" });
  },

  /** 删除好友（后端会级联删除消息/朋友圈并清理音频）。 */
  deleteFriend(id: string): Promise<{ friend_id: string; deleted_audio?: number }> {
    return request(
      `/friends/${encodeURIComponent(id)}`,
      (raw) => {
        const data = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
        return {
          friend_id: typeof data.friend_id === "string" ? data.friend_id : id,
          deleted_audio: typeof data.deleted_audio === "number" ? data.deleted_audio : undefined,
        };
      },
      { method: "DELETE" },
    );
  },

  memories(): Promise<MemoryPayload> {
    return request("/memories", parseMemoryPayload);
  },

  async putMemories(input: {
    nickname?: string;
    native_language?: string;
    target_languages?: string[];
    fact_sheet?: FactSheet;
  }): Promise<MemoryPayload> {
    return request("/memories", parseMemoryPayload, {
      method: "PUT",
      body: JSON.stringify(input),
    });
  },

  ttsSample(text: string, voice?: string): string {
    const params = new URLSearchParams({ text });
    if (voice) params.set("voice", voice);
    return `${BASE}/tts/sample?${params.toString()}`;
  },

  audioUrl(message: Message): string {
    if (message.audio_url) return message.audio_url;
    if (message.audio_path) {
      const name = message.audio_path.split("/").pop() ?? "";
      return name ? `${BASE}/audio/${encodeURIComponent(name)}` : "";
    }
    return "";
  },
};

export type { Friend, Message, Moment, Expression, MemoryPayload, UserProfile };

/* ================================================================== */
/* 偶遇筛选：声线原型元数据（GET /api/v2/archetypes）                    */
/* ================================================================== */

export interface ArchetypeFilters {
  /** 后端 country 原值（United States / United Kingdom / Japan ...） */
  countries: string[];
  /** 后端 language 原值（en / ja / ko / pt / es ...） */
  languages: string[];
}

export function getArchetypeFilters(): Promise<ArchetypeFilters> {
  return request(
    "/archetypes",
    (raw) => {
      const r = isRecord(raw) ? raw : {};
      return {
        countries: asStringArray(r.countries) ?? [],
        languages: asStringArray(r.languages) ?? [],
      };
    },
    { timeoutMs: 8000 },
  );
}

/* ================================================================== */
/* 个人设置：LLM（/api/llm/*）与 STT（/api/v2/settings/*）              */
/* ================================================================== */

function parseSettingsAggregate(raw: unknown): {
  llm: LlmSettingsPayload | null;
  stt: SttSettingsPayload | null;
} {
  if (!isRecord(raw)) return { llm: null, stt: null };
  return {
    llm: isRecord(raw.llm) ? parseLlmSettings(raw.llm) : null,
    stt: isRecord(raw.stt) ? parseSttSettings(raw.stt) : null,
  };
}

/** 聚合设置（docs/V2_STT.md §3 GET /api/v2/settings）；后端未实现时抛 404。 */
export function getSettings(): Promise<{ llm: LlmSettingsPayload | null; stt: SttSettingsPayload | null }> {
  return requestAt("/api/v2/settings", parseSettingsAggregate);
}

/* ---------------------------- LLM ---------------------------- */

export function getLlmSettings(): Promise<LlmSettingsPayload> {
  return requestAt("/api/llm/config", parseLlmSettings);
}

export function testLlmSettings(input: LlmTestInput): Promise<LlmVerifyResult> {
  return requestAt("/api/llm/test", parseLlmVerify, {
    method: "POST",
    body: JSON.stringify(input),
    timeoutMs: 45000,
  });
}

export function saveLlmSettings(input: LlmSaveInput): Promise<LlmSaveResult> {
  return requestAt("/api/llm/config", parseLlmSaveResult, {
    method: "POST",
    body: JSON.stringify(input),
    timeoutMs: 45000,
  });
}

export interface ResetResult {
  ok: boolean;
  message: string;
}

export function resetLlmSettings(): Promise<ResetResult> {
  return requestAt(
    "/api/llm/reset",
    (raw) => ({
      ok: isRecord(raw) ? raw.ok !== false : true,
      message: isRecord(raw) ? str(raw.message) || "已恢复为 .env 默认配置" : "已恢复为 .env 默认配置",
    }),
    { method: "POST", timeoutMs: 20000 },
  );
}

/* ---------------------------- STT ---------------------------- */

/**
 * 读取 STT 配置。优先 `/api/v2/settings/stt`；
 * 若该接口尚未实现（404），退回聚合接口 `/api/v2/settings` 的 `stt` 字段。
 */
export async function getSttSettings(): Promise<SttSettingsPayload> {
  try {
    return await requestAt("/api/v2/settings/stt", parseSttSettings);
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
      const aggregate = await getSettings();
      if (aggregate.stt) return aggregate.stt;
    }
    throw err;
  }
}

export function saveSttSettings(input: SttSaveInput): Promise<SttSaveResult> {
  return requestAt("/api/v2/settings/stt", parseSttSaveResult, {
    method: "POST",
    body: JSON.stringify(input),
    timeoutMs: 60000,
  });
}

export function testSttSettings(input: SttTestInput): Promise<SttVerifyResult> {
  return requestAt("/api/v2/settings/stt/test", parseSttVerify, {
    method: "POST",
    body: JSON.stringify(input),
    timeoutMs: 45000,
  });
}

export function resetSttSettings(): Promise<SttSettingsPayload> {
  return requestAt("/api/v2/settings/stt/reset", parseSttSettings, {
    method: "POST",
    timeoutMs: 20000,
  });
}


/** 把设置页的接口错误翻译成可读中文（401/超时/404/5xx 区分）。 */
export function apiErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "密钥无效或未授权（401），请检查 API Key 是否正确";
    if (err.status === 403) return "没有权限（403），请检查该密钥是否可用";
    if (err.status === 404) return "接口不存在（404）：后端可能尚未升级到该版本";
    if (err.status === 429) return "请求太频繁（429），请稍后再试";
    if (err.status >= 500) return `服务器错误（${err.status}），请查看后端日志`;
    if (/超时|timeout/i.test(err.message)) return "连接超时：当前网络无法访问该地址";
    return err.message;
  }
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === "string" && err) return err;
  return "操作失败，请稍后重试";
}


/* ================================================================== */
/* 聊天内翻译（ChatView「译」按钮 / 批量最近消息）                       */
/* ================================================================== */

export interface TranslateInput {
  text: string;
  /** 省略时后端自动判断方向；UI 按 sender 显式传：好友→zh、自己→好友语言 */
  target?: string;
  source?: "auto";
  friend_id?: string;
}

export function translateText(input: TranslateInput): Promise<TranslationResult> {
  return request("/translate", parseTranslationResponse, {
    method: "POST",
    body: JSON.stringify({ source: "auto", ...input }),
    timeoutMs: 45000,
  });
}

export function translateBatch(input: {
  texts: string[];
  target?: string;
  friend_id?: string;
}): Promise<BatchTranslationResponse> {
  return request("/translate/batch", parseBatchTranslationResponse, {
    method: "POST",
    body: JSON.stringify(input),
    timeoutMs: 90000,
  });
}


/* ================================================================== */
/* 通话流式接口：warmup filler / NDJSON turn                             */
/* ================================================================== */

export interface CallWarmupResult {
  ok: boolean;
}

export function callWarmup(friendId: string): Promise<CallWarmupResult> {
  return request(
    `/chat/${encodeURIComponent(friendId)}/call/warmup`,
    (raw) => {
      const r = isRecord(raw) ? raw : {};
      return { ok: r.ok === true };
    },
    { timeoutMs: 15000 },
  );
}

export type CallTurnEvent =
  | { type: "transcript"; text: string; stt?: unknown }
  | { type: "sentence"; index: number; text: string; audio_url: string }
  | {
      type: "done";
      user_message: Message | null;
      friend_message: Message | null;
      closeness_level: number;
      total_turns: number;
      reply_language: string;
    }
  | { type: "error"; message: string };

export interface StreamCallTurnOptions {
  signal?: AbortSignal;
  onEvent: (event: CallTurnEvent) => void;
}

function parseCallTurnEvent(line: string): CallTurnEvent | null {
  let raw: unknown;
  try {
    raw = JSON.parse(line);
  } catch {
    return null;
  }
  if (!isRecord(raw)) return null;
  const type = str(raw.type);
  if (type === "transcript") return { type: "transcript", text: str(raw.text), stt: raw.stt };
  if (type === "sentence") {
    return {
      type: "sentence",
      index: num(raw.index),
      text: str(raw.text),
      audio_url: str(raw.audio_url),
    };
  }
  if (type === "done") {
    return {
      type: "done",
      user_message: parseMessage(raw.user_message),
      friend_message: parseMessage(raw.friend_message),
      closeness_level: num(raw.closeness_level),
      total_turns: num(raw.total_turns),
      reply_language: str(raw.reply_language),
    };
  }
  if (type === "error") return { type: "error", message: str(raw.message) || "通话流式响应失败" };
  return null;
}

/**
 * 通话轮次流式接口：multipart 上传音频，逐行解析 NDJSON 事件。
 * 失败（网络/非 2xx/流异常）抛 ApiError，由调用方回退普通 /voice 接口。
 */
export async function streamCallTurn(
  friendId: string,
  blob: Blob,
  durationMs: number,
  filename: string,
  options: StreamCallTurnOptions,
): Promise<void> {
  if (typeof ReadableStream === "undefined" || typeof TextDecoder === "undefined") {
    throw new ApiError("当前浏览器不支持流式读取", 0);
  }
  const form = new FormData();
  form.append("audio", blob, filename);
  form.append("duration_ms", String(Math.max(0, Math.round(durationMs))));

  let res: Response;
  try {
    res = await fetch(`${BASE}/chat/${encodeURIComponent(friendId)}/call/turn`, {
      method: "POST",
      body: form,
      signal: options.signal,
      headers: { Accept: "application/x-ndjson" },
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError("无法连接通话流式接口", 0);
  }
  if (!res.ok) throw new ApiError(await detailOf(res), res.status);
  if (!res.body || typeof res.body.getReader !== "function") {
    throw new ApiError("通话流式响应不可读", 0);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const emitLine = (line: string) => {
    const event = parseCallTurnEvent(line);
    if (event) options.onEvent(event);
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newlineIndex = buffer.indexOf("\n");
      while (newlineIndex >= 0) {
        const line = buffer.slice(0, newlineIndex).trim();
        buffer = buffer.slice(newlineIndex + 1);
        if (line) emitLine(line);
        newlineIndex = buffer.indexOf("\n");
      }
    }
    buffer += decoder.decode();
    const rest = buffer.trim();
    if (rest) {
      for (const line of rest.split("\n")) {
        if (line.trim()) emitLine(line.trim());
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* ignore */
    }
  }
}
