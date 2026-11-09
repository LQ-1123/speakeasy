"use client";

/**
 * 微信风格聊天界面 —— 接真实引擎（不是原型）。
 *
 * 数据来源
 * --------
 * - 角色列表：`/api/personas`（含用户自定义角色）
 * - 消息：WebSocket（`useConversation`，与旧界面同一套已验证链路）
 * - 语音：LLM 流式回复 → 逐句 TTS → **默认自动播放**；点气泡可重播
 * - 翻译：`/api/translate`（真实接口，带缓存）
 * - 图片：FileReader → data URL → `text` 消息带 `images`（后端多模态）
 * - 按住说话：按下 `startRecording`，松手 `stopRecording`；上滑取消
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Image as ImageIcon,
  Keyboard,
  Loader2,
  Languages,
  Mic,
  Plus,
  Search,
  Send,
  Settings,
  Trash2,
  UserMinus,
  Smile,
  Volume2,
  X,
} from "lucide-react";

import { CreatePersonaDialog } from "@/components/CreatePersonaDialog";
import { ConfigLockedDialog } from "@/components/ConfigLockedDialog";
import { FALLBACK_MODES, FALLBACK_PERSONAS } from "@/lib/fallback";
import type { ChatMessage, Notice, PersonaPublic } from "@/lib/types";
import { useConversation } from "@/lib/useConversation";

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

const CANCEL_DY = 60;
const nowHM = () => {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

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

export function WeChatApp() {
  const cv = useConversation(FALLBACK_PERSONAS, FALLBACK_MODES);
  const {
    personas: apiPersonas,
    activePersonaId,
    messages,
    status,
    recording,
    micError,
    micDeviceId,
    setMicDeviceId,
    notices,
    dismissNotice,
    sendText,
    selectPersona,
    startRecording,
    stopRecording,
    cancelRecording,
    clearConversation,
    replayTurn,
    replayingTurn,
    turnDurations,
    interrupt,
    playerStats,
  } = cv;

  const personas: PersonaPublic[] = apiPersonas.length > 0 ? apiPersonas : FALLBACK_PERSONAS;
  const active = personas.find((p) => p.id === activePersonaId) ?? personas[0];

  const [draft, setDraft] = useState("");
  const [inputMode, setInputMode] = useState<"text" | "voice">("text");
  const [pendingImg, setPendingImg] = useState<string | null>(null);
  const [translateAll, setTranslateAll] = useState(true); // 默认开：学英语要能对照
  const [tr, setTr] = useState<Record<string, string>>({});
  const [trBusy, setTrBusy] = useState<Record<string, boolean>>({});
  const [rec, setRec] = useState<{ active: boolean; cancel: boolean; secs: number }>({
    active: false,
    cancel: false,
    secs: 0,
  });
  const [createOpen, setCreateOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  /** 清空对话确认弹窗 */
  const [clearOpen, setClearOpen] = useState(false);
  /** 删除角色确认弹窗 */
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteErr, setDeleteErr] = useState("");
  /** LLM 配置状态：未配置时顶栏给出显眼入口 */
  const [llmInfo, setLlmInfo] = useState<{ configured: boolean; model?: string; error?: string } | null>(null);
  const fetchLlmInfo = useCallback(async () => {
    try {
      const r = await fetch("/api/health");
      const j = (await r.json()) as { llm?: { configured?: boolean; model?: string; error?: string } };
      setLlmInfo({ configured: !!j.llm?.configured, model: j.llm?.model, error: j.llm?.error });
    } catch {
      setLlmInfo((prev) => prev ?? { configured: false });
    }
  }, []);
  useEffect(() => {
    void fetchLlmInfo();
  }, [fetchLlmInfo]);

  /** 已发送图片：messageId -> dataURL（服务端不回传图片，本地留一份用于显示） */
  const [sentImages, setSentImages] = useState<Record<string, string>>({});

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const recStart = useRef<{ y: number; t: number }>({ y: 0, t: 0 });
  const recTimer = useRef<number>(0);
  const recActiveRef = useRef(false);
  const pendingImgRef = useRef<string | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 99999, behavior: "smooth" });
  }, [messages.length]);

  /* ---------------------------------------------------------------- 翻译 -- */

  const translateOne = useCallback(async (id: string, text: string) => {
    if (!text.trim() || tr[id]) return;
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
  }, [tr]);

  // 默认自动翻译 AI 的回复
  useEffect(() => {
    if (!translateAll) return;
    for (const m of messages) {
      if (m.role !== "assistant" || m.streaming) continue;
      if (!m.text?.trim() || tr[m.id] || trBusy[m.id]) continue;
      void translateOne(m.id, m.text);
    }
  }, [translateAll, messages, tr, trBusy, translateOne]);

  /* ---------------------------------------------------------------- 发送 -- */

  /** 只能删自定义角色（内置角色没有删除接口） */
  const isCustomPersona = (active?.id ?? "").startsWith("u_");

  const doDeletePersona = async () => {
    const pid = active?.id;
    if (!pid) return;
    setDeleting(true);
    setDeleteErr("");
    try {
      const r = await fetch(`/api/custom/personas/${encodeURIComponent(pid)}`, { method: "DELETE" });
      const j = (await r.json().catch(() => ({}))) as Record<string, unknown>;
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
      setDeleteOpen(false);
      // 角色表由后端驱动，重新加载最省事且不会留下悬空状态
      window.location.reload();
    } catch (e) {
      setDeleteErr(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(false);
    }
  };

  const doSend = () => {
    const text = draft.trim();
    const img = pendingImg;
    if (!text && !img) return;
    if (img) pendingImgRef.current = img;
    sendText(text, img ? [img] : undefined);
    setDraft("");
    setPendingImg(null);
  };

  // 用户消息到达后，把之前暂存的图片挂到它身上
  useEffect(() => {
    const img = pendingImgRef.current;
    if (!img) return;
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (lastUser && !sentImages[lastUser.id]) {
      setSentImages((s) => ({ ...s, [lastUser.id]: img }));
      pendingImgRef.current = null;
    }
  }, [messages, sentImages]);

  const pickImage = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => setPendingImg(String(reader.result));
    reader.readAsDataURL(f);
    e.target.value = "";
  };

  /* -------------------------------------------------------- 按住说话 ---- */

  const startRecUI = (e: React.PointerEvent<HTMLButtonElement>) => {
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* window 兜底 */
    }
    recStart.current = { y: e.clientY, t: Date.now() };
    recActiveRef.current = true;
    setRec({ active: true, cancel: false, secs: 0 });
    startRecording();
    recTimer.current = window.setInterval(() => {
      setRec((r) => ({ ...r, secs: Math.floor((Date.now() - recStart.current.t) / 1000) }));
    }, 200);
  };

  const moveRec = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!recActiveRef.current) return;
    setRec((r) => ({ ...r, cancel: recStart.current.y - e.clientY > CANCEL_DY }));
  };

  const finishRec = useCallback(
    (clientY: number) => {
      if (!recActiveRef.current) return;
      window.clearInterval(recTimer.current);
      const cancelled = recStart.current.y - clientY > CANCEL_DY;
      recActiveRef.current = false;
      setRec({ active: false, cancel: false, secs: 0 });
      if (cancelled) {
        // 取消：仍然要停止录音并把已录片段丢掉（发 audio_end 让服务端丢弃）
        // 取消：停麦但不提交，由服务端丢弃已缓冲音频
        cancelRecording();
        return;
      }
      stopRecording();
    },
    [stopRecording, cancelRecording],
  );

  useEffect(() => {
    if (!rec.active) return;
    const onUp = (ev: PointerEvent) => finishRec(ev.clientY);
    const onCancel = () => {
      window.clearInterval(recTimer.current);
      recActiveRef.current = false;
      setRec({ active: false, cancel: false, secs: 0 });
      cancelRecording();
    };
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    return () => {
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
    };
  }, [rec.active, finishRec, cancelRecording]);

  useEffect(() => () => window.clearInterval(recTimer.current), []);

  /* -------------------------------------------------------------- 渲染 -- */

  const canSend = !!(draft.trim() || pendingImg);
  const lastNotice: Notice | undefined = notices[notices.length - 1];

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
            onClick={() => setCreateOpen(true)}
            title="添加角色"
            className="flex h-[26px] w-[26px] items-center justify-center rounded-[5px] text-[#5c5c5c] transition-colors hover:bg-black/[0.06] hover:text-[#07c160]"
          >
            <Plus size={15} strokeWidth={2} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto wx-scroll pb-1">
          {personas.map((p) => {
            const on = p.id === activePersonaId;
            const [g1, g2] = pairFor(p.id);
            return (
              <button
                key={p.id}
                type="button"
                onClick={() => selectPersona(p.id)}
                className={`flex w-full items-start gap-2.5 px-3 py-[6px] text-left transition-colors ${on ? "bg-black/[0.075]" : "hover:bg-black/[0.04]"}`}
              >
                <div
                  className="flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-[6px] text-[15px] font-medium tracking-tight text-white"
                  style={{
                    backgroundImage: `linear-gradient(145deg, ${g1}, ${g2})`,
                    boxShadow: "inset 0 0.5px 0 rgba(255,255,255,.22), 0 0.5px 1px rgba(0,0,0,.06)",
                  }}
                >
                  {(p.name || "?").trim()[0]}
                </div>
                <div className="min-w-0 flex-1 pt-[1px]">
                  <div className="flex items-baseline gap-1.5">
                    <span className="truncate text-[13.5px] leading-[18px] text-[#191919]">{p.name}</span>
                    {p.accent_short && (
                      <span className="shrink-0 rounded-[3px] bg-black/[0.055] px-[3.5px] pb-[1px] text-[9px] font-medium leading-[12px] text-[#6e6e6e]">
                        {p.accent_short}
                      </span>
                    )}
                    {on && <span className="ml-auto shrink-0 text-[10px] text-[#07c160]">当前</span>}
                  </div>
                  <div className="mt-[3px] truncate text-[12px] leading-[16px] text-[#8c8c8c]">{p.title ?? p.scene ?? ""}</div>
                </div>
              </button>
            );
          })}
        </div>
      </aside>

      {/* ============================================ 右侧聊天区 */}
      <main className="relative flex min-w-0 flex-1 flex-col bg-[#f5f5f5]">
        <header className="flex h-[54px] shrink-0 items-center gap-2.5 px-5 shadow-[0_0.5px_0_rgba(0,0,0,.07)]">
          <span
            className="flex h-[30px] w-[30px] items-center justify-center rounded-[5px] text-[13px] font-medium text-white"
            style={{
              backgroundImage: `linear-gradient(145deg, ${pairFor(active?.id ?? "x")[0]}, ${pairFor(active?.id ?? "x")[1]})`,
              boxShadow: "inset 0 0.5px 0 rgba(255,255,255,.22), 0 0.5px 1px rgba(0,0,0,.06)",
            }}
          >
            {(active?.name ?? "?").trim()[0]}
          </span>
          <span className="text-[14px] font-medium leading-[18px] text-[#191919]">{active?.name ?? "对方"}</span>
          {active?.accent_short && !(active.accent ?? "").startsWith(active.accent_short) && (
            <span className="rounded-[3px] bg-black/[0.055] px-[3.5px] pb-[1px] text-[9px] font-medium leading-[12px] text-[#6e6e6e]">
              {active.accent_short}
            </span>
          )}
          <span className="truncate text-[10.5px] leading-[14px] text-[#a0a0a0]">{active?.accent ?? ""}</span>

          <div className="ml-auto flex items-center gap-1">
            <span
              className={`mr-1 flex items-center gap-1 rounded-full px-2 py-[3px] text-[9.5px] ${
                status === "ready" ? "bg-[#07c160]/[0.12] text-[#07c160]" : "bg-black/[0.05] text-[#8a8a8a]"
              }`}
            >
              <span className={`h-[5px] w-[5px] rounded-full ${status === "ready" ? "bg-[#07c160]" : "bg-[#b0b0b0]"}`} />
              {status === "ready" ? "已连接" : status === "connecting" ? "连接中" : "未连接"}
            </span>
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
            {playerStats.playing && (
              <button
                type="button"
                onClick={interrupt}
                title="停止播放"
                className="flex h-[30px] items-center gap-1 rounded-[6px] px-2 text-[10.5px] text-[#fa5151] hover:bg-[#fa5151]/[0.08]"
              >
                <Volume2 size={13} strokeWidth={1.8} />
                停止
              </button>
            )}
            <IconBtn title="清空对话" onClick={() => setClearOpen(true)}>
              <Trash2 size={15.5} strokeWidth={1.75} />
            </IconBtn>
            {isCustomPersona && (
              <IconBtn title="删除角色" onClick={() => setDeleteOpen(true)}>
                <UserMinus size={15.5} strokeWidth={1.75} />
              </IconBtn>
            )}
            <IconBtn
              title={llmInfo?.configured ? "LLM 设置" : "LLM 未配置 — 点此配置"}
              onClick={() => setSettingsOpen(true)}
              highlight={!llmInfo?.configured}
            >
              <Settings size={15.5} strokeWidth={1.75} />
            </IconBtn>
            <IconBtn title="添加角色" onClick={() => setCreateOpen(true)}>
              <Plus size={15.5} strokeWidth={1.75} />
            </IconBtn>
          </div>
        </header>

        {lastNotice && (
          <div className="shrink-0 border-b border-black/5 bg-[#fffbe6] px-5 py-1.5">
            <p className="flex items-center gap-2 text-[10.5px] leading-[15px] text-[#8a6d3b]">
              <span className="min-w-0 flex-1">{lastNotice.text}</span>
              <button type="button" onClick={() => dismissNotice(lastNotice.id)} className="shrink-0 text-[#b0a07a]">
                <X size={11} />
              </button>
            </p>
          </div>
        )}

        {llmInfo && !llmInfo.configured && (
          <div className="shrink-0 border-b border-black/5 bg-[#fff7e6] px-5 py-2">
            <p className="flex items-center gap-2 text-[11px] leading-[16px] text-[#8a6d3b]">
              <span className="min-w-0 flex-1">
                <b className="font-medium">LLM 还没配置</b> —— 现在只能听到兜底回复，无法真正对话。
              </span>
              <button
                type="button"
                onClick={() => setSettingsOpen(true)}
                className="shrink-0 rounded-[5px] bg-[#07c160] px-2 py-[3px] text-[10.5px] font-medium text-white hover:bg-[#06ad56]"
              >
                立即配置
              </button>
            </p>
          </div>
        )}

        {micError && (
          <div className="shrink-0 border-b border-black/5 bg-[#fff2f0] px-5 py-1.5">
            <p className="text-[10.5px] leading-[15px] text-[#c8443c]">麦克风：{micError}</p>
          </div>
        )}

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto wx-scroll px-6 py-5">
          <div className="mx-auto max-w-[680px] space-y-[14px]">
            <div className="flex justify-center pt-1">
              <span className="rounded-[4px] bg-black/[0.045] px-2 py-[2px] text-[10.5px] leading-[15px] text-[#9a9a9a]">今天</span>
            </div>

            {messages.length === 0 && (
              <p className="py-14 text-center text-[11.5px] leading-[18px] text-[#a8a8a8]">
                点麦克风按住说话，或直接打字开始练习
                <br />
                <span className="text-[10px] text-[#b8b8b8]">对方会用语音回复，文字同步显示在气泡下方</span>
              </p>
            )}

            {messages.map((m) => (
              <Bubble
                key={m.id}
                msg={m}
                persona={active}
                image={sentImages[m.id] ?? m.image}
                duration={turnDurations[`${active?.id ?? "_"}:${m.turnId}`]}
                replaying={replayingTurn === m.turnId}
                onReplay={() => void replayTurn(m.turnId)}
                translation={tr[m.id]}
                translating={!!trBusy[m.id]}
                onTranslate={() => void translateOne(m.id, m.text ?? "")}
              />
            ))}

            {recording && (
              <div className="flex justify-end">
                <span className="rounded-[7px] bg-[#95ec69]/70 px-3 py-[6px] text-[11.5px] text-[#3d6b2f]">正在听你说…</span>
              </div>
            )}
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
                  <span className="text-[9.5px] text-[#a5a5a5]">对方能看懂图片内容</span>
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
                  onPointerDown={startRecUI}
                  onPointerMove={moveRec}
                  onPointerUp={(e) => finishRec(e.clientY)}
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
                      doSend();
                    }
                  }}
                  rows={1}
                  placeholder={pendingImg ? "给这张图配一句话…" : "说点什么…"}
                  className="wx-line max-h-[88px] min-h-[34px] flex-1 resize-none rounded-[6px] border-[0.5px] bg-white px-2.5 py-[7px] text-[13px] leading-[19px] text-[#191919] outline-none shadow-[0_0.5px_1px_rgba(0,0,0,.03)] transition-shadow placeholder:text-[#b5b5b5]"
                />
                <IconBtn title="表情">
                  <Smile size={18} strokeWidth={1.75} />
                </IconBtn>
                <IconBtn title="图片" onClick={() => fileRef.current?.click()}>
                  <ImageIcon size={18} strokeWidth={1.75} />
                </IconBtn>
                {canSend ? (
                  <button
                    type="button"
                    onClick={doSend}
                    className="flex h-[34px] shrink-0 items-center gap-1 rounded-[6px] bg-[#07c160] px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-[#06ad56]"
                  >
                    <Send size={12} strokeWidth={2} />
                    发送
                  </button>
                ) : (
                  <IconBtn title="添加角色" onClick={() => setCreateOpen(true)}>
                    <Plus size={18} strokeWidth={1.75} />
                  </IconBtn>
                )}
              </div>
            )}

            <input ref={fileRef} type="file" accept="image/*" onChange={pickImage} className="hidden" />

            {micDeviceId !== "" && (
              <p className="mt-1 text-center text-[9px] text-[#b0b0b0]">
                输入设备已指定 ·{" "}
                <button type="button" onClick={() => setMicDeviceId("")} className="underline">
                  恢复默认
                </button>
              </p>
            )}
          </div>
        </footer>
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

      <CreatePersonaDialog open={createOpen} onClose={() => setCreateOpen(false)} />
      {deleteOpen && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center p-4">
          <button
            type="button"
            aria-label="关闭"
            onClick={() => !deleting && setDeleteOpen(false)}
            className="absolute inset-0 bg-black/35"
          />
          <div className="wx-rise relative w-full max-w-[340px] overflow-hidden rounded-[10px] bg-white shadow-[0_12px_48px_-8px_rgba(0,0,0,.3)]">
            <div className="px-5 pb-1 pt-4">
              <h3 className="text-[14px] font-semibold leading-[19px] text-[#191919]">删除角色</h3>
              <p className="mt-1 text-[11.5px] leading-[17px] text-[#7a7a7a]">
                将删除角色 <b className="font-medium text-[#191919]">{active?.name ?? ""}</b>
                及其设定、口音配置和聊天记录。此操作无法撤销。
              </p>
              {deleteErr && <p className="mt-2 text-[10.5px] leading-[15px] text-[#fa5151]">删除失败：{deleteErr}</p>}
            </div>
            <div className="flex items-center justify-end gap-2 px-5 pb-4 pt-3">
              <button
                type="button"
                disabled={deleting}
                onClick={() => setDeleteOpen(false)}
                className="wx-line-soft rounded-[6px] border-[0.5px] bg-white px-3 py-[6px] text-[11.5px] text-[#4a4a4a] hover:bg-[#f3f3f3] disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                disabled={deleting}
                onClick={() => void doDeletePersona()}
                className="rounded-[6px] bg-[#fa5151] px-3.5 py-[6px] text-[11.5px] font-medium text-white hover:bg-[#e04646] disabled:opacity-60"
              >
                {deleting ? "删除中…" : "确认删除"}
              </button>
            </div>
          </div>
        </div>
      )}

      {clearOpen && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center p-4">
          <button type="button" aria-label="关闭" onClick={() => setClearOpen(false)} className="absolute inset-0 bg-black/35" />
          <div className="wx-rise relative w-full max-w-[340px] overflow-hidden rounded-[10px] bg-white shadow-[0_12px_48px_-8px_rgba(0,0,0,.3)]">
            <div className="px-5 pb-1 pt-4">
              <h3 className="text-[14px] font-semibold leading-[19px] text-[#191919]">清空对话记录</h3>
              <p className="mt-1 text-[11.5px] leading-[17px] text-[#7a7a7a]">
                将删除与 <b className="font-medium text-[#191919]">{active?.name ?? "该角色"}</b> 的全部聊天记录，
                对方也会忘记你们聊过的内容。此操作无法撤销。
              </p>
            </div>
            <div className="flex items-center justify-end gap-2 px-5 pb-4 pt-3">
              <button
                type="button"
                onClick={() => setClearOpen(false)}
                className="wx-line-soft rounded-[6px] border-[0.5px] bg-white px-3 py-[6px] text-[11.5px] text-[#4a4a4a] hover:bg-[#f3f3f3]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => {
                  clearConversation();
                  setClearOpen(false);
                }}
                className="rounded-[6px] bg-[#fa5151] px-3.5 py-[6px] text-[11.5px] font-medium text-white hover:bg-[#e04646]"
              >
                确认清空
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfigLockedDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ 气泡 */

