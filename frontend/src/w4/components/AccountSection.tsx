"use client";

import { useCallback, useEffect, useState } from "react";
import { LogOut, ShieldCheck, UserRound } from "lucide-react";

import {
  authIdentity,
  hasSession,
  listSessions,
  readSession,
  revokeSession,
  type AuthIdentity,
  type AuthSessionRow,
} from "../v3/session";
import { performLogout } from "../v3/logoutFlow";

function openAuth() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent("sonder:open-auth"));
}

const cardStyle: React.CSSProperties = {
  display: "grid",
  gap: 10,
  padding: "14px 16px",
  borderRadius: 14,
  border: "1px solid rgba(120,120,120,.18)",
};

const buttonStyle: React.CSSProperties = {
  minHeight: 36,
  padding: "0 14px",
  borderRadius: 10,
  border: "1px solid rgba(120,120,120,.28)",
  background: "transparent",
  color: "inherit",
  fontSize: 13,
  cursor: "pointer",
};

const primaryButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  border: "none",
  background: "var(--accent, #4f7cff)",
  color: "#fff",
  fontWeight: 600,
};

const muted: React.CSSProperties = { fontSize: 12, opacity: 0.72, lineHeight: 1.5 };

export function AccountSection() {
  const [signedIn, setSignedIn] = useState(() => hasSession());
  const session = readSession();
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);
  const [sessions, setSessions] = useState<AuthSessionRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (!hasSession()) {
      setSignedIn(false);
      return;
    }
    setSignedIn(true);
    try {
      const [who, rows] = await Promise.all([authIdentity(), listSessions()]);
      setIdentity(who);
      setSessions(rows.items ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function signOut(all: boolean) {
    setBusy(true);
    setError("");
    try {
      /* AuthGate 收到退出事件后卸载业务界面和事件连接。 */
      await performLogout(all);
      setSignedIn(false);
      setIdentity(null);
      setSessions([]);

    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(sessionId: string) {
    setBusy(true);
    try {
      await revokeSession(sessionId);
      setSessions((prev) => prev.filter((row) => row.session_id !== sessionId));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (!signedIn) {
    return (
      <div style={{ display: "grid", gap: 14 }}>
        <div style={cardStyle}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
            <UserRound size={18} strokeWidth={1.8} />
            邮箱账号（内测）
          </div>
          <p style={muted}>
            登录后：多设备同步、可撤销设备会话、数据隔离（动态/私聊情绪/人物关系互不可见）。
            内测注册需要邀请码。
          </p>
          <button type="button" style={primaryButtonStyle} onClick={openAuth}>
            登录 / 注册
          </button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={cardStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
          <UserRound size={18} strokeWidth={1.8} />
          {session?.email || identity?.email || "已登录"}
        </div>
        <p style={muted}>
          用户 ID：{session?.user_id || identity?.user_id || "-"}
          {(identity?.email_verified ?? session?.email_verified) ? " · 邮箱已验证" : " · 邮箱未验证"}
        </p>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" style={buttonStyle} disabled={busy} onClick={() => void signOut(false)}>
            <LogOut size={14} style={{ verticalAlign: "-2px", marginRight: 6 }} />
            退出登录
          </button>
          <button type="button" style={buttonStyle} disabled={busy} onClick={() => void signOut(true)}>
            退出全部设备
          </button>
        </div>
        {error ? <p style={{ ...muted, color: "#d33" }}>{error}</p> : null}
      </div>

      <div style={cardStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
          <ShieldCheck size={18} strokeWidth={1.8} />
          设备会话
        </div>
        {sessions.length === 0 ? (
          <p style={muted}>暂无可管理的会话。</p>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            {sessions.map((row) => (
              <div
                key={row.session_id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 10,
                  fontSize: 13,
                }}
              >
                <span style={{ opacity: 0.82 }}>
                  {row.platform || row.device_key || "未知设备"} · {row.created_at?.slice(0, 10)}
                  {row.current ? " · 当前会话" : ""}
                </span>
                {row.current ? null : (
                  <button
                    type="button"
                    style={buttonStyle}
                    disabled={busy}
                    onClick={() => void revoke(row.session_id)}
                  >
                    撤销
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
