"use client";

/** 单聊页：语音条消息流 + 润色胶囊 + 按住说话 / 文字兜底。 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  Languages,
  Loader2,
  MoreHorizontal,
  Phone,
  RefreshCw,
  Trash2,
  Users,
  X,
} from "lucide-react";

import { Avatar } from "@/components/v2/Avatar";
import { Composer } from "@/components/v2/Composer";
import { SuggestionPill } from "@/components/v2/SuggestionPill";
import { useToast } from "@/components/v2/Toast";
import { VoiceBubble } from "@/components/v2/VoiceBubble";
import { EmptyState, ErrorBanner, Spinner } from "@/components/v2/ui";
import { api, apiErrorText, translateBatch, translateText } from "@/lib/v2/api";
import {
  chatDayLabel,
  closenessLabel,
  errorText,
  localStateOf,
  relativeLabel,
  replyLanguageMeta,
  replyLanguageOf,
  shouldShowDayDivider,
} from "@/lib/v2/format";
import type { Message, TranslationResult } from "@/lib/v2/types";
import type { Friend } from "@/lib/v2/types";
import type { RecordingResult } from "@/lib/v2/useRecorder";

interface ChatViewProps {
  friend: Friend;
  onBack: () => void;
  onFriendRefresh?: (friend: Friend) => void;
  /** 好友被删除后通知父级（关闭单聊并刷新列表）。 */
  onDeleted?: (friend: Friend) => void;
}

const SUGGESTION_POLL_MS = 2500;
const SUGGESTION_TIMEOUT_MS = 20000;
/** 「翻译最近消息」最多处理多少条 */
const TRANSLATE_RECENT_LIMIT = 10;

interface TranslationEntry {
  result: TranslationResult;
  open: boolean;
}

function timeOf(message: Message): number {
  const t = new Date(message.created_at).getTime();
  return Number.isNaN(t) ? 0 : t;
}

function mergeMessages(prev: Message[], incoming: Message[]): Message[] {
  if (incoming.length === 0) return prev;
  const map = new Map<string, Message>();
  for (const message of prev) map.set(message.id, message);
  for (const message of incoming) {
    const existing = map.get(message.id);
    map.set(message.id, existing ? { ...existing, ...message } : message);
  }
  return Array.from(map.values()).sort((a, b) => timeOf(a) - timeOf(b));
}

function TextBubble({ message, friend, mine }: { message: Message; friend: Friend; mine: boolean }) {
  return (
    <div className={`se-bubble-row ${mine ? "me" : ""}`}>
      {!mine ? <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={38} /> : null}
      <div className={`se-text-bubble ${mine ? "me" : "other"}`}>
        {message.msg_type === "call_summary" ? <span className="se-text-bubble-tag">通话摘要</span> : null}
        <p>{message.transcript || "（无内容）"}</p>
      </div>
      {mine ? <Avatar name="我" seed="me-speakeasy" size={38} flag={false} /> : null}
    </div>
  );
}

