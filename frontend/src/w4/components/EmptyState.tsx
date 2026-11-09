"use client";

import { Compass, MessageCircle } from "lucide-react";

import type { Route } from "../model";
import { useApp } from "../store";

/**
 * Desktop empty state. This is the first screen a user sees on a wide display,
 * so it carries a quiet greeting rather than a blank gray canvas.
 */
export function EmptyState({ navigate }: { navigate: (route: Route) => void }) {
  const profile = useApp((state) => state.profile);
  const hour = new Date().getHours();
  const greeting = hour < 6 ? "夜深了" : hour < 11 ? "早上好" : hour < 14 ? "中午好" : hour < 18 ? "下午好" : "晚上好";
  return (
    <div className="w4-main w4-empty-main">
      <div className="w4-empty-hero">
        <span className="w4-empty-orbit" aria-hidden>
          <i />
          <i />
          <MessageCircle size={28} strokeWidth={1.5} />
        </span>
        <p className="w4-empty-kicker">{greeting}{profile.name ? `，${profile.name}` : ""}</p>
        <h2>挑一段对话，慢慢说。</h2>
        <p>语音条不用赶时间。想认识新的人，也可以先去偶遇。</p>
        <div className="w4-empty-actions">
          <button className="w4-btn is-primary" onClick={() => navigate({ name: "discoverPeople" })}>
            <Compass size={16} /> 偶遇一个人
          </button>
          <button className="w4-btn is-tonal" onClick={() => navigate({ name: "discover" })}>
            看看动态
          </button>
        </div>
      </div>
    </div>
  );
}
