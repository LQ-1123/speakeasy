"use client";

import { useEffect, useState, type FormEvent } from "react";
import { X } from "lucide-react";
import "../styles/auth.css";
import { normalizeEmail, validateEmail } from "./authFlow";
import { AuthError, requestEmailCode, verifyEmailCode, type EmailCodeChallenge } from "./session";

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
    <div className={`w4-root se-auth-screen${onClose ? " is-modal" : ""}`} role={onClose ? "dialog" : "main"} aria-modal={onClose ? true : undefined} aria-label="邮箱登录">
      <div className="se-auth-panel">
        <div className="se-auth-brand">
          <span className="se-auth-wordmark">SONDER</span>
          {onClose ? <button type="button" className="w4-icon-btn" onClick={onClose} aria-label="关闭"><X size={20} /></button> : null}
        </div>
        <h1 className="se-auth-title">邮箱验证码登录</h1>
        <form onSubmit={submit} className="se-auth-form" aria-busy={busy}>
          <div className="w4-field">
            <label htmlFor="auth-email" className="w4-field-label">邮箱</label>
            <input id="auth-email" className="w4-input" type="email" required autoComplete="email" placeholder="you@example.com" value={email} disabled={Boolean(challenge)} onChange={(e) => setEmail(e.target.value)} />
          </div>
          {challenge ? <>
            <div className="w4-field">
              <label htmlFor="auth-code" className="w4-field-label">邮箱验证码（10 分钟内有效）</label>
              <input id="auth-code" className="w4-input se-auth-code" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={(e) => { setCode(e.target.value.replace(/\D/g, "")); setInviteRequired(false); }} />
            </div>
            {inviteRequired ? <div className="w4-field">
              <label htmlFor="auth-invite" className="w4-field-label">新账号邀请码（内测）</label>
              <input id="auth-invite" className="w4-input" autoComplete="off" value={invite} onChange={(e) => setInvite(e.target.value)} />
              <p className="w4-field-hint">验证码已通过。首次创建账号需要邀请码，填写后继续即可。</p>
            </div> : null}
            <div className="se-auth-actions">
              <button type="button" className="w4-btn is-tonal" disabled={busy || seconds > 0} onClick={() => void send()}>{seconds > 0 ? `${seconds} 秒后重发` : "重新发送验证码"}</button>
              <button type="button" className="w4-btn" disabled={busy} onClick={() => { setChallenge(null); setInviteRequired(false); setError(""); }}>更换邮箱</button>
            </div>
          </> : null}
          {error ? <div role="alert" className="se-auth-error">{error}</div> : null}
          <button type="submit" className="w4-btn is-primary se-auth-submit" disabled={busy || (!challenge && seconds > 0)}>{busy ? "处理中…" : challenge ? inviteRequired ? "创建账号并登录" : "验证并登录" : seconds > 0 ? `${seconds} 秒后重试` : "发送验证码"}</button>
          <p className="w4-field-hint se-auth-note">登录后保存你的聊天、表达和人物关系。已有账号直接登录，新账号在验证邮箱后填写邀请码。</p>
        </form>
      </div>
    </div>
  );
}
