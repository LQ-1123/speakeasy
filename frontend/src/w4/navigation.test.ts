import { describe, expect, it } from "vitest";
import {
  backRoute,
  isInternalNotificationRoute,
  isTabRoot,
  normalizeHash,
  parseRoute,
  reduceNavigation,
  routeToHash,
  routesEqual,
  sectionOf,
  type NavigationState,
} from "./navigation";

describe("four-tab routes", () => {
  it("keeps contacts as its own destination and expressions inside me", () => {
    expect(sectionOf({ name: "chats" })).toBe("chats");
    expect(sectionOf({ name: "contacts" })).toBe("contacts");
    expect(sectionOf({ name: "contact", friendId: "f1" })).toBe("contacts");
    expect(sectionOf({ name: "requests" })).toBe("contacts");
    expect(sectionOf({ name: "expressions" })).toBe("me");
  });

  it("round trips encoded ids in internal routes", () => {
    const route = { name: "chat", friendId: "person/one" } as const;
    expect(parseRoute(routeToHash(route))).toEqual(route);
  });

  it("backs from a detail to its parent destination", () => {
    expect(backRoute({ name: "chat", friendId: "f1" })).toEqual({ name: "chats" });
    expect(backRoute({ name: "expressions" })).toEqual({ name: "me" });
    expect(backRoute({ name: "requests" })).toEqual({ name: "contacts" });
  });
});

describe("hash 编解码", () => {
  it("所有路由都能 routeToHash → parseRoute 往返", () => {
    const routes = [
      { name: "chats" },
      { name: "chat", friendId: "f 1" },
      { name: "contacts" },
      { name: "contact", friendId: "好友/1" },
      { name: "discover" },
      { name: "discoverPeople" },
      { name: "events" },
      { name: "persona", personaId: "persona-3" },
      { name: "expressions" },
      { name: "me" },
      { name: "settings" },
      { name: "settings", section: "notify" },
      { name: "requests" },
    ] as const;
    for (const route of routes) {
      expect(parseRoute(routeToHash(route))).toEqual(route);
    }
  });

  it("discover 的子路由共用 #/discover 这个 hash", () => {
    expect(routeToHash({ name: "discover" })).toBe("#/discover");
    expect(routeToHash({ name: "discoverFeed" })).toBe("#/discover");
    expect(routeToHash({ name: "discoverMoments" })).toBe("#/discover");
    expect(routeToHash({ name: "moments" })).toBe("#/discover");
    expect(routeToHash({ name: "discoverPeople" })).toBe("#/discover/people");
  });

  it("空/异常 hash 归一化到首页", () => {
    expect(parseRoute("")).toEqual({ name: "chats" });
    expect(parseRoute("#/unknown")).toEqual({ name: "chats" });
    expect(normalizeHash("")).toBe("#/chats");
    expect(normalizeHash("#")).toBe("#/chats");
    expect(normalizeHash("#/")).toBe("#/chats");
    expect(normalizeHash("#/chats")).toBe("#/chats");
    expect(normalizeHash("/chats")).toBe("#/chats");
  });

  it("settings 的 section 会保留", () => {
    expect(parseRoute("#/settings/notify")).toEqual({ name: "settings", section: "notify" });
    expect(parseRoute("#/settings")).toEqual({ name: "settings", section: undefined });
  });
});

describe("back 与 tab 根", () => {
  it("所有返回目标都落在 tab 根（不会 push 历史）", () => {
    const routes = [
      { name: "chat", friendId: "f1" },
      { name: "contact", friendId: "f1" },
      { name: "persona", personaId: "p1" },
      { name: "discoverPeople" },
      { name: "discoverFeed" },
      { name: "discoverMoments" },
      { name: "moments" },
      { name: "events" },
      { name: "settings", section: "notify" },
      { name: "expressions" },
      { name: "requests" },
    ] as const;
    for (const route of routes) {
      expect(isTabRoot(backRoute(route))).toBe(true);
    }
    expect(backRoute({ name: "discoverFeed" })).toEqual({ name: "discover" });
    expect(backRoute({ name: "moments" })).toEqual({ name: "discover" });
  });

  it("isTabRoot 只认四个主 tab", () => {
    expect(isTabRoot({ name: "chats" })).toBe(true);
    expect(isTabRoot({ name: "contacts" })).toBe(true);
    expect(isTabRoot({ name: "discover" })).toBe(true);
    expect(isTabRoot({ name: "me" })).toBe(true);
    expect(isTabRoot({ name: "requests" })).toBe(false);
    expect(isTabRoot({ name: "chat", friendId: "f1" })).toBe(false);
  });

  it("routesEqual 区分参数、忽略 settings 的空 section", () => {
    expect(routesEqual({ name: "chat", friendId: "a" }, { name: "chat", friendId: "a" })).toBe(true);
    expect(routesEqual({ name: "chat", friendId: "a" }, { name: "chat", friendId: "b" })).toBe(false);
    expect(routesEqual({ name: "chats" }, { name: "contacts" })).toBe(false);
    expect(routesEqual({ name: "settings" }, { name: "settings", section: "" })).toBe(true);
    expect(routesEqual({ name: "settings", section: "notify" }, { name: "settings" })).toBe(false);
  });
});

