"use client";

/** 偶遇卡片：被「发现 → 偶遇一个人」复用。 */

import { Pause, Play, RefreshCw, UserPlus } from "lucide-react";

import type { DiscoverCandidate } from "../model";
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
    <article className="disc-deck" key={candidate.id}>
      <div className="disc-deck-hero">
        <div className="disc-deck-avatar">
          <Avatar
            src={candidate.avatar}
            name={candidate.name}
            size={150}
            round
            online={candidate.online}
          />
        </div>
      </div>

      <div className="disc-deck-body">
        <div className="disc-deck-name">
          {candidate.name}
          <span className="disc-flag" aria-label={candidate.country || "地区"}>
            {candidate.flag}
          </span>
        </div>
        <div className="disc-deck-meta">
          {candidate.age ? `${candidate.age} 岁 · ` : ""}
          {candidate.occupation}
          {candidate.city ? ` · ${candidate.city}` : ""}
          {candidate.country ? `，${candidate.country}` : ""}
        </div>
        <div className="disc-deck-chips">
          <span className="disc-chip">{candidate.languageLabel}</span>
          {candidate.accent ? <span className="disc-chip">{candidate.accent}</span> : null}
          {candidate.tags.map((t) => (
            <span key={t} className="disc-chip">
              {t}
            </span>
          ))}
        </div>
        {candidate.bio ? <p className="disc-deck-bio">{candidate.bio}</p> : null}
      </div>

      <div className="disc-deck-say">
        <div className="disc-deck-say-head">
          <Play size={11} fill="currentColor" strokeWidth={0} aria-hidden />
          TA 会这样跟你打招呼
        </div>
        <p className="disc-deck-say-orig">{candidate.greeting}</p>
        {candidate.greetingZh ? <p className="disc-deck-say-zh">{candidate.greetingZh}</p> : null}
        <button
          type="button"
          className={`disc-play ${playing ? "is-playing" : ""}`}
          onClick={onPlay}
          disabled={!candidate.greetingAudio}
          aria-label={playing ? "停止播放" : "听 TA 的声音"}
        >
          <span className="disc-play-icon" aria-hidden>
            {playing ? (
              <Pause size={12} strokeWidth={2.2} />
            ) : (
              <Play size={12} fill="currentColor" strokeWidth={0} />
            )}
          </span>
          <span className="disc-wave" aria-hidden>
            {Array.from({ length: 16 }).map((_, i) => (
              <i
                key={i}
                style={{
                  height: 5 + ((i * 7) % 13),
                  animationDelay: `${i * 45}ms`,
                }}
              />
            ))}
          </span>
          <span className="disc-play-label">
            {playing
              ? "播放中…"
              : candidate.greetingAudio
                ? "听 TA 的声音"
                : "声音还没准备好"}
          </span>
        </button>
      </div>

      <div className="disc-deck-actions">
        <button type="button" className="w4-btn" onClick={onNext}>
          <RefreshCw size={16} />
          换一个
        </button>
        {already ? (
          <button type="button" className="w4-btn is-primary" onClick={onOpen}>
            已经认识 · 去聊天
          </button>
        ) : (
          <button type="button" className="w4-btn is-primary" onClick={onAdd}>
            <UserPlus size={16} />
            认识 TA，聊一句
          </button>
        )}
      </div>
    </article>
  );
}
