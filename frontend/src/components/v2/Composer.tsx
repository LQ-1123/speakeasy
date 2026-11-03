"use client";

/** 输入条：默认按住说话；点键盘切文字输入（无麦环境自动兜底）。 */

import { useEffect, useRef, useState } from "react";
import { ImagePlus, Keyboard, Loader2, Send, Smile, Mic, X } from "lucide-react";

import { HoldToTalk } from "@/components/v2/HoldToTalk";
import { useToast } from "@/components/v2/Toast";
import type { RecordingResult } from "@/lib/v2/useRecorder";

/** 常用表情（48 个）：点选插入输入框，语音模式会自动切到文字输入。 */
const EMOJIS = [
  "😀", "😄", "😁", "😆", "😅", "😂", "🤣", "😊",
  "😇", "🙂", "🙃", "😉", "😌", "😍", "🥰", "😘",
  "😋", "😛", "😝", "😜", "🤪", "🤨", "🧐", "🤓",
  "😎", "🥳", "🤩", "😏", "😒", "😞", "😔", "😟",
  "😕", "🙁", "😣", "😖", "😫", "😩", "🥺", "😢",
  "😭", "😤", "😠", "😡", "🤯", "😳", "😱", "😴",
];

interface ComposerProps {
  onSendVoice: (result: RecordingResult) => Promise<void>;
  onSendText: (text: string) => Promise<void>;
  /** 发送图片（caption 可为空） */
  onSendImage: (file: File, caption: string) => Promise<void>;
  sending: boolean;
  disabled?: boolean;
}

const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"];

export function Composer({
  onSendVoice,
  onSendText,
  onSendImage,
  sending,
  disabled = false,
}: ComposerProps) {
  const { showToast } = useToast();
  const [mode, setMode] = useState<"voice" | "text">("voice");
  const [text, setText] = useState("");
  const [micBlocked, setMicBlocked] = useState(false);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [imagePreview, setImagePreview] = useState<{ file: File; url: string } | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const previewUrlRef = useRef<string | null>(null);

  // 卸载时回收图片预览 blob（只操作 ref，避免卸载期 setState）
  useEffect(() => {
    return () => {
      if (previewUrlRef.current) {
        URL.revokeObjectURL(previewUrlRef.current);
        previewUrlRef.current = null;
      }
    };
  }, []);

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

  /** 插入表情：语音模式自动切文字输入，追加到当前文本并聚焦。 */
  const insertEmoji = (emoji: string) => {
    setText((prev) => `${prev}${emoji}`);
    if (mode === "voice") {
      setMode("text");
      setTimeout(() => inputRef.current?.focus(), 80);
    } else {
      inputRef.current?.focus();
    }
  };

  const emojiButton = (
    <button
      type="button"
      aria-label="表情"
      aria-expanded={emojiOpen}
      title="表情"
      className={`se-composer-icon ${emojiOpen ? "active" : ""}`}
      onClick={() => setEmojiOpen((value) => !value)}
    >
      <Smile size={21} />
    </button>
  );

  const pickImage = () => {
    setEmojiOpen(false);
    fileInputRef.current?.click();
  };

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (!IMAGE_TYPES.includes(file.type)) {
      showToast("只支持 JPG / PNG / WebP / GIF 图片", "error");
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      showToast("图片不能超过 8MB", "error");
      return;
    }
    const url = URL.createObjectURL(file);
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    previewUrlRef.current = url;
    setImagePreview({ file, url });
    setMode("text");
    setTimeout(() => inputRef.current?.focus(), 80);
  };

  const removeImage = () => {
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = null;
    }
    setImagePreview(null);
  };

  const clearPreview = removeImage;

  const imageButton = (
    <button
      type="button"
      aria-label="发送图片"
      title="发送图片"
      className="se-composer-icon"
      onClick={pickImage}
      disabled={disabled || sending}
    >
      <ImagePlus size={21} />
    </button>
  );

  const submitText = async () => {
    const value = text.trim();
    if (sending) return;
    if (imagePreview) {
      try {
        await onSendImage(imagePreview.file, value);
        clearPreview();
        setText("");
      } catch {
        /* 失败保留预览与 caption，便于重试 */
      }
      return;
    }
    if (!value) return;
    setText("");
    try {
      await onSendText(value);
    } catch {
      setText(value);
    }
  };

  return (
    <div className="se-composer">
      {imagePreview ? (
        <div className="se-image-preview">
          <img src={imagePreview.url} alt="待发送图片预览" className="se-image-preview-thumb" />
          <div className="se-image-preview-meta">
            <p className="truncate font-medium text-[color:var(--se-text)]">{imagePreview.file.name || "图片"}</p>
            <p>{(imagePreview.file.size / 1024).toFixed(0)} KB · 可加一句 caption 后发送</p>
          </div>
          <button type="button" className="se-image-preview-remove" aria-label="移除图片" onClick={removeImage}>
            <X size={14} />
          </button>
        </div>
      ) : null}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/jpeg,image/png,image/webp,image/gif"
        className="se-file-input"
        onChange={handleFileChange}
        aria-hidden
        tabIndex={-1}
      />

      <button
        type="button"
        aria-label={mode === "voice" ? "切换到文字输入" : "切换到按住说话"}
        onClick={() => setMode((value) => (value === "voice" ? "text" : "voice"))}
        disabled={Boolean(imagePreview)}
        title={imagePreview ? "有图片待发送" : undefined}
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
            placeholder={imagePreview ? "给这张图配一句话（可留空）" : "说点什么…（英文更地道）"}
            aria-label="输入消息"
            className="se-composer-input"
          />
          {imageButton}
          {emojiButton}
          <button
            type="button"
            onClick={() => void submitText()}
            disabled={sending || disabled || (!text.trim() && !imagePreview)}
            aria-label="发送"
            className="se-send-btn"
          >
            {sending ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
          </button>
        </div>
      )}

      {mode === "voice" ? (
        <>
          {imageButton}
          {emojiButton}
        </>
      ) : null}

      {emojiOpen ? (
        <>
          <button
            type="button"
            aria-label="关闭表情面板"
            className="se-emoji-backdrop"
            onClick={() => setEmojiOpen(false)}
          />
          <div className="se-emoji-panel" role="dialog" aria-label="表情选择器">
            <div className="se-emoji-head">
              <span>常用表情</span>
              <span>点选插入输入框</span>
            </div>
            <div className="se-emoji-grid">
              {EMOJIS.map((emoji) => (
                <button
                  key={emoji}
                  type="button"
                  className="se-emoji-btn"
                  aria-label={`插入表情 ${emoji}`}
                  onClick={() => insertEmoji(emoji)}
                >
                  {emoji}
                </button>
              ))}
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
