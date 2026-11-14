"use client";

/**
 * 原型状态：所有交互都在这里发生，组件只负责展示。
 *
 * 之所以把「等待好友回复 / 生成润色建议 / AI 评论」也做出来，
 * 是因为这些异步状态决定了产品用起来像不像真的：
 * 没有「对方正在说话…」的停顿，再好看的界面也只是静态稿。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createStore, useStore, type StoreApi } from "zustand";
import { createClientId, mergeMessages, reconcileThread } from "./messageCache";
import { useNavigation } from "./navigationStore";


import {
  loadAll as apiLoadAll,
  retryMessageAudio as apiRetryMessageAudio,
  requestMessageSuggestion as apiRequestMessageSuggestion,
  sendText as apiSendText,
  sendVoiceBlob as apiSendVoiceBlob,
  fetchMessages as apiFetchMessages,
  acceptFriend as apiAcceptFriend,
  markThreadRead as apiMarkThreadRead,
  declineFriend as apiDeclineFriend,
  removeFriend as apiRemoveFriend,
  deleteExpressionOnServer as apiDeleteExpression,
  generateMoments as apiGenerateMoments,
  loadMoments as apiLoadMoments,
  patchExpression as apiPatchExpression,
  publishMomentToServer as apiPublishMoment,
  translate as apiTranslate,
  deleteMoment as apiDeleteMoment,
  loadFriendList as apiLoadFriendList,
} from "./apiFacade";
import type { Expression, Friend, Message, Moment, MomentComment, Profile, Suggestion } from "./model";
import { mapClientBootstrap, uploadChatImage as apiUploadChatImage } from "./api";
import { clientApi, nativeBridge } from "./v3/clientApi";
import { connectClientEvents } from "./v3/clientEvents";
import { queryClient } from "./queryClient";
import { AudioPoller, CoalescedRefresh, eventRefreshTargets } from "./clientRefresh";
import { canRequestSuggestion } from "./messageActions";
import { addExpression as apiAddExpression } from "./api";
import { playMessageSound, playRequestSound, unlockNotifyAudio } from "./notify";

interface Toast {
  id: number;
  text: string;
  tone?: "default" | "success" | "error";
}

interface Store {
  friends: Friend[];
  requests: Friend[];
  messages: Message[];
  moments: Moment[];
  expressions: Expression[];
  profile: Profile;
  toast: Toast | null;
  typing: Record<string, boolean>;
  /** 首次 apiLoadAll 是否已经结束（成功或失败都算）：避免深链/通知打开时先闪错误页 */
  booted: boolean;
  /** 哪些消息/动态展开了中文（视图状态，不写进数据） */
  zhOpen: Record<string, boolean>;
  showToast: (text: string, tone?: Toast["tone"]) => void;
  messagesOf: (friendId: string) => Message[];
  friendById: (id: string) => Friend | undefined;
  sendText: (friendId: string, text: string) => void;
  /** 重发一条发送失败的文字消息：复用原 clientId，服务端幂等不会重复落库 */
  retryMessage: (messageId: string) => void;
  retryAudio: (messageId: string) => void;
  requestSuggestion: (messageId: string) => Promise<void>;
  /** 真实录音发送（走后端 STT + LLM + TTS） */
  sendVoiceBlob: (
    friendId: string,
    blob: Blob,
    durationMs: number,
    opts?: { openTranscript?: boolean },
  ) => Promise<void>;
  sendImage: (friendId: string, file: Blob, text?: string) => void;
  markRead: (friendId: string) => void;
  toggleZh: (key: string) => void;
  /** 单条消息翻译：没有译文就现翻，有就收起/展开 */
  translateMessage: (messageId: string) => Promise<void>;
  translateAll: (friendId: string) => void;
  translateAllMoments: () => void;
  toggleShownText: (messageId: string) => void;
  saveSuggestion: (messageId: string) => void;
  dismissSuggestion: (messageId: string) => void;
  likeMoment: (momentId: string) => void;
  addComment: (momentId: string, text: string) => void;
  /** 删除自己发的动态 */
  deleteMoment: (momentId: string) => Promise<void>;
  publishMoment: (text: string, images: string[], mood: string) => void;
  /** 重新拉一次朋友圈（发布后 AI 评论是后台生成的） */
  refreshMoments: () => Promise<void>;
  /** 让 AI 好友生成今天的朋友圈 */
  generateMoments: () => Promise<void>;
  acceptRequest: (friendId: string) => void;
  declineRequest: (friendId: string) => void;
  practiceExpression: (id: string, remembered: boolean) => void;
  toggleMastered: (id: string) => void;
  removeExpression: (id: string) => void;
  updateProfile: (patch: Partial<Profile>) => void;
  deleteFriend: (friendId: string) => void;
  /** 偶遇捏到新人后，刷新好友/会话，让新朋友能被打开 */
  reloadAll: () => Promise<void>;
  loadThread: (friendId: string) => Promise<void>;
  loadOlderThread: (friendId: string) => Promise<void>;
  threadHasMore: Record<string, boolean>;
  bootError: string;
}

