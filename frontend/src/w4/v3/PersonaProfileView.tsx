"use client";

import { useShallow } from "zustand/react/shallow";

/**
 * V3 · Persona 主页。
 *
 * 信息流里点作者进入：头像、昵称、AI 身份说明、介绍、性格、兴趣标签、动态列表，
 * 以及「发消息 / 处理好友申请」入口。
 *
 * 视觉：oversized 头像 + 姓名 → 一句主行动 → 次级资料 → 动态流。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ChevronLeft, MessageCircle, X } from "lucide-react";

import type { Route } from "../model";
import { useApp } from "../store";
import { resolveAvatar } from "../avatar";
import { haptic } from "../../theme/haptics";
import { Avatar } from "../ui";
import {
  acceptEvent,
  addFriend,
  commentPersonaPost,
  declineEvent,
  fetchEvents,
  fetchPersona,
  languageLabel,
  likePersonaPost,
  type FeedItem,
  type PersonaProfile,
  type PersonaRelationBrief,
} from "./api";
import { FeedCard } from "./FeedView";

/**
 * 把「TA 身边的人」翻译成一句自然中文。
 *
 * 只做展示：不显示 closeness / tension 数字，也不显示任何内部 ID。
 * 有摩擦的人优先（对用户更有信息量），其次才是常来往的人。
 */
function relationsLine(relations?: PersonaRelationBrief[]): string {
  if (!relations || !relations.length) return "";
  const tense = relations.filter((r) => r.tension >= 3 && r.nickname).slice(0, 2);
  if (tense.length) {
    return `最近和 ${tense.map((r) => r.nickname).join("、")} 有点小摩擦`;
  }
  const close = relations.filter((r) => r.closeness >= 3 && r.nickname).slice(0, 3);
  if (close.length) {
    // 只把「愉快的共同经历」写成例子：吵架/冲突不适合用「一起」的口吻展示
    const recent = close.find(
      (r) => r.recent_event?.title && r.recent_event.event_type !== "conflict",
    );
    const base = `最近常和 ${close.map((r) => r.nickname).join("、")} 一起`;
    return recent?.recent_event?.title ? `${base}（比如「${recent.recent_event.title}」）` : base;
  }
  return "";
}

function flagOf(country: string): string {
  const map: Array<[string, string]> = [
    ["United Kingdom", "🇬🇧"], ["United States", "🇺🇸"], ["Australia", "🇦🇺"],
    ["Brazil", "🇧🇷"], ["Ireland", "🇮🇪"], ["Italy", "🇮🇹"], ["Japan", "🇯🇵"],
    ["Spain", "🇪🇸"], ["Korea", "🇰🇷"], ["China", "🇨🇳"], ["Singapore", "🇸🇬"],
    ["Germany", "🇩🇪"],
  ];
  return map.find(([k]) => (country || "").includes(k))?.[1] ?? "🌍";
}

