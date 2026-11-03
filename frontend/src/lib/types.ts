/**
 * 协议类型定义 —— 严格对照 `docs/PROTOCOL.md`（权威）。
 *
 * 约定：
 *  - 服务端下行全部是 JSON 文本帧；二进制帧仅用于客户端上行 PCM。
 *  - 字段允许服务端后续扩展，因此所有"可能缺失"的字段一律标成 optional，
 *    解析时通过 lib/guards.ts 做运行时收敛，不用 `any`。
 */

export const PROTOCOL_VERSION = 1;
export const IN_SAMPLE_RATE = 16000;
export const OUT_SAMPLE_RATE = 24000;
export const FRAME_SAMPLES = 320; // 20 ms @ 16 kHz
export const FRAME_MS = (FRAME_SAMPLES / IN_SAMPLE_RATE) * 1000;

/* ------------------------------------------------------------------ 角色 / 模式 */

export type Difficulty = "easy" | "medium" | "hard";

export interface PersonaPublic {
  id: string;
  name: string;
  title?: string;
  scene?: string;
  accent?: string;
  accent_short?: string;
  flag?: string;
  city?: string;
  difficulty?: Difficulty | string;
  tags?: string[];
  traits?: string[];
  voice_notes?: string;
  opening_line?: string;
}

export interface ConversationMode {
  id: string;
  name: string;
  description?: string;
}

/* ------------------------------------------------------------------ 引擎信息 */

export interface EnginesInfo {
  stt?: string;
  tts?: string;
  llmConfigured?: boolean;
  llmModel?: string;
  llmBaseUrl?: string;
}

export interface AudioFormatInfo {
  format: string;
  sampleRate: number;
  channels: number;
}

/* ------------------------------------------------------------------ 指标 */

export interface TurnMetrics {
  stt_ms?: number;
  llm_ttft_ms?: number;
  first_audio_ms?: number;
  total_ms?: number;
  tts_first_sentence_ms?: number;
  sentences?: number;
  audio_bytes?: number;
}

/* ------------------------------------------------------------------ 客户端 → 服务端 */

export interface HelloMessage {
  type: "hello";
  persona_id: string;
  input_sample_rate: number;
  client: string;
  protocol_version: number;
}

export interface AudioEndMessage {
  type: "audio_end";
}

export interface TextMessage {
  type: "text";
  text: string;
  /** 多模态：data URL 列表（最多 4 张，单张 ≤6MB） */
  images?: string[];
}

export interface InterruptMessage {
  type: "interrupt";
}

export interface SetPersonaMessage {
  type: "set_persona";
  persona_id: string;
}

export interface SetModeMessage {
  type: "set_mode";
  mode: string;
}

export interface SuggestionRequestMessage {
  type: "suggestion";
  text?: string;
}

export interface ClearHistoryMessage {
  type: "clear_history";
}

export interface PttHoldMessage {
  type: "ptt_hold";
}

export interface PttCancelMessage {
  type: "ptt_cancel";
}

export interface PingMessage {
  type: "ping";
  t_ms: number;
}

export type ClientMessage =
  | HelloMessage
  | AudioEndMessage
  | TextMessage
  | InterruptMessage
  | SetPersonaMessage
  | SetModeMessage
  | SuggestionRequestMessage
  | ClearHistoryMessage
  | PttHoldMessage
  | PttCancelMessage
  | PingMessage;

/* ------------------------------------------------------------------ 服务端 → 客户端 */

export interface ReadyMessage {
  type: "ready";
  session_id: string;
  protocol_version: number;
  persona_id: string;
  personas?: PersonaPublic[];
  modes?: ConversationMode[];
  engines?: EnginesInfo;
  audio?: { format?: string; sample_rate?: number; channels?: number };
}

export interface VadMessage {
  type: "vad";
  state: "speech_start" | "speech_end";
  speech_ms?: number;
}

export interface UserTextMessage {
  type: "user_text";
  turn_id: number;
  text: string;
  latency?: { stt_ms?: number };
  truncated?: boolean;
}

export interface AssistantDeltaMessage {
  type: "assistant_delta";
  turn_id: number;
  text: string;
}

export interface AssistantSentenceMessage {
  type: "assistant_sentence";
  turn_id: number;
  sentence_index: number;
  text: string;
  tts_backend?: string;
}

export interface AudioMessage {
  type: "audio";
  turn_id: number;
  sentence_index: number;
  seq: number;
  format: string;
  sample_rate: number;
  channels: number;
  data: string;
  is_first?: boolean;
  is_last?: boolean;
}

export interface TurnAudioEndMessage {
  type: "turn_audio_end";
  turn_id: number;
  total_bytes?: number;
  sentences?: number;
}

export interface AssistantDoneMessage {
  type: "assistant_done";
  turn_id: number;
  text: string;
  metrics?: TurnMetrics;
}

export interface SuggestionItem {
  original: string;
  better: string;
  why: string;
  register?: string;
}

export interface SuggestionMessage {
  type: "suggestion";
  turn_id?: number;
  items: SuggestionItem[];
}

export type ErrorCode =
  | "bad_message"
  | "unsupported_sample_rate"
  | "llm_not_configured"
  | "llm_failed"
  | "stt_failed"
  | "tts_failed"
  | "vad_failed"
  | "persona_not_found"
  | "internal";

export interface ErrorMessage {
  type: "error";
  code: ErrorCode | string;
  message: string;
  fatal?: boolean;
  turn_id?: number;
}

export interface PongMessage {
  type: "pong";
  t_ms: number;
  server_time?: number;
}

export interface SessionInfoMessage {
  type: "session_info";
  session_id: string;
  turns?: number;
  persona_id?: string;
}

/** Lead 已在协议中新增：`set_mode` 的确认回执（可选，收不到也不影响本地状态）。 */
export interface ModeChangedMessage {
  type: "mode_changed";
  mode: string;
}

export type ServerMessage =
  | ReadyMessage
  | VadMessage
  | UserTextMessage
  | AssistantDeltaMessage
  | AssistantSentenceMessage
  | AudioMessage
  | TurnAudioEndMessage
  | AssistantDoneMessage
  | SuggestionMessage
  | ErrorMessage
  | PongMessage
  | SessionInfoMessage
  | ModeChangedMessage;

/* ------------------------------------------------------------------ UI 侧模型 */

export type ConnectionStatus = "disconnected" | "connecting" | "ready" | "error";

export interface SentenceRef {
  index: number;
  text: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  turnId: number;
  text: string;
  /** LLM 仍在流式输出 */
  streaming: boolean;
  /** 用户侧乐观回显，尚未被服务端 `user_text` 确认 */
  pending?: boolean;
  truncated?: boolean;
  sentences?: SentenceRef[];
  metrics?: TurnMetrics;
  createdAt: number;
  /** 图片消息：服务端持久化后的 URL（/api/uploads/xxx.png） */
  image?: string;
}

export interface SuggestionCard {
  id: string;
  turnId: number;
  original: string;
  better: string;
  why: string;
  register: string;
  receivedAt: number;
}

export interface Notice {
  id: string;
  level: "info" | "warn" | "error";
  text: string;
}
