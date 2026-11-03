"use client";

/**
 * 语音通话（免提 / hands-free VAD）全屏界面。
 *
 * - 接通后自动监听：直接说话即可，VAD 自动断句（~1.2s 静音）上传 `api.sendVoice`；
 * - 好友语音自动播放，播放结束自动恢复监听，循环；播放期间不录音（enabled=false）；
 * - 静音 = 暂停监听；不支持 VAD 时降级为「按住说话」；麦克风被拒绝可重试或结束；
 * - 结束通话写 call_summary；卸载时停止录音、关闭 AudioContext、停止 tracks、清计时器。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Mic, MicOff, PhoneOff, RefreshCw, Volume2, VolumeX } from "lucide-react";

import { Avatar } from "@/components/v2/Avatar";
import { HoldToTalk } from "@/components/v2/HoldToTalk";
import { useToast } from "@/components/v2/Toast";
import { api, apiErrorText } from "@/lib/v2/api";
import { localStateOf, replyLanguageMeta } from "@/lib/v2/format";
import type { Friend } from "@/lib/v2/types";
import { useHandsFreeVad } from "@/lib/v2/useHandsFreeVad";
import type { RecordingResult } from "@/lib/v2/useRecorder";

interface CallViewProps {
  friend: Friend;
  /** 关闭通话（ChatView 负责 loadMessages 刷新 call_summary） */
  onClose: () => void;
}

type CallPhase = "connecting" | "listening" | "recording" | "processing" | "speaking" | "muted";

const STATUS_TEXT: Record<CallPhase, string> = {
  connecting: "正在接通…",
  listening: "我在听…",
  recording: "正在聆听…",
  processing: "对方正在思考…",
  speaking: "对方正在说话…",
  muted: "已静音",
};

const ORB_HINT: Record<CallPhase, string> = {
  connecting: "正在接通对方…",
  listening: "直接说话即可，我在听",
  recording: "说完停顿一下，我会自动接上",
  processing: "对方正在组织语言，稍等…",
  speaking: "对方正在说话，稍后会自动继续听",
  muted: "已静音，点麦克风或静音键恢复",
};

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
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const lastAudioUrlRef = useRef("");
  const endingRef = useRef(false);
  const mountedRef = useRef(true);
  const busyRef = useRef(false);

  const local = localStateOf(friend);
  const langMeta = replyLanguageMeta(friend);

  /* ------------------------------ 好友语音播放 ------------------------------ */

  const playReply = useCallback(
    (url: string) => {
      if (!mountedRef.current || !url) return;
      const previous = audioRef.current;
      if (previous) {
        previous.onended = null;
        previous.onerror = null;
        previous.pause();
      }
      lastAudioUrlRef.current = url;
      const audio = new Audio(url);
      audioRef.current = audio;
      audio.muted = muted;
      audio.volume = speakerOn ? 1 : 0.35;
      audio.onended = () => {
        if (!mountedRef.current) return;
        setSpeaking(false); // enabled 随之恢复 → VAD 自动重新监听
      };
      audio.onerror = () => {
        if (!mountedRef.current) return;
        setSpeaking(false);
        showToast("对方语音播放失败，点按头像可重试", "error");
      };
      setSpeaking(true);
      void audio.play().catch(() => {
        if (!mountedRef.current) return;
        setSpeaking(false);
        showToast("自动播放被浏览器拦截，点按头像播放语音", "error");
      });
    },
    [muted, showToast, speakerOn],
  );

  const replayLast = () => {
    if (speaking || waiting || !lastAudioUrlRef.current) return;
    playReply(lastAudioUrlRef.current);
  };

  /* ------------------------------ 上传一轮语音 ------------------------------ */

  const sendUtterance = useCallback(
    async (blob: Blob, durationMs: number, filename: string) => {
      if (endingRef.current || busyRef.current) return;
      busyRef.current = true;
      setWaiting(true);
      try {
        const response = await api.sendVoice(friend.id, blob, durationMs, filename);
        if (!mountedRef.current || endingRef.current) return;

        const userText = response.user_message?.transcript?.trim() ?? "";
        const reply = response.friend_messages?.[0];
        const friendText = reply?.transcript?.trim() ?? "";
        turnsRef.current += 1;
        setTurns(turnsRef.current);
        if (userText) setLastUserText(userText);
        if (friendText) setLastFriendText(friendText);
        setWaiting(false);

        const audioUrl = reply ? api.audioUrl(reply) : "";
        if (audioUrl) {
          playReply(audioUrl);
        } else {
          showToast("对方暂时没有语音回复", "info");
        }
      } catch (err) {
        if (!mountedRef.current) return;
        setWaiting(false);
        showToast(`这轮语音没传过去，请再说一次：${apiErrorText(err)}`, "error");
      } finally {
        busyRef.current = false;
      }
    },
    [friend.id, playReply, showToast],
  );

  /* ------------------------------ 免提 VAD ------------------------------ */

  const vadEnabled = active && !muted && !waiting && !speaking && !ending && !vadUnsupported;

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
      const audio = audioRef.current;
      if (audio) {
        audio.onended = null;
        audio.onerror = null;
        audio.pause();
        audio.src = "";
      }
      audioRef.current = null;
    };
  }, []);

  /* ------------------------------ 结束通话 ------------------------------ */

  const endCall = useCallback(async () => {
    if (endingRef.current) return;
    endingRef.current = true;
    setEnding(true);

    const audio = audioRef.current;
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
    }
    setSpeaking(false);

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
  }, [friend.id, onClose, showToast]);

  /* ------------------------------ 状态机 ------------------------------ */

  const callPhase: CallPhase = !active
    ? "connecting"
    : muted
      ? "muted"
      : speaking
        ? "speaking"
        : waiting
          ? "processing"
          : vad.phase === "recording"
            ? "recording"
            : "listening";

  const statusText = STATUS_TEXT[callPhase];
  const level = callPhase === "recording" ? Math.max(vad.level, 0.25) : callPhase === "listening" ? vad.level : 0;
  const orbActive = callPhase === "listening" || callPhase === "recording";
  const orbBusy = callPhase === "speaking" || callPhase === "processing";
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
            {waiting || speaking ? (
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
              disabled={waiting || speaking || ending}
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
