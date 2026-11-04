"use client";

/**
 * 发现：一个「偶遇」，一个「动态」。
 *
 * 桌面端照微信：左列是二级入口，右边是内容。
 * 手机端：进来先看到两个入口（跟微信「发现」一样的分组列表），
 * 点进去才是内容。
 */

import { useState } from "react";
import {
  ChevronRight,
  Compass,
  Heart,
  ImagePlus,
  Languages,
  MessageSquare,
  Plus,
} from "lucide-react";

import { LANGUAGES } from "../format";
import type { Route } from "../model";
import { useApp } from "../store";
import { Avatar, Dialog, Icon, Lightbox } from "../ui";
import { DiscoverCard } from "./DiscoverCard";

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
    publishMoment,
    showToast,
    generateMoments,
  } = useApp();
  const [publishOpen, setPublishOpen] = useState(false);
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
                          {m.comments.map((c) => (
                            <div key={c.id} className={`w4-moment-comment ${c.friendId === "me" ? "is-me" : ""}`}>
                              <b>{personOf(c.friendId).name}</b>
                              {c.friendId === "me" ? "" : "："}
                              {c.text}
                            </div>
                          ))}
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
  const { discoverPool, addDiscovered, friends } = useApp();
  const [language, setLanguage] = useState("");
  const [country, setCountry] = useState("");
  const [offset, setOffset] = useState(0);
  const [playing, setPlaying] = useState(false);

  const countries = [...new Map(discoverPool.map((c) => [c.country, c.flag])).entries()];

  let pool = discoverPool;
  if (language) pool = pool.filter((c) => c.language === language);
  if (country) pool = pool.filter((c) => c.country === country);
  const current = pool.length ? pool[offset % pool.length] : null;
  const already = current ? friends.some((f) => f.id === current.id) : false;

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
                setCountry("");
                setOffset(0);
                setPlaying(false);
              }}
            >
              不限
            </button>
            {LANGUAGES.slice(0, 6).map((l) => (
              <button
                key={l.code}
                className={`w4-filter ${language === l.code ? "on" : ""}`}
                onClick={() => {
                  setLanguage(language === l.code ? "" : l.code);
                  setCountry("");
                  setOffset(0);
                  setPlaying(false);
                }}
              >
                {l.label}
              </button>
            ))}
          </div>

          <div className="w4-filter-row" style={{ marginTop: -6 }}>
            <span className="w4-filter-label">国家</span>
            <button
              className={`w4-filter ${!country ? "on" : ""}`}
              onClick={() => {
                setCountry("");
                setOffset(0);
                setPlaying(false);
              }}
            >
              不限
            </button>
            {countries.map(([name, flag]) => (
              <button
                key={name}
                className={`w4-filter ${country === name ? "on" : ""}`}
                onClick={() => {
                  setCountry(country === name ? "" : name);
                  setOffset(0);
                  setPlaying(false);
                }}
              >
                {flag} {name}
              </button>
            ))}
          </div>

          {!current ? (
            <div className="w4-card" style={{ padding: 30, textAlign: "center" }}>
              <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>这个组合暂时没有合适的人</div>
              <div style={{ fontSize: 12.5, color: "var(--w-text-3)", marginBottom: 16 }}>
                换一种语言，或者取消国家筛选试试。
              </div>
              <button
                className="w4-btn is-primary"
                onClick={() => {
                  setLanguage("");
                  setCountry("");
                  setOffset(0);
                }}
              >
                清空筛选
              </button>
            </div>
          ) : (
            <DiscoverCard
              candidate={current}
              playing={playing}
              already={already}
              onPlay={() => setPlaying((v) => !v)}
              onNext={() => {
                setOffset((o) => o + 1);
                setPlaying(false);
              }}
              onAdd={() => {
                const f = addDiscovered(current);
                navigate({ name: "chat", friendId: f.id });
              }}
              onOpen={() => navigate({ name: "chat", friendId: current.id })}
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
