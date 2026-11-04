"use client";

/** 通讯录 — 好友详情（微信资料页的排布：大头像 → 资料 → 操作）。 */

import { useState } from "react";
import { MessageCircle, Mic, Phone, Trash2 } from "lucide-react";

import { CLOSENESS } from "../format";
import type { Route } from "../model";
import { useApp } from "../store";
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
  const { friendById, messagesOf, deleteFriend, expressions } = useApp();
  const friend = friendById(friendId);
  const [confirm, setConfirm] = useState(false);

  if (!friend) {
    return (
      <div className="w4-main">
        <div className="w4-head">
          <button className="w4-icon-btn w4-only-mobile" onClick={onBack} aria-label="返回">
            <Icon strokeWidth={2}>
              <path d="M15 5l-7 7 7 7" />
            </Icon>
          </button>
          <div className="w4-head-main">
            <div className="w4-head-title">好友不存在</div>
          </div>
        </div>
        <div className="w4-centered" style={{ fontSize: 13, color: "var(--w-text-3)" }}>
          这个人可能已经被删除了。
        </div>
      </div>
    );
  }

  const msgs = messagesOf(friend.id);
  const saved = expressions.filter((e) => e.sourceFriendId === friend.id);

  return (
    <div className="w4-main">
      <div className="w4-head">
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
        <div className="w4-detail">
          <div className="w4-detail-card">
            <div className="w4-detail-head">
              <Avatar friend={friend} size={64} round online={friend.online} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="w4-detail-name">
                  {friend.name} <span>{friend.flag}</span>
                </div>
                <div className="w4-detail-meta">
                  {friend.age} 岁 · {friend.city} · {friend.occupation}
                </div>
                <div className="w4-chips" style={{ marginTop: 6 }}>
                  <span className="w4-chip is-green">{CLOSENESS[friend.closeness]}</span>
                  <span className="w4-chip">{friend.languageLabel}</span>
                  <span className="w4-chip">{friend.voiceLabel}</span>
                </div>
              </div>
            </div>

            <p className="w4-detail-bio">{friend.bio || "这个人还没写简介。"}</p>

            <div className="w4-detail-rows">
              <div className="w4-detail-row">
                <span className="w4-detail-key">当地</span>
                <span>
                  {friend.localTime} · {friend.localPeriod}
                  {friend.localActivity ? `，${friend.localActivity}` : ""}
                </span>
              </div>
              <div className="w4-detail-row">
                <span className="w4-detail-key">说话方式</span>
                <span>{friend.quirks.join("、") || "没什么特别的"}</span>
              </div>
              <div className="w4-detail-row">
                <span className="w4-detail-key">共同经历</span>
                <span>
                  {friend.sharedEpisodes.length
                    ? friend.sharedEpisodes.join("；")
                    : "还没什么共同经历"}
                </span>
              </div>
              <div className="w4-detail-row">
                <span className="w4-detail-key">一起练过</span>
                <span>
                  {friend.turns} 轮对话
                  {saved.length ? `，收藏了 ${saved.length} 条表达` : ""}
                </span>
              </div>
              <div className="w4-detail-row">
                <span className="w4-detail-key">聊天记录</span>
                <span>{msgs.length} 条消息</span>
              </div>
            </div>

            {saved.length ? (
              <div className="w4-detail-saved">
                <div className="w4-info-label">从 TA 这里学到的</div>
                {saved.map((e) => (
                  <div key={e.id} className="w4-detail-saved-row">
                    <span>{e.better}</span>
                  </div>
                ))}
              </div>
            ) : null}

            <div className="w4-detail-actions">
              <button
                className="w4-btn"
                onClick={() => navigate({ name: "chat", friendId: friend.id })}
              >
                <MessageCircle size={15} />
                发消息
              </button>
              <button className="w4-btn" onClick={() => navigate({ name: "chat", friendId: friend.id })}>
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
