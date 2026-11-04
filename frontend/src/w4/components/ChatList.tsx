"use client";

/**
 * 会话列表列。
 *
 * 信息层级按微信/WhatsApp：头像 → 名字（未读加粗）→ 最后一句 →
 * 时间与未读数靠右。列表顺序 = 最近活跃，置顶的「新朋友」不在这里，
 * 而在通讯录里（跟微信的「新的朋友」一致），所以这里只列会话本身。
 */

import { useMemo, useState } from "react";
import { Plus, Search } from "lucide-react";

import { listTime } from "../format";

/**
 * 「已看过」的人。后端对每条会话都返回 unread=1，
 * 若直接渲染，进来就是满屏红点，像没清理的测试数据。
 * 这里用 localStorage 记账：点开过的人不再亮红点。
 */
function seen(friendId: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem("w4.seen." + friendId) === "1";
  } catch {
    return false;
  }
}

export function markSeen(friendId: string) {
  try {
    window.localStorage.setItem("w4.seen." + friendId, "1");
  } catch {
    /* 隐私模式下忽略 */
  }
}

/**
 * 首次打开时建立基线：把「当时看到的所有会话」记为已读。
 * 这样第一屏是干净的（跟微信一致），此后只有新消息才亮红点。
 */
export function ensureBaseline(friendIds: string[]) {
  try {
    if (window.localStorage.getItem("w4.baseline") === "1") return;
    friendIds.forEach((id) => window.localStorage.setItem("w4.seen." + id, "1"));
    window.localStorage.setItem("w4.baseline", "1");
  } catch {
    /* 忽略 */
  }
}
import type { Route } from "../model";
import { useApp } from "../store";
import { Avatar, Icon } from "../ui";

export function ChatList({
  route,
  navigate,
}: {
  route: Route;
  navigate: (r: Route) => void;
}) {
  const { friends, messagesOf, typing, requests } = useApp();
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);

  const rows = useMemo(() => {
    const withTime = friends.map((f) => {
      const msgs = messagesOf(f.id);
      const last = msgs[msgs.length - 1];
      return { friend: f, last, at: last ? last.time : "" };
    });
    withTime.sort((a, b) => new Date(b.at || 0).getTime() - new Date(a.at || 0).getTime());
    if (!query.trim()) return withTime;
    const q = query.trim().toLowerCase();
    return withTime.filter(
      (r) =>
        r.friend.name.toLowerCase().includes(q) ||
        r.friend.city.toLowerCase().includes(q) ||
        (r.last?.text ?? "").toLowerCase().includes(q),
    );
  }, [friends, messagesOf, query]);

  const activeId = route.name === "chat" ? route.friendId : null;

  return (
    <>
      <div className="w4-list-head">
        <span className="w4-list-title">消息</span>
        <div className="w4-head-actions">
          <button
            className={`w4-icon-btn ${searchOpen ? "is-active" : ""}`}
            aria-label="搜索"
            onClick={() => setSearchOpen((v) => !v)}
          >
            <Search size={17} strokeWidth={1.8} />
          </button>
          <button
            className="w4-icon-btn"
            aria-label="认识新朋友"
            onClick={() => navigate({ name: "discover" })}
          >
            <Plus size={18} strokeWidth={1.8} />
          </button>
        </div>
      </div>

      {searchOpen ? (
        <div className="w4-searchwrap">
          <label className="w4-search">
            <Search size={14} strokeWidth={2} />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索好友、城市或消息"
            />
          </label>
        </div>
      ) : null}

      <div className="w4-list-scroll">
        {rows.length === 0 ? (
          <div style={{ padding: "40px 24px", textAlign: "center", fontSize: 13, color: "var(--w-text-3)" }}>
            没有匹配的会话
          </div>
        ) : null}
        {rows.map(({ friend, last }) => {
          const isTyping = typing[friend.id];
          const preview = previewOf(last, friend);
          return (
            <button
              key={friend.id}
              className={`w4-row ${activeId === friend.id ? "on" : ""}`}
              onClick={() => {
                markSeen(friend.id);
                navigate({ name: "chat", friendId: friend.id });
              }}
            >
              <Avatar friend={friend} size={40} online={friend.online} />
              <span className="w4-row-body">
                <span className="w4-row-top">
                  <span className="w4-row-name">
                    <span className="w4-ellipsis">{friend.name}</span>
                    <span className="w4-lang-tag">{friend.languageLabel.slice(0, 2)}</span>
                  </span>
                  <span className="w4-row-time">{listTime(last?.time ?? "")}</span>
                </span>
                <span className="w4-row-bottom">
                  <span className="w4-row-preview">
                    {isTyping ? (
                      <span className="w4-prev-typing">正在说话…</span>
                    ) : (
                      <span className={last?.from === "me" ? "w4-prev-me" : ""}>{preview}</span>
                    )}
                  </span>
                  {friend.unread && !seen(friend.id) ? (
                    <span className="w4-unread">{friend.unread > 99 ? "99+" : friend.unread}</span>
                  ) : null}
                </span>
              </span>
            </button>
          );
        })}
      </div>

    </>
  );
}

function previewOf(last: { kind: string; text: string; from: string } | undefined, friend: { name: string }) {
  if (!last) return "打个招呼吧";
  const isMe = last.from === "me";
  switch (last.kind) {
    case "voice":
      return isMe ? "[语音] 我说了一句" : "[语音] " + truncate(last.text, 22);
    case "image":
      return "[图片]";
    case "call":
      return "[通话] 已结束";
    default:
      return (isMe ? "我：" : "") + truncate(last.text, 24);
  }
}

function truncate(s: string, n: number) {
  const t = (s || "").replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n) + "…" : t;
}
