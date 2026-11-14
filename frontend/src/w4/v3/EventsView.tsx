"use client";
import { CoalescedRefresh } from "../clientRefresh";
import type { ClientEvent } from "./clientEvents";

import { useShallow } from "zustand/react/shallow";

/**
 * V3 · 通知中心（社交事件）。
 *
 * 事件全部由后端生成：点赞 / 评论 / 好友申请，允许「什么都没有」。
 * 只有接受好友申请才会真正建立好友关系（由后端保证）。
 *
 * 视觉：紧凑、可读的通知列表——头像 + 动作 + 来源 + 时间。
 */

import { useCallback, useEffect, useState } from "react";
import { Bell, Check, ChevronLeft, Heart, MessageSquare, RefreshCw, UserPlus, X } from "lucide-react";

import type { Route } from "../model";
import { useApp } from "../store";
import { resolveAvatar } from "../avatar";
import { haptic } from "../../theme/haptics";
import { Avatar } from "../ui";
import { nativeBridge } from "./clientApi";
import {
  acceptEvent,
  declineEvent,
  fetchEvents,
  markAllEventsRead,
  markEventRead,
  relTimeOf,
  translateText,
  type SocialEvent,
} from "./api";

const TYPE_TEXT: Record<string, string> = {
  like: "赞了你的动态",
  comment: "评论了你的动态",
  friend_request: "想加你为好友",
};

