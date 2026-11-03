"use client";

/**
 * 微信风格聊天原型（高保真）。
 *
 * 本次新增
 * --------
 * 1. **按住说话**：按下计时，手指上滑超过 60px 变红=取消，松手发送
 * 2. **图片消息**：选图 → 预览 → 随文字一起发；后端多模态已通，AI 能看图
 * 3. **翻译**：每条消息可单独「译」，顶栏也有全局开关；走真实 /api/translate
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Image as ImageIcon,
  Keyboard,
  Languages,
  Mic,
  Plus,
  Search,
  Send,
  Smile,
  Trash2,
  Volume2,
  X,
} from "lucide-react";

/* ------------------------------------------------------------------ 类型 */

type MsgKind = "text" | "voice" | "image";

interface Msg {
  id: string;
  from: "me" | "them";
  kind: MsgKind;
  text?: string;
  duration?: number;
  image?: string;
  time: string;
}

interface Conversation {
  id: string;
  name: string;
  region: string;
  avatar: string;
  voicePack: string;
  traits: string;
  openingLine: string;
  last: string;
  time: string;
  unread: number;
  messages: Msg[];
}

const VOICE_PACKS: Array<{ id: string; label: string; region: string; kind: "official" | "fallback" }> = [
  { id: "en-GB-RyanNeural", label: "英式 RP · Ryan", region: "GB", kind: "official" },
  { id: "en-GB-SoniaNeural", label: "英式 · Sonia（女）", region: "GB", kind: "official" },
  { id: "en-US-AriaNeural", label: "美式 · Aria（女）", region: "US", kind: "official" },
  { id: "en-US-BrianNeural", label: "美式 · Brian", region: "US", kind: "official" },
  { id: "en-IN-PrabhatNeural", label: "印度英语 · Prabhat", region: "IN", kind: "official" },
  { id: "en-IE-ConnorNeural", label: "爱尔兰 · Connor", region: "IE", kind: "official" },
  { id: "en-AU-NatashaNeural", label: "澳洲 · Natasha（女）", region: "AU", kind: "official" },
  { id: "en-SG-LunaNeural", label: "新加坡 · Luna（女）", region: "SG", kind: "official" },
  { id: "en-ZA-LukeNeural", label: "南非 · Luke", region: "ZA", kind: "official" },
  { id: "en-NG-AbeoNeural", label: "尼日利亚 · Abeo", region: "NG", kind: "official" },
  { id: "it-IT-GiuseppeMultilingualNeural", label: "意大利腔 · Giuseppe", region: "IT", kind: "fallback" },
  { id: "fr-FR-HenriNeural", label: "法国腔 · Henri", region: "FR", kind: "fallback" },
  { id: "de-DE-ConradNeural", label: "德国腔 · Conrad", region: "DE", kind: "fallback" },
  { id: "ja-JP-KeitaNeural", label: "日式腔 · Keita", region: "JP", kind: "fallback" },
];

const voicePackOf = (id: string) => VOICE_PACKS.find((v) => v.id === id);

const AVATAR_PAIRS: Array<[string, string]> = [
  ["#6fa86e", "#4f7f4e"],
  ["#d08462", "#a85c3e"],
  ["#4b93b0", "#2c6d88"],
  ["#8e84bb", "#6b60a0"],
  ["#d09a52", "#a87434"],
  ["#b86076", "#8e4158"],
  ["#5c8fb8", "#3f6f94"],
];

const pairFor = (id: string): [string, string] => {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % AVATAR_PAIRS.length;
  return AVATAR_PAIRS[h];
};

/* ------------------------------------------------------------------ 示例数据 */

