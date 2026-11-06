"use client";

/**
 * V3 · 发现信息流（以 AI Persona 为中心的社交世界）。
 *
 * - 信息流 = 平台 Persona 的公共动态 + 用户自己的动态（公开动态也会互相可见）
 * - 点作者头像/昵称 → Persona 主页；未加好友的人也会出现在信息流里
 * - 支持点赞 / 评论 / 发动态（可见性三档）/ 通知中心（社交事件）
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bell,
  Compass,
  Heart,
  ImagePlus,
  Languages,
  MessageSquare,
  Pause,
  PenSquare,
  RefreshCw,
  Send,
  Trash2,
  UserPlus,
  Volume2,
} from "lucide-react";

import { playVoiceUrl } from "../apiFacade";
import { personaAvatarSrc } from "../avatar";
import { isLocalMode } from "../local/keys";
import type { Route } from "../model";
import { useApp } from "../store";
import { Avatar, Dialog, Icon, Lightbox } from "../ui";
import {
  addFriend,
  createPost,
  deletePost,
  fetchEvents,
  fetchFeed,
  fetchFeedSummary,
  likePersonaPost,
  commentPersonaPost,
  personaSpeechUrl,
  relTimeOf,
  translateText,
  uploadImage,
  VISIBILITY_LABEL,
  type FeedComment,
  type FeedItem,
  type FeedSummary,
} from "./api";
import "./v3.css";

/* 演示版：用户动态只能选内置图片（不支持上传自己的照片） */
const STOCK = [
  "/mock/photos/cafe.jpg",
  "/mock/photos/brunch.jpg",
  "/mock/photos/flowers.jpg",
  "/mock/photos/records.jpg",
  "/mock/photos/beach.jpg",
  "/mock/photos/night.jpg",
  "/mock/photos/latte.jpg",
  "/mock/photos/desk.jpg",
  "/mock/photos/lisbon.jpg",
  "/mock/photos/market.jpg",
  "/mock/photos/tokyo.jpg",
  "/mock/photos/seoul.jpg",
];

const TAGS = ["日常", "工作", "运动", "美食", "音乐", "读书", "旅行", "摄影"];
const TAG_MAX = 6;
const TAG_MAX_LEN = 16;

function flagOf(country: string): string {
  const map: Array<[string, string]> = [
    ["United Kingdom", "🇬🇧"], ["United States", "🇺🇸"], ["Australia", "🇦🇺"],
    ["Brazil", "🇧🇷"], ["Ireland", "🇮🇪"], ["Italy", "🇮🇹"], ["Japan", "🇯🇵"],
    ["Spain", "🇪🇸"], ["Korea", "🇰🇷"], ["China", "🇨🇳"], ["Singapore", "🇸🇬"],
    ["Germany", "🇩🇪"],
  ];
  return map.find(([k]) => (country || "").includes(k))?.[1] ?? "🌍";
}

