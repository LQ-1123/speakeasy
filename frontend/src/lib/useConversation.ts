"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AudioPlayer, type PlayerStats } from "@/lib/audio/player";
import { MicCapture } from "@/lib/audio/mic";
import { asEngineLabel, asNumber, asString, errorMessageOf, isRecord, micErrorMessage, micPreflight } from "@/lib/guards";
import {
  IN_SAMPLE_RATE,
  PROTOCOL_VERSION,
  type ChatMessage,
  type ClientMessage,
  type ConnectionStatus,
  type ConversationMode,
  type EnginesInfo,
  type PersonaPublic,
  type SentenceRef,
  type SuggestionCard,
  type TurnMetrics,
  type Notice,
} from "@/lib/types";

/* ------------------------------------------------------------------ 常量 */

const CLIENT_NAME = "web";
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_BASE_MS = 400;
const PING_INTERVAL_MS = 15000;
/** 本地能量触发 barge-in 的阈值与连续帧数（3 帧 = 60 ms） */
const BARGE_IN_RMS = 0.055;
const BARGE_IN_FRAMES = 3;
const BARGE_IN_COOLDOWN_MS = 1500;

let noticeSeq = 0;
function makeNotice(level: Notice["level"], text: string): Notice {
  noticeSeq += 1;
  return { id: `notice-${noticeSeq}`, level, text };
}

let cardSeq = 0;
function makeCard(turnId: number, item: SuggestionCard): SuggestionCard {
  cardSeq += 1;
  return { ...item, id: `sg-${cardSeq}`, turnId };
}

/* ------------------------------------------------------------------ WS 地址 */

/**
 * 计算 WebSocket 地址。
 *
 * - 生产（FastAPI 根路径挂载静态产物）：`ws(s)://<当前 host>/ws`，与页面同源。
 * - 开发（`next dev -p 3000`）：Next 的 rewrite 不支持 WebSocket，因此直连后端。
 *   端口判定放在**运行时**，静态导出产物里这段分支天然不会命中。
 */
function resolveWsUrl(): string {
  if (typeof window === "undefined") return "";
  const { protocol, hostname, port } = window.location;
  const wsProtocol = protocol === "https:" ? "wss:" : "ws:";

  const isNextDev = port === "3000";
  if (isNextDev) {
    const devOrigin = process.env.NEXT_PUBLIC_DEV_BACKEND_ORIGIN ?? "http://127.0.0.1:8000";
    try {
      const url = new URL(devOrigin);
      const backendPort = url.port || (url.protocol === "https:" ? "443" : "80");
      return `${url.protocol === "https:" ? "wss:" : "ws:"}//${url.hostname}:${backendPort}/ws`;
    } catch {
      return `ws://127.0.0.1:8000/ws`;
    }
  }

  return `${wsProtocol}//${hostname}${port ? `:${port}` : ""}/ws`;
}

/** 相对路径 API：静态导出下必须运行时相对，构建期不读 env（保留给后续扩展）。 */

/* ------------------------------------------------------------------ 引擎信息归一 */

function normalizeEngines(raw: unknown): EnginesInfo {
  if (!isRecord(raw)) return {};
  const llm = raw.llm;
  return {
    stt: asEngineLabel(raw.stt),
    tts: asEngineLabel(raw.tts),
    llmConfigured: isRecord(llm) ? typeof llm.configured === "boolean" ? llm.configured : undefined : undefined,
    llmModel: isRecord(llm) ? asString(llm.model) : undefined,
    llmBaseUrl: isRecord(llm) ? asString(llm.base_url) : undefined,
  };
}

function normalizePersonas(raw: unknown): PersonaPublic[] {
  const list = isRecord(raw) && Array.isArray(raw.personas) ? raw.personas : Array.isArray(raw) ? raw : [];
  const out: PersonaPublic[] = [];
  for (const item of list) {
    if (!isRecord(item)) continue;
    const id = asString(item.id);
    const name = asString(item.name);
    if (!id || !name) continue;
    out.push({
      id,
      name,
      title: asString(item.title),
      scene: asString(item.scene),
      accent: asString(item.accent),
      accent_short: asString(item.accent_short),
      flag: asString(item.flag),
      city: asString(item.city),
      difficulty: asString(item.difficulty),
      tags: Array.isArray(item.tags) ? item.tags.filter((t): t is string => typeof t === "string") : undefined,
      traits: Array.isArray(item.traits) ? item.traits.filter((t): t is string => typeof t === "string") : undefined,
      voice_notes: asString(item.voice_notes),
      opening_line: asString(item.opening_line),
    });
  }
  return out;
}

function normalizeModes(raw: unknown): ConversationMode[] {
  if (!Array.isArray(raw)) return [];
  const out: ConversationMode[] = [];
  for (const item of raw) {
    if (!isRecord(item)) continue;
    const id = asString(item.id);
    const name = asString(item.name);
    if (!id || !name) continue;
    out.push({ id, name, description: asString(item.description) });
  }
  return out;
}

/* ------------------------------------------------------------------ Hook 返回类型 */

