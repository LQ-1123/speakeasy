/**
 * W4 的真实后端适配层。
 *
 * 设计原则：**组件一行不改**。这里只负责把后端的 v2 契约映射成
 * `model.ts` 里那套字段（Claude Code 当初就是照着能直接换数据来设计的）。
 *
 * 任何一步失败都返回 null，让 store 继续用 mock —— 原型永远能打开。
 */

import type { Expression, Friend, Message, Moment, Profile } from "./model";
import { avatarRegionOf, diceAvatar } from "./avatar";

/* ------------------------------------------------------------ 常量与工具 */

/** 老数据 / mock 里没 gender 时的兜底：按名字判断 */
export const NAME_GENDER: Record<string, "male" | "female"> = {
  adrian: "male", callum: "male", finn: "male", haruto: "male", morten: "male",
  tom: "male", sergio: "male", cian: "male", rohan: "male", mitch: "male",
  marcus: "male", zihao: "male", matteo: "male", moritz: "male",
  annika: "female", camila: "female", giulia: "female", ines: "female",
  jiwoo: "female", lin: "female", mei: "female", niamh: "female", rosa: "female",
  rocio: "female", alba: "female", chiara: "female", lena: "female",
  ananya: "female", tahlia: "female", freya: "female", chloe: "female",
  nora: "female",
};

/**
 * 头像：DiceBear personas 现场生成（详见 avatar.ts）。
 *
 * 每个人固定拿「自己 id 对应的那张插画脸」：不需要照片池，
 * 也不会出现「两个人共用一张脸」或「性别不对」的问题。
 */
export function assignAvatars(list: Friend[]): void {
  for (const f of list) {
    const slug = (f.name || "").toLowerCase().replace(/[^a-z]/g, "");
    const gender =
      (f.gender === "male" || f.gender === "female")
        ? (f.gender as "male" | "female")
        : NAME_GENDER[slug] ?? "";
    f.avatar = diceAvatar(f.id || f.name, gender, avatarRegionOf(f.country || ""));
  }
}

const LANG_LABEL: Record<string, string> = {
  en: "英语", ja: "日语", ko: "韩语", fr: "法语",
  de: "德语", es: "西班牙语", it: "意大利语", pt: "葡萄牙语",
};

const COUNTRY_FLAG: Array<[string, string]> = [
  ["United Kingdom", "🇬🇧"], ["England", "🇬🇧"], ["Ireland", "🇮🇪"],
  ["United States", "🇺🇸"], ["Australia", "🇦🇺"], ["Canada", "🇨🇦"],
  ["Italy", "🇮🇹"], ["Japan", "🇯🇵"], ["Korea", "🇰🇷"], ["China", "🇨🇳"],
  ["Germany", "🇩🇪"], ["France", "🇫🇷"], ["Spain", "🇪🇸"], ["Portugal", "🇵🇹"],
  ["Brazil", "🇧🇷"], ["Netherlands", "🇳🇱"], ["Norway", "🇳🇴"], ["Sweden", "🇸🇪"],
  ["India", "🇮🇳"], ["Singapore", "🇸🇬"],
];

function flagOf(country: string): string {
  const hit = COUNTRY_FLAG.find(([k]) => country && country.includes(k));
  return hit ? hit[1] : "🌍";
}

function relTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) {
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  const yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return "昨天";
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString("zh-CN", {
    ...(sameYear ? {} : { year: "numeric" }),
    month: "numeric",
    day: "numeric",
  });
}

