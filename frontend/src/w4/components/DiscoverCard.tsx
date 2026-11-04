"use client";

/** 偶遇卡片：被「发现 → 偶遇一个人」复用。 */

import { Play, RefreshCw, UserPlus } from "lucide-react";

import type { DiscoverCandidate } from "../mock";
import { Avatar } from "../ui";

export function DiscoverCard({
  candidate,
  playing,
  already,
  onPlay,
  onNext,
  onAdd,
  onOpen,
}: {
  candidate: DiscoverCandidate;
  playing: boolean;
  already: boolean;
  onPlay: () => void;
  onNext: () => void;
  onAdd: () => void;
  onOpen: () => void;
}) {
  return (
    <div className="w4-card w4-deck-card" key={candidate.id}>
      <div className="w4-deck-cover">
        <div className="w4-deck-cover-img" style={{ backgroundImage: `url(${candidate.avatar})` }} />
      </div>

      <div className="w4-deck-body">
        <div className="w4-deck-avatar">
          <Avatar src={candidate.avatar} name={candidate.name} size={76} round online={candidate.online} />
        </div>
        <div className="w4-card-name" style={{ justifyContent: "center" }}>
          {candidate.name} <span style={{ fontSize: 13 }}>{candidate.flag}</span>
        </div>
        <div className="w4-card-meta" style={{ textAlign: "center" }}>
          {candidate.age ? `${candidate.age} 岁 · ` : ""}
          {candidate.city}，{candidate.country}
          <br />
          {candidate.occupation}
        </div>
        <div className="w4-chips" style={{ justifyContent: "center", marginTop: 8 }}>
          <span className="w4-chip is-green">{candidate.languageLabel}</span>
          <span className="w4-chip">{candidate.accent}</span>
          {candidate.tags.map((t) => (
            <span key={t} className="w4-chip">
              {t}
            </span>
          ))}
        </div>

        <p className="w4-card-bio" style={{ textAlign: "center", padding: "12px 4px 0" }}>
          {candidate.bio}
        </p>
      </div>

      <div className="w4-card-say" style={{ margin: "14px 16px 0" }}>
        <div className="w4-card-say-head">
          <Play size={11} fill="currentColor" strokeWidth={0} />
          TA 会这样跟你打招呼
        </div>
        <div className="w4-card-say-orig">{candidate.greeting}</div>
        <div className="w4-card-say-zh">{candidate.greetingZh}</div>
        <button
          className={`w4-say-play ${playing ? "is-playing" : ""}`}
          onClick={onPlay}
          disabled={!candidate.greetingAudio}
        >
          <span className="w4-say-play-btn">
            {playing ? (
              <svg width="10" height="10" viewBox="0 0 12 12" fill="currentColor">
                <rect x="2" y="1.5" width="3" height="9" rx="0.6" />
                <rect x="7" y="1.5" width="3" height="9" rx="0.6" />
              </svg>
            ) : (
              <Play size={10} fill="currentColor" strokeWidth={0} />
            )}
          </span>
          <span className="w4-request-audio-wave" aria-hidden>
            {Array.from({ length: 16 }).map((_, i) => (
              <i key={i} style={{ height: 5 + ((i * 7) % 13) }} />
            ))}
          </span>
          <span style={{ fontSize: 11.5, color: "var(--w-text-3)" }}>
            {playing
              ? "播放中…"
              : candidate.greetingAudio
                ? "听 TA 的声音"
                : "声音还没准备好"}
          </span>
        </button>
      </div>

      <div className="w4-card-actions">
        <button className="w4-btn" onClick={onNext}>
          <RefreshCw size={14} />
          换一个
        </button>
        {already ? (
          <button className="w4-btn is-primary" onClick={onOpen}>
            已经认识 · 去聊天
          </button>
        ) : (
          <button className="w4-btn is-primary" onClick={onAdd}>
            <UserPlus size={14} />
            认识 TA，聊一句
          </button>
        )}
      </div>
    </div>
  );
}
