"use client";

/**
 * 发现 · 偶遇一个人（保留 V2 的「捏人」能力，作为信息流之外的补充入口）。
 *
 * V3 起「发现」首页是 Persona 信息流（见 FeedView）；这里只负责「随机认识一个新人」：
 * 后端 spawner 现捏一个 + 生成第一条招呼语音，接受后进入聊天。
 */

import { useEffect, useRef, useState } from "react";

import {
  acceptFriend as apiAcceptFriend,
  declineFriend as apiDeclineFriend,
  discover as apiDiscover,
  discoverLanguages as apiDiscoverLanguages,
  translate as apiTranslate,
} from "../apiFacade";
import { LANGUAGES } from "../format";
import type { Route } from "../model";
import type { DiscoverCandidate } from "../mock";
import { useApp } from "../store";
import { Icon } from "../ui";
import { DiscoverCard } from "./DiscoverCard";

/** 后端语音 id → 口音标签（en-US-Andrew... → 美式口音） */
const ACCENT_BY_VOICE: Record<string, string> = {
  "en-US": "美式口音",
  "en-GB": "英式口音",
  "en-AU": "澳洲口音",
  "en-NZ": "新西兰口音",
  "en-CA": "加拿大口音",
  "en-IE": "爱尔兰口音",
  "es-ES": "西班牙口音",
  "de-DE": "德语口音",
  "fr-FR": "法语口音",
  "it-IT": "意大利口音",
  "pt-BR": "巴西口音",
  "pt-PT": "葡萄牙口音",
  "ja-JP": "日语口音",
  "ko-KR": "韩语口音",
};

function accentLabelOf(voiceId: string): string {
  return ACCENT_BY_VOICE[(voiceId || "").slice(0, 5)] ?? "";
}

