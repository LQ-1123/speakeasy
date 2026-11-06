/**
 * SpeakEasy V3 · 客户端 API + 设备身份。
 *
 * 身份模型
 * --------
 * - 首次启动生成一个随机 `device_key`，调 `/api/v3/auth/register` 换 (user_id, token)；
 * - 之后所有 `/api/**` 请求自动带 `X-User-Id` / `X-User-Token`；
 * - 注册失败（离线/老服务端）时退化为「无身份」= 服务端按 `local` 用户处理，
 *   保证页面仍可打开（与 V2 行为一致）。
 *
 * `installApiIdentity()` 会包装 `window.fetch`，因此**已有的 v2 请求**
 * （好友/聊天/动态）也会自动带上身份头，不需要逐个改动。
 */

import { API_ORIGIN } from "../api";

const ID_STORE = "speakeasy.identity.v1";
const DEVICE_STORE = "speakeasy.device_key.v1";

export interface Identity {
  user_id: string;
  token: string;
  display_name?: string;
}

export interface PersonaBrief {
  id: string;
  nickname: string;
  avatar: string;
  bio: string;
  interests: string[];
  country: string;
  city: string;
  occupation: string;
  age: number;
  gender: string;
  language: string;
  is_ai: boolean;
  ai_label: string;
}

export interface FeedComment {
  id: string;
  content: string;
  created_at: string;
  author: {
    type?: string;
    nickname: string;
    persona_id?: string;
    is_ai?: boolean;
    ai_label?: string;
  };
}

export interface FeedLikedBy {
  persona_id: string;
  nickname: string;
}

export interface FeedItem {
  kind: "persona_post" | "user_post";
  id: string;
  content: string;
  media: string[];
  tags: string[];
  visibility: "public" | "friends" | "private";
  created_at: string;
  like_count: number;
  comment_count: number;
  liked_by_me: boolean;
  is_mine: boolean;
  friendship_status?: "none" | "pending" | "accepted";
  author: PersonaBrief & { is_ai: boolean; ai_label: string };
  comments: FeedComment[];
  liked_by?: FeedLikedBy[];
}

export interface Friendship {
  id: string;
  persona_id: string;
  status: "pending" | "accepted" | "rejected";
  friend_id: string | null;
  request_message: string;
  created_at: string;
  updated_at: string;
  persona?: { id: string; nickname: string; avatar: string; is_ai: boolean } | null;
}

export interface PersonaProfile extends PersonaBrief {
  personality: string;
  timezone: string;
  post_count: number;
  posts: { items: FeedItem[]; next_cursor: string };
  friendship: Friendship | null;
}

export interface SocialEvent {
  id: string;
  type: "like" | "comment" | "friend_request";
  status: string;
  is_read: boolean;
  created_at: string;
  scheduled_at: string;
  post_id: string;
  payload: {
    comment?: string;
    message?: string;
    reaction?: string;
    persona_name?: string;
    post_excerpt?: string;
  };
  persona: { id: string; nickname: string; avatar: string; bio: string; is_ai: boolean; ai_label: string };
}

export interface FeedSummary {
  persona_posts: number;
  my_posts: number;
  friends: number;
  unread_events: number;
}

/* --------------------------------------------------------------- 身份 */

/**
 * 原生 fetch 的引用：**必须在 installApiIdentity 之前捕获**，
 * 否则 ensureIdentity 里的注册请求会再次触发被包装的 fetch → 自等待死锁。
 */
const nativeFetch: typeof fetch | null =
  typeof window !== "undefined" ? window.fetch.bind(window) : null;

let identity: Identity | null = null;
let ready: Promise<Identity | null> | null = null;
let patched = false;

function readLocal<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeLocal(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* 隐私模式等场景忽略 */
  }
}

