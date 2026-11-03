"use client";

/**
 * SpeakEasy 2.0 数据模型 + 运行时归一化。
 *
 * 后端契约见 `docs/V2_API.md`。这里所有解析函数都**永不抛错**：
 * 字段缺失/异构时退回安全默认值，保证后端未就绪或降级时 UI 不白屏。
 */

import { asStringArray, isRecord, num, str } from "@/lib/guards";

export type Tab = "chats" | "discover" | "notes";

export interface OpenLoop {
  event: string;
  follow_up_after: string;
  status: string;
}

export interface LastMessage {
  sender: "user" | "friend" | string;
  transcript: string;
  msg_type: string;
  duration_ms: number;
  created_at: string;
}

export interface Friend {
  id: string;
  name: string;
  avatar_seed: string;
  archetype_id: string;
  voice_id: string;
  nationality: string;
  city: string;
  timezone: string;
  native_lang: string;
  age: number;
  occupation: string;
  speech_quirks: string[];
  bio: string;
  status: string;
  closeness_level: number;
  total_turns: number;
  shared_episodes: string[];
  open_loops: OpenLoop[];
  last_interaction_at: string;
  created_at: string;
  local_time: string;
  local_period: string;
  local_icon: string;
  local_activity: string;
  unread: number;
  last_message: LastMessage | null;
}

export interface Suggestion {
  better_version: string;
  explanation: string;
  tag: string;
  is_saved: boolean;
}

export interface Message {
  id: string;
  friend_id: string;
  sender: "user" | "friend" | string;
  msg_type: "voice" | "text" | "call_summary" | string;
  transcript: string;
  audio_path: string;
  audio_url: string;
  duration_ms: number;
  suggestion: Suggestion | null;
  is_read: boolean;
  context: string;
  created_at: string;
  /** 仅前端：本地临时气泡（等后端返回后替换） */
  pending?: boolean;
  /** 仅前端：转写占位文案 */
  placeholder?: boolean;
  /** 仅前端：本地 blob 预览地址（发送中即时回放自己的语音） */
  local_audio_url?: string;
}

export interface Moment {
  id: string;
  friend_id: string;
  content: string;
  mood_tag: string;
  local_time: string;
  feed_date: string;
  replied: boolean;
  created_at: string;
  friend: Friend | null;
}

export interface Expression {
  id: string;
  original_text: string;
  better_expression: string;
  explanation: string;
  status: "learning" | "mastered" | string;
  source_friend_id: string;
  created_at: string;
  mastery_count: number;
  friend_name: string;
}

export interface FactSheet {
  profile: {
    nickname: string;
    city: string;
    major_or_job: string;
    routine: string;
  };
  interests: string[];
  language_profile: { target: string; weaknesses: string[] };
  recent_events: string[];
}

export interface UserProfile {
  nickname: string;
  native_language: string;
  target_languages: string[];
  fact_sheet: FactSheet;
}

export interface FriendMemory {
  id: string;
  name: string;
  nationality: string;
  city: string;
  avatar_seed: string;
  closeness_level: number;
  shared_episodes: string[];
  open_loops: OpenLoop[];
}

export interface MemoryPayload {
  profile: UserProfile;
  fact_sheet: FactSheet;
  friends: FriendMemory[];
}

export interface ChatTurnResponse {
  user_message: Message | null;
  friend_messages: Message[];
  suggestion_pending: boolean;
  closeness_level: number;
  total_turns: number;
  memory_updated: boolean;
}

export interface BootstrapPayload {
  profile: UserProfile;
  friend_count: number;
  expressions_total: number;
  expressions_learning: number;
  llm_mock: boolean;
  /** 首次启动时后端在后台捏人，前端可加速轮询 */
  pending_onboarding: boolean;
  friends: Friend[];
  requests: Friend[];
}

export interface FriendsPayload {
  friends: Friend[];
  requests: Friend[];
}

/* ------------------------------------------------------------------ 解析 */

export function parseOpenLoop(raw: unknown): OpenLoop | null {
  if (!isRecord(raw)) return null;
  const event = str(raw.event);
  if (!event) return null;
  return {
    event,
    follow_up_after: str(raw.follow_up_after),
    status: str(raw.status) || "pending",
  };
}

export function parseOpenLoops(raw: unknown): OpenLoop[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(parseOpenLoop).filter((x): x is OpenLoop => x !== null);
}

