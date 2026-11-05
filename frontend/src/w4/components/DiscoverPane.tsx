"use client";

/**
 * 发现：一个「偶遇」，一个「动态」。
 *
 * 桌面端照微信：左列是二级入口，右边是内容。
 * 手机端：进来先看到两个入口（跟微信「发现」一样的分组列表），
 * 点进去才是内容。
 */

import { useEffect, useRef, useState } from "react";
import {
  ChevronRight,
  Compass,
  Heart,
  ImagePlus,
  Languages,
  MessageSquare,
  Plus,
  Trash2,
} from "lucide-react";

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
import { Avatar, Dialog, Icon, Lightbox } from "../ui";
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

const MOODS = ["☕ 日常", "🌿 散步", "📚 在读书", "🍜 吃了点好的", "🌙 夜里", "🎧 在听歌"];

const STOCK = [
  "/mock/photos/cafe.jpg",
  "/mock/photos/brunch.jpg",
  "/mock/photos/flowers.jpg",
  "/mock/photos/records.jpg",
  "/mock/photos/beach.jpg",
  "/mock/photos/night.jpg",
];

type Pane = "index" | "moments" | "people";

export function DiscoverPane({
  navigate,
  pane,
}: {
  navigate: (r: Route) => void;
  pane?: "moments" | "people";
}) {
  const [local, setLocal] = useState<Pane>(pane ?? "index");
  const active: Pane = pane ?? local;

  if (active === "moments") {
    return <MomentsView navigate={navigate} onBack={() => setLocal("index")} />;
  }
  if (active === "people") {
    return <PeopleView navigate={navigate} onBack={() => setLocal("index")} />;
  }

  return (
    <div className="w4-main">
      <div className="w4-head">
        <div className="w4-head-main">
          <div className="w4-head-title">发现</div>
        </div>
      </div>
      <div className="w4-scroll">
        <div className="w4-index">
          <div className="w4-cells">
            <button className="w4-cell" onClick={() => setLocal("moments")}>
              <span className="w4-cell-icon" style={{ background: "#e8f0f6", color: "#3d6b8f" }}>
                <ImagePlus size={15} strokeWidth={1.8} />
              </span>
              <span className="w4-cell-label">
                动态
                <span className="w4-cell-desc">看看好友们此刻在做什么</span>
              </span>
              <ChevronRight size={15} className="w4-cell-chevron" />
            </button>
            <button className="w4-cell" onClick={() => setLocal("people")}>
              <span className="w4-cell-icon" style={{ background: "#eef4ea", color: "var(--w-green-ink)" }}>
                <Compass size={15} strokeWidth={1.8} />
              </span>
              <span className="w4-cell-label">
                偶遇一个人
                <span className="w4-cell-desc">每次一位，先听听 TA 的声音</span>
              </span>
              <ChevronRight size={15} className="w4-cell-chevron" />
            </button>
          </div>
          <div className="w4-index-note">
            动态里的好友是按你们的熟稔度和时区挑的，不会一次刷出一百条。
          </div>
        </div>
      </div>
    </div>
  );
}

/* ================================================================== 动态 */

