"use client";

import { useShallow } from "zustand/react/shallow";

/**
 * 单聊页。
 *
 * 三块：顶部信息条、消息流、输入区。桌面端宽屏时右侧出现资料栏，
 * 手机上资料收进顶部的「⋯」。
 *
 * 语音条、翻译、润色建议是这款产品的核心，所以它们都有真实的
 * 交互状态（播放中的波形、展开的译文、可收藏的建议卡）。
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { isNearBottom, isScrollingUp } from "../useScrollMemory";
import {
  Camera,
  Image as ImageIcon,
  Info,
  ArrowUp,
  Languages,
  Mic,
  MicOff,
  Minimize2,
  MoreHorizontal,
  Phone,
  Smile,
  Star,
  Trash2,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";

import { clockTime, dayOf } from "../format";
import "../styles/chat.css";
import type { Friend, Message, Route } from "../model";
import { API_ORIGIN } from "../api";
import { callSession } from "../v3/callSession";
import { registerOverlay } from "../navigationStore";
import {
  callOpening,
  callTurnStream,
  callWarmup,
  playVoiceUrl,
  synthesizeVoice,
  translate as apiTranslate,
  ttsSampleUrl,
  callTurnFromText,
} from "../apiFacade";
import { useApp } from "../store";
import { haptic } from "../../theme/haptics";
import { isPreviewMode } from "../previewAdapter";
import { Avatar, Confirm, Dialog, Icon, Lightbox, VoiceBar } from "../ui";
import { FriendInfo } from "./FriendInfo";

const EMOJIS = "😀 😄 😊 🙂 😅 😂 🤣 😉 😍 🤔 😮 😴 😭 😤 🙃 😇 🤝 👍 👏 🙏 💪 👌 ✌️ 🤞 👋 🫶 ❤️ 💚 💛 💔 ✨ 🔥 ☕ 🍞 🍜 🎧 📚 🌿 🌧️ 🌙 ☀️ 🚲 🏃 📷 🎬 🎂 🥂".split(
  " ",
);

const PHOTOS = [
  "/mock/photos/cafe.jpg",
  "/mock/photos/brunch.jpg",
  "/mock/photos/flowers.jpg",
  "/mock/photos/records.jpg",
  "/mock/photos/beach.jpg",
  "/mock/photos/night.jpg",
];

/** Preview fixture has no server-side TTS: use the browser voice only for review. */
function speechLangFor(friend: Friend): string {
  const code = (friend.language || "").toLowerCase();
  const map: Record<string, string> = {
    en: "en-US",
    de: "de-DE",
    fr: "fr-FR",
    es: "es-ES",
    it: "it-IT",
    pt: "pt-PT",
    ja: "ja-JP",
    ko: "ko-KR",
    zh: "zh-CN",
  };
  const labels: Record<string, string> = {
    英语: "en-US",
    德语: "de-DE",
    法语: "fr-FR",
    西语: "es-ES",
    西班牙语: "es-ES",
    意大利语: "it-IT",
    葡语: "pt-PT",
    日语: "ja-JP",
    韩语: "ko-KR",
    中文: "zh-CN",
  };
  return map[code] ?? labels[friend.language] ?? "en-US";
}


/* WebAudio 播放：荣耀/华为 WebView 对 <audio> 支持差，用 AudioContext 解码播放更稳 */
let _waCtx: AudioContext | null = null;
async function playWebAudio(url: string): Promise<boolean> {
  try {
    const res = await fetch(url);
    if (!res.ok) return false;
    const buf = await res.arrayBuffer();
    _waCtx = _waCtx ?? new AudioContext();
    if (_waCtx.state === "suspended") await _waCtx.resume();
    const audio = await _waCtx.decodeAudioData(buf);
    const src = _waCtx.createBufferSource();
    src.buffer = audio;
    src.connect(_waCtx.destination);
    src.start();
    return true;
  } catch {
    return false;
  }
}