const INITIAL: Conversation[] = [
  {
    id: "oliver",
    name: "Oliver",
    region: "GB",
    avatar: "O",
    voicePack: "en-GB-RyanNeural",
    traits: "温暖, 放松, 幽默",
    openingLine: "Morning! What can I get for you today?",
    last: "Right then, one flat white coming up.",
    time: "23:08",
    unread: 2,
    messages: [
      { id: "o1", from: "them", kind: "text", text: "Morning! What can I get for you today?", time: "23:05" },
      { id: "o2", from: "me", kind: "text", text: "Hi, can I get a flat white please?", time: "23:06" },
      {
        id: "o3",
        from: "them",
        kind: "voice",
        duration: 6,
        text: "Right then, one flat white coming up. Would you like anything to eat with that? The almond croissants just came out of the oven.",
        time: "23:06",
      },
      {
        id: "o4",
        from: "them",
        kind: "voice",
        duration: 4,
        text: "Actually — I should say, we're out of oat milk today. Sorry about that.",
        time: "23:08",
      },
    ],
  },
  {
    id: "raj",
    name: "Raj",
    region: "IN",
    avatar: "R",
    voicePack: "en-IN-PrabhatNeural",
    traits: "直接, 语速快, 技术宅",
    openingLine: "So basically, we are planning to prepone the sprint review.",
    last: "Yes yes, please do the needful.",
    time: "22:41",
    unread: 0,
    messages: [
      { id: "r1", from: "them", kind: "voice", duration: 7, text: "So basically, we are planning to prepone the sprint review to Thursday. Is that fine with you?", time: "22:40" },
      { id: "r2", from: "me", kind: "text", text: "Thursday works for me.", time: "22:41" },
    ],
  },
  {
    id: "marco",
    name: "Marco",
    region: "IT",
    avatar: "M",
    voicePack: "it-IT-GiuseppeMultilingualNeural",
    traits: "热情, 手比划, 审美挑剔",
    openingLine: "Ciao! Let me show you what I have been working on.",
    last: "[语音] 8″",
    time: "21:12",
    unread: 0,
    messages: [
      {
        id: "m1",
        from: "them",
        kind: "voice",
        duration: 8,
        text: "Ciao! Listen, I think the layout needs more space, you know? The white, it is too much. Let us try something warmer.",
        time: "21:12",
      },
    ],
  },
  {
    id: "emily",
    name: "Emily",
    region: "US",
    avatar: "E",
    voicePack: "en-US-AriaNeural",
    traits: "干练, 友好, 爱追问",
    openingLine: "Hey! Do you have five minutes?",
    last: "Sounds good, let's circle back tomorrow.",
    time: "20:03",
    unread: 0,
    messages: [
      { id: "e1", from: "them", kind: "text", text: "Hey! Do you have five minutes to walk me through the deck?", time: "20:01" },
      { id: "e2", from: "me", kind: "text", text: "Sure, give me a sec.", time: "20:02" },
      { id: "e3", from: "them", kind: "text", text: "Sounds good, let's circle back tomorrow.", time: "20:03" },
    ],
  },
  {
    id: "priya",
    name: "Priya",
    region: "SG",
    avatar: "P",
    voicePack: "en-SG-LunaNeural",
    traits: "务实, 利落",
    openingLine: "We push the launch to next week, can or not?",
    last: "Can or not?",
    time: "周二",
    unread: 0,
    messages: [
      { id: "p1", from: "them", kind: "text", text: "We push the launch to next week, can or not?", time: "周二" },
    ],
  },
];

/* ------------------------------------------------------------------ 样式 */

const GLOBAL_CSS = `
.wx-scroll{scrollbar-width:thin;scrollbar-color:rgba(0,0,0,.14) transparent}
.wx-scroll::-webkit-scrollbar{width:9px;height:9px}
.wx-scroll::-webkit-scrollbar-track{background:transparent}
.wx-scroll::-webkit-scrollbar-thumb{background:rgba(0,0,0,.13);border-radius:5px;border:2.5px solid transparent;background-clip:content-box}
.wx-scroll::-webkit-scrollbar-thumb:hover{background:rgba(0,0,0,.22);background-clip:content-box}
.wx-root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.wx-root .wx-line{border-color:rgba(0,0,0,.075)}
.wx-root .wx-line:focus,.wx-root .wx-line:focus-visible{border-color:#07c160;box-shadow:0 0 0 2px rgba(7,193,96,.13)}
.wx-root .wx-line-soft{border-color:rgba(0,0,0,.06)}
.wx-root .wx-noselect{user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}
@keyframes wx-rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.wx-rise{animation:wx-rise .16s ease-out}
@keyframes wx-bar{from{transform:scaleY(.4)}to{transform:scaleY(1)}}
`;

const CANCEL_DY = 60;

