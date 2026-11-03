"use client";

/** 动态 · 偶遇 Tab：偶遇好友（国家/语种筛选）+ 好友申请 + 朋友圈动态流（按住语音评论）。 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Compass, Languages, Loader2, MessageCircle, RefreshCw, Sparkles, Users, X } from "lucide-react";

import { Avatar } from "@/components/v2/Avatar";
import { PublishMoment } from "@/components/v2/PublishMoment";
import { HoldToTalk } from "@/components/v2/HoldToTalk";
import { useToast } from "@/components/v2/Toast";
import { EmptyState, ErrorBanner, Spinner } from "@/components/v2/ui";
import { api, apiErrorText, getArchetypeFilters, translateBatch, translateText } from "@/lib/v2/api";
import { countryMeta, flagOf, langLabel, languageMeta, localStateOf, relativeLabel } from "@/lib/v2/format";
import type { Friend, Moment, TranslationResult } from "@/lib/v2/types";
import type { RecordingResult } from "@/lib/v2/useRecorder";

/**
 * 接口不可用时的兜底列表：值必须与后端 `archetypes` 的 country / language 原值完全一致，
 * 否则 spawner 严格筛选会匹配不到原型（用户反馈过的「选日本却得到英语好友」问题）。
 */
const FALLBACK_COUNTRIES = [
  "Australia",
  "Brazil",
  "China",
  "France",
  "Germany",
  "India",
  "Ireland",
  "Italy",
  "Japan",
  "Korea",
  "Mexico",
  "Spain",
  "United Kingdom",
  "United States",
];

const FALLBACK_LANGUAGES = ["de", "en", "es", "fr", "it", "ja", "ko", "pt", "zh"];

const GENDERS = [
  { value: "", label: "性别不限" },
  { value: "female", label: "女生" },
  { value: "male", label: "男生" },
];

interface DiscoverTabProps {
  requests: Friend[];
  moments: Moment[];
  loading: boolean;
  momentsLoading: boolean;
  error: string | null;
  discovering: boolean;
  generating: boolean;
  acceptingId: string | null;
  onDiscover: (filters: { nationality: string; language: string; gender: string }) => void;
  onGenerate: () => void;
  onRetry: () => void;
  onAccept: (friend: Friend) => void;
  onDecline: (friend: Friend) => void;
  onCommentVoice: (moment: Moment, result: RecordingResult) => Promise<void>;
  onOpenChat: (friend: Friend) => void;
  /** 发布我的动态（父级负责调接口 + 合并列表） */
  publishingMoment: boolean;
  onPublishMoment: (input: { content: string; mood_tag?: string }) => Promise<boolean>;
  /** 发布后等 AI 评论的 moment id 列表 */
  commentingIds: string[];
  /** DiscoverTab 轮询到的动态（静默合并，不触发整页 loading） */
  onMomentsUpdated: (moments: Moment[]) => void;
}

