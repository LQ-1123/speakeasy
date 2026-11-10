import { callSession } from "./callSession";
import { clientApi, nativeBridge } from "./clientApi";
import { isInternalNotificationRoute } from "../navigation";

declare global {
  interface Window {
    speakeasyBack?: () => boolean;
  }
}

export interface VisualViewportSnapshot {
  /** 视觉视口高度（CSS px）。键盘弹起时由浏览器缩小。 */
  height: number;
  /** 视觉视口相对布局视口的纵向偏移。 */
  offsetTop: number;
}

export interface ImeHeightInput {
  /** window.innerHeight：布局视口高度（Android adjustNothing 下键盘不改变它）。 */
  innerHeight: number;
  visualViewport?: VisualViewportSnapshot | null;
}

/**
 * 由 visualViewport 计算输入法遮挡高度（CSS px）——纯函数，便于单测。
 *
 * Android WebView 用 `windowSoftInputMode=adjustNothing` 时布局视口不缩放，
 * 键盘只会缩小视觉视口：遮挡高度 = innerHeight - visualViewport.height - offsetTop。
 * 视觉视口比布局视口还高（部分浏览器工具栏折叠）、字段缺失或非法时返回 0。
 */
export function computeImeHeight({ innerHeight, visualViewport }: ImeHeightInput): number {
  if (!Number.isFinite(innerHeight) || innerHeight <= 0) return 0;
  if (!visualViewport) return 0;
  const height = Number(visualViewport.height);
  if (!Number.isFinite(height) || height <= 0) return 0;
  const offsetTop = Number.isFinite(visualViewport.offsetTop) ? Math.max(0, visualViewport.offsetTop) : 0;
  const covered = innerHeight - height - offsetTop;
  return covered > 0 ? Math.round(covered) : 0;
}

/**
 * 监听 visualViewport：键盘弹起/收起、页面被键盘顶起、旋转、窗口尺寸变化时
 * 把 IME 高度写入 CSS 变量（--ime-height / --se-ime，单位 px，关闭时为 0）。
 *
 * 只有环境没有 visualViewport 时才回退到 native insets（见 installPlatform）。
 */
export function installImeTracking(): () => void {
  if (typeof window === "undefined" || typeof document === "undefined") return () => undefined;
  const viewport = window.visualViewport;
  if (!viewport) return () => undefined;
  const root = document.documentElement;
  let frame = 0;

  const apply = () => {
    frame = 0;
    const ime = computeImeHeight({
      innerHeight: window.innerHeight,
      visualViewport: { height: viewport.height, offsetTop: viewport.offsetTop },
    });
    root.style.setProperty("--ime-height", `${ime}px`);
    root.style.setProperty("--se-ime", `${ime}px`);
    if (ime > 0) root.dataset.imeOpen = "1";
    else delete root.dataset.imeOpen;
  };

  const schedule = () => {
    if (frame) return;
    frame = window.requestAnimationFrame(apply);
  };

  viewport.addEventListener("resize", schedule);
  viewport.addEventListener("scroll", schedule);
  window.addEventListener("resize", schedule);
  window.addEventListener("orientationchange", schedule);
  apply();

  return () => {
    if (frame) window.cancelAnimationFrame(frame);
    viewport.removeEventListener("resize", schedule);
    viewport.removeEventListener("scroll", schedule);
    window.removeEventListener("resize", schedule);
    window.removeEventListener("orientationchange", schedule);
  };
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
  /* Web 层 IME：优先 visualViewport；native insets.ime 只在没有 visualViewport 时兜底。 */
  const removeImeTracking = installImeTracking();

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
    root.style.setProperty("--se-safe-top", `${top}px`);
    root.style.setProperty("--se-safe-bottom", `${bottom}px`);
    if (!window.visualViewport) {
      root.style.setProperty("--ime-height", `${ime}px`);
      root.style.setProperty("--se-ime", `${ime}px`);
    }
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
    if (event.detail.type === "notification_open" && isInternalNotificationRoute(event.detail.route || "")) {
      /* 外部深链：交给 hashchange → syncFromHash（App 只注册一次监听）。
         白名单与 Android NativePolicy.validRoute 对齐（含 persona/settings/requests）。 */
      window.location.hash = event.detail.route!.trim();
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

  /* 设备绑定兜底：JPush 只在首次拿到 registrationId 时派发一次 registered 事件；
     如果那一次事件早于页面脚本就绪（启动竞态）或上报请求失败，后端就永远没有
     这台设备。每次页面启动时再读一次真实状态并幂等上报，保证后端能下发推送。 */
  try {
    const native = nativeBridge();
    const rawStatus = native?.getNotificationStatus?.();
    if (rawStatus) {
      const status = JSON.parse(rawStatus) as { registrationId?: string | null; vendor?: string };
      const registrationId = typeof status.registrationId === "string" ? status.registrationId : "";
      if (registrationId) {
        void clientApi.registerDevice(registrationId, status.vendor).catch(() => undefined);
      }
    }
  } catch {
    /* 浏览器 / 老版本原生壳没有这个桥。 */
  }

  window.addEventListener("speakeasy:native", nativeEvent);
  return () => {
    removeCallLayout();
    removeImeTracking();
    window.removeEventListener("speakeasy:native", nativeEvent);
    if (window.speakeasyBack === handleBack) delete window.speakeasyBack;
  };
}
