/**
 * V3 账号会话（邮箱 + 密码）客户端接入。
 *
 * 与设备身份（`X-User-Id` + `X-User-Token`）并存：**登录成功后**所有 `/api/**`
 * 请求改用 `X-Session-Token`（写请求另带 `X-CSRF-Token`），Web 同时由服务端下发
 * HttpOnly Cookie；Android 直接依赖请求头（无 Cookie）。
 *
 * 登录/注册响应里的 `session_token` 只返回一次，这里存进 localStorage；
 * 退出时调用服务端撤销并清空本地，不会回退成设备身份（避免“退出后又自动登录”）。
 */

import { API_ORIGIN } from "../api";

const SESSION_STORE = "sonder.session.v1";

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
  session_token: string;
  csrf_token?: string;
  user_id: string;
  email?: string;
  email_verified?: boolean;
  expires_at?: string;
}): SessionAuth {
  const session: SessionAuth = {
    token: data.session_token,
    csrf: data.csrf_token ?? "",
    user_id: data.user_id,
    email: data.email,
    email_verified: Boolean(data.email_verified),
    expires_at: data.expires_at,
  };
  const store = storage();
  try {
    store?.setItem(SESSION_STORE, JSON.stringify(session));
  } catch {
    /* quota/隐私模式：内存态仍可用 */
  }
  return session;
}

export function clearSession(): void {
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
  const headers: Record<string, string> = { "X-Session-Token": session.token };
  if (MUTATING.has(method.toUpperCase()) && session.csrf) {
    headers["X-CSRF-Token"] = session.csrf;
  }
  return headers;
}

async function call<T>(path: string, body?: unknown, method = "POST"): Promise<T> {
  const res = await fetch(`${API_ORIGIN}${path}`, {
    method,
    cache: "no-store",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...sessionHeaders(method),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (data?.detail) {
        detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      }
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(detail);
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
  }
}

export interface AuthIdentity {
  user_id: string;
  account: boolean;
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
