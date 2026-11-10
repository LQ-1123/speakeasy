import { describe, expect, it } from "vitest";
import { durationLabel, voiceBubbleWidth, voiceSeconds } from "./format";

describe("durationLabel", () => {
  it("至少显示 1 秒，不出现 0:00", () => {
    expect(durationLabel(0)).toBe("0:01");
    expect(durationLabel(1)).toBe("0:01");
    expect(durationLabel(999)).toBe("0:01");
    expect(durationLabel(1000)).toBe("0:01");
  });

  it("1–59 秒按秒显示", () => {
    expect(durationLabel(3000)).toBe("0:03");
    expect(durationLabel(18_400)).toBe("0:18");
    expect(durationLabel(59_000)).toBe("0:59");
  });

  it("60 秒进位到 1:00，而不是 0:60", () => {
    expect(durationLabel(60_000)).toBe("1:00");
    expect(durationLabel(62_000)).toBe("1:02");
  });
});

describe("voiceSeconds", () => {
  it("夹紧到 1–60 秒", () => {
    expect(voiceSeconds(0)).toBe(1);
    expect(voiceSeconds(-5000)).toBe(1);
    expect(voiceSeconds(30_000)).toBe(30);
    expect(voiceSeconds(60_000)).toBe(60);
    expect(voiceSeconds(90_000)).toBe(60);
    expect(voiceSeconds(Number.NaN)).toBe(1);
  });
});

describe("voiceBubbleWidth", () => {
  it("1 秒 80px、60 秒 220px，线性增长", () => {
    expect(voiceBubbleWidth(1000)).toBe(80);
    expect(voiceBubbleWidth(60_000)).toBe(220);
    expect(voiceBubbleWidth(0)).toBe(80);
    expect(voiceBubbleWidth(120_000)).toBe(220);
    expect(voiceBubbleWidth(30_000)).toBe(149);
  });

  it("单调不减，且始终落在 [80, 220]", () => {
    const widths = [1000, 5000, 12_000, 20_000, 35_000, 50_000, 60_000].map((ms) =>
      voiceBubbleWidth(ms),
    );
    for (let index = 1; index < widths.length; index += 1) {
      expect(widths[index]).toBeGreaterThanOrEqual(widths[index - 1]);
    }
    for (const width of widths) {
      expect(width).toBeGreaterThanOrEqual(80);
      expect(width).toBeLessThanOrEqual(220);
    }
  });

  it("320px 窄屏：60 秒语音条不超过消息列可用宽度（气泡内左右 padding 共 24px）", () => {
    /* chat.css 在 @media (max-width: 380px) 下 .w4-msg-col max-width 84%，聊天区内边距 14px×2 */
    const chatInnerWidth = 320 - 14 * 2;
    const columnWidth = chatInnerWidth * 0.84;
    expect(voiceBubbleWidth(60_000) + 24).toBeLessThanOrEqual(Math.floor(columnWidth));
  });
});
