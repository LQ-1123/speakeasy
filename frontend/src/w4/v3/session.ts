import { API_ORIGIN } from "../api";
import { emailCodeAuthEnabled, configureEmailCodeAuth } from "./authMode";

const SESSION_STORE = "sonder.session.v1";
const SIGNED_OUT_STORE = "se.account.signed-out";
const ACCOUNT_CHANGED_STORE = "se.account.changed";
let accountSession: SessionAuth | null = null;
let accountGeneration = 0;
export const AUTH_EXPIRED_EVENT = "sonder:auth-expired";
export const AUTH_LOGOUT_EVENT = "sonder:auth-logout";

/** Cookie identity is shared by tabs; every tab must discard its previous user's state. */
export function subscribeAccountChanges(onLogout: () => void, onLogin: () => void): () => void {
  const listener = (event: StorageEvent) => {
    if (event.key === SIGNED_OUT_STORE && event.newValue === "1") onLogout();
    if (event.key === ACCOUNT_CHANGED_STORE && event.newValue) onLogin();
  };
  window.addEventListener("storage", listener);
  return () => window.removeEventListener("storage", listener);
}

function bridge() { return typeof window === "undefined" ? undefined : window.SpeakEasyNative; }
function nativePlatform() { return Boolean(bridge()); }
function removeLegacySession() {
  try {
    storage()?.removeItem(SESSION_STORE);
    storage()?.removeItem("speakeasy.identity.v1");
  } catch { /* private storage */ }
}
export function expireAccountSession() {
  clearSession();
  markAccountSignedOut();
  if (typeof window !== "undefined") window.dispatchEvent?.(new Event(AUTH_EXPIRED_EVENT));
}
function markAccountSignedOut() {
  if (emailCodeAuthEnabled()) { try { storage()?.setItem(SIGNED_OUT_STORE, "1"); } catch { /* private storage */ } }
}
export class AuthError extends Error {
  constructor(message: string, public code = "", public status = 0, public retryAfter = 0) { super(message); }
}


export interface SessionAuth {
  token: string;
  csrf: string;
  user_id: string;
  email?: string;
  email_verified?: boolean;
  expires_at?: string;
}

export interface AuthResult extends SessionAuth {
  display_name?: string;
  created?: boolean;
  /** 开发模式（SE_MAIL_DEV=1）才返回：邮箱验证/找回密码令牌 */
  dev_verify_token?: string;
  dev_reset_token?: string;
}

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function readSession(): SessionAuth | null {
  if (emailCodeAuthEnabled()) return accountSession;
  const store = storage();
  if (!store) return null;
  try {
    const raw = store.getItem(SESSION_STORE);
    if (!raw) return null;
    const data = JSON.parse(raw) as Partial<SessionAuth>;
    if (!data || typeof data.token !== "string" || !data.token) return null;
    return {
      token: data.token,
      csrf: typeof data.csrf === "string" ? data.csrf : "",
      user_id: typeof data.user_id === "string" ? data.user_id : "",
      email: typeof data.email === "string" ? data.email : undefined,
      email_verified: Boolean(data.email_verified),
      expires_at: typeof data.expires_at === "string" ? data.expires_at : undefined,
    };
  } catch {
    return null;
  }
}

export function saveSession(data: {
  session_token?: string;
  csrf_token?: string;
  user_id: string;
  email?: string;
  email_verified?: boolean;
  expires_at?: string;
}): SessionAuth {
  const session: SessionAuth = {
    token: data.session_token ?? "",
    csrf: data.csrf_token ?? "",
    user_id: data.user_id,
    email: data.email,
    email_verified: Boolean(data.email_verified),
    expires_at: data.expires_at,
  };
  if (emailCodeAuthEnabled()) {
    removeLegacySession();
    if (nativePlatform()) {
      const native = bridge();
      if (!session.token || !native?.saveSession || !native.readSession || !native.clearSession || native.saveSession(JSON.stringify({ sessionToken: session.token, userId: session.user_id, expiresAt: session.expires_at, email: session.email, csrfToken: session.csrf })) !== true) {
        throw new Error("无法安全保存登录状态，请更新应用后重试");
      }
    }
    accountGeneration += 1;
    accountSession = session;
    try {
      storage()?.removeItem(SIGNED_OUT_STORE);
      storage()?.setItem(ACCOUNT_CHANGED_STORE, crypto.randomUUID());
    } catch { /* private storage or unavailable crypto */ }
    return session;
  }
  const store = storage();
  try {
    store?.setItem(SESSION_STORE, JSON.stringify(session));
  } catch {
    /* quota/隐私模式：内存态仍可用 */
  }
  return session;
}

