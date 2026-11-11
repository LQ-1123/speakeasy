/**
 * 登录 / 注册 / 找回密码弹层（内测：邀请码 + 邮箱验证）。
 *
 * 只负责交互与错误展示，所有校验/文案映射在 `authFlow.ts`（有单测），
 * 网络与凭证持久化在 `session.ts`。成功后回调方决定是否刷新/继续。
 */

import { useState, type FormEvent } from "react";

import {
  authNotice,
  describeAuthFailure,
  hasErrors,
  normalizeEmail,
  validateAuthForm,
  validateEmail,
  type AuthMode,
} from "./authFlow";
import {
  loginEmail,
  registerEmail,
  requestPasswordReset,
  resetPassword,
} from "./session";

const MODES: Array<{ key: AuthMode; label: string }> = [
  { key: "login", label: "登录" },
  { key: "register", label: "注册" },
  { key: "forgot", label: "找回密码" },
];

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

export function AuthScreen({
  onClose,
  onAuthenticated,
}: {
  onClose?: () => void;
  onAuthenticated?: () => void;
}) {
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [resetToken, setResetToken] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const switchMode = (next: AuthMode) => {
    setMode(next);
    setErrors({});
    setNotice("");
  };

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const form = { email, password, confirm, inviteCode };
    const fieldErrors = validateAuthForm(mode, form);
    if (mode === "forgot" && !validateEmail(email)) {
      setErrors({ form: "请输入邮箱" });
      return;
    }
    if (hasErrors(fieldErrors)) {
      setErrors(fieldErrors as Record<string, string>);
      return;
    }
    setErrors({});
    setBusy(true);
    try {
      if (mode === "login") {
        await loginEmail(normalizeEmail(email), password, {
          device_key: "web",
          platform: "web",
        });
        onAuthenticated?.();
      } else if (mode === "register") {
        const result = await registerEmail({
          email: normalizeEmail(email),
          password,
          invite_code: inviteCode.trim(),
          device_key: "web",
        });
        setNotice(authNotice(result) || "注册成功，请到邮箱完成验证");
        onAuthenticated?.();
      } else if (!resetToken) {
        const result = await requestPasswordReset(normalizeEmail(email));
        setResetToken(result.dev_reset_token ?? "");
        setNotice(authNotice({ dev_reset_token: result.dev_reset_token }) || "重置链接已发送，请查收邮箱");
      }
    } catch (error) {
      setErrors({ form: describeAuthFailure(error instanceof Error ? error.message : String(error)) });
    } finally {
      setBusy(false);
    }
  }

  async function submitReset(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!resetToken.trim()) {
      setErrors({ form: "请填写邮件里的重置码" });
      return;
    }
    if (password.length < 8) {
      setErrors({ form: "新密码至少 8 位" });
      return;
    }
    setBusy(true);
    setErrors({});
    try {
      await resetPassword(resetToken.trim(), password);
      setNotice("密码已重置，请用新密码登录");
      setMode("login");
      setPassword("");
      setResetToken("");
    } catch (error) {
      setErrors({ form: describeAuthFailure(error instanceof Error ? error.message : String(error)) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="账号"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
        background: "rgba(0,0,0,.42)",
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <div
        style={{
          width: "min(420px, 100%)",
          borderRadius: 18,
          padding: "22px 22px 18px",
          background: "var(--w4-surface, #fff)",
          color: "var(--w4-fg, #111)",
          boxShadow: "0 24px 60px rgba(0,0,0,.28)",
        }}
      >
        <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
          {MODES.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => switchMode(item.key)}
              style={{
                ...linkStyle,
                fontWeight: mode === item.key ? 700 : 500,
                opacity: mode === item.key ? 1 : 0.6,
              }}
            >
              {item.label}
            </button>
          ))}
          <span style={{ flex: 1 }} />
          {onClose ? (
            <button type="button" onClick={onClose} aria-label="关闭" style={linkStyle}>
              关闭
            </button>
          ) : null}
        </div>

        {mode === "forgot" && resetToken ? (
          <form onSubmit={submitReset} style={{ display: "grid", gap: 12 }}>
            <div>
              <label style={labelStyle} htmlFor="auth-reset-token">
                重置码
              </label>
              <input
                id="auth-reset-token"
                style={inputStyle}
                value={resetToken}
                onChange={(event) => setResetToken(event.target.value)}
                autoComplete="one-time-code"
              />
            </div>
            <div>
              <label style={labelStyle} htmlFor="auth-reset-password">
                新密码（至少 8 位）
              </label>
              <input
                id="auth-reset-password"
                style={inputStyle}
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="new-password"
              />
            </div>
            {errors.form ? (
              <div role="alert" style={{ color: "#d33", fontSize: 13 }}>
                {errors.form}
              </div>
            ) : null}
            {notice ? (
              <div role="status" style={{ fontSize: 12, opacity: 0.8, wordBreak: "break-all" }}>
                {notice}
              </div>
            ) : null}
            <button type="submit" style={primaryStyle} disabled={busy}>
              {busy ? "处理中…" : "重置密码"}
            </button>
          </form>
        ) : (
          <form onSubmit={submit} style={{ display: "grid", gap: 12 }}>
            <div>
              <label style={labelStyle} htmlFor="auth-email">
                邮箱
              </label>
              <input
                id="auth-email"
                style={inputStyle}
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                autoComplete="username"
                placeholder="you@example.com"
              />
              {errors.email ? <div style={{ color: "#d33", fontSize: 12 }}>{errors.email}</div> : null}
            </div>

            {mode !== "forgot" ? (
              <div>
                <label style={labelStyle} htmlFor="auth-password">
                  密码{mode === "register" ? "（至少 8 位）" : ""}
                </label>
                <input
                  id="auth-password"
                  style={inputStyle}
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                />
                {errors.password ? (
                  <div style={{ color: "#d33", fontSize: 12 }}>{errors.password}</div>
                ) : null}
              </div>
            ) : null}

            {mode === "register" ? (
              <>
                <div>
                  <label style={labelStyle} htmlFor="auth-confirm">
                    确认密码
                  </label>
                  <input
                    id="auth-confirm"
                    style={inputStyle}
                    type="password"
                    value={confirm}
                    onChange={(event) => setConfirm(event.target.value)}
                    autoComplete="new-password"
                  />
                  {errors.confirm ? (
                    <div style={{ color: "#d33", fontSize: 12 }}>{errors.confirm}</div>
                  ) : null}
                </div>
                <div>
                  <label style={labelStyle} htmlFor="auth-invite">
                    邀请码（内测）
                  </label>
                  <input
                    id="auth-invite"
                    style={inputStyle}
                    value={inviteCode}
                    onChange={(event) => setInviteCode(event.target.value)}
                    autoComplete="off"
                    placeholder="向运营领取"
                  />
                  {errors.inviteCode ? (
                    <div style={{ color: "#d33", fontSize: 12 }}>{errors.inviteCode}</div>
                  ) : null}
                </div>
              </>
            ) : null}

            {errors.form ? (
              <div role="alert" style={{ color: "#d33", fontSize: 13 }}>
                {errors.form}
              </div>
            ) : null}
            {notice ? (
              <div role="status" style={{ fontSize: 12, opacity: 0.8, wordBreak: "break-all" }}>
                {notice}
              </div>
            ) : null}

            <button type="submit" style={primaryStyle} disabled={busy}>
              {busy
                ? "处理中…"
                : mode === "login"
                  ? "登录"
                  : mode === "register"
                    ? "注册"
                    : "发送重置邮件"}
            </button>
            <p style={{ margin: 0, fontSize: 12, opacity: 0.7, lineHeight: 1.5 }}>
              账号仅用于多设备同步与数据隔离；你的动态、私聊情绪和人物关系互相不可见。
            </p>
          </form>
        )}
      </div>
    </div>
  );
}