describe("原生通知 route 白名单（对齐 NativePolicy.validRoute）", () => {
  it("放行应用内全部合法路由", () => {
    const valid = [
      "#/chats",
      "#/chat/fixture-1",
      "#/chat/f%201",
      "#/contacts",
      "#/contacts/fixture-1",
      "#/discover",
      "#/discover/people",
      "#/events",
      "#/persona/persona-3",
      "#/expressions",
      "#/me",
      "#/settings",
      "#/settings/notify",
      "#/requests",
    ];
    for (const route of valid) expect(isInternalNotificationRoute(route)).toBe(true);
  });

  it("拒绝未知路由、越权 URL 与畸形 route", () => {
    const invalid = [
      "",
      "#",
      "#/",
      "#/unknown",
      "#/chat",
      "#/chat/a/b",
      "#/chat/a b",
      "#/chat/..",
      "#/persona",
      "#/settings/NOTIFY",
      "#/settings/a/b",
      "#/discover/foo",
      "#/requests/1",
      "#/events/1",
      "https://evil.example/#/chats",
      "javascript:alert(1)",
      `#/me${"x".repeat(600)}`,
    ];
    for (const route of invalid) expect(isInternalNotificationRoute(route)).toBe(false);
  });

  it("应用内 routeToHash 的输出必然通过白名单", () => {
    const routes = [
      { name: "chats" },
      { name: "chat", friendId: "f 1" },
      { name: "contacts" },
      { name: "contact", friendId: "好友/1" },
      { name: "discover" },
      { name: "discoverPeople" },
      { name: "discoverFeed" },
      { name: "events" },
      { name: "persona", personaId: "persona-3" },
      { name: "expressions" },
      { name: "me" },
      { name: "settings" },
      { name: "settings", section: "notify" },
      { name: "requests" },
    ] as const;
    for (const route of routes) expect(isInternalNotificationRoute(routeToHash(route))).toBe(true);
  });
});

describe("reduceNavigation（单一点击 → 单次历史操作）", () => {
  const chats: NavigationState = { route: { name: "chats" }, hash: "#/chats" };
  const chat: NavigationState = { route: { name: "chat", friendId: "f1" }, hash: "#/chat/f1" };

  it("打开会话：push 一次，状态与 hash 一起前进", () => {
    const result = reduceNavigation(chats, { type: "navigate", route: { name: "chat", friendId: "f1" } });
    expect(result.history).toBe("push");
    expect(result.routeChanged).toBe(true);
    expect(result.state).toEqual(chat);
  });

  it("返回会话列表用 replace：不会把列表再压一层返回栈", () => {
    const result = reduceNavigation(chat, { type: "back" });
    expect(result.history).toBe("replace");
    expect(result.routeChanged).toBe(true);
    expect(result.state.route).toEqual({ name: "chats" });
    expect(result.state.hash).toBe("#/chats");
  });

  it("tab 切换用 replace（顶部/底部 tab 不污染 Android 返回栈）", () => {
    const result = reduceNavigation(chats, { type: "navigate", route: { name: "contacts" } });
    expect(result.history).toBe("replace");
    expect(result.state.route).toEqual({ name: "contacts" });
  });

  it("详情页仍 push（从会话列表进入人物/资料等）", () => {
    const result = reduceNavigation(chats, { type: "navigate", route: { name: "requests" } });
    expect(result.history).toBe("push");
    const fromContact = reduceNavigation(
      { route: { name: "contact", friendId: "f1" }, hash: "#/contacts/f1" },
      { type: "navigate", route: { name: "chat", friendId: "f1" } },
    );
    expect(fromContact.history).toBe("push");
  });

  it("重复导航到当前路由：零操作（配合 setRoute 短路，避免二次渲染）", () => {
    const result = reduceNavigation(chats, { type: "navigate", route: { name: "chats" } });
    expect(result.history).toBe("none");
    expect(result.routeChanged).toBe(false);
    expect(result.state).toBe(chats);
  });

  it("共用 hash 的子路由只更新 store：discover ↔ discoverFeed", () => {
    const discover: NavigationState = { route: { name: "discover" }, hash: "#/discover" };
    const toFeed = reduceNavigation(discover, { type: "navigate", route: { name: "discoverFeed" } });
    expect(toFeed.history).toBe("none");
    expect(toFeed.routeChanged).toBe(true);
    expect(toFeed.state.hash).toBe("#/discover");
    expect(toFeed.state.route).toEqual({ name: "discoverFeed" });

    const back = reduceNavigation(toFeed.state, { type: "back" });
    expect(back.history).toBe("none");
    expect(back.routeChanged).toBe(true);
    expect(back.state.route).toEqual({ name: "discover" });
  });

  it("hashchange（浏览器后退/通知深链）只同步 store，不写历史", () => {
    const result = reduceNavigation(chat, { type: "hashchange", hash: "#/chats" });
    expect(result.history).toBe("none");
    expect(result.routeChanged).toBe(true);
    expect(result.state.route).toEqual({ name: "chats" });

    const noop = reduceNavigation(chats, { type: "hashchange", hash: "#/chats" });
    expect(noop.routeChanged).toBe(false);
    expect(noop.history).toBe("none");
    expect(noop.state).toBe(chats);
  });

  it("tab 根返回 false（没有可返回的页面，交给原生兜底）", () => {
    const result = reduceNavigation(chats, { type: "back" });
    expect(result.routeChanged).toBe(false);
    expect(result.history).toBe("none");
  });

  it("深链直接进聊天（无历史）按返回也能落到会话列表", () => {
    const deepLink: NavigationState = { route: { name: "chat", friendId: "deeplink" }, hash: "#/chat/deeplink" };
    const result = reduceNavigation(deepLink, { type: "back" });
    expect(result.history).toBe("replace");
    expect(result.state.route).toEqual({ name: "chats" });
  });
});
