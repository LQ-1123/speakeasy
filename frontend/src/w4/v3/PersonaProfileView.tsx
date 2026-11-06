"use client";

/**
 * V3 · Persona 主页。
 *
 * 信息流里点作者进入：头像、昵称、AI 身份说明、介绍、性格、兴趣标签、动态列表，
 * 以及「加好友 / 去聊天 / 处理好友申请」三种状态入口。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, Check, MessageCircle, Sparkles, UserPlus, X } from "lucide-react";

import type { Route } from "../model";
import { useApp } from "../store";
import { personaAvatarSrc } from "../avatar";
import { Avatar, Icon } from "../ui";
import {
  acceptEvent,
  addFriend,
  commentPersonaPost,
  declineEvent,
  fetchEvents,
  fetchPersona,
  languageLabel,
  likePersonaPost,
  relTimeOf,
  type FeedItem,
  type PersonaProfile,
} from "./api";
import { FeedCard } from "./FeedView";
import "./v3.css";

export function PersonaProfileView({
  personaId,
  navigate,
}: {
  personaId: string;
  navigate: (r: Route) => void;
}) {
  const { reloadAll, showToast } = useApp();
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

  const doAddFriend = async () => {
    if (!profile) return;
    setBusy(true);
    try {
      await addFriend(personaId, `Hi ${profile.nickname}! I saw your post and wanted to say hi.`);
      await reloadAll();
      await load();
      showToast(`已添加 ${profile.nickname}，可以开始聊天了`, "success");
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
    } catch {
      showToast("评论失败");
    }
    setDraft("");
    setCommentFor(null);
  };

  const friendship = profile?.friendship;
  const isFriend = friendship?.status === "accepted";
  const isPending = friendship?.status === "pending";

  return (
    <div className="w4-main">
      <div className="w4-head">
        <button className="w4-icon-btn" onClick={() => navigate({ name: "discover" })} aria-label="返回">
          <Icon strokeWidth={2}>
            <path d="M15 5l-7 7 7 7" />
          </Icon>
        </button>
        <div className="w4-head-main">
          <div className="w4-head-title">{profile?.nickname ?? "人物主页"}</div>
          <div className="w4-head-sub">AI 人物主页</div>
        </div>
      </div>

      <div className="w4-scroll">
        {loading && !profile ? (
          <div className="v3-profile-loading">加载中…</div>
        ) : error && !profile ? (
          <div className="v3-error">
            <div className="v3-error-title">主页加载失败</div>
            <div className="v3-error-text">{error}</div>
            <button className="w4-btn is-primary" onClick={() => void load()}>
              重试
            </button>
          </div>
        ) : profile ? (
          <div className="v3-profile">
            <div className="v3-profile-hero">
              <div
                className="v3-profile-cover"
                style={{ backgroundImage: `url(${personaAvatarSrc(profile.avatar_seed || profile.id, profile.avatar_style, profile.avatar_options, profile.avatar)})` }}
                aria-hidden
              />
              <div className="v3-profile-id">
                <Avatar
                  src={personaAvatarSrc(
                    profile.avatar_seed || profile.id,
                    profile.avatar_style,
                    profile.avatar_options,
                    profile.avatar,
                  )}
                  name={profile.nickname}
                  size={78}
                  round
                />
                <div className="v3-profile-name">
                  {profile.nickname}
                  <span className="v3-ai-tag">AI 人物</span>
                </div>
                <div className="v3-profile-meta">
                  {profile.age ? `${profile.age} 岁 · ` : ""}
                  {profile.occupation}
                  {profile.city ? ` · ${profile.city}` : ""}
                  {profile.country ? `，${profile.country}` : ""}
                  {` · 母语：${languageLabel(profile.language)}`}
                </div>
              </div>
            </div>

            <div className="v3-profile-body">
              <p className="v3-profile-bio">{profile.bio}</p>
              {profile.personality ? (
                <div className="v3-profile-section">
                  <span className="v3-profile-label">性格</span>
                  <p>{profile.personality}</p>
                </div>
              ) : null}
              {profile.interests.length ? (
                <div className="v3-profile-section">
                  <span className="v3-profile-label">兴趣</span>
                  <div className="v3-tags">
                    {profile.interests.map((t) => (
                      <span key={t} className="v3-tag">
                        #{t}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}

              <div className="v3-profile-actions">
                {isFriend ? (
                  <button
                    className="w4-btn is-primary"
                    onClick={() =>
                      friendship?.friend_id
                        ? navigate({ name: "chat", friendId: friendship.friend_id })
                        : void load()
                    }
                  >
                    <MessageCircle size={15} /> 去聊天
                  </button>
                ) : isPending ? (
                  <>
                    <button className="w4-btn is-primary" disabled={busy} onClick={() => void respondRequest(true)}>
                      <Check size={15} /> 接受好友申请
                    </button>
                    <button className="w4-btn" disabled={busy} onClick={() => void respondRequest(false)}>
                      <X size={15} /> 拒绝
                    </button>
                  </>
                ) : (
                  <button className="w4-btn is-primary" disabled={busy} onClick={() => void doAddFriend()}>
                    <UserPlus size={15} /> {busy ? "添加中…" : "添加好友"}
                  </button>
                )}
              </div>
            </div>

            <div className="v3-profile-posts">
              <div className="v3-profile-posts-title">
                TA 的动态 <span>{profile.post_count}</span>
              </div>
              {profile.posts.items.length === 0 ? (
                <div className="v3-empty">
                  <div className="v3-empty-text">还没有发过动态。</div>
                </div>
              ) : (
                profile.posts.items.map((item) => (
                  <FeedCard
                    key={item.id}
                    item={item}
                    onOpenPersona={() => undefined}
                    onLike={() => void toggleLike(item)}
                    onComment={() => setCommentFor(commentFor === item.id ? null : item.id)}
                    onQuickAdd={() => void doAddFriend()}
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
            </div>
            <div style={{ height: 24 }} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** 未使用占位，避免打包器把 lucide 图标摇掉（保持与 FeedView 一致的图标集）。 */
export { ArrowLeft };
