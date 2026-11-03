"use client";

/** 语音条气泡：波形 + 时长 + 「文」转写展开 + 0.8x/1.0x 倍速 + 播放高亮。 */

import { useEffect, useRef, useState } from "react";
import { Pause, Play, Volume2 } from "lucide-react";

import { Avatar } from "@/components/v2/Avatar";
import { useToast } from "@/components/v2/Toast";
import { api } from "@/lib/v2/api";
import { durationLabel, waveformBars } from "@/lib/v2/format";
import type { Friend, Message } from "@/lib/v2/types";

interface VoiceBubbleProps {
  message: Message;
  friend: Friend;
  mine: boolean;
  /** 仅好友消息：在气泡下方展示 suggestion 胶囊。 */
  showAvatar?: boolean;
}

export function VoiceBubble({ message, friend, mine, showAvatar = true }: VoiceBubbleProps) {
  const { showToast } = useToast();
  const [expanded, setExpanded] = useState(false);
  const [rate, setRate] = useState(1);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const blobUrlRef = useRef<string | null>(null);
  const retriedRef = useRef(false);
  const mountedRef = useRef(true);
  const rafRef = useRef<number | null>(null);
  const bars = waveformBars(message.id || message.created_at, message.duration_ms > 8000 ? 22 : 16);

  const src = message.local_audio_url || (message.audio_url ? message.audio_url : api.audioUrl(message));

  const stopRaf = () => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  };

  const tick = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.duration && Number.isFinite(audio.duration) && audio.duration > 0) {
      setProgress(Math.min(1, audio.currentTime / audio.duration));
    }
    rafRef.current = requestAnimationFrame(tick);
  };

  const stop = () => {
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.currentTime = 0;
    }
    setPlaying(false);
    setProgress(0);
    stopRaf();
  };

  /** 直接加载失败时（Safari/WAV MIME、Range、瞬时 404）用 fetch+blob 重试一次。 */
  const retryWithBlob = async () => {
    // 已经离开聊天页/组件卸载：绝不能再发请求/播放（否则退回主页会突然响一声）
    if (!mountedRef.current) return;
    if (retriedRef.current) {
      setPlaying(false);
      setLoading(false);
      showToast("音频加载失败：可点「文」查看原文，或刷新页面后重试", "error");
      return;
    }
    retriedRef.current = true;
    setPlaying(false);
    setLoading(true);
    try {
      if (!src) throw new Error("没有音频地址");
      const resp = await fetch(src, { cache: "no-store" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const blob = await resp.blob();
      if (!mountedRef.current) return;
      if (blob.size < 44) throw new Error("音频数据异常");
      if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current);
      blobUrlRef.current = URL.createObjectURL(blob);
      const retry = new Audio(blobUrlRef.current);
      retry.preload = "auto";
      retry.onended = () => {
        setPlaying(false);
        setProgress(0);
        stopRaf();
      };
      retry.onerror = () => {
        setPlaying(false);
        setLoading(false);
        showToast("音频加载失败：可点「文」查看原文，或刷新页面后重试", "error");
      };
      audioRef.current = retry;
      retry.playbackRate = rate;
      if (!mountedRef.current) return;
      await retry.play();
      setPlaying(true);
      setLoading(false);
      stopRaf();
      rafRef.current = requestAnimationFrame(tick);
    } catch (err) {
      setLoading(false);
      setPlaying(false);
      const detail = err instanceof Error ? err.message : "网络或格式问题";
      showToast(`音频加载失败（${detail}）：可点「文」查看原文`, "error");
    }
  };

  const toggle = async () => {
    if (playing) {
      stop();
      return;
    }
    if (!src) {
      showToast("语音还在合成中，稍后再听", "info");
      return;
    }
    let audio = audioRef.current;
    if (!audio) {
      audio = new Audio(src);
      audio.preload = "auto";
      audio.onended = () => {
        setPlaying(false);
        setProgress(0);
        stopRaf();
      };
      audio.onerror = () => {
        if (!mountedRef.current) return;
        void retryWithBlob();
      };
      audioRef.current = audio;
    }
    audio.playbackRate = rate;
    try {
      setLoading(true);
      await audio.play();
      setPlaying(true);
      setLoading(false);
      stopRaf();
      rafRef.current = requestAnimationFrame(tick);
    } catch {
      if (mountedRef.current) void retryWithBlob();
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      // 关键：先标记卸载并解绑事件，再清空 src（清空 src 会触发 error 事件）
      mountedRef.current = false;
      stopRaf();
      const audio = audioRef.current;
      if (audio) {
        audio.onended = null;
        audio.onerror = null;
        audio.pause();
        audio.removeAttribute("src");
        try {
          audio.load();
        } catch {
          /* ignore */
        }
      }
      audioRef.current = null;
      if (blobUrlRef.current) {
        URL.revokeObjectURL(blobUrlRef.current);
        blobUrlRef.current = null;
      }
    };
  }, []);

  const toggleRate = () => {
    const next = rate === 1 ? 0.8 : 1;
    setRate(next);
    if (audioRef.current) audioRef.current.playbackRate = next;
  };

  const placeholderText = message.placeholder
    ? message.transcript || "识别中…"
    : message.transcript || "（转写中…）";

  return (
    <div className={`se-bubble-row ${mine ? "me" : ""}`}>
      {!mine && showAvatar ? (
        <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={38} />
      ) : null}

      <div className="flex min-w-0 flex-col gap-1">
        <div
          className={`se-voice ${mine ? "me" : "other"} ${playing ? "playing" : ""} ${
            message.pending ? "pending" : ""
          }`}
          role="button"
          tabIndex={0}
          aria-label={playing ? "暂停语音" : "播放语音"}
          onClick={() => void toggle()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              void toggle();
            }
          }}
        >
          {playing ? (
            <Pause size={13} className="shrink-0 opacity-70" />
          ) : (
            <Play size={13} className="shrink-0 opacity-70" />
          )}

          <span className="se-wave" aria-hidden>
            {bars.map((height, index) => {
              const passed = progress * bars.length > index;
              return (
                <span
                  key={index}
                  className={passed ? "passed" : ""}
                  style={{
                    height: `${Math.round(height * 0.34)}px`,
                    animationDelay: `${(index % 7) * 60}ms`,
                  }}
                />
              );
            })}
          </span>

          <span className="shrink-0 text-[11px] tabular-nums opacity-70">
            {loading ? "…" : durationLabel(message.duration_ms)}
          </span>

          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              setExpanded((value) => !value);
              if (audioRef.current) audioRef.current.playbackRate = rate;
            }}
            aria-label={expanded ? "收起文字" : "展开文字"}
            className={`se-wen ${mine ? "me" : "other"} ${expanded ? "active" : ""}`}
          >
            文
          </button>
        </div>

        <div className={`se-bubble-meta ${mine ? "me" : ""}`}>
          <button type="button" onClick={toggleRate} className="se-rate" aria-label="切换倍速">
            {rate === 1 ? "1.0x" : "0.8x"}
          </button>
          {message.pending ? <span className="text-[10.5px] text-[color:var(--se-faint)]">已发送</span> : null}
        </div>

        {expanded ? (
          <div className={`se-transcript ${mine ? "me" : ""}`}>
            <p>{placeholderText}</p>
            {message.msg_type === "text" && message.transcript ? (
              <span className="se-transcript-tag">
                <Volume2 size={10} />
                文字消息
              </span>
            ) : null}
          </div>
        ) : null}
      </div>

      {mine ? (
        <Avatar name="我" seed="me-speakeasy" size={38} flag={false} />
      ) : null}
    </div>
  );
}
