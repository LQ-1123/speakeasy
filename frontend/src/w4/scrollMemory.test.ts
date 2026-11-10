import { describe, expect, it } from "vitest";
import {
  BOTTOM_SNAP_THRESHOLD,
  distanceFromBottom,
  isNearBottom,
  isScrollingUp,
  nextStickState,
} from "./useScrollMemory";

const metrics = (scrollTop: number, scrollHeight = 1000, clientHeight = 500) => ({
  scrollTop,
  scrollHeight,
  clientHeight,
});

describe("distanceFromBottom / isNearBottom", () => {
  it("到底部距离为 0，越往上越大", () => {
    expect(distanceFromBottom(metrics(500))).toBe(0);
    expect(distanceFromBottom(metrics(480))).toBe(20);
    expect(distanceFromBottom(metrics(200))).toBe(300);
  });

  it("回弹/过度滚动时夹紧为 0，不会出现负数", () => {
    expect(distanceFromBottom(metrics(620))).toBe(0);
  });

  it("阈值内算贴底，阈值外不算", () => {
    expect(isNearBottom(metrics(500))).toBe(true);
    expect(isNearBottom(metrics(500 - BOTTOM_SNAP_THRESHOLD))).toBe(true);
    expect(isNearBottom(metrics(500 - BOTTOM_SNAP_THRESHOLD - 1))).toBe(false);
    expect(isNearBottom(metrics(500 - BOTTOM_SNAP_THRESHOLD - 1), 0)).toBe(false);
  });

  it("非法滚动数据（尚未布局）按不贴底处理", () => {
    expect(Number.isFinite(distanceFromBottom(metrics(Number.NaN)))).toBe(false);
    expect(isNearBottom(metrics(Number.NaN))).toBe(false);
  });
});

describe("nextStickState", () => {
  it("进入会话/自己发消息/聚焦输入框时强制贴底", () => {
    expect(nextStickState(false, metrics(0), { forceBottom: true })).toBe(true);
    expect(nextStickState(true, metrics(0), { forceBottom: true })).toBe(true);
  });

  it("用户上翻后不再自动贴底，即使还在阈值内", () => {
    expect(nextStickState(false, metrics(480))).toBe(false);
  });

  it("已经在贴底且还在底部附近时继续跟随", () => {
    expect(nextStickState(true, metrics(500))).toBe(true);
    expect(nextStickState(true, metrics(500 - BOTTOM_SNAP_THRESHOLD))).toBe(true);
    expect(nextStickState(true, metrics(200))).toBe(false);
  });
});

describe("isScrollingUp", () => {
  it("scrollTop 明显变小才算用户上翻", () => {
    expect(isScrollingUp(500, 400)).toBe(true);
    expect(isScrollingUp(500, 490)).toBe(false);
    expect(isScrollingUp(400, 500)).toBe(false);
  });
});
