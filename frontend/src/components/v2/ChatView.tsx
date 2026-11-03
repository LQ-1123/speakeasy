"use client";

/** 单聊页：语音条消息流 + 润色胶囊 + 按住说话 / 文字兜底。 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  ImageOff,
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
import { CallView } from "@/components/v2/CallView";
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

/** 服务端轮询回来的「自己消息」是否与本地乐观气泡是同一条。 */
function samePendingMessage(local: Message, server: Message): boolean {
  if (!local.pending || local.sender !== "user" || server.sender !== "user") return false;
  if (local.msg_type !== server.msg_type) return false;
  const delta = Math.abs(timeOf(local) - timeOf(server));
  if (!Number.isFinite(delta) || delta > 120_000) return false;
  if (local.msg_type === "text") {
    return local.transcript.trim() === server.transcript.trim();
  }
  // 语音/图片：类型一致 + 时间接近即视为同一条（语音转写/图片 URL 在服务端才确定）
  return true;
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

/** 图片消息气泡：图片 + caption（图片下方），发送中显示 spinner。 */
function ImageBubble({ message, friend, mine }: { message: Message; friend: Friend; mine: boolean }) {
  const [failed, setFailed] = useState(false);
  const src = message.local_image_url || message.image_url;
  const caption =
    message.transcript && message.transcript !== "（发来一张图片）" ? message.transcript : "";

  return (
    <div className={`se-bubble-row ${mine ? "me" : ""}`}>
      {!mine ? <Avatar name={friend.name} seed={friend.avatar_seed} friend={friend} size={38} /> : null}
      <div className={`se-image-card ${mine ? "me" : ""}`}>
        {src && !failed ? (
          <img
            src={src}
            alt={caption || "图片消息"}
            className="se-image"
            loading="lazy"
            onError={() => setFailed(true)}
          />
        ) : (
          <div className="se-image-placeholder">
            <ImageOff size={16} />
            {failed ? "图片加载失败" : "图片发送中…"}
          </div>
        )}
        {message.pending ? (
          <span className="se-image-pending">
            <Loader2 size={10} className="animate-spin" />
            发送中…
          </span>
        ) : null}
        {caption ? <p className="se-image-caption">{caption}</p> : null}
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
  const [remoteReplying, setRemoteReplying] = useState(false);
  const [suggestionPending, setSuggestionPending] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [callOpen, setCallOpen] = useState(false);
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
      if (!mountedRef.current) {
        stopPolling();
        return;
      }
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

  /** 聊天窗打开期间的轻量消息轮询：补上「服务端稍后才生成的消息」，
   *  例如好友主动追问、发送超时后才落库的回复、其他端产生的新消息。 */
  const livePollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const livePollInFlightRef = useRef(false);

  const refreshMessagesSilently = useCallback(async () => {
    if (!mountedRef.current) return;
    if (document.visibilityState !== "visible") return;
    if (livePollInFlightRef.current || pollInFlightRef.current) return;
    livePollInFlightRef.current = true;
    const node = scrollRef.current;
    const nearBottom = node ? node.scrollHeight - node.scrollTop - node.clientHeight < 160 : true;
    try {
      const list = await api.chatMessages(friendRef.current.id, 30);
      if (!mountedRef.current) return;
      setMessages((prev) => {
        // 发送请求还没返回时，轮询会先拿到服务端的同一条消息；
        // 先移除对应的本地乐观气泡，再合并，避免自己消息出现两遍。
        const remaining = [...prev];
        for (const incoming of list) {
          if (incoming.sender !== "user") continue;
          const idx = remaining.findIndex(
            (message) => message.pending && samePendingMessage(message, incoming),
          );
          if (idx >= 0) remaining.splice(idx, 1);
        }
        return mergeMessages(remaining, list);
      });
      // 聊天窗打开时拉到的消息都已展示 → 标已读（含迟到的好友消息）
      void api.markRead(friendRef.current.id).catch(() => undefined);
      if (nearBottom) requestAnimationFrame(() => scrollToBottom());
    } catch {
      /* 轮询失败静默，等下一次 */
    } finally {
      livePollInFlightRef.current = false;
    }
  }, [scrollToBottom]);

  useEffect(() => {
    const timer = setInterval(() => void refreshMessagesSilently(), 4000);
    livePollRef.current = timer;
    const onVisibility = () => {
      if (document.visibilityState === "visible") void refreshMessagesSilently();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      clearInterval(timer);
      livePollRef.current = null;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refreshMessagesSilently]);

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

  // 服务端「正在生成回复」状态：离开聊天再进来 / 回到主界面时也能恢复
  useEffect(() => {
    let active = true;
    const tick = async () => {
      try {
        const status = await api.friendStatus(friendRef.current.id);
        if (active) setRemoteReplying(status.replying);
      } catch {
        /* 网络抖动静默 */
      }
    };
    void tick();
    const timer = setInterval(tick, 2000);
    return () => {
      active = false;
      clearInterval(timer);
    };
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

  /** 清理本地临时气泡的 blob（语音 / 图片） */
  const releaseTempUrls = (message: Message) => {
    revokeTemp(message.local_audio_url);
    revokeTemp(message.local_image_url);
  };

  const send = async (temp: Message, request: () => Promise<Awaited<ReturnType<typeof api.sendText>>>) => {
    setSending(true);
    setMessages((prev) => mergeMessages(prev, [temp]));
    requestAnimationFrame(() => scrollToBottom(true));
    try {
      const response = await request();
      if (!mountedRef.current) {
        // 用户等待期间已离开聊天：不更新 UI、不标已读（回复算未读）、不启动轮询
        releaseTempUrls(temp);
        return;
      }
      setMessages((prev) => {
        const withoutTemp = prev.filter((message) => message.id !== temp.id);
        const incoming: Message[] = [];
        if (response.user_message) incoming.push(response.user_message);
        incoming.push(...response.friend_messages);
        return mergeMessages(withoutTemp, incoming);
      });
      releaseTempUrls(temp);
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
      releaseTempUrls(temp);
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
      image_url: "",
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
      image_url: "",
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

  const handleSendImage = async (file: File, caption: string) => {
    const url = URL.createObjectURL(file);
    tempUrlsRef.current.push(url);
    const temp: Message = {
      id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      friend_id: friend.id,
      sender: "user",
      msg_type: "image",
      transcript: caption.trim(),
      audio_path: "",
      audio_url: "",
      image_url: "",
      local_image_url: url,
      duration_ms: 0,
      suggestion: null,
      is_read: true,
      context: "",
      created_at: new Date().toISOString(),
      pending: true,
    };
    // 失败时抛出，让 Composer 保留待发送预览（错误提示已在 send 内 toast）
    await send(temp, () => api.sendImage(friend.id, file, caption, file.name || "image.png"));
  };

  const replying = sending || remoteReplying;
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
      .filter(
        (message) =>
          !message.pending &&
          message.msg_type !== "image" &&
          !message.image_url &&
          !message.local_image_url &&
          message.transcript.trim().length > 0,
      )
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
            {replying ? (
              <span className="se-header-typing" aria-live="polite">
                对方正在说话
                <span className="se-typing-dots" aria-hidden>
                  <i />
                  <i />
                  <i />
                </span>
              </span>
            ) : (
              <>
                {local.time} · {local.activity || (friendStateTone ? "那边是深夜" : "在线")} ·{" "}
                {closenessLabel(friend.closeness_level)}
              </>
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCallOpen(true)}
          className="se-chat-action"
          aria-label="语音通话"
          title="语音通话"
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
          const isImage =
            message.msg_type === "image" || Boolean(message.image_url) || Boolean(message.local_image_url);
          const showDivider = shouldShowDayDivider(messages[index - 1], message);
          return (
            <div key={message.id} className="relative">
              {showDivider ? (
                <div className="se-day-divider">
                  <span>{chatDayLabel(message.created_at, now)}</span>
                </div>
              ) : null}
              {isImage ? (
                <ImageBubble message={message} friend={friend} mine={mine} />
              ) : message.msg_type === "text" || message.msg_type === "call_summary" ? (
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

              {!message.pending && !isImage && message.transcript.trim() ? (
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

      <Composer
        onSendVoice={handleSendVoice}
        onSendText={handleSendText}
        onSendImage={handleSendImage}
        sending={sending}
        disabled={loading && messages.length === 0}
      />

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

      {callOpen ? (
        <CallView
          friend={friend}
          onClose={() => {
            setCallOpen(false);
            // 通话结束刷新消息列表：call_summary 会出现在聊天记录里
            void loadMessages();
          }}
        />
      ) : null}
    </div>
  );
}
