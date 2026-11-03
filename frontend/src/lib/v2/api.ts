"use client";

/**
 * `/api/v2` 客户端。
 *
 * 静态导出：所有请求必须走**相对路径**，构建期不读任何 host / env。
 * 每个调用都带超时；失败抛 `ApiError`，由 UI 转成中文提示 + 空态。
 */

import {
  parseBootstrap,
  parseChatTurn,
  parseExpressions,
  parseFriend,
  parseFriendsPayload,
  parseMemoryPayload,
  parseMessages,
  parseMoments,
  type BootstrapPayload,
  type ChatTurnResponse,
  type Expression,
  type FactSheet,
  type Friend,
  type FriendsPayload,
  type MemoryPayload,
  type Message,
  type Moment,
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

async function request<T>(
  path: string,
  parse: (raw: unknown) => T,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body = null, timeoutMs = DEFAULT_TIMEOUT, headers } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
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