export function ChatPane({
  friendId,
  onBack,
  navigate,
}: {
  friendId: string;
  onBack: () => void;
  navigate: (r: Route) => void;
}) {
  const loadOlderThread = useApp((state) => state.loadOlderThread);
  const hasOlder = useApp((state) => state.threadHasMore[friendId]);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const { friendById,
    loadThread,
    messagesOf,
    typing,
    markRead,
    sendText,
    retryMessage,
    sendVoiceBlob,
    sendImage,
    zhOpen,
    toggleZh,
  translateMessage,
    toggleShownText,
    saveSuggestion,
    dismissSuggestion,
    translateAll,
    showToast,
    deleteFriend,
  } = useApp(useShallow((state) => ({ friends: state.friends, friendById: state.friendById, loadThread: state.loadThread, messagesOf: state.messagesOf, typing: state.typing, markRead: state.markRead, sendText: state.sendText, retryMessage: state.retryMessage, sendVoiceBlob: state.sendVoiceBlob, sendImage: state.sendImage, zhOpen: state.zhOpen, toggleZh: state.toggleZh, translateMessage: state.translateMessage, toggleShownText: state.toggleShownText, saveSuggestion: state.saveSuggestion, dismissSuggestion: state.dismissSuggestion, translateAll: state.translateAll, showToast: state.showToast, deleteFriend: state.deleteFriend })));

  const friend = friendById(friendId);
  const msgs = messagesOf(friendId);
  const isTyping = typing[friendId];
  useEffect(() => { void loadThread(friendId); }, [friendId, loadThread]);

  useEffect(() => {
    const w = window as unknown as { w4OnCallAudio?: (s: string) => void };
    w.w4OnCallAudio = (s: string) => {
      if (s.startsWith("native-error") || s.startsWith("native-ex") || s === "bad-url") {
        showToast(`通话音频失败：${s}`, "error");
      }
    };
    return () => {
      delete w.w4OnCallAudio;
    };
  }, [showToast]);

  /* 收到好友新消息时播放提示音 */
  const [draft, setDraft] = useState("");
  const [voiceMode, setVoiceMode] = useState(false);
  const [recording, setRecording] = useState(false);
  const [recSeconds, setRecSeconds] = useState(0);
  const [cancelHint, setCancelHint] = useState(false);
  const [recZone, setRecZone] = useState<"cancel" | "transcribe" | null>(null);
  const [recMouse, setRecMouse] = useState(false);
  const [voiceTranscribe, setVoiceTranscribe] = useState(false);
  const cancelZoneRef = useRef<HTMLDivElement | null>(null);
  const transcribeZoneRef = useRef<HTMLDivElement | null>(null);
  const [photoOpen, setPhotoOpen] = useState(false);
  const [pendingImage, setPendingImage] = useState<File | null>(null);
  const [pendingUrl, setPendingUrl] = useState("");
  const [infoOpen, setInfoOpen] = useState(false);
  /* ---- 通话中的真实回合 ---- */
  const [mobileInfo, setMobileInfo] = useState(false);

  /* 系统返回键：详情浮层打开时先关闭浮层，而不是退回聊天列表 */
  useEffect(() => {
    const w = window as unknown as { w4HandleBack?: () => string };
    w.w4HandleBack = () => {
      if (infoOpen || mobileInfo) {
        setInfoOpen(false);
        setMobileInfo(false);
        return "handled";
      }
      return "unhandled";
    };
    return () => {
      delete w.w4HandleBack;
    };
  }, [infoOpen, mobileInfo]);
  const [wide, setWide] = useState(false);
  const [deskUI, setDeskUI] = useState(false);
  const [zoom, setZoom] = useState<string | null>(null);
  const [msgMenu, setMsgMenu] = useState<{
    message: Message;
    x: number;
    y: number;
  } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  /** 打开对方资料：宽屏走右侧资料栏，窄屏/手机走覆盖层 */
  const openInfo = () => {
    haptic("selection");
    setMobileInfo(!wide);
    setInfoOpen(true);
  };

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const [messageListOffset, setMessageListOffset] = useState(0);
  /* 是否跟随新消息贴底：进入会话 / 自己发消息 / 输入框聚焦时强制贴底，用户上翻后取消 */
  const stickToBottomRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const scrollRafRef = useRef<number | null>(null);
  const suppressScrollRef = useRef(0);
  const scheduleScrollRef = useRef<() => void>(() => undefined);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const recTimer = useRef<number | null>(null);
  const recStart = useRef(0);
  const recRef = useRef<MediaRecorder | null>(null);
  const recStream = useRef<MediaStream | null>(null);
  const recChunks = useRef<Blob[]>([]);
  const startY = useRef(0);
  const playingTimer = useRef<number | null>(null);
  const galleryRef = useRef<HTMLInputElement | null>(null);
  const cameraRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => photoOpen ? registerOverlay(() => setPhotoOpen(false)) : undefined, [photoOpen]);
  useEffect(() => msgMenu ? registerOverlay(() => setMsgMenu(null)) : undefined, [msgMenu]);

  const onPickImage = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    setPhotoOpen(false);
    if (!file) return;
    if (pendingUrl) URL.revokeObjectURL(pendingUrl);
    setPendingImage(file);
    setPendingUrl(URL.createObjectURL(file));
  };

  const clearPendingImage = () => {
    if (pendingUrl) URL.revokeObjectURL(pendingUrl);
    setPendingImage(null);
    setPendingUrl("");
  };

  /* 未读清零 */
  useEffect(() => {
    if (friend) markRead(friend.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [friend?.id, msgs.length]);

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1280px)");
    const update = () => setWide(mq.matches);
    update();
    mq.addEventListener("change", update);
    const mqDesk = window.matchMedia("(min-width: 900px)");
    const updateDesk = () => setDeskUI(mqDesk.matches);
    updateDesk();
    mqDesk.addEventListener("change", updateDesk);
    return () => {
      mq.removeEventListener("change", update);
      mqDesk.removeEventListener("change", updateDesk);
    };
  }, []);

  useEffect(() => {
    setInfoOpen(wide);
  }, [wide]);

  useEffect(() => {
    return () => {
      if (recTimer.current) window.clearInterval(recTimer.current);
      if (playingTimer.current) window.clearTimeout(playingTimer.current);
    };
  }, []);

  /** 播放中的消息：只允许一条同时响 */
  const [playingId, setPlayingId] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const ttsCache = useRef(new Map<string, string>());
  /* 离开聊天页暂停所有播放 / 浏览器预览朗读 */
  useEffect(
    () => () => {
      audioRef.current?.pause();
      audioRef.current = null;
      if (typeof window !== "undefined" && "speechSynthesis" in window) {
        window.speechSynthesis.cancel();
      }
      for (const url of ttsCache.current.values()) URL.revokeObjectURL(url);
      ttsCache.current.clear();
    },
    [],
  );
  const togglePlay = useCallback(
    (m: Message) => {
      if (playingId === m.id) {
        audioRef.current?.pause();
        audioRef.current = null;
        setPlayingId(null);
        return;
      }
      /* 真后端给了音频就播真的；mock 数据没有 audio，退回原来的节奏动画 */
      if (m.audio) {
        audioRef.current?.pause();
        const a = new Audio(m.audio);
        audioRef.current = a;
        a.onended = () => setPlayingId(null);
        a.onerror = () => {
          // 音频文件失效（blob 过期/服务器缺失）→ 现场合成兜底
          void (async () => {
            const synth = await synthesizeVoice(m.text, friendId);
            if (synth && !a.src.endsWith(synth)) {
              const played = await playVoiceUrl(synth);
              if (!played) setPlayingId(null);
              return;
            }
            setPlayingId(null);
          })();
        };
        void a.play().catch(() => setPlayingId(null));
        setPlayingId(m.id);
        return;
      }
      setPlayingId(m.id);
      // 一条消息没有音频文件（本地模式只存了文字）→ 现场合成一条再播
      void (async () => {
        const key = `${friendId}:${m.text}`;
        const cached = ttsCache.current.get(key);
        const synth = cached ?? (m.text ? await synthesizeVoice(m.text, friendId) : null);
        if (synth) {
          if (!cached) ttsCache.current.set(key, synth);
          audioRef.current?.pause();
          const played = await playVoiceUrl(synth);
          if (played) {
            window.setTimeout(() => setPlayingId(null), Math.min(12000, (m.durationMs ?? 4000) * 1.05));
            return;
          }
        }
        if (playingTimer.current) window.clearTimeout(playingTimer.current);
        playingTimer.current = window.setTimeout(
          () => setPlayingId(null),
          Math.min(9000, (m.durationMs ?? 3000) * 1.05),
        );
      })();
    },
    [playingId],
  );

  /**
   * AI 文本消息朗读：真实后端走 /tts/sample（friend_id 绑定角色音色）；
   * 本地预览 fixture 没有服务端 TTS，用浏览器语音做验收兜底。
   */
  const readAloud = useCallback(
    (m: Message) => {
      if (!m.text.trim()) return;
      if (isPreviewMode() && typeof window !== "undefined" && "speechSynthesis" in window) {
        if (playingId === m.id) {
          window.speechSynthesis.cancel();
          setPlayingId(null);
          return;
        }
        audioRef.current?.pause();
        audioRef.current = null;
        window.speechSynthesis.cancel();
        const utterance = new SpeechSynthesisUtterance(m.text);
        utterance.lang = friend ? speechLangFor(friend) : "en-US";
        utterance.rate = 0.96;
        let fallback = 0;
        const clear = () => {
          window.clearTimeout(fallback);
          setPlayingId((current) => (current === m.id ? null : current));
        };
        utterance.onend = clear;
        utterance.onerror = clear;
        fallback = window.setTimeout(clear, Math.min(15000, 1200 + m.text.length * 70));
        setPlayingId(m.id);
        window.speechSynthesis.speak(utterance);
        return;
      }
      togglePlay(m);
    },
    [friend, playingId, togglePlay],
  );

  const send = () => {
    const text = draft.trim();
    if (!pendingImage && !text) return;
    haptic("send");
    /* 发送后一定回到底部：刚发出的气泡要在输入框正上方可见 */
    stickToBottomRef.current = true;
    scheduleScrollRef.current();
    if (pendingImage) {
      void sendImage(friendId, pendingImage, text);
      clearPendingImage();
      setDraft("");
      inputRef.current?.focus();
      return;
    }
    sendText(friendId, text);
    setDraft("");
    inputRef.current?.focus();
  };

  /** 真录音：按住时开麦，松开把音频交给后端（STT → 回复 → TTS） */
  const startRecording = async (y: number) => {
    setRecording(true);
    setCancelHint(false);
    setRecZone(null);
    setRecSeconds(0);
    haptic("recordStart");
    recStart.current = Date.now();
    startY.current = y;
    recTimer.current = window.setInterval(() => {
      setRecSeconds(Math.floor((Date.now() - recStart.current) / 1000));
    }, 200);

    try {
      recStream.current = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";
      const rec = new MediaRecorder(recStream.current, { mimeType: mime });
      recChunks.current = [];
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size) recChunks.current.push(e.data);
      };
      rec.start(200);
      recRef.current = rec;
    } catch {
      stopRecTimer();
      setRecording(false);
      showToast("没有麦克风权限，先在浏览器里允许一下", "error");
    }
  };

  const stopRecTimer = () => {
    if (recTimer.current) window.clearInterval(recTimer.current);
    recTimer.current = null;
  };

  const finishRecording = (cancelled: boolean, transcribe = false) => {
    if (!recording) return;
    if (cancelled) haptic("recordCancel");
    setRecZone(null);
    setCancelHint(false);
    stopRecTimer();
    const ms = Date.now() - recStart.current;
    setRecording(false);
    setCancelHint(false);

    const rec = recRef.current;
    const stream = recStream.current;
    if (!rec || cancelled || ms < 600) {
      if (!cancelled && ms < 600) showToast("按住多说一会儿");
      rec?.state !== "inactive" && rec?.stop();
      stream?.getTracks().forEach((t) => t.stop());
      recRef.current = null;
      recStream.current = null;
      return;
    }

    rec.onstop = () => {
      const blob = new Blob(recChunks.current, { type: rec.mimeType || "audio/webm" });
      recChunks.current = [];
      stream?.getTracks().forEach((t) => t.stop());
      recRef.current = null;
      recStream.current = null;
      if (blob.size < 1200) {
        showToast("没录到声音，再试一次", "error");
        return;
      }
      haptic("send");
      void sendVoiceBlob(friendId, blob, ms, { openTranscript: transcribe });
    };
    rec.stop();
  };

  /** 顺序播放后端逐句下发的语音；播完自动继续听用户说 */
  const startCall = () => { if (friend) void callSession.start(friend); };

  const grouped = useMemo(() => {
    const out: Array<{ day: string; items: Message[] }> = [];
    msgs.forEach((m) => {
      const d = dayOf(m.time);
      const bucket = out[out.length - 1];
      if (bucket && bucket.day === d) bucket.items.push(m);
      else out.push({ day: d, items: [m] });
    });
    return out;
  }, [msgs]);
  const messageVirtualizer = useVirtualizer({ count: msgs.length, getScrollElement: () => scrollRef.current, estimateSize: () => 120, overscan: 5, getItemKey: (index) => msgs[index].id, scrollMargin: messageListOffset });
  useLayoutEffect(() => {
    const list = messageListRef.current;
    const scroller = scrollRef.current;
    if (!list || !scroller) return;
    const measure = () => setMessageListOffset(list.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    return () => observer.disconnect();
  }, [friendId, Boolean(friend)]);

  /* ------------------------------------------------------------ 贴底滚动 */
  /* 进入会话/切好友、首次加载完成、自己发消息、输入框聚焦、键盘弹出时都滚到最新；
     用户主动上翻后不强行贴底，除非又自己发消息。 */

  /* 好友数据还没到时会先渲染「会话不存在」占位：等真正渲染出滚动容器再挂监听/首次贴底 */
  const paneReady = Boolean(friend);
  const lastMessageId = msgs.length ? msgs[msgs.length - 1].id : "";
  const lastMessageMine = msgs.length > 0 && msgs[msgs.length - 1].from === "me";

  const scheduleScrollToBottom = useCallback(() => {
    if (scrollRafRef.current) cancelAnimationFrame(scrollRafRef.current);
    /* 程序化滚动期间把「用户上翻」判定静音一段时间，避免自己滚到底反被当成用户操作 */
    suppressScrollRef.current = performance.now() + 500;
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = null;
      const scroller = scrollRef.current;
      if (!scroller) return;
      if (msgs.length > 0) {
        try {
          /* 虚拟列表：先把最后一条滚进视口（含 scrollMargin 修正），再兜底滚到底 */
          messageVirtualizer.scrollToIndex(msgs.length - 1, { align: "end" });
        } catch {
          /* 还没测量完时忽略，下一帧兜底 */
        }
      }
      requestAnimationFrame(() => {
        const el = scrollRef.current;
        if (!el) return;
        el.scrollTop = el.scrollHeight;
        lastScrollTopRef.current = el.scrollTop;
      });
    });
  }, [messageVirtualizer, msgs.length]);

  scheduleScrollRef.current = scheduleScrollToBottom;

  /* 切换好友/进入会话：一律从最新消息开始，不沿用上一个会话的滚动位置 */
  useEffect(() => {
    stickToBottomRef.current = true;
    const frame = requestAnimationFrame(() => scheduleScrollRef.current());
    /* 等 loadThread 回来 + 图片/字体完成布局，再补一次 */
    const late = window.setTimeout(() => {
      if (stickToBottomRef.current) scheduleScrollRef.current();
    }, 240);
    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(late);
    };
  }, [friendId, paneReady]);

  /* 消息列表变化（含首次加载完成）、对方正在输入、列表偏移变化：贴底时跟随 */
  useLayoutEffect(() => {
    if (stickToBottomRef.current) scheduleScrollRef.current();
  }, [msgs.length, lastMessageId, isTyping, messageListOffset, paneReady]);

  /* 用户滚动：明显上翻就停止跟随；贴底时才继续跟随 */
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    lastScrollTopRef.current = scroller.scrollTop;
    const onScroll = () => {
      const top = scroller.scrollTop;
      const previous = lastScrollTopRef.current;
      lastScrollTopRef.current = top;
      if (isScrollingUp(previous, top)) {
        stickToBottomRef.current = false;
        return;
      }
      /* 程序化贴底滚动会经过中间位置，窗口内不据此判定用户意图 */
      if (performance.now() < suppressScrollRef.current) return;
      stickToBottomRef.current = isNearBottom(scroller);
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    /* 键盘弹出/收起（visualViewport）与 --ime-height 驱动的容器高度变化 → 贴底时重新滚到最新 */
    const observer = new ResizeObserver(() => {
      if (stickToBottomRef.current) scheduleScrollRef.current();
    });
    observer.observe(scroller);
    const viewport = window.visualViewport;
    const onViewport = () => {
      if (stickToBottomRef.current) scheduleScrollRef.current();
    };
    viewport?.addEventListener("resize", onViewport);
    viewport?.addEventListener("scroll", onViewport);
    return () => {
      scroller.removeEventListener("scroll", onScroll);
      observer.disconnect();
      viewport?.removeEventListener("resize", onViewport);
      viewport?.removeEventListener("scroll", onViewport);
    };
  }, [paneReady]);

  /* 自己发消息 → 强制贴底（即使之前翻到了上面） */
  useEffect(() => {
    if (!lastMessageMine) return;
    stickToBottomRef.current = true;
    scheduleScrollRef.current();
  }, [lastMessageId, lastMessageMine]);

  /* 聚焦输入框：键盘弹出后仍让最新消息可见 */
  const handleComposerFocus = useCallback(() => {
    stickToBottomRef.current = true;
    scheduleScrollRef.current();
    window.setTimeout(() => {
      if (stickToBottomRef.current) scheduleScrollRef.current();
    }, 260);
  }, []);

  if (!friend) {
    return (
      <div className="w4-main">
      {pendingImage ? (
        <div className="w4-attach w4-attach-fixed">
          <img src={pendingUrl} alt="" />
          <button onClick={clearPendingImage} aria-label="移除图片">×</button>
        </div>
      ) : null}

        <div className="w4-head se-pane-head">
          <button className="w4-icon-btn" onClick={onBack} aria-label="返回">
            <Icon strokeWidth={2}>
              <path d="M15 5l-7 7 7 7" />
            </Icon>
          </button>
          <div className="w4-head-main">
            <div className="w4-head-title">会话不存在</div>
          </div>
        </div>
        <div className="w4-centered">
          <div className="se-muted-text">这个好友可能已经被删除了。</div>
        </div>
      </div>
    );
  }

  return (
    <div className="w4-main">
      {/* 顶部 */}
      <div className="w4-head se-chat-head">
        <button className="w4-icon-btn w4-only-mobile" onClick={onBack} aria-label="返回">
          <Icon strokeWidth={2}>
            <path d="M15 5l-7 7 7 7" />
          </Icon>
        </button>
        <button className="w4-head-person" onClick={openInfo}>
          <Avatar friend={friend} size={40} round online={friend.online} />
          <span className="w4-head-main">
            <span className="w4-head-title">{friend.name}</span>
            <span className="w4-head-sub">
              {isTyping
                ? "正在说话…"
                : friend.online
                  ? "在线"
                  : `${friend.flag} ${friend.city} · ${friend.localTime || friend.lastActive}`}
            </span>
          </span>
        </button>
        <div className="w4-head-actions">
          <button
            className="se-head-call"
            aria-label="语音通话"
            onClick={() => void startCall()}
            title="语音通话"
          >
            <Phone size={19} strokeWidth={1.8} />
          </button>
        </div>
      </div>

      <div className="w4-chat-layout">
        <div className="w4-chat">
          <div className="w4-chat-scroll" ref={scrollRef}>
            <div className="w4-chat-inner">
              {hasOlder ? <button className="se-load-older" disabled={loadingOlder} onClick={async () => {
                const node = scrollRef.current;
                const height = node?.scrollHeight || 0;
                /* 加载更早消息时保持视野不动：上翻状态由 scroll 监听自然取消，不强制贴底 */
                stickToBottomRef.current = false;
                setLoadingOlder(true);
                await loadOlderThread(friendId);
                setLoadingOlder(false);
                requestAnimationFrame(() => { if (node) node.scrollTop += node.scrollHeight - height; });
              }}>{loadingOlder ? "加载中…" : "更早的消息"}</button> : null}
              {msgs.length === 0 && !hasOlder ? (
                <div className="se-chat-empty">
                  <Avatar friend={friend} size={64} round />
                  <div className="se-chat-empty-title">和 {friend.name} 打个招呼</div>
                  <p>从一句 hello 开始，TA 会用语音回你。</p>
                </div>
              ) : null}
              <div ref={messageListRef} style={{ position: "relative", height: messageVirtualizer.getTotalSize() }}>
              {messageVirtualizer.getVirtualItems().map((item) => {
                const m = msgs[item.index];
                return <div key={m.id} data-index={item.index} ref={messageVirtualizer.measureElement} style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${item.start - messageVirtualizer.options.scrollMargin}px)` }}>
                  {item.index === 0 || dayOf(msgs[item.index - 1].time) !== dayOf(m.time) ? <div className="w4-day"><span>{dayOf(m.time)}</span></div> : null}
                    <MessageRow
                      key={m.id}
                      message={m}
                      friend={friend}
                      zhShown={!!zhOpen[m.id]}
                      playing={playingId === m.id}
                      onPlay={() => togglePlay(m)}
                      onRead={() => readAloud(m)}
                      onTranslateLongPress={() => {
                        haptic("selection");
                        void translateMessage(m.id);
                      }}
                      onToggleZh={() => void translateMessage(m.id)}
                      onToggleText={() => toggleShownText(m.id)}
                      onSave={() => {
                        haptic("save");
                        saveSuggestion(m.id);
                      }}
                      onDismiss={() => dismissSuggestion(m.id)}
                      onRetry={() => retryMessage(m.id)}
                      onZoom={setZoom}
                      onLongPress={(pos) =>
                        setMsgMenu({ message: m, x: pos.x, y: pos.y })
                      }
                      onAvatarClick={openInfo}
                    />
                </div>;
              })}
              </div>

              {isTyping ? (
                <div className="w4-msg">
                  <div className="w4-msg-col">
                    <div className="w4-bubble is-typing">
                      <span className="w4-typing">
                        <i />
                        <i />
                        <i />
                      </span>
                    </div>
                  </div>
                </div>
              ) : null}
              <div style={{ height: 8 }} />
            </div>
          </div>

          {/* 输入区 */}
          <div className="w4-composer">
      {pendingImage ? (
        <div className="w4-attach">
          <img src={pendingUrl} alt="" />
          <button onClick={clearPendingImage} aria-label="移除图片">×</button>
        </div>
      ) : null}

            {deskUI ? (
              /* 电脑：上面输入、下面工具条；语音输入 = 长按鼠标 */
              <div className={`w4-composer-inner w4-cd${recording ? " is-recording" : ""}`}>
                <div
                  className={`w4-cd-input${voiceMode ? " is-voice" : ""}${recording ? " is-rec" : ""}`}
                >
                  <textarea
                    ref={inputRef}
                    className="w4-composer-input"
                    rows={1}
                    value={draft}
                    placeholder={`跟 ${friend.name} 说点什么`}
                    onFocus={handleComposerFocus}
                    onChange={(e) => {
                      setDraft(e.target.value);
                      const el = e.target;
                      el.style.height = "auto";
                      el.style.height = `${Math.min(132, el.scrollHeight)}px`;
                    }}
                    onKeyDown={(e) => {
                      /* 输入法组词中的回车 / 按住回车的 repeat 不发送 */
                      if (e.key === "Enter" && !e.shiftKey && !e.repeat && !e.nativeEvent.isComposing) {
                        e.preventDefault();
                        send();
                      }
                    }}
                  />
                  {voiceMode && !recording ? (
                    <span className="w4-cd-voice-hint">长按鼠标 语音输入</span>
                  ) : null}
                </div>
                <div className="w4-cd-bar">
                  <div className="w4-cd-icons">
                    <button
                      className={`w4-icon-btn ${photoOpen ? "is-active" : ""}`}
                      onClick={() => {
                        setPhotoOpen((v) => !v);
                      }}
                      aria-label="图片"
                    >
                      <Camera size={18} strokeWidth={1.7} />
                    </button>
                  </div>
                  <div className="w4-cd-right">
                    {recording ? (
                      <>
                        <button
                          className="w4-cd-rec-x"
                          onClick={() => finishRecording(true)}
                          aria-label="取消录音"
                        >
                          <X size={16} strokeWidth={2.1} />
                        </button>
                        <div className="w4-cd-rec-pill">
                          <button
                            className="w4-cd-rec-text"
                            onClick={() => finishRecording(false, true)}
                            aria-label="转文字发送"
                          >
                            <Languages size={15} strokeWidth={1.9} />
                            <span>转文字</span>
                          </button>
                          <span className="w4-cd-rec-dots" aria-hidden>
                            <i /><i /><i /><i /><i /><i />
                          </span>
                          <span className="w4-cd-rec-time">{recSeconds}"</span>
                          <button
                            className="w4-cd-rec-send"
                            onClick={() => finishRecording(false, false)}
                            aria-label="发送录音"
                          >
                            <ArrowUp size={18} strokeWidth={2.6} />
                          </button>
                        </div>
                      </>
                    ) : (
                      <>
                        <button
                          className={`w4-cd-voice-btn${recording ? " is-on" : ""}`}
                          onClick={() => {
                            if (recording) return;
                            setRecMouse(true);
                            void startRecording(0);
                          }}
                          aria-label="语音输入"
                          title="点击开始录音"
                        >
                          <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                            <circle cx="12" cy="12" r="9" />
                            <path d="M9 12h.01" />
                            <path d="M12.5 9.5v5" />
                            <path d="M16 10.5v3" />
                          </svg>
                        </button>
                        <button
                          className="w4-send-btn"
                          disabled={!draft.trim() && !pendingImage}
                          onClick={send}
                          aria-label="发送"
                          title="发送"
                        >
                          <ArrowUp size={20} strokeWidth={2.4} />
                        </button>
                      </>
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <div className="w4-composer-inner is-mobile">
                <div className="w4-composer-actions">
                  <button
                    className={`w4-icon-btn ${voiceMode ? "is-active" : ""}`}
                    onClick={() => setVoiceMode((v) => !v)}
                    aria-label={voiceMode ? "切换到打字" : "切换到语音"}
                    title={voiceMode ? "打字" : "语音"}
                  >
                    {voiceMode ? (
                      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                        <rect x="3" y="6" width="18" height="12" rx="2" />
                        <path d="M7 10h.01M11 10h.01M15 10h.01M7 14h10" strokeLinecap="round" />
                      </svg>
                    ) : (
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round">
                        <circle cx="12" cy="12" r="9" />
                        <path d="M9 12h.01" />
                        <path d="M12.5 9.5v5" />
                        <path d="M16 10.5v3" />
                      </svg>
                    )}
                  </button>
                </div>

                {voiceMode ? (
                  <button
                    className={`w4-hold ${recording ? "is-rec" : ""} ${cancelHint ? "is-cancel" : ""}`}
                    onPointerDown={(e) => {
                      e.preventDefault();
                      setRecMouse(false);
                      try {
                        (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
                      } catch {
                        /* 合成事件可能没有真实 pointer，忽略 */
                      }
                      void startRecording(e.clientY);
                    }}
                    onPointerMove={(e) => {
                      if (!recording || recMouse) return;
                      const inside = (el: HTMLDivElement | null) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const pad = 12;
                        return (
                          e.clientX >= r.left - pad &&
                          e.clientX <= r.right + pad &&
                          e.clientY >= r.top - pad &&
                          e.clientY <= r.bottom + pad
                        );
                      };
                      let zone: "cancel" | "transcribe" | null = null;
                      if (inside(transcribeZoneRef.current)) zone = "transcribe";
                      else if (inside(cancelZoneRef.current)) zone = "cancel";
                      else if (startY.current - e.clientY > 90) zone = "cancel";
                      setRecZone(zone);
                      setCancelHint(zone === "cancel");
                    }}
                    onPointerUp={() => {
                      if (recMouse) return;
                      finishRecording(recZone === "cancel", recZone === "transcribe");
                    }}
                    onPointerCancel={() => finishRecording(true)}
                  >
                    {recording
                      ? recMouse
                        ? "录制中…"
                        : recZone === "cancel"
                          ? "松开取消"
                          : recZone === "transcribe"
                            ? "松开转文字"
                            : "松开发送"
                      : "按住 说话"}
                  </button>
                ) : (
                  <div className="w4-composer-field">
                    <textarea
                      ref={inputRef}
                      className="w4-composer-input"
                      rows={1}
                      value={draft}
                      placeholder={`跟 ${friend.name} 说点什么`}
                      onFocus={handleComposerFocus}
                      onChange={(e) => {
                        setDraft(e.target.value);
                        const el = e.target;
                        el.style.height = "auto";
                        el.style.height = `${Math.min(132, el.scrollHeight)}px`;
                      }}
                      onKeyDown={(e) => {
                        /* 输入法组词中的回车 / 按住回车的 repeat 不发送 */
                        if (e.key === "Enter" && !e.shiftKey && !e.repeat && !e.nativeEvent.isComposing) {
                          e.preventDefault();
                          send();
                        }
                      }}
                    />
                    <button
                      className="w4-send-btn"
                      disabled={!draft.trim() && !pendingImage}
                      onClick={send}
                      aria-label="发送"
                      title="发送"
                    >
                      <ArrowUp size={20} strokeWidth={2.4} />
                    </button>
                  </div>
                )}

                <div className="w4-composer-actions">
                  <button
                    className={`w4-icon-btn ${photoOpen ? "is-active" : ""}`}
                    onClick={() => {
                      setPhotoOpen((v) => !v);
                    }}
                    aria-label="图片"
                  >
                    <Camera size={18} strokeWidth={1.7} />
                  </button>
                </div>
              </div>
            )}
          </div>
          {photoOpen ? (
            <div className="w4-pop w4-photo-sheet">
              <button onClick={() => galleryRef.current?.click()}>从相册选择</button>
              <button onClick={() => cameraRef.current?.click()}>拍照</button>
              <input ref={galleryRef} type="file" accept="image/*" hidden onChange={onPickImage} />
              <input ref={cameraRef} type="file" accept="image/*" capture="environment" hidden onChange={onPickImage} />
            </div>
          ) : null}

          {recording && !recMouse ? (
            <div className={`w4-rec-overlay${cancelHint ? " is-cancel" : ""}`}>
              <div className="w4-rec-bars" aria-hidden>
                {Array.from({ length: 9 }).map((_, i) => (
                  <i
                    key={i}
                    style={{
                      height: 10 + ((i * 7) % 20),
                      animationDelay: `${i * 0.08}s`,
                    }}
                  />
                ))}
              </div>
              <div className="w4-rec-time">
                {cancelHint ? "松开取消" : `0:${String(recSeconds).padStart(2, "0")}`}
              </div>
              <div className="w4-rec-hint">
                {recZone === "cancel"
                  ? "松开取消"
                  : recZone === "transcribe"
                    ? "松开转文字"
                    : recMouse
                      ? "松开鼠标发送"
                      : "上滑取消 · 右滑转文字"}
              </div>
            </div>
          ) : null}

          {recording && !recMouse ? <div className="w4-rec-dim" /> : null}

          {recording && !recMouse ? (
            <div className="w4-rec-zones">
              <div
                ref={cancelZoneRef}
                className={`w4-rec-zone is-cancel${recZone === "cancel" ? " on" : ""}`}
              >
                <X size={18} strokeWidth={2.2} />
                取消发送
              </div>
              <div
                ref={transcribeZoneRef}
                className={`w4-rec-zone is-text${recZone === "transcribe" ? " on" : ""}`}
              >
                <Languages size={18} strokeWidth={2.1} />
                转文字
              </div>
            </div>
          ) : null}

        </div>

        {infoOpen ? (
          <aside className={`w4-info ${mobileInfo ? "is-mobile" : ""}`}>
            <div className="w4-info-head">
              对方资料
              <button className="w4-icon-btn" onClick={() => setInfoOpen(false)} aria-label="收起资料">
                <X size={16} strokeWidth={2} />
              </button>
            </div>
            <div className="w4-info-scroll">
              <FriendInfo friend={friend} />
              <div className="w4-info-actions">
                <button
                  className="w4-info-btn"
                  onClick={() => {
                    navigate({ name: "contact", friendId: friend.id });
                  }}
                >
                  查看完整资料
                </button>
                <button className="w4-info-btn" onClick={startCall}>
                  <Phone size={13} />
                  打电话
                </button>
              </div>
              <div className="w4-info-section">
                <div className="w4-info-label">TA 的声音</div>
                <div className="w4-voice-readonly">
                  <span className="w4-voice-readonly-icon">
                    <Mic size={14} />
                  </span>
                  <span className="se-voice-main">
                    <span className="se-voice-name">{friend.voiceLabel}</span>
                    <span className="se-voice-note">每个角色一个声音 · 系统自动分配</span>
                  </span>
                </div>
              </div>
              <div className="w4-info-section">
                <button
                  className="w4-info-btn is-danger is-block"
                  onClick={() => setConfirmDelete(true)}
                >
                  <Trash2 size={13} />
                  删除好友
                </button>
              </div>
            </div>
          </aside>
        ) : null}
      </div>

      {msgMenu ? (
        <>
          {/* 透明遮罩：点别处关掉菜单（不做全屏变暗，跟微信一样） */}
          <div className="w4-ctx-backdrop" onClick={() => setMsgMenu(null)} />
          <div
            className="w4-ctx-menu"
            style={(() => {
              const W = 132;
              const H = 90;
              const pad = 8;
              const vw = typeof window === "undefined" ? 1024 : window.innerWidth;
              const vh = typeof window === "undefined" ? 768 : window.innerHeight;
              /* 从指针位置展开；贴边时翻到另一侧，保证不出屏 */
              const clampX = (v: number) => Math.max(pad, Math.min(v, vw - W - pad));
              const clampY = (v: number) => Math.max(pad, Math.min(v, vh - H - pad));
              const x = clampX(msgMenu.x + 6);
              /* 下方放不下就翻到指针上方 */
              const y = msgMenu.y + H + 12 > vh ? clampY(msgMenu.y - H - 6) : clampY(msgMenu.y + 8);
              return { left: x, top: y, width: W };
            })()}
          >
            <button
              className="w4-ctx-item"
              onClick={() => {
                toggleShownText(msgMenu.message.id);
                setMsgMenu(null);
              }}
            >
              {msgMenu.message.shownText ? "取消转文字" : "转文字"}
            </button>
            <button
              className="w4-ctx-item"
              onClick={() => {
                void translateMessage(msgMenu.message.id);
                setMsgMenu(null);
              }}
            >
              {msgMenu.message.translation && zhOpen[msgMenu.message.id] ? "收起翻译" : "翻译"}
            </button>
          </div>
        </>
      ) : null}

      {zoom ? <Lightbox src={zoom} onClose={() => setZoom(null)} /> : null}

      {confirmDelete ? (
        <Confirm
          title="删除好友"
          text={
            <>
              删除后，你和 {friend.name} 的聊天记录、TA 记住的关于你的事都会一起删掉，
              <br />
              无法恢复。
            </>
          }
          confirmText="删除"
          danger
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() => {
            deleteFriend(friend.id);
            setConfirmDelete(false);
            onBack();
          }}
        />
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------- 消息行 */

function MessageRow({
  message,
  friend,
  zhShown,
  playing,
  onPlay,
  onRead,
  onTranslateLongPress,
  onToggleZh,
  onToggleText,
  onSave,
  onDismiss,
  onRetry,
  onZoom,
  onLongPress,
  onAvatarClick,
}: {
  message: Message;
  friend: Friend;
  zhShown: boolean;
  playing: boolean;
  onPlay: () => void;
  onRead: () => void;
  onTranslateLongPress: () => void;
  onToggleZh: () => void;
  onToggleText: () => void;
  onSave: () => void;
  onDismiss: () => void;
  onRetry: () => void;
  onZoom: (src: string) => void;
  onLongPress?: (pos: { x: number; y: number }) => void;
  onAvatarClick?: () => void;
}) {
  const { profile } = useApp(useShallow((state) => ({ profile: state.profile })));
  const mine = message.from === "me";

  /* 长按/右键语音消息 → 在指针位置弹出「转文字」菜单 */
  const pressTimer = useRef<number | null>(null);
  const pressPos = useRef({ x: 0, y: 0 });
  const longPressed = useRef(false);

  const cancelPress = () => {
    if (pressTimer.current) {
      window.clearTimeout(pressTimer.current);
      pressTimer.current = null;
    }
  };

  /* 长按触发后，手指抬起会补发一次 click —— 它落在刚出现的菜单遮罩上会把菜单立刻关掉，
     所以在 touchend 上 preventDefault，把这次 click 掐掉 */
  const endPress = (e: React.TouchEvent) => {
    cancelPress();
    if (longPressed.current) e.preventDefault();
  };

  const startPress = (e: React.TouchEvent, onHold: (pos: { x: number; y: number }) => void) => {
    longPressed.current = false;
    cancelPress();
    const t = e.touches?.[0];
    pressPos.current = { x: t ? t.clientX : 0, y: t ? t.clientY : 0 };
    pressTimer.current = window.setTimeout(() => {
      longPressed.current = true;
      onHold(pressPos.current);
    }, 420);
  };

  /* 长按之后浏览器还会补一次 click，这里把它吃掉，避免顺手播放语音 */
  const handlePlay = () => {
    if (longPressed.current) {
      longPressed.current = false;
      return;
    }
    onPlay();
  };

  if (message.from === "system") {
    return (
      <div className="w4-system">
        <span>{message.text}</span>
      </div>
    );
  }

  const isVoice = message.kind === "voice";
  const isImage = message.kind === "image";
  const readable = !mine && !isVoice && !isImage && message.text.trim().length > 0;

  const handleBubbleClick = () => {
    if (longPressed.current) {
      longPressed.current = false;
      return;
    }
    const selection = typeof window === "undefined" ? "" : window.getSelection()?.toString() ?? "";
    if (selection.trim()) return;
    if (playing) {
      onRead();
      return;
    }
    haptic("selection");
    onRead();
  };

  const handleBubbleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    handleBubbleClick();
  };

  return (
    <div className={`w4-msg ${mine ? "is-me" : ""}`}>
      {mine ? null : (
        <button className="w4-msg-avatar" onClick={onAvatarClick} aria-label="查看资料">
          <Avatar friend={friend} size={36} />
        </button>
      )}
      <div className="w4-msg-col">
        <div className="w4-bubble-row">
          <div className="w4-bubble-stack">
          <div
            className={`w4-bubble ${isImage ? "is-image" : ""} ${isVoice ? "is-voice" : ""} ${message.failed ? "is-failed" : ""} ${readable ? "is-readable" : ""} ${readable && playing ? "is-reading" : ""}`}
            title={readable ? "单击朗读 · 长按或右键翻译" : clockTime(message.time)}
            role={readable ? "button" : undefined}
            tabIndex={readable ? 0 : undefined}
            aria-label={readable ? `朗读：${message.text}` : undefined}
            onClick={readable ? handleBubbleClick : undefined}
            onKeyDown={readable ? handleBubbleKeyDown : undefined}
            onTouchStart={
              isVoice
                ? (e) => startPress(e, (pos) => onLongPress?.(pos))
                : readable
                  ? (e) => startPress(e, () => onTranslateLongPress())
                  : undefined
            }
            onTouchEnd={isVoice || readable ? endPress : undefined}
            onTouchMove={isVoice || readable ? cancelPress : undefined}
            onTouchCancel={isVoice || readable ? cancelPress : undefined}
            onContextMenu={
              isVoice
                ? (e) => {
                    e.preventDefault();
                    onLongPress?.({ x: e.clientX, y: e.clientY });
                  }
                : readable
                  ? (e) => {
                      e.preventDefault();
                      haptic("selection");
                      onTranslateLongPress();
                    }
                  : undefined
            }
          >
          {isVoice ? (
            <>
              <VoiceBar
                id={message.id}
                durationMs={message.durationMs ?? 3000}
                playing={playing}
                onToggle={handlePlay}
              />
              {message.shownText ? (
                <div className="w4-voice-text">{message.text}</div>
              ) : null}
            </>
          ) : isImage ? (
            <>
              {message.image ? (
                <img
                  src={message.image}
                  alt=""
                  onClick={() => message.image && onZoom(message.image)}
                />
              ) : null}
              {message.text ? <div className="w4-img-caption">{message.text}</div> : null}
            </>
          ) : (
            message.text
          )}
          </div>

          {/* 译文（消息下方，跟原文区分开） */}
          {zhShown && message.translation ? (
            <div className="w4-translation">{message.translation}</div>
          ) : null}
          </div>

          {/* 语音消息的两个操作放气泡右侧（自己的消息由 CSS 翻到左侧） */}
          {isVoice ? (
            <div className="w4-bubble-side">
              <button className="w4-mini-btn" onClick={onToggleText}>
                {message.shownText ? "取消转文字" : "转文字"}
              </button>

            </div>
          ) : null}
        </div>

        {/* 润色建议 */}
        {message.suggestion ? (
          <SuggestionCard
            message={message}
            onSave={onSave}
            onDismiss={onDismiss}
          />
        ) : null}

        {/* 操作条 */}
        <div className="w4-bubble-tools">
          {readable ? (
            <span className={`w4-read-cue ${playing ? "on" : ""}`} aria-hidden>
              <Volume2 size={12} strokeWidth={2} />
              {playing ? "朗读中" : "点按朗读"}
            </span>
          ) : null}
          {/* 语音的「转文字 / 翻译」已经在气泡右侧，这里不再重复 */}
          {!isVoice && message.translation ? (
            <button className="w4-mini-btn" onClick={onToggleZh}>
              {zhShown ? "收起翻译" : "翻译"}
            </button>
          ) : null}
          {mine && message.failed ? (
            <button className="w4-mini-btn is-retry" onClick={onRetry} title="重新发送（不会重复）">
              重试
            </button>
          ) : null}
          <span className={`w4-msg-meta${mine && message.failed ? " is-failed" : ""}`}>
            {clockTime(message.time)}
            {mine ? (message.failed ? " · 发送失败" : message.read ? " · 已读" : " · 发送中") : ""}
          </span>
        </div>
      </div>
    </div>
  );
}

function SuggestionCard({
  message,
  onSave,
  onDismiss,
}: {
  message: Message;
  onSave: () => void;
  onDismiss: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const s = message.suggestion!;
  return (
    <div className="w4-tip">
      <div className="w4-tip-head">
        <Star size={12} strokeWidth={2} fill="currentColor" />
        更地道一点
        <span className="w4-tip-tag">{s.tag}</span>
        <button
          className="w4-tip-close"
          onClick={onDismiss}
          aria-label="忽略"
        >
          <X size={13} strokeWidth={2} />
        </button>
      </div>
      <div className="w4-tip-better">{s.better}</div>
      {expanded ? <div className="w4-tip-more">{s.why}</div> : null}
      <div className="w4-tip-actions">
        <button
          className="w4-tip-btn is-primary"
          onClick={onSave}
          disabled={s.saved}
        >
          {s.saved ? (
            <>
              <Icon size={11} strokeWidth={2.6}>
                <path d="M20 6L9 17l-5-5" />
              </Icon>
              已收藏
            </>
          ) : (
            <>
              <Star size={11} strokeWidth={2.2} />
              收进表达本
            </>
          )}
        </button>
        <button className="w4-tip-btn" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "收起解释" : "为什么"}
        </button>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- 通话 */
