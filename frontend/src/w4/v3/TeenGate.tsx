"use client";

/**
 * 青少年模式门禁（全屏拦截 + 连续 30 分钟提醒）。
 *
 * - 登录账号后每 60 秒心跳一次：顺带累计当日使用时长，并拿到最新 `access`；
 * - `access.allowed === false` → 全屏拦截（宵禁/超限/未核验/未监护/限定人物）；
 * - `access.reminder === true` → 每 30 分钟弹一次「休息一下」，不强制断开；
 * - 未登录（设备身份/local）不发请求，不受影响。
 */

import { useEffect, useRef, useState } from "react";

import { teenHeartbeat, type TeenStatus } from "./accountClient";
import { hasSession } from "./session";
import { teenBlockReason, teenBlockText, teenReminderDue } from "./teenGuard";

const HEARTBEAT_MS = 60_000;
const REMINDER_COOLDOWN_MS = 30 * 60 * 1000;

export function TeenGate() {
  const [status, setStatus] = useState<TeenStatus | null>(null);
  const [reminder, setReminder] = useState(false);
  const lastReminderAt = useRef(0);

  useEffect(() => {
    if (!hasSession()) return;
    let stopped = false;
    let timer: number | undefined;

    const tick = async () => {
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        timer = window.setTimeout(tick, HEARTBEAT_MS);
        return;
      }
      try {
        const next = await teenHeartbeat();
        if (stopped) return;
        setStatus(next);
        const blocked = Boolean(teenBlockReason(next?.access));
        if (!blocked && teenReminderDue(next?.access, lastReminderAt.current, Date.now(), REMINDER_COOLDOWN_MS)) {
          lastReminderAt.current = Date.now();
          setReminder(true);
        }
      } catch {
        /* 网络/门禁接口失败不阻塞使用；下一次心跳再试 */
      }
      if (!stopped) timer = window.setTimeout(tick, HEARTBEAT_MS);
    };

    void tick();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  const blockReason = teenBlockReason(status?.access);
  if (blockReason) {
    return (
      <div
        role="alertdialog"
        aria-modal="true"
        aria-label="青少年模式"
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 90,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: 24,
          background: "var(--w4-bg, #0f1115)",
          color: "inherit",
        }}
      >
        <div style={{ maxWidth: 420, textAlign: "center", display: "grid", gap: 12 }}>
          <div style={{ fontSize: 18, fontWeight: 700 }}>青少年模式</div>
          <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7, opacity: 0.85 }}>
            {teenBlockText(blockReason, status?.access?.retry_after_s ?? 0)}
          </p>
          <p style={{ margin: 0, fontSize: 12, opacity: 0.6 }}>
            如需调整，请由监护人联系运营或在设置 · 账号查看当前状态。
          </p>
        </div>
      </div>
    );
  }

  if (!reminder) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="休息提醒"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 85,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        background: "rgba(0,0,0,.45)",
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) setReminder(false);
      }}
    >
      <div
        style={{
          maxWidth: 360,
          padding: 22,
          borderRadius: 16,
          background: "var(--w4-surface, #fff)",
          display: "grid",
          gap: 10,
          textAlign: "center",
        }}
      >
        <div style={{ fontSize: 16, fontWeight: 700 }}>已经连续使用 30 分钟</div>
        <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6, opacity: 0.8 }}>
          起身活动一下、看看远处，休息好了再继续。今天还有时间，不用急。
        </p>
        <button
          type="button"
          onClick={() => setReminder(false)}
          style={{
            minHeight: 40,
            borderRadius: 10,
            border: "none",
            background: "var(--accent, #4f7cff)",
            color: "#fff",
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          好，休息一下
        </button>
      </div>
    </div>
  );
}
