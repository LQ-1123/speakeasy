"use client";

import { useShallow } from "zustand/react/shallow";

/**
 * 会话列表列。
 *
 * 信息层级按微信/WhatsApp：头像 → 名字（未读加粗）→ 最后一句 →
 * 时间与未读数靠右。列表顺序 = 最近活跃，置顶的「新朋友」不在这里，
 * 而在通讯录里（跟微信的「新的朋友」一致），所以这里只列会话本身。
 */

import { useMemo, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { savedScrollPosition, useScrollMemory } from "../useScrollMemory";
import { Plus, Search } from "lucide-react";

import "../styles/chat.css";

import { listTime } from "../format";

/**
 * 「已看过」的人。后端对每条会话都返回 unread=1，
 * 若直接渲染，进来就是满屏红点，像没清理的测试数据。
 * 这里用 localStorage 记账：点开过的人不再亮红点。
 */
import type { Route } from "../model";
import { useApp } from "../store";
import { Avatar } from "../ui";

export function ChatList({
  route,
  navigate,
}: {
  route: Route;
  navigate: (r: Route) => void;
}) {
  const { friends, messagesOf, typing, bootError, reloadAll } = useApp(useShallow((state) => ({ friends: state.friends, messagesOf: state.messagesOf, typing: state.typing, bootError: state.bootError, reloadAll: state.reloadAll })));
  const scrollRef = useScrollMemory<HTMLDivElement>("messages");
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
  const virtualizer = useVirtualizer({ count: rows.length, getScrollElement: () => scrollRef.current, estimateSize: () => 88, overscan: 6, initialOffset: savedScrollPosition("messages") ?? 0 });

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
              aria-label="搜索好友、城市或消息"
            />
          </label>
        </div>
      ) : null}

      {bootError ? <div className="se-connection-error" role="alert"><span>{bootError}</span><button onClick={() => void reloadAll()}>重试</button></div> : null}
      <div className="w4-list-scroll" ref={scrollRef}>
        {rows.length === 0 && !query.trim() ? <div className="se-empty"><h2>还没有会话</h2><p>到发现认识一位语伴，开始第一段对话。</p><button className="w4-btn is-primary" onClick={() => navigate({ name: "discoverPeople" })}>认识语伴</button></div> : null}
        {rows.length === 0 && query.trim() ? (
          <div className="se-list-empty">没有匹配的会话</div>
        ) : null}
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((item) => {
          const { friend, last } = rows[item.index];
          const isTyping = typing[friend.id];
          const preview = previewOf(last, friend);
          const active = activeId === friend.id;
          return (
            <button
              key={friend.id}
              style={{ position: "absolute", top: 0, left: 6, width: "calc(100% - 12px)", height: item.size, transform: `translateY(${item.start}px)` }}
              className={`w4-row ${active ? "on" : ""}`}
              aria-current={active ? "page" : undefined}
              onClick={() => navigate({ name: "chat", friendId: friend.id })}
            >
              <Avatar friend={friend} size={48} online={friend.online} />
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
                  {friend.unread > 0 ? (
                    <span className="w4-unread">{friend.unread > 99 ? "99+" : friend.unread}</span>
                  ) : null}
                </span>
              </span>
            </button>
          );
        })}
        </div>
      </div>

    </>
  );
}

function previewOf(last: { kind: string; text: string; from: string } | undefined, friend: { name: string }) {
  if (!last) return "打个招呼吧";
  const isMe = last.from === "me";
  switch (last.kind) {
    case "voice":
      return isMe ? "[语音] 我说了一句" : "[语音] " + truncate(last.text, 0);
    case "image":
      return "[图片]";
    case "call":
      return "[通话] 已结束";
    default:
      return (isMe ? "我：" : "") + truncate(last.text, 0);
  }
}

/** n<=0 表示不截断：宽度交给 CSS ellipsis，能撑满整行 */
function truncate(s: string, n: number) {
  const t = (s || "").replace(/\s+/g, " ").trim();
  if (n <= 0) return t;
  return t.length > n ? t.slice(0, n) + "…" : t;
}
