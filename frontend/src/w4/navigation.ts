import type { Route, Section } from "./model";

/**
 * 导航纯函数层：URL（hash）是唯一事实来源，`routeToHash` / `parseRoute`
 * 是唯一的 URL 编解码入口，`reduceNavigation` 是唯一的「导航意图 → 历史操作 +
 * 新状态」决策函数。UI 组件只表达「我要去哪」，不直接碰 history。
 */

export function sectionOf(route: Route): Section {
  switch (route.name) {
    case "chat":
    case "chats":
      return "chats";
    case "contacts":
    case "contact":
    case "requests":
      return "contacts";
    case "discover":
    case "discoverFeed":
    case "discoverMoments":
    case "discoverPeople":
    case "persona":
    case "events":
    case "moments":
      return "discover";
    case "expressions":
    case "me":
    case "settings":
      return "me";
  }
}

export function routeToHash(route: Route): string {
  switch (route.name) {
    case "chat": return `#/chat/${encodeURIComponent(route.friendId)}`;
    case "contact": return `#/contacts/${encodeURIComponent(route.friendId)}`;
    case "persona": return `#/persona/${encodeURIComponent(route.personaId)}`;
    case "settings": return route.section ? `#/settings/${encodeURIComponent(route.section)}` : "#/settings";
    case "discoverFeed":
    case "discoverMoments":
    case "moments": return "#/discover";
    case "discoverPeople": return "#/discover/people";
    case "events": return "#/events";
    default: return `#/${route.name}`;
  }
}

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  switch (parts[0]) {
    case "chat": return parts[1] ? { name: "chat", friendId: parts[1] } : { name: "chats" };
    case "contacts": return parts[1] ? { name: "contact", friendId: parts[1] } : { name: "contacts" };
    case "discover": return parts[1] === "people" ? { name: "discoverPeople" } : { name: "discover" };
    case "persona": return parts[1] ? { name: "persona", personaId: parts[1] } : { name: "discover" };
    case "events": return { name: "events" };
    case "expressions": return { name: "expressions" };
    case "me": return { name: "me" };
    case "settings": return { name: "settings", section: parts[1] };
    case "requests": return { name: "requests" };
    case "chats": return { name: "chats" };
    default: return { name: "chats" };
  }
}

/**
 * 「返回」在页面层级上的父路由。所有分支都落在四个 tab 根上，
 * 因此系统返回永远不会 push 新的历史条目（见 reduceNavigation）。
 */
export function backRoute(route: Route): Route {
  switch (route.name) {
    case "chat": return { name: "chats" };
    case "contacts": return { name: "chats" };
    case "contact": return { name: "contacts" };
    case "persona": return { name: "discover" };
    case "discoverPeople":
    case "discoverFeed":
    case "discoverMoments":
    case "moments":
    case "events": return { name: "discover" };
    case "settings":
    case "expressions": return { name: "me" };
    case "requests": return { name: "contacts" };
    default: return route;
  }
}

/** 归一化地址栏 hash：空 hash / "#" / "#/" 都当作首页。 */
export function normalizeHash(hash: string): string {
  const raw = (hash || "").trim();
  if (!raw || raw === "#" || raw === "#/") return "#/chats";
  if (raw.startsWith("#/")) return raw;
  const path = raw.startsWith("#") ? raw.slice(1) : raw;
  return `#/${path.replace(/^\/+/, "")}`;
}

/* 与 Android NativePolicy.validRoute 相同的字符集，保证两端对同一 route 的判断一致。 */
const NOTIFICATION_ID = /^[A-Za-z0-9_%.~-]{1,128}$/;
const NOTIFICATION_SETTINGS_SECTION = /^[a-z-]{1,32}$/;

/**
 * 原生通知/推送 route 白名单（与 mobile/android 的 NativePolicy.validRoute 对齐）：
 * chat/<id>、persona/<id>、contacts[/<id>]、settings[/<section>]、discover[/people]、
 * chats、events、expressions、me、requests。应用内 routeToHash 的输出必然通过。
 */