function MomentsView({ navigate, onBack }: { navigate: (r: Route) => void; onBack: () => void }) {
  const {
    moments,
    friends,
    profile,
    zhOpen,
    toggleZh,
    translateAllMoments,
    likeMoment,
    addComment,
    deleteMoment,
    publishMoment,
    showToast,
    generateMoments,
  } = useApp();
  const [publishOpen, setPublishOpen] = useState(false);
  const [deleteFor, setDeleteFor] = useState<string | null>(null);
  /** 评论翻译：key = momentId:commentId */
  const [commentZh, setCommentZh] = useState<Record<string, string>>({});
  const [commentZhOpen, setCommentZhOpen] = useState<Record<string, boolean>>({});
  const [commentZhLoading, setCommentZhLoading] = useState<Record<string, boolean>>({});

  const toggleCommentZh = async (momentId: string, commentId: string, text: string, friendId: string) => {
    const key = `${momentId}:${commentId}`;
    if (commentZhOpen[key]) {
      setCommentZhOpen((m) => ({ ...m, [key]: false }));
      return;
    }
    if (commentZh[key]) {
      setCommentZhOpen((m) => ({ ...m, [key]: true }));
      return;
    }
    setCommentZhLoading((m) => ({ ...m, [key]: true }));
    const out = await apiTranslate(text, friendId).catch(() => null);
    setCommentZhLoading((m) => ({ ...m, [key]: false }));
    if (!out) return;
    setCommentZh((m) => ({ ...m, [key]: out }));
    setCommentZhOpen((m) => ({ ...m, [key]: true }));
  };
  const [zoom, setZoom] = useState<string | null>(null);
  const [commentFor, setCommentFor] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  /**
   * 朋友圈作者。
   *
   * mock/后端的 moment 里 authorId 不一定能对上真实好友，
   * 直接回退成「好友」会让每一条都顶着同一个灰色首字母块（视觉上像坏了）。
   * 这里改成：对不上就按 id 稳定地映射到某位真实好友 ——
   * 同一条动态永远是同一个人，不同动态尽量不同人，且一定有头像和名字。
   */
  const personOf = (id: string) => {
    if (id === "me") return { name: profile.name, avatar: profile.avatar, flag: "" };
    const hit = friends.find((x) => x.id === id);
    if (hit) return { name: hit.name, avatar: hit.avatar, flag: hit.flag };
    if (!friends.length) return { name: "好友", avatar: "", flag: "" };
    let h = 0;
    for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 99991;
    const f = friends[h % friends.length];
    return { name: f.name, avatar: f.avatar, flag: f.flag };
  };

  return (
    <div className="w4-main">
      <div className="w4-head">
        <button className="w4-icon-btn" onClick={onBack} aria-label="返回">
          <Icon strokeWidth={2}>
            <path d="M15 5l-7 7 7 7" />
          </Icon>
        </button>
        <div className="w4-head-main">
          <div className="w4-head-title">动态</div>
        </div>
        <div className="w4-head-actions">
          <button className="w4-icon-btn" onClick={translateAllMoments} title="全部译成中文" aria-label="全部翻译">
            <Languages size={16} strokeWidth={1.8} />
          </button>
          <button
            className="w4-btn is-primary"
            style={{ height: 30, padding: "0 11px", fontSize: 12.5, gap: 5 }}
            onClick={() => setPublishOpen(true)}
          >
            <ImagePlus size={14} />
            发一条
          </button>
        </div>
      </div>

      <div className="w4-scroll">
        <div className="w4-feed">
          <div className="w4-feed-cover">
            <img src="/mock/photos/cover.jpg" alt="" />
            <div className="w4-feed-me">
              <span className="w4-feed-me-name">{profile.name}</span>
              <span className="w4-feed-me-avatar">
                <img src={profile.avatar} alt="" />
              </span>
            </div>
          </div>

          {moments.length === 0 ? (
            <div className="w4-feed-empty">
              <div className="w4-feed-empty-icon">
                <ImagePlus size={22} strokeWidth={1.6} />
              </div>
              <div className="w4-feed-empty-title">今天还没人发动态</div>
              <div className="w4-feed-empty-text">
                让好友们发几条，你就能看到他们此刻在做什么 —— 也可以直接评论，聊起来。
              </div>
              <button className="w4-btn is-primary" onClick={() => void generateMoments()}>
                让好友们发几条
              </button>
            </div>
          ) : null}

          {moments.map((m) => {
            const author = personOf(m.authorId);
            const mine = m.authorId === "me";
            const zhShown = !!zhOpen[`mo:${m.id}`];
            return (
              <article className="w4-moment" key={m.id}>
                <button
                  onClick={() => !mine && navigate({ name: "contact", friendId: m.authorId })}
                  aria-label={author.name}
                >
                  <Avatar src={author.avatar} name={author.name} size={42} />
                </button>
                <div className="w4-moment-body">
                  <div className={`w4-moment-name ${mine ? "is-me" : ""}`}>
                    {author.name}
                    {author.flag ? <span style={{ marginLeft: 3, fontSize: 12 }}>{author.flag}</span> : null}
                  </div>
                  {m.text ? <div className="w4-moment-text">{m.text}</div> : null}
                  {zhShown && m.translation ? <div className="w4-moment-zh">{m.translation}</div> : null}

                  {m.images.length ? (
                    <div className={`w4-moment-imgs n${Math.min(4, m.images.length)}`}>
                      {m.images.map((src) => (
                        <img key={src} src={src} alt="" onClick={() => setZoom(src)} />
                      ))}
                    </div>
                  ) : null}

                  <div className="w4-moment-foot">
                    <span>{m.time}</span>
                    {m.mood ? <span>{m.mood}</span> : null}
                    <span style={{ flex: 1 }} />
                    {m.translation ? (
                      <button className="w4-moment-act" onClick={() => toggleZh(`mo:${m.id}`)}>
                        <Languages size={13} strokeWidth={1.8} />
                        {zhShown ? "收起" : "翻译"}
                      </button>
                    ) : null}
                    <button
                      className={`w4-moment-act ${m.liked ? "is-on" : ""}`}
                      onClick={() => likeMoment(m.id)}
                    >
                      <Heart size={13} strokeWidth={1.8} fill={m.liked ? "currentColor" : "none"} />
                      赞
                    </button>
                    <button
                      className="w4-moment-act"
                      onClick={() => setCommentFor(commentFor === m.id ? null : m.id)}
                    >
                      <MessageSquare size={13} strokeWidth={1.8} />
                      评论
                    </button>
                    {mine ? (
                      <button className="w4-moment-act is-danger" onClick={() => setDeleteFor(m.id)}>
                        <Trash2 size={13} strokeWidth={1.8} />
                        删除
                      </button>
                    ) : null}
                  </div>

                  {m.likedBy.length > 0 || m.comments.length > 0 || m.commentsPending ? (
                    <div className="w4-moment-interact">
                      {m.likedBy.length > 0 ? (
                        <div className="w4-likes">
                          <span className="w4-likes-icon">
                            <Heart size={9} strokeWidth={0} fill="currentColor" />
                          </span>
                          <span className="w4-like-row">
                            {m.likedBy.map((id) => (
                              <span key={id} className="w4-like-name">
                                {personOf(id).name}
                              </span>
                            ))}
                          </span>
                        </div>
                      ) : null}
                      {m.comments.length || m.commentsPending ? (
                        <div className="w4-moment-comments">
                          {m.comments.map((c) => {
                            const key = `${m.id}:${c.id}`;
                            const canTranslate = c.friendId !== "me";
                            const zh = commentZh[key];
                            const open = !!commentZhOpen[key];
                            const loading = !!commentZhLoading[key];
                            return (
                              <div key={c.id} className={`w4-moment-comment ${c.friendId === "me" ? "is-me" : ""}`}>
                                <b>{personOf(c.friendId).name}</b>
                                {c.friendId === "me" ? "" : "："}
                                {c.text}
                                {canTranslate ? (
                                  <button
                                    className="w4-comment-tr"
                                    onClick={() => void toggleCommentZh(m.id, c.id, c.text, c.friendId)}
                                  >
                                    {loading ? "翻译中…" : open ? "收起翻译" : "翻译"}
                                  </button>
                                ) : null}
                                {open && zh ? <div className="w4-comment-zh">{zh}</div> : null}
                              </div>
                            );
                          })}
                          {m.commentsPending ? (
                            <div className="w4-comment-pending">
                              <span className="w4-typing">
                                <i />
                                <i />
                                <i />
                              </span>
                              好友们正在看…
                            </div>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                  ) : null}

                  {commentFor === m.id ? (
                    <div className="w4-comment-input">
                      <input
                        autoFocus
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && !e.nativeEvent.isComposing && draft.trim()) {
                            addComment(m.id, draft.trim());
                            setDraft("");
                            setCommentFor(null);
                          }
                        }}
                        placeholder={mine ? "补充一句…" : `回复 ${author.name}…`}
                      />
                      <button
                        className="w4-btn is-primary"
                        style={{ height: 28, padding: "0 10px", fontSize: 12 }}
                        disabled={!draft.trim()}
                        onClick={() => {
                          addComment(m.id, draft.trim());
                          setDraft("");
                          setCommentFor(null);
                        }}
                      >
                        发送
                      </button>
                    </div>
                  ) : null}
                </div>
              </article>
            );
          })}
          <div style={{ height: 24 }} />
        </div>
      </div>

      {zoom ? <Lightbox src={zoom} onClose={() => setZoom(null)} /> : null}

      {publishOpen ? (
        <PublishDialog
          onClose={() => setPublishOpen(false)}
          onPublish={(text, images, mood) => {
            publishMoment(text, images, mood);
            setPublishOpen(false);
            showToast("已发布，好友们会来评论", "success");
          }}
        />
      ) : null}

      {deleteFor ? (
        <Dialog
          title="删除这条动态？"
          onClose={() => setDeleteFor(null)}
          footer={
            <>
              <button className="w4-btn" onClick={() => setDeleteFor(null)}>
                取消
              </button>
              <button
                className="w4-btn is-primary"
                onClick={() => {
                  const id = deleteFor;
                  setDeleteFor(null);
                  void deleteMoment(id);
                }}
              >
                删除
              </button>
            </>
          }
        >
          <div style={{ fontSize: 13, lineHeight: 1.7, color: "var(--w-text-2)" }}>
            删除后这条动态和下面的评论一起消失，无法恢复。
          </div>
        </Dialog>
      ) : null}
    </div>
  );
}

function PublishDialog({
  onClose,
  onPublish,
}: {
  onClose: () => void;
  onPublish: (text: string, images: string[], mood: string) => void;
}) {
  const [text, setText] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [mood, setMood] = useState(MOODS[0]);

  return (
    <Dialog
      title="发动态"
      onClose={onClose}
      footer={
        <>
          <button className="w4-btn" onClick={onClose}>
            取消
          </button>
          <button
            className="w4-btn is-primary"
            disabled={!text.trim() && picked.length === 0}
            onClick={() => onPublish(text.trim(), picked, mood)}
          >
            发布
          </button>
        </>
      }
    >
      <textarea
        className="w4-textarea"
        style={{ border: 0, padding: 0, fontSize: 14.5, minHeight: 96, background: "transparent" }}
        placeholder="这一刻的想法…"
        value={text}
        onChange={(e) => setText(e.target.value)}
        autoFocus
      />
      <div className="w4-publish-pics">
        {STOCK.map((p) => {
          const on = picked.includes(p);
          return (
            <button
              key={p}
              className={`w4-publish-pic ${on ? "on" : ""}`}
              onClick={() =>
                setPicked((list) => (on ? list.filter((x) => x !== p) : [...list, p].slice(0, 9)))
              }
            >
              <img src={p} alt="" />
              {on ? <span className="w4-pick-order">{picked.indexOf(p) + 1}</span> : null}
            </button>
          );
        })}
      </div>
      <div className="w4-field" style={{ marginTop: 14 }}>
        <div className="w4-field-label">心情</div>
        <div className="w4-chips">
          {MOODS.map((m) => (
            <button
              key={m}
              className={`w4-chip ${mood === m ? "is-green" : ""}`}
              style={{ cursor: "pointer" }}
              onClick={() => setMood(m)}
            >
              {m}
            </button>
          ))}
        </div>
      </div>
      <div className="w4-field-hint">发出去之后，关系近的好友会按各自的时区和语言来评论。</div>
    </Dialog>
  );
}

/* ================================================================== 偶遇 */

function PeopleView({ navigate, onBack }: { navigate: (r: Route) => void; onBack: () => void }) {
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
        <button className="w4-icon-btn" onClick={onBack} aria-label="返回">
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