/**
 * AI 好友之间不互相评论、不互相点赞。
 *
 * 原因：
 *  - 对练习没有价值（用户只能旁观）
 *  - 会让「这些是真实母语者」的设定显得像剧本
 *  - 每条 AI 评论都是一次 LLM 调用，钱花在用户不会看的地方
 *
 * 所以：好友发的动态只保留「用户自己的赞/评论」；用户发的动态才接收好友评论。
 */
function sanitizeMoment(m: Moment): Moment {
  if (m.authorId === "me") return m;
  const mineLiked = Array.isArray(m.likedBy) ? m.likedBy.includes("me") : false;
  return {
    ...m,
    comments: [],
    commentsPending: false,
    likedBy: mineLiked ? ["me"] : [],
    liked: mineLiked,
  };
}

/** 后端 /settings + /settings/tts → store 里的 Settings（只做展示需要的映射） */
const AppCtx = createContext<StoreApi<Store> | null>(null);

/**
 * 合并后端返回的 profile 补丁：只覆盖真的有值的字段。
 *
 * reloadAll / 事件推送拿到的 `Partial<Profile>` 里，可选字段可能是 undefined；
 * 直接 `...patch` 会把本地已有值（比如刚在 Onboarding 里填的昵称）清空。
 */
function mergeProfilePatch(prev: Profile, patch: Partial<Profile>): Profile {
  const next: Profile = { ...prev };
  for (const key of Object.keys(patch) as Array<keyof Profile>) {
    const value = patch[key];
    if (value !== undefined) {
      (next as unknown as Record<string, unknown>)[key] = value;
    }
  }
  return next;
}

export function useApp(): Store;
export function useApp<T>(selector: (state: Store) => T): T;
export function useApp<T>(selector?: (state: Store) => T): T | Store {
  const ctx = useContext(AppCtx);
  if (!ctx) throw new Error("useApp must be used inside <AppProvider>");
  return useStore(ctx, selector ?? ((state: Store) => state as unknown as T));
}

let seq = 1000;
const nextId = (prefix: string) => `${prefix}-${(seq += 1)}`;