function deviceKey(): string {
  const existing = readLocal<string>(DEVICE_STORE);
  if (typeof existing === "string" && existing.length >= 8) return existing;
  const bytes = new Uint8Array(16);
  if (typeof crypto !== "undefined" && crypto.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  const key = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  writeLocal(DEVICE_STORE, key);
  return key;
}

/** 注册/取回设备身份（幂等；同一 device_key 永远同一 user_id）。 */
export function ensureIdentity(): Promise<Identity | null> {
  if (typeof window === "undefined") return Promise.resolve(null);
  if (ready) return ready;
  const stored = readLocal<Identity>(ID_STORE);
  ready = (async () => {
    try {
      const doFetch = nativeFetch ?? window.fetch.bind(window);
      const res = await doFetch(`${API_ORIGIN}/api/v3/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_key: deviceKey(), display_name: stored?.display_name ?? "" }),
      });
      if (!res.ok) throw new Error(`register ${res.status}`);
      const data = (await res.json()) as Identity;
      identity = { user_id: data.user_id, token: data.token, display_name: data.display_name };
      writeLocal(ID_STORE, identity);
      return identity;
    } catch {
      identity = stored;
      return stored;
    }
  })();
  return ready;
}

export function currentIdentity(): Identity | null {
  return identity;
}

/**
 * 丢弃本地身份并允许重新注册。
 * 触发场景：服务端数据库被重置 / token 轮换 / 身份被清理 —— 此时继续带旧头会一直 401。
 */
export function resetIdentity(): void {
  identity = null;
  ready = null;
  try {
    window.localStorage.removeItem(ID_STORE);
  } catch {
    /* ignore */
  }
}

function isOurApi(url: string): boolean {
  if (url.startsWith("/api/")) return true;
  return Boolean(API_ORIGIN) && url.startsWith(`${API_ORIGIN}/api/`);
}

/** 包装 window.fetch：给所有 /api 请求补身份头。幂等，可在每次挂载时调用。 */
export function installApiIdentity(): void {
  if (patched || typeof window === "undefined") return;
  patched = true;
  const original = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (!isOurApi(url)) return original(input, init);
    let id: Identity | null = null;
    try {
      id = await ensureIdentity();
    } catch {
      id = readLocal<Identity>(ID_STORE);
    }
    if (!id) return original(input, init);

    const send = (who: Identity) => {
      const headers = new Headers(
        init?.headers ?? (input instanceof Request ? input.headers : undefined),
      );
      headers.set("X-User-Id", who.user_id);
      headers.set("X-User-Token", who.token);
      return original(input, { ...(init ?? {}), headers });
    };

    const res = await send(id);
    if (res.status !== 401) return res;
    /* 身份失效（服务端重置/轮换）：重注册后重试一次，避免整页一直报错 */
    resetIdentity();
    const next = await ensureIdentity();
    return next ? send(next) : res;
  };
}

/* --------------------------------------------------------------- 请求 */

async function request<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  const res = await fetch(`${API_ORIGIN}${path}`, {
    cache: "no-store",
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (res.status === 401 && !retried) {
    resetIdentity();
    await ensureIdentity();
    return request<T>(path, init, true);
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (data?.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export interface DiscoverNext {
  persona: PersonaBrief;
  greeting: string;
  post_id: string;
  friendship_status: "none" | "pending" | "accepted";
  remaining: number;
  source: string;
}

/* --------------------------------------------------------------- 接口 */

export function fetchFeed(cursor?: string, limit = 12): Promise<{ items: FeedItem[]; next_cursor: string }> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set("cursor", cursor);
  return request(`/api/v3/feed?${query.toString()}`);
}

export function fetchFeedSummary(): Promise<FeedSummary> {
  return request("/api/v3/feed/summary");
}

export function fetchPersona(personaId: string): Promise<PersonaProfile> {
  return request(`/api/v3/personas/${encodeURIComponent(personaId)}`);
}

/** 偶遇：从已生成的世界里挑一个人（不重新捏人）。 */
export function fetchDiscoverNext(language?: string): Promise<DiscoverNext> {
  const q = language ? `?language=${encodeURIComponent(language)}` : "";
  return request(`/api/v3/discover/next${q}`);
}

/** 用人物自己的声音朗读一条动态（公共音频，可直接给 <audio> / WebAudio 用）。 */
export function personaSpeechUrl(postId: string): string {
  return `${API_ORIGIN}/api/v3/persona-posts/${encodeURIComponent(postId)}/speech`;
}

/** 信息流内容翻译（复用 V2 的翻译链路：LLM 优先，带缓存）。 */
export async function translateText(text: string, target = "zh"): Promise<string> {
  const data = await request<{ translation?: string; ok?: boolean }>("/api/v2/translate", {
    method: "POST",
    body: JSON.stringify({ text, target }),
  });
  return data.translation ?? "";
}

export function fetchPersonas(limit = 24): Promise<{ items: PersonaBrief[] }> {
  return request(`/api/v3/personas?limit=${limit}`);
}

export function addFriend(personaId: string, message = ""): Promise<{ friend_id: string; created: boolean; friendship: Friendship }> {
  return request(`/api/v3/personas/${encodeURIComponent(personaId)}/friend`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

export function removeFriend(personaId: string): Promise<{ removed: boolean }> {
  return request(`/api/v3/personas/${encodeURIComponent(personaId)}/friend`, { method: "DELETE" });
}

export function createPost(body: {
  content: string;
  visibility: "public" | "friends" | "private";
  media?: string[];
  tags?: string[];
}): Promise<{ post: FeedItem; social: { scheduled: boolean; instant: boolean; events: SocialEvent[] } }> {
  return request("/api/v3/posts", { method: "POST", body: JSON.stringify(body) });
}

export function deletePost(postId: string): Promise<{ deleted: boolean }> {
  return request(`/api/v3/posts/${encodeURIComponent(postId)}`, { method: "DELETE" });
}

export function likePersonaPost(postId: string): Promise<{ liked: boolean; like_count: number }> {
  return request(`/api/v3/persona-posts/${encodeURIComponent(postId)}/like`, { method: "POST" });
}

export function commentPersonaPost(postId: string, content: string): Promise<FeedComment> {
  return request(`/api/v3/persona-posts/${encodeURIComponent(postId)}/comments`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}

export function fetchEvents(unreadOnly = false): Promise<{ items: SocialEvent[]; unread: number }> {
  return request(`/api/v3/events?unread_only=${unreadOnly ? "true" : "false"}`);
}

export function markEventRead(eventId: string): Promise<{ read: boolean }> {
  return request(`/api/v3/events/${encodeURIComponent(eventId)}/read`, { method: "POST", body: "{}" });
}

export function markAllEventsRead(): Promise<{ updated: number }> {
  return request("/api/v3/events/read-all", { method: "POST" });
}

export function acceptEvent(eventId: string): Promise<{ status: string; friend_id: string }> {
  return request(`/api/v3/events/${encodeURIComponent(eventId)}/accept`, { method: "POST" });
}

export function declineEvent(eventId: string): Promise<{ status: string; friend_id: string }> {
  return request(`/api/v3/events/${encodeURIComponent(eventId)}/decline`, { method: "POST" });
}

/* --------------------------------------------------------------- 展示工具 */

export function relTimeOf(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diff = Date.now() - d.getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export const VISIBILITY_LABEL: Record<string, string> = {
  public: "公开",
  friends: "好友可见",
  private: "仅自己可见",
};
