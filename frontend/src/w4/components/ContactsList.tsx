"use client";

/**
 * 通讯录列。跟微信一样按拼音首字母分组，
 * 顶部是「新的朋友」「我的二维码」这类入口。
 * 好友名字都是英文，所以用名字首字母分组；中文名归到「#」。
 */

import { useMemo, useState } from "react";
import { Search, UserPlus } from "lucide-react";

import type { Route } from "../model";
import { useApp } from "../store";
import { Avatar, Icon } from "../ui";

export function ContactsList({
  route,
  navigate,
  requestsCount,
}: {
  route: Route;
  navigate: (r: Route) => void;
  requestsCount: number;
}) {
  const { friends, messagesOf, typing } = useApp();
  const [query, setQuery] = useState("");

  const groups = useMemo(() => {
    const filtered = friends.filter((f) =>
      query.trim()
        ? f.name.toLowerCase().includes(query.trim().toLowerCase()) ||
          f.city.toLowerCase().includes(query.trim().toLowerCase())
        : true,
    );
    const map = new Map<string, typeof friends>();
    filtered.forEach((f) => {
      const letter = /^[a-z]/i.test(f.name) ? f.name[0].toUpperCase() : "#";
      if (!map.has(letter)) map.set(letter, []);
      map.get(letter)!.push(f);
    });
    return [...map.entries()].sort(([a], [b]) => (a === "#" ? 1 : b === "#" ? -1 : a.localeCompare(b)));
  }, [friends, query]);

  const activeId = route.name === "contact" ? route.friendId : null;

  return (
    <>
      <div className="w4-list-head">
        <span className="w4-list-title">通讯录</span>
        <div className="w4-head-actions">
          <button className="w4-icon-btn" aria-label="搜索" onClick={() => setQuery(query ? "" : " ")}>
            <Search size={17} strokeWidth={1.8} />
          </button>
          <button
            className="w4-icon-btn"
            aria-label="添加好友"
            onClick={() => navigate({ name: "discover" })}
          >
            <UserPlus size={18} strokeWidth={1.8} />
          </button>
        </div>
      </div>

      <div className="w4-searchwrap">
        <label className="w4-search">
          <Search size={14} strokeWidth={2} />
          <input
            value={query.trim()}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索好友"
          />
        </label>
      </div>

      <div className="w4-list-scroll">
        {!query ? (
          <div style={{ paddingBottom: 4 }}>
            <button className="w4-shortcut" onClick={() => navigate({ name: "requests" })}>
              <span className="w4-shortcut-icon" style={{ background: "#e8993a" }}>
                <UserPlus size={18} strokeWidth={1.9} />
              </span>
              <span className="w4-shortcut-label">新的朋友</span>
              {requestsCount ? <span className="w4-unread">{requestsCount}</span> : null}
            </button>
          </div>
        ) : null}

        {groups.map(([letter, list]) => (
          <div key={letter}>
            <div className="w4-group-label">{letter}</div>
            {list.map((f) => {
              const msgs = messagesOf(f.id);
              const last = msgs[msgs.length - 1];
              return (
                <button
                  key={f.id}
                  className={`w4-row ${activeId === f.id ? "on" : ""}`}
                  onClick={() => navigate({ name: "contact", friendId: f.id })}
                >
                  <Avatar friend={f} size={40} online={f.online} />
                  <span className="w4-row-body">
                    <span className="w4-row-top">
                      <span className="w4-row-name">
                        <span className="w4-ellipsis">{f.name}</span>
                        <span className="w4-lang-tag">{f.languageLabel.slice(0, 2)}</span>
                      </span>
                    </span>
                    <span className="w4-row-bottom">
                      <span className="w4-row-preview">
                        {typing[f.id]
                          ? "正在说话…"
                          : last
                            ? f.city + " · " + f.occupation
                            : f.city + " · 还没聊过"}
                      </span>
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        ))}

        {friends.length === 0 ? (
          <div style={{ padding: "40px 24px", textAlign: "center", fontSize: 13, color: "var(--w-text-3)" }}>
            通讯录还是空的。
          </div>
        ) : null}
      </div>
    </>
  );
}