function Bubble({
  msg,
  persona,
  image,
  duration,
  replaying,
  onReplay,
  translation,
  translating,
  onTranslate,
}: {
  msg: ChatMessage;
  persona?: PersonaPublic;
  image?: string;
  duration?: number;
  replaying: boolean;
  onReplay: () => void;
  translation?: string;
  translating: boolean;
  onTranslate: () => void;
}) {
  const mine = msg.role === "user";
  const [g1, g2] = pairFor(persona?.id ?? "x");
  const text = msg.text ?? "";
  const streaming = msg.streaming;

  // 语音气泡时长：优先用真实音频时长，否则按字数估算
  const est = Math.max(1, Math.round(text.length / 14));
  const dur = Math.round(duration && duration > 0.2 ? duration : est);

  return (
    <div className={`flex items-start gap-2.5 ${mine ? "flex-row-reverse" : "flex-row"}`}>
      <div
        className="mt-[1px] flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[5px] text-[13px] font-medium text-white"
        style={
          mine
            ? { backgroundImage: "linear-gradient(145deg, #7f9db3, #5d7d94)", boxShadow: "inset 0 0.5px 0 rgba(255,255,255,.22), 0 0.5px 1px rgba(0,0,0,.06)" }
            : { backgroundImage: `linear-gradient(145deg, ${g1}, ${g2})`, boxShadow: "inset 0 0.5px 0 rgba(255,255,255,.22), 0 0.5px 1px rgba(0,0,0,.06)" }
        }
      >
        {mine ? "我" : (persona?.name ?? "?").trim()[0]}
      </div>

      <div className={`flex min-w-0 max-w-[76%] flex-col ${mine ? "items-end" : "items-start"}`}>
        {/* 图片 */}
        {image && (
          <div className="overflow-hidden rounded-[7px] bg-white p-[3px]" style={{ filter: "drop-shadow(0 1px 1.5px rgba(0,0,0,.075))" }}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={image} alt="图片消息" className="max-h-[220px] max-w-[264px] rounded-[5px] object-cover" />
          </div>
        )}

        {/* 文字 */}
        {!!text.trim() && (
          <div
            className={`relative rounded-[7px] px-3 py-[7px] text-[13px] leading-[20px] text-[#191919] ${mine ? "bg-[#95ec69]" : "bg-white"}`}
            style={{ filter: "drop-shadow(0 1px 1.5px rgba(0,0,0,.075))" }}
          >
            <span aria-hidden className={`absolute top-[12px] h-[7px] w-[7px] rotate-45 ${mine ? "-right-[2px] bg-[#95ec69]" : "-left-[2px] bg-white"}`} />
            {text}
            {streaming && <span className="ml-[1px] inline-block h-[13px] w-[2px] animate-pulse bg-[#07c160] align-middle" />}
          </div>
        )}

        {/* 对方的语音条（可点重播） */}
        {!mine && !!text.trim() && !streaming && (
          <button
            type="button"
            onClick={onReplay}
            title="点一下重播语音"
            className="mt-[5px] flex items-center gap-1.5 rounded-[5px] bg-black/[0.04] px-2 py-[3px] text-[9.5px] text-[#7a7a7a] transition-colors hover:bg-black/[0.065] hover:text-[#07c160]"
          >
            <Volume2 size={11} strokeWidth={2} className={replaying ? "animate-pulse text-[#07c160]" : ""} />
            {replaying ? "播放中…" : `重播 ${dur}″`}
          </button>
        )}

        {streaming && !text.trim() && (
          <div className="flex items-center gap-1.5 rounded-[7px] bg-white px-3 py-[7px] text-[11px] text-[#a0a0a0]">
            <Loader2 size={11} className="animate-spin" />
            正在想…
          </div>
        )}

        {/* 译文 */}
        {translation && !!text.trim() && (
          <div className="wx-rise mt-[5px] max-w-full rounded-[5px] border-l-[2px] border-[#07c160]/45 bg-black/[0.028] px-2 py-[5px] text-[12px] leading-[18px] text-[#5f5f5f]">
            {translation}
          </div>
        )}

        <div className={`mt-[5px] flex items-center gap-[9px] px-[2px] ${mine ? "flex-row-reverse" : ""}`}>
          <span className="text-[9.5px] leading-[13px] tabular-nums text-[#b4b4b4]">
            {new Date(msg.createdAt || Date.now()).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
          </span>
          {!mine && !!text.trim() && !streaming && (
            <button
              type="button"
              onClick={onTranslate}
              disabled={translating}
              className="text-[9.5px] leading-[13px] text-[#a0a0a0] transition-colors hover:text-[#07c160] disabled:opacity-50"
            >
              {translating ? "翻译中…" : translation ? "重译" : "译"}
            </button>
          )}
          {msg.pending && <span className="text-[9px] text-[#b4b4b4]">发送中</span>}
        </div>
      </div>
    </div>
  );
}

function IconBtn({
  children,
  title,
  onClick,
  highlight,
}: {
  children: React.ReactNode;
  title: string;
  onClick?: () => void;
  highlight?: boolean;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={`flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[6px] transition-colors ${
        highlight
          ? "animate-pulse bg-[#fa5151]/[0.12] text-[#fa5151] hover:bg-[#fa5151]/[0.18]"
          : "text-[#5c5c5c] hover:bg-black/[0.045] hover:text-[#191919] active:bg-black/[0.07]"
      }`}
    >
      {children}
    </button>
  );
}
