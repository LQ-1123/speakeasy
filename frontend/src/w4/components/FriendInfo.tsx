"use client";

/** 会话页右侧的资料卡。 */

import { Clock, MapPin, Sparkles } from "lucide-react";

import { previewVoice, playVoiceUrl, setFriendVoice as apiSetFriendVoice } from "../apiFacade";
import { CLOSENESS, languageLabel } from "../format";
import { isLocalMode } from "../local/keys";
import type { Friend } from "../model";
import { useApp } from "../store";
import { Avatar, Icon } from "../ui";

export function FriendInfo({ friend }: { friend: Friend }) {
  const { expressions, updateFriendVoice, showToast } = useApp();
  const fromThisFriend = expressions.filter((e) => e.sourceFriendId === friend.id).length;

  const switchVoice = async (voice: "anna" | "alex") => {
    const ok = await apiSetFriendVoice(friend.id, voice);
    if (!ok) {
      showToast("切换失败，稍后再试", "error");
      return;
    }
    updateFriendVoice(friend.id, voice, voice === "alex" ? "男声" : "女声");
    showToast(voice === "alex" ? "已换成男声" : "已换成女声");
  };

  const previewCurrent = async () => {
    const voice: "anna" | "alex" =
      friend.voice === "alex" || friend.voice === "anna"
        ? friend.voice
        : friend.gender === "male"
          ? "alex"
          : "anna";
    const url = await previewVoice("Hi! This is how I sound when we talk.", voice);
    if (!url) {
      showToast("试听失败：检查 SiliconFlow Key", "error");
      return;
    }
    await playVoiceUrl(url);
  };

  return (
    <>
      <div className="w4-info-person">
        <Avatar friend={friend} size={78} round online={friend.online} />
        <div className="w4-info-name">
          {friend.name}
          <span style={{ fontSize: 13 }}>{friend.flag}</span>
        </div>
        <div className="w4-info-meta">
          {friend.age} 岁 · {friend.city} · {friend.occupation}
        </div>
        <div className="w4-chips" style={{ justifyContent: "center", marginTop: 8 }}>
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
            <Clock size={12} style={{ verticalAlign: -1, marginRight: 3 }} />
            当地
          </dt>
          <dd>
            {friend.localTime} · {friend.localPeriod}
            {friend.localActivity ? `，${friend.localActivity}` : ""}
          </dd>
        </div>
        <div className="w4-kv">
          <dt>
            <MapPin size={12} style={{ verticalAlign: -1, marginRight: 3 }} />
            时差
          </dt>
          <dd>{friend.timezone ? timeDiff(friend.timezone) : "未知"}</dd>
        </div>
      </div>

      <div className="w4-info-section">
        <div className="w4-info-label">TA 记得的</div>
        {friend.sharedEpisodes.length ? (
          friend.sharedEpisodes.map((s) => (
            <div key={s} className="w4-kv" style={{ padding: "2px 0" }}>
              <dd style={{ paddingLeft: 0 }}>· {s}</dd>
            </div>
          ))
        ) : (
          <div style={{ fontSize: 12.5, color: "var(--w-text-3)" }}>还没什么共同经历，多聊聊。</div>
        )}
        {friend.openLoops.length ? (
          <div className="w4-openloop">
            <Sparkles size={12} strokeWidth={1.8} />
            {friend.openLoops[0]}
          </div>
        ) : null}
      </div>

      {isLocalMode() ? (
        <div className="w4-info-section">
          <div className="w4-info-label">TA 的声音</div>
          <div className="w4-chips">
            <button className="w4-chip" onClick={() => void switchVoice("anna")}>
              女声
            </button>
            <button className="w4-chip" onClick={() => void switchVoice("alex")}>
              男声
            </button>
            <button className="w4-chip" onClick={() => void switchVoice(Math.random() < 0.5 ? "alex" : "anna")}>
              随机
            </button>
            <button className="w4-chip" onClick={() => void previewCurrent()}>
              试听
            </button>
          </div>
          <div style={{ fontSize: 12, color: "var(--w-text-3)", marginTop: 4 }}>
            换完发一条消息或打一次电话就能听到。
          </div>
        </div>
      ) : null}

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
        <div style={{ fontSize: 12.5, color: "var(--w-text-2)" }}>
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
