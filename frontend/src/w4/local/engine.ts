/**
 * 本地模式的「后端业务逻辑」：全部跑在手机里。
 *
 * - 数据：IndexedDB
 * - LLM：DeepSeek 直连
 * - TTS：Fish 直连（blob URL）
 * - STT：SiliconFlow 直连
 *
 * 这一版覆盖：资料/onboarding、好友申请（LLM 捏人）、文字聊天、
 * 语音消息、翻译、已读。动态/通话等后续阶段补齐。
 */

import { diceAvatar } from "../avatar";
import type { Friend, Message, Profile } from "../model";
import { dbGet, dbSet, newId } from "./db";
import { loadKeys } from "./keys";
import { llmChat, llmJson } from "./llm";
import { fishTts } from "./tts";
import { transcribeBlob } from "./stt";

type LocalState = {
  profile: Profile | null;
  friends: Friend[];
  requests: Friend[];
  messages: Message[];
};

const STATE_KEY = "state.v1";

let state: LocalState | null = null;

async function getState(): Promise<LocalState> {
  if (state) return state;
  const saved = await dbGet<LocalState>(STATE_KEY);
  state = saved ?? { profile: null, friends: [], requests: [], messages: [] };
  return state;
}

async function persist(): Promise<void> {
  if (state) await dbSet(STATE_KEY, state);
}

function nowIso(): string {
  return new Date().toISOString();
}

