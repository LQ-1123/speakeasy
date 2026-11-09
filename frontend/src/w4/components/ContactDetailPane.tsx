"use client";

import { useShallow } from "zustand/react/shallow";

/** 通讯录 — 好友详情（Material 3 Expressive：头像身份 → 资料 → 动态 → 操作）。 */

import { useEffect, useState } from "react";
import { MessageCircle, Mic, Trash2 } from "lucide-react";

import { CLOSENESS, listTime } from "../format";
import type { Route } from "../model";
import { useApp } from "../store";
import { fetchFriendPosts, type FriendPostItem } from "../v3/api";
import { Avatar, Confirm, Icon } from "../ui";

export function ContactDetailPane({
  friendId,
  onBack,
  navigate,
}: {
  friendId: string;
  onBack: () => void;
  navigate: (r: Route) => void;
}) {
  const { friendById, messagesOf, deleteFriend, expressions } = useApp(useShallow((state) => ({ friendById: state.friendById, messagesOf: state.messagesOf, deleteFriend: state.deleteFriend, expressions: state.expressions })));
  const friend = friendById(friendId);
  const [confirm, setConfirm] = useState(false);
  const [posts, setPosts] = useState<FriendPostItem[]>([]);

  // 注意：hooks 必须放在 friend 的空值提前 return 之前，否则好友数据异步到达时 hooks 数量变化会崩
  useEffect(() => {
    const fid = friend?.id;
    if (!fid) {
      setPosts([]);
      return;
    }
    let alive = true;
    setPosts([]);
    void fetchFriendPosts(fid, 6)
      .then((res) => {
        if (alive) setPosts(res.items || []);
      })
      .catch(() => {
        if (alive) setPosts([]);
      });
    return () => {
      alive = false;
    };
  }, [friend?.id]);

  if (!friend) {
    return (
      <div className="w4-main">
        <div className="w4-head se-pane-head">
          <button className="w4-icon-btn w4-only-mobile" onClick={onBack} aria-label="返回">
            <Icon strokeWidth={2}>
              <path d="M15 5l-7 7 7 7" />
            </Icon>
          </button>
          <div className="w4-head-main">
            <div className="w4-head-title">好友不存在</div>
          </div>
        </div>
        <div className="w4-centered">
          <div className="se-muted-text">这个人可能已经被删除了。</div>
        </div>
      </div>
    );
  }

  const msgs = messagesOf(friend.id);
  const saved = expressions.filter((e) => e.sourceFriendId === friend.id);

  return (
    <div className="w4-main">
      <div className="w4-head se-pane-head">
        <button className="w4-icon-btn w4-only-mobile" onClick={onBack} aria-label="返回">
          <Icon strokeWidth={2}>
            <path d="M15 5l-7 7 7 7" />
          </Icon>
        </button>
        <div className="w4-head-main">
          <div className="w4-head-title">好友资料</div>
        </div>
      </div>

      <div className="w4-scroll">
        <div className="se-detail">
          <div className="se-detail-card">
            <div className="se-detail-head">
              <Avatar friend={friend} size={72} round online={friend.online} />
              <div className="se-detail-identity">
                <div className="se-detail-name">
                  {friend.name} <span className="se-info-flag">{friend.flag}</span>
                </div>
                <div className="se-detail-meta">
                  {friend.age} 岁 · {friend.city} · {friend.occupation}
                </div>
                <div className="w4-chips se-detail-chips">
                  <span className="w4-chip is-green">{CLOSENESS[friend.closeness]}</span>
                  <span className="w4-chip">{friend.languageLabel}</span>
                  <span className="w4-chip">{friend.voiceLabel}</span>
                </div>
              </div>
            </div>

            <p className="se-detail-bio">{friend.bio || "这个人还没写简介。"}</p>

            <div className="se-detail-rows">
              <div className="se-detail-row">
                <span className="se-detail-key">当地</span>
                <span>
                  {friend.localTime} · {friend.localPeriod}
                  {friend.localActivity ? `，${friend.localActivity}` : ""}
                </span>
              </div>
              <div className="se-detail-row">
                <span className="se-detail-key">说话方式</span>
                <span>{friend.quirks.join("、") || "没什么特别的"}</span>
              </div>
              <div className="se-detail-row">
                <span className="se-detail-key">共同经历</span>
                <span>
                  {friend.sharedEpisodes.length
                    ? friend.sharedEpisodes.join("；")
                    : "还没什么共同经历"}
                </span>
              </div>
              <div className="se-detail-row">
                <span className="se-detail-key">一起练过</span>
                <span>
                  {friend.turns} 轮对话
                  {saved.length ? `，收藏了 ${saved.length} 条表达` : ""}
                </span>
              </div>
              <div className="se-detail-row">
                <span className="se-detail-key">聊天记录</span>
                <span>{msgs.length} 条消息</span>
              </div>
            </div>

            {posts.length ? (
              <div className="se-detail-posts">
                <div className="w4-info-label">TA 的动态</div>
                {posts.map((post) => (
                  <div key={post.id} className="se-detail-post">
                    <div className="se-detail-post-head">
                      <Avatar friend={friend} size={28} round />
                      <span className="se-detail-post-meta">
                        {friend.name} · {listTime(post.created_at)}
                      </span>
                    </div>
                    {post.content ? (
                      <p className="se-detail-post-text">{post.content}</p>
                    ) : null}
                    {post.media?.length ? (
                      <div className={`se-detail-imgs n${Math.min(4, post.media.length)}`}>
                        {post.media.slice(0, 4).map((src) => (
                          <img
                            key={src}
                            src={src}
                            alt={post.alt_text || ""}
                            loading="lazy"
                          />
                        ))}
                      </div>
                    ) : null}
                    {post.media_credit ? (
                      <div className="se-detail-credit">图：{post.media_credit}</div>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}

            {saved.length ? (
              <div className="se-detail-saved">
                <div className="w4-info-label">从 TA 这里学到的</div>
                {saved.map((e) => (
                  <div key={e.id} className="se-detail-saved-row">
                    <span>{e.better}</span>
                  </div>
                ))}
              </div>
            ) : null}

            <div className="se-detail-actions">
              <button
                className="w4-btn is-primary"
                onClick={() => navigate({ name: "chat", friendId: friend.id })}
              >
                <MessageCircle size={15} />
                发消息
              </button>
              <button className="w4-btn is-tonal" onClick={() => navigate({ name: "chat", friendId: friend.id })}>
                <Mic size={15} />
                按住说话
              </button>
              <button className="w4-btn is-danger" onClick={() => setConfirm(true)}>
                <Trash2 size={15} />
                删除
              </button>
            </div>
          </div>
        </div>
      </div>

      {confirm ? (
        <Confirm
          title="删除好友"
          text={
            <>
              删除后，你和 {friend.name} 的聊天记录都会消失，无法恢复。
            </>
          }
          confirmText="删除"
          danger
          onCancel={() => setConfirm(false)}
          onConfirm={() => {
            deleteFriend(friend.id);
            setConfirm(false);
            navigate({ name: "contacts" });
          }}
        />
      ) : null}
    </div>
  );
}