export function parseFriend(raw: unknown): Friend | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  const name = str(raw.name);
  if (!id || !name) return null;

  const last = isRecord(raw.last_message) ? raw.last_message : null;
  return {
    id,
    name,
    avatar_seed: str(raw.avatar_seed) || name,
    archetype_id: str(raw.archetype_id),
    voice_id: str(raw.voice_id),
    nationality: str(raw.nationality),
    city: str(raw.city),
    timezone: str(raw.timezone),
    native_lang: str(raw.native_lang),
    age: num(raw.age),
    occupation: str(raw.occupation),
    speech_quirks: asStringArray(raw.speech_quirks) ?? [],
    bio: str(raw.bio),
    status: str(raw.status) || "active",
    closeness_level: num(raw.closeness_level),
    total_turns: num(raw.total_turns),
    shared_episodes: asStringArray(raw.shared_episodes) ?? [],
    open_loops: parseOpenLoops(raw.open_loops),
    last_interaction_at: str(raw.last_interaction_at),
    created_at: str(raw.created_at),
    local_time: str(raw.local_time),
    local_period: str(raw.local_period),
    local_icon: str(raw.local_icon),
    local_activity: str(raw.local_activity),
    unread: num(raw.unread),
    last_message: last
      ? {
          sender: str(last.sender) || "friend",
          transcript: str(last.transcript),
          msg_type: str(last.msg_type) || "voice",
          duration_ms: num(last.duration_ms),
          created_at: str(last.created_at),
        }
      : null,
  };
}

export function parseFriendsPayload(raw: unknown): FriendsPayload {
  if (!isRecord(raw)) return { friends: [], requests: [] };
  const friendsRaw = Array.isArray(raw.friends)
    ? raw.friends
    : Array.isArray(raw)
      ? (raw as unknown[])
      : [];
  const friends = friendsRaw.map(parseFriend).filter((x): x is Friend => x !== null);
  const requests = (Array.isArray(raw.requests) ? raw.requests : [])
    .map(parseFriend)
    .filter((x): x is Friend => x !== null);
  return { friends, requests };
}

export function parseSuggestion(raw: unknown): Suggestion | null {
  if (!isRecord(raw)) return null;
  const better = str(raw.better_version);
  if (!better) return null;
  return {
    better_version: better,
    explanation: str(raw.explanation),
    tag: str(raw.tag),
    is_saved: raw.is_saved === true,
  };
}

export function parseMessage(raw: unknown): Message | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  const msgType = str(raw.msg_type) || "voice";
  return {
    id,
    friend_id: str(raw.friend_id),
    sender: str(raw.sender) === "user" ? "user" : "friend",
    msg_type: msgType,
    transcript: str(raw.transcript),
    audio_path: str(raw.audio_path),
    audio_url: str(raw.audio_url),
    duration_ms: num(raw.duration_ms),
    suggestion: parseSuggestion(raw.suggestion),
    is_read: raw.is_read !== false,
    context: str(raw.context),
    created_at: str(raw.created_at),
  };
}

export function parseMessages(raw: unknown): Message[] {
  const list = Array.isArray(raw)
    ? raw
    : isRecord(raw) && Array.isArray(raw.messages)
      ? raw.messages
      : [];
  return list.map(parseMessage).filter((x): x is Message => x !== null);
}

export function parseMoment(raw: unknown): Moment | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  const friend = parseFriend(raw.friend);
  return {
    id,
    friend_id: str(raw.friend_id),
    content: str(raw.content),
    mood_tag: str(raw.mood_tag),
    local_time: str(raw.local_time),
    feed_date: str(raw.feed_date),
    replied: raw.replied === true || raw.is_replied === true,
    created_at: str(raw.created_at),
    friend,
  };
}

export function parseMoments(raw: unknown): Moment[] {
  const list = isRecord(raw) && Array.isArray(raw.moments) ? raw.moments : Array.isArray(raw) ? raw : [];
  return list.map(parseMoment).filter((x): x is Moment => x !== null);
}

