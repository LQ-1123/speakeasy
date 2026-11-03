"use client";

/** 底部 Tab Bar（固定）：消息 / 动态·偶遇 / 表达本·画像。 */

import { BookOpen, Compass, MessageCircle } from "lucide-react";
import type { Tab } from "@/lib/v2/types";

interface TabBarProps {
  tab: Tab;
  unread: number;
  requests: number;
  onChange: (tab: Tab) => void;
}

const ITEMS: Array<{ key: Tab; label: string; Icon: typeof MessageCircle }> = [
  { key: "chats", label: "消息", Icon: MessageCircle },
  { key: "discover", label: "动态 · 偶遇", Icon: Compass },
  { key: "notes", label: "表达本 · 画像", Icon: BookOpen },
];

export function TabBar({ tab, unread, requests, onChange }: TabBarProps) {
  return (
    <nav className="se-tabbar" aria-label="主导航">
      {ITEMS.map(({ key, label, Icon }) => {
        const active = tab === key;
        const badge = key === "chats" ? unread : key === "discover" ? requests : 0;
        return (
          <button
            key={key}
            type="button"
            onClick={() => onChange(key)}
            aria-current={active ? "page" : undefined}
            className={`se-tab ${active ? "active" : ""}`}
          >
            <span className="relative">
              <Icon size={22} strokeWidth={active ? 2.1 : 1.7} />
              {badge > 0 ? <span className="se-tab-badge">{badge > 99 ? "99+" : badge}</span> : null}
            </span>
            <span className="se-tab-label">{label}</span>
          </button>
        );
      })}
    </nav>
  );
}