export function clearSession(): void {
  accountGeneration += 1;
  accountSession = null;
  if (emailCodeAuthEnabled()) { try { bridge()?.clearSession?.(); } catch { /* native unavailable */ } }
  const store = storage();
  try {
    store?.removeItem(SESSION_STORE);
  } catch {
    /* ignore */
  }
}

export function hasSession(): boolean {
  return readSession() !== null;
}

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** 会话请求头（`X-CSRF-Token` 只在写请求带）。 */
export function sessionHeaders(method = "GET"): Record<string, string> {
  const session = readSession();
  if (!session) return {};
  const headers: Record<string, string> = session.token ? { "X-Session-Token": session.token } : {};
  if (MUTATING.has(method.toUpperCase()) && session.csrf) {
    headers["X-CSRF-Token"] = session.csrf;
  }
  return headers;
}

async function call<T>(path: string, body?: unknown, method = "POST"): Promise<T> {
  const sentSession = readSession();
  const res = await fetch(`${API_ORIGIN}${path}`, {
    method,
    cache: "no-store",
    credentials: "include",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...sessionHeaders(method),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    let code = "";
    try {
      const data = await res.json();
      const detail = data?.detail;
      if (typeof detail === "string") message = detail;
      else if (detail) { message = detail.message || message; code = detail.code || ""; }
    } catch { /* non JSON */ }
    if (res.status === 401 && emailCodeAuthEnabled() && sentSession && sentSession === readSession() && !path.includes("/email-code/")) expireAccountSession();
    throw new AuthError(message, code, res.status, Number(res.headers.get("Retry-After")) || 0);
  }
  return (await res.json()) as T;
}

function toAuthResult(data: Record<string, unknown>): AuthResult {
  const saved = saveSession({
    session_token: String(data.session_token ?? ""),
    csrf_token: typeof data.csrf_token === "string" ? data.csrf_token : "",
    user_id: String(data.user_id ?? ""),
    email: typeof data.email === "string" ? data.email : undefined,
    email_verified: Boolean(data.email_verified),
    expires_at: typeof data.expires_at === "string" ? data.expires_at : undefined,
  });
  return {
    ...saved,
    display_name: typeof data.display_name === "string" ? data.display_name : undefined,
    created: Boolean(data.created),
    dev_verify_token:
      typeof data.dev_verify_token === "string" ? data.dev_verify_token : undefined,
    dev_reset_token: typeof data.dev_reset_token === "string" ? data.dev_reset_token : undefined,
  };
}

/** 邮箱登录（成功后持久化会话）。 */
export async function loginEmail(
  email: string,
  password: string,
  options: { device_key?: string; platform?: string } = {},
): Promise<AuthResult> {
  const data = await call<Record<string, unknown>>("/api/v3/auth/login", {
    email,
    password,
    device_key: options.device_key ?? "",
    platform: options.platform ?? "",
  });
  return toAuthResult(data);
}

/** 邮箱注册（邀请内测：`invite_code` 必填时服务端会 403）。 */
export async function registerEmail(input: {
  email: string;
  password: string;
  invite_code?: string;
  display_name?: string;
  device_key?: string;
}): Promise<AuthResult> {
  const data = await call<Record<string, unknown>>("/api/v3/auth/register/email", {
    email: input.email,
    password: input.password,
    invite_code: input.invite_code ?? "",
    display_name: input.display_name ?? "",
    device_key: input.device_key ?? "",
  });
  return toAuthResult(data);
}

/** 退出当前会话（服务端撤销 + 本地清空）。 */
export async function logoutSession(): Promise<void> {
  try {
    await call("/api/v3/auth/logout", {});
  } catch {
    /* 网络失败也要允许本地退出 */
  } finally {
    clearSession();
    markAccountSignedOut();
    if (typeof window !== "undefined") window.dispatchEvent?.(new Event(AUTH_LOGOUT_EVENT));
  }
}

/** 退出全部设备会话并清空本地。 */
export async function logoutAllSessions(): Promise<void> {
  try {
    await call("/api/v3/auth/logout-all", {});
  } catch {
    /* ignore */
  } finally {
    clearSession();
    markAccountSignedOut();
    if (typeof window !== "undefined") window.dispatchEvent?.(new Event(AUTH_LOGOUT_EVENT));
  }
}

export interface AuthIdentity {
  user_id: string;
  account?: boolean;
  has_account?: boolean;
  csrf_token?: string;
  expires_at?: string;
  resolved_by?: string;
  email?: string;
  email_verified?: boolean;
  age?: { group: string; verified: boolean; guardian_bound?: boolean };
  features?: Record<string, boolean>;
}

/** 当前身份（账号能力矩阵）。 */
export function authIdentity(): Promise<AuthIdentity> {
  return call<AuthIdentity>("/api/v3/auth/identity", undefined, "GET");
}

/** 设备会话列表（可撤销设备会话）。 */
export interface AuthSessionRow {
  session_id: string;
  device_key?: string;
  platform?: string;
  created_at: string;
  last_seen_at?: string;
  expires_at?: string;
  current?: boolean;
}

export function listSessions(): Promise<{ items: AuthSessionRow[] }> {
  return call<{ items: AuthSessionRow[] }>("/api/v3/auth/sessions", undefined, "GET");
}

export function revokeSession(sessionId: string): Promise<{ ok: boolean }> {
  return call(`/api/v3/auth/sessions/${encodeURIComponent(sessionId)}`, undefined, "DELETE");
}

export function verifyEmailToken(token: string): Promise<{ ok: boolean; user_id: string }> {
  return call("/api/v3/auth/verify-email", { token });
}

export function resendVerification(): Promise<{ ok: boolean }> {
  return call("/api/v3/auth/resend-verification", {});
}

export function requestPasswordReset(email: string): Promise<{ ok: boolean; dev_reset_token?: string }> {
  return call("/api/v3/auth/password/forgot", { email });
}

export function resetPassword(token: string, password: string): Promise<{ ok: boolean }> {
  return call("/api/v3/auth/password/reset", { token, password });
}

export function changePassword(oldPassword: string, newPassword: string): Promise<{ ok: boolean }> {
  return call("/api/v3/auth/password/change", {
    old_password: oldPassword,
    new_password: newPassword,
  });
}


export async function loadAuthConfig(): Promise<{ email_code_auth: boolean; invite_required: boolean }> {
  const config = await call<{ email_code_auth: boolean; invite_required: boolean }>("/api/v3/auth/config", undefined, "GET");
  configureEmailCodeAuth(config.email_code_auth);
  return config;
}

/** No legacy identity lookup: failed signed-upgrade migration cannot block account login. */
export async function restoreAccountSession(): Promise<SessionAuth | null> {
  removeLegacySession();
  try { if (storage()?.getItem(SIGNED_OUT_STORE) === "1") return null; } catch { /* private storage */ }
  const generation = accountGeneration;
  if (nativePlatform()) {
    const native = bridge();
    if (!native?.readSession || !native.saveSession || !native.clearSession) throw new Error("请更新应用以使用邮箱登录");
    let stored: { sessionToken?: string; userId?: string; expiresAt?: string; email?: string; csrfToken?: string } | null = null;
    try { stored = JSON.parse(native.readSession() || "null"); } catch { native.clearSession(); }
    if (!stored?.sessionToken || !stored.userId) return null;
    accountSession = { token: stored.sessionToken, user_id: stored.userId, csrf: stored.csrfToken ?? "", email: stored.email, expires_at: stored.expiresAt };
  }
  try {
    const who = await authIdentity();
    if (generation !== accountGeneration) return null;
    if (!(who.has_account ?? who.account) || !who.email_verified) { clearSession(); return null; }
    accountSession = { token: accountSession?.token ?? "", user_id: who.user_id, csrf: who.csrf_token || accountSession?.csrf || "", email: who.email, email_verified: true, expires_at: who.expires_at };
    return accountSession;
  } catch (error) {
    if (error instanceof AuthError && error.status === 401) return null;
    throw error;
  }
}

export interface EmailCodeChallenge { challenge_id: string; retry_after: number; expires_in: number }
export function requestEmailCode(email: string): Promise<EmailCodeChallenge> {
  return call("/api/v3/auth/email-code/request", { email: email.trim().toLowerCase() });
}
export async function verifyEmailCode(input: { challenge_id: string; code: string; invite_code?: string }): Promise<AuthResult> {
  const native = bridge();
  const data = await call<Record<string, unknown>>("/api/v3/auth/email-code/verify", {
    ...input, platform: nativePlatform() ? "android" : "web", device_key: native?.getInstallationId?.() ?? "",
  });
  return toAuthResult(data);
}
