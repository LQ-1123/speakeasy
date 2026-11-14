"use client";

import { useShallow } from "zustand/react/shallow";

/** 新的朋友：好友申请列表，可以试听 TA 的声音再决定。 */

import { useEffect, useRef, useState } from "react";
import { Check, Play, X } from "lucide-react";

import { prefetchVoiceUrl } from "../apiFacade";
import { resolveGreetingVoice } from "../greetingVoice";
import type { Route } from "../model";
import { useApp } from "../store";
import { haptic } from "../../theme/haptics";
import { Avatar, Empty, Icon } from "../ui";
import type { Friend } from "../model";

export function RequestsPane({ navigate }: { navigate: (r: Route) => void }) {
  const { requests, acceptRequest, declineRequest, showToast } = useApp(useShallow((state) => ({ requests: state.requests, acceptRequest: state.acceptRequest, declineRequest: state.declineRequest, showToast: state.showToast })));
  const [playingId, setPlayingId] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  /* 离开新的朋友页暂停预览 */
  useEffect(
    () => () => {
      audioRef.current?.pause();
      audioRef.current = null;
    },
    [],
  );

  /* 预取：把每条申请的招呼语音提前取成 blob，点下去直接响。
     不能只 `new Audio(url).load()`：签名过期/旧数据里 greeting_audio 缺失时，
     浏览器原生 <audio> 无法带身份头，onerror 后只能用 /tts/sample 现场合成。 */
  useEffect(() => {
    for (const r of requests.slice(0, 4)) {
      if (!r.greetingAudio) continue;
      void prefetchVoiceUrl(r.greetingAudio);
    }
  }, [requests]);

  /** 试听：有预生成的语音文件就直接播（即时）；没有才现场合成兜底 */
  const playPreview = (friend: Friend) => {
    if (playingId === friend.id) {
      audioRef.current?.pause();
      audioRef.current = null;
      setPlayingId(null);
      return;
    }
    audioRef.current?.pause();
    setPlayingId(friend.id);
    void (async () => {
      /* 招呼语音：服务端已生成就直接用；只有在完全缺失/失效时才现场合成 */
      const src = await resolveGreetingVoice(friend);
      if (!src) {
        setPlayingId(null);
        showToast("招呼语音暂时无法播放，请点击重试", "error");
        return;
      }
      const a = new Audio(src);
      audioRef.current = a;
      a.onended = () => setPlayingId(null);
      a.onerror = () => setPlayingId(null);
      try {
        await a.play();
      } catch {
        setPlayingId(null);
      }
    })();
  };

  return (
    <div className="w4-main">
      <div className="w4-head se-pane-head">
        <button
          className="w4-icon-btn w4-only-mobile"
          onClick={() => navigate({ name: "contacts" })}
          aria-label="返回"
        >
          <Icon strokeWidth={2}>
            <path d="M15 5l-7 7 7 7" />
          </Icon>
        </button>
        <div className="w4-head-main">
          <div className="w4-head-title">新的朋友</div>
          <div className="w4-head-sub">
            {requests.length ? `${requests.length} 个申请` : "没有待处理的申请"}
          </div>
        </div>
      </div>

      <div className="w4-scroll">
        {requests.length === 0 ? (
          <Empty
            icon={<UserPlusGlyph />}
            title="暂时没有人加你"
            text={
              <>
                你可以在「偶遇」里主动认识别人，
                <br />
                聊得来的好友也会介绍他们的朋友给你。
              </>
            }
            action={
              <button className="w4-btn is-primary" onClick={() => navigate({ name: "discover" })}>
                去偶遇
              </button>
            }
          />
        ) : (
          <div className="w4-requests">
            {requests.map((r) => (
              <RequestRow
                key={r.id}
                friend={r}
                playing={playingId === r.id}
                onPlay={() => playPreview(r)}
                onAccept={() => acceptRequest(r.id)}
                onDecline={() => declineRequest(r.id)}
              />
            ))}
            <div className="w4-requests-note">
              接受了之后，TA 会用语音先跟你打个招呼。
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function RequestRow({
  friend,
  playing,
  onPlay,
  onAccept,
  onDecline,
}: {
  friend: Friend;
  playing: boolean;
  onPlay: () => void;
  onAccept: () => void;
  onDecline: () => void;
}) {
  return (
    <div className="w4-request-card">
      <Avatar friend={friend} size={48} round />
      <div className="w4-request-main">
        <div className="w4-request-name">
          {friend.name} <span>{friend.flag}</span>
          <span className="w4-request-city">{friend.city}</span>
        </div>
        <div className="w4-request-note">{friend.requestNote}</div>
        <button className={`w4-request-audio ${playing ? "is-playing" : ""}`} onClick={onPlay}>
          <span className="w4-request-audio-btn">
            {playing ? (
              <svg width="9" height="9" viewBox="0 0 12 12" fill="currentColor">
                <rect x="2" y="1.5" width="3" height="9" rx="0.6" />
                <rect x="7" y="1.5" width="3" height="9" rx="0.6" />
              </svg>
            ) : (
              <Play size={10} fill="currentColor" strokeWidth={0} />
            )}
          </span>
          <span className="w4-request-audio-wave" aria-hidden>
            {Array.from({ length: 12 }).map((_, i) => (
              <i key={i} style={{ height: 5 + ((i * 5) % 11) }} />
            ))}
          </span>
          <span className="w4-request-audio-text">
            {playing ? "正在播放…" : "听听 TA 怎么说话"}
          </span>
        </button>
      </div>
      <div className="w4-request-actions">
        <button
          className="w4-btn is-primary"
          onClick={() => {
            haptic("success");
            onAccept();
          }}
        >
          <Check size={13} />
          接受
        </button>
        <button className="w4-btn" onClick={onDecline}>
          <X size={13} />
          忽略
        </button>
      </div>
    </div>
  );
}

function UserPlusGlyph() {
  return (
    <Icon size={26} strokeWidth={1.5}>
      <path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M19 8v6M22 11h-6" />
    </Icon>
  );
}
