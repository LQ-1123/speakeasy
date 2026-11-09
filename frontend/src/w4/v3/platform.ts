import { callSession } from "./callSession";
import { clientApi, nativeBridge } from "./clientApi";

declare global {
  interface Window {
    speakeasyBack?: () => boolean;
  }
}

/**
 * Platform adapter: insets, hardware back, call events and push registration.
 * Theme colours live in src/theme/theme-store.tsx and are deliberately not
 * recalculated here — an IME inset change must never resample a palette.
 */
export function installPlatform(back: () => boolean): () => void {
  const root = document.documentElement;
  const removeCallLayout = callSession.subscribe((call) => {
    root.dataset.callMinimized = String(Boolean(call?.minimized));
  });

  const applyInsets = () => {
    const native = nativeBridge();
    if (!native) return;
    let theme: { insets?: { top?: number; bottom?: number; ime?: number } } = {};
    try {
      theme = JSON.parse(native.getTheme?.() || "{}");
    } catch {
      theme = {};
    }
    const top = Math.max(0, Number(theme.insets?.top) || 0);
    const bottom = Math.max(0, Number(theme.insets?.bottom) || 0);
    const ime = Math.max(0, Number(theme.insets?.ime) || 0);
    root.style.setProperty("--safe-top", `${top}px`);
    root.style.setProperty("--safe-bottom", `${bottom}px`);
    root.style.setProperty("--ime-height", `${ime}px`);
    root.style.setProperty("--se-safe-top", `${top}px`);
    root.style.setProperty("--se-safe-bottom", `${bottom}px`);
    root.style.setProperty("--se-ime", `${ime}px`);
  };

  const nativeEvent = (raw: Event) => {
    const event = raw as CustomEvent<{
      type?: string;
      registration_id?: string;
      vendor?: string;
      route?: string;
      insets?: { top?: number; bottom?: number; ime?: number };
    }>;
    if (event.detail.type === "theme") applyInsets();
    if (event.detail.type === "call") callSession.handleNativeEvent(event);
    if (event.detail.type === "push" && event.detail.registration_id) {
      void clientApi.registerDevice(event.detail.registration_id, event.detail.vendor).catch(() => undefined);
    }
    if (
      event.detail.type === "notification_open" &&
      /^#\/(chat|chats|discover|events|contacts|me|expressions)(\/|$)/.test(event.detail.route || "")
    ) {
      window.location.hash = event.detail.route!;
    }
  };

  const handleBack = () => {
    const call = callSession.getSnapshot();
    if (call && !call.minimized) {
      callSession.minimize();
      return true;
    }
    return back() === true;
  };
  window.speakeasyBack = handleBack;

  try {
    const state = nativeBridge()?.getCallState?.();
    if (state) {
      callSession.handleNativeEvent(
        new CustomEvent("speakeasy:native", { detail: { type: "call", state: JSON.parse(state) } }),
      );
    }
  } catch {
    /* Optional older bridge state. */
  }

  applyInsets();
  window.addEventListener("speakeasy:native", nativeEvent);
  return () => {
    removeCallLayout();
    window.removeEventListener("speakeasy:native", nativeEvent);
    if (window.speakeasyBack === handleBack) delete window.speakeasyBack;
  };
}
