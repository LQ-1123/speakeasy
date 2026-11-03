"use client";

/** 动态 · 偶遇 Tab：偶遇好友（国家/语种筛选）+ 好友申请 + 朋友圈动态流（按住语音评论）。 */

import { useState } from "react";
import { Check, Compass, Loader2, MessageCircle, RefreshCw, Sparkles, Users, X } from "lucide-react";

import { Avatar } from "@/components/v2/Avatar";
import { HoldToTalk } from "@/components/v2/HoldToTalk";
import { useToast } from "@/components/v2/Toast";
import { EmptyState, ErrorBanner, Spinner } from "@/components/v2/ui";
import { flagOf, localStateOf, relativeLabel } from "@/lib/v2/format";
import type { Friend, Moment } from "@/lib/v2/types";
import type { RecordingResult } from "@/lib/v2/useRecorder";

const NATIONS = [
  { value: "", label: "🌍 国家不限" },
  { value: "Italy", label: "🇮🇹 意大利" },
  { value: "Germany", label: "🇩🇪 德国" },
  { value: "Japan", label: "🇯🇵 日本" },
  { value: "Korea", label: "🇰🇷 韩国" },
  { value: "France", label: "🇫🇷 法国" },
  { value: "Spain", label: "🇪🇸 西班牙" },
  { value: "Brazil", label: "🇧🇷 巴西" },
  { value: "Mexico", label: "🇲🇽 墨西哥" },
  { value: "UK", label: "🇬🇧 英国" },
  { value: "USA", label: "🇺🇸 美国" },
];

const LANGS = [
  { value: "", label: "语种不限" },
  { value: "en", label: "英语" },
  { value: "it", label: "意大利语" },
  { value: "de", label: "德语" },
  { value: "ja", label: "日语" },
  { value: "ko", label: "韩语" },
  { value: "fr", label: "法语" },
  { value: "es", label: "西班牙语" },
  { value: "pt", label: "葡萄牙语" },
];

const GENDERS = [
  { value: "", label: "性别不限" },
  { value: "female", label: "女生" },
  { value: "male", label: "男生" },
];

interface DiscoverTabProps {
  requests: Friend[];
  moments: Moment[];
  loading: boolean;
  momentsLoading: boolean;
  error: string | null;
  discovering: boolean;
  generating: boolean;
  acceptingId: string | null;
  onDiscover: (filters: { nationality: string; language: string; gender: string }) => void;
  onGenerate: () => void;
  onRetry: () => void;
  onAccept: (friend: Friend) => void;
  onDecline: (friend: Friend) => void;
  onCommentVoice: (moment: Moment, result: RecordingResult) => Promise<void>;
  onOpenChat: (friend: Friend) => void;
}

