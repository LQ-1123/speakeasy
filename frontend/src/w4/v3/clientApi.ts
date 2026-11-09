import { API_ORIGIN } from "../api";
import type { Identity } from "./api";

export interface ClientBootstrap {
  profile: Record<string, unknown>;
  friends: Record<string, unknown>[];
  requests: Record<string, unknown>[];
  conversations: Array<{ friend_id: string; last_message: Record<string, unknown> | null; unread: number }>;
  expressions: Record<string, unknown>[];
  capabilities: Record<string, unknown>;
  unread_events: number;
}

export interface CallTicket {
  call_id: string;
  ticket: string;
  ws_path: string;
  expires_in: number;
}

export interface ClientPreferences {
  messages: boolean;
  interactions: boolean;
  sound: boolean;
  theme: "system" | "light" | "dark";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(`${API_ORIGIN}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
    signal: init?.signal ?? AbortSignal.timeout(15_000),
  }); } catch { throw new Error("连接未完成，请检查网络后重试。"); }
  if (!response.ok) {
    const message = response.status === 401 ? "账户验证已过期，请重试。" : response.status === 403 ? "当前账户无法执行此操作。" : response.status === 429 ? "操作较频繁，请稍后再试。" : "请求未完成，请稍后重试。";
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const clientApi = {
  bootstrap: () => request<ClientBootstrap>("/api/v3/client/bootstrap"),
  messages: (friendId: string, cursor?: string) => {
    const q = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    return request<{ messages: Record<string, unknown>[]; next_cursor?: string }>(
      `/api/v2/friends/${encodeURIComponent(friendId)}/messages${q}`,
    );
  },
  createCall: (friendId: string, requestId: string) =>
    request<CallTicket>("/api/v3/calls", {
      method: "POST",
      body: JSON.stringify({ friend_id: friendId, request_id: requestId }),
    }),
  refreshCallTicket: (callId: string, signal?: AbortSignal) =>
    request<CallTicket>(`/api/v3/calls/${encodeURIComponent(callId)}/ticket`, { method: "POST", signal }),
  endCall: (callId: string) =>
    request<void>(`/api/v3/calls/${encodeURIComponent(callId)}`, { method: "DELETE" }),
  eventTicket: () => request<CallTicket & { current_sequence: number }>("/api/v3/client/event-ticket", { method: "POST" }),
  markEventRead: async (id: string) => {
    await request<void>(`/api/v3/client/events/${encodeURIComponent(id)}/read`, { method: "PATCH" });
    nativeBridge()?.cancelNotification?.(id);
  },
  preferences: () => request<ClientPreferences>("/api/v3/client/preferences"),
  savePreferences: (value: Partial<ClientPreferences>) =>
    request<ClientPreferences>("/api/v3/client/preferences", { method: "PATCH", body: JSON.stringify(value) }),
  addExpression: (fields: { original_text: string; better_expression: string; explanation: string }) =>
    request<{ expression: Record<string, unknown> }>("/api/v2/expressions", {
      method: "POST",
      body: JSON.stringify(fields),
    }),
  registerDevice: (registration_id: string, vendor?: string) =>
    request<void>("/api/v3/client/devices", {
      method: "POST",
      body: JSON.stringify({ registration_id, platform: "android", vendor, enabled: true }),
    }),
};

export interface NativeCallPayload {
  apiOrigin: string;
  callId: string;
  friendId?: string;
  ticket: string;
  wsPath: string;
  friendName: string;
  userId: string;
  token: string;
}

export interface SpeakEasyNative {
  getCapabilities?: () => string;
  /** New bridge: complete light/dark ThemeSnapshot JSON. */
  getThemeSnapshot?: () => string;
  /** Legacy compatibility bridge (accent/onAccent only). */
  getTheme?: () => string;
  performHaptic?: (type: string) => void;
  getIdentity?: () => string;
  saveIdentity?: (json: string) => void;
  startCall?: (json: string) => void;
  callAction?: (json: string) => void;
  getCallState?: () => string;
  requestPush?: () => void;
  setPreferences?: (json: string) => void;
  playSound?: (kind: string) => void;
  cancelNotification?: (eventId: string) => void;
}

declare global {
  interface Window {
    SpeakEasyNative?: SpeakEasyNative;
  }
}

export function nativeBridge(): SpeakEasyNative | undefined {
  if (typeof window === "undefined") return undefined;
  return window.SpeakEasyNative;
}

export async function nativeClientIdentity(): Promise<Identity | null> {
  const native = nativeBridge();
  if (native?.getIdentity) {
    let capabilities: { identityMigration?: string; identityError?: string } = {};
    try { capabilities = JSON.parse(native.getCapabilities?.() || "{}"); } catch { /* old bridge */ }
    if (capabilities.identityMigration === "pending") {
      await new Promise<void>((resolve, reject) => {
        const timeout = window.setTimeout(() => {
          window.removeEventListener("speakeasy:native", listener);
          reject(new Error("Identity migration is taking longer than expected. Please reopen the app."));
        }, 20_000);
        const listener = (raw: Event) => {
          const detail = (raw as CustomEvent<{ type?: string; status?: string; error?: string }>).detail;
          if (detail.type !== "identity" || detail.status === "pending") return;
          window.clearTimeout(timeout);
          window.removeEventListener("speakeasy:native", listener);
          if (detail.status === "failed") reject(new Error(detail.error || "Your existing identity could not be restored. Please retry."));
          else resolve();
        };
        window.addEventListener("speakeasy:native", listener);
      });
    } else if (capabilities.identityMigration === "failed") {
      throw new Error(capabilities.identityError || "Your existing identity could not be restored. Please retry.");
    }
    try {
      const value = native.getIdentity();
      if (value && value !== "null") {
        const identity = JSON.parse(value) as Identity;
        if (identity?.user_id && identity.token) return identity;
      }
    } catch { /* use the web identity */ }
  }
  return null;
}

export async function currentClientIdentity(): Promise<Identity | null> {
  const native = await nativeClientIdentity();
  if (native) return native;
  const { ensureIdentity } = await import("./api");
  return ensureIdentity();
}

export function websocketUrl(path: string): string {
  const url = new URL(path, API_ORIGIN || window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}
