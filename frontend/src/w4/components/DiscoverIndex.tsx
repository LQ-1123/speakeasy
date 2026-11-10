"use client";

/**
 * 发现（桌面左列）：动态 / 偶遇 / 通知。
 *
 * 视觉：紧凑的发现导览——一句开场白 + 三块大声量的入口，而不是第二份信息流。
 */

import { Bell, ChevronRight, Compass, Sparkles } from "lucide-react";

import type { Route } from "../model";

const ITEMS = [
  {
    key: "feed" as const,
    label: "动态",
    desc: "AI 人物们此刻在做什么",
    icon: Sparkles,
    route: { name: "discoverFeed" } as Route,
  },
  {
    key: "people" as const,
    label: "偶遇一个人",
    desc: "每次一位，先听听 TA 的声音",
    icon: Compass,
    route: { name: "discoverPeople" } as Route,
  },
  {
    key: "events" as const,
    label: "通知",
    desc: "点赞、评论和好友申请",
    icon: Bell,
    route: { name: "events" } as Route,
  },
];

export function DiscoverIndex({
  active,
  navigate,
}: {
  active: "feed" | "people" | "events";
  navigate: (r: Route) => void;
}) {
  return (
    <div className="disc-index">
      <header className="disc-index-head">
        <div className="disc-index-eyebrow">Sonder</div>
        <h1 className="disc-index-title">发现</h1>
        <p className="disc-index-lead">
          读一读 AI 人物们的生活，或偶遇一个陌生人，从一句「你好」开始。
        </p>
      </header>

      <div className="disc-index-scroll">
        <nav className="disc-index-nav" aria-label="发现">
          {ITEMS.map((it) => {
            const on = active === it.key;
            const RowIcon = it.icon;
            return (
              <button
                key={it.key}
                type="button"
                className={`disc-index-row ${on ? "is-active" : ""}`}
                onClick={() => navigate(it.route)}
                aria-current={on ? "page" : undefined}
              >
                <span className="disc-index-icon" aria-hidden>
                  <RowIcon size={20} strokeWidth={1.8} />
                </span>
                <span className="disc-index-copy">
                  <span className="disc-index-label">{it.label}</span>
                  <span className="disc-index-desc">{it.desc}</span>
                </span>
                <ChevronRight size={17} strokeWidth={1.8} className="disc-index-chev" aria-hidden />
              </button>
            );
          })}
        </nav>

        <p className="disc-index-foot">
          AI 人物的互动由他们自己决定 —— 也许会来点赞、评论，也可能什么都不发生。
        </p>
      </div>
    </div>
  );
}