function sanitizeName(raw: string): string {
  const name = (raw || "").trim().split(/\s+/)[0] || "Friend";
  return name.replace(/[^A-Za-z\u00C0-\u024F'-]/g, "").slice(0, 20) || "Friend";
}

type Persona = {
  name: string;
  age: number;
  gender: "male" | "female";
  nationality: string;
  city: string;
  occupation: string;
  native_lang: string;
  reply_language: string;
  bio: string;
  personality_prompt: string;
  greeting: string;
  fish_reference_id?: string;
};

async function generatePersona(language: string, avoid: string[]): Promise<Persona | null> {
  const avoidText = avoid.length ? `不要使用这些名字：${avoid.join(", ")}。` : "";
  const prompt = `You create a realistic conversation partner for a language learner.
Target language code: ${language}.
Return STRICT JSON only with keys:
name, age (20-35), gender ("male"|"female"), nationality, city, occupation,
native_lang (ISO code), reply_language (same as target), bio (1 short sentence),
personality_prompt (2nd-person instructions for how this person texts: tone, quirks, topics),
greeting (one casual opening message in the target language, max 18 words).
${avoidText}`;
  const data = await llmJson<Persona>(
    [
      { role: "system", content: "You output valid JSON only. No markdown." },
      { role: "user", content: prompt },
    ],
    { maxTokens: 900, temperature: 0.95 },
  );
  if (!data || !data.name) return null;
  return {
    ...data,
    name: sanitizeName(data.name),
    age: Number(data.age) || 24,
    gender: data.gender === "male" ? "male" : "female",
    native_lang: (data.native_lang || language).toLowerCase(),
    reply_language: (data.reply_language || language).toLowerCase(),
  };
}

function toFriend(p: Persona, opts: { request: boolean; greetingAudio?: string }): Friend {
  const id = newId();
  const flagOf = (country: string): string => {
    const c = country.slice(0, 2).toUpperCase();
    if (!/^[A-Z]{2}$/.test(c)) return "🌍";
    return String.fromCodePoint(...[...c].map((ch) => 0x1f1e6 + ch.charCodeAt(0) - 65));
  };
  const languageLabel = (code: string): string =>
    ({ en: "英语", ja: "日语", ko: "韩语", es: "西语", fr: "法语", de: "德语", it: "意大利语", pt: "葡语" } as Record<string, string>)[code] || code;
  return {
    personality_prompt: p.personality_prompt || "",
    id,
    name: p.name,
    gender: p.gender,
    avatar: diceAvatar(id, p.gender),
    city: p.city || "",
    country: p.nationality || "",
    flag: flagOf(p.nationality || ""),
    language: p.reply_language || "en",
    languageLabel: languageLabel(p.reply_language || "en"),
    timezone: "",
    localTime: "",
    localPeriod: "",
    localActivity: "",
    occupation: p.occupation || "",
    age: p.age,
    bio: p.bio || "",
    quirks: [],
    closeness: 1,
    turns: 0,
    sharedEpisodes: [],
    openLoops: [],
    unread: 0,
    replying: false,
    online: true,
    lastActive: "刚刚",
    voice: p.fish_reference_id || "",
    voiceLabel: "默认",
    greeting: p.greeting || "",
    greetingZh: "",
    greetingAudio: opts.greetingAudio,
    isRequest: opts.request,
    requestNote: p.greeting || "",
  } as Friend;
}

function lastTurns(friendId: string, limit = 12): Message[] {
  const list = (state?.messages ?? []).filter((m) => m.friendId === friendId);
  return list.slice(-limit);
}

const LANG_NAMES: Record<string, string> = {
  en: "English",
  ja: "Japanese",
  ko: "Korean",
  es: "Spanish",
  fr: "French",
  de: "German",
  it: "Italian",
  pt: "Portuguese",
};

async function generateReply(friend: Friend, userText: string): Promise<string> {
  const lang = friend.language || "en";
  const persona = (friend as unknown as { personality_prompt?: string }).personality_prompt || friend.bio;
  const sys = `You are ${friend.name}, ${friend.age}, from ${friend.city || friend.country}. ${persona}
You are texting a language-learning friend on a chat app. Reply ONLY in ${LANG_NAMES[lang] || lang}.
Keep it natural and short: 1-2 short spoken-style lines, contractions, casual tone, at most one question.
Never mention being an AI. Never use markdown.`;
  const history = lastTurns(friend.id, 10)
    .filter((m) => m.kind === "text" || m.kind === "voice")
    .map((m) => ({
      role: (m.from === "me" ? "user" : "assistant") as "user" | "assistant",
      content: m.text,
    }));
  const reply = await llmChat(
    [{ role: "system", content: sys }, ...history, { role: "user", content: userText }],
    { maxTokens: 500, temperature: 0.9 },
  );
  return reply.replace(/^["“]|["”]$/g, "").trim().slice(0, 300);
}

function appendMessage(msg: Message): void {
  if (!state) return;
  state.messages.push(msg);
}

function makeMessage(partial: Partial<Message> & Pick<Message, "friendId" | "from" | "text">): Message {
  return {
    id: newId(),
    kind: partial.kind ?? "text",
    time: nowIso(),
    read: partial.from === "me",
    ...partial,
  } as Message;
}

/* ------------------------------------------------------------------ */
/* 对外：与 api.ts 对应的本地实现                                      */
/* ------------------------------------------------------------------ */

export async function localLoadAll(): Promise<{
  friends: Friend[];
  messages: Message[];
  profile: Partial<Profile>;
  requests: Friend[];
} | null> {
  const s = await getState();
  return {
    friends: s.friends,
    messages: s.messages,
    profile: (s.profile ?? { name: "", targets: ["en"] }) as Partial<Profile>,
    requests: s.requests,
  };
}

export async function localFriendById(id: string): Promise<Friend | null> {
  const st = await getState();
  return st.friends.find((f) => f.id === id) ?? st.requests.find((f) => f.id === id) ?? null;
}

export async function localLoadFriendList(): Promise<{ friends: Friend[]; requests: Friend[] }> {
  const s = await getState();
  return { friends: s.friends, requests: s.requests };
}

export async function localFetchMessages(friendId: string): Promise<Message[] | null> {
  const s = await getState();
  return s.messages.filter((m) => m.friendId === friendId);
}

export async function localMarkRead(friendId: string): Promise<void> {
  const s = await getState();
  s.messages = s.messages.map((m) => (m.friendId === friendId ? { ...m, read: true } : m));
  s.friends = s.friends.map((f) => (f.id === friendId ? { ...f, unread: 0 } : f));
  await persist();
}

export async function localSaveProfile(
  nickname: string,
  targets: string[],
  birthday?: string,
): Promise<boolean> {
  const s = await getState();
  s.profile = {
    ...(s.profile ?? {}),
    name: nickname || "我",
    targets: targets.length ? targets : ["en"],
    birthday: birthday ?? "",
  } as Profile;
  await persist();

  // 首次填写资料时，本地生成 2 个好友申请
  if (s.requests.length === 0 && s.friends.length === 0) {
    void (async () => {
      const lang = (targets[0] || "en").slice(0, 2);
      const made: Friend[] = [];
      for (let i = 0; i < 2; i += 1) {
        const p = await generatePersona(lang, made.map((f) => f.name));
        if (!p) continue;
        const audio = p.greeting ? await fishTts(p.greeting, { gender: p.gender }) : null;
        const f = toFriend(p, { request: true, greetingAudio: audio?.url });
        made.push(f);
        state?.requests.push(f);
      }
      await persist();
    })();
  }
  return true;
}

export async function localDiscover(language = "en"): Promise<{ friend: Friend; message: string; greeting: string; greetingAudio?: string } | null> {
  const s = await getState();
  const p = await generatePersona(language, [...s.requests, ...s.friends].map((f) => f.name));
  if (!p) return null;
  const audio = p.greeting ? await fishTts(p.greeting, { gender: p.gender }) : null;
  const friend = toFriend(p, { request: true, greetingAudio: audio?.url });
  s.requests.push(friend);
  await persist();
  return { friend, message: p.greeting || "", greeting: p.greeting || "", greetingAudio: audio?.url };
}

export async function localAcceptFriend(id: string): Promise<boolean> {
  const s = await getState();
  const req = s.requests.find((f) => f.id === id);
  if (!req) return false;
  s.requests = s.requests.filter((f) => f.id !== id);
  s.friends.push({ ...req, isRequest: false, unread: 0 });
  await persist();
  return true;
}

export async function localDeclineFriend(id: string): Promise<boolean> {
  const s = await getState();
  s.requests = s.requests.filter((f) => f.id !== id);
  await persist();
  return true;
}

export async function localRemoveFriend(id: string): Promise<boolean> {
  const s = await getState();
  s.friends = s.friends.filter((f) => f.id !== id);
  s.messages = s.messages.filter((m) => m.friendId !== id);
  await persist();
  return true;
}

export async function localSendText(friendId: string, text: string): Promise<Message[] | null> {
  const s = await getState();
  const friend = s.friends.find((f) => f.id === friendId);
  if (!friend) return null;
  const mine = makeMessage({ friendId, from: "me", text, kind: "text", read: true });
  appendMessage(mine);
  await persist();
  try {
    const reply = await generateReply(friend, text);
    const audio = await fishTts(reply, { gender: friend.gender === "male" ? "male" : "female" });
    const friendMsg = makeMessage({
      friendId,
      from: "friend",
      text: reply,
      kind: "voice",
      audio: audio?.url,
      durationMs: audio?.durationMs,
    });
    appendMessage(friendMsg);
    friend.turns += 1;
    friend.lastActive = "刚刚";
  } catch {
    /* 网络失败：保留用户消息 */
  }
  await persist();
  return s.messages.filter((m) => m.friendId === friendId);
}

export async function localSendVoiceBlob(
  friendId: string,
  blob: Blob,
  durationMs: number,
): Promise<Message[] | null> {
  const s = await getState();
  const friend = s.friends.find((f) => f.id === friendId);
  if (!friend) return null;
  let text = "";
  try {
    text = await transcribeBlob(blob);
  } catch {
    text = "";
  }
  const mine = makeMessage({
    friendId,
    from: "me",
    text: text || "（语音）",
    kind: "voice",
    durationMs,
    read: true,
  });
  appendMessage(mine);
  await persist();
  if (text) {
    try {
      const reply = await generateReply(friend, text);
      const audio = await fishTts(reply, { gender: friend.gender === "male" ? "male" : "female" });
      appendMessage(
        makeMessage({
          friendId,
          from: "friend",
          text: reply,
          kind: "voice",
          audio: audio?.url,
          durationMs: audio?.durationMs,
        }),
      );
      friend.turns += 1;
    } catch {
      /* ignore */
    }
  }
  await persist();
  return s.messages.filter((m) => m.friendId === friendId);
}

export async function localTranslate(text: string): Promise<string | null> {
  if (!text.trim()) return null;
  try {
    const out = await llmChat(
      [
        { role: "system", content: "你是翻译。把用户输入翻译成简体中文，只输出译文，不要解释。" },
        { role: "user", content: text.slice(0, 1200) },
      ],
      { maxTokens: 600, temperature: 0.2 },
    );
    return out.trim() || null;
  } catch {
    return null;
  }
}

export function localHasLlmKey(): boolean {
  return Boolean(loadKeys().llmKey.trim());
}

function splitSentences(text: string): string[] {
  const parts = text
    .split(/(?<=[.!?])\s+/)
    .map((x) => x.trim())
    .filter(Boolean);
  if (parts.length <= 1) return [text.trim()].filter(Boolean);
  return parts.slice(0, 4);
}

export type LocalCallEvent =
  | { type: "transcript"; text: string }
  | { type: "sentence"; index: number; text: string; audioUrl: string }
  | { type: "done"; totalTurns: number }
  | { type: "error"; message: string };

/** 本地语音通话一回合：STT → LLM → 逐句 TTS */
export async function localCallTurnStream(
  friendId: string,
  blob: Blob,
  durationMs: number,
  onEvent: (e: LocalCallEvent) => void,
): Promise<boolean> {
  const s = await getState();
  const friend = s.friends.find((f) => f.id === friendId);
  if (!friend) {
    onEvent({ type: "error", message: "好友不存在" });
    return false;
  }
  let text = "";
  try {
    text = await transcribeBlob(blob);
  } catch {
    text = "";
  }
  onEvent({ type: "transcript", text });
  if (!text.trim()) {
    onEvent({ type: "error", message: "没有听清，再说一次？" });
    return false;
  }
  appendMessage(
    makeMessage({ friendId, from: "me", text, kind: "voice", durationMs, read: true }),
  );
  await persist();

  try {
    const reply = await generateReply(friend, text);
    const sentences = splitSentences(reply);
    let firstAudio: string | undefined;
    for (let i = 0; i < sentences.length; i += 1) {
      const audio = await fishTts(sentences[i], {
        gender: friend.gender === "male" ? "male" : "female",
      });
      if (i === 0) firstAudio = audio?.url;
      onEvent({ type: "sentence", index: i, text: sentences[i], audioUrl: audio?.url ?? "" });
    }
    appendMessage(
      makeMessage({
        friendId,
        from: "friend",
        text: reply,
        kind: "call",
        audio: firstAudio,
      }),
    );
    friend.turns += 1;
    await persist();
    onEvent({ type: "done", totalTurns: friend.turns });
    return true;
  } catch (e) {
    onEvent({ type: "error", message: String(e).slice(0, 120) });
    return false;
  }
}
