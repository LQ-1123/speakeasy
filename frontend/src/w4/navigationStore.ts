import { create } from "zustand";
import type { Route } from "./model";
import {
  normalizeHash,
  parseRoute,
  reduceNavigation,
  routesEqual,
  type NavigationEvent,
  type NavigationResult,
  type NavigationState,
} from "./navigation";

interface NavigationStore {
  route: Route;
  /**
   * 唯一写入口：路由相等时短路，避免 hashchange / 直接导航重复触发渲染。
   * 历史层（pushState/replaceState）由本模块的 navigateTo/backToParent 负责，
   * 组件不要直接改 location.hash。
   */
  setRoute: (route: Route) => void;
}

export const useNavigation = create<NavigationStore>((set, get) => ({
  route: parseRoute(typeof window === "undefined" ? "" : window.location.hash),
  setRoute: (route) => {
    if (routesEqual(get().route, route)) return;
    set({ route });
  },
}));

function readState(): NavigationState {
  const hash = typeof window === "undefined" ? "#/chats" : normalizeHash(window.location.hash);
  return { route: useNavigation.getState().route, hash };
}

function apply(event: NavigationEvent): NavigationResult {
  const result = reduceNavigation(readState(), event);
  if (typeof window !== "undefined") {
    if (result.history === "push") {
      // pushState 不触发 hashchange：状态由下面显式写一次，避免二次渲染。
      window.history.pushState(null, "", result.state.hash);
    } else if (result.history === "replace") {
      // replaceState 同样不触发 hashchange：显式写 store（且只写一次）。
      window.history.replaceState(null, "", result.state.hash);
    }
  }
  if (result.routeChanged) useNavigation.getState().setRoute(result.state.route);
  return result;
}

/**
 * 应用内导航唯一入口。返回 reduceNavigation 的决策（history/routeChanged），
 * 调用方据此判断「是否真的发生了导航」（系统返回键需要 true/false）。
 */
export function navigateTo(route: Route): NavigationResult {
  return apply({ type: "navigate", route });
}

/** 系统返回键：走页面层级父路由；命中 tab 根/父级时使用 replace，不污染返回栈。 */
export function backToParent(): NavigationResult {
  return apply({ type: "back" });
}

/**
 * 外部 hash 变化（浏览器后退/前进、通知、深链、?preview 桥）同步进 store。
 * 这是 URL 作为唯一事实来源的入口；程序内导航不再走 hashchange，避免双重 setRoute。
 */
export function syncFromHash(hash?: string): NavigationResult {
  return apply({ type: "hashchange", hash: hash ?? (typeof window === "undefined" ? "" : window.location.hash) });
}

const overlays: Array<() => void> = [];
export function registerOverlay(close: () => void): () => void {
  overlays.push(close);
  return () => { const index = overlays.lastIndexOf(close); if (index >= 0) overlays.splice(index, 1); };
}
export function closeTopOverlay(): boolean {
  const close = overlays[overlays.length - 1];
  if (!close) return false;
  close();
  return true;
}
