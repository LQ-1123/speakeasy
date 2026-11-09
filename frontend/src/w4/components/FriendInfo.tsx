"use client";

import { useShallow } from "zustand/react/shallow";

/** 会话页右侧的资料卡。 */

import { Clock, MapPin, Sparkles } from "lucide-react";

import { CLOSENESS, languageLabel } from "../format";
import type { Friend } from "../model";
import { useApp } from "../store";
import { Avatar } from "../ui";

export function FriendInfo({ friend }: { friend: Friend }) {
  const { expressions } = useApp(useShallow((state) => ({ expressions: state.expressions })));
  const fromThisFriend = expressions.filter((e) => e.sourceFriendId === friend.id).length;

  return (
    <>
      <div className="w4-info-person">
        <Avatar friend={friend} size={78} round online={friend.online} />
        <div className="w4-info-name">
          {friend.name}
          <span className="se-info-flag">{friend.flag}</span>
        </div>
        <div className="w4-info-meta">
          {friend.age} 岁 · {friend.city} · {friend.occupation}
        </div>
        <div className="w4-chips se-info-chips">
          <span className="w4-chip is-green">{CLOSENESS[friend.closeness]}</span>
          <span className="w4-chip">{friend.languageLabel}</span>
          <span className="w4-chip">{friend.voiceLabel}</span>
        </div>
      </div>

      {friend.bio ? <p className="w4-info-bio">{friend.bio}</p> : null}

      <div className="w4-info-section">
        <div className="w4-info-label">现在</div>
        <div className="w4-kv">
          <dt>
            <Clock size={12} className="se-kv-icon" />
            当地
          </dt>
          <dd>
            {friend.localTime} · {friend.localPeriod}
            {friend.localActivity ? `，${friend.localActivity}` : ""}
          </dd>
        </div>
        <div className="w4-kv">
          <dt>
            <MapPin size={12} className="se-kv-icon" />
            时差
          </dt>
          <dd>{friend.timezone ? timeDiff(friend.timezone) : "未知"}</dd>
        </div>
      </div>

      <div className="w4-info-section">
        <div className="w4-info-label">TA 记得的</div>
        {friend.sharedEpisodes.length ? (
          friend.sharedEpisodes.map((s) => (
            <div key={s} className="w4-kv se-kv-note">
              <dd className="se-kv-plain">· {s}</dd>
            </div>
          ))
        ) : (
          <div className="se-info-empty">还没什么共同经历，多聊聊。</div>
        )}
        {friend.openLoops.length ? (
          <div className="w4-openloop">
            <Sparkles size={12} strokeWidth={1.8} />
            {friend.openLoops[0]}
          </div>
        ) : null}
      </div>

      <div className="w4-info-section">
        <div className="w4-info-label">说话方式</div>
        <div className="w4-chips">
          {friend.quirks.map((q) => (
            <span key={q} className="w4-chip">
              {q}
            </span>
          ))}
        </div>
      </div>

      <div className="w4-info-section">
        <div className="w4-info-label">一起练过的</div>
        <div className="se-info-turns">
          聊过 <b>{friend.turns}</b> 轮
          {fromThisFriend ? (
            <>
              ，收藏了 <b>{fromThisFriend}</b> 条表达
            </>
          ) : null}
        </div>
      </div>
    </>
  );
}

function timeDiff(tz: string): string {
  const map: Record<string, string> = {
    "Europe/London": "比中国慢 8 小时",
    "Europe/Rome": "比中国慢 7 小时",
    "Europe/Lisbon": "比中国慢 8 小时",
    "Europe/Madrid": "比中国慢 7 小时",
    "Europe/Dublin": "比中国慢 8 小时",
    "Europe/Oslo": "比中国慢 7 小时",
    "Asia/Tokyo": "比中国快 1 小时",
    "Asia/Seoul": "比中国快 1 小时",
    "Pacific/Auckland": "比中国快 5 小时",
  };
  return map[tz] ?? "—";
}

export { languageLabel };