export function parseExpression(raw: unknown): Expression | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  const better = str(raw.better_expression);
  const original = str(raw.original_text);
  if (!better && !original) return null;
  const friend = isRecord(raw.friend) ? raw.friend : null;
  return {
    id,
    original_text: original,
    better_expression: better,
    explanation: str(raw.explanation),
    status: str(raw.status) || "learning",
    source_friend_id: str(raw.source_friend_id),
    created_at: str(raw.created_at),
    mastery_count: num(raw.mastery_count),
    friend_name: str(raw.friend_name) || (friend ? str(friend.name) : ""),
  };
}

export function parseExpressions(raw: unknown): Expression[] {
  const list = isRecord(raw) && Array.isArray(raw.expressions) ? raw.expressions : Array.isArray(raw) ? raw : [];
  return list.map(parseExpression).filter((x): x is Expression => x !== null);
}

export function emptyFactSheet(): FactSheet {
  return {
    profile: { nickname: "", city: "", major_or_job: "", routine: "" },
    interests: [],
    language_profile: { target: "en", weaknesses: [] },
    recent_events: [],
  };
}

export function parseFactSheet(raw: unknown): FactSheet {
  const base = emptyFactSheet();
  if (!isRecord(raw)) return base;
  const p = isRecord(raw.profile) ? raw.profile : {};
  const lp = isRecord(raw.language_profile) ? raw.language_profile : {};
  return {
    profile: {
      nickname: str(p.nickname),
      city: str(p.city),
      major_or_job: str(p.major_or_job),
      routine: str(p.routine),
    },
    interests: asStringArray(raw.interests) ?? [],
    language_profile: {
      target: str(lp.target) || "en",
      weaknesses: asStringArray(lp.weaknesses) ?? [],
    },
    recent_events: asStringArray(raw.recent_events) ?? [],
  };
}

export function parseProfile(raw: unknown): UserProfile {
  const r = isRecord(raw) ? raw : {};
  const factRaw = isRecord(r.fact_sheet) ? r.fact_sheet : {};
  const fact = parseFactSheet(factRaw);
  return {
    nickname: str(r.nickname) || fact.profile.nickname,
    native_language: str(r.native_language) || "zh",
    target_languages: asStringArray(r.target_languages) ?? ["en"],
    fact_sheet: fact,
  };
}

export function parseFriendMemory(raw: unknown): FriendMemory | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  return {
    id,
    name: str(raw.name) || "好友",
    nationality: str(raw.nationality),
    city: str(raw.city),
    avatar_seed: str(raw.avatar_seed) || str(raw.name),
    closeness_level: num(raw.closeness_level),
    shared_episodes: asStringArray(raw.shared_episodes) ?? [],
    open_loops: parseOpenLoops(raw.open_loops),
  };
}

export function parseMemoryPayload(raw: unknown): MemoryPayload {
  const r = isRecord(raw) ? raw : {};
  const profile = parseProfile(r.profile);
  const factSheet = isRecord(r.fact_sheet) ? parseFactSheet(r.fact_sheet) : profile.fact_sheet;
  const friends = (Array.isArray(r.friends) ? r.friends : [])
    .map(parseFriendMemory)
    .filter((x): x is FriendMemory => x !== null);
  return { profile, fact_sheet: factSheet, friends };
}

export function parseBootstrap(raw: unknown): BootstrapPayload {
  const r = isRecord(raw) ? raw : {};
  const friends = parseFriendsPayload(r);
  const stats = isRecord(r.stats) ? r.stats : {};
  const llm = isRecord(r.llm) ? r.llm : {};
  return {
    profile: parseProfile(r.profile),
    friend_count: num(r.friend_count) || num(stats.friends) || friends.friends.length,
    expressions_total: num(r.expressions_total) || num(stats.expressions),
    expressions_learning: num(r.expressions_learning) || num(stats.learning),
    // llm.mock=true 表示后端无 Key、走本地规则引擎（演示模式）
    llm_mock: r.llm_mock === true || llm.mock === true,
    pending_onboarding: r.pending_onboarding === true,
    friends: friends.friends,
    requests: friends.requests,
  };
}

export function parseChatTurn(raw: unknown): ChatTurnResponse {
  const r = isRecord(raw) ? raw : {};
  const user = parseMessage(r.user_message);
  const list = Array.isArray(r.friend_messages) ? r.friend_messages : [];
  const friendMessages = list.map(parseMessage).filter((x): x is Message => x !== null);
  return {
    user_message: user,
    friend_messages: friendMessages,
    suggestion_pending: r.suggestion_pending === true,
    closeness_level: num(r.closeness_level),
    total_turns: num(r.total_turns),
    memory_updated: r.memory_updated === true,
  };
}