function clockOf(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/* ------------------------------------------------------------ 网络请求 */

async function getJson<T>(url: string, init?: RequestInit): Promise<T | null> {
  try {
    const r = await fetch(url, { cache: "no-store", ...init });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

/**
 * 后端地址：
 * - 同源部署（后端顺带发前端静态文件）：留空即可
 * - 前后端分离：构建时设置 NEXT_PUBLIC_API_BASE=https://你的域名
 */
export const API_ORIGIN = (process.env.NEXT_PUBLIC_API_BASE ?? "").replace(/\/+$/, "");
const BASE = `${API_ORIGIN}/api/v2`;

/** 把后端返回的相对媒体地址（/api/v2/audio/xxx）补成绝对地址 */
export function mediaUrl(path?: string | null): string | undefined {
  if (!path) return undefined;
  const p = String(path).trim();
  if (!p) return undefined;
  if (/^(https?:|data:|blob:)/i.test(p)) return p;
  return `${API_ORIGIN}${p.startsWith("/") ? p : `/${p}`}`;
}

/* ------------------------------------------------------------ 字段映射 */

type RawFriend = Record<string, unknown>;
type RawMessage = Record<string, unknown>;

function mapFriend(raw: RawFriend): Friend {
  const id = String(raw.id ?? raw.friend_id ?? "");
  const name = String(raw.name ?? "?");
  const country = String(raw.nationality ?? "");
  const lang = String(raw.reply_language ?? "en");
  const turns = Number(raw.total_turns ?? 0);
  const lastAt = (raw.last_interaction_at as string) ?? null;
  const localTime = String(raw.local_time ?? "");
  return {
    id,
    name,
    gender: String(raw.gender ?? ""),
    avatar: diceAvatar(
      id || name,
      raw.gender === "male" || raw.gender === "female"
        ? (String(raw.gender) as "male" | "female")
        : NAME_GENDER[(name || "").toLowerCase().replace(/[^a-z]/g, "")] ?? "",
      avatarRegionOf(String(raw.nationality || raw.country || "")),
    ),
    city: String(raw.city ?? ""),
    country,
    flag: flagOf(country),
    language: lang,
    languageLabel: LANG_LABEL[lang] ?? lang,
    timezone: String(raw.timezone ?? ""),
    localTime: localTime || clockOf(lastAt),
    localPeriod: String(raw.local_period ?? ""),
    localActivity: String(raw.local_activity ?? ""),
    occupation: String(raw.occupation ?? ""),
    age: Number(raw.age ?? 0),
    bio: String(raw.bio ?? ""),
    quirks: Array.isArray(raw.speech_quirks) ? (raw.speech_quirks as string[]).slice(0, 4) : [],
    closeness: (Math.min(5, Math.max(1, Number(raw.closeness_level ?? 1))) as Friend["closeness"]),
    turns,
    sharedEpisodes: Array.isArray(raw.shared_episodes)
      ? (raw.shared_episodes as string[]).filter((s) => typeof s === "string").slice(0, 3)
      : [],
    openLoops: Array.isArray(raw.open_loops)
      ? (raw.open_loops as unknown[])
          .map((o) => (typeof o === "string" ? o : String((o as { text?: string })?.text ?? "")))
          .filter(Boolean)
          .slice(0, 2)
      : [],
    unread: Number(raw.unread ?? 0),
    replying: Boolean(raw.replying),
    online: Boolean(raw.is_awake),
    lastActive: relTime(lastAt),
    voice: String(raw.voice_id ?? ""),
    voiceLabel: String(raw.voice_id ?? "").split("-").pop()?.replace("Neural", "") ?? "默认",
    /* 好友申请会带一句 request_message（偶遇捏人时写入的招呼语） */
    greeting: String(raw.request_message ?? ""),
    greetingZh: "",
    greetingAudio: mediaUrl(raw.greeting_audio ? String(raw.greeting_audio) : undefined),
  };
}

function mapMessage(raw: RawMessage, fallbackFriend: string): Message | null {
  const sender = String(raw.sender ?? "");
  const kindRaw = String(raw.msg_type ?? "text");
  const kind: Message["kind"] =
    kindRaw === "voice" ? "voice"
    : kindRaw === "image" ? "image"
    : kindRaw === "call" ? "call"
    : kindRaw === "system" ? "system"
    : "text";
  const id = String(raw.id ?? "");
  if (!id) return null;
  const mapped: Message = {
    id,
    friendId: String(raw.friend_id ?? fallbackFriend),
    from: sender === "user" ? "me" : "friend",
    kind,
    text: String(raw.transcript ?? ""),
    time: String(raw.created_at ?? new Date().toISOString()),
    read: Boolean(raw.is_read),
  };
  if (typeof raw.duration_ms === "number") mapped.durationMs = raw.duration_ms;
  if (raw.audio_url) mapped.audio = mediaUrl(String(raw.audio_url));
  if (raw.image_url) mapped.image = String(raw.image_url);
  if (raw.suggestion) {
    const rawSug = raw.suggestion as unknown;
    if (typeof rawSug === "string") {
      mapped.suggestion = { better: rawSug, why: "", tag: "地道", saved: false };
    } else if (rawSug && typeof rawSug === "object") {
      const s = rawSug as Record<string, any>;
      mapped.suggestion = {
        better: String(s.better_version ?? s.better ?? s.text ?? ""),
        why: String(s.explanation ?? s.why ?? ""),
        tag: ((): "地道" | "自然" | "礼貌" | "语法" => {
          const map: Record<string, "地道" | "自然" | "礼貌" | "语法"> = {
            grammar: "语法",
            natural: "自然",
            polite: "礼貌",
            idiomatic: "地道",
          };
          return map[String(s.tag ?? "")] ?? "地道";
        })(),
        saved: Boolean(s.is_saved ?? s.saved),
      };
    }
  }
  return mapped;
}

/* ------------------------------------------------------------ 对外接口 */

export interface LiveSnapshot {
  friends: Friend[];
  messages: Message[];
  /** 待处理的好友申请（bootstrap.requests） */
  requests: Friend[];
  /** 只覆盖后端确实提供的字段 */
  profile: Partial<Profile>;
}

/** 拉一次全量：好友 + 每位好友最近消息 + 我的资料 */
export async function loadAll(): Promise<LiveSnapshot | null> {
  const boot = await getJson<Record<string, unknown>>(`${BASE}/bootstrap`);
  /* 只有“请求失败”才算掉线；好友为空是合法状态（新手引导/清空后），
     否则前端会误判成离线并回退到 mock 名单 —— 删都删不掉。 */
  if (!boot) return null;
  const rawFriends = (boot.friends as RawFriend[]) ?? [];

  const friends = rawFriends.map(mapFriend);
  assignAvatars(friends); // 头像整批分配（两阶段，避免抢照片）

  // 每位好友取最近 20 条；并发但限制在 4 个一组，避免瞬间打满
  const messages: Message[] = [];
  for (let i = 0; i < friends.length; i += 4) {
    const batch = friends.slice(i, i + 4);
    const results = await Promise.all(
      batch.map(async (f) => {
        const r = await getJson<{ messages?: RawMessage[] }>(
          `${BASE}/friends/${f.id}/messages?limit=20`,
        );
        return (r?.messages ?? [])
          .map((m) => mapMessage(m, f.id))
          .filter((m): m is Message => m !== null);
      }),
    );
    results.forEach((list) => messages.push(...list));
  }
  messages.sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime());

  const rawProfile = (boot.profile as Record<string, unknown>) ?? {};
  const fact = (rawProfile.fact_sheet ?? {}) as Record<string, any>;
  const factProfile = (fact.profile ?? {}) as Record<string, any>;
  const profile: Partial<Profile> = {
    name: String(rawProfile.nickname ?? "我"),
    city: String(factProfile.city ?? ""),
    job: String(factProfile.major_or_job ?? ""),
    targets: Array.isArray(rawProfile.target_languages)
      ? (rawProfile.target_languages as string[])
      : ["en"],
    interests: Array.isArray(fact.interests) ? (fact.interests as string[]) : undefined,
    birthday: String(rawProfile.birthday ?? ""),
  } as Partial<Profile>;

  const requests = ((boot.requests as RawFriend[]) ?? []).map((r) => ({
    ...mapFriend(r),
    isRequest: true,
    requestNote: String((r as Record<string, unknown>).request_message ?? ""),
  }));

  return { friends, messages, profile, requests };
}

/** 发一条文字；返回该好友的最新消息（用于替换本地状态） */
export async function sendText(
  friendId: string,
  text: string,
): Promise<Message[] | null> {
  const ok = await getJson(`${BASE}/chat/${friendId}/text`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, context: null }),
  });
  if (!ok) return null;
  const r = await getJson<{ messages?: RawMessage[] }>(
    `${BASE}/friends/${friendId}/messages?limit=20`,
  );
  return (r?.messages ?? [])
    .map((m) => mapMessage(m, friendId))
    .filter((m): m is Message => m !== null);
}