export function EventsView({ navigate }: { navigate: (r: Route) => void }) {
  const nativeLanguage = useApp((state) => state.profile.nativeLanguage || "zh");
  const { reloadAll, showToast } = useApp(
    useShallow((state) => ({ reloadAll: state.reloadAll, showToast: state.showToast })),
  );
  const [events, setEvents] = useState<SocialEvent[]>([]);
  const [unread, setUnread] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  /* 人物用母语评论/申请，中文用户可一键翻译 */
  const [zh, setZh] = useState<Record<string, string>>({});
  const [zhOpen, setZhOpen] = useState<Record<string, boolean>>({});
  const [zhBusy, setZhBusy] = useState<string>("");

  const translateEvent = async (ev: SocialEvent) => {
    const text = ev.payload.comment || ev.payload.message || "";
    if (!text) return;
    if (zhOpen[ev.id]) {
      setZhOpen((m) => ({ ...m, [ev.id]: false }));
      return;
    }
    if (zh[ev.id]) {
      setZhOpen((m) => ({ ...m, [ev.id]: true }));
      return;
    }
    setZhBusy(ev.id);
    const out = await translateText(text, nativeLanguage).catch(() => "");
    setZhBusy("");
    if (out) {
      setZh((m) => ({ ...m, [ev.id]: out }));
      setZhOpen((m) => ({ ...m, [ev.id]: true }));
    } else {
      showToast("翻译失败，请稍后重试");
    }
  };

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetchEvents(false);
      setEvents(res.items);
      setUnread(res.unread);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /* 事件是延迟产生的：每 15 秒静默刷新一次，用户停在通知页也能看到新互动 */
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const res = await fetchEvents(false);
        if (!alive) return;
        setEvents(res.items);
        setUnread(res.unread);
      } catch {
        /* 轮询失败静默 */
      }
    };
    const refresh = new CoalescedRefresh(tick);
    const onEvent = (raw: Event) => {
      const events = (raw as CustomEvent<ClientEvent[]>).detail;
      if (events?.some((event) => event.type === "social" || event.type === "friend_request")) void refresh.run();
    };
    window.addEventListener("speakeasy:client-event", onEvent);
    return () => {
      alive = false;
      refresh.stop();
      window.removeEventListener("speakeasy:client-event", onEvent);
    };
  }, []);

  const readAll = async () => {
    try {
      await markAllEventsRead();
      for (const event of events) nativeBridge()?.cancelNotification?.(`social:${event.id}`);
      setEvents((prev) => prev.map((e) => ({ ...e, is_read: true })));
      setUnread(0);
      haptic("save");
    } catch {
      showToast("操作失败");
    }
  };

  const respond = async (ev: SocialEvent, accept: boolean) => {
    setBusyId(ev.id);
    try {
      if (accept) await acceptEvent(ev.id);
      else await declineEvent(ev.id);
      nativeBridge()?.cancelNotification?.(`social:${ev.id}`);
      haptic("save");
      await reloadAll();
      await load();
      showToast(accept ? `已和 ${ev.persona.nickname} 成为好友` : "已拒绝", "success");
    } catch (exc) {
      showToast(exc instanceof Error ? exc.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  };

  const open = async (ev: SocialEvent) => {
    if (!ev.is_read) {
      void markEventRead(ev.id)
        .then(() => nativeBridge()?.cancelNotification?.(`social:${ev.id}`))
        .catch(() => undefined);
      setEvents((prev) => prev.map((x) => (x.id === ev.id ? { ...x, is_read: true } : x)));
      setUnread((n) => Math.max(0, n - 1));
    }
    navigate({ name: "persona", personaId: ev.persona.id });
  };

  return (
    <div className="w4-main disc-main">
      <div className="w4-head disc-head">
        <button className="w4-icon-btn" onClick={() => navigate({ name: "discover" })} aria-label="返回">
          <ChevronLeft size={20} strokeWidth={2} />
        </button>
        <div className="w4-head-main">
          <div className="w4-head-title disc-head-title">通知</div>
          <div className="w4-head-sub">{unread > 0 ? `${unread} 条未读` : "AI 人物的互动"}</div>
        </div>
        <div className="w4-head-actions disc-head-actions">
          <button className="w4-btn disc-readall" onClick={() => void readAll()}>
            全部已读
          </button>
          <button className="w4-icon-btn" onClick={() => void load()} aria-label="刷新" title="刷新">
            <RefreshCw size={18} strokeWidth={1.8} />
          </button>
        </div>
      </div>

      <div className="w4-scroll">
        <div className="w4-feed disc-events-page">
          {loading ? (
            <div className="disc-state">
              <div className="disc-spinner" aria-hidden />
              <div className="disc-state-text" style={{ marginTop: 16 }}>
                正在收取通知…
              </div>
            </div>
          ) : null}

          {error ? (
            <div className="disc-state">
              <div className="disc-state-title">通知加载失败</div>
              <div className="disc-state-text">{error}</div>
              <button className="w4-btn is-primary" onClick={() => void load()}>
                重试
              </button>
            </div>
          ) : null}

          {!loading && !error && events.length === 0 ? (
            <div className="disc-state">
              <div className="disc-state-icon">
                <Bell size={28} strokeWidth={1.6} />
              </div>
              <div className="disc-state-title">暂时没有互动</div>
              <div className="disc-state-text">
                发一条公开动态试试 —— AI 人物可能会点赞、评论或申请加你，也可能什么都不发生。
              </div>
              <button className="w4-btn is-primary" onClick={() => navigate({ name: "discover" })}>
                去发现
              </button>
            </div>
          ) : null}

          {!loading && events.length > 0 ? (
            <div className="disc-events">
              {events.map((ev) => {
                const quote = ev.payload.comment || ev.payload.message || "";
                return (
                  <div key={ev.id} className={`disc-event ${ev.is_read ? "" : "is-unread"}`}>
                    <button
                      type="button"
                      className="disc-event-avatar"
                      onClick={() => void open(ev)}
                      aria-label={ev.persona.nickname}
                    >
                      <Avatar
                        src={resolveAvatar(ev.persona)}
                        name={ev.persona.nickname}
                        size={48}
                        round
                      />
                      <span
                        className={`disc-event-icon ${
                          ev.type === "comment"
                            ? "is-comment"
                            : ev.type === "friend_request"
                              ? "is-request"
                              : "is-like"
                        }`}
                        aria-hidden
                      >
                        {ev.type === "like" ? (
                          <Heart size={11} strokeWidth={0} fill="currentColor" />
                        ) : ev.type === "comment" ? (
                          <MessageSquare size={11} strokeWidth={2.1} />
                        ) : (
                          <UserPlus size={11} strokeWidth={2.1} />
                        )}
                      </span>
                    </button>

                    <div className="disc-event-main">
                      <div className="disc-event-line">
                        <button
                          type="button"
                          className="disc-event-name disc-inline-btn"
                          onClick={() => void open(ev)}
                        >
                          {ev.persona.nickname}
                        </button>
                        <span className="disc-ai-tag sm">AI</span>
                        <span>{TYPE_TEXT[ev.type] ?? ev.type}</span>
                        <span className="disc-event-time">
                          {relTimeOf(ev.scheduled_at || ev.created_at)}
                        </span>
                      </div>

                      {quote ? <p className="disc-event-quote">“{quote}”</p> : null}

                      {quote ? (
                        <div className="disc-event-trans">
                          <button type="button" className="disc-link" onClick={() => void translateEvent(ev)}>
                            {zhBusy === ev.id ? "翻译中…" : zhOpen[ev.id] ? "收起翻译" : "翻译"}
                          </button>
                          {zhOpen[ev.id] && zh[ev.id] ? <div className="disc-event-zh">{zh[ev.id]}</div> : null}
                        </div>
                      ) : null}

                      {ev.payload.post_excerpt ? (
                        <div className="disc-event-source">
                          来源：你的动态 <b>「{ev.payload.post_excerpt}」</b>
                        </div>
                      ) : null}

                      {ev.type === "friend_request" && ev.status === "pending" ? (
                        <div className="disc-event-actions">
                          <button
                            className="w4-btn is-primary disc-event-accept"
                            disabled={busyId === ev.id}
                            onClick={() => void respond(ev, true)}
                          >
                            <Check size={16} /> 接受
                          </button>
                          <button
                            className="w4-btn disc-event-decline"
                            disabled={busyId === ev.id}
                            onClick={() => void respond(ev, false)}
                          >
                            <X size={15} /> 拒绝
                          </button>
                        </div>
                      ) : null}

                      {ev.type === "friend_request" && ev.status === "accepted" ? (
                        <div className="disc-event-status">已接受</div>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