/* ================================================================== */
/* 个人设置：LLM / STT 配置（契约见 docs/V2_STT.md §3、§5）              */
/* ================================================================== */

export interface LlmConfigView {
  base_url: string;
  model: string;
  temperature: number;
  max_tokens: number;
  suggestion_model: string;
  api_key_set: boolean;
  api_key_masked: string;
  configured: boolean;
  source: string;
}

export interface LlmHealthView {
  configured: boolean;
  base_url: string;
  model: string;
  mock: boolean;
  error: string;
}

export interface LlmPreset {
  id: string;
  name: string;
  base_url: string;
  model: string;
  key_hint: string;
  note: string;
}

export interface LlmSettingsPayload {
  config: LlmConfigView | null;
  health: LlmHealthView | null;
  presets: LlmPreset[];
}

export interface LlmVerifyResult {
  ok: boolean;
  message: string;
  latency_ms: number;
  model_reply: string;
}

export interface LlmSaveResult {
  ok: boolean;
  message: string;
  config: LlmConfigView | null;
  verified: LlmVerifyResult | null;
}

export interface LlmSaveInput {
  base_url?: string;
  model?: string;
  api_key?: string;
  verify?: boolean;
  temperature?: number;
  max_tokens?: number;
}

export interface LlmTestInput {
  base_url?: string;
  model?: string;
  api_key?: string;
}

export type SttProvider = "auto" | "local" | "siliconflow";

export interface SttConfigView {
  provider: SttProvider;
  enabled: boolean;
  base_url: string;
  model: string;
  language: string;
  timeout_s: number;
  api_key_set: boolean;
  api_key_masked: string;
  configured: boolean;
  source: string;
}

export interface SttHealthView {
  enabled: boolean;
  provider: string;
  configured: boolean;
  /** 当前实际生效的 STT 后端：faster-whisper | sensevoice ... */
  backend: string;
  model: string;
  last_error: string;
  last_latency_ms: number | null;
  requests: number;
  last_success_at: string;
}

export interface SttPreset {
  id: "local" | "siliconflow" | string;
  name: string;
  base_url: string;
  model: string;
}

export interface SttSettingsPayload {
  config: SttConfigView | null;
  health: SttHealthView | null;
  presets: SttPreset[];
}

export interface SttVerifyResult {
  ok: boolean;
  message: string;
  latency_ms: number;
  text: string;
}

export interface SttSaveInput {
  provider: SttProvider;
  base_url?: string;
  model?: string;
  api_key?: string;
  language?: string;
  timeout_s?: number;
  verify?: boolean;
}

export interface SttTestInput {
  provider?: SttProvider;
  base_url?: string;
  model?: string;
  api_key?: string;
  language?: string;
  timeout_s?: number;
}

export interface SttSaveResult extends SttSettingsPayload {
  ok: boolean;
  message: string;
  verified: SttVerifyResult | null;
}

export const DEFAULT_STT_CONFIG: SttConfigView = {
  provider: "auto",
  enabled: false,
  base_url: "https://api.siliconflow.cn/v1",
  model: "FunAudioLLM/SenseVoiceSmall",
  language: "en",
  timeout_s: 30,
  api_key_set: false,
  api_key_masked: "",
  configured: false,
  source: "default",
};

export const DEFAULT_STT_PRESETS: SttPreset[] = [
  { id: "local", name: "本地 Whisper（离线）", base_url: "", model: "distil-large-v3" },
  {
    id: "siliconflow",
    name: "硅基流动 SenseVoiceSmall",
    base_url: "https://api.siliconflow.cn/v1",
    model: "FunAudioLLM/SenseVoiceSmall",
  },
];

function parseProvider(raw: unknown): SttProvider {
  const value = str(raw);
  return value === "local" || value === "siliconflow" ? value : "auto";
}

export function parseLlmConfigView(raw: unknown): LlmConfigView | null {
  if (!isRecord(raw)) return null;
  return {
    base_url: str(raw.base_url),
    model: str(raw.model),
    temperature: num(raw.temperature),
    max_tokens: num(raw.max_tokens),
    suggestion_model: str(raw.suggestion_model),
    api_key_set: raw.api_key_set === true,
    api_key_masked: str(raw.api_key_masked),
    configured: raw.configured === true,
    source: str(raw.source),
  };
}