export function DiscoverTab({
  requests,
  moments,
  loading,
  momentsLoading,
  error,
  discovering,
  generating,
  acceptingId,
  onDiscover,
  onGenerate,
  onRetry,
  onAccept,
  onDecline,
  onCommentVoice,
  onOpenChat,
  publishingMoment,
  onPublishMoment,
  commentingIds,
  onMomentsUpdated,
}: DiscoverTabProps) {
  const { showToast } = useToast();
  const [nationality, setNationality] = useState("");
  const [language, setLanguage] = useState("");
  const [gender, setGender] = useState("");
  const [commentingId, setCommentingId] = useState<string | null>(null);
  const [momentTranslations, setMomentTranslations] = useState<
    Record<string, { result: TranslationResult; open: boolean }>
  >({});
  const [translatingMoments, setTranslatingMoments] = useState<Record<string, boolean>>({});
  const [translatingAll, setTranslatingAll] = useState(false);
  const [countries, setCountries] = useState<string[]>(FALLBACK_COUNTRIES);
  const [languages, setLanguages] = useState<string[]>(FALLBACK_LANGUAGES);

  // 筛选项动态化：国家/语种来自后端声线原型池（失败保留对齐后的静态兜底）
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await getArchetypeFilters();
        if (cancelled) return;
        if (data.countries.length > 0) setCountries(data.countries);
        if (data.languages.length > 0) setLanguages(data.languages);
      } catch {
        /* 接口不可用时用静态兜底（值已与后端对齐） */
      }
    })();
    // 深链预选：/?tab=discover&country=Japan&language=ja（截图/调试用）
    try {
      const params = new URLSearchParams(window.location.search);
      const country = params.get("country");
      const lang = params.get("language");
      if (country) setNationality(country);
      if (lang) setLanguage(lang);
    } catch {
      /* 忽略非法 URL */
    }
    return () => {
      cancelled = true;
    };
  }, []);

  // 深链/历史选择可能不在后端当前列表中，补一个选项避免 <select> 显示空白
  const countryOptions = useMemo(() => {
    const list = [...countries];
    if (nationality && !list.includes(nationality)) list.unshift(nationality);
    return list;
  }, [countries, nationality]);

  const languageOptions = useMemo(() => {
    const list = [...languages];
    if (language && !list.includes(language)) list.unshift(language);
    return list;
  }, [languages, language]);

  /* ---------------- 用户动态发布后：4s 轮询 AI 评论（最多 ~30s） ---------------- */

  const momentsRef = useRef(moments);
  momentsRef.current = moments;
  const onMomentsUpdatedRef = useRef(onMomentsUpdated);
  onMomentsUpdatedRef.current = onMomentsUpdated;
  const pollDeadlineRef = useRef(0);

  // 新的用户动态出现（发布/首次加载）时重新开始 30s 窗口
  const userMomentKey = useMemo(
    () =>
      moments
        .filter((moment) => moment.author_type === "user")
        .map((moment) => moment.id)
        .join(","),
    [moments],
  );

  useEffect(() => {
    if (!userMomentKey) return;
    pollDeadlineRef.current = Date.now() + 30000;
  }, [userMomentKey]);

  useEffect(() => {
    const timer = setInterval(() => {
      const pending = momentsRef.current.some(
        (moment) => moment.author_type === "user" && moment.comments.length === 0,
      );
      if (!pending) return;
      if (Date.now() > pollDeadlineRef.current) return;
      // 页面不可见时暂停轮询
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      void (async () => {
        try {
          const list = await api.moments();
          onMomentsUpdatedRef.current(list);
        } catch {
          /* 轮询失败静默，下次再试 */
        }
      })();
    }, 4000);
    return () => clearInterval(timer);
  }, []);

  const toggleMomentTranslation = async (moment: Moment) => {
    const existing = momentTranslations[moment.id];
    if (existing) {
      setMomentTranslations((prev) => ({
        ...prev,
        [moment.id]: { ...existing, open: !existing.open },
      }));
      return;
    }
    if (translatingMoments[moment.id]) return;
    setTranslatingMoments((prev) => ({ ...prev, [moment.id]: true }));
    try {
      const result = await translateText({
        text: moment.content,
        target: "zh",
        friend_id: moment.friend_id,
      });
      if (!result.ok) {
        showToast(result.message || "翻译失败，请稍后重试", "error");
        return;
      }
      if (!result.translation.trim() || result.translation.trim() === moment.content.trim()) {
        showToast(`原文已经是${result.target_label || "中文"}，无需翻译`, "info");
        return;
      }
      setMomentTranslations((prev) => ({ ...prev, [moment.id]: { result, open: true } }));
    } catch (err) {
      showToast(apiErrorText(err), "error");
    } finally {
      setTranslatingMoments((prev) => {
        const next = { ...prev };
        delete next[moment.id];
        return next;
      });
    }
  };

  const handleTranslateAllMoments = async () => {
    if (translatingAll) return;
    const pending = moments.filter(
      (moment) => moment.author_type !== "user" && !momentTranslations[moment.id]?.result,
    );
    if (pending.length === 0) {
      showToast("所有动态都翻译过了", "info");
      return;
    }
    setTranslatingAll(true);
    try {
      const response = await translateBatch({
        texts: pending.map((moment) => moment.content),
        target: "zh",
      });
      const next: Record<string, { result: TranslationResult; open: boolean }> = {
        ...momentTranslations,
      };
      let added = 0;
      pending.forEach((moment, index) => {
        const item = response.items[index];
        if (item && item.ok && item.translation.trim() !== moment.content.trim()) {
          next[moment.id] = { result: item, open: true };
          added += 1;
        }
      });
      if (added > 0) {
        setMomentTranslations(next);
        showToast(`已翻译 ${added} 条动态`, "success");
      } else {
        showToast(response.message || "动态已经是中文，无需翻译", response.ok ? "info" : "error");
      }
    } catch (err) {
      showToast(apiErrorText(err), "error");
    } finally {
      setTranslatingAll(false);
    }
  };

  return (
    <div className="pb-3">
      {/* ------------------------------------------------ 偶遇卡片 */}
      <div className="se-hero">
        <div className="flex items-start justify-between gap-2">
          <div>
            <p className="flex items-center gap-1.5 text-[15px] font-semibold text-white">
              <Compass size={16} />
              偶遇一位新朋友
            </p>
            <p className="mt-1 text-[11.5px] leading-relaxed text-white/80">
              捏一个真实的外国朋友，他们会先发来一条破冰语音。
            </p>
          </div>
          <span className="text-[26px]" aria-hidden>
            🌍
          </span>
        </div>

        <div className="mt-3 flex flex-col gap-2">
          <div className="flex gap-2">
            <select
              aria-label="国家筛选"
              value={nationality}
              onChange={(event) => setNationality(event.target.value)}
              className="se-filter"
            >
              <option value="">🌍 国家不限</option>
              {countryOptions.map((country) => {
                const meta = countryMeta(country);
                return (
                  <option key={country} value={country}>
                    {meta.flag} {meta.label}
                  </option>
                );
              })}
            </select>
            <select
              aria-label="语种筛选"
              value={language}
              onChange={(event) => setLanguage(event.target.value)}
              className="se-filter"
            >
              <option value="">🗣 语种不限</option>
              {languageOptions.map((code) => {
                const meta = languageMeta(code);
                return (
                  <option key={code} value={code}>
                    {meta.flag} {langLabel(code)}
                  </option>
                );
              })}
            </select>
          </div>
          <div className="flex gap-2">
            <select
              aria-label="性别筛选"
              value={gender}
              onChange={(event) => setGender(event.target.value)}
              className="se-filter"
            >
              {GENDERS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={discovering}
              onClick={() => onDiscover({ nationality, language, gender })}
              className="se-discover-btn"
            >
              {discovering ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}
              {discovering ? "正在偶遇…" : "偶遇"}
            </button>
          </div>
          {nationality || language ? (
            <p className="se-filter-hint">
              严格匹配：
              {nationality ? `${countryMeta(nationality).flag} ${countryMeta(nationality).label}` : "任意国家"}
              {" · "}
              {language ? `${languageMeta(language).flag} ${langLabel(language)}` : "任意语种"}
            </p>
          ) : null}
        </div>
      </div>

      {/* ------------------------------------------------ 好友申请 */}
      {requests.length > 0 ? (
        <div className="se-card mx-3 mt-3 overflow-hidden">
          <div className="se-card-head">
            <span className="flex items-center gap-1.5">
              <Users size={14} className="text-[color:var(--se-green)]" />
              新朋友想认识你
            </span>
            <span className="se-badge">{requests.length}</span>
          </div>
          {requests.map((friend) => (
            <div key={friend.id} className="flex items-center gap-2.5 border-t border-black/[0.05] px-3 py-2.5">
              <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={42} />
              <button type="button" onClick={() => onOpenChat(friend)} className="min-w-0 flex-1 text-left">
                <p className="truncate text-[14px] font-medium">{friend.name}</p>
                <p className="truncate text-[11.5px] text-[color:var(--se-dim)]">
                  {flagOf(friend)} {friend.city || friend.nationality} · 想加你为好友
                </p>
              </button>
              <button type="button" onClick={() => onDecline(friend)} className="se-mini-btn ghost" aria-label="忽略">
                <X size={14} />
              </button>
              <button
                type="button"
                onClick={() => onAccept(friend)}
                disabled={acceptingId === friend.id}
                className="se-mini-btn primary"
              >
                <Check size={14} />
                {acceptingId === friend.id ? "…" : "接受"}
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {/* ------------------------------------------------ 朋友圈动态 */}
      <div className="mt-4 flex items-center justify-between px-4">
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-[color:var(--se-text)]">
          <MessageCircle size={14} />
          朋友圈动态
        </span>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => void handleTranslateAllMoments()}
            disabled={translatingAll || moments.length === 0}
            className="flex items-center gap-1 text-[11.5px] text-[color:var(--se-green)] disabled:opacity-50"
          >
            {translatingAll ? <Loader2 size={12} className="animate-spin" /> : <Languages size={12} />}
            {translatingAll ? "翻译中…" : "全部翻译"}
          </button>
          <button
            type="button"
            onClick={onGenerate}
            disabled={generating}
            className="flex items-center gap-1 text-[11.5px] text-[color:var(--se-green)] disabled:opacity-50"
          >
            {generating ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            {generating ? "生成中…" : "换一批"}
          </button>
        </div>
      </div>

      <PublishMoment publishing={publishingMoment} onPublish={onPublishMoment} />

      {error ? <ErrorBanner message={`动态加载失败：${error}`} onRetry={onRetry} /> : null}

      {momentsLoading && moments.length === 0 ? (
        <Spinner label="正在看看大家今天在干嘛…" />
      ) : null}

      {!momentsLoading && moments.length === 0 && !error ? (
        <EmptyState
          title="今天还没有动态"
          hint="让好友们发点生活碎片吧，你可以按住语音给他们评论。"
          icon={<span className="text-[26px]">🌤️</span>}
          action={
            <button type="button" onClick={onGenerate} disabled={generating} className="se-cta">
              {generating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
              生成今日动态
            </button>
          }
        />
      ) : null}

      <div className="mt-2 flex flex-col gap-2.5 px-3">
        {moments.map((moment) => {
          const isUser = moment.author_type === "user";
          const friend = moment.friend;
          const local = friend ? localStateOf(friend) : null;
          const comments = moment.comments.slice(0, 3);
          const remainingComments = Math.max(0, moment.comments.length - comments.length);
          const waitingForAi = isUser && comments.length === 0 && commentingIds.includes(moment.id);
          const displayName = isUser ? moment.author_name || "我" : friend?.name ?? (moment.author_name || "好友");
          return (
            <article key={moment.id} className={`se-moment ${isUser ? "mine" : ""}`}>
              <div className="flex items-center gap-2.5">
                {isUser ? (
                  <Avatar name="我" seed="me-speakeasy" size={40} flag={false} />
                ) : friend ? (
                  <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={40} />
                ) : (
                  <Avatar name={displayName} seed={moment.friend_id} size={40} />
                )}
                {isUser || !friend ? (
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[14px] font-medium">{displayName}</p>
                    <p className="truncate text-[11px] text-[color:var(--se-dim)]">
                      {isUser ? "我的动态 · 好友会来评论" : "远方"}
                    </p>
                  </div>
                ) : (
                  <button type="button" onClick={() => onOpenChat(friend)} className="min-w-0 flex-1 text-left">
                    <p className="truncate text-[14px] font-medium">{displayName}</p>
                    <p className="truncate text-[11px] text-[color:var(--se-dim)]">
                      {flagOf(friend)} {friend.city || friend.nationality}
                      {local ? ` · ${local.time}` : moment.local_time ? ` · ${moment.local_time}` : ""}
                    </p>
                  </button>
                )}
                <span className="text-[10.5px] text-[color:var(--se-faint)]">
                  {relativeLabel(moment.created_at)}
                </span>
              </div>

              {moment.content ? (
                <p className="mt-2 whitespace-pre-wrap text-[13.5px] leading-relaxed text-[color:var(--se-text)]">
                  {moment.content}
                </p>
              ) : null}

              {moment.image_url ? (
                <img
                  src={moment.image_url}
                  alt={moment.content || "动态图片"}
                  className="se-moment-image"
                  loading="lazy"
                />
              ) : null}

              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                {moment.mood_tag ? <span className="se-mood">#{moment.mood_tag}</span> : null}
                {!isUser ? (
                  <button
                    type="button"
                    className={`se-translate-btn ${momentTranslations[moment.id] ? "done" : ""}`}
                    aria-label="翻译这条动态"
                    aria-expanded={momentTranslations[moment.id]?.open === true}
                    disabled={translatingMoments[moment.id] === true}
                    onClick={() => void toggleMomentTranslation(moment)}
                  >
                    {translatingMoments[moment.id] ? (
                      <Loader2 size={10} className="animate-spin" />
                    ) : (
                      <Languages size={11} />
                    )}
                    {translatingMoments[moment.id] ? "翻译中…" : "译"}
                  </button>
                ) : null}
                {!isUser && momentTranslations[moment.id]?.result.cached ? (
                  <span className="se-translate-cached">缓存</span>
                ) : null}
              </div>

              {!isUser && momentTranslations[moment.id]?.open ? (
                <button
                  type="button"
                  className="se-translation mt-1.5"
                  onClick={() => void toggleMomentTranslation(moment)}
                  aria-label="收起译文"
                >
                  <span className="se-translation-head">
                    <Languages size={10} />
                    译 · {momentTranslations[moment.id]?.result.target_label || "简体中文"}
                    <span className="se-translation-collapse">收起</span>
                  </span>
                  <span className="se-translation-text">
                    {momentTranslations[moment.id]?.result.translation}
                  </span>
                </button>
              ) : null}

              {comments.length > 0 || isUser ? (
                <div className="se-moment-comments">
                  {comments.map((comment) => {
                    const commentFriend = comment.friend;
                    return (
                      <div key={comment.id} className="se-comment">
                        <Avatar
                          name={commentFriend?.name ?? "AI 好友"}
                          seed={commentFriend?.avatar_seed ?? comment.friend_id}
                          friend={commentFriend ?? undefined}
                          size={22}
                          flag={false}
                        />
                        <div className="min-w-0 flex-1">
                          <p className="se-comment-name">{commentFriend?.name ?? "AI 好友"}</p>
                          <p className="se-comment-text">{comment.content}</p>
                        </div>
                      </div>
                    );
                  })}
                  {comments.length === 0 ? (
                    waitingForAi ? (
                      <p className="se-comment-pending">
                        <Loader2 size={11} className="animate-spin" />
                        AI 好友正在评论…
                      </p>
                    ) : (
                      <p className="se-comment-pending">等好友来评论…</p>
                    )
                  ) : null}
                  {remainingComments > 0 ? (
                    <p className="se-comment-more">还有 {remainingComments} 条评论</p>
                  ) : null}
                </div>
              ) : null}

              {!isUser ? (
                <div className="mt-2.5 flex items-center justify-between gap-2 border-t border-black/[0.05] pt-2">
                  <span className="text-[10.5px] text-[color:var(--se-faint)]">
                    {moment.replied ? "已评论 · 话题已带进私聊" : "按住语音评论，把话题带进私聊"}
                  </span>
                  <div className="flex items-center gap-1.5">
                    <button
                      type="button"
                      onClick={() => friend && onOpenChat(friend)}
                      className="se-moment-open"
                      aria-label="打开私聊"
                    >
                      私聊
                    </button>
                    <HoldToTalk
                      compact
                      label="按住 评论"
                      disabled={commentingId === moment.id}
                      onComplete={async (result) => {
                        setCommentingId(moment.id);
                        try {
                          await onCommentVoice(moment, result);
                        } finally {
                          setCommentingId(null);
                        }
                      }}
                      onUnavailable={(message) => showToast(message, "info")}
                      onError={(message) => showToast(message, "error")}
                    />
                  </div>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>

      {loading && moments.length === 0 ? <Spinner /> : null}
    </div>
  );
}
