"use client";

/**
 * 语音通话（免提 VAD + 流式回复 + 占位 filler）。
 *
 * 一轮对话：
 * 1) VAD 自动断句 → 立即播放 warmup 预生成的 filler（「嗯嗯，我在听」）；
 * 2) 同时 POST /call/turn（NDJSON 流）：transcript → 更新我的字幕；sentence 逐句入队播放、
 *    好友字幕渐进增长；done → 更新轮数并在队列播完后恢复监听；
 * 3) filler 播完仍无首句时显示「对方正在思考…」；
 * 4) 流式失败/浏览器不支持 → 回退 api.sendVoice（toast 一次「已切换普通模式」）；
 * 5) 结束/卸载：abort 流、停止音频、清队列、回收资源。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Mic, MicOff, PhoneOff, RefreshCw, Volume2, VolumeX } from "lucide-react";

import { Avatar } from "@/components/v2/Avatar";
import { HoldToTalk } from "@/components/v2/HoldToTalk";
import { useToast } from "@/components/v2/Toast";
import { api, apiErrorText, callWarmup, streamCallTurn, type CallTurnEvent } from "@/lib/v2/api";
import { localStateOf, replyLanguageMeta } from "@/lib/v2/format";
import type { Friend, Message } from "@/lib/v2/types";
import { useHandsFreeVad } from "@/lib/v2/useHandsFreeVad";
import type { RecordingResult } from "@/lib/v2/useRecorder";

interface CallViewProps {
  friend: Friend;
  /** 关闭通话（ChatView 负责 loadMessages 刷新 call_summary） */
  onClose: () => void;
}

type CallPhase = "connecting" | "listening" | "recording" | "processing" | "thinking" | "speaking" | "muted";

const STATUS_TEXT: Record<CallPhase, string> = {
  connecting: "正在接通…",
  listening: "我在听…",
  recording: "正在聆听…",
  processing: "对方正在思考…",
  thinking: "对方正在思考…",
  speaking: "对方正在说话…",
  muted: "已静音",
};

const ORB_HINT: Record<CallPhase, string> = {
  connecting: "正在接通对方…",
  listening: "直接说话即可，我在听",
  recording: "说完停顿一下，我会自动接话",
  processing: "对方正在组织语言…",
  thinking: "对方正在组织语言…",
  speaking: "对方正在说话，稍后会自动继续听",
  muted: "已静音，点麦克风或静音键恢复",
};

interface QueuedSentence {
  url: string;
  index: number;
}

