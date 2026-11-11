/**
 * 广告曝光/点击/关闭上报的纯逻辑（便于单测）。
 *
 * 规则（与方案一致）：
 * - **可见曝光**：内容在视口内连续可见 ≥1 秒才上报 `impression`（避免“预加载也算曝光”）；
 * - 同一次投放（delivery_id）只上报一次 impression（sessionStorage 去重）；
 * - `close` / `report` 之后本页不再展示该投放，服务端会再屏蔽该活动 30 天。
 */

export type AdEventType = "impression" | "click" | "close" | "report";

export const IMPRESSION_VISIBLE_MS = 1000;
const REPORTED_KEY_PREFIX = "sonder.ad.impression.";

export function isAdEventType(value: string): value is AdEventType {
  return value === "impression" || value === "click" || value === "close" || value === "report";
}

/** 可见时长是否达到上报阈值。 */
export function shouldReportImpression(visibleMs: number, threshold = IMPRESSION_VISIBLE_MS): boolean {
  return Number.isFinite(visibleMs) && visibleMs >= threshold;
}

export function adEventBody(type: AdEventType, visibleMs?: number): Record<string, unknown> {
  if (type === "impression") {
    return { type, payload: { visible_ms: Math.max(0, Math.round(visibleMs ?? 0)) } };
  }
  return { type, payload: {} };
}

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

export function impressionAlreadyReported(deliveryId: string): boolean {
  if (!deliveryId) return true;
  try {
    return storage()?.getItem(REPORTED_KEY_PREFIX + deliveryId) === "1";
  } catch {
    return false;
  }
}

export function markImpressionReported(deliveryId: string): void {
  if (!deliveryId) return;
  try {
    storage()?.setItem(REPORTED_KEY_PREFIX + deliveryId, "1");
  } catch {
    /* 隐私模式忽略 */
  }
}

/** 关闭/举报后本页隐藏的投放集合（刷新后仍由服务端屏蔽，不依赖本地）。 */
const hidden = new Set<string>();

export function hideDelivery(deliveryId: string): void {
  if (deliveryId) hidden.add(deliveryId);
}

export function isHidden(deliveryId: string): boolean {
  return hidden.has(deliveryId);
}

export function resetHidden(): void {
  hidden.clear();
}

/** 只允许 http(s) 落地页，避免 javascript: 之类的注入。 */
export function safeLandingUrl(url: string | undefined | null): string {
  const raw = (url ?? "").trim();
  if (!raw) return "";
  if (!/^https?:\/\//i.test(raw)) return "";
  return raw;
}
