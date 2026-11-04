"use client";

/**
 * SpeakEasy v3 —— 面向「只想练口语的普通人」的界面
 *
 * 设计取舍（针对上一版反馈）
 * - 只用三种颜色：纸白 / 墨黑 / 低饱和青绿。没有渐变、没有紫橙、没有 emoji 当图标
 * - 不出现「表达本 / 画像 / 记忆卡 / 口癖 / 共同记忆」这类词
 *   → 换成「认识新朋友 / 我们聊过什么 / 我在练」
 * - 每屏只有 1 个主操作：聊天=发消息；认识人=认识 TA；我=保存
 * - 次级信息默认不显示，点一下才出现（翻译、换一个）
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "@/app/v3.css";

/* ------------------------------------------------------------------ 类型 */
type Friend = {
  id: string;
  name: string;
  city?: string;
  nationality?: string;
  age?: number;
  occupation?: string;
  bio?: string;
  reply_language?: string;
  closeness_level?: number;
  total_turns?: number;
  unread?: number;
  /* 接口里是"最后一条消息对象"，不是字符串 —— 渲染前必须取 transcript */
  last_message?: unknown;
  last_interaction_at?: string | null;
  local_time?: string;
  local_icon?: string;
  local_period?: string;
  replying?: boolean;
  speech_quirks?: string[];
  shared_episodes?: string[];
};

type Msg = {
  id: string;
  sender: "user" | "friend" | string;
  msg_type: string;
  transcript: string;
  audio_url?: string | null;
  duration_ms?: number;
  created_at?: string;
};

type Profile = {
  nickname?: string;
  native_language?: string;
  target_languages?: string[];
};

/* ------------------------------------------------------------- 小工具 */
const LANGS: Array<{ code: string; label: string }> = [
  { code: "en", label: "英语" },
  { code: "ja", label: "日语" },
  { code: "ko", label: "韩语" },
  { code: "fr", label: "法语" },
  { code: "de", label: "德语" },
  { code: "es", label: "西班牙语" },
  { code: "it", label: "意大利语" },
  { code: "pt", label: "葡萄牙语" },
];

const LANG_NAME = (code?: string) =>
  LANGS.find((l) => l.code === code)?.label ?? "外语";

const HUES = ["#7d8f7a", "#8a7d6e", "#6f7f8c", "#8c7a7d", "#75806f", "#807a8c"];
function hueOf(id: string) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 997;
  return HUES[h % HUES.length];
}
function initial(name: string) {
  return (name || "?").trim().charAt(0).toUpperCase();
}
function shortTime(s?: string | null) {
  if (!s) return "";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}
function dur(ms?: number) {
  const s = Math.max(0, Math.round((ms || 0) / 1000));
  return `0:${String(s).padStart(2, "0")}`;
}

/** 列表里那一行预览：接口可能返回字符串，也可能返回最后一条消息对象。 */
function previewOf(f: Friend): string {
  const lm = f.last_message;
  if (typeof lm === "string" && lm.trim()) return lm;
  if (lm && typeof lm === "object") {
    const t = (lm as { transcript?: unknown }).transcript;
    if (typeof t === "string" && t.trim()) return t;
  }
  return "开始聊天";
}

/* --------------------------------------------------------------- 图标 */
const IcoChat = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
    <path d="M21 11.5a8.4 8.4 0 01-9 8.4 8.9 8.9 0 01-4-.9L3 21l1.9-4.9A8.4 8.4 0 113 12a8.4 8.4 0 0118-.5z" />
  </svg>
);
const IcoMeet = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
    <circle cx="9" cy="8" r="3.4" />
    <path d="M2.8 20c.6-3.6 3.2-5.6 6.2-5.6s5.6 2 6.2 5.6" />
    <path d="M17 6.5v5M19.5 9h-5" />
  </svg>
);
const IcoMe = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
    <circle cx="12" cy="8.5" r="3.6" />
    <path d="M4.5 20.5c.9-4.2 3.9-6.4 7.5-6.4s6.6 2.2 7.5 6.4" />
  </svg>
);
const IcoBack = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
    <path d="M15 5l-7 7 7 7" />
  </svg>
);
const IcoMic = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5 11a7 7 0 0014 0M12 18v3" />
  </svg>
);
const IcoPlay = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
    <path d="M8 5.5v13l11-6.5z" />
  </svg>
);

