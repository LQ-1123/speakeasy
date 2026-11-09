import { describe, expect, it } from "vitest";
import { closeTopOverlay, registerOverlay } from "./navigationStore";

describe("system back dispatch", () => {
  it("closes the newest overlay and leaves navigation to the next back", () => {
    const calls: string[] = [];
    const removeFirst = registerOverlay(() => calls.push("first"));
    const removeSecond = registerOverlay(() => calls.push("second"));
    expect(closeTopOverlay()).toBe(true);
    expect(calls).toEqual(["second"]);
    removeSecond();
    expect(closeTopOverlay()).toBe(true);
    expect(calls).toEqual(["second", "first"]);
    removeFirst();
    expect(closeTopOverlay()).toBe(false);
  });
});
