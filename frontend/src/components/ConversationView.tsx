"use client";

import { useEffect, useRef } from "react";
import { AlertTriangle, MessageSquareDashed } from "lucide-react";

import type { ChatMessage, PersonaPublic } from "@/lib/types";

interface ConversationViewProps {
  messages: ChatMessage[];
  persona: PersonaPublic | undefined;
  activeSentence: { turnId: number; sentenceIndex: number } | null;
  vadSpeaking: boolean;
  pending: boolean;
  connected: boolean;
}

function fmtClock(ts: number): string {
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

/**
 * 中栏 · 对话区
 *
 * 消息布局：
 *  - user 右对齐、窄气泡、无头像（用户已知是谁）。
 *  - assistant 左对齐、贴左边缘、无气泡底（像台本），避免"聊天 App 感"。
 *  - 流式输出时在文末渲染一个闪烁 caret。
 *  - 正在朗读的句子：该句用强调色下划线 + 左侧竖条标记，
 *    依据是 `assistant_sentence` 的 sentence_index 与播放器回报的当前 index。
 */
export function ConversationView({
  messages,
  persona,
  activeSentence,
  vadSpeaking,
  pending,
  connected,
}: ConversationViewProps) {
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);

  // 用户手动上滚时停止自动跟随
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      stickToBottom.current = distance < 80;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (!stickToBottom.current) return;
    bottomRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [messages, pending]);

  return (
    <div
      ref={containerRef}
      className="scroll-thin min-h-0 flex-1 overflow-y-auto px-5 py-4"
      role="log"
      aria-live="polite"
      aria-label="对话记录"
    >
      {messages.length === 0 && (
        <EmptyState persona={persona} connected={connected} />
      )}

      <div className="mx-auto flex max-w-[760px] flex-col gap-5">
        {messages.map((message) => (
          <MessageRow
            key={message.id}
            message={message}
            persona={persona}
            activeSentence={activeSentence}
          />
        ))}

        {vadSpeaking && (
          <div className="flex items-center gap-2 text-[10.5px] text-[color:var(--accent)]">
            <span className="flex gap-0.5">
              <span className="h-2 w-0.5 animate-pulse bg-current" />
              <span className="h-3 w-0.5 animate-pulse bg-current [animation-delay:120ms]" />
              <span className="h-1.5 w-0.5 animate-pulse bg-current [animation-delay:240ms]" />
            </span>
            正在识别你的语音…
          </div>
        )}

        <div ref={bottomRef} className="h-px" />
      </div>
    </div>
  );
}