export function PersonaProfileView({
  personaId,
  navigate,
}: {
  personaId: string;
  navigate: (r: Route) => void;
}) {
  const { reloadAll, showToast } = useApp(
    useShallow((state) => ({ reloadAll: state.reloadAll, showToast: state.showToast })),
  );
  const [profile, setProfile] = useState<PersonaProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [commentFor, setCommentFor] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingEventId, setPendingEventId] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    setError("");
    try {
      const data = await fetchPersona(personaId);
      if (seq.current !== my) return;
      setProfile(data);
      if (data.friendship?.status === "pending") {
        const events = await fetchEvents(false).catch(() => null);
        const ev = events?.items.find(
          (e) => e.type === "friend_request" && e.persona.id === personaId && e.status === "pending",
        );
        setPendingEventId(ev?.id ?? null);
      }
    } catch (exc) {
      if (seq.current === my) setError(exc instanceof Error ? exc.message : "加载失败");
    } finally {
      if (seq.current === my) setLoading(false);
    }
  }, [personaId]);

  useEffect(() => {
    void load();
  }, [load]);

  const friendship = profile?.friendship;
  const isFriend = friendship?.status === "accepted";
  const isPending = friendship?.status === "pending";

  /** 主行动「发消息」：已是好友直接进聊天；否则先加好友再进聊天。 */
  const startChat = async () => {
    if (!profile || busy) return;
    if (isFriend && friendship?.friend_id) {
      navigate({ name: "chat", friendId: friendship.friend_id });
      return;
    }
    setBusy(true);
    try {
      const res = await addFriend(
        personaId,
        `Hi ${profile.nickname}! I saw your post and wanted to say hi.`,
      );
      haptic("save");
      await reloadAll();
      showToast(`已添加 ${profile.nickname}，可以开始聊天了`, "success");
      if (res?.friend_id) {
        navigate({ name: "chat", friendId: res.friend_id });
      } else {
        await load();
      }
    } catch (exc) {
      showToast(exc instanceof Error ? exc.message : "添加失败");
    } finally {
      setBusy(false);
    }
  };

  const respondRequest = async (accept: boolean) => {
    if (!profile) return;
    setBusy(true);
    try {
      if (pendingEventId) {
        if (accept) await acceptEvent(pendingEventId);
        else await declineEvent(pendingEventId);
      } else if (accept) {
        await addFriend(personaId);
      }
      haptic("save");
      await reloadAll();
      await load();
      showToast(accept ? "已接受好友申请" : "已拒绝好友申请", "success");
    } catch (exc) {
      showToast(exc instanceof Error ? exc.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const toggleLike = async (item: FeedItem) => {
    if (!profile) return;
    setProfile((prev) =>
      prev
        ? {
            ...prev,
            posts: {
              ...prev.posts,
              items: prev.posts.items.map((x) =>
                x.id === item.id
                  ? { ...x, liked_by_me: !x.liked_by_me, like_count: x.like_count + (x.liked_by_me ? -1 : 1) }
                  : x,
              ),
            },
          }
        : prev,
    );
    try {
      await likePersonaPost(item.id);
      haptic("save");
    } catch {
      showToast("点赞失败");
    }
  };

  const submitComment = async (item: FeedItem, text: string) => {
    try {
      const c = await commentPersonaPost(item.id, text.trim());
      setProfile((prev) =>
        prev
          ? {
              ...prev,
              posts: {
                ...prev.posts,
                items: prev.posts.items.map((x) =>
                  x.id === item.id
                    ? { ...x, comment_count: x.comment_count + 1, comments: [...x.comments, c].slice(-3) }
                    : x,
                ),
              },
            }
          : prev,
      );
      haptic("send");
    } catch {
      showToast("评论失败");
    }
    setDraft("");
    setCommentFor(null);
  };

  const relations = relationsLine(profile?.relations);
  const meta: string[] = [];
  if (profile) {
    if (profile.age) meta.push(`${profile.age} 岁`);
    if (profile.occupation) meta.push(profile.occupation);
    if (profile.city) meta.push(profile.city);
    if (profile.country) meta.push(profile.country);
  }

  return (
    <div className="w4-main disc-main">
      <div className="w4-head disc-head">
        <button className="w4-icon-btn" onClick={() => navigate({ name: "discover" })} aria-label="返回">
          <ChevronLeft size={20} strokeWidth={2} />
        </button>
        <div className="w4-head-main">
          <div className="w4-head-title disc-head-title">{profile?.nickname ?? "人物主页"}</div>
          <div className="w4-head-sub">AI 人物主页</div>
        </div>
      </div>

      <div className="w4-scroll">
        <div className="w4-feed disc-profile-page">
          {loading && !profile ? (
            <div className="disc-state">
              <div className="disc-spinner" aria-hidden />
              <div className="disc-state-text" style={{ marginTop: 16 }}>
                正在打开主页…
              </div>
            </div>
          ) : error && !profile ? (
            <div className="disc-state">
              <div className="disc-state-title">主页加载失败</div>
              <div className="disc-state-text">{error}</div>
              <button className="w4-btn is-primary" onClick={() => void load()}>
                重试
              </button>
            </div>
          ) : profile ? (
            <>
              <section className="disc-profile-hero" aria-label={`${profile.nickname} 的资料`}>
                <div className="disc-profile-avatar">
                  <Avatar
                    src={resolveAvatar(profile)}
                    name={profile.nickname}
                    size={132}
                    round
                  />
                  <span className="disc-profile-badge">AI</span>
                </div>

                <h1 className="disc-profile-name">
                  {profile.nickname}
                  <span className="disc-profile-flag" aria-label={profile.country || "地区"}>
                    {flagOf(profile.country)}
                  </span>
                </h1>

                <div className="disc-profile-meta">
                  <span className="disc-meta-chip is-accent">{profile.ai_label || "AI 人物"}</span>
                  {meta.length ? <span className="disc-meta-chip">{meta.join(" · ")}</span> : null}
                  <span className="disc-meta-chip">母语 {languageLabel(profile.language)}</span>
                </div>

                {profile.bio ? <p className="disc-profile-bio">{profile.bio}</p> : null}

                <div className="disc-profile-actions">
                  {isPending ? (
                    <>
                      <button
                        className="w4-btn is-primary disc-profile-primary"
                        disabled={busy}
                        onClick={() => void respondRequest(true)}
                      >
                        <Check size={18} />
                        {busy ? "处理中…" : "接受好友申请"}
                      </button>
                      <button
                        className="w4-btn disc-profile-secondary"
                        disabled={busy}
                        onClick={() => void respondRequest(false)}
                      >
                        <X size={17} />
                        拒绝
                      </button>
                    </>
                  ) : (
                    <button
                      className="w4-btn is-primary disc-profile-primary"
                      disabled={busy}
                      onClick={() => void startChat()}
                    >
                      <MessageCircle size={18} />
                      {busy ? "正在打开…" : "发消息"}
                    </button>
                  )}
                </div>
              </section>

              {profile.personality || profile.interests.length || relations ? (
                <section className="disc-about" aria-label="关于 TA">
                  <div className="disc-section-head">
                    <h2 className="disc-section-title">关于 TA</h2>
                  </div>
                  <div>
                    {profile.personality ? (
                      <div className="disc-about-row">
                        <span className="disc-about-label">性格</span>
                        <p className="disc-about-value">{profile.personality}</p>
                      </div>
                    ) : null}
                    {profile.interests.length ? (
                      <div className="disc-about-row">
                        <span className="disc-about-label">兴趣</span>
                        <div className="disc-about-chips">
                          {profile.interests.map((t) => (
                            <span key={t} className="disc-chip">
                              {t}
                            </span>
                          ))}
                        </div>
                      </div>
                    ) : null}
                    {relations ? (
                      <div className="disc-about-row">
                        <span className="disc-about-label">身边的人</span>
                        <p className="disc-about-value">{relations}</p>
                      </div>
                    ) : null}
                  </div>
                </section>
              ) : null}

              <section className="disc-profile-posts" aria-label="TA 的动态">
                <div className="disc-section-head">
                  <h2 className="disc-section-title">TA 的动态</h2>
                  <span className="disc-section-count">{profile.post_count}</span>
                </div>
                {profile.posts.items.length === 0 ? (
                  <div className="disc-state">
                    <div className="disc-state-text">还没有发过动态。</div>
                  </div>
                ) : (
                  profile.posts.items.map((item) => (
                    <FeedCard
                      key={item.id}
                      item={item}
                      onOpenPersona={() => undefined}
                      onLike={() => void toggleLike(item)}
                      onComment={() => setCommentFor(commentFor === item.id ? null : item.id)}
                      onDelete={() => undefined}
                      onZoom={() => undefined}
                      onNotify={showToast}
                      commentOpen={commentFor === item.id}
                      draft={draft}
                      setDraft={setDraft}
                      onSubmitComment={(text) => void submitComment(item, text)}
                    />
                  ))
                )}
              </section>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