const nowHM = () => {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

/* ------------------------------------------------------------------ 主组件 */

export function WeChatPrototype() {
  const [convs, setConvs] = useState<Conversation[]>(INITIAL);
  const [activeId, setActiveId] = useState("oliver");
  const [inputMode, setInputMode] = useState<"text" | "voice">("text");
  const [draft, setDraft] = useState("");
  const [playingId, setPlayingId] = useState<string>("");
  const [showTranscribe, setShowTranscribe] = useState<Record<string, boolean>>({});
  const [editor, setEditor] = useState<{ mode: "create" | "edit"; id: string } | null>(null);

  /** 录音 */
  const [rec, setRec] = useState<{ active: boolean; cancel: boolean; secs: number }>({ active: false, cancel: false, secs: 0 });
  /** 待发送图片 */
  const [pendingImg, setPendingImg] = useState<string | null>(null);
  /** 全局自动翻译 */
  const [translateAll, setTranslateAll] = useState(false);
  const [tr, setTr] = useState<Record<string, string>>({});
  const [trBusy, setTrBusy] = useState<Record<string, boolean>>({});

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const recStart = useRef<{ y: number; t: number }>({ y: 0, t: 0 });
  const recTimer = useRef<number>(0);
  const recActiveRef = useRef(false);

  const active = convs.find((c) => c.id === activeId) ?? convs[0];

  useEffect(() => {
    if (!playingId) return;
    const t = setTimeout(() => setPlayingId(""), 1600);
    return () => clearTimeout(t);
  }, [playingId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 99999, behavior: "smooth" });
  }, [activeId, active?.messages.length]);

  /* ---------------------------------------------------------------- 翻译 -- */

  const translateOne = useCallback(async (id: string, text: string) => {
    if (!text.trim()) return;
    setTrBusy((b) => ({ ...b, [id]: true }));
    try {
      const r = await fetch("/api/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const j = (await r.json()) as { ok?: boolean; translation?: string };
      setTr((t) => ({ ...t, [id]: j.ok && j.translation ? j.translation : "翻译暂不可用" }));
    } catch {
      setTr((t) => ({ ...t, [id]: "翻译暂不可用" }));
    } finally {
      setTrBusy((b) => ({ ...b, [id]: false }));
    }
  }, []);

  useEffect(() => {
    if (!translateAll || !active) return;
    for (const m of active.messages) {
      if (m.from !== "them" || !m.text) continue;
      if (tr[m.id] || trBusy[m.id]) continue;
      void translateOne(m.id, m.text);
    }
  }, [translateAll, active, tr, trBusy, translateOne]);

  /* ---------------------------------------------------------------- 发送 -- */

  const append = useCallback(
    (msgs: Msg[]) => {
      const last = msgs[msgs.length - 1];
      setConvs((list) =>
        list.map((c) =>
          c.id === activeId
            ? {
                ...c,
                messages: [...c.messages, ...msgs],
                last: last?.image ? "[图片]" : (last?.text ?? ""),
                time: "刚刚",
                unread: 0,
              }
            : c,
        ),
      );
    },
    [activeId],
  );

  const sendMessage = () => {
    const text = draft.trim();
    if (!text && !pendingImg) return;
    const time = nowHM();
    const msgs: Msg[] = [];
    if (pendingImg) msgs.push({ id: `img-${Date.now()}`, from: "me", kind: "image", image: pendingImg, time });
    if (text) msgs.push({ id: `u-${Date.now()}`, from: "me", kind: "text", text, time });
    append(msgs);
    setDraft("");
    setPendingImg(null);
  };

  const pickImage = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => setPendingImg(String(reader.result));
    reader.readAsDataURL(f);
    e.target.value = "";
  };

  /* -------------------------------------------------------- 按住说话 ---- */

  const startRec = (e: React.PointerEvent<HTMLButtonElement>) => {
    // setPointerCapture 在某些环境（合成事件、旧浏览器）会抛错，
    // 不能让它中断录音状态；捕获失败时靠 window 级 pointerup 兜底。
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* 忽略：下面有 window 兜底 */
    }
    recStart.current = { y: e.clientY, t: Date.now() };
    recActiveRef.current = true;
    setRec({ active: true, cancel: false, secs: 0 });
    recTimer.current = window.setInterval(() => {
      setRec((r) => ({ ...r, secs: Math.floor((Date.now() - recStart.current.t) / 1000) }));
    }, 200);
  };

  const moveRec = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!rec.active) return;
    setRec((r) => ({ ...r, cancel: recStart.current.y - e.clientY > CANCEL_DY }));
  };

  /** 松手结算：cancelled 由调用方判定 */
  const finishRec = useCallback(
    (clientY: number) => {
      if (!recActiveRef.current) return;
      window.clearInterval(recTimer.current);
      const dur = Math.max(1, Math.round((Date.now() - recStart.current.t) / 1000));
      const cancelled = recStart.current.y - clientY > CANCEL_DY;
      recActiveRef.current = false;
      setRec({ active: false, cancel: false, secs: 0 });
      if (cancelled) return;
      append([
        {
          id: `v-${Date.now()}`,
          from: "me",
          kind: "voice",
          duration: dur,
          text: "（原型演示：真实版本这里放语音转写结果）",
          time: nowHM(),
        },
      ]);
    },
    [append],
  );

  const endRec = (e: React.PointerEvent<HTMLButtonElement>) => {
    finishRec(e.clientY);
  };

  const cancelRec = () => {
    window.clearInterval(recTimer.current);
    recActiveRef.current = false;
    setRec({ active: false, cancel: false, secs: 0 });
  };

  // 窗口级兜底：手指移出按钮后松手也能正确结算/取消
  useEffect(() => {
    if (!rec.active) return;
    recActiveRef.current = true;
    const onUp = (ev: PointerEvent) => finishRec(ev.clientY);
    const onCancel = () => cancelRec();
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    return () => {
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
    };
  }, [rec.active, finishRec]);

  useEffect(() => () => window.clearInterval(recTimer.current), []);

  /* ---------------------------------------------------------- 角色管理 -- */

  const savePersona = (p: { id: string; name: string; region: string; voicePack: string; traits: string; openingLine: string }) => {
    setConvs((list) => {
      if (list.some((c) => c.id === p.id)) {
        return list.map((c) =>
          c.id === p.id
            ? { ...c, name: p.name, region: p.region, voicePack: p.voicePack, traits: p.traits, openingLine: p.openingLine, avatar: p.name.trim()[0]?.toUpperCase() ?? c.avatar }
            : c,
        );
      }
      return [
        {
          id: p.id,
          name: p.name,
          region: p.region,
          avatar: p.name.trim()[0]?.toUpperCase() ?? "?",
          voicePack: p.voicePack,
          traits: p.traits,
          openingLine: p.openingLine,
          last: p.openingLine || "（还没聊过）",
          time: "刚刚",
          unread: 0,
          messages: p.openingLine ? [{ id: `${p.id}-1`, from: "them" as const, kind: "text" as const, text: p.openingLine, time: "刚刚" }] : [],
        },
        ...list,
      ];
    });
    setActiveId(p.id);
    setEditor(null);
  };

  const deletePersona = (id: string) => {
    setConvs((list) => {
      const next = list.filter((c) => c.id !== id);
      if (activeId === id) setActiveId(next[0]?.id ?? "");
      return next;
    });
    setEditor(null);
  };

  const editing = editor ? convs.find((c) => c.id === editor.id) : undefined;

  /* -------------------------------------------------------------- 渲染 -- */

  return (
    <div className="wx-root flex h-screen w-full overflow-hidden bg-[#f5f5f5]">
      <style>{GLOBAL_CSS}</style>

      {/* ============================================ 左侧会话列表 */}
      <aside className="flex w-[292px] shrink-0 flex-col bg-[#ededed]">
        <div className="flex items-center gap-2 px-3 pb-2 pt-3">
          <div className="relative flex-1">
            <Search size={12.5} strokeWidth={2} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[#a0a0a0]" />
            <input
              placeholder="搜索"
              className="h-[26px] w-full rounded-[5px] border-0 bg-black/[0.055] pl-[26px] pr-2 text-[12px] text-[#2c2c2c] outline-none transition-colors placeholder:text-[#a0a0a0] focus:bg-black/[0.075]"
            />
          </div>
          <button
            type="button"
            onClick={() => setEditor({ mode: "create", id: "" })}
            title="添加角色"
            className="flex h-[26px] w-[26px] items-center justify-center rounded-[5px] text-[#5c5c5c] transition-colors hover:bg-black/[0.06] hover:text-[#07c160]"
          >
            <Plus size={15} strokeWidth={2} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto wx-scroll pb-1">
          {convs.length === 0 && (
            <p className="px-4 py-8 text-center text-[11.5px] leading-relaxed text-[#9a9a9a]">
              还没有角色
              <br />
              点右上角 + 添加一个
            </p>
          )}

          {convs.map((c) => {
            const on = c.id === activeId;
            const vp = voicePackOf(c.voicePack);
            const [g1, g2] = pairFor(c.id);
            return (
              <button
                key={c.id}
                type="button"
                onClick={() => setActiveId(c.id)}
                className={`flex w-full items-start gap-2.5 px-3 py-[6px] text-left transition-colors ${on ? "bg-black/[0.075]" : "hover:bg-black/[0.04]"}`}
              >
                <div className="relative shrink-0">
                  <div
                    className="flex h-[38px] w-[38px] items-center justify-center rounded-[6px] text-[15px] font-medium tracking-tight text-white"
                    style={{ backgroundImage: `linear-gradient(145deg, ${g1}, ${g2})`, boxShadow: "inset 0 0.5px 0 rgba(255,255,255,.22), 0 0.5px 1px rgba(0,0,0,.06)" }}
                  >
                    {c.avatar}
                  </div>
                  {c.unread > 0 && (
                    <span className="absolute -right-[5px] -top-[5px] flex h-[15px] min-w-[15px] items-center justify-center rounded-full bg-[#fa5151] px-[3.5px] text-[9.5px] font-medium leading-none text-white ring-[1.5px] ring-[#ededed]">
                      {c.unread}
                    </span>
                  )}
                </div>

                <div className="min-w-0 flex-1 pt-[1px]">
                  <div className="flex items-baseline gap-1.5">
                    <span className="truncate text-[13.5px] leading-[18px] text-[#191919]">{c.name}</span>
                    <span className="shrink-0 rounded-[3px] bg-black/[0.055] px-[3.5px] pb-[1px] text-[9px] font-medium leading-[12px] text-[#6e6e6e]">{c.region}</span>
                    <span className="ml-auto shrink-0 text-[10.5px] tabular-nums text-[#adadad]">{c.time}</span>
                  </div>
                  <div className="mt-[3px] truncate text-[12px] leading-[16px] text-[#8c8c8c]">{c.last}</div>
                  <div className="mt-[3px] flex items-center gap-1">
                    <span className="truncate text-[9.5px] leading-[13px] text-[#a5a5a5]">{vp?.label ?? "未设置语音"}</span>
                    {vp?.kind === "fallback" && <span className="shrink-0 text-[8.5px] leading-[13px] text-[#c08040]">兜底</span>}
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      </aside>

      {/* ============================================ 右侧聊天区 */}
      <main className="relative flex min-w-0 flex-1 flex-col bg-[#f5f5f5]">
        {!active ? (
          <div className="flex flex-1 items-center justify-center text-[12px] text-[#a8a8a8]">左侧还没有角色</div>
        ) : (
          <>
            <header className="flex h-[54px] shrink-0 items-center gap-2.5 px-5 shadow-[0_0.5px_0_rgba(0,0,0,.07)]">
              <button
                type="button"
                onClick={() => setEditor({ mode: "edit", id: active.id })}
                title="编辑角色"
                className="flex h-[30px] w-[30px] items-center justify-center rounded-[5px] text-[13px] font-medium text-white transition-transform active:scale-[0.97]"
                style={{ backgroundImage: `linear-gradient(145deg, ${pairFor(active.id)[0]}, ${pairFor(active.id)[1]})`, boxShadow: "inset 0 0.5px 0 rgba(255,255,255,.22), 0 0.5px 1px rgba(0,0,0,.06)" }}
              >
                {active.avatar}
              </button>
              <button
                type="button"
                onClick={() => setEditor({ mode: "edit", id: active.id })}
                className="text-[14px] font-medium leading-[18px] text-[#191919] transition-colors hover:text-[#07c160]"
              >
                {active.name}
              </button>
              <span className="rounded-[3px] bg-black/[0.055] px-[3.5px] pb-[1px] text-[9px] font-medium leading-[12px] text-[#6e6e6e]">{active.region}</span>
              <span className="text-[10.5px] leading-[14px] text-[#a0a0a0]">{voicePackOf(active.voicePack)?.label ?? "未设置语音"}</span>

              <div className="ml-auto flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => setTranslateAll((v) => !v)}
                  title={translateAll ? "关闭自动翻译" : "自动翻译所有回复"}
                  className={`flex h-[30px] items-center gap-1 rounded-[6px] px-2 text-[10.5px] transition-colors ${
                    translateAll ? "bg-[#07c160]/[0.12] text-[#07c160]" : "text-[#5c5c5c] hover:bg-black/[0.045]"
                  }`}
                >
                  <Languages size={13} strokeWidth={1.8} />
                  翻译
                </button>
                <IconBtn title="语音通话">
                  <Volume2 size={15.5} strokeWidth={1.75} />
                </IconBtn>
                <IconBtn title="编辑角色" onClick={() => setEditor({ mode: "edit", id: active.id })}>
                  <Plus size={15.5} strokeWidth={1.75} />
                </IconBtn>
              </div>
            </header>

            <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto wx-scroll px-6 py-5">
              <div className="mx-auto max-w-[680px] space-y-[14px]">
                <div className="flex justify-center pt-1">
                  <span className="rounded-[4px] bg-black/[0.045] px-2 py-[2px] text-[10.5px] leading-[15px] text-[#9a9a9a]">今天 23:05</span>
                </div>

                {active.messages.length === 0 && <p className="py-14 text-center text-[11.5px] text-[#a8a8a8]">打个招呼，开始练习</p>}

                {active.messages.map((m) => (
                  <MessageRow
                    key={m.id}
                    msg={m}
                    avatar={active.avatar}
                    convId={active.id}
                    playing={playingId === m.id}
                    onPlay={() => setPlayingId(m.id)}
                    transcribed={!!showTranscribe[m.id]}
                    onToggleTranscribe={() => setShowTranscribe((s) => ({ ...s, [m.id]: !s[m.id] }))}
                    onAvatarClick={() => setEditor({ mode: "edit", id: active.id })}
                    translation={tr[m.id]}
                    translating={!!trBusy[m.id]}
                    onTranslate={() => void translateOne(m.id, m.text ?? "")}
                  />
                ))}
              </div>
            </div>

            <footer className="shrink-0 px-4 pb-3 pt-2">
              <div className="mx-auto max-w-[680px]">
                {pendingImg && (
                  <div className="wx-rise mb-2 flex items-center gap-2 rounded-[6px] bg-black/[0.035] p-2">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={pendingImg} alt="待发送图片" className="h-[52px] w-[52px] rounded-[4px] object-cover" />
                    <span className="flex-1 text-[10.5px] leading-[15px] text-[#7a7a7a]">
                      图片已就绪 — 可以加一句话，或直接发送
                      <br />
                      <span className="text-[9.5px] text-[#a5a5a5]">AI 能看懂图片内容</span>
                    </span>
                    <button type="button" onClick={() => setPendingImg(null)} className="text-[#a0a0a0] hover:text-[#fa5151]">
                      <X size={13} />
                    </button>
                  </div>
                )}

                {inputMode === "voice" ? (
                  <div className="flex items-center gap-2">
                    <IconBtn title="切回键盘" onClick={() => setInputMode("text")}>
                      <Keyboard size={18} strokeWidth={1.75} />
                    </IconBtn>
                    <button
                      type="button"
                      onPointerDown={startRec}
                      onPointerMove={moveRec}
                      onPointerUp={endRec}
                      onPointerCancel={cancelRec}
                      className={`wx-noselect wx-line-soft h-[34px] flex-1 select-none rounded-[6px] border-[0.5px] bg-white text-[12.5px] transition-colors ${
                        rec.active ? (rec.cancel ? "text-[#fa5151]" : "bg-[#e8e8e8] text-[#4a4a4a]") : "text-[#4a4a4a] active:bg-[#f0f0f0]"
                      }`}
                    >
                      {rec.active ? (rec.cancel ? "松开手指，取消发送" : `松开发送 · ${rec.secs}″`) : "按住 说话"}
                    </button>
                    <IconBtn title="表情">
                      <Smile size={18} strokeWidth={1.75} />
                    </IconBtn>
                    <IconBtn title="图片" onClick={() => fileRef.current?.click()}>
                      <ImageIcon size={18} strokeWidth={1.75} />
                    </IconBtn>
                  </div>
                ) : (
                  <div className="flex items-end gap-2">
                    <IconBtn title="切换语音" onClick={() => setInputMode("voice")}>
                      <Mic size={18} strokeWidth={1.75} />
                    </IconBtn>
                    <textarea
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          sendMessage();
                        }
                      }}
                      rows={1}
                      placeholder={pendingImg ? "给这张图配一句话…" : ""}
                      className="wx-line max-h-[88px] min-h-[34px] flex-1 resize-none rounded-[6px] border-[0.5px] bg-white px-2.5 py-[7px] text-[13px] leading-[19px] text-[#191919] outline-none shadow-[0_0.5px_1px_rgba(0,0,0,.03)] transition-shadow placeholder:text-[#b5b5b5]"
                    />
                    <IconBtn title="表情">
                      <Smile size={18} strokeWidth={1.75} />
                    </IconBtn>
                    <IconBtn title="图片" onClick={() => fileRef.current?.click()}>
                      <ImageIcon size={18} strokeWidth={1.75} />
                    </IconBtn>
                    {draft.trim() || pendingImg ? (
                      <button
                        type="button"
                        onClick={sendMessage}
                        className="flex h-[34px] shrink-0 items-center gap-1 rounded-[6px] bg-[#07c160] px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-[#06ad56]"
                      >
                        <Send size={12} strokeWidth={2} />
                        发送
                      </button>
                    ) : (
                      <IconBtn title="更多">
                        <Plus size={18} strokeWidth={1.75} />
                      </IconBtn>
                    )}
                  </div>
                )}

                <input ref={fileRef} type="file" accept="image/*" onChange={pickImage} className="hidden" />
              </div>
            </footer>
          </>
        )}
      </main>

      {/* ============================================ 录音浮层 */}
      {rec.active && (
        <div className="pointer-events-none fixed inset-0 z-40 flex items-center justify-center">
          <div
            className={`wx-rise flex w-[172px] flex-col items-center gap-2 rounded-[14px] px-4 py-5 shadow-[0_12px_40px_-8px_rgba(0,0,0,.45)] backdrop-blur-sm transition-colors ${
              rec.cancel ? "bg-[#c8443c]/95" : "bg-black/[0.78]"
            }`}
          >
            <Mic size={26} strokeWidth={1.6} className={rec.cancel ? "text-white" : "animate-pulse text-[#4ade80]"} />
            <div className="tabular-nums text-[24px] font-light leading-none text-white">{rec.secs}″</div>
            <div className="mt-1 flex h-[16px] items-end gap-[2px]">
              {[7, 12, 16, 9, 13, 8, 11].map((h, i) => (
                <span
                  key={i}
                  className="w-[2px] origin-bottom rounded-full bg-white/70"
                  style={{ height: h, animation: `wx-bar .55s ease-in-out ${i * 70}ms infinite alternate` }}
                />
              ))}
            </div>
            <div className="mt-1 text-center text-[10.5px] leading-[15px] text-white/85">
              {rec.cancel ? "松开手指，取消发送" : "手指上滑，取消发送"}
            </div>
          </div>
        </div>
      )}

      {editor && (
        <PersonaEditor mode={editor.mode} initial={editing} onClose={() => setEditor(null)} onSave={savePersona} onDelete={deletePersona} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 图标按钮 */

function IconBtn({ children, title, onClick }: { children: React.ReactNode; title: string; onClick?: () => void }) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[6px] text-[#5c5c5c] transition-colors hover:bg-black/[0.045] hover:text-[#191919] active:bg-black/[0.07]"
    >
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------ 角色编辑弹窗 */

const FIELD =
  "wx-line w-full rounded-[6px] border-[0.5px] bg-white px-2.5 py-[7px] text-[12px] leading-[17px] text-[#191919] outline-none transition-shadow placeholder:text-[#bcbcbc]";

function PersonaEditor({
  mode,
  initial,
  onClose,
  onSave,
  onDelete,
}: {
  mode: "create" | "edit";
  initial?: Conversation;
  onClose: () => void;
  onSave: (p: { id: string; name: string; region: string; voicePack: string; traits: string; openingLine: string }) => void;
  onDelete: (id: string) => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [region, setRegion] = useState(initial?.region ?? "GB");
  const [voicePack, setVoicePack] = useState(initial?.voicePack ?? VOICE_PACKS[0].id);
  const [traits, setTraits] = useState(initial?.traits ?? "");
  const [openingLine, setOpeningLine] = useState(initial?.openingLine ?? "");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const vp = voicePackOf(voicePack);
  const grouped = useMemo(
    () => ({ official: VOICE_PACKS.filter((v) => v.kind === "official"), fallback: VOICE_PACKS.filter((v) => v.kind === "fallback") }),
    [],
  );
  const canSave = name.trim().length > 0;
  const [g1, g2] = pairFor(initial?.id ?? (name || "new"));

  const submit = () => {
    if (!canSave) return;
    const id = initial?.id ?? `u_${name.trim().toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 10) || Date.now()}`;
    onSave({ id, name: name.trim(), region, voicePack, traits, openingLine });
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <button type="button" aria-label="关闭" onClick={onClose} className="absolute inset-0 bg-black/40 backdrop-blur-[1px]" />
      <div className="relative flex max-h-[86vh] w-full max-w-[480px] flex-col overflow-hidden rounded-[10px] bg-[#fafafa] shadow-[0_12px_48px_-8px_rgba(0,0,0,.28)]">
        <div className="flex items-center gap-3 px-5 pb-3 pt-4">
          <div
            className="flex h-[42px] w-[42px] items-center justify-center rounded-[7px] text-[16px] font-medium text-white"
            style={{ backgroundImage: `linear-gradient(145deg, ${g1}, ${g2})`, boxShadow: "inset 0 0.5px 0 rgba(255,255,255,.22), 0 1px 2px rgba(0,0,0,.08)" }}
          >
            {name.trim()[0]?.toUpperCase() ?? "?"}
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="text-[14px] font-semibold leading-[19px] tracking-tight text-[#191919]">{mode === "create" ? "添加角色" : "编辑角色"}</h2>
            <p className="mt-[1px] text-[10.5px] leading-[15px] text-[#a0a0a0]">{mode === "create" ? "设计一个对话伙伴" : "改设定、换语音包，或删除"}</p>
          </div>
          <button type="button" onClick={onClose} className="flex h-[26px] w-[26px] items-center justify-center rounded-[5px] text-[#a0a0a0] transition-colors hover:bg-black/[0.05] hover:text-[#4a4a4a]">
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-[13px] overflow-y-auto wx-scroll px-5 py-3">
          <Field label="名字">
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={20} placeholder="如 Oliver" className={FIELD} />
          </Field>
          <div className="grid grid-cols-[88px_1fr] gap-2.5">
            <Field label="地区代码">
              <input value={region} onChange={(e) => setRegion(e.target.value.toUpperCase().slice(0, 2))} maxLength={2} placeholder="GB" className={`${FIELD} text-center uppercase tracking-wide`} />
            </Field>
            <Field label="语音包（口音）">
              <select value={voicePack} onChange={(e) => setVoicePack(e.target.value)} className={FIELD}>
                <optgroup label="官方英语变体">
                  {grouped.official.map((v) => (
                    <option key={v.id} value={v.id}>{v.label}</option>
                  ))}
                </optgroup>
                <optgroup label="外语腔（兜底）">
                  {grouped.fallback.map((v) => (
                    <option key={v.id} value={v.id}>{v.label}</option>
                  ))}
                </optgroup>
              </select>
            </Field>
          </div>
          {vp && (
            <p className="-mt-[3px] pl-[1px] text-[10px] leading-[15px] text-[#a0a0a0]">
              {vp.kind === "official" ? "真实地区口音，发音自然" : "口音浓烈，个别单词可能读不准"}
            </p>
          )}
          <Field label="性格">
            <input value={traits} onChange={(e) => setTraits(e.target.value)} maxLength={60} placeholder="温暖, 放松, 幽默" className={FIELD} />
          </Field>
          <Field label="开场白">
            <textarea value={openingLine} onChange={(e) => setOpeningLine(e.target.value)} maxLength={200} rows={2} placeholder="Morning! What can I get for you today?" className={`${FIELD} resize-none`} />
          </Field>
        </div>

        <div className="flex items-center gap-2 px-5 pb-4 pt-2">
          {mode === "edit" && initial && (
            <button
              type="button"
              onClick={() => (confirmDelete ? onDelete(initial.id) : setConfirmDelete(true))}
              className={`flex items-center gap-1 rounded-[6px] px-2 py-[6px] text-[11px] font-medium transition-colors ${
                confirmDelete ? "bg-[#fa5151] text-white" : "text-[#fa5151] hover:bg-[#fa5151]/[0.08]"
              }`}
            >
              <Trash2 size={11.5} strokeWidth={1.9} />
              {confirmDelete ? "确认删除" : "删除"}
            </button>
          )}
          <div className="ml-auto flex items-center gap-2">
            <button type="button" onClick={onClose} className="wx-line-soft rounded-[6px] border-[0.5px] bg-white px-3 py-[6px] text-[11.5px] text-[#4a4a4a] transition-colors hover:bg-[#f3f3f3]">
              取消
            </button>
            <button
              type="button"
              onClick={submit}
              disabled={!canSave}
              className="rounded-[6px] bg-[#07c160] px-3.5 py-[6px] text-[11.5px] font-medium text-white transition-colors hover:bg-[#06ad56] disabled:cursor-not-allowed disabled:bg-[#cfcfcf]"
            >
              {mode === "create" ? "添加" : "保存"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-[5px] block text-[10px] font-medium tracking-wide text-[#8f8f8f]">{label}</label>
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ 单条消息 */

function MessageRow({
  msg,
  avatar,
  convId,
  playing,
  onPlay,
  transcribed,
  onToggleTranscribe,
  onAvatarClick,
  translation,
  translating,
  onTranslate,
}: {
  msg: Msg;
  avatar: string;
  convId: string;
  playing: boolean;
  onPlay: () => void;
  transcribed: boolean;
  onToggleTranscribe: () => void;
  onAvatarClick: () => void;
  translation?: string;
  translating?: boolean;
  onTranslate: () => void;
}) {
  const mine = msg.from === "me";
  const [g1, g2] = pairFor(convId);
  const hasText = !!(msg.text && msg.text.trim());

  return (
    <div className={`flex items-start gap-2.5 ${mine ? "flex-row-reverse" : "flex-row"}`}>
      <button
        type="button"
        onClick={mine ? undefined : onAvatarClick}
        title={mine ? undefined : "编辑角色"}
        className="mt-[1px] flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[5px] text-[13px] font-medium text-white transition-opacity hover:opacity-85"
        style={
          mine
            ? { backgroundImage: "linear-gradient(145deg, #7f9db3, #5d7d94)", boxShadow: "inset 0 0.5px 0 rgba(255,255,255,.22), 0 0.5px 1px rgba(0,0,0,.06)" }
            : { backgroundImage: `linear-gradient(145deg, ${g1}, ${g2})`, boxShadow: "inset 0 0.5px 0 rgba(255,255,255,.22), 0 0.5px 1px rgba(0,0,0,.06)" }
        }
      >
        {mine ? "我" : avatar}
      </button>

      <div className={`flex min-w-0 max-w-[76%] flex-col ${mine ? "items-end" : "items-start"}`}>
        {msg.kind === "text" && (
          <div
            className={`relative rounded-[7px] px-3 py-[7px] text-[13px] leading-[20px] text-[#191919] ${mine ? "bg-[#95ec69]" : "bg-white"}`}
            style={{ filter: "drop-shadow(0 1px 1.5px rgba(0,0,0,.075))" }}
          >
            <Tail mine={mine} />
            {msg.text}
          </div>
        )}

        {msg.kind === "voice" && (
          <button
            type="button"
            onClick={onPlay}
            className={`relative flex items-center gap-2 rounded-[7px] px-3 py-[8px] transition-[filter] active:brightness-[0.97] ${mine ? "bg-[#95ec69]" : "bg-white"}`}
            style={{ filter: "drop-shadow(0 1px 1.5px rgba(0,0,0,.075))", minWidth: Math.min(230, 82 + (msg.duration ?? 3) * 12) }}
          >
            <Tail mine={mine} />
            <WaveBars playing={playing} mine={mine} />
            <span className="tabular-nums text-[12.5px] leading-[16px] text-[#4a4a4a]">{msg.duration}″</span>
          </button>
        )}

        {msg.kind === "voice" && msg.text && (
          <div className={`mt-[5px] max-w-full px-[2px] text-[12.5px] leading-[19px] ${transcribed ? "text-[#5f5f5f]" : "text-[#8a8a8a]"}`}>{msg.text}</div>
        )}

        {msg.kind === "image" && msg.image && (
          <div className="overflow-hidden rounded-[7px] bg-white p-[3px]" style={{ filter: "drop-shadow(0 1px 1.5px rgba(0,0,0,.075))" }}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={msg.image} alt="图片消息" className="max-h-[220px] max-w-[264px] rounded-[5px] object-cover" />
          </div>
        )}

        {translation && (
          <div className="wx-rise mt-[5px] max-w-full rounded-[5px] border-l-[2px] border-[#07c160]/45 bg-black/[0.028] px-2 py-[5px] text-[12px] leading-[18px] text-[#5f5f5f]">
            {translation}
          </div>
        )}

        <div className={`mt-[5px] flex items-center gap-[9px] px-[2px] ${mine ? "flex-row-reverse" : ""}`}>
          <span className="text-[9.5px] leading-[13px] tabular-nums text-[#b4b4b4]">{msg.time}</span>
          {msg.kind === "voice" && (
            <button type="button" onClick={onToggleTranscribe} className="text-[9.5px] leading-[13px] text-[#a0a0a0] transition-colors hover:text-[#07c160]">
              {transcribed ? "隐藏文字" : "转文字"}
            </button>
          )}
          {hasText && (
            <button
              type="button"
              onClick={onTranslate}
              disabled={translating}
              className="text-[9.5px] leading-[13px] text-[#a0a0a0] transition-colors hover:text-[#07c160] disabled:opacity-50"
            >
              {translating ? "翻译中…" : translation ? "重译" : "译"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Tail({ mine }: { mine: boolean }) {
  return <span aria-hidden className={`absolute top-[12px] h-[7px] w-[7px] rotate-45 ${mine ? "-right-[2px] bg-[#95ec69]" : "-left-[2px] bg-white"}`} />;
}

function WaveBars({ playing, mine }: { playing: boolean; mine: boolean }) {
  const heights = [5, 9, 13, 8, 11, 6];
  return (
    <span className="flex items-center gap-[1.5px]" aria-hidden>
      {heights.map((h, i) => (
        <span
          key={i}
          className={`w-[2px] rounded-full ${mine ? "bg-[#3f7a3a]" : "bg-[#7aa86e]"} ${playing ? "animate-pulse" : ""}`}
          style={{ height: h, animationDelay: playing ? `${i * 90}ms` : undefined, animationDuration: playing ? "700ms" : undefined }}
        />
      ))}
    </span>
  );
}
