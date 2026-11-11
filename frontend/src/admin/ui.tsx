"use client";

/**
 * 管理台通用 UI 组件。
 *
 * 原则
 * ----
 * 1. **只用 --md-* 语义变量**：不写死颜色，主题切换/壁纸取色都不会让管理台花掉。
 * 2. **不引入新依赖**：现有 @tanstack/react-query / lucide-react / zustand 足够。
 * 3. **可访问性默认正确**：按钮是真 button，抽屉/对话框有 role 与 Esc 关闭，
 *    表单控件都有 label 关联——这些一次性做对，后面 16 个分区就不用重复想。
 */

import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";

/* -------------------------------------------------------------------------- */
/* 状态：空 / 载入 / 错误                                                       */
/* -------------------------------------------------------------------------- */

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="admin-empty">
      <div className="admin-empty-title">{title}</div>
      {description ? <div className="admin-empty-desc">{description}</div> : null}
      {action}
    </div>
  );
}

export function LoadingState({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="admin-loading" role="status" aria-live="polite">
      {label}
    </div>
  );
}

export function ErrorState({
  title = "出错了",
  message,
  issues,
  onRetry,
}: {
  title?: string;
  message?: string;
  issues?: string[];
  onRetry?: () => void;
}) {
  return (
    <div className="admin-error" role="alert">
      <strong>{title}</strong>
      {message ? <div>{message}</div> : null}
      {issues && issues.length ? (
        <ul className="admin-error-issues">
          {issues.map((issue, index) => (
            <li key={`${index}-${issue}`}>{issue}</li>
          ))}
        </ul>
      ) : null}
      {onRetry ? (
        <div>
          <button type="button" className="admin-btn" onClick={onRetry}>
            重试
          </button>
        </div>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 按钮                                                                        */
/* -------------------------------------------------------------------------- */

export function Button({
  children,
  onClick,
  variant = "default",
  size,
  disabled,
  type = "button",
  title,
  loading,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  size?: "sm";
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
  loading?: boolean;
}) {
  return (
    <button
      type={type}
      className="admin-btn"
      data-variant={variant === "default" ? undefined : variant}
      data-size={size}
      onClick={onClick}
      disabled={disabled || loading}
      title={title}
    >
      {loading ? "…" : children}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* 表单控件                                                                    */
/* -------------------------------------------------------------------------- */

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div className="admin-field">
      <label className="admin-field-label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error ? <div className="admin-field-error">{error}</div> : null}
      {hint && !error ? <div className="admin-field-hint">{hint}</div> : null}
    </div>
  );
}

export function TextInput({
  value,
  onChange,
  placeholder,
  type = "text",
  invalid,
  disabled,
  id,
  onEnter,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: "text" | "password" | "number" | "url" | "search";
  invalid?: boolean;
  disabled?: boolean;
  id?: string;
  onEnter?: () => void;
}) {
  return (
    <input
      id={id}
      className="admin-input"
      type={type}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      aria-invalid={invalid ? "true" : undefined}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter" && onEnter) {
          event.preventDefault();
          onEnter();
        }
      }}
    />
  );
}

export function TextArea({
  value,
  onChange,
  placeholder,
  rows,
  mono,
  invalid,
  disabled,
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  rows?: number;
  mono?: boolean;
  invalid?: boolean;
  disabled?: boolean;
  id?: string;
}) {
  return (
    <textarea
      id={id}
      className="admin-textarea"
      value={value}
      rows={rows}
      placeholder={placeholder}
      disabled={disabled}
      data-mono={mono ? "true" : undefined}
      aria-invalid={invalid ? "true" : undefined}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function Select<T extends string>({
  value,
  onChange,
  options,
  disabled,
  id,
}: {
  value: T;
  onChange: (value: T) => void;
  options: Array<{ value: T; label: string }>;
  disabled?: boolean;
  id?: string;
}) {
  return (
    <select
      id={id}
      className="admin-select"
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value as T)}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

/** 滑杆 + 数值显示。用于世界节奏、DNA 数值等参数化输入。 */
export function Slider({
  label,
  hint,
  value,
  min,
  max,
  step,
  onChange,
  disabled,
  format,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  format?: (value: number) => string;
}) {
  const id = useId();
  return (
    <div className="admin-slider-row">
      <div>
        <label className="admin-slider-label" htmlFor={id}>
          {label}
        </label>
        {hint ? <div className="admin-slider-hint">{hint}</div> : null}
      </div>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <div className="admin-slider-value">{format ? format(value) : String(value)}</div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 分段按钮                                                                    */
/* -------------------------------------------------------------------------- */

export function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (value: T) => void;
  options: Array<{ value: T; label: string }>;
}) {
  return (
    <div className="admin-segmented" role="group">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 标签云                                                                      */
/* -------------------------------------------------------------------------- */

export function TagCloud({
  tags,
  onRemove,
  onAdd,
  placeholder = "输入后回车添加",
  addLabel = "添加",
}: {
  tags: string[];
  onRemove?: (tag: string) => void;
  onAdd?: (tag: string) => void;
  placeholder?: string;
  addLabel?: string;
}) {
  const [draft, setDraft] = useState("");
  const commit = () => {
    const value = draft.trim();
    if (!value || !onAdd) return;
    onAdd(value);
    setDraft("");
  };
  return (
    <div className="admin-col">
      <div className="admin-tagcloud">
        {tags.length === 0 ? <span className="admin-muted">（空）</span> : null}
        {tags.map((tag) => (
          <span className="admin-tag" key={tag}>
            {tag}
            {onRemove ? (
              <button
                type="button"
                className="admin-tag-x"
                aria-label={`移除 ${tag}`}
                onClick={() => onRemove(tag)}
              >
                ×
              </button>
            ) : null}
          </span>
        ))}
      </div>
      {onAdd ? (
        <div className="admin-row">
          <div style={{ flex: "1 1 180px" }}>
            <TextInput value={draft} onChange={setDraft} placeholder={placeholder} onEnter={commit} />
          </div>
          <Button onClick={commit} disabled={!draft.trim()}>
            {addLabel}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 表格                                                                        */
/* -------------------------------------------------------------------------- */

export interface Column<T> {
  key: string;
  header: string;
  /** 单元格渲染。返回 ReactNode；null/undefined 渲染成 "—"。 */
  render: (row: T) => ReactNode;
  /** 数字列右对齐。 */
  numeric?: boolean;
  width?: string;
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  empty,
  loading,
}: {
  columns: Array<Column<T>>;
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  empty?: ReactNode;
  loading?: boolean;
}) {
  if (loading) return <LoadingState />;
  if (!rows.length) {
    return <div className="admin-table-wrap">{empty ?? <div className="admin-table-empty">暂无数据</div>}</div>;
  }
  return (
    <div className="admin-table-wrap">
      <table className="admin-table" data-clickable={onRowClick ? "true" : undefined}>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} style={column.width ? { width: column.width } : undefined}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((column) => {
                const value = column.render(row);
                return (
                  <td key={column.key} className={column.numeric ? "admin-num" : undefined}>
                    {value === null || value === undefined || value === "" ? (
                      <span className="admin-muted">—</span>
                    ) : (
                      value
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 分页条。offset/limit 语义与后端一致。 */
export function Pager({
  total,
  offset,
  limit,
  onChange,
}: {
  total: number;
  offset: number;
  limit: number;
  onChange: (offset: number) => void;
}) {
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="admin-pager">
      <span className="admin-muted">
        共 {total} 条 · 第 {page}/{pages} 页
      </span>
      <Button size="sm" disabled={offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        上一页
      </Button>
      <Button
        size="sm"
        disabled={offset + limit >= total}
        onClick={() => onChange(offset + limit)}
      >
        下一页
      </Button>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 徽章                                                                        */
/* -------------------------------------------------------------------------- */

export function Badge({
  children,
  tone,
}: {
  children: ReactNode;
  tone?: "success" | "error" | "warn" | "info";
}) {
  return (
    <span className="admin-badge" data-tone={tone}>
      {children}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* 抽屉                                                                        */
/* -------------------------------------------------------------------------- */

export function Drawer({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const titleId = useId();
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <>
      <div className="admin-drawer-backdrop" onClick={onClose} aria-hidden="true" />
      <aside className="admin-drawer" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="admin-drawer-head">
          <h2 className="admin-drawer-title" id={titleId}>
            {title}
          </h2>
          <div style={{ marginLeft: "auto" }}>
            <Button variant="ghost" size="sm" onClick={onClose} title="关闭（Esc）">
              ×
            </Button>
          </div>
        </div>
        <div className="admin-drawer-body">{children}</div>
        {footer ? <div className="admin-drawer-foot">{footer}</div> : null}
      </aside>
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* 确认对话框                                                                  */
/* -------------------------------------------------------------------------- */

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "确认",
  cancelLabel = "取消",
  danger,
  onConfirm,
  onCancel,
  busy,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
}) {
  const titleId = useId();
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;
  return (
    <div className="admin-dialog-backdrop" onClick={onCancel}>
      <div
        className="admin-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="admin-dialog-title" id={titleId}>
          {title}
        </h2>
        <div className="admin-dialog-body">{body}</div>
        <div className="admin-dialog-actions">
          <Button onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} loading={busy}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Toast                                                                       */
/* -------------------------------------------------------------------------- */

export interface ToastItem {
  id: number;
  message: string;
  tone?: "info" | "error";
}

let toastSeq = 0;

/**
 * 极简 toast：调用方持有一个 state 数组 + push 函数。
 * 不做全局单例——管理台每个分区独立挂载，全局单例会引入清理顺序问题。
 */
export function useToasts(): {
  toasts: ToastItem[];
  push: (message: string, tone?: "info" | "error") => void;
} {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const push = (message: string, tone: "info" | "error" = "info") => {
    const id = ++toastSeq;
    setToasts((current) => [...current, { id, message, tone }]);
    window.setTimeout(() => {
      setToasts((current) => current.filter((item) => item.id !== id));
    }, 4200);
  };
  return { toasts, push };
}

export function ToastStack({ toasts }: { toasts: ToastItem[] }) {
  if (!toasts.length) return null;
  return (
    <div className="admin-toast-stack" aria-live="polite">
      {toasts.map((toast) => (
        <div className="admin-toast" key={toast.id} data-tone={toast.tone === "error" ? "error" : undefined}>
          {toast.message}
        </div>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 音频试听                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * 音频试听：按需拉 blob 再播放，避免把管理会话 token 塞进 URL。
 * `load` 返回 Blob；组件负责 object URL 的生命周期。
 */
export function AudioPreview({
  load,
  label = "试听",
  disabled,
}: {
  load: () => Promise<Blob>;
  label?: string;
  disabled?: boolean;
}) {
  const [src, setSrc] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const urlRef = useRef("");

  useEffect(() => {
    return () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    };
  }, []);

  const play = async () => {
    setBusy(true);
    setError("");
    try {
      const blob = await load();
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      const url = URL.createObjectURL(blob);
      urlRef.current = url;
      setSrc(url);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "试听失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="admin-audio">
      <Button size="sm" onClick={play} loading={busy} disabled={disabled}>
        {label}
      </Button>
      {src ? <audio src={src} controls autoPlay /> : null}
      {error ? <span className="admin-field-error">{error}</span> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 图片选择器                                                                  */
/* -------------------------------------------------------------------------- */

export interface ImageOption {
  url: string;
  label?: string;
}

export function ImagePicker({
  images,
  selected,
  onSelect,
  onRefresh,
}: {
  images: ImageOption[];
  selected?: string;
  onSelect: (url: string) => void;
  onRefresh?: () => void;
}) {
  return (
    <div className="admin-col">
      {onRefresh ? (
        <div className="admin-row">
          <Button size="sm" onClick={onRefresh}>
            刷新图库
          </Button>
        </div>
      ) : null}
      {images.length === 0 ? (
        <EmptyState title="图库为空" description="先去「图片素材」入库或上传图片。" />
      ) : (
        <div className="admin-img-grid">
          {images.map((image) => (
            <button
              type="button"
              key={image.url}
              className="admin-img-cell"
              aria-pressed={image.url === selected}
              onClick={() => onSelect(image.url)}
              title={image.label || image.url}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={image.url} alt={image.label || ""} loading="lazy" />
              {image.label ? <span className="admin-img-cell-label">{image.label}</span> : null}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
