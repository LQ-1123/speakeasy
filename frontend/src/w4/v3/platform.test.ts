import { describe, expect, it } from "vitest";
import { computeImeHeight } from "./platform";

/**
 * 输入法（IME）遮挡高度的纯函数契约：
 * --ime-height / --se-ime 的值 = 键盘遮挡住的 CSS px；0 表示键盘关闭。
 * 这些用例同时是 chat-ui composer 布局的输入契约。
 */
describe("computeImeHeight", () => {
  it("键盘弹起：布局视口 - 视觉视口 = 遮挡高度", () => {
    expect(computeImeHeight({ innerHeight: 800, visualViewport: { height: 500, offsetTop: 0 } })).toBe(300);
  });

  it("页面被键盘向上顶时扣掉 offsetTop", () => {
    expect(computeImeHeight({ innerHeight: 800, visualViewport: { height: 500, offsetTop: 20 } })).toBe(280);
  });

  it("键盘关闭时恒为 0", () => {
    expect(computeImeHeight({ innerHeight: 800, visualViewport: { height: 800, offsetTop: 0 } })).toBe(0);
    expect(computeImeHeight({ innerHeight: 844, visualViewport: { height: 844, offsetTop: 0 } })).toBe(0);
  });

  it("视觉视口比布局视口高（工具栏折叠/缩放手势）时钳制为 0，不产生负 inset", () => {
    expect(computeImeHeight({ innerHeight: 700, visualViewport: { height: 760, offsetTop: 0 } })).toBe(0);
    expect(computeImeHeight({ innerHeight: 700, visualViewport: { height: 500, offsetTop: 260 } })).toBe(0);
  });

  it("负 offsetTop（下拉回弹等瞬时状态）按 0 处理", () => {
    expect(computeImeHeight({ innerHeight: 800, visualViewport: { height: 500, offsetTop: -15 } })).toBe(300);
  });

  it("缺少 visualViewport 或字段非法时返回 0（native insets 兜底由 installPlatform 负责）", () => {
    expect(computeImeHeight({ innerHeight: 800 })).toBe(0);
    expect(computeImeHeight({ innerHeight: 800, visualViewport: null })).toBe(0);
    expect(computeImeHeight({ innerHeight: Number.NaN, visualViewport: { height: 500, offsetTop: 0 } })).toBe(0);
    expect(computeImeHeight({ innerHeight: 0, visualViewport: { height: 500, offsetTop: 0 } })).toBe(0);
    expect(computeImeHeight({ innerHeight: 800, visualViewport: { height: 0, offsetTop: 0 } })).toBe(0);
    expect(computeImeHeight({ innerHeight: 800, visualViewport: { height: Number.NaN, offsetTop: 0 } })).toBe(0);
    expect(computeImeHeight({ innerHeight: 800, visualViewport: { height: 500, offsetTop: Number.NaN } })).toBe(300);
  });

  it("小数四舍五入为整数 CSS px", () => {
    expect(computeImeHeight({ innerHeight: 800.4, visualViewport: { height: 500.2, offsetTop: 0 } })).toBe(300);
    expect(computeImeHeight({ innerHeight: 800, visualViewport: { height: 499.5, offsetTop: 0 } })).toBe(301);
    expect(computeImeHeight({ innerHeight: 800.6, visualViewport: { height: 500.2, offsetTop: 0.2 } })).toBe(300);
  });
});
