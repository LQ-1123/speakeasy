"use client";

/**
 * 客户端「配置已锁定」提示。
 *
 * 模型 / 密钥 / TTS 配置统一收进本机管理端（/admin），客户端不再提供任何输入框，
 * 避免零基础客户误配置导致整个应用不可用。
 */
export function ConfigLockedDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="配置说明"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 80,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(8, 10, 14, 0.62)",
        padding: 20,
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: "min(440px, 92vw)",
          background: "#141821",
          border: "1px solid #2a3040",
          borderRadius: 14,
          padding: "18px 20px",
          color: "#e8ebf1",
          lineHeight: 1.65,
        }}
        onClick={(event) => event.stopPropagation()}
      >
        <div style={{ fontWeight: 650, fontSize: 15, marginBottom: 6 }}>配置由管理端统一管理</div>
        <p style={{ margin: "0 0 10px", color: "#9aa4b5", fontSize: 13 }}>
          为了不让配置被误改，客户端不再提供模型与密钥设置。所有配置都在本机管理端完成：
        </p>
        <p style={{ margin: "0 0 14px", fontSize: 13 }}>
          <a
            href="/admin"
            target="_blank"
            rel="noreferrer"
            style={{ color: "#5bd1a5", textDecoration: "underline" }}
          >
            打开本机管理端 /admin
          </a>
          <span style={{ color: "#9aa4b5" }}>（仅本机可访问）</span>
        </p>
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <button
            type="button"
            onClick={onClose}
            style={{
              background: "#1d2230",
              color: "#e8ebf1",
              border: "1px solid #2a3040",
              borderRadius: 8,
              padding: "7px 16px",
              cursor: "pointer",
            }}
          >
            知道了
          </button>
        </div>
      </div>
    </div>
  );
}
