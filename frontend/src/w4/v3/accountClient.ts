/** V3 账号相关客户端调用（会话请求头由 installApiIdentity 统一注入）。 */

import { API_ORIGIN } from "../api";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_ORIGIN}${path}`, { cache: "no-store", ...init });
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

function post<T>(path: string, body?: unknown): Promise<T> {
  return call<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

export interface AdStatus {
  eligible: boolean;
  reason: string;
  ads_opt_in: boolean;
  age_gate: string;
  label: string;
}

export function adStatus(): Promise<AdStatus> {
  return call<AdStatus>("/api/v3/ads/status");
}

export function setAdsOptIn(enabled: boolean): Promise<{ ok: boolean; ads_opt_in: boolean }> {
  return post("/api/v3/ads/opt-in", { enabled });
}

export function adEvent(
  deliveryId: string,
  type: "impression" | "click" | "close" | "report",
  payload: Record<string, unknown> = {},
): Promise<{ ok: boolean; status: string }> {
  return post("/api/v3/ads/events", { delivery_id: deliveryId, type, payload });
}

export interface TeenAccess {
  allowed: boolean;
  reason: string;
  reminder?: boolean;
  retry_after_s?: number;
}

export interface TeenStatus {
  role?: string;
  access: TeenAccess;
  usage?: Record<string, number | string>;
  settings?: Record<string, unknown>;
}

export function teenStatus(): Promise<TeenStatus> {
  return call<TeenStatus>("/api/v3/teen/status");
}

/** 心跳：顺带累计使用时长并返回最新门禁结论（青少年模式）。 */
export function teenHeartbeat(): Promise<TeenStatus> {
  return post<TeenStatus>("/api/v3/teen/heartbeat");
}

export type ReportTarget = "persona" | "post" | "comment" | "message" | "ad" | "user";

export function reportContent(
  targetType: ReportTarget,
  targetId: string,
  category: string,
  detail = "",
): Promise<{ ok: boolean; report: Record<string, unknown> }> {
  return post("/api/v3/reports", {
    target_type: targetType,
    target_id: targetId,
    category,
    detail,
  });
}