export function DiscoverTab({
  requests,
  moments,
  loading,
  momentsLoading,
  error,
  discovering,
  generating,
  acceptingId,
  onDiscover,
  onGenerate,
  onRetry,
  onAccept,
  onDecline,
  onCommentVoice,
  onOpenChat,
}: DiscoverTabProps) {
  const { showToast } = useToast();
  const [nationality, setNationality] = useState("");
  const [language, setLanguage] = useState("");
  const [gender, setGender] = useState("");
  const [commentingId, setCommentingId] = useState<string | null>(null);

  return (
    <div className="pb-3">
      {/* ------------------------------------------------ 偶遇卡片 */}
      <div className="se-hero">
        <div className="flex items-start justify-between gap-2">
          <div>
            <p className="flex items-center gap-1.5 text-[15px] font-semibold text-white">
              <Compass size={16} />
              偶遇一位新朋友
            </p>
            <p className="mt-1 text-[11.5px] leading-relaxed text-white/80">
              捏一个真实的外国朋友，他们会先发来一条破冰语音。
            </p>
          </div>
          <span className="text-[26px]" aria-hidden>
            🌍
          </span>
        </div>

        <div className="mt-3 flex flex-col gap-2">
          <div className="flex gap-2">
            <select
              aria-label="国家筛选"
              value={nationality}
              onChange={(event) => setNationality(event.target.value)}
              className="se-filter"
            >
              {NATIONS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
            <select
              aria-label="语种筛选"
              value={language}
              onChange={(event) => setLanguage(event.target.value)}
              className="se-filter"
            >
              {LANGS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div className="flex gap-2">
            <select
              aria-label="性别筛选"
              value={gender}
              onChange={(event) => setGender(event.target.value)}
              className="se-filter"
            >
              {GENDERS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={discovering}
              onClick={() => onDiscover({ nationality, language, gender })}
              className="se-discover-btn"
            >
              {discovering ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}
              {discovering ? "正在偶遇…" : "偶遇"}
            </button>
          </div>
        </div>
      </div>

      {/* ------------------------------------------------ 好友申请 */}
      {requests.length > 0 ? (
        <div className="se-card mx-3 mt-3 overflow-hidden">
          <div className="se-card-head">
            <span className="flex items-center gap-1.5">
              <Users size={14} className="text-[color:var(--se-green)]" />
              新朋友想认识你
            </span>
            <span className="se-badge">{requests.length}</span>
          </div>
          {requests.map((friend) => (
            <div key={friend.id} className="flex items-center gap-2.5 border-t border-black/[0.05] px-3 py-2.5">
              <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={42} />
              <button type="button" onClick={() => onOpenChat(friend)} className="min-w-0 flex-1 text-left">
                <p className="truncate text-[14px] font-medium">{friend.name}</p>
                <p className="truncate text-[11.5px] text-[color:var(--se-dim)]">
                  {flagOf(friend)} {friend.city || friend.nationality} · 想加你为好友
                </p>
              </button>
              <button type="button" onClick={() => onDecline(friend)} className="se-mini-btn ghost" aria-label="忽略">
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

      {/* ------------------------------------------------ 朋友圈动态 */}
      <div className="mt-4 flex items-center justify-between px-4">
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-[color:var(--se-text)]">
          <MessageCircle size={14} />
          朋友圈动态
        </span>
        <button
          type="button"
          onClick={onGenerate}
          disabled={generating}
          className="flex items-center gap-1 text-[11.5px] text-[color:var(--se-green)] disabled:opacity-50"
        >
          {generating ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          {generating ? "生成中…" : "换一批"}
        </button>
      </div>

      {error ? <ErrorBanner message={`动态加载失败：${error}`} onRetry={onRetry} /> : null}

      {momentsLoading && moments.length === 0 ? (
        <Spinner label="正在看看大家今天在干嘛…" />
      ) : null}

      {!momentsLoading && moments.length === 0 && !error ? (
        <EmptyState
          title="今天还没有动态"
          hint="让好友们发点生活碎片吧，你可以按住语音给他们评论。"
          icon={<span className="text-[26px]">🌤️</span>}
          action={
            <button type="button" onClick={onGenerate} disabled={generating} className="se-cta">
              {generating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
              生成今日动态
            </button>
          }
        />
      ) : null}

      <div className="mt-2 flex flex-col gap-2.5 px-3">
        {moments.map((moment) => {
          const friend = moment.friend;
          const local = friend ? localStateOf(friend) : null;
          return (
            <article key={moment.id} className="se-moment">
              <div className="flex items-center gap-2.5">
                {friend ? (
                  <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={40} />
                ) : (
                  <Avatar name="?" seed={moment.friend_id} size={40} />
                )}
                <button type="button" onClick={() => friend && onOpenChat(friend)} className="min-w-0 flex-1 text-left">
                  <p className="truncate text-[14px] font-medium">{friend?.name ?? "好友"}</p>
                  <p className="truncate text-[11px] text-[color:var(--se-dim)]">
                    {friend ? `${flagOf(friend)} ${friend.city || friend.nationality}` : "远方"}
                    {local ? ` · ${local.time}` : moment.local_time ? ` · ${moment.local_time}` : ""}
                  </p>
                </button>
                <span className="text-[10.5px] text-[color:var(--se-faint)]">
                  {relativeLabel(moment.created_at)}
                </span>
              </div>

              <p className="mt-2 whitespace-pre-wrap text-[13.5px] leading-relaxed text-[color:var(--se-text)]">
                {moment.content}
              </p>

              {moment.mood_tag ? <span className="se-mood">#{moment.mood_tag}</span> : null}

              <div className="mt-2.5 flex items-center justify-between gap-2 border-t border-black/[0.05] pt-2">
                <span className="text-[10.5px] text-[color:var(--se-faint)]">
                  {moment.replied ? "已评论 · 话题已带进私聊" : "按住语音评论，把话题带进私聊"}
                </span>
                <div className="flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => friend && onOpenChat(friend)}
                    className="se-moment-open"
                    aria-label="打开私聊"
                  >
                    私聊
                  </button>
                  <HoldToTalk
                    compact
                    label="按住 评论"
                    disabled={commentingId === moment.id}
                    onComplete={async (result) => {
                      setCommentingId(moment.id);
                      try {
                        await onCommentVoice(moment, result);
                      } finally {
                        setCommentingId(null);
                      }
                    }}
                    onUnavailable={(message) => showToast(message, "info")}
                    onError={(message) => showToast(message, "error")}
                  />
                </div>
              </div>
            </article>
          );
        })}
      </div>

      {loading && moments.length === 0 ? <Spinner /> : null}
    </div>
  );
}
