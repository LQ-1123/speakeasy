import { describe, expect, it } from "vitest";
import { closeTopOverlay, registerOverlay, useNavigation } from "./navigationStore";

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

describe("navigation store 单一写入源", () => {
  it("setRoute 对相同路由短路：不通知订阅者（hashchange 不会造成二次渲染）", () => {
    useNavigation.setState({ route: { name: "chats" } });
    let notified = 0;
    const unsubscribe = useNavigation.subscribe(() => {
      notified += 1;
    });
    try {
      useNavigation.getState().setRoute({ name: "chats" });
      expect(notified).toBe(0);

      useNavigation.getState().setRoute({ name: "chat", friendId: "f1" });
      expect(notified).toBe(1);

      /* 同一路由的重复写入（旧实现的 navigate + hashchange 双写）不应触发渲染 */
      useNavigation.getState().setRoute({ name: "chat", friendId: "f1" });
      expect(notified).toBe(1);

      useNavigation.getState().setRoute({ name: "chat", friendId: "f2" });
      expect(notified).toBe(2);
    } finally {
      unsubscribe();
      useNavigation.setState({ route: { name: "chats" } });
    }
  });
});