export function ChatView({ friend, onBack, onFriendRefresh, onDeleted }: ChatViewProps) {
  const { showToast } = useToast();
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [suggestionPending, setSuggestionPending] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [now, setNow] = useState(() => new Date());
  /** 每条消息的译文缓存（按 message.id），open 控制展开/收起 */
  const [translations, setTranslations] = useState<Record<string, TranslationEntry>>({});
  const [translatingIds, setTranslatingIds] = useState<Record<string, boolean>>({});
  const [batchTranslating, setBatchTranslating] = useState(false);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollInFlightRef = useRef(false);
  const tempUrlsRef = useRef<string[]>([]);
  const mountedRef = useRef(true);
  const friendRef = useRef(friend);
  friendRef.current = friend;

  const local = useMemo(() => localStateOf(friend, now), [friend, now]);
  const langMeta = useMemo(() => replyLanguageMeta(friend), [friend]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const scrollToBottom = useCallback((smooth = false) => {
    const node = scrollRef.current;
    if (!node) return;
    node.scrollTo({ top: node.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  /** 轮询直到出现润色结果或超时：先立刻拉一次，再每 2.5s 一次。 */
  const startSuggestionPolling = useCallback(() => {
    stopPolling();
    const deadline = Date.now() + SUGGESTION_TIMEOUT_MS;
    const tick = () => {
      if (Date.now() > deadline) {
        stopPolling();
        setSuggestionPending(false);
        return;
      }
      if (pollInFlightRef.current) return;
      pollInFlightRef.current = true;
      void (async () => {
        try {
          const list = await api.chatMessages(friendRef.current.id, 30);
          setMessages((prev) => mergeMessages(prev, list));
          // 聊天窗打开期间拉到的消息都已展示 → 立即已读，回列表不应再有红点
          void api.markRead(friendRef.current.id).catch(() => undefined);
          if (list.some((message) => message.sender === "user" && message.suggestion)) {
            stopPolling();
            setSuggestionPending(false);
          }
        } catch {
          /* 网络抖动时继续轮询 */
        } finally {
          pollInFlightRef.current = false;
        }
      })();
    };
    tick();
    pollRef.current = setInterval(tick, SUGGESTION_POLL_MS);
  }, [stopPolling]);

  const loadMessages = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const list = await api.chatMessages(friendRef.current.id, 30);
      setMessages(list);
      void api.markRead(friendRef.current.id).catch(() => undefined);
    } catch (err) {
      setLoadError(errorText(err));
    } finally {
      setLoading(false);
      requestAnimationFrame(() => scrollToBottom());
    }
  }, [scrollToBottom]);

  useEffect(() => {
    mountedRef.current = true;
    setTranslations({});
    setTranslatingIds({});
    void loadMessages();
    return () => {
      mountedRef.current = false;
      stopPolling();
      tempUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
      tempUrlsRef.current = [];
      void api.idle(friend.id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [friend.id]);

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 180;
    if (nearBottom) scrollToBottom();
  }, [messages, scrollToBottom]);

  /** 清理本地 blob url（消息替换后） */
  const revokeTemp = (url?: string) => {
    if (!url) return;
    URL.revokeObjectURL(url);
    tempUrlsRef.current = tempUrlsRef.current.filter((item) => item !== url);
  };

  const send = async (temp: Message, request: () => Promise<Awaited<ReturnType<typeof api.sendText>>>) => {
    setSending(true);
    setMessages((prev) => mergeMessages(prev, [temp]));
    requestAnimationFrame(() => scrollToBottom(true));
    try {
      const response = await request();
      setMessages((prev) => {
        const withoutTemp = prev.filter((message) => message.id !== temp.id);
        const incoming: Message[] = [];
        if (response.user_message) incoming.push(response.user_message);
        incoming.push(...response.friend_messages);
        return mergeMessages(withoutTemp, incoming);
      });
      revokeTemp(temp.local_audio_url);
      // 好友回复已直接展示在聊天窗里 → 标记已读，否则退回列表会亮红点。
      // 若用户在等待期间已经退出聊天，则不标记（回复未看到，应保留未读）。
      if (mountedRef.current) {
        void api.markRead(friend.id).catch(() => undefined);
      }
      if (response.suggestion_pending) {
        setSuggestionPending(true);
        startSuggestionPolling();
      }
      if (response.closeness_level > 0 && response.closeness_level !== friend.closeness_level) {
        onFriendRefresh?.({ ...friend, closeness_level: response.closeness_level, total_turns: response.total_turns });
      }
      requestAnimationFrame(() => scrollToBottom(true));
    } catch (err) {
      setMessages((prev) => prev.filter((message) => message.id !== temp.id));
      revokeTemp(temp.local_audio_url);
      showToast(`发送失败：${errorText(err)}`, "error");
      throw err;
    } finally {
      setSending(false);
    }
  };

  const handleSendText = async (text: string) => {
    const temp: Message = {
      id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      friend_id: friend.id,
      sender: "user",
      msg_type: "text",
      transcript: text,
      audio_path: "",
      audio_url: "",
      duration_ms: 0,
      suggestion: null,
      is_read: true,
      context: "",
      created_at: new Date().toISOString(),
      pending: true,
    };
    await send(temp, () => api.sendText(friend.id, text));
  };

  const handleSendVoice = async (result: RecordingResult) => {
    const url = URL.createObjectURL(result.blob);
    tempUrlsRef.current.push(url);
    const temp: Message = {
      id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      friend_id: friend.id,
      sender: "user",
      msg_type: "voice",
      transcript: "识别中…",
      audio_path: "",
      audio_url: "",
      local_audio_url: url,
      duration_ms: result.durationMs,
      suggestion: null,
      is_read: true,
      context: "",
      created_at: new Date().toISOString(),
      pending: true,
      placeholder: true,
    };
    try {
      await send(temp, () => api.sendVoice(friend.id, result.blob, result.durationMs, result.filename));
    } catch {
      /* 错误提示已在 send 内处理，这里吞掉避免未捕获 rejection */
    }
  };

  const friendStateTone = local.period === "night" || local.period === "late_night";

  /**
   * 翻译目标语言（Lead 的 UX 约定）：
   * - 好友消息：外语 → 中文对照（zh）
   * - 我的消息：中文/英文 → 对方语言（reply_language，回退 en）
   */
  const targetFor = useCallback(
    (message: Message): string => (message.sender === "user" ? replyLanguageOf(friend) : "zh"),
    [friend],
  );

  const toggleTranslation = useCallback(
    async (message: Message) => {
      const text = message.transcript.trim();
      if (!text || message.pending) return;

      const existing = translations[message.id];
      if (existing) {
        setTranslations((prev) => ({
          ...prev,
          [message.id]: { ...existing, open: !existing.open },
        }));
        return;
      }
      if (translatingIds[message.id]) return;

      setTranslatingIds((prev) => ({ ...prev, [message.id]: true }));
      try {
        const result = await translateText({
          text,
          target: targetFor(message),
          friend_id: friend.id,
        });
        if (!result.ok) {
          // 无 LLM Key 时直接展示后端 message（引导去右上角 ⚙ 个人设置）
          showToast(result.message || "翻译失败，请稍后重试", "error");
          return;
        }
        if (!result.translation.trim() || result.translation.trim() === text) {
          // 原文已经是目标语言（例如英语好友的英语消息），不显示无意义的「译文」
          showToast(`原文已经是${result.target_label || "目标语言"}，无需翻译`, "info");
          return;
        }
        setTranslations((prev) => ({ ...prev, [message.id]: { result, open: true } }));
      } catch (err) {
        showToast(apiErrorText(err), "error");
      } finally {
        setTranslatingIds((prev) => {
          const next = { ...prev };
          delete next[message.id];
          return next;
        });
      }
    },
    [friend.id, showToast, targetFor, translatingIds, translations],
  );

  /** ⋯ 菜单：批量翻译最近 N 条（按 sender 分组 target，分别走 batch）。 */
  const handleTranslateRecent = useCallback(async () => {
    if (batchTranslating) return;
    const candidates = messages
      .filter((message) => !message.pending && message.transcript.trim().length > 0)
      .slice(-TRANSLATE_RECENT_LIMIT);
    if (candidates.length === 0) {
      showToast("还没有可翻译的聊天记录", "info");
      return;
    }
    setBatchTranslating(true);
    try {
      // batch 接口只有一个 target：按目标语言分组成 1~2 个请求
      const groups = new Map<string, Message[]>();
      for (const message of candidates) {
        const target = targetFor(message);
        const list = groups.get(target) ?? [];
        list.push(message);
        groups.set(target, list);
      }
      const responses = await Promise.all(
        Array.from(groups.entries()).map(async ([target, group]) => ({
          target,
          group,
          response: await translateBatch({
            texts: group.map((message) => message.transcript),
            target,
            friend_id: friend.id,
          }),
        })),
      );

      const nextTranslations: Record<string, TranslationEntry> = { ...translations };
      let added = 0;
      let firstFailure = "";
      for (const { group, response } of responses) {
        group.forEach((message, index) => {
          const item = response.items[index];
          const sameAsSource =
            item && item.ok && item.translation.trim() === message.transcript.trim();
          if (item && item.ok && !sameAsSource) {
            nextTranslations[message.id] = { result: item, open: true };
            added += 1;
          } else if (item && !item.ok && !firstFailure) {
            firstFailure = item.message || response.message || "";
          }
        });
        if (response.items.length === 0 && !firstFailure) {
          firstFailure = response.message || "批量翻译失败";
        }
      }
      if (added > 0) {
        setTranslations(nextTranslations);
        showToast(`已翻译最近 ${added} 条消息`, "success");
      }
      if (firstFailure) {
        showToast(firstFailure, "error");
      }
    } catch (err) {
      showToast(apiErrorText(err), "error");
    } finally {
      setBatchTranslating(false);
    }
  }, [batchTranslating, friend.id, messages, showToast, targetFor, translations]);

  const handleDeleteFriend = async () => {
    if (deleting) return;
    const ok = window.confirm(
      `确定删除好友「${friend.name}」？\n聊天记录、朋友圈和对应语音会一起删除，且不可恢复。`,
    );
    if (!ok) return;
    setDeleting(true);
    try {
      await api.deleteFriend(friend.id);
      showToast("已删除好友", "success");
      onDeleted?.(friend);
    } catch (err) {
      showToast(`删除失败：${errorText(err)}`, "error");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="se-chat">
      <header className="se-chat-header">
        <button type="button" onClick={onBack} className="se-chat-back" aria-label="返回">
          <ChevronLeft size={24} />
        </button>
        <div className="min-w-0 flex-1 text-center">
          <div className="flex min-w-0 items-center justify-center gap-1.5">
            <span className="truncate text-[15px] font-semibold">{friend.name}</span>
            <span className="se-lang-tag" title={`回复语言：${langMeta.label}`}>
              {langMeta.flag} {langMeta.label}
            </span>
            <span className="text-[12px]" aria-hidden>
              {friend.nationality ? `${local.icon}` : ""}
            </span>
          </div>
          <p className="mt-0.5 truncate text-[10.5px] text-[color:var(--se-dim)]">
            {local.time} · {local.activity || (friendStateTone ? "那边是深夜" : "在线")} · {closenessLabel(friend.closeness_level)}
          </p>
        </div>
        <button
          type="button"
          onClick={() => showToast("语音通话即将上线 📞", "info")}
          className="se-chat-action"
          aria-label="语音通话"
        >
          <Phone size={18} />
        </button>
        <button
          type="button"
          onClick={() => setMenuOpen((value) => !value)}
          className="se-chat-action"
          aria-label="更多"
        >
          <MoreHorizontal size={20} />
        </button>

        {menuOpen ? (
          <>
            <button type="button" aria-label="关闭菜单" className="fixed inset-0 z-30" onClick={() => setMenuOpen(false)} />
            <div className="se-menu">
              <button
                type="button"
                onClick={() => {
                  setMenuOpen(false);
                  setInfoOpen(true);
                }}
              >
                <Users size={14} /> 好友资料
              </button>
              <button
                type="button"
                disabled={batchTranslating}
                onClick={() => {
                  setMenuOpen(false);
                  void handleTranslateRecent();
                }}
              >
                {batchTranslating ? <Loader2 size={14} className="animate-spin" /> : <Languages size={14} />}
                {batchTranslating ? "翻译中…" : "翻译最近消息"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setMenuOpen(false);
                  void api.markRead(friend.id).then(
                    () => showToast("已标记已读", "success"),
                    () => showToast("标记已读失败", "error"),
                  );
                }}
              >
                <RefreshCw size={14} /> 标记已读
              </button>
              <button
                type="button"
                className="danger"
                disabled={deleting}
                onClick={() => {
                  setMenuOpen(false);
                  void handleDeleteFriend();
                }}
              >
                <Trash2 size={14} /> {deleting ? "删除中…" : "删除好友"}
              </button>
            </div>
          </>
        ) : null}
      </header>

      <div ref={scrollRef} className="se-chat-body">
        {loadError ? <ErrorBanner message={`聊天记录加载失败：${loadError}`} onRetry={() => void loadMessages()} /> : null}
        {loading ? <Spinner label="加载聊天记录…" /> : null}
        {!loading && !loadError && messages.length === 0 ? (
          <EmptyState
            title="还没有聊天记录"
            hint={`${friend.name} 来自 ${friend.city || "远方"}，发条语音打个招呼吧 👋`}
            icon={<span className="text-[26px]">👋</span>}
          />
        ) : null}

        {!loading && !loadError && messages.length === 1 ? (
          <div className="se-chat-hint">
            <span aria-hidden>💡</span>
            点气泡播放语音条 · 按住下方按钮回复 {friend.name}
          </div>
        ) : null}

        {messages.map((message, index) => {
          const mine = message.sender === "user";
          const showDivider = shouldShowDayDivider(messages[index - 1], message);
          return (
            <div key={message.id} className="relative">
              {showDivider ? (
                <div className="se-day-divider">
                  <span>{chatDayLabel(message.created_at, now)}</span>
                </div>
              ) : null}
              {message.msg_type === "text" || message.msg_type === "call_summary" ? (
                <TextBubble message={message} friend={friend} mine={mine} />
              ) : (
                <VoiceBubble message={message} friend={friend} mine={mine} />
              )}
              {!mine && message.suggestion ? (
                <div className="pl-[52px]">
                  <SuggestionPill suggestion={message.suggestion} message={message} friend={friend} />
                </div>
              ) : null}
              {mine && message.suggestion ? (
                <div className="flex justify-end pr-[52px]">
                  <SuggestionPill suggestion={message.suggestion} message={message} friend={friend} />
                </div>
              ) : null}

              {!message.pending && message.transcript.trim() ? (
                <>
                  <div className={`se-translate-row ${mine ? "me" : ""}`}>
                    <button
                      type="button"
                      className={`se-translate-btn ${translations[message.id] ? "done" : ""}`}
                      aria-label="翻译这条消息"
                      aria-expanded={translations[message.id]?.open === true}
                      disabled={translatingIds[message.id] === true}
                      onClick={() => void toggleTranslation(message)}
                    >
                      {translatingIds[message.id] ? (
                        <Loader2 size={10} className="animate-spin" />
                      ) : (
                        <Languages size={11} />
                      )}
                      {translatingIds[message.id] ? "翻译中…" : "译"}
                    </button>
                    {translations[message.id]?.result.cached ? (
                      <span className="se-translate-cached">缓存</span>
                    ) : null}
                  </div>
                  {translations[message.id]?.open ? (
                    <div className={`se-translate-wrap ${mine ? "me" : ""}`}>
                      <button
                        type="button"
                        className="se-translation"
                        onClick={() => void toggleTranslation(message)}
                        aria-label="收起译文"
                      >
                        <span className="se-translation-head">
                          <Languages size={10} />
                          译 · {translations[message.id]?.result.target_label || "中文"}
                          <span className="se-translation-collapse">收起</span>
                        </span>
                        <span className="se-translation-text">
                          {translations[message.id]?.result.translation}
                        </span>
                      </button>
                    </div>
                  ) : null}
                </>
              ) : null}
            </div>
          );
        })}

        {suggestionPending ? (
          <div className="se-typing">
            <span className="se-typing-dots" aria-hidden>
              <i />
              <i />
              <i />
            </span>
            正在润色你的表达…
          </div>
        ) : null}
        <div className="h-2" />
      </div>

      <Composer onSendVoice={handleSendVoice} onSendText={handleSendText} sending={sending} disabled={loading && messages.length === 0} />

      {infoOpen ? (
        <div className="absolute inset-0 z-40 flex items-end">
          <button type="button" aria-label="关闭" className="absolute inset-0 bg-black/40" onClick={() => setInfoOpen(false)} />
          <div className="se-sheet anim-in">
            <div className="se-sheet-head">
              <span>好友资料</span>
              <button type="button" onClick={() => setInfoOpen(false)} aria-label="关闭">
                <X size={16} />
              </button>
            </div>
            <div className="se-sheet-body">
              <div className="flex items-center gap-3">
                <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={54} />
                <div className="min-w-0">
                  <p className="text-[16px] font-semibold">{friend.name}</p>
                  <p className="text-[12px] text-[color:var(--se-dim)]">
                    {friend.nationality} · {friend.city} · {friend.age ? `${friend.age} 岁` : ""}
                  </p>
                  <p className="text-[12px] text-[color:var(--se-dim)]">
                    {local.time} 当地 · {closenessLabel(friend.closeness_level)} · {friend.total_turns} 轮对话
                  </p>
                  <p className="mt-1 flex items-center gap-1.5 text-[11px] text-[color:var(--se-dim)]">
                    <span className="se-lang-tag">
                      {langMeta.flag} {langMeta.label}
                    </span>
                    回复语言 · {friend.nationality}
                  </p>
                </div>
              </div>
              {friend.bio ? <p className="mt-3 text-[12.5px] leading-relaxed text-[color:var(--se-dim)]">{friend.bio}</p> : null}
              {friend.speech_quirks.length > 0 ? (
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {friend.speech_quirks.map((quirk) => (
                    <span key={quirk} className="se-quirk">
                      “{quirk}”
                    </span>
                  ))}
                </div>
              ) : null}
              {friend.shared_episodes.length > 0 ? (
                <div className="mt-4">
                  <p className="se-sheet-label">共同记忆</p>
                  <ul className="mt-1 space-y-1">
                    {friend.shared_episodes.slice(0, 4).map((episode) => (
                      <li key={episode} className="text-[12px] leading-relaxed text-[color:var(--se-dim)]">
                        · {episode}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {friend.open_loops.length > 0 ? (
                <div className="mt-4">
                  <p className="se-sheet-label">未完结的话题</p>
                  <ul className="mt-1 space-y-1">
                    {friend.open_loops.slice(0, 4).map((loop) => (
                      <li key={loop.event} className="text-[12px] leading-relaxed text-[color:var(--se-dim)]">
                        · {loop.event} {loop.follow_up_after ? `（${relativeLabel(loop.follow_up_after, now)}）` : ""}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}

      {sending ? (
        <div className="pointer-events-none absolute right-3 top-[64px] z-20 flex items-center gap-1.5 rounded-full bg-black/55 px-2.5 py-1 text-[11px] text-white">
          <Loader2 size={11} className="animate-spin" />
          对方正在说话…
        </div>
      ) : null}
    </div>
  );
}
