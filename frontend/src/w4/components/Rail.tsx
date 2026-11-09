"use client";

import { useShallow } from "zustand/react/shallow";

/**
 * Desktop rail: messages / contacts / discover / me with an expressive tonal
 * selected state. The mobile tab bar mirrors the same destinations.
 */

import { Compass, MessageCircle, Sparkles, UsersRound } from "lucide-react";

import type { Route, Section } from "../model";
import { useApp } from "../store";
import { Avatar } from "../ui";
import { haptic } from "../../theme/haptics";

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
  const { profile } = useApp(useShallow((state) => ({ profile: state.profile })));

  const items: Array<{
    key: Section;
    label: string;
    icon: React.ReactNode;
    route: Route;
    badge?: number;
  }> = [
    {
      key: "chats",
      label: "消息",
      icon: <MessageCircle size={22} strokeWidth={1.7} />,
      route: { name: "chats" },
      badge: unread,
    },
    {
      key: "contacts",
      label: "通讯录",
      icon: <UsersRound size={22} strokeWidth={1.7} />,
      route: { name: "contacts" },
      badge: requests,
    },
    {
      key: "discover",
      label: "发现",
      icon: <Compass size={22} strokeWidth={1.7} />,
      route: { name: "discover" },
    },
  ];

  return (
    <nav className="w4-rail" aria-label="主导航">
      <button
        className="w4-rail-brand"
        onClick={() => {
          haptic("selection");
          navigate({ name: "chats" });
        }}
        aria-label="SpeakEasy 首页"
      >
        <Sparkles size={22} strokeWidth={1.7} />
      </button>
      <div className="w4-rail-top-space" style={{ height: 4 }} />
      {items.map((item) => (
        <button
          key={item.key}
          className={`w4-rail-btn ${active === item.key ? "on" : ""}`}
          onClick={() => {
            haptic("selection");
            navigate(item.route);
          }}
          aria-label={item.label}
          aria-current={active === item.key ? "page" : undefined}
        >
          {item.icon}
          <span className="w4-rail-tip">{item.label}</span>
          {item.badge ? (
            <span className="w4-rail-badge">{item.badge > 99 ? "99+" : item.badge}</span>
          ) : null}
        </button>
      ))}

      <div className="w4-rail-spacer" />

      <button
        className={`w4-rail-btn ${active === "me" ? "on" : ""}`}
        onClick={() => {
          haptic("selection");
          navigate({ name: "me" });
        }}
        aria-label="我"
        aria-current={active === "me" ? "page" : undefined}
      >
        <span className="w4-rail-avatar">
          <Avatar src={profile.avatar} name={profile.name} size={36} />
        </span>
        <span className="w4-rail-tip">我</span>
      </button>
    </nav>
  );
}
