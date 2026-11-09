"use client";

import { useEffect, useRef, useState } from "react";
import { Heart, MessageCircle, MessageSquare, UserPlus, X } from "lucide-react";

import { haptic } from "../../theme/haptics";
import { Avatar } from "../ui";
import { listTime } from "../format";
import { useApp } from "../store";
import { callSession } from "./callSession";
import { useNavigation } from "../navigationStore";
import { usePreferences } from "./preferences";
import { notificationFor } from "./notificationPolicy";
import { playMessageSound, playRequestSound } from "../notify";
import type { ClientEvent } from "./clientEvents";
import "../styles/notifications.css";

/**
 * 全局通知：一条紧凑的玻璃质感横幅。
 * 显示「谁 + 做了什么 + 来源 + 时间」，点击直达对应页面；
 * 位置在安全区之下；通话最小化时，自动下移到通话条下面，不遮挡通话界面。
 */

type BannerKind = "message" | "like" | "comment" | "friend_request" | "social";

interface Banner {
  id: string;
  kind: BannerKind;
  name: string;
  action: string;
  source: string;
  detail: string;
  time: string;
  route: string;
  friendId?: string;
}

const ACTION_BY_KIND: Record<BannerKind, string> = {
  message: "发来一条新消息",
  like: "赞了你的动态",
  comment: "评论了你的动态",
  friend_request: "想成为你的语伴",
  social: "有新的互动",
};

const SOURCE_BY_KIND: Record<BannerKind, string> = {
  message: "消息",
  like: "动态",
  comment: "动态",
  friend_request: "好友申请",
  social: "动态",
};

function bannerFromEvent(event: ClientEvent, route: string): Banner {
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  const isMessage = event.type === "message";
  const socialType = String(payload.event_type ?? "");
  const kind: BannerKind = isMessage
    ? "message"
    : socialType === "like" || socialType === "comment" || socialType === "friend_request"
      ? socialType
      : event.type === "friend_request"
        ? "friend_request"
        : "social";

  const rawName = payload.name ?? payload.persona_name ?? payload.friend_name;
  const rawDetail = isMessage
    ? payload.text
    : kind === "comment"
      ? payload.comment
      : kind === "friend_request"
        ? payload.message
        : payload.post_excerpt;
  const createdAt = (event as ClientEvent & { created_at?: string }).created_at;

  return {
    id: `${event.id}:${event.sequence}`,
    kind,
    name: typeof rawName === "string" ? rawName : "",
    action: ACTION_BY_KIND[kind],
    source: SOURCE_BY_KIND[kind],
    detail: typeof rawDetail === "string" ? rawDetail.trim().slice(0, 90) : "",
    time: createdAt ? listTime(createdAt) : "刚刚",
    route,
    friendId: event.friend_id,
  };
}

function KindIcon({ kind }: { kind: BannerKind }) {
  if (kind === "like") return <Heart size={11} strokeWidth={2.2} />;
  if (kind === "comment") return <MessageSquare size={11} strokeWidth={2.2} />;
  if (kind === "friend_request") return <UserPlus size={11} strokeWidth={2.2} />;
  return <MessageCircle size={11} strokeWidth={2.2} />;
}

export function GlobalNotification() {
  const [banner, setBanner] = useState<Banner | null>(null);
  const [dockOffset, setDockOffset] = useState(0);
  const timer = useRef<number | undefined>(undefined);
  const friends = useApp((state) => state.friends);

  useEffect(() => {
    const receive = (raw: Event) => {
      const events = (raw as CustomEvent<ClientEvent[]>).detail;
      if (!Array.isArray(events)) return;
      const decision = notificationFor(
        events,
        usePreferences.getState().value,
        useNavigation.getState().route,
        Boolean(callSession.getSnapshot()),
      );
      if (!decision) return;
      setBanner(bannerFromEvent(decision.event, decision.route));
      if (decision.sound) {
        (decision.event.type === "message" ? playMessageSound : playRequestSound)();
      }
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setBanner(null), 6000);
    };
    const dismiss = () => setBanner(null);
    window.addEventListener("speakeasy:client-event", receive);
    window.addEventListener("hashchange", dismiss);
    return () => {
      window.clearTimeout(timer.current);
      window.removeEventListener("speakeasy:client-event", receive);
      window.removeEventListener("hashchange", dismiss);
    };
  }, []);

  /* 通话最小化时，通话条在文档流顶部；量一下它的实际底边，把横幅放到下面。 */
  useEffect(() => {
    if (!banner) return;
    let frame = 0;
    const measure = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const dock = document.querySelector<HTMLElement>(".se-call-dock");
        setDockOffset(dock ? Math.round(dock.getBoundingClientRect().bottom + 8) : 0);
      });
    };
    measure();
    const unsubscribe = callSession.subscribe(measure);
    /* 通话条是 .w4-app 的直接子节点：只观察这一层的增删，开销很小。 */
    const root = document.querySelector<HTMLElement>(".w4-app") ?? document.body;
    const observer = new MutationObserver(measure);
    observer.observe(root, { childList: true });
    return () => {
      window.cancelAnimationFrame(frame);
      unsubscribe();
      observer.disconnect();
    };
  }, [banner]);

  if (!banner) return null;

  const friend = banner.friendId
    ? friends.find((item) => item.id === banner.friendId)
    : undefined;
  const name = banner.name || friend?.name || "SpeakEasy";

  const open = () => {
    haptic("selection");
    const route = banner.route;
    setBanner(null);
    window.location.hash = route;
  };

  return (
    <div
      key={banner.id}
      className="se-global-notification"
      role="status"
      aria-live="polite"
      style={dockOffset ? { top: dockOffset } : undefined}
    >
      <button
        className="se-notif-main"
        onClick={open}
        title={banner.detail || undefined}
        aria-label={`${name} ${banner.action}，${banner.source}，${banner.time}`}
      >
        <span className="se-notif-avatar">
          <Avatar src={friend?.avatar} name={name} size={38} round />
          <span className="se-notif-kind" aria-hidden>
            <KindIcon kind={banner.kind} />
          </span>
        </span>
        <span className="se-notif-copy">
          <span className="se-notif-line1">
            <strong>{name}</strong>
            <span className="se-notif-action">{banner.action}</span>
          </span>
          <span className="se-notif-line2">
            <span className="se-notif-source">{banner.source}</span>
            {banner.detail ? <span className="se-notif-detail">{banner.detail}</span> : null}
          </span>
        </span>
        <span className="se-notif-time">{banner.time}</span>
      </button>
      <button
        className="se-notif-close"
        aria-label="关闭提醒"
        onClick={() => {
          haptic("selection");
          setBanner(null);
        }}
      >
        <X size={14} strokeWidth={2} />
      </button>
    </div>
  );
}
