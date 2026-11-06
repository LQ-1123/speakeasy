"use client";

/**
 * 发现 · 偶遇一个人。
 *
 * V3 起「捏人」发生在**世界生成**阶段（一次生成人物 + 信息流）。
 * 这里不再调用 spawner 现场捏人，而是**从已生成的世界里挑一位还没加好友的人物**：
 * 头像、资料、声音都和信息流里完全一致。
 */

import { useEffect, useRef, useState } from "react";

import { API_ORIGIN } from "../api";
import { personaAvatarSrc } from "../avatar";
import type { Route } from "../model";
import type { DiscoverCandidate } from "../mock";
import { useApp } from "../store";
import { Icon } from "../ui";
import {
  addFriend as apiAddFriend,
  fetchDiscoverNext,
  languageLabel,
  personaSpeechUrl,
  translateText,
  type DiscoverNext,
} from "../v3/api";
import { DiscoverCard } from "./DiscoverCard";

const COUNTRY_FLAG: Array<[string, string]> = [
  ["United Kingdom", "🇬🇧"], ["United States", "🇺🇸"], ["Australia", "🇦🇺"],
  ["Brazil", "🇧🇷"], ["Ireland", "🇮🇪"], ["Italy", "🇮🇹"], ["Japan", "🇯🇵"],
  ["Spain", "🇪🇸"], ["Korea", "🇰🇷"], ["China", "🇨🇳"], ["Singapore", "🇸🇬"],
  ["Germany", "🇩🇪"],
];

function flagOf(country: string): string {
  return COUNTRY_FLAG.find(([k]) => (country || "").includes(k))?.[1] ?? "🌍";
}

function toCandidate(res: DiscoverNext, zh: string): DiscoverCandidate {
  const p = res.persona;
  return {
    id: p.id,
    name: p.nickname,
    avatar: personaAvatarSrc(p.avatar_seed || p.id, p.avatar_style, p.avatar_options, p.avatar),
    age: p.age ?? 0,
    city: p.city,
    country: p.country,
    flag: flagOf(p.country),
    language: p.language,
    languageLabel: languageLabel(p.language),
    occupation: p.occupation ?? "",
    bio: p.bio || "",
    greeting: res.greeting || "…",
    greetingZh: zh,
    /* 用**帖子语言对应的声线**朗读 TA 最新那条动态（后端合成并缓存） */
    greetingAudio: res.post_id
      ? personaSpeechUrl(res.post_id, res.post_hash)
      : undefined,
    voiceLabel: "",
    accent: "",
    online: false,
    tags: Array.from(
      new Set([...(res.match_tags || []), ...(p.interests || [])]),
    ).slice(0, 3),
  };
}

export function PeopleView({ navigate }: { navigate: (r: Route) => void }) {
  const { reloadAll } = useApp();
  const [candidate, setCandidate] = useState<DiscoverCandidate | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [playing, setPlaying] = useState(false);
  const [adding, setAdding] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const seq = useRef(0);

  const stopAudio = () => {
    audioRef.current?.pause();
    audioRef.current = null;
    setPlaying(false);
  };

  const pickOne = async () => {
    const my = ++seq.current;
    setLoading(true);
    setError("");
    setCandidate(null);
    stopAudio();
    try {
      const res = await fetchDiscoverNext();
      const zh = res.greeting
        ? (await translateText(res.greeting, "zh").catch(() => "")) ?? ""
        : "";
      if (seq.current !== my) return;
      setCandidate(toCandidate(res, zh));
    } catch {
      if (seq.current === my) setError("这次没找到合适的人，再试一次");
    } finally {
      if (seq.current === my) setLoading(false);
    }
  };

  useEffect(() => {
    void pickOne();
    return () => {
      seq.current += 1;
      audioRef.current?.pause();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* 偶遇卡片出现时就把「听 TA 的声音」预取到浏览器缓存 */
  useEffect(() => {
    if (!candidate?.greetingAudio) return;
    const audio = new Audio();
    audio.preload = "auto";
    audio.src = candidate.greetingAudio;
    return () => {
      audio.src = "";
    };
  }, [candidate?.greetingAudio]);

  const togglePlay = () => {
    if (!candidate?.greetingAudio) return;
    if (playing) {
      stopAudio();
      return;
    }
    const a = new Audio(candidate.greetingAudio.startsWith("http") ? candidate.greetingAudio : `${API_ORIGIN}${candidate.greetingAudio}`);
    audioRef.current = a;
    a.onended = () => setPlaying(false);
    a.onerror = () => setPlaying(false);
    a.play()
      .then(() => setPlaying(true))
      .catch(() => setPlaying(false));
  };

  /** 加好友（幂等）→ 刷新好友列表 → 进入聊天 */
  const startChat = async () => {
    if (!candidate || adding) return;
    setAdding(true);
    stopAudio();
    try {
      const res = await apiAddFriend(candidate.id);
      await reloadAll();
      if (res?.friend_id) navigate({ name: "chat", friendId: res.friend_id });
      else navigate({ name: "persona", personaId: candidate.id });
    } catch {
      setError("加好友失败，请稍后重试");
    } finally {
      setAdding(false);
    }
  };

  return (
    <div className="w4-main">
      <div className="w4-head">
        <button className="w4-icon-btn" onClick={() => navigate({ name: "discover" })} aria-label="返回">
          <Icon strokeWidth={2}>
            <path d="M15 5l-7 7 7 7" />
          </Icon>
        </button>
        <div className="w4-head-main">
          <div className="w4-head-title">偶遇</div>
          <div className="w4-head-sub">从信息流的世界里认识一个人，先听 TA 的声音</div>
        </div>
      </div>

      <div className="w4-scroll">
        <div className="w4-discover">
          <div className="v3-discover-note">
            这些人就是信息流里的人物：头像、资料、声音完全一致；点「换一个」继续浏览。
          </div>

          {loading ? (
            <div className="w4-card w4-deck-loading">
              <div className="w4-deck-spinner" aria-hidden />
              <div className="w4-deck-loading-title">正在从世界里挑一个人…</div>
              <div className="w4-deck-loading-sub">不会再现场捏人，所以很快</div>
            </div>
          ) : error || !candidate ? (
            <div className="w4-card" style={{ padding: 30, textAlign: "center" }}>
              <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 12 }}>
                {error || "这次没找到合适的人"}
              </div>
              <button className="w4-btn is-primary" onClick={() => void pickOne()}>
                再试一次
              </button>
            </div>
          ) : (
            <DiscoverCard
              candidate={candidate}
              playing={playing}
              already={false}
              onPlay={togglePlay}
              onNext={() => void pickOne()}
              onAdd={() => void startChat()}
              onOpen={() => void startChat()}
            />
          )}

          <div className="w4-deck-note">
            <Icon size={12} style={{ verticalAlign: -2 }}>
              <circle cx="12" cy="12" r="9" />
              <path d="M12 11v5M12 8h.01" />
            </Icon>{" "}
            世界在信息流里一次生成：人物、性格、动态和声音都是同一批；偶遇只是从里面挑人。
          </div>
        </div>
      </div>
    </div>
  );
}