function clockText(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function truncateText(text: string, max = 58): string {
  const value = (text || "").trim();
  if (value.length <= max) return value;
  return `${value.slice(0, max)}…`;
}

export function CallView({ friend, onClose }: CallViewProps) {
  const { showToast } = useToast();
  const [active, setActive] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [turns, setTurns] = useState(0);
  const [waiting, setWaiting] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [ending, setEnding] = useState(false);
  const [micBlocked, setMicBlocked] = useState(false);
  const [muted, setMuted] = useState(false);
  const [speakerOn, setSpeakerOn] = useState(true);
  const [lastUserText, setLastUserText] = useState("");
  const [lastFriendText, setLastFriendText] = useState("");
  const [vadUnsupported, setVadUnsupported] = useState(false);

  const startedAtRef = useRef(Date.now());
  const turnsRef = useRef(0);
  const endingRef = useRef(false);
  const mountedRef = useRef(true);
  const busyRef = useRef(false);

  /* -------- 播放与流式状态 -------- */
  const fillerUrlRef = useRef("");
  const preloadedFillerRef = useRef<HTMLAudioElement | null>(null);
  const fillerAudioRef = useRef<HTMLAudioElement | null>(null);
  const sentenceAudioRef = useRef<HTMLAudioElement | null>(null);
  const queueRef = useRef<QueuedSentence[]>([]);
  const fillerPlayingRef = useRef(false);
  const sentencePlayingRef = useRef(false);
  const doneReceivedRef = useRef(false);
  const doneInfoRef = useRef<{ total_turns: number; friend_message: Message | null } | null>(null);
  const turnActiveRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const hasSentenceRef = useRef(false);
  const firstAudioStartedRef = useRef(false);
  const fallbackToastShownRef = useRef(false);
  const lastFriendTextRef = useRef("");
  lastFriendTextRef.current = lastFriendText;
  const lastReplyUrlRef = useRef("");

  /* -------- 每轮计时（验收用：window.__callTimings） -------- */
  const turnStartedAtRef = useRef(0);
  const timingsRef = useRef<Record<string, number>>({});
  const markTiming = useCallback((name: string) => {
    if (!turnStartedAtRef.current) return;
    timingsRef.current[name] = Math.round(performance.now() - turnStartedAtRef.current);
    if (typeof window !== "undefined") {
      (window as unknown as { __callTimings?: Record<string, number> }).__callTimings = {
        ...timingsRef.current,
      };
    }
  }, []);

  const local = localStateOf(friend);
  const langMeta = replyLanguageMeta(friend);

  /* ------------------------------ 预加载 filler ------------------------------ */

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const result = await callWarmup(friend.id);
        if (cancelled || !result.filler_url) return;
        fillerUrlRef.current = result.filler_url;
        const audio = new Audio(result.filler_url);
        audio.preload = "auto";
        audio.load();
        preloadedFillerRef.current = audio;
      } catch {
        /* 失败静默：没有 filler 直接进「思考」状态 */
      }
    })();
    return () => {
      cancelled = true;
      const preloaded = preloadedFillerRef.current;
      if (preloaded) {
        preloaded.onended = null;
        preloaded.onerror = null;
        preloaded.pause();
      }
      preloadedFillerRef.current = null;
    };
  }, [friend.id]);

  /* ------------------------------ 音频清理 / 播放队列 ------------------------------ */

  const stopAllAudio = useCallback(() => {
    const filler = fillerAudioRef.current;
    if (filler) {
      filler.onended = null;
      filler.onerror = null;
      filler.pause();
      filler.src = "";
    }
    fillerAudioRef.current = null;
    fillerPlayingRef.current = false;

    const sentence = sentenceAudioRef.current;
    if (sentence) {
      sentence.onended = null;
      sentence.onerror = null;
      sentence.pause();
      sentence.src = "";
    }
    sentenceAudioRef.current = null;
    sentencePlayingRef.current = false;
    queueRef.current = [];
  }, []);

  const finishTurn = useCallback(() => {
    if (!turnActiveRef.current) return;
    turnActiveRef.current = false;
    busyRef.current = false;

    const done = doneInfoRef.current;
    doneInfoRef.current = null;
    doneReceivedRef.current = false;

    if (done) {
      turnsRef.current = done.total_turns > 0 ? done.total_turns : turnsRef.current + 1;
      setTurns(turnsRef.current);
      if (done.friend_message?.transcript) {
        setLastFriendText((prev) => prev || done.friend_message?.transcript || "");
      }
    } else {
      turnsRef.current += 1;
      setTurns(turnsRef.current);
    }
    setWaiting(false);
    setThinking(false);
    setSpeaking(false);
    markTiming("finished");
  }, [markTiming]);

  const maybeFinishTurn = useCallback(() => {
    if (!turnActiveRef.current) return;
    if (fillerPlayingRef.current || sentencePlayingRef.current || queueRef.current.length > 0) return;
    if (!doneReceivedRef.current) {
      setSpeaking(false);
      setThinking(true);
      return;
    }
    finishTurn();
  }, [finishTurn]);

  const playNextRef = useRef<() => void>(() => undefined);

  const playNextSentence = () => {
    if (!mountedRef.current || endingRef.current || !turnActiveRef.current) return;
    if (sentencePlayingRef.current || fillerPlayingRef.current) return;
    const next = queueRef.current.shift();
    if (!next) {
      maybeFinishTurn();
      return;
    }
    lastReplyUrlRef.current = next.url;
    const audio = new Audio(next.url);
    sentenceAudioRef.current = audio;
    sentencePlayingRef.current = true;
    setThinking(false);
    setSpeaking(true);
    if (!firstAudioStartedRef.current) {
      firstAudioStartedRef.current = true;
      markTiming("first_audio_start");
    }
    audio.muted = muted;
    audio.volume = speakerOn ? 1 : 0.35;

    const advance = () => {
      if (sentenceAudioRef.current !== audio) return;
      sentencePlayingRef.current = false;
      sentenceAudioRef.current = null;
      setSpeaking(false);
      playNextRef.current();
    };
    audio.onended = advance;
    audio.onerror = () => {
      showToast("对方语音播放失败", "error");
      advance();
    };
    void audio.play().catch(() => {
      showToast("自动播放被浏览器拦截，点按头像重试", "error");
      advance();
    });
  };
  playNextRef.current = playNextSentence;

  /** 播放占位 filler（warmup 预生成），同时继续等流式首句。 */
  const startFiller = useCallback(() => {
    const url = fillerUrlRef.current;
    if (!url) {
      setThinking(true);
      return;
    }
    const audio = new Audio(url);
    fillerAudioRef.current = audio;
    fillerPlayingRef.current = true;
    setThinking(false);
    setSpeaking(true);
    markTiming("filler_start");
    audio.muted = muted;
    audio.volume = speakerOn ? 1 : 0.35;

    const onFillerDone = () => {
      if (fillerAudioRef.current !== audio) return;
      fillerPlayingRef.current = false;
      fillerAudioRef.current = null;
      setSpeaking(false);
      playNextRef.current();
    };
    audio.onended = onFillerDone;
    audio.onerror = onFillerDone;
    void audio.play().catch(onFillerDone);
  }, [markTiming, muted, speakerOn]);

  /* ------------------------------ 失败回退普通接口 ------------------------------ */

  const fallbackToSendVoice = useCallback(
    async (blob: Blob, durationMs: number, filename: string, reason?: string) => {
      if (!mountedRef.current || endingRef.current || !turnActiveRef.current) return;
      if (!fallbackToastShownRef.current) {
        fallbackToastShownRef.current = true;
        showToast(reason ? `流式播放不可用（${reason}），已切换普通模式` : "流式播放不可用，已切换普通模式", "info");
      }
      try {
        const response = await api.sendVoice(friend.id, blob, durationMs, filename);
        if (!mountedRef.current || endingRef.current) return;
        const userText = response.user_message?.transcript?.trim() ?? "";
        const reply = response.friend_messages?.[0] ?? null;
        if (userText) setLastUserText(userText);
        if (reply?.transcript) {
          hasSentenceRef.current = true;
          setLastFriendText(reply.transcript);
        }
        markTiming("fallback_done");
        doneInfoRef.current = { total_turns: response.total_turns, friend_message: reply };
        doneReceivedRef.current = true;
        const audioUrl = reply ? api.audioUrl(reply) : "";
        if (audioUrl) {
          lastReplyUrlRef.current = audioUrl;
          queueRef.current.push({ url: audioUrl, index: 0 });
          playNextRef.current();
        } else {
          maybeFinishTurn();
        }
      } catch (err) {
        if (!mountedRef.current) return;
        showToast(`语音没有送达：${apiErrorText(err)}`, "error");
        doneReceivedRef.current = true;
        maybeFinishTurn();
      }
    },
    [friend.id, markTiming, maybeFinishTurn, showToast],
  );

  /* ------------------------------ 流式一轮 ------------------------------ */

  const runStreamTurn = useCallback(
    async (blob: Blob, durationMs: number, filename: string) => {
      const controller = new AbortController();
      abortRef.current = controller;
      let streamError = "";
      let gotDone = false;
      try {
        await streamCallTurn(friend.id, blob, durationMs, filename, {
          signal: controller.signal,
          onEvent: (event: CallTurnEvent) => {
            if (!mountedRef.current || endingRef.current) return;
            if (event.type === "transcript") {
              markTiming("transcript");
              if (event.text) setLastUserText(event.text);
              return;
            }
            if (event.type === "sentence") {
              hasSentenceRef.current = true;
              markTiming(`sentence_${event.index}`);
              if (event.index === 0) markTiming("first_sentence");
              if (event.text) {
                setLastFriendText((prev) => (prev ? `${prev} ${event.text}` : event.text));
              }
              if (event.audio_url) {
                queueRef.current.push({ url: event.audio_url, index: event.index });
                playNextRef.current();
              }
              return;
            }
            if (event.type === "done") {
              gotDone = true;
              markTiming("done");
              doneReceivedRef.current = true;
              doneInfoRef.current = {
                total_turns: event.total_turns,
                friend_message: event.friend_message,
              };
              if (!hasSentenceRef.current && event.friend_message?.transcript) {
                setLastFriendText(event.friend_message.transcript);
                const fallbackUrl = api.audioUrl(event.friend_message);
                if (fallbackUrl) lastReplyUrlRef.current = fallbackUrl;
              }
              maybeFinishTurn();
              return;
            }
            if (event.type === "error") {
              streamError = event.message || "流式响应失败";
              controller.abort();
            }
          },
        });
        if (!gotDone) {
          doneReceivedRef.current = true;
          maybeFinishTurn();
        }
      } catch (err) {
        if (!mountedRef.current || endingRef.current) return;
        const aborted = err instanceof DOMException && err.name === "AbortError";
        if (aborted && !streamError) return; // 用户挂断/卸载导致的 abort
        await fallbackToSendVoice(
          blob,
          durationMs,
          filename,
          streamError || (err instanceof Error ? err.message : undefined),
        );
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [fallbackToSendVoice, friend.id, markTiming, maybeFinishTurn],
  );

  /* ------------------------------ 每轮入口 ------------------------------ */

  const sendUtterance = useCallback(
    async (blob: Blob, durationMs: number, filename: string) => {
      if (endingRef.current || busyRef.current) return;
      busyRef.current = true;
      turnActiveRef.current = true;
      doneReceivedRef.current = false;
      doneInfoRef.current = null;
      hasSentenceRef.current = false;
      firstAudioStartedRef.current = false;
      queueRef.current = [];
      turnStartedAtRef.current = performance.now();
      timingsRef.current = {};
      markTiming("vad_end");
      markTiming("turn_start");
      setWaiting(true);
      setThinking(false);
      setSpeaking(false);
      setLastUserText("");
      setLastFriendText("");

      // 先把 filler 播起来（填住 STT + 首句生成的间隙），同时开始流式请求
      startFiller();
      await runStreamTurn(blob, durationMs, filename);
    },
    [markTiming, runStreamTurn, startFiller],
  );

  /* ------------------------------ 免提 VAD ------------------------------ */

  const vadEnabled = active && !muted && !waiting && !speaking && !thinking && !ending && !vadUnsupported;

  const vad = useHandsFreeVad({
    enabled: vadEnabled,
    onUtterance: (utterance) => sendUtterance(utterance.blob, utterance.durationMs, utterance.filename),
    onUnavailable: (message) => {
      setVadUnsupported(true);
      showToast(message, "info");
    },
    onDenied: (message) => {
      setMicBlocked(true);
      showToast(message, "error");
    },
    onError: (message) => showToast(message, "error"),
  });

  /* 首次用户手势恢复 AudioContext（自动播放策略） */
  const resumeAudio = useCallback(() => {
    void vad.resume();
  }, [vad]);

  /* ------------------------------ 接通 & 计时 ------------------------------ */

  useEffect(() => {
    mountedRef.current = true;
    const connectTimer = setTimeout(() => {
      if (mountedRef.current) setActive(true);
    }, 800);
    const tick = setInterval(() => {
      if (mountedRef.current) setElapsed(Math.floor((Date.now() - startedAtRef.current) / 1000));
    }, 1000);
    return () => {
      mountedRef.current = false;
      clearTimeout(connectTimer);
      clearInterval(tick);
      abortRef.current?.abort();
      stopAllAudio();
    };
  }, [stopAllAudio]);

  /* ------------------------------ 结束通话 ------------------------------ */

  const endCall = useCallback(async () => {
    if (endingRef.current) return;
    endingRef.current = true;
    setEnding(true);
    abortRef.current?.abort();
    stopAllAudio();
    setSpeaking(false);
    setThinking(false);

    const durationS = Math.max(0, Math.floor((Date.now() - startedAtRef.current) / 1000));
    const turnCount = turnsRef.current;
    try {
      await api.callSummary(friend.id, { duration_s: durationS, turns: turnCount });
      showToast(`通话已结束 ${clockText(durationS)}`, "success");
    } catch (err) {
      showToast(`通话已结束，但摘要保存失败：${apiErrorText(err)}`, "error");
    } finally {
      if (mountedRef.current) onClose();
    }
  }, [friend.id, onClose, showToast, stopAllAudio]);

  /* ------------------------------ 重播最近回复 ------------------------------ */

  /** 重播最近一段回复（不参与轮次计数，失败静默 toast）。 */
  const replayLast = () => {
    if (speaking || thinking || waiting) return;
    const playing = sentenceAudioRef.current ?? fillerAudioRef.current;
    if (playing) {
      try {
        playing.currentTime = 0;
      } catch {
        /* ignore */
      }
      void playing.play().catch(() => showToast("重播失败", "error"));
      setSpeaking(true);
      return;
    }
    if (queueRef.current.length > 0) {
      playNextRef.current();
      return;
    }
    const url = lastReplyUrlRef.current;
    if (!url) {
      showToast("还没有可重播的语音", "info");
      return;
    }
    const audio = new Audio(url);
    sentenceAudioRef.current = audio;
    sentencePlayingRef.current = true;
    setSpeaking(true);
    const finish = () => {
      if (sentenceAudioRef.current !== audio) return;
      sentencePlayingRef.current = false;
      sentenceAudioRef.current = null;
      setSpeaking(false);
    };
    audio.onended = finish;
    audio.onerror = finish;
    void audio.play().catch(() => {
      finish();
      showToast("重播失败", "error");
    });
  };

  /* ------------------------------ 状态机 ------------------------------ */

  const callPhase: CallPhase = !active
    ? "connecting"
    : muted
      ? "muted"
      : speaking
        ? "speaking"
        : waiting
          ? thinking
            ? "thinking"
            : "processing"
          : vad.phase === "recording"
            ? "recording"
            : "listening";

  const statusText = STATUS_TEXT[callPhase];
  const level = callPhase === "recording" ? Math.max(vad.level, 0.25) : callPhase === "listening" ? vad.level : 0;
  const orbActive = callPhase === "listening" || callPhase === "recording";
  const orbBusy = callPhase === "speaking" || callPhase === "processing" || callPhase === "thinking";
  const bars = [0.55, 0.9, 0.7, 1, 0.62];

  const handleFallbackVoice = async (result: RecordingResult) => {
    await sendUtterance(result.blob, result.durationMs, result.filename);
  };

  return (
    <div className="se-call" onPointerDown={resumeAudio} data-phase={callPhase}>
      <div className="se-call-glow" aria-hidden />

      <header className="se-call-head">
        <span className="se-call-head-label">
          <Mic size={13} /> 语音通话
        </span>
        <div className="se-call-head-actions">
          <button
            type="button"
            className={`se-call-head-btn ${speakerOn ? "" : "off"}`}
            aria-label={speakerOn ? "关闭扬声器" : "打开扬声器"}
            aria-pressed={speakerOn}
            onClick={() => setSpeakerOn((value) => !value)}
          >
            {speakerOn ? <Volume2 size={18} /> : <VolumeX size={18} />}
          </button>
          <button
            type="button"
            className={`se-call-head-btn ${muted ? "off" : ""}`}
            aria-label={muted ? "取消静音" : "静音"}
            aria-pressed={muted}
            onClick={() => setMuted((value) => !value)}
          >
            {muted ? <MicOff size={18} /> : <Mic size={18} />}
          </button>
        </div>
      </header>

      <main className="se-call-main">
        <button
          type="button"
          className={`se-call-avatar ${orbBusy ? "pulse" : ""}`}
          onClick={replayLast}
          aria-label="重播对方语音"
        >
          <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={104} ring flag={false} />
          <span className="se-call-avatar-ring" aria-hidden />
        </button>

        <h1 className="se-call-name">{friend.name}</h1>
        <p className="se-call-meta">
          {langMeta.flag} {langMeta.label} · {local.time} 当地
        </p>

        <p className={`se-call-status ${callPhase}`}>
          {statusText}
          {callPhase === "speaking" ? (
            <span className="se-call-wave" aria-hidden>
              <i />
              <i />
              <i />
              <i />
              <i />
            </span>
          ) : null}
        </p>
        <p className="se-call-timer">{clockText(elapsed)}</p>

        {lastUserText || lastFriendText ? (
          <div className="se-call-subtitles">
            {lastUserText ? (
              <p className="se-call-sub">
                <span className="se-call-sub-name">我</span>
                <span className="se-call-sub-text">{truncateText(lastUserText)}</span>
              </p>
            ) : null}
            {lastFriendText ? (
              <p className="se-call-sub">
                <span className="se-call-sub-name">{friend.name}</span>
                <span className="se-call-sub-text">{truncateText(lastFriendText)}</span>
              </p>
            ) : null}
          </div>
        ) : null}

        {micBlocked ? (
          <p className="se-call-warn">
            <MicOff size={12} /> 麦克风不可用（请检查浏览器权限）
            <button type="button" className="se-call-warn-retry" onClick={() => void vad.retry()}>
              <RefreshCw size={11} /> 重试
            </button>
          </p>
        ) : null}

        {turns > 0 ? <p className="se-call-turns">已进行 {turns} 轮对话</p> : null}
      </main>

      <footer className="se-call-controls">
        <div className="se-call-orb-wrap">
          <button
            type="button"
            className={`se-call-orb ${orbActive ? "active" : ""} ${callPhase === "recording" ? "recording" : ""} ${
              callPhase === "muted" ? "muted" : ""
            } ${orbBusy ? "busy" : ""}`}
            aria-label={muted ? "取消静音" : "静音"}
            aria-pressed={muted}
            data-level={level.toFixed(2)}
            onClick={() => setMuted((value) => !value)}
          >
            <span className="se-call-orb-rings" aria-hidden>
              <i />
              <i />
            </span>
            {orbBusy ? (
              <Loader2 size={30} className="animate-spin" />
            ) : muted ? (
              <MicOff size={32} />
            ) : (
              <Mic size={32} />
            )}
          </button>
          <span className="se-call-level" aria-hidden>
            {bars.map((factor, index) => (
              <i
                key={index}
                style={{
                  height: `${6 + Math.round(level * 26 * factor)}px`,
                  opacity: orbActive ? 0.85 : 0.35,
                }}
              />
            ))}
          </span>
          <p className="se-call-orb-hint">{ORB_HINT[callPhase]}</p>
        </div>

        {vadUnsupported ? (
          <div className="se-call-fallback">
            <HoldToTalk
              onComplete={handleFallbackVoice}
              onUnavailable={() => setMicBlocked(true)}
              onError={(message) => showToast(message, "error")}
              disabled={waiting || speaking || thinking || ending}
              label="按住说话（兼容模式）"
            />
          </div>
        ) : null}

        <div className="se-call-bottom">
          <button
            type="button"
            className={`se-call-ctrl ${muted ? "active" : ""}`}
            aria-label={muted ? "取消静音" : "静音"}
            onClick={() => setMuted((value) => !value)}
          >
            {muted ? <MicOff size={20} /> : <Mic size={20} />}
            <span>{muted ? "已静音" : "静音"}</span>
          </button>

          <button
            type="button"
            className="se-call-end"
            aria-label="结束通话"
            disabled={ending}
            onClick={() => void endCall()}
          >
            {ending ? <Loader2 size={22} className="animate-spin" /> : <PhoneOff size={24} />}
          </button>

          <button
            type="button"
            className={`se-call-ctrl ${speakerOn ? "active" : ""}`}
            aria-label={speakerOn ? "关闭扬声器" : "打开扬声器"}
            onClick={() => setSpeakerOn((value) => !value)}
          >
            {speakerOn ? <Volume2 size={20} /> : <VolumeX size={20} />}
            <span>扬声器</span>
          </button>
        </div>
      </footer>
    </div>
  );
}