export function PeopleView({ navigate }: { navigate: (r: Route) => void }) {
  const { friends, reloadAll } = useApp();
  const [language, setLanguage] = useState("");
  const [candidate, setCandidate] = useState<DiscoverCandidate | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const seq = useRef(0);
  const pendingRef = useRef<string | null>(null);
  const [langOptions, setLangOptions] = useState<{ code: string; label: string }[]>(
    LANGUAGES.map((l) => ({ code: l.code, label: l.label })),
  );

  /* 语言列表来自后端 /discover/languages：只列真实声线能说的语言 */
  useEffect(() => {
    void (async () => {
      const codes = await apiDiscoverLanguages();
      if (!codes || !codes.length) return;
      const known = new Map(LANGUAGES.map((l) => [l.code, l.label]));
      const EXTRA: Record<string, string> = { zh: "中文", en: "英语", de: "德语" };
      setLangOptions(
        codes.map((code) => ({
          code,
          label: known.get(code) ?? EXTRA[code] ?? code.toUpperCase(),
        })),
      );
    })();
  }, []);

  const stopAudio = () => {
    audioRef.current?.pause();
    audioRef.current = null;
    setPlaying(false);
  };

  /** 真实捏人：后端 spawner 造一个人 + 生成第一条打招呼语音（10–30 秒） */
  const pickOne = async (lang: string) => {
    const my = ++seq.current;
    setLoading(true);
    setError("");
    setCandidate(null);
    stopAudio();
    /* 上一个没接受的偶遇：从好友申请里撤掉，避免越攒越多 */
    if (pendingRef.current) {
      void apiDeclineFriend(pendingRef.current);
      pendingRef.current = null;
    }
    try {
      const res = await apiDiscover(lang || undefined);
      if (seq.current !== my) return;
      if (!res) throw new Error("discover 失败");
      const f = res.friend;
      let zh = "";
      if (res.greeting) {
        zh = (await apiTranslate(res.greeting, f.id).catch(() => null)) ?? "";
      }
      pendingRef.current = f.id;
      if (seq.current !== my) return;
      setCandidate({
        id: f.id,
        name: f.name,
        avatar: f.avatar,
        age: f.age ?? 0,
        city: f.city,
        country: f.country,
        flag: f.flag,
        language: f.language,
        languageLabel: f.languageLabel,
        occupation: f.occupation ?? "",
        bio: f.bio || f.localActivity || "",
        greeting: res.greeting || "…",
        greetingZh: zh,
        greetingAudio: res.greetingAudio,
        voiceLabel: f.voiceLabel,
        accent: accentLabelOf(f.voice ?? f.voiceLabel),
        online: Boolean(f.online),
        tags: [],
      });
    } catch {
      if (seq.current === my) setError("这次没捏出来，再试一次");
    } finally {
      if (seq.current === my) setLoading(false);
    }
  };

  useEffect(() => {
    void pickOne("");
    return () => {
      seq.current += 1;
      audioRef.current?.pause();
      /* 离开偶遇页还没接受的，不要留在好友申请里 */
      if (pendingRef.current) {
        void apiDeclineFriend(pendingRef.current);
        pendingRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const togglePlay = () => {
    if (!candidate?.greetingAudio) return;
    if (playing) {
      stopAudio();
      return;
    }
    const a = new Audio(candidate.greetingAudio);
    audioRef.current = a;
    a.onended = () => setPlaying(false);
    a.onerror = () => setPlaying(false);
    /* 只有真的播起来才点亮按钮，避免"显示播放中但没声音" */
    a.play()
      .then(() => setPlaying(true))
      .catch(() => setPlaying(false));
  };

  const already = candidate ? friends.some((f) => f.id === candidate.id) : false;

  /** 「认识 TA」：先接受好友申请，再进聊天 */
  const openChat = async () => {
    if (!candidate) return;
    stopAudio();
    await apiAcceptFriend(candidate.id);
    pendingRef.current = null;
    await reloadAll();
    navigate({ name: "chat", friendId: candidate.id });
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
          <div className="w4-head-sub">每次一位，先听 TA 说一句</div>
        </div>
      </div>

      <div className="w4-scroll">
        <div className="w4-discover">
          <div className="w4-filter-row">
            <span className="w4-filter-label">语言</span>
            <button
              className={`w4-filter ${!language ? "on" : ""}`}
              onClick={() => {
                setLanguage("");
                void pickOne("");
              }}
            >
              不限
            </button>
            {langOptions.map((l) => (
              <button
                key={l.code}
                className={`w4-filter ${language === l.code ? "on" : ""}`}
                onClick={() => {
                  setLanguage(l.code);
                  void pickOne(l.code);
                }}
              >
                {l.label}
              </button>
            ))}
          </div>

          {loading ? (
            <div className="w4-card w4-deck-loading">
              <div className="w4-deck-spinner" aria-hidden />
              <div className="w4-deck-loading-title">正在找一位新朋友…</div>
              <div className="w4-deck-loading-sub">
                要现捏一个人、配好声音，再让 TA 说第一句，通常 10–30 秒
              </div>
            </div>
          ) : error || !candidate ? (
            <div className="w4-card" style={{ padding: 30, textAlign: "center" }}>
              <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 12 }}>
                {error || "这次没找到合适的人"}
              </div>
              <button className="w4-btn is-primary" onClick={() => void pickOne(language)}>
                再试一次
              </button>
            </div>
          ) : (
            <DiscoverCard
              candidate={candidate}
              playing={playing}
              already={already}
              onPlay={togglePlay}
              onNext={() => void pickOne(language)}
              onAdd={() => void openChat()}
              onOpen={() => void openChat()}
            />
          )}

          <div className="w4-deck-note">
            <Icon size={12} style={{ verticalAlign: -2 }}>
              <circle cx="12" cy="12" r="9" />
              <path d="M12 11v5M12 8h.01" />
            </Icon>{" "}
            偶遇的人来自真实声线库。认识了之后，TA 会记得你说过的事。
          </div>
        </div>
      </div>
    </div>
  );
}
