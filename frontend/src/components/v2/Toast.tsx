"use client";

/** 轻量中文 Toast：顶部浮层，自动消失；全局 context 便于任意组件调用。 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

type ToastKind = "info" | "success" | "error";

interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
}

interface ToastContextValue {
  showToast: (text: string, kind?: ToastKind) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) return { showToast: () => undefined };
  return ctx;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const idRef = useRef(0);
  const timersRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((item) => item.id !== id));
    const timer = timersRef.current.get(id);
    if (timer) clearTimeout(timer);
    timersRef.current.delete(id);
  }, []);

  const showToast = useCallback(
    (text: string, kind: ToastKind = "info") => {
      if (!text) return;
      idRef.current += 1;
      const id = idRef.current;
      setItems((prev) => [...prev.slice(-2), { id, kind, text }]);
      const timer = setTimeout(() => {
        setItems((prev) => prev.filter((item) => item.id !== id));
        timersRef.current.delete(id);
      }, 2600);
      timersRef.current.set(id, timer);
    },
    [],
  );

  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      timers.forEach((timer) => clearTimeout(timer));
      timers.clear();
    };
  }, []);

  const value = useMemo(() => ({ showToast }), [showToast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none absolute inset-x-0 top-0 z-50 flex flex-col items-center gap-2 px-6 pt-[calc(env(safe-area-inset-top)+58px)]">
        {items.map((item) => {
          const Icon = item.kind === "success" ? CheckCircle2 : item.kind === "error" ? AlertTriangle : Info;
          const tone =
            item.kind === "success"
              ? "bg-[#eefbf1] text-[#0d7a3c] border-[#bce6c9]"
              : item.kind === "error"
                ? "bg-[#fff0f0] text-[#c53a3a] border-[#f4c4c4]"
                : "bg-white/95 text-[#333] border-black/[0.08]";
          return (
            <div
              key={item.id}
              className={`pointer-events-auto flex max-w-full items-start gap-2 rounded-xl border px-3.5 py-2.5 text-[12.5px] leading-snug shadow-[0_6px_20px_rgba(0,0,0,0.08)] backdrop-blur ${tone}`}
            >
              <Icon size={14} className="mt-0.5 shrink-0" />
              <p className="min-w-0 flex-1">{item.text}</p>
              <button
                type="button"
                aria-label="关闭提示"
                onClick={() => dismiss(item.id)}
                className="shrink-0 opacity-50 transition-opacity hover:opacity-100"
              >
                <X size={12} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
