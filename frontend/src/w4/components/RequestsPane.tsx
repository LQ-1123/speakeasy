"use client";

/** 新的朋友：好友申请列表，可以试听 TA 的声音再决定。 */

import { useState } from "react";
import { Check, Play, X } from "lucide-react";

import type { Route } from "../model";
import { useApp } from "../store";
import { Avatar, Empty, Icon } from "../ui";
import type { Friend } from "../model";

export function RequestsPane({ navigate }: { navigate: (r: Route) => void }) {
  const { requests, acceptRequest, declineRequest } = useApp();
  const [playingId, setPlayingId] = useState<string | null>(null);

  return (
    <div className="w4-main">
      <div className="w4-head">
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
                onPlay={() => setPlayingId((p) => (p === r.id ? null : r.id))}
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
        <button className="w4-btn is-primary" style={{ height: 28, padding: "0 10px", fontSize: 12 }} onClick={onAccept}>
          <Check size={13} />
          接受
        </button>
        <button className="w4-btn" style={{ height: 28, padding: "0 10px", fontSize: 12 }} onClick={onDecline}>
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
