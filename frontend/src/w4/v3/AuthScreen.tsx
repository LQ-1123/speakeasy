"use client";

import { useEffect, useState, type FormEvent } from "react";
import { normalizeEmail, validateEmail } from "./authFlow";
import { AuthError, requestEmailCode, verifyEmailCode, type EmailCodeChallenge } from "./session";

const inputStyle: React.CSSProperties = {
  width: "100%",
  minHeight: 42,
  padding: "10px 12px",
  borderRadius: 10,
  border: "1px solid rgba(120,120,120,.28)",
  background: "var(--w4-input-bg, rgba(127,127,127,.08))",
  color: "inherit",
  fontSize: 14,
};

const labelStyle: React.CSSProperties = {
  display: "block",
  fontSize: 12,
  opacity: 0.75,
  marginBottom: 6,
};

const primaryStyle: React.CSSProperties = {
  width: "100%",
  minHeight: 44,
  borderRadius: 12,
  border: "none",
  background: "var(--accent, #4f7cff)",
  color: "#fff",
  fontSize: 15,
  fontWeight: 600,
  cursor: "pointer",
};

const linkStyle: React.CSSProperties = {
  background: "none",
  border: "none",
  color: "var(--accent, #4f7cff)",
  fontSize: 13,
  cursor: "pointer",
  padding: 0,
};

export function AuthScreen({ onClose, onAuthenticated }: { onClose?: () => void; onAuthenticated?: () => void }) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [invite, setInvite] = useState("");
  const [challenge, setChallenge] = useState<EmailCodeChallenge | null>(null);
  const [inviteRequired, setInviteRequired] = useState(false);
  const [retryAt, setRetryAt] = useState(0);
  const [seconds, setSeconds] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const tick = () => setSeconds(Math.max(0, Math.ceil((retryAt - Date.now()) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [retryAt]);

  async function send() {
    if (busy || Date.now() < retryAt) return;
    const invalid = validateEmail(email);
    if (invalid) { setError(invalid); return; }
    setBusy(true); setError("");
    try {
      const next = await requestEmailCode(normalizeEmail(email));
      setChallenge(next); setCode(""); setInvite(""); setInviteRequired(false);
      setRetryAt(Date.now() + next.retry_after * 1000);
    } catch (err) {
      if (err instanceof AuthError && err.retryAfter) setRetryAt(Date.now() + err.retryAfter * 1000);
      setError(err instanceof Error ? err.message : "发送失败，请稍后重试");
    } finally { setBusy(false); }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!challenge) { await send(); return; }
    if (busy) return;
    if (!/^\d{6}$/.test(code)) { setError("请输入 6 位邮箱验证码"); return; }
    if (inviteRequired && !invite.trim()) { setError("请输入邀请码"); return; }
    setBusy(true); setError("");
    try {
      await verifyEmailCode({ challenge_id: challenge.challenge_id, code, ...(inviteRequired ? { invite_code: invite.trim() } : {}) });
      onAuthenticated?.();
    } catch (err) {
      if (err instanceof AuthError && err.code === "INVITE_REQUIRED") setInviteRequired(true);
      setError(err instanceof Error ? err.message : "验证失败，请重试");
    } finally { setBusy(false); }
  }

  return (
    <div role="dialog" aria-modal="true" aria-label="邮箱登录" style={{ position: "fixed", inset: 0, zIndex: 60, display: "flex", alignItems: "center", justifyContent: "center", padding: 20, background: "rgba(0,0,0,.42)" }}>
      <div style={{ width: "min(420px, 100%)", borderRadius: 18, padding: "22px 22px 18px", background: "var(--w4-surface, #fff)", color: "var(--w4-fg, #111)", boxShadow: "0 24px 60px rgba(0,0,0,.28)" }}>
        <div style={{ display: "flex", alignItems: "center", marginBottom: 16 }}><strong>邮箱验证码登录</strong><span style={{ flex: 1 }} />{onClose ? <button type="button" onClick={onClose} style={linkStyle}>关闭</button> : null}</div>
        <form onSubmit={submit} style={{ display: "grid", gap: 12 }}>
          <div><label htmlFor="auth-email" style={labelStyle}>邮箱</label><input id="auth-email" type="email" required autoComplete="email" placeholder="you@example.com" value={email} disabled={Boolean(challenge)} onChange={(e) => setEmail(e.target.value)} style={inputStyle} /></div>
          {challenge ? <>
            <div><label htmlFor="auth-code" style={labelStyle}>邮箱验证码（10 分钟内有效）</label><input id="auth-code" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={(e) => { setCode(e.target.value.replace(/\D/g, "")); setInviteRequired(false); }} style={inputStyle} /></div>
            {inviteRequired ? <div><label htmlFor="auth-invite" style={labelStyle}>新账号邀请码（内测）</label><input id="auth-invite" autoComplete="off" value={invite} onChange={(e) => setInvite(e.target.value)} style={inputStyle} /><p style={{ fontSize: 12, opacity: .72 }}>验证码已通过。首次创建账号需要邀请码，填写后继续即可。</p></div> : null}
            <div style={{ display: "flex", justifyContent: "space-between" }}><button type="button" disabled={busy || seconds > 0} onClick={() => void send()} style={linkStyle}>{seconds > 0 ? `${seconds} 秒后重发` : "重新发送验证码"}</button><button type="button" disabled={busy} onClick={() => { setChallenge(null); setInviteRequired(false); setError(""); }} style={linkStyle}>更换邮箱</button></div>
          </> : null}
          {error ? <div role="alert" style={{ color: "#d33", fontSize: 13 }}>{error}</div> : null}
          <button type="submit" disabled={busy || (!challenge && seconds > 0)} style={primaryStyle}>{busy ? "处理中…" : challenge ? inviteRequired ? "创建账号并登录" : "验证并登录" : seconds > 0 ? `${seconds} 秒后重试` : "发送验证码"}</button>
          <p style={{ margin: 0, fontSize: 12, opacity: .7, lineHeight: 1.5 }}>登录后保存你的聊天、表达和人物关系。已有账号直接登录，新账号在验证邮箱后填写邀请码。</p>
        </form>
      </div>
    </div>
  );
}
