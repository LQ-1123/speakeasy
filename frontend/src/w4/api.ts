/**
 * W4 的真实后端适配层。
 *
 * 设计原则：**组件一行不改**。这里只负责把后端的 v2 契约映射成
 * `model.ts` 里那套字段（Claude Code 当初就是照着能直接换数据来设计的）。
 *
 * 任何一步失败都返回 null，让 store 继续用 mock —— 原型永远能打开。
 */

import type { Friend, Message, Profile } from "./model";
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

const BASE = "/api/v2";

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
    greeting: "",
    greetingZh: "",
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
  if (raw.audio_url) mapped.audio = String(raw.audio_url);
  if (raw.image_url) mapped.image = String(raw.image_url);
  if (raw.suggestion) {
    const s = String(raw.suggestion);
    mapped.suggestion = { better: s, why: "", tag: "地道", saved: false };
  }
  return mapped;
}

/* ------------------------------------------------------------ 对外接口 */

export interface LiveSnapshot {
  friends: Friend[];
  messages: Message[];
  /** 只覆盖后端确实提供的字段 */
  profile: Partial<Profile>;
}

/** 拉一次全量：好友 + 每位好友最近消息 + 我的资料 */
export async function loadAll(): Promise<LiveSnapshot | null> {
  const boot = await getJson<Record<string, unknown>>(`${BASE}/bootstrap`);
  if (!boot) return null;
  const rawFriends = (boot.friends as RawFriend[]) ?? [];
  if (!rawFriends.length) return null;

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
  const profile: Partial<Profile> = {
    name: String(rawProfile.nickname ?? "我"),
    targets: Array.isArray(rawProfile.target_languages)
      ? (rawProfile.target_languages as string[])
      : ["en"],
  };

  return { friends, messages, profile };
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
export async function discover(language?: string): Promise<Friend | null> {
  const r = await getJson<{ friend?: RawFriend }>(`${BASE}/friends/discover`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(language ? { language } : {}),
  });
  if (!r?.friend) return null;
  return mapFriend(r.friend);
}

/** 保存「我在练」 */
export async function saveProfile(nickname: string, targets: string[]): Promise<boolean> {
  const r = await getJson(`${BASE}/memories`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nickname, target_languages: targets }),
  });
  return r !== null;
}