export function isInternalNotificationRoute(route: string): boolean {
  const raw = (route || "").trim();
  if (!raw.startsWith("#/") || raw.length > 512) return false;
  const path = raw.slice(2);
  if (!path || path.includes("\\") || path.includes(" ")) return false;
  const parts = path.split("/");
  if (parts.length === 0 || parts.length > 2) return false;
  for (const part of parts) {
    if (!part && parts.length > 1) return false;
    if (part === "." || part === "..") return false;
  }
  const head = parts[0];
  const single = parts.length === 1;
  const id = parts[1] || "";
  switch (head) {
    case "chat":
    case "persona":
      return parts.length === 2 && NOTIFICATION_ID.test(id);
    case "contacts":
      return single || NOTIFICATION_ID.test(id);
    case "settings":
      return single || NOTIFICATION_SETTINGS_SECTION.test(id);
    case "discover":
      return single || id === "people";
    case "chats":
    case "events":
    case "expressions":
    case "me":
    case "requests":
      return single;
    default:
      return false;
  }
}

/** 四个主导航分区（tab 根）。切 tab / 回 tab 根都不应该新增历史条目。 */
const TAB_ROOTS: ReadonlySet<Route["name"]> = new Set(["chats", "contacts", "discover", "me"]);

export function isTabRoot(route: Route): boolean {
  return TAB_ROOTS.has(route.name);
}

/** 路由语义相等（含参数），用于 store 的 setRoute 短路与渲染去重。 */
export function routesEqual(a: Route, b: Route): boolean {
  if (a.name !== b.name) return false;
  if (a.name === "chat" && b.name === "chat") return a.friendId === b.friendId;
  if (a.name === "contact" && b.name === "contact") return a.friendId === b.friendId;
  if (a.name === "persona" && b.name === "persona") return a.personaId === b.personaId;
  if (a.name === "settings" && b.name === "settings") return (a.section || "") === (b.section || "");
  return true;
}

export type HistoryAction = "none" | "push" | "replace";

export interface NavigationState {
  route: Route;
  /** 当前地址栏 hash（已归一化），与 route 一起构成导航状态。 */
  hash: string;
}

export type NavigationEvent =
  | { type: "navigate"; route: Route }
  | { type: "hashchange"; hash: string }
  | { type: "back" };

export interface NavigationResult {
  state: NavigationState;
  /** 调用方需要执行的历史操作；replace 不会触发 hashchange，需同时写 store。 */
  history: HistoryAction;
  routeChanged: boolean;
}

/**
 * 唯一的历史层决策：
 *  - `navigate`：目标 hash 与当前一致（例如 discoverFeed ↔ discover 共用一个 hash）时
 *    只更新 store，不碰历史，避免「先跳错再跳对」；
 *  - 去 tab 根 / 回父级（返回）用 replace：不会把上一页压进返回栈，
 *    修掉「从会话列表按返回又回到上一个聊天」；
 *  - 其余详情页 push：保留浏览器/链接语义。
 */
function decide(current: NavigationState, next: Route, fromBack: boolean): NavigationResult {
  const targetHash = routeToHash(next);
  const routeChanged = !routesEqual(current.route, next);
  if (targetHash === current.hash) {
    return { state: routeChanged ? { route: next, hash: current.hash } : current, history: "none", routeChanged };
  }
  const replace = fromBack || isTabRoot(next) || routesEqual(next, backRoute(current.route));
  return {
    state: { route: next, hash: targetHash },
    history: replace ? "replace" : "push",
    routeChanged: true,
  };
}

/** 可单测的导航 reducer：不碰 window，只描述历史操作与新状态。 */
export function reduceNavigation(state: NavigationState, event: NavigationEvent): NavigationResult {
  switch (event.type) {
    case "navigate":
      return decide(state, event.route, false);
    case "back":
      return decide(state, backRoute(state.route), true);
    case "hashchange": {
      const hash = normalizeHash(event.hash);
      const route = parseRoute(hash);
      const routeChanged = !routesEqual(state.route, route);
      if (!routeChanged && hash === state.hash) return { state, history: "none", routeChanged: false };
      return { state: { route: routeChanged ? route : state.route, hash }, history: "none", routeChanged };
    }
  }
}
