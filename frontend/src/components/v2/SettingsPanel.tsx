"use client";

/**
 * 个人设置面板（已解耦）。
 *
 * 模型 / 密钥 / 语音识别 / 语音合成配置全部移到本机管理端 `/admin`，
 * 客户端只保留这个只读说明，避免零基础客户改坏配置。
 */

interface SettingsPanelProps {
  onClose: () => void;
  /** 保留字段：历史上用于保存后刷新全局状态 */
  onChanged?: () => void;
}

export function SettingsPanel({ onClose }: SettingsPanelProps) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="设置说明"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 70,
        background: "#0f1115",
        color: "#e8ebf1",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "14px 18px",
          borderBottom: "1px solid #2a3040",
        }}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="返回"
          style={{
            background: "transparent",
            border: "1px solid #2a3040",
            borderRadius: 8,
            color: "#e8ebf1",
            padding: "5px 12px",
            cursor: "pointer",
          }}
        >
          返回
        </button>
        <strong style={{ fontSize: 15 }}>设置</strong>
      </header>
      <main style={{ padding: "22px 18px", maxWidth: 620, lineHeight: 1.7 }}>
        <h2 style={{ fontSize: 16, margin: "0 0 8px" }}>配置由管理端统一管理</h2>
        <p style={{ margin: "0 0 12px", color: "#9aa4b5", fontSize: 13.5 }}>
          对话模型、语音识别、语音合成的密钥与参数都收在本机管理端，客户端不再提供修改入口，
          这样任何误操作都不会把应用改到不可用。
        </p>
        <p style={{ margin: "0 0 16px", fontSize: 13.5 }}>
          <a
            href="/admin"
            target="_blank"
            rel="noreferrer"
            style={{ color: "#5bd1a5", textDecoration: "underline" }}
          >
            打开管理端 /admin
          </a>
        </p>
        <p style={{ margin: 0, color: "#9aa4b5", fontSize: 12.5 }}>
          学习的语言、兴趣等偏好仍可在使用过程中设置，不涉及密钥。
        </p>
      </main>
    </div>
  );
}