function MessageRow({
  message,
  persona,
  activeSentence,
}: {
  message: ChatMessage;
  persona: PersonaPublic | undefined;
  activeSentence: { turnId: number; sentenceIndex: number } | null;
}) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div className="anim-in flex justify-end">
        <div className="max-w-[78%]">
          <div
            className={`rounded-sm border px-3 py-2 ${
              message.pending
                ? "border-[color:var(--line-strong)] bg-[color:var(--surface-2)] opacity-70"
                : "border-[color:var(--line-strong)] bg-[color:var(--surface-2)]"
            }`}
          >
            <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-[color:var(--text)]">
              {message.text || "（空）"}
            </p>
          </div>
          <div className="mt-1 flex items-center justify-end gap-2">
            {message.truncated && (
              <span className="flex items-center gap-1 text-[9.5px] text-[color:var(--accent-warn)]">
                <AlertTriangle size={9} />
                识别被截断
              </span>
            )}
            {message.pending && (
              <span className="text-[9.5px] text-[color:var(--text-faint)]">发送中…</span>
            )}
            <span className="tnum text-[9px] text-[color:var(--text-faint)]">{fmtClock(message.createdAt)}</span>
          </div>
        </div>
      </div>
    );
  }

  // ---- assistant
  const speakingIndex =
    activeSentence && activeSentence.turnId === message.turnId ? activeSentence.sentenceIndex : -1;

  return (
    <div className="anim-in flex justify-start">
      <div className="min-w-0 max-w-[86%]">
        <div className="mb-1 flex items-center gap-1.5">
          <span className="text-[10.5px] font-semibold uppercase tracking-[0.1em] text-[color:var(--text-dim)]">
            {persona?.flag} {persona?.name ?? "Assistant"}
          </span>
          {message.metrics?.first_audio_ms !== undefined && (
            <span className="tnum rounded-[2px] border border-[color:var(--line-strong)] px-1 py-px text-[8.5px] text-[color:var(--text-faint)]">
              {Math.round(message.metrics.first_audio_ms)}ms
            </span>
          )}
        </div>

        <div className="border-l-2 border-[color:var(--accent-line)] pl-3">
          {message.sentences && message.sentences.length > 0 ? (
            <div className="flex flex-col gap-1">
              {message.sentences.map((sentence) => {
                const speaking = sentence.index === speakingIndex;
                return (
                  <p
                    key={sentence.index}
                    className={`text-[14px] leading-relaxed transition-all duration-200 ${
                      speaking
                        ? "-ml-3 border-l-2 border-[color:var(--accent)] pl-3 font-medium text-[color:var(--accent)]"
                        : "text-[color:var(--text)]"
                    }`}
                  >
                    {sentence.text}
                  </p>
                );
              })}
              {/* 流式增量里尚未断句的尾部文本 */}
              {message.streaming && !sentencesCoverFullText(message) && (
                <p className="text-[14px] leading-relaxed text-[color:var(--text-dim)]">
                  {tailText(message)}
                  <span className="caret" />
                </p>
              )}
            </div>
          ) : (
            <p className="whitespace-pre-wrap break-words text-[14px] leading-relaxed text-[color:var(--text)]">
              {message.text}
              {message.streaming && <span className="caret" />}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

/** 已断句文本拼接后，是否已覆盖流式全文（用于避免尾部重复显示） */
function sentencesCoverFullText(message: ChatMessage): boolean {
  if (!message.sentences || message.sentences.length === 0) return false;
  const joined = message.sentences
    .map((s) => s.text)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  const full = message.text.replace(/\s+/g, " ").trim();
  return joined.length >= full.length - 2;
}

/** 取流式全文中位于已断句部分之后的部分 */
function tailText(message: ChatMessage): string {
  if (!message.sentences || message.sentences.length === 0) return message.text;
  const joined = message.sentences
    .map((s) => s.text)
    .join(" ")
    .replace(/\s+/g, " ");
  const full = message.text.replace(/\s+/g, " ");
  const idx = full.indexOf(joined.trim());
  if (idx === -1) return "";
  return full.slice(idx + joined.trim().length).trimStart();
}

function EmptyState({ persona, connected }: { persona: PersonaPublic | undefined; connected: boolean }) {
  return (
    <div className="mx-auto flex max-w-[560px] flex-col items-center justify-center py-16 text-center">
      <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-sm border border-[color:var(--line-strong)] bg-[color:var(--surface-2)]">
        <MessageSquareDashed size={18} className="text-[color:var(--text-faint)]" />
      </div>

      <div className="text-[13px] font-medium text-[color:var(--text-dim)]">
        {connected ? "已就绪，点麦克风或直接打字开始" : "后端未连接"}
      </div>

      <p className="mt-2 max-w-[420px] text-[11.5px] leading-relaxed text-[color:var(--text-faint)]">
        {connected
          ? persona?.scene
            ? persona.scene
            : "选一个角色，然后说话或打字。左侧可随时切换角色与对话模式。"
          : "顶栏显示「未连接后端」时，页面仍可浏览。启动 FastAPI（127.0.0.1:8000）后会自动重连；此期间可直接用下方输入框试打字，或刷新页面。"}
      </p>

      {persona?.opening_line && connected && (
        <div className="mt-5 w-full rounded-sm border border-[color:var(--line)] bg-[color:var(--surface-2)] px-3.5 py-2.5 text-left">
          <div className="panel-label mb-1">开场白</div>
          <p className="text-[13px] italic leading-relaxed text-[color:var(--text-dim)]">
            “{persona.opening_line}”
          </p>
        </div>
      )}

      {!connected && (
        <div className="mt-5 w-full rounded-sm border border-[color:var(--line)] bg-[color:var(--surface-2)] px-3.5 py-2.5 text-left">
          <div className="panel-label mb-1.5">自检清单</div>
          <ul className="space-y-1 text-[11px] leading-relaxed text-[color:var(--text-faint)]">
            <li>1. 后端是否在 http://127.0.0.1:8000 运行（`curl /api/health`）</li>
            <li>2. 若是 `npm run dev`，确认 next.config 的 rewrites 指向同一地址</li>
            <li>3. 顶栏点「连接后端」可立即重试，无需刷新</li>
          </ul>
        </div>
      )}
    </div>
  );
}