export interface ConversationApi {
  status: ConnectionStatus;
  statusDetail: string;
  sessionId: string | null;
  engines: EnginesInfo;
  personas: PersonaPublic[];
  modes: ConversationMode[];
  activePersonaId: string;
  activeMode: string;
  messages: ChatMessage[];
  suggestions: SuggestionCard[];
  notices: Notice[];
  metrics: TurnMetrics;
  serverSampleRate: number;
  vadSpeaking: boolean;
  recording: boolean;
  micAvailable: boolean;
  micError: string | null;
  /** 当前选中的输入设备 id（空 = 系统默认） */
  micDeviceId: string;
  /** 换输入设备（会重建 mic） */
  setMicDeviceId: (id: string) => void;
  playerStats: PlayerStats;
  activeSentence: { turnId: number; sentenceIndex: number } | null;
  pending: boolean;
  websocketOpen: boolean;

  connect: () => void;
  disconnect: () => void;
  sendText: (text: string, images?: string[]) => void;
  selectPersona: (id: string) => void;
  selectMode: (id: string) => void;
  interrupt: () => void;
  toggleRecording: () => void;
  /** 清空当前角色的对话记录 */
  clearConversation: () => void;
  /** 按住说话：按下开始 */
  startRecording: () => void;
  /** 按住说话：松手提交，返回是否真的在录 */
  stopRecording: () => boolean;
  /** 按住说话：上滑取消，丢弃这段录音 */
  cancelRecording: () => boolean;
  /** 语音气泡重播 */
  replayTurn: (turnId: number) => Promise<void>;
  /** 正在重播的轮次（用于气泡高亮） */
  replayingTurn: number | null;
  /** 每轮音频时长（秒），键为 `personaId:turnId` */
  turnDurations: Record<string, number>;
  requestSuggestion: (text?: string) => void;
  clearNotices: () => void;
  dismissNotice: (id: string) => void;
  unlockAudio: () => Promise<void>;
  mic: MicCapture | null;
  player: AudioPlayer | null;
}

/* ------------------------------------------------------------------ Hook */