export function AppProvider({ children }: { children: ReactNode }) {
  const [friends, setFriends] = useState<Friend[]>([]);
  const [requests, setRequests] = useState<Friend[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [threadHasMore, setThreadHasMore] = useState<Record<string, boolean>>({});
  const [moments, setMoments] = useState<Moment[]>(() => []);
  const [expressions, setExpressions] = useState<Expression[]>([]);
  const [profile, setProfile] = useState<Profile>({ name: '', avatar: '', city: '', job: '', targets: ['en'], level: '能聊几句', interests: [], weaknesses: [], gender: '', nativeLanguage: 'zh', levels: {}, birthday: '', setupCompleted: undefined, communicationPreference: '', setupCompletedAt: '' });
  const [bootError, setBootError] = useState("");
  const [booted, setBooted] = useState(false);
  /* 提示音：跟踪未读数 / 好友申请数变化 */
  const notifyReady = useRef(false);
  const lastUnread = useRef(0);
  const lastRequests = useRef(0);

  useEffect(() => {
    const unlock = () => unlockNotifyAudio();
    document.addEventListener("pointerdown", unlock, { once: true });
    return () => document.removeEventListener("pointerdown", unlock);
  }, []);
  const [typing, setTyping] = useState<Record<string, boolean>>({});
  const [zhOpen, setZhOpen] = useState<Record<string, boolean>>({});
  const [toast, setToast] = useState<Toast | null>(null);
  const fetchThread = useCallback((friendId: string) => queryClient.fetchQuery({
    queryKey: ["thread", friendId], queryFn: () => apiFetchMessages(friendId), staleTime: 0,
  }), []);
  const audioPoller = useMemo(() => new AudioPoller(async (friendId) => {
    if (document.visibilityState === "hidden") return true;
    const fresh = await fetchThread(friendId);
    if (!fresh) return true;
    setMessages((list) => mergeMessages(list, fresh));
    return fresh.some((message) => message.audioPending);
  }), [fetchThread]);
  useEffect(() => () => audioPoller.stop(), [audioPoller]);


  useEffect(() => {
    let cancelled = false;
    void (async () => {
      /* 旧版用 localStorage 记「已看过」，现在红点直接跟后端 unread 走，清掉遗留键 */
      try {
        Object.keys(window.localStorage)
          .filter((k) => k.startsWith("w4.seen.") || k === "w4.baseline")
          .forEach((k) => window.localStorage.removeItem(k));
      } catch {
        /* 隐私模式忽略 */
      }

      const snap = await apiLoadAll().catch((error) => { setBootError(error instanceof Error ? error.message : "Unable to connect"); return null; });
      if (cancelled) return;
      setBooted(true);
      if (!snap) return;
      setBootError("");
      setFriends(snap.friends);
      setMessages((list) => mergeMessages(list, snap.messages));
      setRequests(snap.requests ?? []);
      lastUnread.current = snap.friends.reduce((n, f) => n + (f.unread || 0), 0);
      lastRequests.current = (snap.requests ?? []).length;
      setProfile((prev) => ({ ...prev, ...snap.profile }));

      setExpressions(snap.expressions);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const timers = useRef<number[]>([]);

  /* 好友/申请轮询：别人来加你好友、好友裂变推荐，不用刷新页面就能看到 */
  useEffect(() => {
    let stopped = false;
    const refresh = async () => {
      if (stopped || document.visibilityState === "hidden") return;
      const snap = await apiLoadFriendList();
      if (stopped || !snap) return;
      const unread = snap.friends.reduce((n, f) => n + (f.unread || 0), 0);
      lastUnread.current = unread;
      lastRequests.current = snap.requests.length;
      notifyReady.current = true;
      setFriends(snap.friends);
      setRequests(snap.requests);
    };
    const friendRefresh = new CoalescedRefresh(refresh);
    const threadRefresh = new CoalescedRefresh(async () => {
      const route = useNavigation.getState().route;
      if (route.name !== "chat" || stopped) return;
      const fresh = await fetchThread(route.friendId);
      if (!fresh || stopped) return;
      setMessages((list) => mergeMessages(list, fresh));
      if (!fresh.some((message) => message.audioPending)) audioPoller.cancel(route.friendId);
    });
    const disconnect = connectClientEvents((events) => {
      const route = useNavigation.getState().route;
      const target = eventRefreshTargets(events, route.name === "chat" ? route.friendId : null);
      if (target.friends) void friendRefresh.run();
      if (target.thread) void threadRefresh.run();
    }, (raw) => {
      if (stopped) return;
      const snapshot = mapClientBootstrap(raw);
      setFriends(snapshot.friends);
      setRequests(snapshot.requests);
      setExpressions(snapshot.expressions);
      setProfile((profile) => mergeProfilePatch(profile, snapshot.profile));
      setMessages((list) => mergeMessages(list, snapshot.messages));
    });
    const onVisible = () => { if (document.visibilityState === "visible") { void friendRefresh.run(); void threadRefresh.run(); } };
    const onKick = () => { void friendRefresh.run(); void threadRefresh.run(); };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("speakeasy:refresh", onKick);
    return () => {
      stopped = true;
      disconnect();
      friendRefresh.stop();
      threadRefresh.stop();
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("speakeasy:refresh", onKick);
    };
  }, []);
  const toastTimer = useRef<number | null>(null);
  useEffect(() => {
    const list = timers.current;
    return () => {
      list.forEach((t) => window.clearTimeout(t));
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
    };
  }, []);

  const later = useCallback((fn: () => void, ms: number) => {
    const t = window.setTimeout(fn, ms);
    timers.current.push(t);
  }, []);

  const showToast = useCallback((text: string, tone: Toast["tone"] = "default") => {
    setToast({ id: Date.now(), text, tone });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 2400);
  }, []);

  const messagesOf = useCallback(
    (friendId: string) =>
      [...messages]
        .filter((m) => m.friendId === friendId)
        .sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime()),
    [messages],
  );

  const friendById = useCallback((id: string) => friends.find((f) => f.id === id), [friends]);

  const replyIndex = useRef<Record<string, number>>({});
  const transcriptIndex = useRef(0);
  const suggestionIndex = useRef(0);

  const markRead = useCallback((friendId: string) => {
    setFriends((list) => list.map((f) => (f.id === friendId && f.unread ? { ...f, unread: 0 } : f)));
    setMessages((list) => list.map((m) => (m.friendId === friendId && !m.read ? { ...m, read: true } : m)));
    /* 同步到后端：否则刷新一次红点又回来了，看起来就像假红点 */
    void apiMarkThreadRead(friendId);
  }, []);

  /* 正在 POST 的 clientId：防止同一 clientId 被并发重复提交 */
  const inFlightSends = useRef(new Set<string>());
  /* 同一好友 600ms 内重复提交同一句（双击/回车连发）直接忽略 */
  const lastSend = useRef<Record<string, { text: string; at: number }>>({});

  /* 生产后端会先落文字、后台补 Fish 语音：短轮询把 audio_url 取回来，
     用户先看到文字，不必等 3–8 秒合成；最多轮询 6 次（约 10 秒）。 */
  const scheduleAudioRefresh = useCallback((friendId: string, fresh: Message[]) => {
    if (fresh.some((message) => message.audioPending)) audioPoller.start(friendId);
    else audioPoller.cancel(friendId);
  }, [audioPoller]);

  const postText = useCallback(
    async (friendId: string, text: string, clientId: string, tempId: string) => {
      if (inFlightSends.current.has(clientId)) return;
      inFlightSends.current.add(clientId);
      setTyping((state) => ({ ...state, [friendId]: true }));
      const fresh = await apiSendText(friendId, text, clientId);
      inFlightSends.current.delete(clientId);
      setTyping((state) => ({ ...state, [friendId]: false }));
      if (!fresh) {
        /* 失败不再无限「发送中」：保留气泡并标 failed，点重试复用同一 clientId */
        setMessages((list) =>
          list.map((m) => (m.clientId === clientId ? { ...m, failed: true } : m)),
        );
        showToast("消息发送失败，点气泡旁的「重试」", "error");
        return;
      }
      setMessages((list) => mergeMessages(list, fresh, [tempId]));
      scheduleAudioRefresh(friendId, fresh);
      setFriends((list) => list.map((friend) => friend.id === friendId ? { ...friend, unread: 0 } : friend));
    },
    [showToast, scheduleAudioRefresh],
  );

  const sendText = useCallback((friendId: string, text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    const now = Date.now();
    const previous = lastSend.current[friendId];
    /* 防重入：双击发送 / 输入法回车连发同一句时不产生两条消息 */
    if (previous && previous.text === trimmed && now - previous.at < 600) return;
    lastSend.current[friendId] = { text: trimmed, at: now };

    const clientId = createClientId();
    const tempId = nextId("pending");
    setMessages((list) => [
      ...list,
      {
        id: tempId,
        clientId,
        friendId,
        from: "me",
        kind: "text",
        text: trimmed,
        time: new Date().toISOString(),
        read: false,
      },
    ]);
    void postText(friendId, trimmed, clientId, tempId);
  }, [postText]);

  const retryMessage = useCallback((messageId: string) => {
    const target = messages.find((m) => m.id === messageId);
    if (!target || target.from !== "me" || !target.clientId || !target.failed) return;
    setMessages((list) => list.map((m) => (m.id === messageId ? { ...m, failed: false } : m)));
    lastSend.current[target.friendId] = { text: target.text, at: Date.now() };
    void postText(target.friendId, target.text, target.clientId, target.id);
  }, [messages, postText]);

  const retryingAudio = useRef(new Set<string>());
  const requestingSuggestions = useRef(new Set<string>());
  const requestSuggestion = useCallback(async (messageId: string) => {
    const target = messages.find((message) => message.id === messageId);
    if (!target || !canRequestSuggestion(target) || requestingSuggestions.current.has(messageId)) return;
    requestingSuggestions.current.add(messageId);
    try {
      const fresh = await apiRequestMessageSuggestion(messageId, target.friendId);
      if (fresh) setMessages((list) => mergeMessages(list, [fresh]));
      else {
        const thread = await fetchThread(target.friendId);
        if (thread) setMessages((list) => mergeMessages(list, thread));
      }
    } catch (error) {
      showToast(error instanceof Error ? error.message : "润色失败，请稍后重试", "error");
    } finally { requestingSuggestions.current.delete(messageId); }
  }, [messages, fetchThread, showToast]);
  const retryAudio = useCallback((messageId: string) => {
    const target = messages.find((message) => message.id === messageId);
    if (!target || target.audioStatus !== "failed" || retryingAudio.current.has(messageId)) return;
    retryingAudio.current.add(messageId);
    setMessages((list) => mergeMessages(list, [{ ...target, audioStatus: "queued", audioPending: true, audioError: undefined }]));
    void (async () => {
      try {
        const fresh = await apiRetryMessageAudio(messageId, target.friendId);
        const next = fresh ?? { ...target, audioStatus: "queued" as const, audioPending: true, audioError: undefined };
        setMessages((list) => mergeMessages(list, [next]));
        scheduleAudioRefresh(target.friendId, [next]);
      } catch (error) {
        setMessages((list) => list.map((message) => message.id === messageId && message.audioPending && !message.audio ? target : message));
        showToast(error instanceof Error ? error.message : "语音重试失败，请稍后重试", "error");
      } finally { retryingAudio.current.delete(messageId); }
    })();
  }, [messages, scheduleAudioRefresh, showToast]);

  const sendVoiceBlob = useCallback(
    async (
      friendId: string,
      blob: Blob,
      durationMs: number,
      opts?: { openTranscript?: boolean },
    ) => {
      const tempId = nextId("u");
      setMessages((list) => [
        ...list,
        {
          id: tempId,
          friendId,
          from: "me",
          kind: "voice",
          text: "（正在识别…）",
          durationMs,
          time: new Date().toISOString(),
          read: false,
          shownText: Boolean(opts?.openTranscript),
        },
      ]);
      setTyping((t) => ({ ...t, [friendId]: true }));

      const fresh = await apiSendVoiceBlob(friendId, blob, durationMs);
      setTyping((t) => ({ ...t, [friendId]: false }));

      if (!fresh) {
        setMessages((list) => list.filter((m) => m.id !== tempId));
        showToast("这条没发出去，再试一次", "error");
        return;
      }
      /* 后端返回的是该好友的完整消息列表：用它替换这一栏 */
      let merged = fresh;
      if (opts?.openTranscript) {
        const idx = fresh.reduce(
          (acc: number, m: Message, i: number) =>
            m.from === "me" && m.kind === "voice" ? i : acc,
          -1,
        );
        if (idx >= 0) {
          merged = fresh.map((m: Message, i: number) =>
            i === idx ? { ...m, shownText: true } : m,
          );
        }
      }
      setMessages((list) => mergeMessages(list, merged, [tempId]));
      scheduleAudioRefresh(friendId, merged);

    },
    [showToast, scheduleAudioRefresh],
  );

  const sendImage = useCallback(
    async (friendId: string, file: Blob, text = "") => {
      setTyping((t) => ({ ...t, [friendId]: true }));
      const localUrl = URL.createObjectURL(file);
      setMessages((list) => [
        ...list,
        {
          id: nextId("u"),
          friendId,
          from: "me",
          kind: "image",
          text,
          image: localUrl,
          time: new Date().toISOString(),
          read: false,
        },
      ]);
      const ok = await apiUploadChatImage(friendId, file, text);
      if (!ok) {
        setTyping((t) => ({ ...t, [friendId]: false }));
        showToast("图片发送失败，请检查网络", "error");
        return;
      }
      setTyping((t) => ({ ...t, [friendId]: false }));
      const fresh = await apiFetchMessages(friendId);
      if (fresh && fresh.length) {
        setMessages((list) => [
          ...list.filter((m) => m.friendId !== friendId),
          ...fresh,
        ]);
      }
      setFriends((list) =>
        list.map((f) =>
          f.id === friendId ? { ...f, lastActive: "刚刚", unread: 0 } : f,
        ),
      );
    },
    [showToast, apiFetchMessages],
  );

  const toggleZh = useCallback((key: string) => {
    setZhOpen((open) => ({ ...open, [key]: !open[key] }));
  }, []);

  const translateMessage = useCallback(
    async (messageId: string) => {
      const msg = messages.find((m) => m.id === messageId);
      if (!msg) return;
      /* 已有译文：只切换显示 */
      if (msg.translation) {
        setZhOpen((open) => ({ ...open, [messageId]: !open[messageId] }));
        return;
      }
      showToast("正在翻译…");
      const zh = await apiTranslate(msg.text, msg.friendId, profile.nativeLanguage || "zh");
      if (!zh) {
        showToast("翻译失败，稍后再试", "error");
        return;
      }
      setMessages((list) =>
        list.map((m) => (m.id === messageId ? { ...m, translation: zh } : m)),
      );
      setZhOpen((open) => ({ ...open, [messageId]: true }));
    },
    [messages, showToast, profile.nativeLanguage],
  );

  const translateAll = useCallback(
    (friendId: string) => {
      setZhOpen((open) => {
        const next = { ...open };
        messages
          .filter((m) => m.friendId === friendId && m.from === "friend" && m.translation)
          .forEach((m) => {
            next[m.id] = true;
          });
        return next;
      });
      showToast("已翻译最近消息", "success");
    },
    [messages, showToast, profile.nativeLanguage],
  );

  const translateAllMoments = useCallback(() => {
    setZhOpen((open) => {
      const next = { ...open };
      moments.forEach((m) => {
        if (m.authorId !== "me" && m.translation) next[`mo:${m.id}`] = true;
      });
      return next;
    });
    showToast("已翻译全部动态", "success");
  }, [moments, showToast]);

  const toggleShownText = useCallback((messageId: string) => {
    setMessages((list) =>
      list.map((m) => (m.id === messageId ? { ...m, shownText: !m.shownText } : m)),
    );
  }, []);

  const saveSuggestion = useCallback(
    (messageId: string) => {
      const msg = messages.find((m) => m.id === messageId);
      if (!msg?.suggestion || msg.suggestion.saved) return;
      void apiAddExpression(msg.suggestion.better, msg.text, msg.suggestion.why).then((saved) => {
        if (!saved) { showToast("Could not save expression. Please try again.", "error"); return; }
        setExpressions((list) => [saved, ...list.filter((entry) => entry.id !== saved.id)]);
        setMessages((list) => list.map((m) => m.id === messageId ? { ...m, suggestion: { ...m.suggestion!, saved: true } } : m));
        showToast("Expression saved", "success");
      });
    }, [messages, showToast],
  );

  const dismissSuggestion = useCallback((messageId: string) => {
    setMessages((list) => list.map((m) => (m.id === messageId ? { ...m, suggestion: undefined } : m)));
  }, []);

  const likeMoment = useCallback((momentId: string) => {
    setMoments((list) =>
      list.map((m) => {
        if (m.id !== momentId) return m;
        const liked = !m.liked;
        return {
          ...m,
          liked,
          likedBy: liked ? [...m.likedBy, "me"] : m.likedBy.filter((id) => id !== "me"),
        };
      }),
    );
  }, []);

  const deleteMoment = useCallback(
    async (momentId: string) => {
      const ok = await apiDeleteMoment(momentId);
      if (!ok) {
        showToast("删除失败，稍后再试");
        return;
      }
      setMoments((list) => list.filter((m) => m.id !== momentId));
      showToast("已删除");
    },
    [showToast],
  );

  const addComment = useCallback((momentId: string, text: string) => {
    const comment: MomentComment = { id: nextId("cm"), friendId: "me", text, time: "刚刚" };
    setMoments((list) =>
      list.map((m) => (m.id === momentId ? { ...m, comments: [...m.comments, comment] } : m)),
    );
  }, []);

  const publishMoment = useCallback((text: string, _images: string[], mood: string) => {
    void apiPublishMoment(text, mood).then(async (ok) => {
      if (!ok) { showToast("Post could not be saved", "error"); return; }
      const fresh = await apiLoadMoments();
      if (fresh) setMoments(fresh);
    });
  }, [showToast]);

  const acceptRequest = useCallback(
    (friendId: string) => {
      const req = requests.find((r) => r.id === friendId);
      if (!req) return;
      setRequests((list) => list.filter((r) => r.id !== friendId));
      setFriends((list) => [
        { ...req, isRequest: false, requestNote: undefined, closeness: 1, unread: 1, lastActive: "刚刚" },
        ...list,
      ]);
      setMessages((list) => [
        ...list,
        {
          id: nextId("f"),
          friendId,
          from: "friend",
          kind: "voice",
          text: req.greeting,
          translation: req.greetingZh,
          durationMs: 5200,
          time: new Date().toISOString(),
          read: false,
        },
      ]);
      showToast(`已添加 ${req.name}`, "success");
      void apiAcceptFriend(friendId);
    },
    [requests, showToast],
  );

  const declineRequest = useCallback((friendId: string) => {
    setRequests((list) => list.filter((r) => r.id !== friendId));
    showToast("已忽略");
    void apiDeclineFriend(friendId);
  }, [showToast]);

  const reloadAll = useCallback(async () => {
    setBooted(false);
    const snap = await apiLoadAll().catch((error) => { setBootError(error instanceof Error ? error.message : "Unable to connect"); return null; });
    setBooted(true);
    if (!snap) return;
    setFriends(snap.friends);
    setMessages((list) => mergeMessages(list, snap.messages));
    setRequests(snap.requests ?? []);
    /* 补齐 profile：否则重载后 Onboarding 填好的资料（setupCompleted 等）会丢失 */
    setProfile((prev) => mergeProfilePatch(prev, snap.profile));
    setBootError("");
  }, []);

  const loadThread = useCallback(async (friendId: string) => {
    const fresh = await queryClient.fetchQuery({ queryKey: ["thread", friendId], queryFn: () => apiFetchMessages(friendId), staleTime: 10_000 });
    if (!fresh) { showToast("Could not load conversation", "error"); return; }
    /* 退出再进入时把本地乐观消息与服务端消息对齐：有同 clientId 的合并，没落库的标失败可重试 */
    setMessages((list) => reconcileThread(list, fresh, friendId));
    setThreadHasMore((state) => ({ ...state, [friendId]: fresh.length >= 30 }));
    scheduleAudioRefresh(friendId, fresh);
  }, [showToast, scheduleAudioRefresh]);

  const loadOlderThread = useCallback(async (friendId: string) => {
    const first = messages.filter((item) => item.friendId === friendId).sort((a, b) => a.time.localeCompare(b.time))[0];
    if (!first) return;
    const cursor = `${first.time}|${first.id}`;
    const fresh = await queryClient.fetchQuery({ queryKey: ["thread", friendId, cursor], queryFn: () => apiFetchMessages(friendId, cursor), staleTime: 60_000 });
    if (!fresh) { showToast("Could not load earlier messages", "error"); return; }
    setMessages((list) => { const ids = new Set(list.map((item) => item.id)); return [...fresh.filter((item) => !ids.has(item.id)), ...list]; });
    setThreadHasMore((state) => ({ ...state, [friendId]: fresh.length >= 30 }));
  }, [messages, showToast]);

  const practiceExpression = useCallback((id: string, remembered: boolean) => {
    const cur = expressions.find((e) => e.id === id);
    const nextMastery = Math.min(3, (cur?.mastery ?? 1) + (remembered ? 1 : 0));
    void apiPatchExpression(id, {
      mastery_count: nextMastery,
      status: nextMastery >= 3 ? "mastered" : "learning",
    });
    setExpressions((list) =>
      list.map((e) => {
        if (e.id !== id) return e;
        const mastery = Math.min(3, e.mastery + (remembered ? 1 : 0));
        return {
          ...e,
          mastery,
          due: Math.max(0, e.due - (remembered ? 45 : 8)),
          status: mastery >= 3 ? "mastered" : "learning",
          lastPracticed: "刚刚",
        };
      }),
    );
  }, [expressions]);

  const toggleMastered = useCallback((id: string) => {
    const cur = expressions.find((e) => e.id === id);
    const toMastered = cur?.status !== "mastered";
    setExpressions((list) =>
      list.map((e) =>
        e.id === id
          ? e.status === "mastered"
            ? { ...e, status: "learning", mastery: 1, due: 60 }
            : { ...e, status: "mastered", mastery: 3, due: 0 }
          : e,
      ),
    );
    void apiPatchExpression(id, {
      status: toMastered ? "mastered" : "learning",
      mastery_count: toMastered ? 3 : 1,
    });
  }, [expressions]);

  const removeExpression = useCallback((id: string) => {
    setExpressions((list) => list.filter((e) => e.id !== id));
    void apiDeleteExpression(id);
  }, []);

  const updateProfile = useCallback((patch: Partial<Profile>) => {
    setProfile((p) => ({ ...p, ...patch }));
  }, []);

  const deleteFriend = useCallback((friendId: string) => {
    setFriends((list) => list.filter((f) => f.id !== friendId));
    setMessages((list) => list.filter((m) => m.friendId !== friendId));
    void apiRemoveFriend(friendId);
  }, []);

  const refreshMoments = useCallback(async () => {
    const fresh = await apiLoadMoments();
    if (fresh && fresh.length) setMoments(fresh);
  }, []);

  const generateMoments = useCallback(async () => {
    showToast("正在让好友们发动态…");
    const fresh = await apiGenerateMoments();
    if (fresh && fresh.length) {
      setMoments(fresh);
      showToast("今天的朋友圈来了", "success");
    } else {
      showToast("生成失败，稍后再试", "error");
    }
  }, [showToast]);

  const value = useMemo<Store>(
    () => ({
      friends,
      requests,
      messages,
      moments,
      expressions,
      profile,
      toast,
      typing,
      booted,
      zhOpen,
      showToast,
      messagesOf,
      friendById,
      sendText,
      retryMessage,
      retryAudio,
      requestSuggestion,
      sendVoiceBlob,
      sendImage,
      markRead,
      toggleZh,
      translateMessage,
      translateAll,
      translateAllMoments,
      toggleShownText,
      saveSuggestion,
      dismissSuggestion,
      likeMoment,
      addComment,
      deleteMoment,
      publishMoment,
      refreshMoments,
      generateMoments,
      acceptRequest,
      declineRequest,
      practiceExpression,
      toggleMastered,
      removeExpression,
      updateProfile,
      deleteFriend,
      reloadAll,
      loadThread,
      loadOlderThread,
      threadHasMore,
      bootError,
    }),
    [
      friends,
      requests,
      messages,
      moments,
      expressions,
      profile,
      toast,
      typing,
      booted,
      zhOpen,
      showToast,
      messagesOf,
      friendById,
      sendText,
      retryMessage,
      retryAudio,
      requestSuggestion,
      sendVoiceBlob,
      sendImage,
      markRead,
      toggleZh,
      translateAll,
      translateAllMoments,
      toggleShownText,
      saveSuggestion,
      dismissSuggestion,
      likeMoment,
      addComment,
      publishMoment,
      refreshMoments,
      generateMoments,
      acceptRequest,
      declineRequest,
      practiceExpression,
      toggleMastered,
      removeExpression,
      updateProfile,
      deleteFriend,
      bootError,
      loadThread,
      loadOlderThread,
      threadHasMore,
    ],
  );

  const domainStore = useRef<StoreApi<Store> | null>(null);
  if (!domainStore.current) domainStore.current = createStore(() => value);
  useLayoutEffect(() => { domainStore.current!.setState(value, true); }, [value]);
  return <AppCtx.Provider value={domainStore.current}>{children}</AppCtx.Provider>;
}
