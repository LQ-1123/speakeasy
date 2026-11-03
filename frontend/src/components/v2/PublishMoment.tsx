"use client";

/**
 * 「发布动态」卡片：点击展开 textarea + 心情 emoji 选择 + 发布。
 *
 * 发布成功后由父级刷新动态列表，AI 好友评论异步生成（DiscoverTab 轮询刷新）。
 */

import { useEffect, useRef, useState } from "react";
import { ImagePlus, Loader2, Pencil, Send, X } from "lucide-react";

import { useToast } from "@/components/v2/Toast";

const MAX_LENGTH = 200;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"];

const MOODS = [
  { emoji: "😊", label: "开心" },
  { emoji: "🌤️", label: "惬意" },
  { emoji: "🎧", label: "在听歌" },
  { emoji: "🍜", label: "干饭了" },
  { emoji: "😴", label: "有点累" },
  { emoji: "🎉", label: "值得庆祝" },
  { emoji: "💭", label: "随便想想" },
  { emoji: "🌙", label: "深夜" },
];

interface PublishMomentProps {
  publishing: boolean;
  /** 返回 true 表示发布成功（父级已插入列表）；带 image 时走 /moments/photo */
  onPublish: (input: { content: string; mood_tag?: string; image?: File }) => Promise<boolean>;
}

export function PublishMoment({ publishing, onPublish }: PublishMomentProps) {
  const { showToast } = useToast();
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState("");
  const [mood, setMood] = useState("");
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [imageUrl, setImageUrl] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const imageUrlRef = useRef<string | null>(null);

  useEffect(() => {
    return () => {
      if (imageUrlRef.current) URL.revokeObjectURL(imageUrlRef.current);
      imageUrlRef.current = null;
    };
  }, []);

  const trimmed = content.trim();
  const tooLong = content.length > MAX_LENGTH;
  const canSubmit = (trimmed.length > 0 || imageFile !== null) && !tooLong && !publishing;

  const pickImage = () => fileInputRef.current?.click();

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
    if (imageUrlRef.current) URL.revokeObjectURL(imageUrlRef.current);
    const url = URL.createObjectURL(file);
    imageUrlRef.current = url;
    setImageUrl(url);
    setImageFile(file);
  };

  const removeImage = () => {
    if (imageUrlRef.current) URL.revokeObjectURL(imageUrlRef.current);
    imageUrlRef.current = null;
    setImageUrl("");
    setImageFile(null);
  };

  const submit = async () => {
    if (!canSubmit) {
      if (tooLong) showToast(`内容最多 ${MAX_LENGTH} 字`, "error");
      else if (!trimmed && !imageFile) showToast("先说点什么或选张图片再发布吧", "info");
      return;
    }
    const ok = await onPublish({
      content: trimmed,
      mood_tag: mood || undefined,
      image: imageFile ?? undefined,
    });
    if (!ok) return;
    setContent("");
    setMood("");
    removeImage();
    setOpen(false);
    if (textareaRef.current) textareaRef.current.blur();
  };

  if (!open) {
    return (
      <div className="se-publish mx-3 mt-2">
        <button type="button" className="se-publish-trigger" onClick={() => setOpen(true)}>
          <span className="se-publish-pen" aria-hidden>
            <Pencil size={14} />
          </span>
          <span className="min-w-0 flex-1 text-left">分享此刻…（好友会评论你）</span>
          <span className="flex gap-0.5 text-[15px]" aria-hidden>
            😊
          </span>
        </button>
      </div>
    );
  }

  return (
    <div className="se-publish mx-3 mt-2">
      <div className="flex items-center justify-between pb-1.5">
        <span className="text-[11.5px] font-semibold text-[color:var(--se-dim)]">发布动态</span>
        <button type="button" onClick={() => setOpen(false)} aria-label="收起发布框" className="se-publish-close">
          <X size={15} />
        </button>
      </div>

      <textarea
        ref={textareaRef}
        autoFocus
        value={content}
        maxLength={MAX_LENGTH + 20}
        onChange={(event) => setContent(event.target.value)}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            void submit();
          }
        }}
        placeholder="今天发生了什么？说给好友们听听…"
        aria-label="动态内容"
        className="se-publish-textarea"
      />

      <div className="se-publish-image">
        <input
          ref={fileInputRef}
          type="file"
          accept="image/jpeg,image/png,image/webp,image/gif"
          className="se-file-input"
          onChange={handleFileChange}
          aria-hidden
          tabIndex={-1}
        />
        {imageUrl ? (
          <div className="flex items-center gap-2">
            <img src={imageUrl} alt="动态图片预览" className="se-publish-thumb" />
            <button type="button" className="se-publish-image-remove" onClick={removeImage} aria-label="移除图片">
              <X size={13} />
              移除图片
            </button>
          </div>
        ) : (
          <button type="button" className="se-publish-img-btn" onClick={pickImage}>
            <ImagePlus size={14} />
            添加图片
          </button>
        )}
      </div>

      <div className="se-publish-moods" role="group" aria-label="心情">
        {MOODS.map((item) => {
          const value = `${item.emoji} ${item.label}`;
          const active = mood === value;
          return (
            <button
              key={value}
              type="button"
              className={`se-mood-chip ${active ? "active" : ""}`}
              aria-pressed={active}
              onClick={() => setMood(active ? "" : value)}
            >
              {item.emoji} {item.label}
            </button>
          );
        })}
      </div>

      <div className="se-publish-actions">
        <span className={`se-publish-count ${tooLong ? "over" : ""}`}>
          {content.length}/{MAX_LENGTH}
          {tooLong ? " · 超长" : ""}
        </span>
        <button type="button" className="se-publish-cancel" onClick={() => setOpen(false)} disabled={publishing}>
          取消
        </button>
        <button type="button" className="se-publish-submit" onClick={() => void submit()} disabled={!canSubmit}>
          {publishing ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
          {publishing ? "发布中…" : "发布"}
        </button>
      </div>
    </div>
  );
}
