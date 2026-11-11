"use client";

/** 内容举报弹层：选择类别 + 可选说明，提交到 /api/v3/reports。 */

import { useState } from "react";

import { reportContent, type ReportTarget } from "./accountClient";
import { describeAuthFailure } from "./authFlow";

const CATEGORIES: Array<{ key: string; label: string }> = [
  { key: "harassment", label: "骚扰 / 攻击" },
  { key: "sexual", label: "色情 / 露骨内容" },
  { key: "violence", label: "暴力或威胁" },
  { key: "self_harm", label: "自伤 / 危机内容" },
  { key: "misinformation", label: "虚假信息" },
  { key: "spam", label: "垃圾营销 / 刷屏" },
  { key: "other", label: "其他" },
];

export function ReportDialog({
  targetType,
  targetId,
  onClose,
  onDone,
}: {
  targetType: ReportTarget;
  targetId: string;
  onClose: () => void;
  onDone?: (message: string) => void;
}) {
  const [category, setCategory] = useState("harassment");
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);

  async function submit() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await reportContent(targetType, targetId, category, detail.trim());
      setSent(true);
      onDone?.("已提交举报，运营会尽快处理");
    } catch (err) {
      setError(describeAuthFailure(err instanceof Error ? err.message : String(err)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="举报"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 70,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
        background: "rgba(0,0,0,.42)",
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        style={{
          width: "min(380px, 100%)",
          borderRadius: 16,
          padding: 20,
          background: "var(--w4-surface, #fff)",
          color: "inherit",
          boxShadow: "0 20px 50px rgba(0,0,0,.26)",
          display: "grid",
          gap: 12,
        }}
      >
        <div style={{ fontWeight: 600 }}>举报这条内容</div>
        {sent ? (
          <>
            <p style={{ margin: 0, fontSize: 13, opacity: 0.78 }}>
              已提交，运营会尽快核查。你可以在设置 · 账号里查看处理状态（同一内容 24 小时内只需举报一次）。
            </p>
            <button
              type="button"
              onClick={onClose}
              style={{ minHeight: 38, borderRadius: 10, border: "none", background: "var(--accent,#4f7cff)", color: "#fff", cursor: "pointer" }}
            >
              知道了
            </button>
          </>
        ) : (
          <>
            <div style={{ display: "grid", gap: 6 }}>
              {CATEGORIES.map((item) => (
                <label key={item.key} style={{ display: "flex", gap: 8, fontSize: 13 }}>
                  <input
                    type="radio"
                    name="report-category"
                    value={item.key}
                    checked={category === item.key}
                    onChange={() => setCategory(item.key)}
                  />
                  {item.label}
                </label>
              ))}
            </div>
            <textarea
              value={detail}
              onChange={(event) => setDetail(event.target.value)}
              maxLength={500}
              rows={3}
              placeholder="补充说明（可选）"
              style={{
                width: "100%",
                padding: 10,
                borderRadius: 10,
                border: "1px solid rgba(120,120,120,.28)",
                background: "rgba(127,127,127,.08)",
                color: "inherit",
                fontSize: 13,
                resize: "vertical",
              }}
            />
            {error ? <div role="alert" style={{ color: "#d33", fontSize: 13 }}>{error}</div> : null}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button
                type="button"
                onClick={onClose}
                style={{ minHeight: 36, padding: "0 14px", borderRadius: 10, border: "1px solid rgba(120,120,120,.28)", background: "transparent", color: "inherit", cursor: "pointer" }}
              >
                取消
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void submit()}
                style={{ minHeight: 36, padding: "0 14px", borderRadius: 10, border: "none", background: "var(--accent,#4f7cff)", color: "#fff", cursor: "pointer" }}
              >
                {busy ? "提交中…" : "提交举报"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