/** 拉某位好友的最新消息（轮询用） */
export async function fetchMessages(friendId: string): Promise<Message[] | null> {
  const r = await getJson<{ messages?: RawMessage[] }>(
    `${BASE}/friends/${friendId}/messages?limit=30`,
  );
  if (!r) return null;
  return (r.messages ?? [])
    .map((m) => mapMessage(m, friendId))
    .filter((m): m is Message => m !== null);
}

/** 翻译一条 */
export async function translate(
  text: string,
  friendId: string,
): Promise<string | null> {
  const r = await getJson<Record<string, unknown>>(`${BASE}/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, friend_id: friendId }),
  });
  if (!r) return null;
  const out = r.translation ?? r.text ?? r.result;
  return typeof out === "string" && out.trim() ? out : null;
}

/** 捏一个新朋友 */
export type DiscoverResult = {
  friend: Friend;
  /** 后端现捏的打招呼语音（icebreaker） */
  greeting: string;
  greetingAudio?: string;
};

export async function discover(language?: string): Promise<DiscoverResult | null> {
  const r = await getJson<{ friend?: RawFriend; message?: RawMessage }>(
    `${BASE}/friends/discover`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(language ? { language } : {}),
    },
  );
  if (!r?.friend) return null;
  const friend = mapFriend(r.friend);
  const msg = r.message ? mapMessage(r.message, friend.id) : null;
  return {
    friend,
    greeting: msg?.text ?? "",
    greetingAudio: msg?.audio,
  };
}

/** 保存「我在练」 */
export async function saveProfile(
  nickname: string,
  targets: string[],
  birthday?: string,
): Promise<boolean> {
  const body: Record<string, unknown> = { nickname, target_languages: targets };
  if (birthday !== undefined) body.birthday = birthday;
  const r = await getJson(`${BASE}/memories`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r !== null;
}

/* ========================================================================== */
/* 设置                                                                        */
/* ========================================================================== */

export type LiveSettings = {
  llm: Record<string, any>;
  stt: Record<string, any>;
  tts: Record<string, any>;
};

/** 读真实设置（LLM / 语音识别 / 语音合成） */
export async function loadSettings(): Promise<LiveSettings | null> {
  const r = await getJson<LiveSettings>(`${BASE}/settings`);
  if (!r || !r.llm) return null;
  return r;
}

/** 保存 TTS（只传有变化的字段，后端按部分更新处理） */
export async function saveTts(body: Record<string, string | undefined>) {
  return getJson(`${BASE}/settings/tts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function resetTts() {
  return getJson(`${BASE}/settings/tts/reset`, { method: "POST" });
}

export async function testTts() {
  return getJson(`${BASE}/settings/tts/test`, { method: "POST" });
}

export async function saveStt(body: Record<string, string | undefined>) {
  return getJson(`${BASE}/settings/stt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function testStt() {
  return getJson(`${BASE}/settings/stt/test`, { method: "POST" });
}

/* ========================================================================== */
/* 朋友圈                                                                      */
/* ========================================================================== */

function mapMoment(raw: Record<string, any>): Moment {
  /* 我的动态：后端 author_type="user"、friend_id 为空 —— 必须映射成 "me"，
     否则会被「AI 互评过滤」当成好友动态，把 AI 评论全清掉 */
  const isMine = raw.author_type === "user" || (!raw.friend_id && raw.author_name === "我");
  const comments = (raw.comments ?? []).map((c: Record<string, any>) => ({
    id: String(c.id ?? ""),
    friendId: String(c.friend_id ?? ""),
    text: String(c.content ?? c.text ?? ""),
    time: String(c.created_at ?? ""),
  }));
  const imageUrl = raw.image_url ? String(raw.image_url) : "";
  return {
    id: String(raw.id ?? ""),
    authorId: isMine ? "me" : String(raw.friend_id ?? ""),
    images: imageUrl ? [imageUrl] : [],
    text: String(raw.content ?? ""),
    translation: raw.content_zh ? String(raw.content_zh) : undefined,
    mood: String(raw.mood_tag ?? ""),
    time: relTime(String(raw.created_at ?? "")) || String(raw.created_at ?? ""),
    comments,
    likedBy: [],
  };
}

/** 今天的朋友圈（为空时调用 generateMoments 生成一批） */
export async function loadMoments(): Promise<Moment[] | null> {
  const r = await getJson<{ moments?: Record<string, any>[] }>(`${BASE}/moments`);
  if (!r) return null;
  return (r.moments ?? []).map(mapMoment);
}

/** 让 AI 好友按今天的日期生成一批动态（同一天幂等） */
export async function generateMoments(): Promise<Moment[] | null> {
  const r = await getJson<{ moments?: Record<string, any>[] }>(`${BASE}/moments/generate`, {
    method: "POST",
  });
  if (!r) return null;
  return (r.moments ?? []).map(mapMoment);
}

/** 我发一条动态；AI 评论由后端后台生成，稍后重拉列表 */
export async function publishMomentToServer(text: string, mood: string) {
  return getJson(`${BASE}/moments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: text, mood_tag: mood }),
  });
}

