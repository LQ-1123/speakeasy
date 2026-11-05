"use client";

import { useEffect, useState } from "react";

/** 会话页右侧的资料卡。 */

import { Clock, MapPin, Sparkles } from "lucide-react";

import { previewVoice, playVoiceUrl, setFriendVoice as apiSetFriendVoice } from "../apiFacade";
import { CLOSENESS, languageLabel } from "../format";
import { cachedFishVoices, loadFishVoices, type FishVoice } from "../local/fish";
import { COSY_VOICES } from "../local/tts";
import { isLocalMode } from "../local/keys";
import type { Friend } from "../model";
import { useApp } from "../store";
import { Avatar, Icon } from "../ui";

export function FriendInfo({ friend }: { friend: Friend }) {
  const { expressions, updateFriendVoice, showToast } = useApp();
  const fromThisFriend = expressions.filter((e) => e.sourceFriendId === friend.id).length;
  const [fishVoices, setFishVoices] = useState<FishVoice[]>(() => cachedFishVoices());
  useEffect(() => {
    if (!isLocalMode()) return;
    let alive = true;
    void loadFishVoices().then((list) => {
      if (alive) setFishVoices(list);
    });
    return () => {
      alive = false;
    };
  }, []);

  const currentVoice = friend.voice || (friend.gender === "male" ? "alex" : "anna");

  const switchVoice = async (voice: string) => {
    if (!voice) return;
    const ok = await apiSetFriendVoice(friend.id, voice);
    if (!ok) {
      showToast("切换失败，稍后再试", "error");
      return;
    }
    const meta = COSY_VOICES.find((v) => v.id === voice);
    updateFriendVoice(friend.id, voice, meta ? meta.label : "默认");
    showToast(`已换成 ${meta ? meta.label : voice}`);
  };

  const previewCurrent = async () => {
    const url = await previewVoice("Hi! This is how I sound when we talk.", currentVoice);
    if (!url) {
      showToast("试听失败：检查网络或 Key", "error");
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
          <select
            className="w4-select"
            value={friend.voice.startsWith("fish:") || COSY_VOICES.some((v) => v.id === friend.voice) ? friend.voice : ""}
            onChange={(e) => void switchVoice(e.target.value)}
            style={{ marginBottom: 8 }}
          >
            {!COSY_VOICES.some((v) => v.id === friend.voice) && !friend.voice.startsWith("fish:") ? (
              <option value="">当前：默认音色</option>
            ) : null}
            {fishVoices.length ? (
              <optgroup label={`Fish 音色库（${fishVoices.length} 个在线音色）`}>
                {fishVoices.map((v) => (
                  <option key={v.id} value={`fish:${v.id}`}>
                    {v.gender === "male" ? "男" : "女"} · {v.label}
                  </option>
                ))}
              </optgroup>
            ) : null}
            <optgroup label="内置音色">
              {COSY_VOICES.filter((v) => v.gender === "female").map((v) => (
                <option key={v.id} value={v.id}>
                  {v.label}
                </option>
              ))}
            </optgroup>
            <optgroup label="男声">
              {COSY_VOICES.filter((v) => v.gender === "male").map((v) => (
                <option key={v.id} value={v.id}>
                  {v.label}
                </option>
              ))}
            </optgroup>
          </select>
          <div className="w4-chips">
            <button
              className="w4-chip"
              onClick={() => {
                if (fishVoices.length) {
                  const v = fishVoices[Math.floor(Math.random() * fishVoices.length)];
                  void switchVoice(`fish:${v.id}`);
                  return;
                }
                const v = COSY_VOICES[Math.floor(Math.random() * COSY_VOICES.length)];
                void switchVoice(v.id);
              }}
            >
              随机
            </button>
            <button className="w4-chip" onClick={() => void previewCurrent()}>
              试听
            </button>
          </div>
          <div style={{ fontSize: 12, color: "var(--w-text-3)", marginTop: 4 }}>
            换完发一条消息或打一次电话就能听到。
            {fishVoices.length === 0 ? "（Fish 音色库需要 App 内使用；浏览器受跨域限制）" : ""}
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
