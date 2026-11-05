"use client";

/**
 * 单聊页。
 *
 * 三块：顶部信息条、消息流、输入区。桌面端宽屏时右侧出现资料栏，
 * 手机上资料收进顶部的「⋯」。
 *
 * 语音条、翻译、润色建议是这款产品的核心，所以它们都有真实的
 * 交互状态（播放中的波形、展开的译文、可收藏的建议卡）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
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
import type { Friend, Message, Route } from "../model";
import {
  callTurnStream,
  callWarmup,
  changeFriendVoice,
  loadArchetypes,
  translate as apiTranslate,
  type ArchetypeLite,
} from "../api";
import { useApp } from "../store";
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

export function ChatPane({
  friendId,
  onBack,
  navigate,
}: {
  friendId: string;
  onBack: () => void;
  navigate: (r: Route) => void;
}) {
  const {
    friendById,
    messagesOf,
    typing,
    markRead,
    sendText,
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
    settings,
    updateFriendVoice,
    deleteFriend,
    callSummary,
  } = useApp();

  const friend = friendById(friendId);
  const msgs = messagesOf(friendId);
  const isTyping = typing[friendId];

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
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [photoOpen, setPhotoOpen] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [voiceOpen, setVoiceOpen] = useState(false);
  const [voiceList, setVoiceList] = useState<ArchetypeLite[]>([]);
  const [voiceBusy, setVoiceBusy] = useState(false);
  /* ---- 通话中的真实回合 ---- */
  const [callTurns, setCallTurns] = useState(0);
  const [callUserText, setCallUserText] = useState("");
  const [callFriendText, setCallFriendText] = useState("");
  const [callTalking, setCallTalking] = useState(false);
  const [callWaiting, setCallWaiting] = useState(false);
  const callRec = useRef<MediaRecorder | null>(null);
  const callStreamRef = useRef<MediaStream | null>(null);
  const callChunks = useRef<Blob[]>([]);
  const callStartAt = useRef(0);
  const callAudioQueue = useRef<string[]>([]);
  const callAudioPlaying = useRef(false);
  const [speakerOn, setSpeakerOn] = useState(true);
  const speakerOnRef = useRef(true);
  const currentCallAudio = useRef<HTMLAudioElement | null>(null);

  /* ---- 免提自动断句（VAD）---- */
  const vadRef = useRef<{
    stream: MediaStream;
    ctx: AudioContext;
    analyser: AnalyserNode;
    timer: number | null;
    rec: MediaRecorder | null;
    chunks: Blob[];
    startedAt: number;
    silenceSince: number;
  } | null>(null);
  const callListeningRef = useRef(false);
  const callMutedRef = useRef(false);
  const [callListening, setCallListening] = useState(false);
  const [callMuted, setCallMuted] = useState(false);
  const [callFriendSpeaking, setCallFriendSpeaking] = useState(false);
  const [callUserZh, setCallUserZh] = useState("");
  const [callFriendZh, setCallFriendZh] = useState("");

  /* 关掉声音时立刻停下正在播的那句，而不是等它播完 */
  useEffect(() => {
    speakerOnRef.current = speakerOn;
    if (!speakerOn) currentCallAudio.current?.pause();
  }, [speakerOn]);
  const [mobileInfo, setMobileInfo] = useState(false);
  const [wide, setWide] = useState(false);
  const [deskUI, setDeskUI] = useState(false);
  const [zoom, setZoom] = useState<string | null>(null);
  const [msgMenu, setMsgMenu] = useState<{
    message: Message;
    x: number;
    y: number;
  } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [calling, setCalling] = useState(false);
  const [callSeconds, setCallSeconds] = useState(0);
  const [callPhase, setCallPhase] = useState<"dialing" | "live">("dialing");

  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const recTimer = useRef<number | null>(null);
  const recStart = useRef(0);
  const recRef = useRef<MediaRecorder | null>(null);
  const recStream = useRef<MediaStream | null>(null);
  const recChunks = useRef<Blob[]>([]);
  const startY = useRef(0);
  const playingTimer = useRef<number | null>(null);

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
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs.length, isTyping, friendId]);

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
        a.onerror = () => setPlayingId(null);
        void a.play().catch(() => setPlayingId(null));
        setPlayingId(m.id);
        return;
      }
      setPlayingId(m.id);
      if (playingTimer.current) window.clearTimeout(playingTimer.current);
      playingTimer.current = window.setTimeout(
        () => setPlayingId(null),
        Math.min(9000, (m.durationMs ?? 3000) * 1.05),
      );
    },
    [playingId],
  );

  const send = () => {
    const text = draft.trim();
    if (!text) return;
    sendText(friendId, text);
    setDraft("");
    setEmojiOpen(false);
    inputRef.current?.focus();
  };

  /** 真录音：按住时开麦，松开把音频交给后端（STT → 回复 → TTS） */
  const startRecording = async (y: number) => {
    setRecording(true);
    setCancelHint(false);
    setRecZone(null);
    setRecSeconds(0);
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
      void sendVoiceBlob(friendId, blob, ms, { openTranscript: transcribe });
    };
    rec.stop();
  };

  const openVoicePicker = async () => {
    if (!friend) return;
    setVoiceOpen(true);
    const all = await loadArchetypes();
    const lang = (friend.language || "en").toLowerCase();
    const same = all.filter((a) => !a.native_lang || a.native_lang === lang);
    /* 同语言的口音优先；没有就退回全部（后端会校验） */
    setVoiceList(same.length ? same : all);
  };

  const pickVoice = async (a: ArchetypeLite) => {
    if (!friend) return;
    setVoiceBusy(true);
    const ok = await changeFriendVoice(friend.id, a.archetype_id);
    setVoiceBusy(false);
    if (!ok) {
      showToast("换口音失败，稍后再试", "error");
      return;
    }
    updateFriendVoice(friend.id, a.voice_id, a.accent_label || a.display_name);
    setVoiceOpen(false);
    showToast(`已换成 ${a.accent_label || a.display_name}`, "success");
  };

  /** 顺序播放后端逐句下发的语音；播完自动继续听用户说 */
  const pumpCallAudio = async () => {
    if (callAudioPlaying.current) return;
    callAudioPlaying.current = true;
    while (callAudioQueue.current.length) {
      const url = callAudioQueue.current.shift();
      if (!url) break;
      await new Promise<void>((resolve) => {
        const audio = new Audio(url);
        audio.muted = !speakerOnRef.current;
        currentCallAudio.current = audio;
        let settled = false;
        let timer = 0;
        const finish = () => {
          if (settled) return;
          settled = true;
          if (timer) window.clearTimeout(timer);
          resolve();
        };
        audio.onended = finish;
        audio.onerror = finish;
        audio.addEventListener("loadedmetadata", () => {
          const d = Number.isFinite(audio.duration) ? audio.duration : 0;
          if (d > 0) {
            if (timer) window.clearTimeout(timer);
            timer = window.setTimeout(finish, d * 1000 + 2000);
          }
        });
        /* 兜底：避免某些环境 ended 永远不触发，通话卡在“正在说” */
        timer = window.setTimeout(finish, 20000);
        void audio.play().catch(finish);
      });
    }
    callAudioPlaying.current = false;
    setCallFriendSpeaking(false);
    window.setTimeout(() => resumeCallListening(), 350);
  };

  /** 一段话录完：发给后端，流式拿回逐句语音 */
  const sendCallBlob = async (blob: Blob, ms: number) => {
    if (!friend) return;
    setCallWaiting(true);
    setCallUserText("");
    setCallFriendText("");
    setCallUserZh("");
    setCallFriendZh("");
    const ok = await callTurnStream(friend.id, blob, ms, (ev) => {
      if (ev.type === "transcript") {
        setCallUserText(ev.text);
        if (ev.text) {
          void apiTranslate(ev.text, friend.id).then((zh) => {
            if (zh) setCallUserZh(zh);
          });
        }
      } else if (ev.type === "sentence") {
        setCallFriendText((t) => (t ? t + " " : "") + ev.text);
        void apiTranslate(ev.text, friend.id).then((zh) => {
          if (zh) setCallFriendZh((t) => (t ? t + " " : "") + zh);
        });
        setCallFriendSpeaking(true);
        if (ev.audioUrl) {
          callAudioQueue.current.push(ev.audioUrl);
          void pumpCallAudio();
        }
      } else if (ev.type === "done") {
        setCallWaiting(false);
        setCallTurns((n) => n + 1);
        window.setTimeout(() => {
          if (callAudioPlaying.current || callAudioQueue.current.length) return;
          setCallFriendSpeaking(false);
          resumeCallListening();
        }, 1200);
      } else if (ev.type === "error") {
        setCallWaiting(false);
        showToast(ev.message || "这一轮没接上", "error");
      }
    });
    if (!ok) {
      setCallWaiting(false);
      showToast("通话连接失败", "error");
    }
    if (!callAudioQueue.current.length && !callAudioPlaying.current) {
      setCallWaiting(false);
      window.setTimeout(() => resumeCallListening(), 350);
    }
  };

  /** VAD 主循环：说话开始录音，停顿 ~0.85s 自动发送 */
  function vadTick() {
    const v = vadRef.current;
    if (!v) return;
    const buf: Float32Array<ArrayBuffer> = new Float32Array(
      new ArrayBuffer(v.analyser.fftSize * Float32Array.BYTES_PER_ELEMENT),
    );
    v.analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let k = 0; k < buf.length; k += 1) sum += buf[k] * buf[k];
    const rms = Math.sqrt(sum / buf.length);
    const now = Date.now();
    const speaking = rms > 0.02;

    if (!v.rec && speaking) {
      v.chunks = [];
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";
      const rec = new MediaRecorder(v.stream, { mimeType: mime });
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size) v.chunks.push(e.data);
      };
      v.startedAt = now;
      v.silenceSince = 0;
      v.rec = rec;
      rec.onstop = () => {
        if (!vadRef.current) return;
        const ms = Date.now() - v.startedAt;
        const blob = new Blob(v.chunks, { type: rec.mimeType || "audio/webm" });
        v.chunks = [];
        v.rec = null;
        setCallTalking(false);
        if (blob.size >= 1200) void sendCallBlob(blob, ms);
        else window.setTimeout(() => vadTick(), 200);
      };
      rec.start(150);
      setCallTalking(true);
    } else if (v.rec) {
      if (speaking) {
        v.silenceSince = 0;
      } else if (!v.silenceSince) {
        v.silenceSince = now;
      }
      const silentFor = v.silenceSince ? now - v.silenceSince : 0;
      const total = now - v.startedAt;
      if (silentFor >= 850 || total >= 15000) {
        const rec = v.rec;
        v.rec = null;
        try {
          if (rec && rec.state !== "inactive") rec.stop();
        } catch {
          /* ignore */
        }
        return;
      }
    }
    v.timer = window.setTimeout(vadTick, 100);
  }

  /** 恢复“继续听”：只在没静音、没在等回复、对方也没在说话时 */
  const resumeCallListening = () => {
    const v = vadRef.current;
    if (!v || v.rec) return;
    if (callMutedRef.current || !callListeningRef.current) return;
    if (callWaiting) return;
    if (callAudioPlaying.current) {
      if (callAudioQueue.current.length || currentCallAudio.current) return;
      callAudioPlaying.current = false;
      setCallFriendSpeaking(false);
    }
    if (callFriendSpeaking) setCallFriendSpeaking(false);
    if (v.timer) window.clearTimeout(v.timer);
    v.timer = window.setTimeout(vadTick, 120);
  };

  const startVad = async () => {
    if (vadRef.current || callMutedRef.current) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const ctx = new AudioContext();
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      vadRef.current = {
        stream,
        ctx,
        analyser,
        timer: null,
        rec: null,
        chunks: [],
        startedAt: 0,
        silenceSince: 0,
      };
      callListeningRef.current = true;
      setCallListening(true);
      resumeCallListening();
    } catch {
      showToast("没有麦克风权限，先在浏览器里允许一下", "error");
    }
  };

  const stopVad = () => {
    const v = vadRef.current;
    vadRef.current = null;
    callListeningRef.current = false;
    setCallListening(false);
    setCallTalking(false);
    setCallFriendSpeaking(false);
    if (!v) return;
    if (v.timer) window.clearTimeout(v.timer);
    try {
      if (v.rec && v.rec.state !== "inactive") v.rec.stop();
    } catch {
      /* ignore */
    }
    v.stream.getTracks().forEach((t) => t.stop());
    void v.ctx.close().catch(() => {});
  };

  const toggleCallMute = () => {
    const next = !callMutedRef.current;
    callMutedRef.current = next;
    setCallMuted(next);
    const v = vadRef.current;
    if (next) {
      if (v?.timer) window.clearTimeout(v.timer);
      if (v) {
        try {
          if (v.rec && v.rec.state !== "inactive") v.rec.stop();
        } catch {
          /* ignore */
        }
        v.rec = null;
      }
      setCallTalking(false);
    } else {
      resumeCallListening();
      if (!vadRef.current) void startVad();
    }
  };

  const startCall = async () => {
    setCalling(true);
    setCallTurns(0);
    setCallUserText("");
    setCallFriendText("");
    setCallFriendSpeaking(false);
    callMutedRef.current = false;
    setCallMuted(false);
    if (friend) await callWarmup(friend.id);
  };

  const endCall = (seconds: number) => {
    stopVad();
    setCalling(false);
    callAudioQueue.current = [];
    currentCallAudio.current?.pause();
    currentCallAudio.current = null;
    if (callRec.current && callRec.current.state !== "inactive") callRec.current.stop();
    callStreamRef.current?.getTracks().forEach((t) => t.stop());
    callRec.current = null;
    callStreamRef.current = null;
    if (seconds >= 2) {
      callSummary(friendId, seconds, callTurns);
      showToast(callTurns > 0 ? `通话结束，聊了 ${callTurns} 轮` : "通话结束，摘要已存进会话");
    }
  };

  useEffect(() => {
    if (!calling) {
      stopVad();
      return;
    }
    if (callPhase === "live" && !callMutedRef.current) void startVad();
    return () => stopVad();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [calling, callPhase]);

  useEffect(() => {
    if (!calling) return;
    setCallSeconds(0);
    setCallPhase("dialing");
    const dial = window.setTimeout(() => setCallPhase("live"), 1600);
    const tick = window.setInterval(() => setCallSeconds((s) => s + 1), 1000);
    return () => {
      window.clearTimeout(dial);
      window.clearInterval(tick);
    };
  }, [calling]);

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

  if (!friend) {
    return (
      <div className="w4-main">
        <div className="w4-head">
          <button className="w4-icon-btn" onClick={onBack} aria-label="返回">
            <Icon strokeWidth={2}>
              <path d="M15 5l-7 7 7 7" />
            </Icon>
          </button>
          <div className="w4-head-main">
            <div className="w4-head-title">会话不存在</div>
          </div>
        </div>
        <div className="w4-centered" style={{ color: "var(--w-text-3)", fontSize: 13 }}>
          这个好友可能已经被删除了。
        </div>
      </div>
    );
  }

  return (
    <div className="w4-main">
      {/* 顶部 */}
      <div className="w4-head">
        <button className="w4-icon-btn w4-only-mobile" onClick={onBack} aria-label="返回">
          <Icon strokeWidth={2}>
            <path d="M15 5l-7 7 7 7" />
          </Icon>
        </button>
        <button
          className="w4-head-person"
          onClick={() => {
            setMobileInfo(true);
            setInfoOpen(true);
          }}
        >
          <Avatar friend={friend} size={34} />
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
            className="w4-icon-btn"
            aria-label="语音通话"
            onClick={() => void startCall()}
            title="语音通话"
          >
            <Phone size={17} strokeWidth={1.8} />
          </button>
          <button
            className="w4-icon-btn"
            aria-label="翻译最近消息"
            title="把最近的对方消息都翻译成中文"
            onClick={() => translateAll(friend.id)}
          >
            <Languages size={17} strokeWidth={1.8} />
          </button>
          <button
            className={`w4-icon-btn ${infoOpen ? "is-active" : ""}`}
            aria-label="对方资料"
            onClick={() => setInfoOpen((v) => !v)}
          >
            <Info size={17} strokeWidth={1.8} />
          </button>
        </div>
      </div>

      <div className="w4-chat-layout">
        <div className="w4-chat">
          <div className="w4-chat-scroll" ref={scrollRef}>
            <div className="w4-chat-inner">
              <div className="w4-chat-intro">
                <Avatar friend={friend} size={52} round />
                <div className="w4-chat-intro-name">{friend.name}</div>
                <div className="w4-chat-intro-meta">
                  {friend.flag} {friend.city} · {friend.localTime}
                  {friend.localActivity ? ` · ${friend.localActivity}` : ""}
                </div>
                <div className="w4-chat-intro-hint">
                  {friend.closeness >= 3
                    ? `你们已经聊了 ${friend.turns} 轮。TA 记得你说过的事。`
                    : "说错了也没关系，TA 会等你说完。"}
                </div>
              </div>

              {grouped.map((group) => (
                <div key={group.day}>
                  <div className="w4-day">
                    <span>{group.day}</span>
                  </div>
                  {group.items.map((m) => (
                    <MessageRow
                      key={m.id}
                      message={m}
                      friend={friend}
                      zhShown={!!zhOpen[m.id]}
                      playing={playingId === m.id}
                      onPlay={() => togglePlay(m)}
                      onToggleZh={() => void translateMessage(m.id)}
                      onToggleText={() => toggleShownText(m.id)}
                      onSave={() => saveSuggestion(m.id)}
                      onDismiss={() => dismissSuggestion(m.id)}
                      onZoom={setZoom}
                      onLongPress={(pos) =>
                        setMsgMenu({ message: m, x: pos.x, y: pos.y })
                      }
                    />
                  ))}
                </div>
              ))}

              {isTyping ? (
                <div className="w4-msg">
                  <Avatar friend={friend} size={32} />
                  <div className="w4-msg-col">
                    <div className="w4-bubble" style={{ padding: "10px 13px" }}>
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
                    onChange={(e) => {
                      setDraft(e.target.value);
                      const el = e.target;
                      el.style.height = "auto";
                      el.style.height = `${Math.min(132, el.scrollHeight)}px`;
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
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
                      className={`w4-icon-btn ${emojiOpen ? "is-active" : ""}`}
                      onClick={() => {
                        setEmojiOpen((v) => !v);
                        setPhotoOpen(false);
                      }}
                      aria-label="表情"
                    >
                      <Smile size={18} strokeWidth={1.7} />
                    </button>
                    <button
                      className={`w4-icon-btn ${photoOpen ? "is-active" : ""}`}
                      onClick={() => {
                        setPhotoOpen((v) => !v);
                        setEmojiOpen(false);
                      }}
                      aria-label="图片"
                    >
                      <ImageIcon size={18} strokeWidth={1.7} />
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
                          disabled={!draft.trim()}
                          onClick={send}
                        >
                          发送
                        </button>
                      </>
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <div className="w4-composer-inner">
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
                      const mouse = e.pointerType === "mouse";
                      setRecMouse(mouse);
                      if (mouse) {
                        e.preventDefault();
                        if (!recording) void startRecording(e.clientY);
                        return;
                      }
                      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
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
                      onChange={(e) => {
                        setDraft(e.target.value);
                        const el = e.target;
                        el.style.height = "auto";
                        el.style.height = `${Math.min(132, el.scrollHeight)}px`;
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                          e.preventDefault();
                          send();
                        }
                      }}
                    />
                    <button
                      className="w4-send-btn"
                      disabled={!draft.trim()}
                      onClick={send}
                    >
                      发送
                    </button>
                  </div>
                )}

                <div className="w4-composer-actions">
                  <button
                    className={`w4-icon-btn ${emojiOpen ? "is-active" : ""}`}
                    onClick={() => {
                      setEmojiOpen((v) => !v);
                      setPhotoOpen(false);
                    }}
                    aria-label="表情"
                  >
                    <Smile size={18} strokeWidth={1.7} />
                  </button>
                  <button
                    className={`w4-icon-btn ${photoOpen ? "is-active" : ""}`}
                    onClick={() => {
                      setPhotoOpen((v) => !v);
                      setEmojiOpen(false);
                    }}
                    aria-label="图片"
                  >
                    <ImageIcon size={18} strokeWidth={1.7} />
                  </button>
                </div>
              </div>
            )}
          </div>

          {emojiOpen ? (
            <div className="w4-pop">
              <div className="w4-emoji-grid">
                {EMOJIS.map((e) => (
                  <button
                    key={e}
                    onClick={() => {
                      setDraft((d) => d + e);
                      inputRef.current?.focus();
                    }}
                  >
                    {e}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {photoOpen ? (
            <div className="w4-pop">
              <div className="w4-photo-grid">
                {PHOTOS.map((p) => (
                  <button
                    key={p}
                    onClick={() => {
                      sendImage(friendId, p);
                      setPhotoOpen(false);
                    }}
                  >
                    <img src={p} alt="" />
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {recording && !recMouse ? (
            <div className="w4-rec-overlay">
              <div className="w4-rec-bars" aria-hidden>
                {Array.from({ length: 9 }).map((_, i) => (
                  <i
                    key={i}
                    style={{
                      height: 10 + ((i * 7) % 20),
                      animationDelay: `${i * 0.08}s`,
                      background: cancelHint ? "#f08a80" : "#fff",
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
              {recMouse && false ? (
                <div className="w4-rec-bar">
                  <button className="w4-rec-bar-btn" onClick={() => finishRecording(true)}>
                    <X size={15} strokeWidth={2.2} />
                    取消
                  </button>
                  <button
                    className="w4-rec-bar-btn"
                    onClick={() => finishRecording(false, true)}
                  >
                    <Languages size={15} strokeWidth={2} />
                    转文字
                  </button>
                  <button
                    className="w4-rec-bar-btn is-send"
                    onClick={() => finishRecording(false)}
                  >
                    <ArrowUp size={15} strokeWidth={2.4} />
                    发送
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}

          {recording && !recMouse ? (
            <div className="w4-rec-zones">
              <div
                ref={cancelZoneRef}
                className={`w4-rec-zone is-cancel${recZone === "cancel" ? " on" : ""}`}
              >
                取消
              </div>
              <div
                ref={transcribeZoneRef}
                className={`w4-rec-zone is-text${recZone === "transcribe" ? " on" : ""}`}
              >
                <Languages size={15} strokeWidth={2} />
                滑到这里 转文字
              </div>
            </div>
          ) : null}

          {calling ? (
            <CallOverlay
              friend={friend}
              phase={callPhase}
              seconds={callSeconds}
              turns={callTurns}
              userText={callUserText}
              userZh={callUserZh}
              friendText={callFriendText}
              friendZh={callFriendZh}
              talking={callTalking}
              waiting={callWaiting}
              listening={callListening}
              muted={callMuted}
              friendSpeaking={callFriendSpeaking}
              speakerOn={speakerOn}
              onToggleMute={toggleCallMute}
              onToggleSpeaker={() => setSpeakerOn((v) => !v)}
              onEnd={() => endCall(callSeconds)}
            />
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
                <button className="w4-info-btn" onClick={() => setCalling(true)}>
                  <Phone size={13} />
                  打电话
                </button>
              </div>
              <div className="w4-info-section">
                <div className="w4-info-label">TA 的声音</div>
                <button className="w4-voice-card" onClick={() => void openVoicePicker()}>
                  <span className="w4-voice-card-icon">
                    <Mic size={14} />
                  </span>
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <span style={{ fontSize: 12.5, fontWeight: 500, display: "block" }}>
                      {friend.voiceLabel}
                    </span>
                    <span style={{ fontSize: 11.5, color: "var(--w-text-3)" }}>
                      {settings.tts.cloned ? "使用你的音色克隆" : "真人声线 · 可换"}
                    </span>
                  </span>
                  <span style={{ fontSize: 11.5, color: "var(--w-text-3)" }}>换</span>
                </button>
              </div>
              <div className="w4-info-section">
                <button
                  className="w4-info-btn is-danger"
                  style={{ width: "100%" }}
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
              const H = 82;
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

      {voiceOpen ? (
        <div className="w4-voice-modal" role="dialog" aria-label="换个口音">
          <div className="w4-voice-modal-card">
            <div className="w4-voice-modal-head">
              <span>给 {friend.name} 换个口音</span>
              <button className="w4-icon-btn" onClick={() => setVoiceOpen(false)} aria-label="关闭">
                <X size={16} strokeWidth={2} />
              </button>
            </div>
            <div className="w4-voice-modal-list">
              {voiceList.map((a) => (
                <button
                  key={a.archetype_id}
                  className="w4-voice-modal-item"
                  disabled={voiceBusy}
                  onClick={() => void pickVoice(a)}
                >
                  <span className="w4-voice-modal-name">{a.accent_label || a.display_name}</span>
                  <span className="w4-voice-modal-meta">{a.country || a.native_lang}</span>
                </button>
              ))}
              {voiceList.length === 0 ? (
                <div className="w4-voice-modal-empty">没有可选的口音</div>
              ) : null}
            </div>
            <div className="w4-voice-modal-hint">换口音只改 TA 的声音和说话节奏，聊天记录不变。</div>
          </div>
        </div>
      ) : null}

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
  onToggleZh,
  onToggleText,
  onSave,
  onDismiss,
  onZoom,
  onLongPress,
}: {
  message: Message;
  friend: Friend;
  zhShown: boolean;
  playing: boolean;
  onPlay: () => void;
  onToggleZh: () => void;
  onToggleText: () => void;
  onSave: () => void;
  onDismiss: () => void;
  onZoom: (src: string) => void;
  onLongPress?: (pos: { x: number; y: number }) => void;
}) {
  const { profile } = useApp();
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

  const startPress = (e: React.TouchEvent) => {
    longPressed.current = false;
    cancelPress();
    const t = e.touches?.[0];
    pressPos.current = { x: t ? t.clientX : 0, y: t ? t.clientY : 0 };
    pressTimer.current = window.setTimeout(() => {
      longPressed.current = true;
      onLongPress?.(pressPos.current);
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

  return (
    <div className={`w4-msg ${mine ? "is-me" : ""}`}>
      {mine ? (
        <Avatar src={profile.avatar} name={profile.name} size={32} />
      ) : (
        <Avatar friend={friend} size={32} />
      )}
      <div className="w4-msg-col">
        <div className="w4-bubble-row">
          <div className="w4-bubble-stack">
          <div
            className={`w4-bubble ${isImage ? "is-image" : ""}`}
            title={clockTime(message.time)}
            onTouchStart={isVoice ? (e) => startPress(e) : undefined}
            onTouchEnd={isVoice ? endPress : undefined}
            onTouchMove={isVoice ? cancelPress : undefined}
            onTouchCancel={isVoice ? cancelPress : undefined}
            onContextMenu={
              isVoice
                ? (e) => {
                    e.preventDefault();
                    onLongPress?.({ x: e.clientX, y: e.clientY });
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
          {/* 语音的「转文字 / 翻译」已经在气泡右侧，这里不再重复 */}
          {!isVoice && message.translation ? (
            <button className="w4-mini-btn" onClick={onToggleZh}>
              {zhShown ? "收起翻译" : "翻译"}
            </button>
          ) : null}
          <span className="w4-msg-meta" style={{ padding: 0, marginLeft: 2 }}>
            {clockTime(message.time)}
            {mine ? (message.read ? " · 已读" : " · 发送中") : ""}
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
          style={{ marginLeft: "auto", color: "var(--w-text-3)", display: "inline-flex" }}
          onClick={onDismiss}
          aria-label="忽略"
        >
          <X size={12} strokeWidth={2} />
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

function CallOverlay({
  friend,
  phase,
  seconds,
  turns,
  userText,
  userZh,
  friendText,
  friendZh,
  talking,
  waiting,
  listening,
  muted,
  friendSpeaking,
  speakerOn,
  onToggleMute,
  onToggleSpeaker,
  onEnd,
}: {
  friend: Friend;
  phase: "dialing" | "live";
  seconds: number;
  turns: number;
  userText: string;
  userZh: string;
  friendText: string;
  friendZh: string;
  talking: boolean;
  waiting: boolean;
  listening: boolean;
  muted: boolean;
  friendSpeaking: boolean;
  speakerOn: boolean;
  onToggleMute: () => void;
  onToggleSpeaker: () => void;
  onEnd: () => void;
}) {
  const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
  const ss = String(seconds % 60).padStart(2, "0");

  const status =
    phase === "dialing"
      ? "正在连接…"
      : muted
        ? "已静音，点一下话筒继续"
        : friendSpeaking
          ? `${friend.name} 正在说…`
          : waiting
            ? `${friend.name} 正在想怎么回你…`
            : talking
              ? "在听你说…"
              : turns === 0
                ? "免提中 · 你说完停顿一下，我就回你"
                : `已聊 ${turns} 轮 · 继续说就好`;

  return (
    <div className="w4-call">
      {/* 模糊背景：把 TA 的头像放大、虚化，作为通话时的氛围底 */}
      <div className="w4-call-bg" style={{ backgroundImage: `url("${friend.avatar}")` }} />
      <div className="w4-call-shade" />

      <div className="w4-call-top">
        <button className="w4-call-min" onClick={onEnd} aria-label="收起通话">
          <Minimize2 size={18} strokeWidth={1.9} />
        </button>
        <span className="w4-call-label">语音通话</span>
      </div>

      <div className="w4-call-stage">
        <div className={`w4-call-cardwrap${waiting ? " is-waiting" : ""}`}>
          <div className="w4-call-card">
            <img src={friend.avatar} alt="" />
          </div>
          <span className="w4-call-card-badge">
            <img src={friend.avatar} alt="" />
          </span>
        </div>

        <div className="w4-call-dots" aria-label={`通话进行中，已聊 ${turns} 轮`}>
          <i className={phase === "live" ? "on" : ""} />
          <i className={talking ? "on" : ""} />
          <i className={turns > 0 ? "on" : ""} />
        </div>

        <div className="w4-call-name">{friend.name}</div>
        <div className="w4-call-time">
          {phase === "dialing" ? "正在接通…" : `通话中 ${mm}:${ss}`}
        </div>

        <div className="w4-call-sub">
          {userText ? (
            <div className="w4-call-line is-me">
              你：{userText}
              {userZh ? <div className="w4-call-zh">{userZh}</div> : null}
            </div>
          ) : null}
          {friendText ? (
            <div className="w4-call-line">
              {friendText}
              {friendZh ? <div className="w4-call-zh">{friendZh}</div> : null}
            </div>
          ) : null}
          {talking || waiting || friendSpeaking ? (
            <span className="w4-call-wave" aria-hidden>
              <i /><i /><i /><i /><i /><i /><i />
            </span>
          ) : null}
        </div>

        <div className="w4-call-status">{status}</div>
      </div>

      <div className="w4-call-keys">
        <div className="w4-call-key">
          <button
            className={`w4-call-keybtn is-talk${talking ? " is-rec" : ""}${muted ? " is-off" : ""}`}
            onClick={onToggleMute}
            aria-label={muted ? "打开麦克风" : "静音"}
          >
            {muted ? <MicOff size={27} strokeWidth={1.9} /> : <Mic size={27} strokeWidth={1.9} />}
          </button>
          <span>{muted ? "已静音" : talking ? "正在听你说" : "免提中"}</span>
        </div>

        <div className="w4-call-key">
          <button className="w4-call-keybtn is-hangup" onClick={onEnd} aria-label="挂断">
            <Phone size={27} strokeWidth={1.9} style={{ transform: "rotate(135deg)" }} />
          </button>
          <span>挂断</span>
        </div>

        <div className="w4-call-key">
          <button
            className={`w4-call-keybtn is-speaker${speakerOn ? "" : " is-off"}`}
            onClick={onToggleSpeaker}
            aria-label={speakerOn ? "关闭声音" : "打开声音"}
          >
            {speakerOn ? <Volume2 size={27} strokeWidth={1.9} /> : <VolumeX size={27} strokeWidth={1.9} />}
          </button>
          <span>{speakerOn ? "声音已开" : "声音已关"}</span>
        </div>
      </div>
    </div>
  );
}
