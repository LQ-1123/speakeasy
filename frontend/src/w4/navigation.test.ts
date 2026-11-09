import { describe, expect, it } from "vitest";
import { backRoute, parseRoute, routeToHash, sectionOf } from "./navigation";

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