export function parseLlmHealthView(raw: unknown): LlmHealthView | null {
  if (!isRecord(raw)) return null;
  return {
    configured: raw.configured === true,
    base_url: str(raw.base_url),
    model: str(raw.model),
    mock: raw.mock === true,
    error: str(raw.error),
  };
}

export function parseLlmPresets(raw: unknown): LlmPreset[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(isRecord)
    .map((item) => ({
      id: str(item.id),
      name: str(item.name),
      base_url: str(item.base_url),
      model: str(item.model),
      key_hint: str(item.key_hint),
      note: str(item.note),
    }))
    .filter((item) => item.id !== "");
}

export function parseLlmSettings(raw: unknown): LlmSettingsPayload {
  const r = isRecord(raw) ? raw : {};
  return {
    config: parseLlmConfigView(r.config),
    health: parseLlmHealthView(r.health),
    presets: parseLlmPresets(r.presets),
  };
}

export function parseLlmVerify(raw: unknown): LlmVerifyResult {
  const r = isRecord(raw) ? raw : {};
  return {
    ok: r.ok === true,
    message: str(r.message),
    latency_ms: num(r.latency_ms),
    model_reply: str(r.model_reply),
  };
}

export function parseLlmSaveResult(raw: unknown): LlmSaveResult {
  const r = isRecord(raw) ? raw : {};
  return {
    ok: r.ok === true,
    message: str(r.message),
    config: parseLlmConfigView(r.config),
    verified: isRecord(r.verified) ? parseLlmVerify(r.verified) : null,
  };
}

export function parseSttConfigView(raw: unknown): SttConfigView | null {
  if (!isRecord(raw)) return null;
  const timeout = num(raw.timeout_s);
  return {
    provider: parseProvider(raw.provider),
    enabled: raw.enabled === true,
    base_url: str(raw.base_url),
    model: str(raw.model),
    language: str(raw.language) || "en",
    timeout_s: timeout > 0 ? timeout : 30,
    api_key_set: raw.api_key_set === true,
    api_key_masked: str(raw.api_key_masked),
    configured: raw.configured === true,
    source: str(raw.source),
  };
}

export function parseSttHealthView(raw: unknown): SttHealthView | null {
  if (!isRecord(raw)) return null;
  const latency = raw.last_latency_ms;
  return {
    enabled: raw.enabled === true,
    provider: str(raw.provider) || "auto",
    configured: raw.configured === true,
    backend: str(raw.backend) || "faster-whisper",
    model: str(raw.model),
    last_error: str(raw.last_error),
    last_latency_ms: typeof latency === "number" && Number.isFinite(latency) ? latency : null,
    requests: num(raw.requests),
    last_success_at: str(raw.last_success_at),
  };
}

export function parseSttPresets(raw: unknown): SttPreset[] {
  if (!Array.isArray(raw)) return [];
  const presets = raw
    .filter(isRecord)
    .map((item) => ({
      id: str(item.id),
      name: str(item.name),
      base_url: str(item.base_url),
      model: str(item.model),
    }))
    .filter((item) => item.id !== "");
  return presets.length > 0 ? presets : DEFAULT_STT_PRESETS;
}

export function parseSttSettings(raw: unknown): SttSettingsPayload {
  const r = isRecord(raw) ? raw : {};
  return {
    config: parseSttConfigView(r.config),
    health: parseSttHealthView(r.health),
    presets: parseSttPresets(r.presets),
  };
}

export function parseSttVerify(raw: unknown): SttVerifyResult {
  const r = isRecord(raw) ? raw : {};
  return {
    ok: r.ok === true,
    message: str(r.message),
    latency_ms: num(r.latency_ms),
    text: str(r.text),
  };
}

export function parseSttSaveResult(raw: unknown): SttSaveResult {
  const r = isRecord(raw) ? raw : {};
  return {
    ok: r.ok === true,
    message: str(r.message),
    config: parseSttConfigView(r.config),
    health: parseSttHealthView(r.health),
    presets: parseSttPresets(r.presets),
    verified: isRecord(r.verified) ? parseSttVerify(r.verified) : null,
  };
}
