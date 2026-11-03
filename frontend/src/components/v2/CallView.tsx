"use client";

/**
 * 语音通话（回合制 push-to-talk）全屏界面。
 *
 * - 打开 ~800ms 后「正在接通…」→「通话中」；
 * - 按住说话复用 HoldToTalk/useVoiceRecorder → api.sendVoice（与聊天语音条同一接口）；
 * - 好友回复语音自动播放，播放期间禁止录音；播放失败 toast，可点按头像重试；
 * - 结束通话写 call_summary（POST /chat/{id}/call/summary），关闭后由 ChatView 刷新消息列表。
 * - 视觉：深色渐变、大头像脉冲、计时、字幕、静音/扬声器纯视觉开关、红色结束按钮。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Mic, MicOff, PhoneOff, Volume2, VolumeX } from "lucide-react";

import { Avatar } from "@/components/v2/Avatar";
import { HoldToTalk } from "@/components/v2/HoldToTalk";
import { useToast } from "@/components/v2/Toast";
import { api, apiErrorText } from "@/lib/v2/api";
import { localStateOf, replyLanguageMeta } from "@/lib/v2/format";
import type { Friend } from "@/lib/v2/types";
import type { RecordingResult } from "@/lib/v2/useRecorder";

interface CallViewProps {
  friend: Friend;
  /** 关闭通话（ChatView 负责 loadMessages 刷新 call_summary） */
  onClose: () => void;
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
  const [speaking, setSpeaking] = useState(false);
  const [ending, setEnding] = useState(false);
  const [micBlocked, setMicBlocked] = useState(false);
  const [muted, setMuted] = useState(false);
  const [speakerOn, setSpeakerOn] = useState(true);
  const [lastUserText, setLastUserText] = useState("");
  const [lastFriendText, setLastFriendText] = useState("");

  const startedAtRef = useRef(Date.now());
  const turnsRef = useRef(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const lastAudioUrlRef = useRef("");
  const endingRef = useRef(false);
  const mountedRef = useRef(true);
  const busyRef = useRef(false);

  const local = localStateOf(friend);
  const langMeta = replyLanguageMeta(friend);

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

  /* ------------------------------ 播放好友语音 ------------------------------ */

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
        if (mountedRef.current) setSpeaking(false);
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

  /* ------------------------------ 每轮说话 ------------------------------ */

  const handleVoice = useCallback(
    async (result: RecordingResult) => {
      if (!active || endingRef.current || busyRef.current) return;
      busyRef.current = true;
      setWaiting(true);
      try {
        const response = await api.sendVoice(friend.id, result.blob, result.durationMs, result.filename);
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
        showToast(`发送失败：${apiErrorText(err)}`, "error");
      } finally {
        busyRef.current = false;
      }
    },
    [active, friend.id, playReply, showToast],
  );

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

  /* ------------------------------ 状态文案 ------------------------------ */

  const statusText = !active ? "正在接通…" : waiting || speaking ? "对方正在说话…" : "通话中";
  const statusBusy = !active || waiting || speaking;
  const holdDisabled = !active || waiting || speaking || ending;

  return (
    <div className="se-call">
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
          className={`se-call-avatar ${statusBusy ? "pulse" : ""}`}
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

        <p className={`se-call-status ${speaking ? "speaking" : ""}`}>
          {statusText}
          {speaking ? (
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
        ) : (
          <p className="se-call-hint">
            {active ? "按住下方按钮说话，松手发送" : "正在接通对方…"}
          </p>
        )}

        {micBlocked ? (
          <p className="se-call-warn">
            <MicOff size={12} /> 麦克风不可用（请检查浏览器权限），仍可结束通话
          </p>
        ) : null}

        {turns > 0 ? <p className="se-call-turns">已进行 {turns} 轮对话</p> : null}
      </main>

      <footer className="se-call-controls">
        <div className="se-call-hold">
          <HoldToTalk
            onComplete={handleVoice}
            onUnavailable={(message) => {
              setMicBlocked(true);
              showToast(message, "info");
            }}
            onError={(message) => showToast(message, "error")}
            disabled={holdDisabled}
            label={waiting ? "对方正在回复…" : "按住 说话"}
          />
        </div>

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
