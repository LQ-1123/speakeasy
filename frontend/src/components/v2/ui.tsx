"use client";

/** SpeakEasy 2.0 通用 UI 原语：加载/空态/错误条/徽标/按钮。 */

import type { ReactNode } from "react";
import { AlertCircle, Inbox, Loader2, RefreshCw } from "lucide-react";

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-8 text-[13px] text-[color:var(--se-dim)]">
      <Loader2 size={15} className="animate-spin" />
      {label ?? "加载中…"}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  hint,
  action,
}: {
  icon?: ReactNode;
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-8 py-12 text-center">
      <div className="mb-1 flex h-14 w-14 items-center justify-center rounded-full bg-black/[0.04] text-[color:var(--se-faint)]">
        {icon ?? <Inbox size={26} strokeWidth={1.6} />}
      </div>
      <p className="text-[14px] font-medium text-[color:var(--se-text)]">{title}</p>
      {hint ? <p className="max-w-[260px] text-[12px] leading-relaxed text-[color:var(--se-dim)]">{hint}</p> : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}

export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="mx-3 mt-3 flex items-start gap-2 rounded-lg bg-[color:var(--se-danger-soft)] px-3 py-2.5 text-[12px] leading-relaxed text-[color:var(--se-danger)]">
      <AlertCircle size={14} className="mt-0.5 shrink-0" />
      <p className="min-w-0 flex-1">{message}</p>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="flex shrink-0 items-center gap-1 rounded-md bg-white/70 px-2 py-1 text-[11px] font-medium text-[color:var(--se-danger)] active:opacity-70"
        >
          <RefreshCw size={11} />
          重试
        </button>
      ) : null}
    </div>
  );
}

export function Chip({
  children,
  active,
  onClick,
  tone = "default",
}: {
  children: ReactNode;
  active?: boolean;
  onClick?: () => void;
  tone?: "default" | "green" | "amber";
}) {
  const base =
    tone === "amber"
      ? "border-[#f6c98a] bg-[#fff7ea] text-[#b57418]"
      : active
        ? "border-[color:var(--se-green)] bg-[color:var(--se-green-soft)] text-[color:var(--se-green-ink)]"
        : "border-black/[0.08] bg-white text-[color:var(--se-dim)]";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`shrink-0 rounded-full border px-3 py-1.5 text-[12px] font-medium transition-colors active:opacity-70 ${base}`}
    >
      {children}
    </button>
  );
}

export function IconButton({
  children,
  label,
  onClick,
  disabled,
  className = "",
}: {
  children: ReactNode;
  label: string;
  onClick?: () => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className={`flex h-9 w-9 items-center justify-center rounded-full text-[color:var(--se-text)] transition-colors active:bg-black/[0.06] disabled:opacity-35 ${className}`}
    >
      {children}
    </button>
  );
}

export function SectionLabel({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between px-4 pb-1.5 pt-4">
      <span className="text-[12px] font-semibold tracking-wide text-[color:var(--se-dim)]">{children}</span>
      {right}
    </div>
  );
}

export function StatusDot({ tone = "green" }: { tone?: "green" | "gray" | "amber" }) {
  const color = tone === "green" ? "bg-[#07c160]" : tone === "amber" ? "bg-[#fa9d3b]" : "bg-[#c4c4c4]";
  return <span className={`inline-block h-[7px] w-[7px] shrink-0 rounded-full ${color}`} />;
}