/** 让好友对这条动态评论（AI 生成一条评论） */
export async function commentMomentOnServer(momentId: string, text: string) {
  return getJson(`${BASE}/moments/${momentId}/comment`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

/* ========================================================================== */
/* 表达本                                                                      */
/* ========================================================================== */

function mapExpression(raw: Record<string, any>): Expression {
  const mastery = Number(raw.mastery_count ?? 1);
  return {
    id: String(raw.id ?? ""),
    original: String(raw.original_text ?? ""),
    better: String(raw.better_expression ?? ""),
    why: String(raw.explanation ?? ""),
    sourceFriendId: String(raw.source_friend_id ?? ""),
    createdAt: String(raw.created_at ?? ""),
    status: raw.status === "mastered" ? "mastered" : "learning",
    mastery: Math.max(1, Math.min(3, mastery)),
    due: raw.status === "mastered" ? 0 : 60,
    lastPracticed: raw.last_practiced_at ? String(raw.last_practiced_at) : undefined,
  };
}

export async function loadExpressions(): Promise<Expression[] | null> {
  const r = await getJson<{ expressions?: Record<string, any>[] }>(`${BASE}/expressions`);
  if (!r) return null;
  return (r.expressions ?? []).map(mapExpression);
}

export async function addExpression(
  better: string,
  original = "",
  explanation = "",
): Promise<Expression | null> {
  const r = await getJson<{ expression?: Record<string, any> }>(`${BASE}/expressions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      original_text: original,
      better_expression: better,
      explanation,
    }),
  });
  return r?.expression ? mapExpression(r.expression) : null;
}

export async function patchExpression(id: string, fields: Record<string, unknown>) {
  return getJson(`${BASE}/expressions/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
}

export async function deleteExpressionOnServer(id: string) {
  return getJson(`${BASE}/expressions/${id}`, { method: "DELETE" });
}

/* ========================================================================== */
/* 好友状态（申请的接受/拒绝、删除）                                            */
/* ========================================================================== */

export async function acceptFriend(id: string) {
  return getJson(`${BASE}/friends/${id}/accept`, { method: "POST" });
}

export async function declineFriend(id: string) {
  return getJson(`${BASE}/friends/${id}/decline`, { method: "POST" });
}

export async function removeFriend(id: string) {
  return getJson(`${BASE}/friends/${id}`, { method: "DELETE" });
}

/**
 * 发一条真实语音。
 *
 * 后端做三件事：语音识别 → LLM 回复 → TTS 合成，返回该好友的最新消息列表。
 * 这条链路通常要 5–20 秒，调用方要显示「正在听 / 正在想」。
 */
export async function sendVoiceBlob(
  friendId: string,
  blob: Blob,
  durationMs: number,
): Promise<Message[] | null> {
  const form = new FormData();
  form.append("audio", blob, blob.type.includes("ogg") ? "voice.ogg" : "voice.webm");
  form.append("duration_ms", String(Math.round(durationMs)));

  const resp = await fetch(`${BASE}/chat/${friendId}/voice`, { method: "POST", body: form });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => "");
    console.warn("[voice] 发送失败", resp.status, detail.slice(0, 200));
    return null;
  }
  return fetchMessages(friendId);
}

/* ========================================================================== */
/* 对话模型设置                                                                */
/* ========================================================================== */

/** 保存对话模型（后端会重建客户端，下一条消息即生效） */
export async function saveLlm(body: {
  base_url?: string;
  model?: string;
  api_key?: string;
  temperature?: number;
  max_tokens?: number;
  verify?: boolean;
}) {
  return getJson(`${BASE}/settings/llm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verify: true, ...body }),
  });
}

/* ========================================================================== */
/* 好友口音                                                                    */
/* ========================================================================== */

export type ArchetypeLite = {
  archetype_id: string;
  display_name: string;
  accent_label: string;
  voice_id: string;
  native_lang: string;
  country: string;
};

let ARCHETYPES_CACHE: ArchetypeLite[] | null = null;

/** 可选口音列表（按原生语言过滤用） */
export async function loadArchetypes(): Promise<ArchetypeLite[]> {
  if (ARCHETYPES_CACHE) return ARCHETYPES_CACHE;
  const r = await getJson<{ archetypes?: Record<string, any>[] }>(`${BASE}/archetypes`);
  if (!r) return [];
  ARCHETYPES_CACHE = (r.archetypes ?? []).map((a) => ({
    archetype_id: String(a.archetype_id ?? ""),
    display_name: String(a.display_name ?? ""),
    accent_label: String(a.accent_label ?? ""),
    voice_id: String(a.voice_id ?? ""),
    native_lang: String((a.locale_info ?? {}).native_lang ?? ""),
    country: String((a.locale_info ?? {}).country ?? ""),
  }));
  return ARCHETYPES_CACHE;
}

/** 给好友换口音：后端按 archetype 的 voice_id + prosody 一起改 */
export async function changeFriendVoice(friendId: string, archetypeId: string) {
  return getJson(`${BASE}/friends/${friendId}/voice`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ archetype_id: archetypeId }),
  });
}

/* ========================================================================== */
/* 打电话                                                                      */
/* ========================================================================== */

/** 接通前的预热：后端加载本地 STT + 预热 LLM，减少第一句的等待 */
export async function callWarmup(friendId: string) {
  return getJson(`${BASE}/chat/${friendId}/call/warmup`);
}

/** 挂断后把通话摘要落进聊天记录（时长 + 轮数） */
export async function saveCallSummary(friendId: string, durationS: number, turns: number) {
  return getJson(`${BASE}/chat/${friendId}/call/summary`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ duration_s: Math.round(durationS), turns }),
  });
}

export type CallTurnEvent =
  | { type: "transcript"; text: string }
  | { type: "sentence"; index: number; text: string; audioUrl: string }
  | { type: "done"; totalTurns?: number }
  | { type: "error"; message: string };

/**
 * 通话一个回合：上传一句录音，后端按 NDJSON 流式回
 * 转写 → 逐句文本 + 音频 URL → done。
 */
export async function callTurnStream(
  friendId: string,
  blob: Blob,
  durationMs: number,
  onEvent: (e: CallTurnEvent) => void,
): Promise<boolean> {
  const form = new FormData();
  form.append("audio", blob, blob.type.includes("ogg") ? "call.ogg" : "call.webm");
  form.append("duration_ms", String(Math.round(durationMs)));

  const resp = await fetch(`${BASE}/chat/${friendId}/call/turn`, { method: "POST", body: form });
  if (!resp.ok || !resp.body) {
    console.warn("[call] turn 失败", resp.status);
    return false;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl = buffer.indexOf("\n");
    while (nl >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      nl = buffer.indexOf("\n");
      if (!line) continue;
      try {
        const raw = JSON.parse(line) as Record<string, any>;
        if (raw.type === "transcript") onEvent({ type: "transcript", text: String(raw.text ?? "") });
        else if (raw.type === "sentence")
          onEvent({
            type: "sentence",
            index: Number(raw.index ?? 0),
            text: String(raw.text ?? ""),
            audioUrl: String(raw.audio_url ?? ""),
          });
        else if (raw.type === "done") onEvent({ type: "done", totalTurns: Number(raw.total_turns ?? 0) });
        else if (raw.type === "error") onEvent({ type: "error", message: String(raw.message ?? "") });
      } catch {
        /* 跳过坏行 */
      }
    }
  }
  return true;
}

/** 告诉后端这条会话已读（红点才会真的消失，刷新后不再回来） */
export async function markThreadRead(friendId: string) {
  return getJson(`${BASE}/friends/${friendId}/read`, { method: "POST" });
}

/** 「新的朋友」试听：让后端按这句话 + 这个声线现合成一段 wav */
export function ttsSampleUrl(text: string, voice: string, friendId?: string): string {
  const q = new URLSearchParams({ text: text.slice(0, 200), voice: voice || "en-GB-RyanNeural" });
  if (friendId) q.set("friend_id", friendId);
  return `${BASE}/tts/sample?${q.toString()}`;
}

/** 偶遇可选语言：后端只返回「真实声线能说」的语言 */
export async function discoverLanguages(): Promise<string[] | null> {
  const r = await getJson<{ languages?: string[] }>(`${BASE}/discover/languages`);
  if (!r || !Array.isArray(r.languages)) return null;
  return r.languages.filter((x): x is string => typeof x === "string" && !!x);
}

/** 删除自己发的动态 */
export async function deleteMoment(momentId: string): Promise<boolean> {
  const r = await getJson<{ deleted?: string }>(`${BASE}/moments/${momentId}`, {
    method: "DELETE",
  });
  return !!r?.deleted;
}

/** 轻量刷新：只拉好友列表 + 好友申请（不拉历史消息），用于轮询 */
export async function loadFriendList(): Promise<{
  friends: Friend[];
  requests: Friend[];
} | null> {
  const r = await getJson<{ friends?: RawFriend[]; requests?: RawFriend[] }>(
    `${BASE}/friends?limit=50`,
  );
  if (!r) return null;
  return {
    friends: ((r.friends as RawFriend[]) ?? []).map(mapFriend),
    requests: ((r.requests as RawFriend[]) ?? []).map((x) => ({
      ...mapFriend(x),
      isRequest: true,
      requestNote: String((x as Record<string, unknown>).request_message ?? ""),
    })),
  };
}
