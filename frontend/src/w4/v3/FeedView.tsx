"use client";

import { useShallow } from "zustand/react/shallow";

/**
 * V3 · 发现信息流（以 AI Persona 为中心的社交世界）。
 *
 * - 信息流 = 平台 Persona 的公共动态 + 用户自己的动态（公开动态也会互相可见）
 * - 点作者头像/昵称 → Persona 主页；未加好友的人也会出现在信息流里
 * - 支持点赞 / 评论 / 发动态（可见性三档）/ 通知中心（社交事件）
 *
 * 视觉：编辑式信息流（无卡片套卡片）——作者行 + 正文 + 大图 + 药丸操作组 +
 * 扁平内嵌评论区。样式全部在 ../styles/discover.css。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Bell,
  ChevronLeft,
  Compass,
  Globe,
  Heart,
  ImagePlus,
  Languages,
  Lock,
  MessageSquare,
  Pause,
  PenSquare,
  RefreshCw,
  ShieldAlert,
  Trash2,
  Users,
  Volume2,
} from "lucide-react";

import { playVoiceUrl } from "../apiFacade";
import { useScrollMemory } from "../useScrollMemory";
import { resolveAvatar } from "../avatar";
import { haptic } from "../../theme/haptics";

import type { Route } from "../model";
import { useApp } from "../store";
import { Avatar, Dialog, Lightbox } from "../ui";
import { ReportDialog } from "./ReportDialog";
import { SponsoredCard, type SponsoredItem } from "./SponsoredCard";
import {
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
import "../styles/discover.css";

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

export function FeedView({
  navigate,
  onBack,
}: {
  navigate: (r: Route) => void;
  onBack?: () => void;
}) {
  const { showToast, profile } = useApp(
    useShallow((state) => ({ showToast: state.showToast, profile: state.profile })),
  );
  const [items, setItems] = useState<FeedItem[]>([]);
  const [cursor, setCursor] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [summary, setSummary] = useState<FeedSummary | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [zoom, setZoom] = useState<string | null>(null);
  const [commentFor, setCommentFor] = useState<string | null>(null);
  const [reportFor, setReportFor] = useState<FeedItem | null>(null);
  const [draft, setDraft] = useState("");
  const seq = useRef(0);
  const scrollRef = useScrollMemory<HTMLDivElement>("discover");
  const virtualizer = useVirtualizer({ count: items.length, getScrollElement: () => scrollRef.current, estimateSize: () => 620, overscan: 3, getItemKey: (index) => items[index].id });

  const localMode = false;

  const load = useCallback(
    async (reset: boolean, nextCursor = ""): Promise<{
      items: FeedItem[];
      next_cursor?: string;
    } | null> => {
      const my = ++seq.current;
      if (reset) {
        setLoading(true);
        setError("");
      } else {
        setLoadingMore(true);
      }
      try {
        const page = await fetchFeed(reset ? undefined : nextCursor || undefined, 10);
        if (seq.current !== my) return null;
        setItems((prev) => (reset ? page.items : [...prev, ...page.items]));
        setCursor(page.next_cursor || "");
        void fetchFeedSummary().then((s) => setSummary(s)).catch(() => undefined);
        return page;
      } catch (exc) {
        if (seq.current !== my) return null;
        setError(exc instanceof Error ? exc.message : "加载失败");
        return null;
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
    const onEvent = () => void tick();
    window.addEventListener("speakeasy:client-event", onEvent);
    return () => {
      alive = false;
      window.removeEventListener("speakeasy:client-event", onEvent);
    };
  }, [localMode, showToast]);

  const [refreshing, setRefreshing] = useState(false);
  const [pull, setPull] = useState(0);
  const pullRef = useRef(0);
  /** mode: 顶部下拉 / 底部上拉；手势开始时不在两端就不参与刷新 */
  const pullStart = useRef<{ y: number; mode: "top" | "bottom" | null } | null>(null);
  const updatePull = (v: number) => {
    pullRef.current = v;
    setPull(v);
  };

  /** 手机手势：内容已经在顶部时，继续下拉才刷新；已经在底部时，继续上拉也刷新 */
  const isMobileNow = () => window.matchMedia("(max-width: 900px)").matches;
  const onTouchStart = (e: React.TouchEvent<HTMLDivElement>) => {
    if (!isMobileNow()) return;
    const el = e.currentTarget;
    const atTop = el.scrollTop <= 0;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
    pullStart.current = {
      y: e.touches[0].clientY,
      mode: atTop ? "top" : atBottom ? "bottom" : null,
    };
  };
  const onTouchMove = (e: React.TouchEvent<HTMLDivElement>) => {
    const start = pullStart.current;
    if (!start || !start.mode) return;
    const el = e.currentTarget;
    const dy = e.touches[0].clientY - start.y;
    if (start.mode === "top") {
      // 必须仍然在顶部：中途滚动过（用户先往上滑离开了顶部）就不算下拉刷新
      if (dy > 0 && el.scrollTop <= 0) {
        updatePull(Math.min(90, dy * 0.6));
      } else if (dy <= 0) {
        updatePull(0);
      }
      return;
    }
    // bottom 模式：到底后继续上拉
    if (dy < 0) {
      updatePull(Math.min(90, -dy * 0.6));
    } else {
      updatePull(0);
    }
  };
  const onTouchEnd = () => {
    const started = pullStart.current;
    const fired = Boolean(started?.mode) && pullRef.current >= 45;
    pullStart.current = null;
    updatePull(0);
    if (fired) void refresh();
  };

  /** 手动刷新：重新拉第一页，并明确告诉用户「有新内容」还是「已是最新」 */
  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    const prevFirst = items[0]?.id ?? "";
    try {
      const page = await load(true);
      if (!page) {
        showToast("刷新失败，请稍后重试");
        return;
      }
      scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
      const same = Boolean(prevFirst) && page.items[0]?.id === prevFirst;
      showToast(same ? "已是最新" : "已刷新", same ? undefined : "success");
      if (!same) haptic("success");
    } finally {
      setRefreshing(false);
    }
  };

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
      haptic("save");
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
        haptic("send");
      } catch {
        showToast("评论失败，请稍后重试");
      }
    } else {
      showToast("好友的评论会在对方回应时出现");
    }
    setDraft("");
    setCommentFor(null);
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
    <div className="w4-main disc-main">
      <div className="w4-head disc-head">
        {onBack ? (
          <button className="w4-icon-btn" onClick={onBack} aria-label="返回">
            <ChevronLeft size={20} strokeWidth={2} />
          </button>
        ) : null}
        <div className="w4-head-main">
          <div className="w4-head-title disc-head-title">动态</div>
          <div className="w4-head-sub">
            {summary ? `${summary.persona_posts} 条人物动态 · ${summary.friends} 位好友` : "TA 们的生活动态"}
          </div>
        </div>
        <div className="w4-head-actions disc-head-actions">
          <button
            className="w4-icon-btn disc-bell"
            onClick={() => navigate({ name: "events" })}
            title="通知"
            aria-label="通知"
          >
            <Bell size={19} strokeWidth={1.8} />
            {unread > 0 ? <span className="disc-badge">{unread > 99 ? "99+" : unread}</span> : null}
          </button>
          <button
            className="w4-icon-btn disc-hide-narrow"
            onClick={() => void refresh()}
            disabled={refreshing}
            title="刷新"
            aria-label="刷新"
            aria-busy={refreshing}
          >
            <RefreshCw size={18} strokeWidth={1.8} className={refreshing ? "disc-spin" : undefined} />
          </button>
          <button
            className="w4-icon-btn"
            onClick={() => navigate({ name: "discoverPeople" })}
            title="偶遇一个人"
            aria-label="偶遇一个人"
          >
            <Compass size={18} strokeWidth={1.8} />
          </button>
          <button className="w4-btn is-primary disc-publish-btn" onClick={() => setPublishOpen(true)}>
            <PenSquare size={15} />
            发动态
          </button>
        </div>
      </div>

      {pull > 0 ? (
        <div className="disc-pull">{pull >= 45 ? "松手刷新" : "下拉刷新"}</div>
      ) : null}
      <div
        className="w4-scroll"
        onScroll={onScroll}
        ref={scrollRef}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        <div className="w4-feed">
          {localMode ? (
            <div className="disc-notice">
              当前处于离线模式，动态暂不可用；可在设置 → 本地与密钥 切回在线模式。
            </div>
          ) : null}

          {!localMode && loading && items.length === 0 ? <FeedSkeleton /> : null}

          {!localMode && error && items.length === 0 ? (
            <div className="disc-state">
              <div className="disc-state-title">动态加载失败</div>
              <div className="disc-state-text">{error}</div>
              <button className="w4-btn is-primary" onClick={() => void load(true)}>
                重试
              </button>
            </div>
          ) : null}

          {!loading && !error && items.length === 0 && !localMode ? (
            <div className="disc-state">
              <div className="disc-state-title">还没有动态</div>
              <div className="disc-state-text">发一条，看看 AI 人物会怎么回应。</div>
              <button className="w4-btn is-primary" onClick={() => setPublishOpen(true)}>
                发第一条动态
              </button>
            </div>
          ) : null}

          <div style={{ position: "relative", height: virtualizer.getTotalSize() }}>
            {virtualizer.getVirtualItems().map((row) => {
              const item = items[row.index];
              if (item.kind === "sponsored") {
                return (
                  <div
                    key={item.id}
                    data-index={row.index}
                    ref={virtualizer.measureElement}
                    style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${row.start}px)` }}
                  >
                    <SponsoredCard
                      item={item as unknown as SponsoredItem}
                      onHide={() => setItems((prev) => prev.filter((x) => x.id !== item.id))}
                    />
                  </div>
                );
              }
              return (
                <div
                  key={item.id}
                  data-index={row.index}
                  ref={virtualizer.measureElement}
                  style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${row.start}px)` }}
                >
                  <FeedCard
                    key={item.id}
                    item={item}
                    onOpenPersona={() =>
                      item.kind === "persona_post"
                        ? navigate({ name: "persona", personaId: item.author.id })
                        : navigate({ name: "me" })
                    }
                    onLike={() => void toggleLike(item)}
                    onComment={() => setCommentFor(commentFor === item.id ? null : item.id)}
                    onDelete={() => void removePost(item)}
                    onZoom={setZoom}
                    onNotify={showToast}
                    commentOpen={commentFor === item.id}
                    draft={draft}
                    setDraft={setDraft}
                    onSubmitComment={(text) => void submitComment(item, text)}
                    onReport={() => setReportFor(item)}
                    myName={profile.name}
                    myAvatar={profile.avatar}
                  />
                </div>
              );
            })}
          </div>

          {loadingMore ? <div className="disc-end">加载更多…</div> : null}
          {!loadingMore && !cursor && items.length > 0 ? <div className="disc-end">已经到底了</div> : null}
        </div>
      </div>

      {zoom ? <Lightbox src={zoom} onClose={() => setZoom(null)} /> : null}

      {reportFor ? (
        <ReportDialog
          targetType={reportFor.kind === "persona_post" ? "post" : "user"}
          targetId={reportFor.kind === "persona_post" ? reportFor.id : reportFor.author.id}
          onClose={() => setReportFor(null)}
          onDone={(message) => showToast(message, "success")}
        />
      ) : null}

      {publishOpen ? (
        <PublishDialog
          myName={profile.name}
          myAvatar={profile.avatar}
          onClose={() => setPublishOpen(false)}
          onPublish={async (content, visibility, media, tags) => {
            try {
              const res = await createPost({ content, visibility, media, tags });
              setPublishOpen(false);
              setItems((prev) => [res.post, ...prev]);
              void fetchFeedSummary().then((s) => setSummary(s)).catch(() => undefined);
              haptic("save");
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
  onDelete,
  onReport,
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
  onDelete: () => void;
  onReport?: () => void;
  onZoom: (src: string) => void;
  onNotify?: (msg: string) => void;
  commentOpen: boolean;
  draft: string;
  setDraft: (v: string) => void;
  onSubmitComment: (text: string) => void;
  myName?: string;
  myAvatar?: string;
}) {
  const nativeLanguage = useApp((state) => state.profile.nativeLanguage || "zh");
  const isPersona = item.kind === "persona_post";
  const author = item.author;
  // 用户动态跟随「我」的资料；AI 人物统一用客户端 Avataaars 渲染（与服务端头像二选一）
  const isUserPost = item.kind === "user_post";
  const displayName = isUserPost && myName ? myName : author.nickname;
  /* AI 人物统一走 resolveAvatar（seed + style + options → Avataaars，带 LRU 缓存）；
     用户动态跟随「我」的资料头像，不重新解析，避免发帖人换脸。 */
  const personaAvatar = isPersona ? resolveAvatar(author) : "";
  const displayAvatar = isUserPost ? myAvatar || "" : personaAvatar || author.avatar || "";

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
    const out = await translateText(c.content, nativeLanguage).catch(() => "");
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
    const out = await translateText(item.content, nativeLanguage).catch(() => "");
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
    haptic("toggle");
    const ok = await playVoiceUrl(personaSpeechUrl(item.id, item.content_hash, item.speech_version)).catch(() => false);
    setPlaying(false);
    if (!ok) onNotify?.("朗读失败，请稍后重试");
  };

  const likeLabel = item.liked_by_me ? "取消赞" : "赞";

  return (
    <article className="disc-post">
      <header className="disc-post-head">
        <button
          type="button"
          onClick={onOpenPersona}
          aria-label={`查看 ${displayName} 的主页`}
          className="disc-avatar-btn"
        >
          <Avatar
            src={displayAvatar}
            name={displayName}
            size={48}
            round
          />
        </button>
        <div className="disc-author">
          <div className="disc-author-line">
            <button type="button" className="disc-author-name disc-inline-btn" onClick={onOpenPersona}>
              {displayName}
            </button>
            {isPersona ? <span className="disc-ai-tag">AI</span> : null}
            {isPersona ? (
              <span className="disc-flag" aria-label={author.country || "地区"}>
                {flagOf(author.country ?? "")}
              </span>
            ) : null}
          </div>
          <div className="disc-author-meta">
            {isPersona && author.occupation ? (
              <>
                <span>{author.occupation}</span>
                <span className="disc-meta-dot" aria-hidden />
              </>
            ) : null}
            <span>{relTimeOf(item.created_at)}</span>
          </div>
        </div>
        {item.visibility !== "public" ? (
          <div className="disc-post-side">
            <span className="disc-visibility">{VISIBILITY_LABEL[item.visibility]}</span>
          </div>
        ) : null}
      </header>

      {item.content ? <p className="disc-post-text">{item.content}</p> : null}
      {zhOpen && zh ? <div className="disc-translation">{zh}</div> : null}

      {item.media.length ? (
        <div className={`disc-media n${Math.min(4, item.media.length)}`}>
          {item.media.map((src) => (
            <button
              type="button"
              key={src}
              className="disc-media-item"
              onClick={() => onZoom(src)}
              aria-label="查看大图"
            >
              <img src={src} alt={item.alt_text || ""} loading="lazy" />
            </button>
          ))}
        </div>
      ) : null}

      {item.media.length && item.media_credit ? (
        <div className="disc-credit">图：{item.media_credit}</div>
      ) : null}

      {item.tags.length ? (
        <div className="disc-tags">
          {item.tags.slice(0, 4).map((t) => (
            <span key={t} className="disc-tag">
              #{t}
            </span>
          ))}
        </div>
      ) : null}

      <div className="disc-actions">
        <div className="disc-action-group">
          {isPersona ? (
            <button
              type="button"
              className={`disc-action ${item.liked_by_me ? "is-on" : ""}`}
              onClick={onLike}
              aria-pressed={item.liked_by_me}
              aria-label={likeLabel}
            >
              <Heart size={16} strokeWidth={1.9} fill={item.liked_by_me ? "currentColor" : "none"} />
              <span>{item.liked_by_me ? "已赞" : "赞"}</span>
              {item.like_count > 0 ? <span className="disc-count">{item.like_count}</span> : null}
            </button>
          ) : (
            <span className="disc-stat">
              <Heart size={15} strokeWidth={1.9} />
              <span className="disc-count">{item.like_count}</span>
            </span>
          )}
          {isPersona ? (
            <button type="button" className="disc-action" onClick={onComment}>
              <MessageSquare size={16} strokeWidth={1.9} />
              <span>评论</span>
              {item.comment_count > 0 ? <span className="disc-count">{item.comment_count}</span> : null}
            </button>
          ) : (
            <span className="disc-stat">
              <MessageSquare size={15} strokeWidth={1.9} />
              <span className="disc-count">{item.comment_count}</span>
            </span>
          )}
          <button
            type="button"
            className={`disc-action ${zhOpen ? "is-on" : ""}`}
            onClick={() => void toggleZh()}
            disabled={zhLoading}
            aria-pressed={zhOpen}
          >
            <Languages size={16} strokeWidth={1.9} />
            <span>{zhLoading ? "翻译中…" : zhOpen ? "收起" : "翻译"}</span>
          </button>
          {isPersona ? (
            <button
              type="button"
              className={`disc-action ${playing ? "is-on" : ""}`}
              onClick={() => void togglePlay()}
              aria-pressed={playing}
            >
              {playing ? <Pause size={16} strokeWidth={1.9} /> : <Volume2 size={16} strokeWidth={1.9} />}
              <span>{playing ? "播放中" : "朗读"}</span>
            </button>
          ) : null}
        </div>
        {item.is_mine ? (
          <button
            type="button"
            className="disc-action disc-action-standalone is-danger"
            onClick={onDelete}
            aria-label="删除动态"
          >
            <Trash2 size={16} strokeWidth={1.9} />
            <span>删除</span>
          </button>
        ) : onReport ? (
          <button
            type="button"
            className="disc-action disc-action-standalone"
            onClick={onReport}
            aria-label="举报这条内容"
          >
            <ShieldAlert size={16} strokeWidth={1.9} />
            <span>举报</span>
          </button>
        ) : null}
      </div>

      {item.comments.length || (item.liked_by?.length ?? 0) > 0 ? (
        <section className="disc-engagement" aria-label="互动">
          {(item.liked_by?.length ?? 0) > 0 ? (
            <div className="disc-likes">
              <span className="disc-likes-icon" aria-hidden>
                <Heart size={11} strokeWidth={0} fill="currentColor" />
              </span>
              <span className="disc-likes-names">
                {item.liked_by!.map((x) => (
                  <span key={x.persona_id} className="disc-likes-name">
                    {x.nickname}
                  </span>
                ))}
              </span>
            </div>
          ) : null}
          {item.comments.length ? (
            <div className="disc-comments">
              {item.comments.map((c) => (
                <div key={c.id} className="disc-comment">
                  <div className="disc-comment-line">
                    <span className="disc-comment-name">{c.author.nickname}</span>
                    {c.author.is_ai ? <span className="disc-ai-tag sm">AI</span> : null}
                    <span className="disc-comment-text">{c.content}</span>
                    {c.author.is_ai ? (
                      <button type="button" className="disc-link" onClick={() => void trComment(c)}>
                        {cZhBusy === c.id ? "翻译中…" : cZhOpen[c.id] ? "收起翻译" : "翻译"}
                      </button>
                    ) : null}
                  </div>
                  {cZhOpen[c.id] && cZh[c.id] ? <div className="disc-comment-zh">{cZh[c.id]}</div> : null}
                </div>
              ))}
              {item.comment_count > item.comments.length ? (
                <div className="disc-more-comments">还有 {item.comment_count - item.comments.length} 条评论</div>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : null}

      {commentOpen ? (
        <div className="disc-comment-form">
          <input
            autoFocus
            className="disc-comment-input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing && draft.trim()) {
                onSubmitComment(draft.trim());
              }
            }}
            placeholder={`回应 ${author.nickname}…`}
            aria-label={`回应 ${author.nickname}`}
          />
          <button
            type="button"
            className="w4-btn is-primary disc-send"
            disabled={!draft.trim()}
            onClick={() => onSubmitComment(draft.trim())}
          >
            发送
          </button>
        </div>
      ) : null}
    </article>
  );
}

/* ------------------------------------------------------------------ 发布 */

const VISIBILITY_OPTIONS = [
  {
    value: "public" as const,
    label: "公开",
    desc: "出现在发现信息流，AI 人物可能点赞/评论/加你",
    icon: Globe,
  },
  {
    value: "friends" as const,
    label: "好友可见",
    desc: "只有你的 AI 好友能看到",
    icon: Users,
  },
  {
    value: "private" as const,
    label: "仅自己可见",
    desc: "只有你自己记录，不进入任何信息流",
    icon: Lock,
  },
];

function PublishDialog({
  onClose,
  onPublish,
  myName,
  myAvatar,
}: {
  onClose: () => void;
  myName?: string;
  myAvatar?: string;
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

  const visibleOptions = useMemo(() => VISIBILITY_OPTIONS, []);

  const togglePick = (p: string) => {
    if (!picked.includes(p) && picked.length >= 4) return;
    haptic("selection");
    setPicked((list) =>
      list.includes(p) ? list.filter((x) => x !== p) : [...list, p],
    );
  };

  return (
    <Dialog
      title="发动态"
      onClose={onClose}
      footer={
        <>
          <button className="w4-btn disc-dialog-btn" onClick={onClose}>
            取消
          </button>
          <button
            className="w4-btn is-primary disc-dialog-btn"
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
      <div className="disc-publish">
        <div className="disc-publish-author">
          <Avatar src={myAvatar} name={myName || "我"} size={40} round />
          <div>
            <div className="disc-publish-author-name">{myName || "我"}</div>
            <div className="disc-publish-author-hint">公开动态会出现在发现信息流</div>
          </div>
        </div>

        <textarea
          className="disc-publish-text"
          placeholder="这一刻的想法…（公开动态会被 AI 人物看到）"
          value={text}
          onChange={(e) => setText(e.target.value)}
          autoFocus
        />

        <div className="disc-publish-section">
          <div className="disc-publish-label">照片</div>
          <div className="disc-publish-grid">
            {picked.map((p, i) => (
              <button
                type="button"
                key={p}
                className="disc-publish-tile is-on"
                title="点击移除"
                aria-label={`移除第 ${i + 1} 张照片`}
                onClick={() => togglePick(p)}
              >
                <img src={p} alt="" />
                <span className="disc-publish-order">{i + 1}</span>
              </button>
            ))}
            {picked.length < 4 ? (
              <button
                type="button"
                className="disc-publish-tile disc-publish-add"
                disabled={uploading}
                onClick={() => fileRef.current?.click()}
              >
                <ImagePlus size={18} strokeWidth={1.8} />
                <span>{uploading ? "上传中…" : "上传"}</span>
              </button>
            ) : null}
          </div>
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
          <div className="w4-field-hint">
            {uploadError ? <span style={{ color: "var(--md-error)" }}>{uploadError}</span> : "可选内置图片，或上传自己的照片（JPG/PNG/WebP，≤8MB，最多 4 张）"}
          </div>
        </div>

        <div className="disc-publish-section">
          <div className="disc-publish-label">内置图片</div>
          <div className="disc-publish-grid">
            {STOCK.map((p) => {
              const on = picked.includes(p);
              return (
                <button
                  type="button"
                  key={p}
                  className={`disc-publish-tile ${on ? "is-on" : ""}`}
                  aria-pressed={on}
                  aria-label={on ? "取消这张图片" : "选择这张图片"}
                  onClick={() => togglePick(p)}
                >
                  <img src={p} alt="" />
                  {on ? <span className="disc-publish-order">{picked.indexOf(p) + 1}</span> : null}
                </button>
              );
            })}
          </div>
        </div>

        <div className="disc-publish-section">
          <div className="disc-publish-label">可见范围</div>
          <div className="disc-vis-options">
            {visibleOptions.map((o) => {
              const OptionIcon = o.icon;
              return (
                <button
                  type="button"
                  key={o.value}
                  className={`disc-vis-option ${visibility === o.value ? "is-on" : ""}`}
                  aria-pressed={visibility === o.value}
                  onClick={() => {
                    haptic("selection");
                    setVisibility(o.value);
                  }}
                >
                  <span className="disc-vis-icon" aria-hidden>
                    <OptionIcon size={16} strokeWidth={1.9} />
                  </span>
                  <span className="disc-vis-copy">
                    <span className="disc-vis-title">{o.label}</span>
                    <span className="disc-vis-desc">{o.desc}</span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="disc-publish-section">
          <div className="disc-publish-label">兴趣标签（帮 AI 人物找到你）</div>
          <div className="w4-chips">
            {TAGS.map((t) => (
              <button
                type="button"
                key={t}
                className={`w4-chip ${tags.includes(t) ? "is-green" : ""}`}
                aria-pressed={tags.includes(t)}
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
                type="button"
                key={`custom-${t}`}
                className="w4-chip is-green"
                title="点击删除自定义标签"
                onClick={() => setTags((list) => list.filter((x) => x !== t))}
              >
                {t} ×
              </button>
            ))}
          </div>
          <div className="disc-tag-row">
            <input
              className="w4-input"
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
          <div className="w4-field-hint">
            公开动态会进入所有人的发现信息流；AI 人物可能点赞、评论或申请加你，也可能什么都不发生。
          </div>
        </div>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ 占位 */

function FeedSkeleton() {
  return (
    <>
      {[0, 1, 2].map((i) => (
        <div className="disc-skeleton" key={i} aria-hidden>
          <div className="disc-skeleton-head">
            <div className="disc-skeleton-avatar" />
            <div className="disc-skeleton-meta">
              <div className="disc-skeleton-line w40" />
              <div className="disc-skeleton-line w60" />
            </div>
          </div>
          <div className="disc-skeleton-body">
            <div className="disc-skeleton-line w100" />
            <div className="disc-skeleton-line w90" />
            <div className="disc-skeleton-line w60" />
          </div>
          <div className="disc-skeleton-media" />
        </div>
      ))}
    </>
  );
}
