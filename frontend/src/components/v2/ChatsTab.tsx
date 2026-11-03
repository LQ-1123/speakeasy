"use client";

/** 消息 Tab：好友列表 + 新好友申请 + 当地时间/未读。 */

import { Check, ChevronRight, MessageCircle, UserPlus, X } from "lucide-react";

import { Avatar } from "@/components/v2/Avatar";
import { EmptyState, ErrorBanner, Spinner } from "@/components/v2/ui";
import { flagOf, localStateOf, relativeLabel } from "@/lib/v2/format";
import type { Friend } from "@/lib/v2/types";

interface ChatsTabProps {
  friends: Friend[];
  requests: Friend[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onOpenChat: (friend: Friend) => void;
  onAccept: (friend: Friend) => void;
  onDecline: (friend: Friend) => void;
  onExplore: () => void;
  acceptingId: string | null;
}

function previewOf(friend: Friend): string {
  const last = friend.last_message;
  if (!last) return "打个招呼，开始第一次对话";
  const text = last.transcript?.trim();
  const prefix = last.sender === "user" ? "我： " : "";
  if (!text) return last.msg_type === "voice" ? `${prefix}[语音]` : `${prefix}（新消息）`;
  return `${prefix}${text}`;
}

export function ChatsTab({
  friends,
  requests,
  loading,
  error,
  onRetry,
  onOpenChat,
  onAccept,
  onDecline,
  onExplore,
  acceptingId,
}: ChatsTabProps) {
  return (
    <div className="pb-2">
      {error ? <ErrorBanner message={`好友列表加载失败：${error}`} onRetry={onRetry} /> : null}
      {loading && friends.length === 0 ? <Spinner label="加载好友…" /> : null}

      {requests.length > 0 ? (
        <div className="se-card mx-3 mt-3 overflow-hidden">
          <div className="se-card-head">
            <span className="flex items-center gap-1.5">
              <UserPlus size={14} className="text-[color:var(--se-green)]" />
              新的朋友
            </span>
            <span className="se-badge">{requests.length}</span>
          </div>
          {requests.map((friend) => (
            <div key={friend.id} className="flex items-center gap-3 border-t border-black/[0.05] px-3 py-2.5">
              <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={42} />
              <div className="min-w-0 flex-1">
                <p className="truncate text-[14px] font-medium">{friend.name}</p>
                <p className="truncate text-[11.5px] text-[color:var(--se-dim)]">
                  {flagOf(friend)} {friend.city || friend.nationality} · {friend.occupation || "想认识你"}
                </p>
              </div>
              <button
                type="button"
                onClick={() => onDecline(friend)}
                className="se-mini-btn ghost"
                aria-label={`忽略 ${friend.name}`}
              >
                <X size={14} />
              </button>
              <button
                type="button"
                onClick={() => onAccept(friend)}
                disabled={acceptingId === friend.id}
                className="se-mini-btn primary"
              >
                <Check size={14} />
                {acceptingId === friend.id ? "…" : "接受"}
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {friends.length === 0 && !loading ? (
        <EmptyState
          title="还没有好友"
          hint="去「动态 · 偶遇」认识一位外国朋友，他们会先给你发一条破冰语音。"
          icon={<MessageCircle size={26} strokeWidth={1.6} />}
          action={
            <button type="button" onClick={onExplore} className="se-cta">
              <UserPlus size={14} />
              去偶遇好友
            </button>
          }
        />
      ) : null}

      {friends.length > 0 ? (
        <>
          <div className="se-list">
            {friends.map((friend) => {
              const local = localStateOf(friend);
              const unread = friend.unread > 0;
              const lastAt = friend.last_message?.created_at || friend.last_interaction_at;
              return (
                <button
                  key={friend.id}
                  type="button"
                  onClick={() => onOpenChat(friend)}
                  className="se-friend-row"
                >
                  <div className="relative">
                    <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={48} />
                    {unread ? (
                      <span className="se-unread-badge">{friend.unread > 99 ? "99+" : friend.unread}</span>
                    ) : null}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-1.5">
                      <span className="truncate text-[15px] font-medium text-[color:var(--se-text)]">{friend.name}</span>
                      <span className="shrink-0 text-[11px] text-[color:var(--se-faint)]">
                        {friend.city || friend.nationality}
                      </span>
                    </div>
                    <p className="mt-0.5 truncate text-[12.5px] leading-relaxed text-[color:var(--se-dim)]">
                      {previewOf(friend)}
                    </p>
                  </div>
                  <div className="flex w-[62px] shrink-0 flex-col items-end gap-1 self-stretch pt-0.5">
                    <span className="text-[10.5px] tabular-nums text-[color:var(--se-faint)]">
                      {relativeLabel(lastAt)}
                    </span>
                    <span className="flex items-center gap-1 rounded-full bg-black/[0.035] px-1.5 py-0.5 text-[10px] text-[color:var(--se-dim)]">
                      <span aria-hidden>{local.icon}</span>
                      {local.time}
                    </span>
                    <ChevronRight size={13} className="mt-auto text-[color:var(--se-faint)] opacity-0 group-hover:opacity-100" />
                  </div>
                </button>
              );
            })}
          </div>
          <p className="px-4 pb-2 pt-3 text-center text-[10.5px] text-[color:var(--se-faint)]">
            好友列表每 10 秒自动刷新 · 语音条异步回复
          </p>
        </>
      ) : null}
    </div>
  );
}