/* ---------------------------------------------------------- 波形（静态） */
function Wave({ durMs, playing }: { durMs: number; playing: boolean }) {
  const n = durMs > 7000 ? 20 : 14;
  const bars = useMemo(() => {
    const out: number[] = [];
    let s = durMs || 1;
    for (let i = 0; i < n; i++) {
      s = (s * 9301 + 49297) % 233280;
      out.push(5 + Math.round((s / 233280) * 15));
    }
    return out;
  }, [n, durMs]);
  return (
    <span className="v3-wave" aria-hidden>
      {bars.map((h, i) => (
        <i key={i} className={playing && i < n * 0.4 ? "on" : ""} style={{ height: h }} />
      ))}
    </span>
  );
}

/* ================================================================ 主应用 */
export function V3App() {
  const [tab, setTab] = useState<"chats" | "meet" | "me">("chats");
  const [friends, setFriends] = useState<Friend[]>([]);
  const [profile, setProfile] = useState<Profile>({});
  const [loading, setLoading] = useState(true);
  const [openId, setOpenId] = useState<string | null>(null);
  const [toast, setToast] = useState("");

  const showToast = useCallback((s: string) => {
    setToast(s);
    window.setTimeout(() => setToast(""), 2200);
  }, []);

  const reload = useCallback(async () => {
    try {
      const r = await fetch("/api/v2/bootstrap", { cache: "no-store" });
      const j = await r.json();
      setFriends(j.friends || []);
      setProfile(j.profile || {});
    } catch {
      /* 静默 */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const openFriend = friends.find((f) => f.id === openId) || null;

  const showList = openFriend !== null || tab === "chats";
  const showMain = openFriend !== null || tab !== "chats";

  const goTab = (t: "chats" | "meet" | "me") => {
    setOpenId(null);
    setTab(t);
  };

  return (
    <div className="v3">
      <div className="v3-col">
        <div className="v3-shell">
          <aside className="v3-rail">
            <button
              className={`v3-rail-btn ${tab === "chats" && !openFriend ? "on" : ""}`}
              onClick={() => goTab("chats")}
              aria-label="消息"
            >
              <IcoChat />
            </button>
            <button
              className={`v3-rail-btn ${tab === "meet" && !openFriend ? "on" : ""}`}
              onClick={() => goTab("meet")}
              aria-label="认识人"
            >
              <IcoMeet />
            </button>
            <button
              className={`v3-rail-btn ${tab === "me" && !openFriend ? "on" : ""}`}
              onClick={() => goTab("me")}
              aria-label="我"
            >
              <IcoMe />
            </button>
            <span className="v3-rail-ava">{initial(profile.nickname || "我")}</span>
          </aside>

          <aside className="v3-listpane" data-hide={showList ? "0" : "1"}>
            <ChatsScreen
              friends={friends}
              loading={loading}
              onOpen={setOpenId}
              onGoMeet={() => goTab("meet")}
            />
          </aside>

          <main className="v3-mainpane" data-hide={showMain ? "0" : "1"}>
            {openFriend ? (
              <ChatScreen
                friend={openFriend}
                onBack={() => {
                  setOpenId(null);
                  void reload();
                }}
                onToast={showToast}
              />
            ) : tab === "meet" ? (
              <MeetScreen
                onDone={(f) => {
                  void reload();
                  setOpenId(f.id);
                }}
                onToast={showToast}
              />
            ) : tab === "me" ? (
              <MeScreen
                profile={profile}
                onSaved={(p) => {
                  setProfile(p);
                  showToast("保存好了");
                }}
                onToast={showToast}
              />
            ) : (
              <div className="v3-placeholder">
                <IcoChat />
              </div>
            )}
          </main>
        </div>

        {!openFriend ? (
          <nav className="v3-tabs">
            <button className={`v3-tab ${tab === "chats" ? "on" : ""}`} onClick={() => goTab("chats")}>
              <IcoChat />
              消息
            </button>
            <button className={`v3-tab ${tab === "meet" ? "on" : ""}`} onClick={() => goTab("meet")}>
              <IcoMeet />
              认识人
            </button>
            <button className={`v3-tab ${tab === "me" ? "on" : ""}`} onClick={() => goTab("me")}>
              <IcoMe />
              我
            </button>
          </nav>
        ) : null}
      </div>
      {toast ? <div className="v3-toast">{toast}</div> : null}
    </div>
  );
}

/* ================================================================ 消息列表 */
function ChatsScreen({
  friends,
  loading,
  onOpen,
  onGoMeet,
}: {
  friends: Friend[];
  loading: boolean;
  onOpen: (id: string) => void;
  onGoMeet: () => void;
}) {
  return (
    <>
      <div className="v3-nav">
        <div className="v3-nav-center">
          <div className="v3-nav-title">消息</div>
        </div>
      </div>
      {loading ? (
        <div className="v3-scroll">
          <div className="v3-loading">正在打开…</div>
        </div>
      ) : friends.length === 0 ? (
        <div className="v3-empty">
          <p>
            还没有聊天的人。
            <br />
            认识一个母语者，现在就能开口。
          </p>
          <button className="v3-btn-main" style={{ maxWidth: 220 }} onClick={onGoMeet}>
            认识一个人
          </button>
        </div>
      ) : (
        <div className="v3-scroll">
          {friends.map((f) => (
            <button key={f.id} className="v3-row" onClick={() => onOpen(f.id)}>
              <span className="v3-ava" style={{ background: hueOf(f.id) }}>
                {initial(f.name)}
              </span>
              <span className="v3-row-main">
                <span className="v3-row-top">
                  <span className="v3-name">{f.name}</span>
                  <span className="v3-time">
                    {shortTime(f.last_interaction_at) || f.local_time || ""}
                  </span>
                </span>
                <span className="v3-preview">
                  {f.replying ? "正在说话…" : previewOf(f)}
                </span>
              </span>
              {f.unread ? <span className="v3-dot" /> : null}
            </button>
          ))}
        </div>
      )}
    </>
  );
}

/* ================================================================ 聊天 */
function ChatScreen({
  friend,
  onBack,
  onToast,
}: {
  friend: Friend;
  onBack: () => void;
  onToast: (s: string) => void;
}) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [replying, setReplying] = useState(false);
  const [shownTools, setShownTools] = useState<string | null>(null);
  const [trans, setTrans] = useState<Record<string, string>>({});
  const [saidIds, setSaidIds] = useState<Record<string, boolean>>({});
  const [playingId, setPlayingId] = useState<string | null>(null);
  /* 按住说话 */
  const [voiceMode, setVoiceMode] = useState(false);
  const [recording, setRecording] = useState(false);
  const [willCancel, setWillCancel] = useState(false);
  const recRef = useRef<{
    rec: MediaRecorder | null;
    chunks: Blob[];
    startY: number;
    startAt: number;
    stream: MediaStream | null;
    cancelled: boolean;
  }>({ rec: null, chunks: [], startY: 0, startAt: 0, stream: null, cancelled: false });
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/v2/friends/${friend.id}/messages?limit=40`, { cache: "no-store" });
      const j = await r.json();
      const list: Msg[] = j.messages || [];
      setMsgs((prev) => {
        const map = new Map<string, Msg>();
        prev.forEach((m) => map.set(m.id, m));
        list.forEach((m) => map.set(m.id, m));
        return Array.from(map.values()).sort((a, b) => {
          const t1 = new Date(a.created_at || 0).getTime();
          const t2 = new Date(b.created_at || 0).getTime();
          return (Number.isNaN(t1) ? 0 : t1) - (Number.isNaN(t2) ? 0 : t2);
        });
      });
    } catch {
      /* 静默 */
    }
  }, [friend.id]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [msgs.length, replying]);

  const speak = (m: Msg) => {
    const src = m.audio_url || "";
    if (!src) return;
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    const a = new Audio(src);
    audioRef.current = a;
    a.onended = () => setPlayingId(null);
    a.onerror = () => {
      setPlayingId(null);
      onToast("这条语音没能加载");
    };
    setPlayingId(m.id);
    void a.play().catch(() => {
      setPlayingId(null);
      onToast("点一下再试一次");
    });
  };

  const translate = async (m: Msg) => {
    if (trans[m.id]) {
      setShownTools(shownTools === m.id ? null : m.id);
      return;
    }
    try {
      const r = await fetch("/api/v2/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: m.transcript, friend_id: friend.id }),
      });
      const j = await r.json();
      const out = j.translation || j.text || j.result || "";
      if (!out) throw new Error("空");
      setTrans((t) => ({ ...t, [m.id]: out }));
    } catch {
      onToast("暂时翻译不了这条");
    }
  };

  const startRec = async (y: number) => {
    if (recRef.current.rec) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
      const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) chunks.push(e.data);
      };
      recRef.current = {
        rec,
        chunks,
        startY: y,
        startAt: Date.now(),
        stream,
        cancelled: false,
      };
      rec.start();
      setRecording(true);
      setWillCancel(false);
    } catch {
      onToast("需要麦克风权限，告诉我允许就能录");
    }
  };

  const finishRec = (cancel: boolean) => {
    const st = recRef.current;
    if (!st.rec) return;
    st.cancelled = cancel;
    try {
      st.rec.stop();
    } catch {
      /* ignore */
    }
    st.rec.onstop = async () => {
      st.stream?.getTracks().forEach((t) => t.stop());
      const ms = Date.now() - st.startAt;
      const blob = new Blob(st.chunks, { type: st.chunks[0]?.type || "audio/webm" });
      recRef.current = { rec: null, chunks: [], startY: 0, startAt: 0, stream: null, cancelled: false };
      setRecording(false);
      setWillCancel(false);
      if (cancel || ms < 400 || blob.size < 1200) {
        if (!cancel) onToast("时间太短，按住多说一会儿");
        return;
      }
      // 乐观占位 + 上传
      const temp: Msg = {
        id: `local-v-${Date.now()}`,
        sender: "user",
        msg_type: "voice",
        transcript: "",
        duration_ms: ms,
        created_at: new Date().toISOString(),
      };
      setMsgs((p) => [...p, temp]);
      setReplying(true);
      try {
        const fd = new FormData();
        fd.append("audio", blob, "voice.webm");
        fd.append("duration_ms", String(ms));
        const r = await fetch(`/api/v2/chat/${friend.id}/voice`, { method: "POST", body: fd });
        if (!r.ok) throw new Error(String(r.status));
        const j = await r.json();
        const incoming: Msg[] = [];
        if (j.user_message) incoming.push(j.user_message);
        (j.friend_messages || []).forEach((m: Msg) => incoming.push(m));
        setMsgs((p) => {
          const rest = p.filter((m) => m.id !== temp.id);
          const map = new Map<string, Msg>();
          rest.forEach((m) => map.set(m.id, m));
          incoming.forEach((m) => map.set(m.id, m));
          return Array.from(map.values());
        });
      } catch {
        setMsgs((p) => p.filter((m) => m.id !== temp.id));
        onToast("语音没发出去，再试一次");
      } finally {
        setReplying(false);
      }
    };
  };

  const send = async () => {
    const body = text.trim();
    if (!body || sending) return;
    setSending(true);
    setText("");
    // 乐观气泡
    const temp: Msg = {
      id: `local-${Date.now()}`,
      sender: "user",
      msg_type: "text",
      transcript: body,
      created_at: new Date().toISOString(),
    };
    setMsgs((p) => [...p, temp]);
    setReplying(true);
    try {
      const r = await fetch(`/api/v2/chat/${friend.id}/text`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: body, context: null }),
      });
      if (!r.ok) throw new Error(String(r.status));
      const j = await r.json();
      const incoming: Msg[] = [];
      if (j.user_message) incoming.push(j.user_message);
      (j.friend_messages || []).forEach((m: Msg) => incoming.push(m));
      setMsgs((p) => {
        const rest = p.filter((m) => m.id !== temp.id);
        const map = new Map<string, Msg>();
        rest.forEach((m) => map.set(m.id, m));
        incoming.forEach((m) => map.set(m.id, m));
        return Array.from(map.values());
      });
    } catch {
      setMsgs((p) => p.filter((m) => m.id !== temp.id));
      onToast("没发出去，检查一下网络");
    } finally {
      setSending(false);
      setReplying(false);
    }
  };

  const greet = useMemo(() => {
    const l = LANG_NAME(friend.reply_language);
    return `${friend.name}${friend.city ? " · " + friend.city : ""}`;
  }, [friend]);

  return (
    <>
      <div className="v3-nav">
        <button className="v3-icon" onClick={onBack} aria-label="返回">
          <IcoBack />
        </button>
        <div className="v3-nav-center">
          <div className="v3-nav-title">{friend.name}</div>
          <div className="v3-nav-sub">
            {replying || friend.replying ? "正在说话…" : greet}
          </div>
        </div>
        <span className="v3-icon" />
      </div>

      <div className="v3-chat" ref={scrollRef}>
        {msgs.length === 0 ? (
          <div className="v3-empty">
            <p>
              {friend.name} 在中国时间 {friend.local_time || "现在"}，正等着你开口。
              <br />
              说一句「Hi」，聊起来就行 —— 说错了也没关系。
            </p>
          </div>
        ) : null}

        {msgs.map((m, i) => {
          const mine = m.sender === "user";
          const prev = msgs[i - 1];
          const showDay =
            !prev ||
            new Date(prev.created_at || 0).toDateString() !==
              new Date(m.created_at || 0).toDateString();
          const isVoice = m.msg_type === "voice";
          const toolsOpen = shownTools === m.id;
          return (
            <div key={m.id}>
              {showDay ? <div className="v3-day">今天</div> : null}
              <div className={`v3-turn ${mine ? "me" : "other"}`}>
                <span className="v3-ava sm" style={{ background: hueOf(mine ? "me" : friend.id) }}>
                  {mine ? "我" : initial(friend.name)}
                </span>
                <div className="v3-bubble" onClick={() => setShownTools(toolsOpen ? null : m.id)}>
                  {isVoice ? (
                    <button className="v3-voice" onClick={() => speak(m)} aria-label="播放语音">
                      <Wave durMs={m.duration_ms || 3000} playing={playingId === m.id} />
                      <span className="v3-dur">{dur(m.duration_ms)}</span>
                    </button>
                  ) : (
                    m.transcript
                  )}
                </div>
              </div>

              {saidIds[m.id] && m.transcript ? (
                <div className="v3-said">{m.transcript}</div>
              ) : null}
              {trans[m.id] ? <div className="v3-trans">{trans[m.id]}</div> : null}

              {toolsOpen && m.transcript ? (
                <div className="v3-tools">
                  {isVoice ? (
                    <button onClick={() => setSaidIds((p) => ({ ...p, [m.id]: !p[m.id] }))}>
                      {saidIds[m.id] ? "收起文字" : "看文字"}
                    </button>
                  ) : null}
                  <button onClick={() => void translate(m)}>{trans[m.id] ? "收起中文" : "译成中文"}</button>
                  {isVoice ? <button onClick={() => speak(m)}>重播</button> : null}
                </div>
              ) : null}
            </div>
          );
        })}

        {replying ? (
          <div className="v3-turn other">
            <span className="v3-ava sm" style={{ background: hueOf(friend.id) }}>
              {initial(friend.name)}
            </span>
            <div className="v3-bubble">
              <span className="v3-typing">
                <i />
                <i />
                <i />
              </span>
            </div>
          </div>
        ) : null}
        <div ref={bottomRef} />
      </div>

      {recording ? (
        <div className="v3-rec-tip">{willCancel ? "松手取消" : "松手发送 · 上滑取消"}</div>
      ) : null}

      <div className="v3-input-wrap">
        <button
          className="v3-mic"
          aria-label={voiceMode ? "切换到打字" : "切换到语音"}
          onClick={() => setVoiceMode((v) => !v)}
        >
          {voiceMode ? <span style={{ fontSize: 19 }}>⌨</span> : <IcoMic />}
        </button>

        {voiceMode ? (
          <div
            className={`v3-holdbar ${recording ? (willCancel ? "cancel" : "rec") : ""}`}
            onPointerDown={(e) => {
              (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
              void startRec(e.clientY);
            }}
            onPointerMove={(e) => {
              if (!recRef.current.rec) return;
              setWillCancel(recRef.current.startY - e.clientY > 56);
            }}
            onPointerUp={(e) => finishRec(recRef.current.startY - e.clientY > 56)}
            onPointerCancel={() => finishRec(true)}
            onLostPointerCapture={() => {
              if (recRef.current.rec) finishRec(false);
            }}
          >
            {recording ? (willCancel ? "松开取消" : "松开发送") : "按住 说话"}
          </div>
        ) : null}

        <input
          className="v3-input"
          style={voiceMode ? { display: "none" } : undefined}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) void send();
          }}
          placeholder={`跟 ${friend.name} 说点什么`}
        />
        {text.trim() ? (
          <button className="v3-send" onClick={() => void send()} disabled={sending}>
            发送
          </button>
        ) : null}
      </div>
    </>
  );
}

/* ================================================================ 认识人 */
function MeetScreen({
  onDone,
  onToast,
}: {
  onDone: (f: Friend) => void;
  onToast: (s: string) => void;
}) {
  const [cand, setCand] = useState<Friend | null>(null);
  const [busy, setBusy] = useState(false);
  const [lang, setLang] = useState<string>("");
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const pick = useCallback(async () => {
    setBusy(true);
    try {
      const r = await fetch("/api/v2/friends/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lang ? { language: lang } : {}),
      });
      if (!r.ok) throw new Error(String(r.status));
      const j = await r.json();
      const f: Friend = j.friend || j;
      setCand(f);
    } catch {
      onToast("刚才没抽到人，再试一次");
    } finally {
      setBusy(false);
    }
  }, [lang, onToast]);

  useEffect(() => {
    if (!cand) void pick();
  }, [cand, pick]);

  const hear = async () => {
    if (!cand) return;
    try {
      const r = await fetch(`/api/v2/friends/${cand.id}/messages?limit=5`, { cache: "no-store" });
      const j = await r.json();
      const v = (j.messages || []).find((m: Msg) => m.audio_url);
      if (!v) {
        onToast("TA 还没说话，先跟 TA 打个招呼");
        return;
      }
      const a = new Audio(v.audio_url);
      audioRef.current = a;
      void a.play();
    } catch {
      onToast("暂时听不到");
    }
  };

  return (
    <div className="v3-meet">
      <h1 className="v3-meet-title">认识一个说外语的人</h1>
      <p className="v3-meet-sub">
        TA 会先跟你打招呼，用母语跟你说话。你不用准备，随便回一句就行。
      </p>

      {cand ? (
        <div className="v3-card">
          <div className="v3-card-head">
            <span className="v3-ava lg" style={{ background: hueOf(cand.id) }}>
              {initial(cand.name)}
            </span>
            <div>
              <div className="v3-card-name">{cand.name}</div>
              <div className="v3-card-meta">
                {[cand.nationality, cand.city, cand.age ? `${cand.age} 岁` : ""]
                  .filter(Boolean)
                  .join(" · ")}
              </div>
            </div>
          </div>
          {cand.bio ? <p className="v3-card-bio">{cand.bio}</p> : null}
          <button className="v3-hear" onClick={() => void hear()}>
            <IcoPlay /> 听听 TA 的声音
          </button>
          <div className="v3-actions">
            <button className="v3-btn-ghost" onClick={() => setCand(null)} disabled={busy}>
              {busy ? "…" : "换一个"}
            </button>
            <button className="v3-btn-main" onClick={() => onDone(cand)}>
              认识 TA
            </button>
          </div>
        </div>
      ) : (
        <div className="v3-card">
          <div className="v3-loading">正在找一个人…</div>
        </div>
      )}

      <div className="v3-filters">
        <span>想练：</span>
        {LANGS.slice(0, 4).map((l) => (
          <button
            key={l.code}
            onClick={() => {
              setLang(lang === l.code ? "" : l.code);
              setCand(null);
            }}
            style={lang === l.code ? { color: "var(--accent)", fontWeight: 500 } : undefined}
          >
            {l.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ================================================================ 我 */
function MeScreen({
  profile,
  onSaved,
  onToast,
}: {
  profile: Profile;
  onSaved: (p: Profile) => void;
  onToast: (s: string) => void;
}) {
  const [nick, setNick] = useState(profile.nickname || "");
  const [targets, setTargets] = useState<string[]>(
    Array.isArray(profile.target_languages) && profile.target_languages.length
      ? profile.target_languages
      : ["en"],
  );
  const [saving, setSaving] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const dirty = useMemo(() => {
    const a = (profile.target_languages || []).join(",");
    return nick !== (profile.nickname || "") || targets.join(",") !== a;
  }, [nick, targets, profile]);

  const save = async () => {
    setSaving(true);
    try {
      const r = await fetch("/api/v2/memories", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nickname: nick.trim() || profile.nickname || "我",
          target_languages: targets,
        }),
      });
      if (!r.ok) throw new Error(String(r.status));
      const j = await r.json();
      onSaved(j.profile || { ...profile, nickname: nick, target_languages: targets });
    } catch {
      onToast("没保存上，再试一次");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="v3-me">
      <h1 className="v3-me-title">我</h1>
      <p className="v3-me-sub">告诉我你在练什么语言，遇到的人就会尽量说这种话。</p>

      <div className="v3-label">我在练</div>
      <div className="v3-chips">
        {LANGS.map((l) => (
          <button
            key={l.code}
            className={`v3-chip ${targets.includes(l.code) ? "on" : ""}`}
            onClick={() =>
              setTargets((prev) =>
                prev.includes(l.code)
                  ? prev.length > 1
                    ? prev.filter((c) => c !== l.code)
                    : prev
                  : [...prev, l.code],
              )
            }
          >
            {l.label}
          </button>
        ))}
      </div>
      <div className="v3-note">可以多选。选英语，遇到的人多数会跟你说英语。</div>

      <div className="v3-label">你的名字</div>
      <input
        className="v3-input"
        style={{ width: "100%", borderRadius: 12 }}
        value={nick}
        onChange={(e) => setNick(e.target.value)}
        placeholder="别人怎么称呼你"
      />

      <button className="v3-save" onClick={() => void save()} disabled={saving || !dirty}>
        {saving ? "保存中…" : dirty ? "保存" : "已保存"}
      </button>

      <div className="v3-minor">
        <button onClick={() => setShowSettings(true)}>
          声音设置 <span>试听 / 换声音 ›</span>
        </button>
      </div>

      {showSettings ? <SettingsSheet onClose={() => setShowSettings(false)} onToast={onToast} /> : null}
    </div>
  );
}

/* ================================================================ 声音设置（内联） */
function SettingsSheet({ onClose, onToast }: { onClose: () => void; onToast: (s: string) => void }) {
  const [provider, setProvider] = useState<"edge" | "fish">("edge");
  const [masked, setMasked] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState<null | "save" | "test">(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [audio, setAudio] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const r = await fetch("/api/v2/settings/tts", { cache: "no-store" });
        const j = await r.json();
        const t = j.tts || {};
        setProvider(t.provider === "fish" ? "fish" : "edge");
        setMasked(t.fish_api_key || "");
      } catch {
        /* 忽略 */
      }
    })();
  }, []);

  const save = async () => {
    setBusy("save");
    setErr("");
    setMsg("");
    try {
      const body: Record<string, unknown> = { provider };
      if (apiKey.trim()) body.api_key = apiKey.trim();
      const r = await fetch("/api/v2/settings/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || String(r.status));
      setMasked((j.tts || {}).fish_api_key || masked);
      setApiKey("");
      setMsg("保存好了");
    } catch (e) {
      setErr("没保存上：" + (e instanceof Error ? e.message : String(e)));
    } finally {
      setBusy(null);
    }
  };

  const test = async () => {
    setBusy("test");
    setErr("");
    setMsg("");
    setAudio("");
    try {
      const r = await fetch("/api/v2/settings/tts/test", { method: "POST" });
      const j = await r.json();
      if (!j.ok) {
        setErr(String(j.detail || "合成失败"));
        return;
      }
      if (j.audio_url) {
        setAudio(j.audio_url);
        onToast("试听一下");
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="v3-sheet">
      <div className="v3-nav">
        <button className="v3-icon" onClick={onClose} aria-label="返回">
          <IcoBack />
        </button>
        <div className="v3-nav-center">
          <div className="v3-nav-title">声音设置</div>
        </div>
        <span className="v3-icon" />
      </div>

      <div className="v3-sheet-body">
        <p className="v3-me-sub">换一种说话的方式。改完下一条语音就生效。</p>

        <div className="v3-label">用哪种声音</div>
        <div className="v3-seg">
          <button
            className={provider === "edge" ? "on" : ""}
            onClick={() => setProvider("edge")}
          >
            普通 · 免费
          </button>
          <button
            className={provider === "fish" ? "on" : ""}
            onClick={() => setProvider("fish")}
          >
            更自然
          </button>
        </div>
        <div className="v3-hint">
          「更自然」会带情绪，听起来像真人在说话；「普通」是系统朗读，稳定但偏机械。
        </div>

        {provider === "fish" ? (
          <>
            <div className="v3-field">
              <label>服务密钥</label>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={masked || "粘贴密钥"}
                autoComplete="off"
              />
              <div className="v3-hint">留空表示继续用原来的密钥。</div>
            </div>
            <button className="v3-try" onClick={() => void test()} disabled={busy !== null}>
              {busy === "test" ? "正在生成…" : "听一句试试"}
            </button>
            {audio ? <audio style={{ width: "100%", marginTop: 12 }} controls autoPlay src={audio} /> : null}
          </>
        ) : null}

        {msg ? <p className="v3-ok">{msg}</p> : null}
        {err ? <p className="v3-err">{err}</p> : null}

        <button className="v3-save" onClick={() => void save()} disabled={busy !== null}>
          {busy === "save" ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  );
}
