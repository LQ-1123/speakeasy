"use client";

/** 按住说话按钮：按住录音、上滑取消、松手发送；不可用时降级为文字。 */

import { useCallback, useRef, type PointerEvent as ReactPointerEvent } from "react";
import { Loader2, Mic, X } from "lucide-react";

import { useVoiceRecorder, type RecordingResult } from "@/lib/v2/useRecorder";
import { durationLabel } from "@/lib/v2/format";

interface HoldToTalkProps {
  onComplete: (result: RecordingResult) => void | Promise<void>;
  onUnavailable: (message: string) => void;
  onError?: (message: string) => void;
  disabled?: boolean;
  label?: string;
  compact?: boolean;
}

export function HoldToTalk({
  onComplete,
  onUnavailable,
  onError,
  disabled = false,
  label = "按住 说话",
  compact = false,
}: HoldToTalkProps) {
  const {
    supported,
    probing,
    recording,
    starting,
    sending,
    elapsedMs,
    cancelArmed,
    start,
    finish,
    cancel,
    setDragCancel,
  } = useVoiceRecorder({ onComplete, onError, onUnavailable });

  const startYRef = useRef(0);
  const activePointerRef = useRef<number | null>(null);
  const pressingRef = useRef(false);

  const begin = useCallback(
    async (clientY: number) => {
      if (disabled || sending) return;
      pressingRef.current = true;
      startYRef.current = clientY;
      const started = await start();
      if (!started) pressingRef.current = false;
    },
    [disabled, sending, start],
  );

  const move = useCallback(
    (clientY: number) => {
      if (!pressingRef.current) return;
      const dy = clientY - startYRef.current;
      setDragCancel(dy < -60);
    },
    [setDragCancel],
  );

  const end = useCallback(() => {
    if (!pressingRef.current) return;
    pressingRef.current = false;
    // 关键：不能先 setDragCancel(false)。finish() 需要读取当前 cancelRef 来决定
    // 「上滑取消」还是「松手发送」；先清掉会导致上滑后依然发送。
    void finish().finally(() => setDragCancel(false));
  }, [finish, setDragCancel]);

  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 && event.pointerType === "mouse") return;
    event.preventDefault();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      /* 合成事件 / 旧浏览器不支持捕获，不影响按住逻辑 */
    }
    activePointerRef.current = event.pointerId;
    if (!supported && !probing && !recording) {
      onUnavailable("当前环境没有可用麦克风，已切换为文字输入");
      return;
    }
    void begin(event.clientY);
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (activePointerRef.current !== event.pointerId) return;
    move(event.clientY);
  };

  const onPointerUp = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (activePointerRef.current !== event.pointerId) return;
    activePointerRef.current = null;
    end();
  };

  const onPointerCancelHandler = () => {
    activePointerRef.current = null;
    if (recording) cancel();
    pressingRef.current = false;
  };

  const onContextMenu = (event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
  };

  const busy = starting || sending;
  const active = recording || starting;

  return (
    <button
      type="button"
      disabled={disabled}
      aria-label={label}
      title={label}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancelHandler}
      onContextMenu={onContextMenu}
      onKeyDown={(event) => {
        if ((event.key === " " || event.key === "Enter") && !active && !busy) {
          event.preventDefault();
          void begin(0);
        }
      }}
      onKeyUp={(event) => {
        if (event.key === " " || event.key === "Enter") {
          event.preventDefault();
          end();
        }
      }}
      className={`se-hold ${active ? "active" : ""} ${cancelArmed ? "cancel" : ""} ${
        compact ? "compact" : ""
      }`}
      style={{ touchAction: "none", userSelect: "none", WebkitUserSelect: "none" }}
    >
      {cancelArmed ? (
        <>
          <X size={compact ? 14 : 17} className="shrink-0" />
          <span>松开 取消</span>
          <span className="tabular-nums opacity-80">{durationLabel(elapsedMs)}</span>
        </>
      ) : recording ? (
        <>
          <span className="se-rec-dot" aria-hidden />
          <span>松手 发送</span>
          <span className="tabular-nums opacity-80">{durationLabel(elapsedMs)}</span>
        </>
      ) : starting || sending ? (
        <>
          <Loader2 size={compact ? 14 : 17} className="animate-spin" />
          <span>{sending ? "对方正在说话…" : "准备中…"}</span>
        </>
      ) : (
        <>
          <Mic size={compact ? 14 : 17} className="shrink-0" />
          <span>{label}</span>
        </>
      )}
    </button>
  );
}
