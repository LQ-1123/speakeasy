"use client";

/**
 * 左侧图标栏（桌面）。手机端隐藏，由底部 TabBar 代替。
 * 顺序与微信一致：消息 / 通讯录 / 发现 / 我，下方是自己的头像。
 */

import {
  BookMarked,
  Compass,
  MessageCircle,
  UserRound,
  UsersRound,
} from "lucide-react";

import type { Route, Section } from "../model";
import { useApp } from "../store";
import { Avatar } from "../ui";

export function Rail({
  active,
  unread,
  requests,
  navigate,
}: {
  active: Section;
  unread: number;
  requests: number;
  navigate: (r: Route) => void;
}) {
  const { profile } = useApp();

  const items: Array<{
    key: Section;
    label: string;
    icon: React.ReactNode;
    route: Route;
    badge?: number;
    dot?: boolean;
  }> = [
    {
      key: "chats",
      label: "消息",
      icon: <MessageCircle size={21} strokeWidth={1.7} />,
      route: { name: "chats" },
      badge: unread,
    },
    {
      key: "contacts",
      label: "通讯录",
      icon: <UsersRound size={21} strokeWidth={1.7} />,
      route: { name: "contacts" },
      badge: requests,
      dot: true,
    },
    {
      key: "discover",
      label: "发现",
      icon: <Compass size={21} strokeWidth={1.7} />,
      route: { name: "discover" },
    },
    {
      key: "notes",
      label: "表达本",
      icon: <BookMarked size={21} strokeWidth={1.7} />,
      route: { name: "expressions" },
    },
  ];

  return (
    <nav className="w4-rail" aria-label="主导航">
      <div className="w4-rail-top-space" style={{ height: 8 }} />
      {items.map((item) => (
        <button
          key={item.key}
          className={`w4-rail-btn ${active === item.key ? "on" : ""}`}
          onClick={() => navigate(item.route)}
          aria-label={item.label}
          aria-current={active === item.key ? "page" : undefined}
        >
          {active === item.key ? <span className="w4-rail-pip" /> : null}
          {item.icon}
          {item.badge ? (
            item.dot ? (
              <span
                className="w4-rail-dot"
                aria-label={`${item.badge} 条待处理`}
                title={`${item.badge} 条待处理`}
              />
            ) : (
              <span className="w4-rail-badge">{item.badge > 99 ? "99+" : item.badge}</span>
            )
          ) : null}
          <span className="w4-rail-tip">{item.label}</span>
        </button>
      ))}

      <div className="w4-rail-spacer" />

      <button
        className={`w4-rail-btn ${active === "me" ? "on" : ""}`}
        onClick={() => navigate({ name: "me" })}
        aria-label="我"
      >
        {active === "me" ? <span className="w4-rail-pip" /> : null}
        <span className="w4-rail-avatar">
          <Avatar src={profile.avatar} name={profile.name} size={36} />
        </span>
        <span className="w4-rail-tip">我</span>
      </button>
      <button className="w4-rail-btn" onClick={() => navigate({ name: "me" })} aria-label="个人资料" hidden>
        <UserRound size={18} />
      </button>
    </nav>
  );
}