export function useConversation(fallbackPersonas: PersonaPublic[], fallbackModes: ConversationMode[]): ConversationApi {
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [statusDetail, setStatusDetail] = useState("尚未连接");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [engines, setEngines] = useState<EnginesInfo>({});
  const [serverPersonas, setServerPersonas] = useState<PersonaPublic[]>([]);
  const [serverModes, setServerModes] = useState<ConversationMode[]>([]);
  const [activePersonaId, setActivePersonaId] = useState<string>(fallbackPersonas[0]?.id ?? "oliver");
  const [activeMode, setActiveMode] = useState<string>(fallbackModes[0]?.id ?? "free");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [suggestions, setSuggestions] = useState<SuggestionCard[]>([]);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [metrics, setMetrics] = useState<TurnMetrics>({});
  const [serverSampleRate, setServerSampleRate] = useState<number>(24000);
  const [vadSpeaking, setVadSpeaking] = useState(false);
  const [recording, setRecording] = useState(false);
  const [micAvailable, setMicAvailable] = useState<boolean>(false);
  //: 用户显式选择的输入设备（空 = 系统默认）。存 localStorage 跨会话记住。
  const [micDeviceId, setMicDeviceIdState] = useState<string>(() => {
    if (typeof window === 'undefined') return '';
    try { return window.localStorage.getItem('speakeasy.micDeviceId') || ''; } catch { return ''; }
  });
  const micDeviceIdRef = useRef(micDeviceId);
  /** 每个角色的独立消息线程：personaId -> messages */
  const threadsRef = useRef<Record<string, ChatMessage[]>>({});
  /** 当前线程所属角色（音频缓存键要用） */
  const threadPersonaRef = useRef<string>("");
  /** messages 的即时镜像（切换线程时要读最新值） */
  const messagesRef = useRef<ChatMessage[]>([]);
  /** 按角色缓存 PCM（重播用）：`personaId:turnId` -> chunks */
  const audioCacheRef = useRef<Map<string, Array<{ seq: number; sentenceIndex: number; data: string; sampleRate: number }>>>(new Map());
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);
  // 音频缓存键以角色为前缀；角色一变就同步，避免键与 UI 取键不一致
  useEffect(() => {
    if (activePersonaId) threadPersonaRef.current = activePersonaId;
  }, [activePersonaId]);
  /** 每一轮的音频总时长（秒），用于语音气泡显示 */
  const [turnDurations, setTurnDurations] = useState<Record<string, number>>({});
  /** 正在重播的轮次 */
  const [replayingTurn, setReplayingTurn] = useState<number | null>(null);
  const replayPlayerRef = useRef<AudioPlayer | null>(null);
  useEffect(() => { micDeviceIdRef.current = micDeviceId; }, [micDeviceId]);

  /** 换输入设备：停掉当前 mic，下次录音用新设备。 */
  const setMicDeviceId = useCallback((id: string) => {
    setMicDeviceIdState(id);
    micDeviceIdRef.current = id;
    try {
      if (id) window.localStorage.setItem('speakeasy.micDeviceId', id);
      else window.localStorage.removeItem('speakeasy.micDeviceId');
    } catch { /* 隐私模式忽略 */ }
    // 释放旧设备，下次按新设备重建
    const old = micRef.current;
    if (old) {
      try { void old.stop(); } catch { /* 忽略 */ }
      micRef.current = null;
      setMicInstance(null);
    }
    setMicError(null);
  }, []);
  const [micError, setMicError] = useState<string | null>(null);
  const [playerStats, setPlayerStats] = useState<PlayerStats>({
    playing: false,
    droppedFrames: 0,
    gapFrames: 0,
    bufferedSeconds: 0,
  });
  const [activeSentence, setActiveSentence] = useState<{ turnId: number; sentenceIndex: number } | null>(null);
  const [wsOpen, setWsOpen] = useState(false);
  /** 供波形组件读取 AnalyserNode 的稳定实例引用（实例只在解锁音频后出现） */
  const [micInstance, setMicInstance] = useState<MicCapture | null>(null);
  const [playerInstance, setPlayerInstance] = useState<AudioPlayer | null>(null);

  /* ---------- refs：热路径状态，不进 React 渲染循环 ---------- */
  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MicCapture | null>(null);
  const playerRef = useRef<AudioPlayer | null>(null);

  const personaRef = useRef(activePersonaId);
  const modeRef = useRef(activeMode);

  const reconnectAttempts = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pingTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const manualClose = useRef(false);
  const pendingPingAt = useRef<number | null>(null);

  /** 当前服务端回合 id（-1 表示无） */
  const currentTurnRef = useRef(-1);
  /** 乐观回显的临时 turn id（负数，避免与服务端冲突） */
  const optimisticTurnRef = useRef(0);
  /** barge-in 连续高能量帧计数与冷却 */
  const bargeFrames = useRef(0);
  const bargeCooldownUntil = useRef(0);

  useEffect(() => {
    personaRef.current = activePersonaId;
  }, [activePersonaId]);

  useEffect(() => {
    modeRef.current = activeMode;
  }, [activeMode]);

  const personas = serverPersonas.length > 0 ? serverPersonas : fallbackPersonas;
  const modes = serverModes.length > 0 ? serverModes : fallbackModes;

  const pushNotice = useCallback((level: Notice["level"], text: string) => {
    setNotices((prev) => {
      // 相同文本去重（最多保留 6 条）
      if (prev.some((n) => n.text === text && n.level === level)) return prev;
      const next = [...prev, makeNotice(level, text)];
      return next.slice(-6);
    });
  }, []);

  /* ------------------------------------------------------------------ 消息发送 */

  const send = useCallback((msg: ClientMessage) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(msg));
    return true;
  }, []);

  const sendBinary = useCallback((buffer: ArrayBuffer) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(buffer);
    return true;
  }, []);

  /* ------------------------------------------------------------------ 对话状态更新 */

  const upsertAssistantDelta = useCallback((turnId: number, delta: string) => {
    setMessages((prev) => {
      const idx = prev.findIndex((m) => m.role === "assistant" && m.turnId === turnId);
      if (idx === -1) {
        const created: ChatMessage = {
          id: `assistant-${turnId}`,
          role: "assistant",
          turnId,
          text: delta,
          streaming: true,
          createdAt: Date.now(),
        };
        return [...prev, created];
      }
      const next = prev.slice();
      next[idx] = { ...next[idx], text: next[idx].text + delta, streaming: true };
      return next;
    });
  }, []);

  const attachSentence = useCallback((turnId: number, sentenceIndex: number, text: string) => {
    setMessages((prev) => {
      const idx = prev.findIndex((m) => m.role === "assistant" && m.turnId === turnId);
      if (idx === -1) {
        const created: ChatMessage = {
          id: `assistant-${turnId}`,
          role: "assistant",
          turnId,
          text,
          streaming: true,
          sentences: [{ index: sentenceIndex, text }],
          createdAt: Date.now(),
        };
        return [...prev, created];
      }
      const next = prev.slice();
      const msg = next[idx];
      const existing = msg.sentences ?? [];
      if (existing.some((s) => s.index === sentenceIndex)) return prev;
      const merged: SentenceRef[] = [...existing, { index: sentenceIndex, text }].sort(
        (a, b) => a.index - b.index,
      );
      next[idx] = { ...msg, sentences: merged };
      return next;
    });
  }, []);

  const appendUserMessage = useCallback((turnId: number, text: string, truncated?: boolean) => {
    setMessages((prev) => {
      // 优先认领乐观回显（同文本、pending、turnId 为负）
      const optimisticIdx = prev.findIndex(
        (m) => m.role === "user" && m.pending && m.text.trim() === text.trim(),
      );
      if (optimisticIdx !== -1) {
        const next = prev.slice();
        const claimed = next[optimisticIdx];
        next[optimisticIdx] = {
          ...claimed,
          turnId,
          pending: false,
          truncated,
          createdAt: claimed.createdAt,
        };
        return next;
      }
      // 服务端没有文本（例如空识别）时也插入占位
      const created: ChatMessage = {
        id: `user-${turnId}`,
        role: "user",
        turnId,
        text,
        streaming: false,
        truncated,
        createdAt: Date.now(),
      };
      return [...prev, created];
    });
  }, []);

  /* ------------------------------------------------------------------ 播放器 ---------- */

  const getPlayer = useCallback((): AudioPlayer => {
    if (playerRef.current) return playerRef.current;
    const player = new AudioPlayer({
      onSentenceChange: (turnId, sentenceIndex) => {
        setActiveSentence({ turnId, sentenceIndex });
      },
      onDrain: () => {
        setActiveSentence(null);
      },
      onStats: (stats) => setPlayerStats(stats),
    });
    playerRef.current = player;
    setPlayerInstance(player);
    return player;
  }, []);

  /* ------------------------------------------------------------------ 消息分发 ---------- */

  const handleServerMessage = useCallback(
    (raw: unknown) => {
      if (!isRecord(raw)) return;
      const type = asString(raw.type);
      if (!type) return;

      switch (type) {
        case "ready": {
          setStatus("ready");
          setStatusDetail("已连接");
          reconnectAttempts.current = 0;
          setSessionId(asString(raw.session_id) ?? null);

          const personaList = normalizePersonas(raw.personas);
          if (personaList.length > 0) setServerPersonas(personaList);

          const modeList = normalizeModes(raw.modes);
          if (modeList.length > 0) setServerModes(modeList);

          setEngines(normalizeEngines(raw.engines));

          const audio = isRecord(raw.audio) ? raw.audio : null;
          const rate = audio ? asNumber(audio.sample_rate) : undefined;
          if (rate && rate > 0) setServerSampleRate(rate);

          const pid = asString(raw.persona_id);
          if (pid && pid !== personaRef.current) {
            // 服务端起手用了默认角色：以后端为准并同步到本地状态
            setActivePersonaId(pid);
            personaRef.current = pid;
          }

          const protocolVersion = asNumber(raw.protocol_version);
          if (protocolVersion !== undefined && protocolVersion !== PROTOCOL_VERSION) {
            pushNotice("warn", `协议版本不一致（服务端 ${protocolVersion}，前端 ${PROTOCOL_VERSION}）`);
          }

          const llmConfigured = isRecord(raw.engines)
            ? isRecord(raw.engines.llm)
              ? raw.engines.llm.configured
              : undefined
            : undefined;
          if (llmConfigured === false) {
            pushNotice("warn", "后端未配置 LLM_API_KEY：回复会失败，文字输入仍可测试 STT/TTS。");
          }
          return;
        }

        case "vad": {
          const state = asString(raw.state);
          const speaking = state === "speech_start";
          setVadSpeaking(speaking);
          if (speaking) {
            // 服务端 VAD 也参与 barge-in：正在播就停
            const player = playerRef.current;
            if (player?.playing) {
              player.flush("interrupt");
              setActiveSentence(null);
            }
          }
          return;
        }

        case "user_text": {
          const turnId = asNumber(raw.turn_id) ?? -1;
          const text = asString(raw.text) ?? "";
          const truncated = raw.truncated === true;
          if (turnId >= 0) currentTurnRef.current = turnId;
          appendUserMessage(turnId, text, truncated);

          const latency = isRecord(raw.latency) ? raw.latency : null;
          const stt = latency ? asNumber(latency.stt_ms) : undefined;

          if (turnId >= 0) {
            const player = playerRef.current;
            player?.beginTurn(turnId);
          }
          setMetrics((prev) => ({ ...prev, stt_ms: stt ?? prev.stt_ms }));
          return;
        }

        case "assistant_delta": {
          const turnId = asNumber(raw.turn_id) ?? -1;
          const text = asString(raw.text) ?? "";
          if (turnId < 0 || !text) return;
          currentTurnRef.current = turnId;
          upsertAssistantDelta(turnId, text);
          return;
        }

        case "assistant_sentence": {
          const turnId = asNumber(raw.turn_id) ?? -1;
          const sentenceIndex = asNumber(raw.sentence_index) ?? 0;
          const text = asString(raw.text) ?? "";
          if (turnId < 0) return;
          attachSentence(turnId, sentenceIndex, text);
          return;
        }

        case "history": {
          // 服务端回放该角色的历史对话（刷新 / 切角色时）
          const pid = asString(raw.persona_id) ?? personaRef.current;
          const list = Array.isArray(raw.messages) ? (raw.messages as Array<Record<string, unknown>>) : [];
          const mapped: ChatMessage[] = list.map((m, i) => {
            const ts = typeof m.ts === "number" ? m.ts * 1000 : Date.now();
            return {
              id: `h-${pid}-${i}`,
              role: m.role === "assistant" ? "assistant" : "user",
              turnId: 0,
              text: String(m.text ?? ""),
              streaming: false,
              createdAt: ts,
              ...(typeof m.image === "string" && m.image ? { image: m.image } : {}),
            };
          });
          threadsRef.current[pid] = mapped;
          if (pid === personaRef.current) setMessages(mapped);
          return;
        }

        case "audio": {
          // 按轮缓存 PCM，供语音气泡重播；上限 30 轮，避免长会话吃内存
          {
            const cid = asNumber(raw.turn_id);
            const cdata = asString(raw.data);
            const csr = asNumber(raw.sample_rate) ?? serverSampleRate;
            const cseq = asNumber(raw.seq) ?? 0;
            const csi = asNumber(raw.sentence_index) ?? 0;
            if (cid !== undefined && cdata) {
              const ckey = `${threadPersonaRef.current || "_"}:${cid}`;
              const bucket = audioCacheRef.current.get(ckey) ?? [];
              bucket.push({ seq: cseq, sentenceIndex: csi, data: cdata, sampleRate: csr });
              audioCacheRef.current.set(ckey, bucket);
              try {
                const bytes = Math.floor((cdata.length * 3) / 4);
                const dur = bytes / 2 / csr;
                setTurnDurations((m) => ({ ...m, [ckey]: (m[ckey] ?? 0) + dur }));
              } catch {
                /* 忽略 */
              }
              if (audioCacheRef.current.size > 30) {
                const first = audioCacheRef.current.keys().next().value;
                if (first !== undefined) audioCacheRef.current.delete(first);
              }
            }
          }
          const turnId = asNumber(raw.turn_id);
          const seq = asNumber(raw.seq);
          const sentenceIndex = asNumber(raw.sentence_index) ?? 0;
          const data = asString(raw.data);
          const sampleRate = asNumber(raw.sample_rate) ?? serverSampleRate;
          if (turnId === undefined || seq === undefined || !data) return;

          const player = getPlayer();
          void player.pushAudio({ turnId, sentenceIndex, seq, data, sampleRate });
          return;
        }

        case "turn_audio_end": {
          const turnId = asNumber(raw.turn_id);
          if (turnId !== undefined) playerRef.current?.endTurnAudio(turnId);
          return;
        }

        case "assistant_done": {
          const turnId = asNumber(raw.turn_id) ?? -1;
          const text = asString(raw.text) ?? "";
          const m = isRecord(raw.metrics) ? raw.metrics : null;
          if (turnId < 0) return;

          setMessages((prev) => {
            const idx = prev.findIndex((msg) => msg.role === "assistant" && msg.turnId === turnId);
            if (idx === -1) {
              if (!text) return prev;
              return [
                ...prev,
                {
                  id: `assistant-${turnId}`,
                  role: "assistant",
                  turnId,
                  text,
                  streaming: false,
                  createdAt: Date.now(),
                },
              ];
            }
            const next = prev.slice();
            next[idx] = {
              ...next[idx],
              // 服务端全文是权威：流式增量可能因丢包不完整
              text: text || next[idx].text,
              streaming: false,
            };
            return next;
          });

          if (m) {
            const parsed: TurnMetrics = {
              stt_ms: asNumber(m.stt_ms),
              llm_ttft_ms: asNumber(m.llm_ttft_ms),
              first_audio_ms: asNumber(m.first_audio_ms),
              total_ms: asNumber(m.total_ms),
              tts_first_sentence_ms: asNumber(m.tts_first_sentence_ms),
              sentences: asNumber(m.sentences),
              audio_bytes: asNumber(m.audio_bytes),
            };
            setMetrics((prev) => ({ ...prev, ...parsed }));
            setMessages((prev) => {
              const idx = prev.findIndex((msg) => msg.role === "assistant" && msg.turnId === turnId);
              if (idx === -1) return prev;
              const next = prev.slice();
              next[idx] = { ...next[idx], metrics: parsed };
              return next;
            });
          }
          return;
        }

        case "suggestion": {
          const turnId = asNumber(raw.turn_id) ?? currentTurnRef.current;
          const items = Array.isArray(raw.items) ? raw.items : [];
          const parsed: SuggestionCard[] = [];
          for (const item of items) {
            if (!isRecord(item)) continue;
            const original = asString(item.original);
            const better = asString(item.better);
            if (!original || !better) continue;
            parsed.push(
              makeCard(turnId, {
                id: "",
                turnId,
                original,
                better,
                why: asString(item.why) ?? "",
                register: asString(item.register) ?? "neutral",
                receivedAt: 0,
              }),
            );
          }
          if (parsed.length === 0) return;
          setSuggestions((prev) => {
            // 同一 original+better 不重复插入
            const filtered = parsed.filter(
              (card) => !prev.some((p) => p.original === card.original && p.better === card.better),
            );
            return [...prev, ...filtered];
          });
          return;
        }

        case "error": {
          const code = asString(raw.code) ?? "internal";
          const message = asString(raw.message) ?? "未知错误";
          const fatal = raw.fatal === true;
          const level: Notice["level"] = fatal ? "error" : "warn";
          pushNotice(level, `${mapErrorCode(code)}：${message}`);
          if (fatal) {
            setStatus("error");
            setStatusDetail(message);
          }
          return;
        }

        case "pong": {
          const sentAt = pendingPingAt.current;
          if (sentAt !== null) {
            const rtt = Math.max(0, Math.round(performance.now() - sentAt));
            pendingPingAt.current = null;
            setStatusDetail(`已连接 · RTT ${rtt} ms`);
          }
          return;
        }

        case "mode_changed": {
          const mode = asString(raw.mode);
          if (mode) {
            setActiveMode(mode);
            modeRef.current = mode;
          }
          return;
        }

        case "session_info": {
          // 调试消息：仅记录回合数，不参与 UI 主流程
          const turns = asNumber(raw.turns);
          if (turns !== undefined) setStatusDetail((prev) => (prev.includes("回合") ? prev : `${prev} · ${turns} 回合`));
          return;
        }

        default:
          // 未知消息类型：忽略（协议允许服务端扩展）
          return;
      }
    },
    [
      appendUserMessage,
      attachSentence,
      getPlayer,
      pushNotice,
      serverSampleRate,
      upsertAssistantDelta,
    ],
  );

  /* ------------------------------------------------------------------ 连接 / 重连 ---------- */

  const startPing = useCallback(() => {
    if (pingTimer.current) clearInterval(pingTimer.current);
    pingTimer.current = setInterval(() => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return;
      pendingPingAt.current = performance.now();
      send({ type: "ping", t_ms: Date.now() });
    }, PING_INTERVAL_MS);
  }, [send]);

  const stopPing = useCallback(() => {
    if (pingTimer.current) {
      clearInterval(pingTimer.current);
      pingTimer.current = null;
    }
    pendingPingAt.current = null;
  }, []);

  const openSocket = useCallback(() => {
    if (typeof window === "undefined") return;

    const existing = wsRef.current;
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const url = resolveWsUrl();
    if (!url) return;

    setStatus("connecting");
    setStatusDetail("正在连接后端…");

    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      setStatus("error");
      setStatusDetail(`无法创建 WebSocket：${errorMessageOf(err)}`);
      return;
    }
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      setWsOpen(true);
      reconnectAttempts.current = 0;
      setStatusDetail("握手完成，等待 ready…");
      send({
        type: "hello",
        persona_id: personaRef.current,
        input_sample_rate: IN_SAMPLE_RATE,
        client: CLIENT_NAME,
        protocol_version: PROTOCOL_VERSION,
      });
      // 同步当前模式（服务端可能不支持，回 bad_message 时我们只提示一次）
      send({ type: "set_mode", mode: modeRef.current });
      startPing();
    };

    ws.onmessage = (event: MessageEvent) => {
      if (typeof event.data !== "string") return; // 下行不期望二进制
      let parsed: unknown;
      try {
        parsed = JSON.parse(event.data);
      } catch {
        return;
      }
      handleServerMessage(parsed);
    };

    ws.onerror = () => {
      // onclose 会随后触发，统一在那里处理重连
      setStatusDetail("连接出现错误");
    };

    ws.onclose = () => {
      setWsOpen(false);
      stopPing();
      wsRef.current = null;
      playerRef.current?.flush("interrupt");

      if (manualClose.current) {
        setStatus("disconnected");
        setStatusDetail("已手动断开");
        return;
      }

      if (reconnectAttempts.current >= MAX_RECONNECT_ATTEMPTS) {
        setStatus("error");
        setStatusDetail("后端未连接（已达最大重试次数）");
        return;
      }

      reconnectAttempts.current += 1;
      const delay = RECONNECT_BASE_MS * 2 ** (reconnectAttempts.current - 1);
      setStatus("connecting");
      setStatusDetail(`连接断开，${(delay / 1000).toFixed(1)}s 后第 ${reconnectAttempts.current} 次重连…`);
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      reconnectTimer.current = setTimeout(() => openSocket(), delay);
    };
  }, [handleServerMessage, send, startPing, stopPing]);

  const connect = useCallback(() => {
    manualClose.current = false;
    reconnectAttempts.current = 0;
    openSocket();
  }, [openSocket]);

  const disconnect = useCallback(() => {
    manualClose.current = true;
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    stopPing();
    wsRef.current?.close();
    wsRef.current = null;
    setWsOpen(false);
    setStatus("disconnected");
    setStatusDetail("已手动断开");
  }, [stopPing]);

  /* ------------------------------------------------------------------ 录音 ---------- */

  const handleInterrupt = useCallback(() => {
    playerRef.current?.flush("interrupt");
    setActiveSentence(null);
    send({ type: "interrupt" });
  }, [send]);

  const ensureMic = useCallback(async (): Promise<MicCapture | null> => {
    if (micRef.current) return micRef.current;

    // 预检：把"环境必然不允许"的情况提前说清楚，而不是抛笼统的 Permission denied
    const envProblem = micPreflight();
    if (envProblem) {
      setMicAvailable(false);
      setMicError(envProblem);
      pushNotice("warn", `无法打开麦克风：${envProblem}`);
      return null;
    }

    const player = getPlayer();
    const mic = new MicCapture({
      deviceId: micDeviceIdRef.current || undefined,
      onFrame: (frame, rms) => {
        // 1) 上行发送
        sendBinary(frame);

        // 2) 本地能量 barge-in：正在播放且用户开口
        const now = performance.now();
        if (player.playing && now > bargeCooldownUntil.current) {
          if (rms > BARGE_IN_RMS) {
            bargeFrames.current += 1;
            if (bargeFrames.current >= BARGE_IN_FRAMES) {
              bargeFrames.current = 0;
              bargeCooldownUntil.current = now + BARGE_IN_COOLDOWN_MS;
              player.flush("interrupt");
              setActiveSentence(null);
              send({ type: "interrupt" });
              pushNotice("info", "检测到你开口，已打断朗读");
            }
          } else {
            bargeFrames.current = 0;
          }
        } else if (!player.playing) {
          bargeFrames.current = 0;
        }
      },
      onError: (message) => {
        setMicError(message);
        pushNotice("error", message);
      },
    });

    try {
      await mic.start();
      micRef.current = mic;
      setMicInstance(mic);
      setMicAvailable(true);
      setMicError(null);
      return mic;
    } catch (err) {
      const message = micErrorMessage(err);
      setMicAvailable(false);
      setMicError(message);
      pushNotice("warn", `无法打开麦克风：${message}`);
      return null;
    }
  }, [getPlayer, pushNotice, send, sendBinary]);

  /** 停止录音并提交（按住说话的"松手"） */
  const stopRecording = useCallback((): boolean => {
    const mic = micRef.current;
    if (mic?.running) {
      mic.stop();
      setRecording(false);
      send({ type: "audio_end" });
      return true;
    }
    return false;
  }, [send]);

  /** 开始录音（按住说话的"按下"） */
  const startRecording = useCallback(() => {
    const mic = micRef.current;
    if (mic?.running) return;
    // 按住模式：告诉服务端"先别提交，只累积"
    send({ type: "ptt_hold" });
    void (async () => {
      const player = getPlayer();
      // 播放 ctx 需要用户手势解锁
      try {
        await player.unlock();
      } catch {
        /* 首次点击即手势，失败也不阻塞录音 */
      }

      const ready = await ensureMic();
      if (!ready) return;

      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        pushNotice("warn", "尚未连接后端，录音不会上传。请先点击顶栏的连接按钮。");
      }

      // 每次开口前重置播放排程，避免上一轮的残留
      player.flush("interrupt");
      setActiveSentence(null);

      bargeFrames.current = 0;
      bargeCooldownUntil.current = 0;
      try {
        await ready.start();
        setRecording(true);
      } catch (err) {
        pushNotice("error", `启动录音失败：${errorMessageOf(err)}`);
      }
    })();
  }, [ensureMic, getPlayer, pushNotice, send]);

  /** 取消录音（上滑取消）：停麦 + 丢弃缓冲，不产生任何一轮 */
  const cancelRecording = useCallback((): boolean => {
    const mic = micRef.current;
    const wasRunning = !!mic?.running;
    if (mic?.running) {
      mic.stop();
      setRecording(false);
    }
    // 关键：不发 audio_end（那会提交），改发 ptt_cancel 让服务端丢弃
    send({ type: "ptt_cancel" });
    return wasRunning;
  }, [send]);

  /** 清空当前角色的对话记录（内存 + 磁盘），并让 LLM 忘掉 */
  const clearConversation = useCallback(() => {
    const pid = personaRef.current;
    if (!pid) return;

    // 本地状态
    threadsRef.current[pid] = [];
    setMessages([]);
    // 该角色的语音缓存 / 时长
    for (const k of Array.from(audioCacheRef.current.keys())) {
      if (k.startsWith(`${pid}:`)) audioCacheRef.current.delete(k);
    }
    setTurnDurations((m) => {
      const next: Record<string, number> = {};
      for (const [k, v] of Object.entries(m)) {
        if (!k.startsWith(`${pid}:`)) next[k] = v;
      }
      return next;
    });
    playerRef.current?.flush("interrupt");
    setActiveSentence(null);

    if (!send({ type: "clear_history" })) {
      pushNotice("info", "已清空本地记录（未连接后端，重启后仍会回放旧记录）");
    }
  }, [pushNotice, send, setMessages]);

  const toggleRecording = useCallback(() => {
    if (recording) stopRecording();
    else startRecording();
  }, [recording, startRecording, stopRecording]);

  /** 重播某一轮的语音（语音气泡点击） */
  const replayTurn = useCallback(async (turnId: number) => {
    const ckey = `${threadPersonaRef.current || "_"}:${turnId}`;
    const chunks = audioCacheRef.current.get(ckey);
    if (!chunks || chunks.length === 0) return;
    try {
      if (replayPlayerRef.current) {
        await replayPlayerRef.current.dispose().catch(() => undefined);
      }
      const p = new AudioPlayer({});
      replayPlayerRef.current = p;
      setReplayingTurn(turnId);
      await p.unlock();
      p.beginTurn(turnId);
      const ordered = [...chunks].sort((a, b) => a.seq - b.seq);
      for (const c of ordered) {
        void p.pushAudio({
          turnId,
          sentenceIndex: c.sentenceIndex,
          seq: c.seq,
          data: c.data,
          sampleRate: c.sampleRate,
        });
      }
      p.endTurnAudio(turnId);
      // 播完清理
      const total = turnDurations[`${threadPersonaRef.current || "_"}:${turnId}`] ?? 1;
      window.setTimeout(() => {
        setReplayingTurn((cur) => (cur === turnId ? null : cur));
      }, Math.max(600, total * 1000 + 300));
    } catch {
      setReplayingTurn(null);
    }
  }, [turnDurations]);

  /* ------------------------------------------------------------------ 对外动作 ---------- */

  const sendText = useCallback(
    (text: string, images?: string[]) => {
      const trimmed = text.trim();
      if (!trimmed && !(images && images.length > 0)) return;

      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        pushNotice("warn", "尚未连接后端，消息未发送。请先连接。");
        return;
      }

      optimisticTurnRef.current -= 1;
      const tempTurn = optimisticTurnRef.current;
      setMessages((prev) => [
        ...prev,
        {
          id: `user-pending-${Math.abs(tempTurn)}`,
          role: "user",
          turnId: tempTurn,
          text: trimmed,
          streaming: false,
          pending: true,
          createdAt: Date.now(),
        },
      ]);

      // 打断正在播的内容：文字提交意味着"我现在这句"
      if (playerRef.current?.playing) {
        playerRef.current.flush("interrupt");
        setActiveSentence(null);
        send({ type: "interrupt" });
      }

      send(images && images.length > 0 ? { type: "text", text: trimmed, images } : { type: "text", text: trimmed });
    },
    [pushNotice, send],
  );

  const selectPersona = useCallback(
    (id: string) => {
      if (!id || id === personaRef.current) return;

      // ---- 换对话框：先存当前线程，再载入目标角色的线程 ----
      const prev = personaRef.current;
      if (prev) threadsRef.current[prev] = messagesRef.current;

      // A 的声音和在途回复都不许留到 B 的对话里
      send({ type: "interrupt" });
      playerRef.current?.flush("interrupt");
      setActiveSentence(null);

      threadPersonaRef.current = id;
      setMessages(threadsRef.current[id] ?? []);

      setActivePersonaId(id);
      personaRef.current = id;
      const ok = send({ type: "set_persona", persona_id: id });
      if (!ok) pushNotice("info", "已切换角色（未连接后端，将在下次连接时生效）");
    },
    [pushNotice, send, setMessages],
  );

  const selectMode = useCallback(
    (id: string) => {
      if (!id || id === modeRef.current) return;
      setActiveMode(id);
      modeRef.current = id;
      const ok = send({ type: "set_mode", mode: id });
      if (!ok) pushNotice("info", "已切换对话模式（未连接后端，将在下次连接时生效）");
    },
    [pushNotice, send],
  );

  const requestSuggestion = useCallback(
    (text?: string) => {
      const payload: ClientMessage = text ? { type: "suggestion", text } : { type: "suggestion" };
      if (!send(payload)) pushNotice("warn", "尚未连接后端，无法请求润色建议");
    },
    [pushNotice, send],
  );

  const unlockAudio = useCallback(async () => {
    try {
      await getPlayer().unlock();
    } catch {
      pushNotice("warn", "音频输出未能解锁，请再点击一次页面");
    }
  }, [getPlayer, pushNotice]);

  const clearNotices = useCallback(() => setNotices([]), []);
  const dismissNotice = useCallback((id: string) => {
    setNotices((prev) => prev.filter((n) => n.id !== id));
  }, []);

  /* ------------------------------------------------------------------ 生命周期 ---------- */

  // 挂载即尝试连接；卸载时彻底清理
  useEffect(() => {
    openSocket();
    return () => {
      manualClose.current = true;
      stopPing();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
      wsRef.current = null;
      void micRef.current?.dispose();
      micRef.current = null;
      void playerRef.current?.dispose();
      playerRef.current = null;
    };
    // 仅首次挂载：openSocket 内部已用 ref 保证幂等
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pending = useMemo(
    () => messages.some((m) => m.role === "assistant" && m.streaming),
    [messages],
  );

  return {
    status,
    statusDetail,
    sessionId,
    engines,
    personas,
    modes,
    activePersonaId,
    activeMode,
    messages,
    suggestions,
    notices,
    metrics,
    serverSampleRate,
    vadSpeaking,
    recording,
    micAvailable,
    micError,
    micDeviceId,
    setMicDeviceId,
    playerStats,
    activeSentence,
    pending,
    websocketOpen: wsOpen,

    connect,
    disconnect,
    sendText,
    selectPersona,
    selectMode,
    interrupt: handleInterrupt,
    toggleRecording,
    clearConversation,
    startRecording,
    stopRecording,
    cancelRecording,
    replayTurn,
    replayingTurn,
    turnDurations,
    requestSuggestion,
    clearNotices,
    dismissNotice,
    unlockAudio,
    mic: micInstance,
    player: playerInstance,
  };
}

function mapErrorCode(code: string): string {
  switch (code) {
    case "bad_message":
      return "消息格式错误";
    case "unsupported_sample_rate":
      return "上行采样率不被支持";
    case "llm_not_configured":
      return "未配置 LLM";
    case "llm_failed":
      return "LLM 调用失败";
    case "stt_failed":
      return "语音识别失败";
    case "tts_failed":
      return "语音合成失败";
    case "vad_failed":
      return "VAD 失败";
    case "persona_not_found":
      return "角色不存在";
    case "internal":
      return "服务端内部错误";
    default:
      return code;
  }
}