export function FeedView({ navigate }: { navigate: (r: Route) => void }) {
  const { reloadAll, showToast, profile } = useApp();
  const [items, setItems] = useState<FeedItem[]>([]);
  const [cursor, setCursor] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [summary, setSummary] = useState<FeedSummary | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [zoom, setZoom] = useState<string | null>(null);
  const [commentFor, setCommentFor] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const seq = useRef(0);

  const localMode = isLocalMode();

  const load = useCallback(
    async (reset: boolean, nextCursor = "") => {
      const my = ++seq.current;
      if (reset) {
        setLoading(true);
        setError("");
      } else {
        setLoadingMore(true);
      }
      try {
        const page = await fetchFeed(reset ? undefined : nextCursor || undefined, 10);
        if (seq.current !== my) return;
        setItems((prev) => (reset ? page.items : [...prev, ...page.items]));
        setCursor(page.next_cursor || "");
        void fetchFeedSummary().then((s) => setSummary(s)).catch(() => undefined);
      } catch (exc) {
        if (seq.current !== my) return;
        setError(exc instanceof Error ? exc.message : "加载失败");
      } finally {
        if (seq.current === my) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    if (localMode) {
      setLoading(false);
      return;
    }
    void load(true);
  }, [load, localMode]);

  /* 毫秒级朗读：首屏几条人物动态的音频先在后台取到浏览器缓存，
     用户点「朗读」时几乎无网络等待（后端也已提前合成 + 内容寻址 URL）。 */
  useEffect(() => {
    if (!items.length) return;
    const prefetch = items
      .filter((it) => it.kind === "persona_post")
      .slice(0, 4)
      .map((it) => {
        const audio = new Audio();
        audio.preload = "auto";
        audio.src = personaSpeechUrl(it.id, it.content_hash);
        return audio;
      });
    return () => {
      for (const audio of prefetch) audio.src = "";
    };
  }, [items]);

  /* 社交事件轮询：更新未读数，并在有新事件时提示 */
  const lastEventCount = useRef(-1);
  useEffect(() => {
    if (localMode) return;
    let alive = true;
    const tick = async () => {
      try {
        const res = await fetchEvents(false);
        if (!alive) return;
        setSummary((prev) =>
          prev ? { ...prev, unread_events: res.unread } : prev ?? null,
        );
        if (lastEventCount.current >= 0 && res.unread > lastEventCount.current) {
          showToast("有 AI 人物回应了你的动态", "success");
        }
        lastEventCount.current = res.unread;
      } catch {
        /* 轮询失败静默 */
      }
    };
    void tick();
    const timer = window.setInterval(tick, 20000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [localMode, showToast]);

  const onScroll = (e: React.UIEvent<HTMLDivElement>) => {
    if (loading || loadingMore || !cursor) return;
    const el = e.currentTarget;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 320) {
      void load(false, cursor);
    }
  };

  const toggleLike = async (item: FeedItem) => {
    if (item.kind !== "persona_post") return;
    const before = { liked: item.liked_by_me, count: item.like_count };
    setItems((prev) =>
      prev.map((x) =>
        x.id === item.id
          ? { ...x, liked_by_me: !before.liked, like_count: before.count + (before.liked ? -1 : 1) }
          : x,
      ),
    );
    try {
      const res = await likePersonaPost(item.id);
      setItems((prev) =>
        prev.map((x) => (x.id === item.id ? { ...x, liked_by_me: res.liked, like_count: res.like_count } : x)),
      );
    } catch {
      setItems((prev) =>
        prev.map((x) => (x.id === item.id ? { ...x, liked_by_me: before.liked, like_count: before.count } : x)),
      );
      showToast("点赞失败，请稍后重试");
    }
  };

  const submitComment = async (item: FeedItem, text: string) => {
    if (!text.trim()) return;
    if (item.kind === "persona_post") {
      try {
        const c = await commentPersonaPost(item.id, text.trim());
        setItems((prev) =>
          prev.map((x) =>
            x.id === item.id
              ? { ...x, comment_count: x.comment_count + 1, comments: [...x.comments, c].slice(-3) }
              : x,
          ),
        );
      } catch {
        showToast("评论失败，请稍后重试");
      }
    } else {
      showToast("好友的评论会在对方回应时出现");
    }
    setDraft("");
    setCommentFor(null);
  };

  const quickAdd = async (item: FeedItem) => {
    try {
      await addFriend(item.author.id);
      setItems((prev) =>
        prev.map((x) =>
          x.author.id === item.author.id ? { ...x, friendship_status: "accepted" } : x,
        ),
      );
      await reloadAll();
      showToast(`已添加 ${item.author.nickname}，可以聊天了`, "success");
    } catch (exc) {
      showToast(exc instanceof Error ? exc.message : "添加失败");
    }
  };

  const removePost = async (item: FeedItem) => {
    try {
      await deletePost(item.id);
      setItems((prev) => prev.filter((x) => x.id !== item.id));
      showToast("已删除", "success");
    } catch (exc) {
      showToast(exc instanceof Error ? exc.message : "删除失败");
    }
  };

  const unread = summary?.unread_events ?? 0;

  return (
    <div className="w4-main">
      <div className="w4-head">
        <div className="w4-head-main">
          <div className="w4-head-title">发现</div>
          <div className="w4-head-sub">
            {summary ? `${summary.persona_posts} 条人物动态 · ${summary.friends} 位好友` : "AI 人物构成的社交世界"}
          </div>
        </div>
        <div className="w4-head-actions">
          <button
            className="w4-icon-btn v3-bell"
            onClick={() => navigate({ name: "events" })}
            title="通知"
            aria-label="通知"
          >
            <Bell size={17} strokeWidth={1.7} />
            {unread > 0 ? <span className="v3-badge">{unread > 99 ? "99+" : unread}</span> : null}
          </button>
          <button
            className="w4-icon-btn v3-secondary"
            onClick={() => void load(true)}
            title="刷新"
            aria-label="刷新"
          >
            <RefreshCw size={16} strokeWidth={1.7} />
          </button>
          <button
            className="w4-icon-btn v3-secondary"
            onClick={() => navigate({ name: "discoverPeople" })}
            title="偶遇一个人"
            aria-label="偶遇一个人"
          >
            <Compass size={16} strokeWidth={1.7} />
          </button>
          <button
            className="w4-btn is-primary v3-publish-btn"
            style={{ height: 30, padding: "0 11px", fontSize: 12.5, gap: 5 }}
            onClick={() => setPublishOpen(true)}
          >
            <PenSquare size={13} />
            发动态
          </button>
        </div>
      </div>

      <div className="w4-scroll" onScroll={onScroll}>
        <div className="w4-feed">
          {localMode ? (
            <div className="v3-notice">
              当前是「本地模式」：发现信息流由服务器提供，本地模式无法加载。可在
              设置 → 本地与密钥 里切回服务器模式。
            </div>
          ) : null}

          {!localMode && loading && items.length === 0 ? (
            <FeedSkeleton />
          ) : null}

          {!localMode && error && items.length === 0 ? (
            <div className="v3-error">
              <div className="v3-error-title">信息流加载失败</div>
              <div className="v3-error-text">{error}</div>
              <button className="w4-btn is-primary" onClick={() => void load(true)}>
                重试
              </button>
            </div>
          ) : null}

          {!loading && !error && items.length === 0 && !localMode ? (
            <div className="v3-empty">
              <div className="v3-empty-title">还没有动态</div>
              <div className="v3-empty-text">发一条，看看 AI 人物会怎么回应。</div>
              <button className="w4-btn is-primary" onClick={() => setPublishOpen(true)}>
                发第一条动态
              </button>
            </div>
          ) : null}

          {items.map((item) => (
            <FeedCard
              key={item.id}
              item={item}
              onOpenPersona={() => navigate({ name: "persona", personaId: item.author.id })}
              onLike={() => void toggleLike(item)}
              onComment={() => setCommentFor(commentFor === item.id ? null : item.id)}
              onQuickAdd={() => void quickAdd(item)}
              onDelete={() => void removePost(item)}
              onZoom={setZoom}
              onNotify={showToast}
              commentOpen={commentFor === item.id}
              draft={draft}
              setDraft={setDraft}
              onSubmitComment={(text) => void submitComment(item, text)}
              myName={profile.name}
              myAvatar={profile.avatar}
            />
          ))}

          {loadingMore ? <div className="v3-more">加载更多…</div> : null}
          {!loadingMore && !cursor && items.length > 0 ? (
            <div className="v3-more">已经到底了</div>
          ) : null}
          <div style={{ height: 24 }} />
        </div>
      </div>

      {zoom ? <Lightbox src={zoom} onClose={() => setZoom(null)} /> : null}

      {publishOpen ? (
        <PublishDialog
          onClose={() => setPublishOpen(false)}
          onPublish={async (content, visibility, media, tags) => {
            try {
              const res = await createPost({ content, visibility, media, tags });
              setPublishOpen(false);
              setItems((prev) => [res.post, ...prev]);
              void fetchFeedSummary().then((s) => setSummary(s)).catch(() => undefined);
              showToast(
                visibility === "public"
                  ? "已发布到发现，AI 人物可能会来互动"
                  : `已发布（${VISIBILITY_LABEL[visibility]}）`,
                "success",
              );
            } catch (exc) {
              showToast(exc instanceof Error ? exc.message : "发布失败");
            }
          }}
        />
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ 卡片 */

export function FeedCard({
  item,
  onOpenPersona,
  onLike,
  onComment,
  onQuickAdd,
  onDelete,
  onZoom,
  onNotify,
  commentOpen,
  draft,
  setDraft,
  onSubmitComment,
  myName,
  myAvatar,
}: {
  item: FeedItem;
  onOpenPersona: () => void;
  onLike: () => void;
  onComment: () => void;
  onQuickAdd: () => void;
  onDelete: () => void;
  onZoom: (src: string) => void;
  onNotify?: (msg: string) => void;
  commentOpen: boolean;
  draft: string;
  setDraft: (v: string) => void;
  onSubmitComment: (text: string) => void;
  myName?: string;
  myAvatar?: string;
}) {
  const isPersona = item.kind === "persona_post";
  const author = item.author;
  // 用户动态跟随「我」的资料；AI 人物统一用客户端 Avataaars 渲染（与服务端头像二选一）
  const isUserPost = item.kind === "user_post";
  const displayName = isUserPost && myName ? myName : author.nickname;
  const personaAvatar = isPersona
    ? personaAvatarSrc(
        author.avatar_seed || author.id,
        author.avatar_style,
        author.avatar_options,
        author.avatar,
      )
    : "";
  const displayAvatar = isUserPost
    ? myAvatar || author.avatar
    : personaAvatar || author.avatar;

  /* 翻译 / 朗读：信息流是英语内容，翻译走 V2 的 LLM 翻译链路，
     朗读用该人物自己的声线（后端 /persona-posts/{id}/speech 合成并缓存）。 */
  const [zh, setZh] = useState("");
  const [zhOpen, setZhOpen] = useState(false);
  const [zhLoading, setZhLoading] = useState(false);
  const [playing, setPlaying] = useState(false);
  /* 人物用母语评论：逐条翻译 */
  const [cZh, setCZh] = useState<Record<string, string>>({});
  const [cZhOpen, setCZhOpen] = useState<Record<string, boolean>>({});
  const [cZhBusy, setCZhBusy] = useState("");

  const trComment = async (c: FeedComment) => {
    if (cZhOpen[c.id]) {
      setCZhOpen((m) => ({ ...m, [c.id]: false }));
      return;
    }
    if (cZh[c.id]) {
      setCZhOpen((m) => ({ ...m, [c.id]: true }));
      return;
    }
    setCZhBusy(c.id);
    const out = await translateText(c.content, "zh").catch(() => "");
    setCZhBusy("");
    if (out) {
      setCZh((m) => ({ ...m, [c.id]: out }));
      setCZhOpen((m) => ({ ...m, [c.id]: true }));
    }
  };

  const toggleZh = async () => {
    if (zhOpen) {
      setZhOpen(false);
      return;
    }
    if (zh) {
      setZhOpen(true);
      return;
    }
    setZhLoading(true);
    const out = await translateText(item.content, "zh").catch(() => "");
    setZhLoading(false);
    if (out) {
      setZh(out);
      setZhOpen(true);
    } else {
      onNotify?.("翻译失败，请稍后重试");
    }
  };

  const togglePlay = async () => {
    if (!isPersona || playing) return;
    setPlaying(true);
    const ok = await playVoiceUrl(personaSpeechUrl(item.id, item.content_hash)).catch(() => false);
    setPlaying(false);
    if (!ok) onNotify?.("朗读失败，请稍后重试");
  };

  return (
    <article className="w4-moment">
      <button onClick={onOpenPersona} aria-label={displayName} className="v3-avatar-btn">
        <Avatar
                src={
                  displayAvatar ||
                  personaAvatarSrc(
                    author.avatar_seed || author.id,
                    author.avatar_style,
                    author.avatar_options,
                    author.avatar,
                  )
                }
                name={displayName}
                size={42}
              />
      </button>
      <div className="w4-moment-body">
        <div className={`w4-moment-name ${item.is_mine ? "is-me" : ""}`}>
          <button className="v3-name-btn" onClick={onOpenPersona}>
            {displayName}
          </button>
          {isPersona ? <span className="v3-ai-tag">AI</span> : null}
          {isPersona ? (
            <span className="v3-flag">{flagOf((author as unknown as { country?: string }).country ?? "")}</span>
          ) : null}
          {item.visibility !== "public" ? (
            <span className="v3-vis">{VISIBILITY_LABEL[item.visibility]}</span>
          ) : null}
        </div>

        {item.content ? <div className="w4-moment-text">{item.content}</div> : null}
        {zhOpen && zh ? <div className="v3-post-zh">{zh}</div> : null}

        {item.media.length ? (
          <div className={`w4-moment-imgs n${Math.min(4, item.media.length)}`}>
            {item.media.map((src) => (
              <img
                key={src}
                src={src}
                alt={item.alt_text || ""}
                loading="lazy"
                onClick={() => onZoom(src)}
              />
            ))}
          </div>
        ) : null}

        {item.media.length && item.media_credit ? (
          <div className="v3-media-credit">图：{item.media_credit}</div>
        ) : null}

        {item.tags.length ? (
          <div className="v3-tags">
            {item.tags.slice(0, 4).map((t) => (
              <span key={t} className="v3-tag">
                #{t}
              </span>
            ))}
          </div>
        ) : null}

        <div className="w4-moment-foot">
          <span>{relTimeOf(item.created_at)}</span>
          <span style={{ flex: 1 }} />
          <button
            className="w4-moment-act"
            onClick={() => void toggleZh()}
            disabled={zhLoading}
          >
            <Languages size={13} strokeWidth={1.8} />
            {zhLoading ? "翻译中…" : zhOpen ? "收起" : "翻译"}
          </button>
          {isPersona ? (
            <button
              className={`w4-moment-act ${playing ? "is-on" : ""}`}
              onClick={() => void togglePlay()}
            >
              {playing ? <Pause size={13} strokeWidth={1.8} /> : <Volume2 size={13} strokeWidth={1.8} />}
              {playing ? "播放中" : "朗读"}
            </button>
          ) : null}
          {isPersona ? (
            <>
              <button className={`w4-moment-act ${item.liked_by_me ? "is-on" : ""}`} onClick={onLike}>
                <Heart size={13} strokeWidth={1.8} fill={item.liked_by_me ? "currentColor" : "none"} />
                赞
              </button>
              <button className="w4-moment-act" onClick={onComment}>
                <MessageSquare size={13} strokeWidth={1.8} />
                评论
              </button>
            </>
          ) : (
            <span className="v3-stat">
              <Heart size={12} strokeWidth={1.8} /> {item.like_count}
              <MessageSquare size={12} strokeWidth={1.8} style={{ marginLeft: 8 }} /> {item.comment_count}
            </span>
          )}
          {item.is_mine ? (
            <button className="w4-moment-act is-danger" onClick={onDelete}>
              <Trash2 size={13} strokeWidth={1.8} />
              删除
            </button>
          ) : null}
          {isPersona && item.friendship_status === "none" ? (
            <button className="w4-moment-act v3-add" onClick={onQuickAdd}>
              <UserPlus size={13} strokeWidth={1.8} />
              加好友
            </button>
          ) : null}
        </div>

        {item.comments.length || (item.liked_by?.length ?? 0) > 0 ? (
          <div className="w4-moment-interact">
            {(item.liked_by?.length ?? 0) > 0 ? (
              <div className="w4-likes">
                <span className="w4-likes-icon">
                  <Heart size={9} strokeWidth={0} fill="currentColor" />
                </span>
                <span className="w4-like-row">
                  {item.liked_by!.map((x) => (
                    <span key={x.persona_id} className="w4-like-name">
                      {x.nickname}
                    </span>
                  ))}
                </span>
              </div>
            ) : null}
            {item.comments.length ? (
              <div className="w4-moment-comments">
                {item.comments.map((c) => (
                  <div key={c.id} className="w4-moment-comment">
                    <b>{c.author.nickname}</b>
                    {isPersona ? "" : "："}
                    {c.content}
                    {c.author.is_ai ? <span className="v3-ai-tag sm">AI</span> : null}
                    {c.author.is_ai ? (
                      <button
                        className="v3-link-btn"
                        onClick={() => void trComment(c)}
                        style={{ marginLeft: 6 }}
                      >
                        {cZhBusy === c.id ? "翻译中…" : cZhOpen[c.id] ? "收起翻译" : "翻译"}
                      </button>
                    ) : null}
                    {cZhOpen[c.id] && cZh[c.id] ? (
                      <div className="v3-comment-zh">{cZh[c.id]}</div>
                    ) : null}
                  </div>
                ))}
                {item.comment_count > item.comments.length ? (
                  <div className="v3-more-comments">还有 {item.comment_count - item.comments.length} 条评论</div>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}

        {commentOpen ? (
          <div className="w4-comment-input">
            <input
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.nativeEvent.isComposing && draft.trim()) {
                  onSubmitComment(draft.trim());
                }
              }}
              placeholder={`回应 ${author.nickname}…`}
            />
            <button
              className="w4-btn is-primary"
              style={{ height: 28, padding: "0 10px", fontSize: 12 }}
              disabled={!draft.trim()}
              onClick={() => onSubmitComment(draft.trim())}
            >
              发送
            </button>
          </div>
        ) : null}
      </div>
    </article>
  );
}

/* ------------------------------------------------------------------ 发布 */

function PublishDialog({
  onClose,
  onPublish,
}: {
  onClose: () => void;
  onPublish: (
    content: string,
    visibility: "public" | "friends" | "private",
    media: string[],
    tags: string[],
  ) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [visibility, setVisibility] = useState<"public" | "friends" | "private">("public");
  const [picked, setPicked] = useState<string[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [tagInput, setTagInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const fileRef = useRef<HTMLInputElement | null>(null);

  const customTags = tags.filter((t) => !TAGS.includes(t));
  const addTag = (raw: string) => {
    const tag = raw.trim().replace(/^#+/, "").slice(0, TAG_MAX_LEN);
    if (!tag) return;
    setTags((list) => (list.includes(tag) || list.length >= TAG_MAX ? list : [...list, tag]));
    setTagInput("");
  };

  const visibleOptions = useMemo(
    () => [
      { value: "public" as const, label: "公开", desc: "出现在发现信息流，AI 人物可能点赞/评论/加你" },
      { value: "friends" as const, label: "好友可见", desc: "只有你的 AI 好友能看到" },
      { value: "private" as const, label: "仅自己可见", desc: "只有你自己记录，不进入任何信息流" },
    ],
    [],
  );

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
            disabled={busy || (!text.trim() && picked.length === 0)}
            onClick={async () => {
              setBusy(true);
              try {
                await onPublish(text.trim(), visibility, picked, tags);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "发布中…" : "发布"}
          </button>
        </>
      }
    >
      <textarea
        className="w4-textarea"
        style={{ border: 0, padding: 0, fontSize: 14.5, minHeight: 96, background: "transparent" }}
        placeholder="这一刻的想法…（公开动态会被 AI 人物看到）"
        value={text}
        onChange={(e) => setText(e.target.value)}
        autoFocus
      />

      <div className="w4-field-hint" style={{ marginTop: 2 }}>
        可选内置图片，或上传自己的照片（JPG/PNG/WebP，≤8MB，最多 4 张）
      </div>

      {picked.length ? (
        <div className="w4-publish-pics" style={{ marginTop: 8 }}>
          {picked.map((p, i) => (
            <button
              key={p}
              className="w4-publish-pic on"
              title="点击移除"
              onClick={() => setPicked((list) => list.filter((x) => x !== p))}
            >
              <img src={p} alt="" />
              <span className="w4-pick-order">{i + 1}</span>
            </button>
          ))}
        </div>
      ) : null}

      <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
        <button
          className="w4-btn"
          type="button"
          disabled={uploading || picked.length >= 4}
          onClick={() => fileRef.current?.click()}
        >
          {uploading ? "上传中…" : "上传照片"}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/jpeg,image/png,image/webp,image/gif"
          multiple
          hidden
          onChange={async (e) => {
            const files = Array.from(e.target.files || []);
            e.target.value = "";
            if (!files.length) return;
            setUploading(true);
            setUploadError("");
            try {
              for (const file of files) {
                if (picked.length >= 4) break;
                const data = await uploadImage(file);
                setPicked((list) => (list.length >= 4 || list.includes(data.url) ? list : [...list, data.url]));
              }
            } catch (exc) {
              setUploadError(exc instanceof Error ? exc.message : "上传失败");
            } finally {
              setUploading(false);
            }
          }}
        />
        <span className="w4-field-hint" style={{ alignSelf: "center" }}>
          {uploadError ? <span style={{ color: "#c0392b" }}>{uploadError}</span> : "最多 4 张"}
        </span>
      </div>

      <div className="w4-publish-pics">
        {STOCK.map((p) => {
          const on = picked.includes(p);
          return (
            <button
              key={p}
              className={`w4-publish-pic ${on ? "on" : ""}`}
              onClick={() => setPicked((list) => (on ? list.filter((x) => x !== p) : [...list, p].slice(0, 4)))}
            >
              <img src={p} alt="" />
              {on ? <span className="w4-pick-order">{picked.indexOf(p) + 1}</span> : null}
            </button>
          );
        })}
      </div>

      <div className="w4-field" style={{ marginTop: 14 }}>
        <div className="w4-field-label">可见范围</div>
        <div className="v3-vis-options">
          {visibleOptions.map((o) => (
            <button
              key={o.value}
              className={`v3-vis-option ${visibility === o.value ? "on" : ""}`}
              onClick={() => setVisibility(o.value)}
            >
              <span className="v3-vis-title">
                <Icon size={13} strokeWidth={2}>
                  {o.value === "public" ? (
                    <>
                      <circle cx="12" cy="12" r="9" />
                      <path d="M3 12h18M12 3a15 15 0 010 18a15 15 0 010-18" />
                    </>
                  ) : o.value === "friends" ? (
                    <>
                      <path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2" />
                      <circle cx="9" cy="7" r="4" />
                      <path d="M22 21v-2a4 4 0 00-3-3.87" />
                    </>
                  ) : (
                    <>
                      <rect x="4" y="10" width="16" height="10" rx="2" />
                      <path d="M8 10V7a4 4 0 018 0v3" />
                    </>
                  )}
                </Icon>
                {o.label}
              </span>
              <span className="v3-vis-desc">{o.desc}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="w4-field" style={{ marginTop: 12 }}>
        <div className="w4-field-label">兴趣标签（帮 AI 人物找到你）</div>
        <div className="w4-chips">
          {TAGS.map((t) => (
            <button
              key={t}
              className={`w4-chip ${tags.includes(t) ? "is-green" : ""}`}
              style={{ cursor: "pointer" }}
              onClick={() =>
                setTags((list) =>
                  list.includes(t)
                    ? list.filter((x) => x !== t)
                    : list.length >= TAG_MAX
                      ? list
                      : [...list, t],
                )
              }
            >
              {t}
            </button>
          ))}
          {customTags.map((t) => (
            <button
              key={`custom-${t}`}
              className="w4-chip is-green"
              style={{ cursor: "pointer" }}
              title="点击删除自定义标签"
              onClick={() => setTags((list) => list.filter((x) => x !== t))}
            >
              {t} ×
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <input
            className="w4-input"
            style={{ flex: 1 }}
            placeholder={`自定义标签（最多 ${TAG_MAX} 个，回车添加）`}
            value={tagInput}
            maxLength={TAG_MAX_LEN}
            onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addTag(tagInput);
              }
            }}
          />
          <button className="w4-btn" type="button" onClick={() => addTag(tagInput)}>
            添加
          </button>
        </div>
      </div>

      <div className="w4-field-hint">
        公开动态会进入所有人的发现信息流；AI 人物可能点赞、评论或申请加你，也可能什么都不发生。
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ 占位 */

function FeedSkeleton() {
  return (
    <>
      {[0, 1, 2].map((i) => (
        <div className="v3-skeleton" key={i}>
          <div className="v3-skeleton-avatar" />
          <div className="v3-skeleton-body">
            <div className="v3-skeleton-line w60" />
            <div className="v3-skeleton-line w90" />
            <div className="v3-skeleton-line w40" />
          </div>
        </div>
      ))}
    </>
  );
}

export { ImagePlus, Languages, Send };
