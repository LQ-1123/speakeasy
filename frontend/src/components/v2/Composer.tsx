"use client";

/** 输入条：默认按住说话；点键盘切文字输入（无麦环境自动兜底）。 */

import { useEffect, useRef, useState } from "react";
import { Keyboard, Send, Smile, Mic } from "lucide-react";

import { HoldToTalk } from "@/components/v2/HoldToTalk";
import { useToast } from "@/components/v2/Toast";
import type { RecordingResult } from "@/lib/v2/useRecorder";

interface ComposerProps {
  onSendVoice: (result: RecordingResult) => Promise<void>;
  onSendText: (text: string) => Promise<void>;
  sending: boolean;
  disabled?: boolean;
}

export function Composer({ onSendVoice, onSendText, sending, disabled = false }: ComposerProps) {
  const { showToast } = useToast();
  const [mode, setMode] = useState<"voice" | "text">("voice");
  const [text, setText] = useState("");
  const [micBlocked, setMicBlocked] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (mode === "text") {
      const timer = setTimeout(() => inputRef.current?.focus(), 60);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [mode]);

  const switchToText = (reason?: string) => {
    setMode("text");
    setMicBlocked(true);
    if (reason) showToast(reason, "info");
  };

  const submitText = async () => {
    const value = text.trim();
    if (!value || sending) return;
    setText("");
    try {
      await onSendText(value);
    } catch {
      setText(value);
    }
  };

  return (
    <div className="se-composer">
      <button
        type="button"
        aria-label={mode === "voice" ? "切换到文字输入" : "切换到按住说话"}
        onClick={() => setMode((value) => (value === "voice" ? "text" : "voice"))}
        className="se-composer-icon"
      >
        {mode === "voice" ? <Keyboard size={21} /> : <Mic size={21} />}
      </button>

      {mode === "voice" ? (
        <div className="min-w-0 flex-1">
          <HoldToTalk
            onComplete={onSendVoice}
            onUnavailable={(message) => switchToText(message)}
            onError={(message) => showToast(message, "error")}
            disabled={disabled || sending}
            label={micBlocked ? "按住 说话（麦克风不可用）" : "按住 说话"}
          />
          <p className="mt-1 text-center text-[10px] leading-none text-[color:var(--se-faint)]">
            {micBlocked ? "没有检测到麦克风 · 已切换文字输入" : "按住说话，上滑取消"}
          </p>
        </div>
      ) : (
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <input
            ref={inputRef}
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                event.preventDefault();
                void submitText();
              }
            }}
            placeholder="说点什么…（英文更地道）"
            aria-label="输入消息"
            className="se-composer-input"
          />
          <button
            type="button"
            onClick={() => void submitText()}
            disabled={!text.trim() || sending || disabled}
            aria-label="发送"
            className="se-send-btn"
          >
            <Send size={16} />
          </button>
        </div>
      )}

      {mode === "voice" ? (
        <button type="button" aria-label="表情" className="se-composer-icon" onClick={() => showToast("表情功能即将上线", "info")}>
          <Smile size={21} />
        </button>
      ) : null}
    </div>
  );
}
