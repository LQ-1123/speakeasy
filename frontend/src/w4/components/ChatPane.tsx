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
  Languages,
  Mic,
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

  /* 关掉声音时立刻停下正在播的那句，而不是等它播完 */
  useEffect(() => {
    speakerOnRef.current = speakerOn;
    if (!speakerOn) currentCallAudio.current?.pause();
  }, [speakerOn]);
  const [mobileInfo, setMobileInfo] = useState(false);
  const [wide, setWide] = useState(false);
  const [zoom, setZoom] = useState<string | null>(null);
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
    return () => mq.removeEventListener("change", update);
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

  const finishRecording = (cancelled: boolean) => {
    if (!recording) return;
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
      void sendVoiceBlob(friendId, blob, ms);
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

  /** 顺序播放后端逐句下发的语音，避免叠在一起 */
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
        audio.onended = () => resolve();
        audio.onerror = () => resolve();
        void audio.play().catch(() => resolve());
      });
    }
    callAudioPlaying.current = false;
  };

  const startCallTalk = async () => {
    if (!friend) return;
    setCallTalking(true);
    setCallUserText("");
    callStartAt.current = Date.now();
    try {
      callStreamRef.current = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";
      const rec = new MediaRecorder(callStreamRef.current, { mimeType: mime });
      callChunks.current = [];
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size) callChunks.current.push(e.data);
      };
      rec.start(200);
      callRec.current = rec;
    } catch {
      setCallTalking(false);
      showToast("没有麦克风权限，先在浏览器里允许一下", "error");
    }
  };

  const finishCallTalk = () => {
    if (!callTalking) return;
    setCallTalking(false);
    const ms = Date.now() - callStartAt.current;
    const rec = callRec.current;
    const stream = callStreamRef.current;
    if (!rec || !friend || ms < 500) {
      if (rec && rec.state !== "inactive") rec.stop();
      stream?.getTracks().forEach((t) => t.stop());
      callRec.current = null;
      callStreamRef.current = null;
      if (ms < 500) showToast("说一句再松手");
      return;
    }

    rec.onstop = () => {
      const blob = new Blob(callChunks.current, { type: rec.mimeType || "audio/webm" });
      callChunks.current = [];
      stream?.getTracks().forEach((t) => t.stop());
      callRec.current = null;
      callStreamRef.current = null;
      if (blob.size < 1200) {
        showToast("没录到声音", "error");
        return;
      }

      setCallWaiting(true);
      setCallFriendText("");
      void callTurnStream(friend.id, blob, ms, (ev) => {
        if (ev.type === "transcript") {
          setCallUserText(ev.text);
        } else if (ev.type === "sentence") {
          setCallFriendText((t) => (t ? t + " " : "") + ev.text);
          if (ev.audioUrl) {
            callAudioQueue.current.push(ev.audioUrl);
            void pumpCallAudio();
          }
        } else if (ev.type === "done") {
          setCallWaiting(false);
          setCallTurns((n) => n + 1);
        } else if (ev.type === "error") {
          setCallWaiting(false);
          showToast(ev.message || "这一轮没接上", "error");
        }
      }).then((ok) => {
        if (!ok) {
          setCallWaiting(false);
          showToast("通话连接失败", "error");
        }
      });
    };
    rec.stop();
  };

  const startCall = async () => {
    setCalling(true);
    setCallTurns(0);
    setCallUserText("");
    setCallFriendText("");
    if (friend) await callWarmup(friend.id);
  };

  const endCall = (seconds: number) => {
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
            title="把最近的对方消息都译成中文"
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
                      onToggleZh={() => toggleZh(m.id)}
                      onToggleText={() => toggleShownText(m.id)}
                      onSave={() => saveSuggestion(m.id)}
                      onDismiss={() => dismissSuggestion(m.id)}
                      onZoom={setZoom}
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
                    <Mic size={17} strokeWidth={1.8} />
                  )}
                </button>
              </div>

              {voiceMode ? (
                <button
                  className={`w4-hold ${recording ? "is-rec" : ""} ${cancelHint ? "is-cancel" : ""}`}
                  onPointerDown={(e) => {
                    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
                    startRecording(e.clientY);
                  }}
                  onPointerMove={(e) => {
                    if (!recording) return;
                    setCancelHint(startY.current - e.clientY > 50);
                  }}
                  onPointerUp={() => finishRecording(cancelHint)}
                  onPointerCancel={() => finishRecording(true)}
                >
                  {recording ? (cancelHint ? "松开取消" : "松开发送") : "按住 说话"}
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

          {recording ? (
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
              <div className="w4-rec-hint">{cancelHint ? "" : "上滑取消"}</div>
            </div>
          ) : null}

          {calling ? (
            <CallOverlay
              friend={friend}
              phase={callPhase}
              seconds={callSeconds}
              turns={callTurns}
              userText={callUserText}
              friendText={callFriendText}
              talking={callTalking}
              waiting={callWaiting}
              speakerOn={speakerOn}
              onTalkStart={() => void startCallTalk()}
              onTalkEnd={finishCallTalk}
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
}) {
  const { profile } = useApp();
  const mine = message.from === "me";

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
          <div
            className={`w4-bubble ${isImage ? "is-image" : ""}`}
            title={clockTime(message.time)}
          >
          {isVoice ? (
            <>
              <VoiceBar
                id={message.id}
                durationMs={message.durationMs ?? 3000}
                playing={playing}
                onToggle={onPlay}
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

          {/* 语音消息的两个操作放气泡右侧（自己的消息由 CSS 翻到左侧） */}
          {isVoice ? (
            <div className="w4-bubble-side">
              <button className="w4-mini-btn" onClick={onToggleText}>
                {message.shownText ? "收文字" : "看文字"}
              </button>
              {message.translation ? (
                <button className="w4-mini-btn" onClick={onToggleZh}>
                  {zhShown ? "收起中文" : "译成中文"}
                </button>
              ) : null}
            </div>
          ) : null}
        </div>

        {/* 译文（消息下方，跟原文区分开） */}
        {zhShown && message.translation ? (
          <div className="w4-translation">{message.translation}</div>
        ) : null}

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
          {/* 语音的「看文字 / 译成中文」已经在气泡右侧，这里不再重复 */}
          {!isVoice && message.translation ? (
            <button className="w4-mini-btn" onClick={onToggleZh}>
              {zhShown ? "收起中文" : "译成中文"}
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
  friendText,
  talking,
  waiting,
  speakerOn,
  onTalkStart,
  onTalkEnd,
  onToggleSpeaker,
  onEnd,
}: {
  friend: Friend;
  phase: "dialing" | "live";
  seconds: number;
  turns: number;
  userText: string;
  friendText: string;
  talking: boolean;
  waiting: boolean;
  speakerOn: boolean;
  onTalkStart: () => void;
  onTalkEnd: () => void;
  onToggleSpeaker: () => void;
  onEnd: () => void;
}) {
  const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
  const ss = String(seconds % 60).padStart(2, "0");

  const status =
    phase === "dialing"
      ? "正在连接…"
      : talking
        ? "正在听你说，松手就发出去"
        : waiting
          ? `${friend.name} 正在想怎么回你…`
          : turns === 0
            ? "免提已开 · 按住话筒说话"
            : `已聊 ${turns} 轮 · 按住话筒继续说`;

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
          {userText ? <div className="w4-call-line is-me">你：{userText}</div> : null}
          {friendText ? <div className="w4-call-line">{friendText}</div> : null}
          {talking || waiting ? (
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
            className={`w4-call-keybtn is-talk${talking ? " is-rec" : ""}`}
            onPointerDown={() => void onTalkStart()}
            onPointerUp={onTalkEnd}
            onPointerCancel={onTalkEnd}
            onPointerLeave={() => talking && onTalkEnd()}
            aria-label="按住说话"
          >
            <Mic size={27} strokeWidth={1.9} />
          </button>
          <span>{talking ? "松开发送" : "按住说话"}</span>
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
