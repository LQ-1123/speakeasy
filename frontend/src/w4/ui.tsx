"use client";

/** 所有页共用的零件。样式在 w4.css，这里只管结构。 */

import { type CSSProperties, useEffect, useMemo, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import { avatarTint, durationLabel, initial, voiceBubbleWidth } from "./format";
import type { Friend } from "./model";

/* ------------------------------------------------------------------ 头像 */

export function Avatar({
  friend,
  src,
  name,
  size = 42,
  round = false,
  online = false,
  className = "",
}: {
  friend?: Friend;
  src?: string;
  name?: string;
  size?: number;
  round?: boolean;
  online?: boolean;
  className?: string;
}) {
  const image = src ?? friend?.avatar;
  const label = name ?? friend?.name ?? "?";
  const id = friend?.id ?? label;
  return (
    <span
      className={`w4-avatar ${round ? "is-round" : ""} ${className}`}
      style={{ width: size, height: size }}
      aria-hidden
    >
      {image ? (
        <img src={image} alt="" loading="lazy" />
      ) : (
        <span
          className="w4-avatar-initial"
          style={{ background: avatarTint(id), fontSize: Math.round(size * 0.4) }}
        >
          {initial(label)}
        </span>
      )}
      {online ? <i className="w4-avatar-dot" /> : null}
    </span>
  );
}

/* ------------------------------------------------------------------ 图标 */

export function Icon({
  children,
  size = 18,
  strokeWidth = 1.8,
  className = "",
  style,
}: {
  children: ReactNode;
  size?: number;
  strokeWidth?: number;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <span
      className={`icon ${className}`}
      style={{ width: size, height: size, ...style }}
      aria-hidden
    >
      <svg
        viewBox="0 0 24 24"
        width={size}
        height={size}
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {children}
      </svg>
    </span>
  );
}

/* ------------------------------------------------------------------ 语音 */

/** 固定伪随机波形：同一条消息每次渲染形状一致。 */
function waveBars(seed: number, count: number): number[] {
  const out: number[] = [];
  let s = seed || 7;
  for (let i = 0; i < count; i += 1) {
    s = (s * 9301 + 49297) % 233280;
    out.push(5 + Math.round((s / 233280) * 13));
  }
  return out;
}

export function VoiceBar({
  id,
  durationMs,
  playing,
  onToggle,
}: {
  id: string;
  durationMs: number;
  playing: boolean;
  onToggle: () => void;
}) {
  const seed = useMemo(() => id.split("").reduce((a, c) => a + c.charCodeAt(0), 0), [id]);
  const bars = useMemo(() => waveBars(seed, 17), [seed]);
  return (
    <button
      type="button"
      className={`w4-voice ${playing ? "is-playing" : ""}`}
      style={{ width: voiceBubbleWidth(durationMs) }}
      onClick={onToggle}
      aria-label={playing ? "暂停" : "播放语音"}
    >
      <span className="w4-voice-play">
        {playing ? (
          <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
            <rect x="2" y="1.5" width="3" height="9" rx="0.6" />
            <rect x="7" y="1.5" width="3" height="9" rx="0.6" />
          </svg>
        ) : (
          <svg width="13" height="13" viewBox="0 0 12 12" fill="currentColor">
            <path d="M2.6 1.4v9.2c0 .5.55.8.98.55l7.2-4.6a.65.65 0 000-1.1l-7.2-4.6a.65.65 0 00-.98.55z" />
          </svg>
        )}
      </span>
      <span className="w4-wave" aria-hidden>
        {bars.map((h, i) => (
          <i key={i} style={{ height: h }} />
        ))}
      </span>
      <span className="w4-voice-dur">{durationLabel(durationMs)}</span>
    </button>
  );
}

/* ------------------------------------------------------------------ 浮层 */

export function Overlay({
  onClose,
  children,
  className = "",
}: {
  onClose: () => void;
  children: ReactNode;
  className?: string;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const node = (
    <div
      className={`w4-overlay ${className}`}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      {children}
    </div>
  );
  /* 挂到 body：否则手机端 .w4-main 的 transform 会让 position:fixed 相对它定位，
     弹窗会跑出屏幕（看起来“不居中”） */
  return typeof document === "undefined" ? node : createPortal(node, document.body);
}

export function Dialog({
  title,
  onClose,
  children,
  footer,
  width,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  return (
    <Overlay onClose={onClose}>
      <div className="w4-dialog" style={width ? { width: `min(${width}px, 100%)` } : undefined}>
        <div className="w4-dialog-head">
          <span className="w4-dialog-title">{title}</span>
          <button className="w4-icon-btn" onClick={onClose} aria-label="关闭">
            <Icon size={16} strokeWidth={2}>
              <path d="M6 6l12 12M18 6L6 18" />
            </Icon>
          </button>
        </div>
        <div className="w4-dialog-body">{children}</div>
        {footer ? <div className="w4-dialog-foot">{footer}</div> : null}
      </div>
    </Overlay>
  );
}

export function Confirm({
  title,
  text,
  confirmText = "确定",
  danger = false,
  onCancel,
  onConfirm,
}: {
  title: string;
  text: ReactNode;
  confirmText?: string;
  danger?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Overlay onClose={onCancel}>
      <div className="w4-confirm">
        <div className="w4-confirm-title">{title}</div>
        <div className="w4-confirm-text">{text}</div>
        <div className="w4-confirm-actions">
          <button className="w4-btn" onClick={onCancel}>
            取消
          </button>
          <button
            className={`w4-btn ${danger ? "is-danger" : "is-primary"}`}
            onClick={onConfirm}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </Overlay>
  );
}

export function Lightbox({ src, onClose }: { src: string; onClose: () => void }) {
  return (
    <div
      className="w4-lightbox"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <button className="w4-lightbox-close" onClick={onClose} aria-label="关闭">
        <X size={18} />
      </button>
      <img src={src} alt="" />
    </div>
  );
}

/* ------------------------------------------------------------------ 其他 */

export function Switch({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      className={`w4-switch ${on ? "on" : ""}`}
      onClick={() => onChange(!on)}
    />
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (v: T) => void;
}) {
  return (
    <div className="w4-seg">
      {options.map((o) => (
        <button
          key={o.value}
          className={value === o.value ? "on" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Empty({
  icon,
  title,
  text,
  action,
}: {
  icon: ReactNode;
  title?: string;
  text?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="w4-centered">
      <div className="w4-empty">
        <div className="w4-empty-icon">{icon}</div>
        {title ? <div className="w4-empty-title">{title}</div> : null}
        {text ? <div className="w4-empty-text">{text}</div> : null}
        {action ? <div style={{ marginTop: 16 }}>{action}</div> : null}
      </div>
    </div>
  );
}

/** 桌面端的「按 Esc 退出」提示 */
export function useEscape(fn: () => void, active = true) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") ref.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);
}
