"use client";

/**
 * 发现（桌面左列）：信息流 / 偶遇 / 通知。
 */

import { Bell, ChevronRight, Compass, Sparkles } from "lucide-react";

import type { Route } from "../model";

export function DiscoverIndex({
  active,
  navigate,
}: {
  active: "feed" | "people" | "events";
  navigate: (r: Route) => void;
}) {
  const items = [
    {
      key: "feed" as const,
      label: "信息流",
      desc: "AI 人物们此刻在做什么",
      icon: <Sparkles size={17} strokeWidth={1.8} />,
      tint: "var(--w-green-ink)",
      route: { name: "discover" } as Route,
    },
    {
      key: "people" as const,
      label: "偶遇一个人",
      desc: "每次一位，先听听 TA 的声音",
      icon: <Compass size={17} strokeWidth={1.8} />,
      tint: "#3d6b8f",
      route: { name: "discoverPeople" } as Route,
    },
    {
      key: "events" as const,
      label: "通知",
      desc: "点赞、评论和好友申请",
      icon: <Bell size={17} strokeWidth={1.8} />,
      tint: "#b7791f",
      route: { name: "events" } as Route,
    },
  ];

  return (
    <div className="w4-disc-col">
      <div className="w4-list-head">
        <div className="w4-head-main">
          <div className="w4-head-title">发现</div>
        </div>
      </div>
      <div className="w4-scroll">
        <nav className="w4-disc-nav" aria-label="发现">
          {items.map((it) => {
            const on = active === it.key;
            return (
              <button
                key={it.key}
                className={`w4-disc-item ${on ? "on" : ""}`}
                onClick={() => navigate(it.route)}
                aria-current={on ? "page" : undefined}
              >
                <span
                  className="w4-disc-ico"
                  style={on ? undefined : { background: `${it.tint}14`, color: it.tint }}
                >
                  {it.icon}
                </span>
                <span className="w4-disc-text">
                  <span className="w4-disc-label">{it.label}</span>
                  <span className="w4-disc-desc">{it.desc}</span>
                </span>
                <ChevronRight size={15} className="w4-disc-chev" />
              </button>
            );
          })}
        </nav>
      </div>
    </div>
  );
}
