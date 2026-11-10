import { nativeBridge, type NativeNotificationStatus } from "./v3/clientApi";
import { usePreferences } from "./v3/preferences";
import { callSession } from "./v3/callSession";

export function unlockNotifyAudio(): void { /* User gestures unlock regular media playback. */ }
function play(kind: "message" | "request"): void {
  if (!usePreferences.getState().value.sound || callSession.getSnapshot()) return;
  const native = nativeBridge();
  if (native?.playSound) { native.playSound(kind); return; }
  const audio = new Audio(`/sounds/${kind}.wav`);
  audio.volume = 0.5;
  void audio.play().catch(() => undefined);
}
export function playMessageSound(): void { play("message"); }
export function playRequestSound(): void { play("request"); }

/* ------------------------------------------------------- system notifications */

export interface SystemNotificationPayload {
  /** Backend client-event id: used for dedupe/cancel with the push notification. */
  id: string;
  title: string;
  /** Must stay generic — message text never goes to the lock screen. */
  body: string;
  route: string;
}

/** Observed native notification/push state, or null outside the Android shell. */
export function systemNotificationStatus(): NativeNotificationStatus | null {
  const native = nativeBridge();
  if (!native?.getNotificationStatus) return null;
  try {
    return JSON.parse(native.getNotificationStatus()) as NativeNotificationStatus;
  } catch {
    return null;
  }
}

/** Requests POST_NOTIFICATIONS (Android 13+); false when the OS cannot prompt. */
export function requestSystemNotificationPermission(): boolean {
  const native = nativeBridge();
  if (!native?.requestNotificationPermission) return false;
  try {
    return native.requestNotificationPermission() === true;
  } catch {
    return false;
  }
}

/** Posts a generic local notification; false when unsupported/denied/deduped. */
export function postSystemNotification(payload: SystemNotificationPayload): boolean {
  const native = nativeBridge();
  if (!native?.postNotification) return false;
  try {
    if (!payload.id || !payload.route) return false;
    return native.postNotification(JSON.stringify(payload)) === true;
  } catch {
    return false;
  }
}

/** Cancels both the local notification and the JPush notification for an event. */
export function cancelSystemNotification(id: string): void {
  if (!id) return;
  try {
    nativeBridge()?.cancelNotification?.(id);
  } catch {
    /* Older bridge. */
  }
}

export function openSystemNotificationSettings(): void {
  try {
    nativeBridge()?.openNotificationSettings?.();
  } catch {
    /* Older bridge. */
  }
}

/** Dismissal is shared between the opt-in card and the settings pane. */
export const PUSH_PROMPT_KEY = "speakeasy.push.prompted.v1";
export function markPushPrompted(): void {
  try {
    window.localStorage.setItem(PUSH_PROMPT_KEY, "1");
  } catch {
    /* privacy mode */
  }
  if (typeof window !== "undefined") window.dispatchEvent(new Event("speakeasy:push-prompted"));
}

export function onPushPrompted(listener: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  window.addEventListener("speakeasy:push-prompted", listener);
  return () => window.removeEventListener("speakeasy:push-prompted", listener);
}

export interface NativeNotificationAlert {
  type: "push" | "notification";
  status: string;
  message?: string;
  registrationId?: string;
}

/**
 * Human-readable explanation for push/notification events that the user must be
 * told about instead of being left with a button that appears to do nothing.
 */
export function alertTextFor(detail: NativeNotificationAlert): string | null {
  const status = detail.status;
  if (status === "unavailable") return detail.message || "当前安装包未配置推送服务，离开应用后可能收不到提醒。";
  if (status === "denied") return detail.message || "通知权限未开启，请在系统设置中允许 Sonder 发送通知。";
  if (status === "error") return detail.message || "推送服务暂时不可用，请稍后重试。";
  if (status === "timeout") return detail.message || "推送注册超时，请检查网络后重试。";
  if (detail.type === "notification" && status === "denied") return "通知权限未开启，可在系统设置中允许。";
  if (detail.type === "notification" && status === "granted") return "系统通知已开启。";
  return null;
}
