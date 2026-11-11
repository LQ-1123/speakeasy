import { afterEach, expect, it, vi } from "vitest";

import {
  CLEAR_PREFIXES,
  clearLocalClientData,
  clearSessionState,
  selectKeysToClear,
} from "./logoutCleanup";

afterEach(() => vi.unstubAllGlobals());

function memoryStorage(initial: Record<string, string>) {
  const store: Record<string, unknown> = { ...initial };
  store.getItem = (key: string) => (typeof store[key] === "string" ? (store[key] as string) : null);
  store.setItem = (key: string, value: string) => {
    store[key] = value;
  };
  store.removeItem = (key: string) => {
    delete store[key];
  };
  store.clear = () => {
    for (const key of Object.keys(store)) {
      if (typeof store[key] === "string") delete store[key];
    }
  };
  return store as unknown as Storage & { data: Map<string, string> };
}

it("selects only account/cache keys, never unrelated app data", () => {
  const keys = [
    "speakeasy.identity.v1",
    "speakeasy.events.cursor.v1:u_1",
    "sonder.session.v1",
    "sonder.ad.impression.ad_1",
    "w4.seen.abc",
    "theme-mode",
    "some-other-app-key",
  ];
  expect(selectKeysToClear(keys)).toEqual([
    "speakeasy.identity.v1",
    "speakeasy.events.cursor.v1:u_1",
    "sonder.session.v1",
    "sonder.ad.impression.ad_1",
    "w4.seen.abc",
  ]);
  expect(CLEAR_PREFIXES.length).toBe(3);
});

it("clears local storage keys and returns removed names", () => {
  const store = memoryStorage({
    "speakeasy.identity.v1": "x",
    "sonder.session.v1": "y",
    "w4.seen.1": "z",
    "keep-me": "1",
  });
  const removed = clearLocalClientData(store);
  expect(removed.sort()).toEqual(["sonder.session.v1", "speakeasy.identity.v1", "w4.seen.1"]);
  expect(store.getItem("keep-me")).toBe("1");
  expect(store.getItem("sonder.session.v1")).toBeNull();
});

it("clears session storage entirely", () => {
  const store = memoryStorage({ "sonder.ad.impression.ad_1": "1", other: "2" });
  clearSessionState(store);
  expect(store.getItem("sonder.ad.impression.ad_1")).toBeNull();
  expect(store.getItem("other")).toBeNull();
});
