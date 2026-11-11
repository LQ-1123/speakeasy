import { describe, expect, it } from "vitest";

import { mapClientBootstrap } from "../api";
import type { Profile } from "../model";
import type { ClientBootstrap } from "../v3/clientApi";
import {
  MAX_INTERESTS,
  MAX_TARGET_LANGUAGES,
  addCustomInterest,
  buildPayload,
  createDraft,
  firstInvalidStep,
  levelFor,
  normalizeInterest,
  stepForMissing,
  toggleInterest,
  toggleTarget,
  validateStep,
  withTargetLevel,
  type OnboardingDraft,
} from "./Onboarding";

/**
 * 这些用例只覆盖纯逻辑（校验 / 去重 / 水平映射 / payload / bootstrap 映射），
 * 不渲染 DOM —— 组件本身由设计验收和手动流程验证。
 */

/** 一份三步都合法的基础草稿 */
function draft(overrides: Partial<OnboardingDraft> = {}): OnboardingDraft {
  return {
    name: "小林",
    nativeLanguage: "zh",
    targets: ["en"],
    levels: { en: "能聊几句" },
    interests: ["Coffee"],
    communicationPreference: "natural",
    ...overrides,
  };
}

describe("第一步：昵称与母语", () => {
  it("缺昵称或母语时不能继续", () => {
    expect(validateStep(1, draft({ name: "" })).length).toBeGreaterThan(0);
    expect(validateStep(1, draft({ name: "   " })).length).toBeGreaterThan(0);
    expect(validateStep(1, draft({ nativeLanguage: "" })).length).toBeGreaterThan(0);
    /* 两个都缺时两条错误都要给出来，方便一次提示完 */
    expect(validateStep(1, draft({ name: "", nativeLanguage: "" }))).toHaveLength(2);
  });

  it("昵称与母语都填好后可以继续", () => {
    expect(validateStep(1, draft())).toEqual([]);
    expect(firstInvalidStep(draft())).not.toBe(1);
  });
});

describe("第二步：目标语言与水平", () => {
  it("没有目标语言时不能继续", () => {
    expect(validateStep(2, draft({ targets: [], levels: {} })).length).toBeGreaterThan(0);
  });

  it("多门语言各自独立的水平", () => {
    let levels: Record<string, string> = {};
    levels = withTargetLevel(levels, "en", "日常对话");
    levels = withTargetLevel(levels, "de", "刚开始");
    expect(levelFor(levels, "en")).toBe("日常对话");
    expect(levelFor(levels, "de")).toBe("刚开始");
    expect(levelFor(levels, "en")).not.toBe(levelFor(levels, "de"));
    /* 没设置过的语言回落到默认「能聊几句」 */
    expect(levelFor(levels, "ja")).toBe("能聊几句");
    expect(levelFor({}, "en")).toBe("能聊几句");
  });

  it("最多同时选 3 门，且母语不可选", () => {
    const targets = ["en", "ja", "ko"];
    /* 第 4 门被拒绝：返回同一个数组引用，UI 据此提示上限 */
    expect(toggleTarget(targets, "es", "zh")).toBe(targets);
    expect(toggleTarget(["en"], "zh", "zh")).toEqual(["en"]);
    /* 已选语言再点一次 = 取消 */
    expect(toggleTarget(["en", "ja"], "en", "zh")).toEqual(["ja"]);
    expect(MAX_TARGET_LANGUAGES).toBe(3);
  });
});

describe("第三步：兴趣标签", () => {
  it(`最多 ${MAX_INTERESTS} 个，超出被拒绝`, () => {
    const full = [
      "Technology",
      "Gaming",
      "Music",
      "Movies",
      "Travel",
      "Photography",
      "Coffee",
      "Food",
    ];
    expect(full).toHaveLength(MAX_INTERESTS);
    expect(toggleInterest(full, "Sports")).toBe(full);
    expect(toggleInterest(full.slice(0, MAX_INTERESTS - 1), "Sports")).toEqual([
      ...full.slice(0, MAX_INTERESTS - 1),
      "Sports",
    ]);
    expect(toggleInterest(full, "Coffee")).toEqual(full.filter((tag) => tag !== "Coffee"));
  });

  it("自定义标签去重（忽略大小写/空白/#）且不超上限", () => {
    const list = ["Coffee"];
    expect(addCustomInterest(list, " coffee ")).toBe(list);
    expect(addCustomInterest(list, "   ")).toBe(list);
    expect(addCustomInterest(list, "#Coffee")).toBe(list);
    expect(addCustomInterest(list, "Gaming")).toEqual(["Coffee", "Gaming"]);
    expect(normalizeInterest("  #  街头 摄影  ")).toBe("街头 摄影");

    const full = Array.from({ length: MAX_INTERESTS }, (_, index) => `tag-${index}`);
    expect(addCustomInterest(full, "Sports")).toBe(full);
  });

  it("构建 payload 时兴趣去重并截断到上限", () => {
    const payload = buildPayload(
      draft({
        interests: ["Coffee", "coffee", "Coffee", ...Array.from({ length: 10 }, (_, i) => `t${i}`)],
      }),
    );
    expect(payload.interests).toHaveLength(MAX_INTERESTS);
    expect(new Set(payload.interests.map((item) => item.toLowerCase())).size).toBe(
      payload.interests.length,
    );
  });
});

describe("保存 payload", () => {
  it("第三步点完成时输出后端需要的全部字段", () => {
    const payload = buildPayload(
      draft({
        name: "  小林  ",
        nativeLanguage: "zh",
        targets: ["en", "de"],
        levels: { en: "日常对话", de: "刚开始" },
        interests: ["Coffee", "Food"],
        communicationPreference: "improve",
      }),
    );
    expect(payload).toEqual({
      nickname: "小林",
      native_language: "zh",
      target_languages: ["en", "de"],
      levels: { en: "日常对话", de: "刚开始" },
      interests: ["Coffee", "Food"],
      communication_preference: "improve",
      complete_setup: true,
    });
  });

  it("母语不会被当成目标语言，缺失的水平补默认值", () => {
    const payload = buildPayload(
      draft({ nativeLanguage: "en", targets: ["en", "ja", "ko", "es"], levels: { ja: "日常对话" } }),
    );
    expect(payload.target_languages).toEqual(["ja", "ko", "es"]);
    expect(payload.levels).toEqual({
      ja: "日常对话",
      ko: "能聊几句",
      es: "能聊几句",
    });
  });

  it("缺字段的 draft 也能产出完整结构（不抛异常）", () => {
    const payload = buildPayload(
      draft({ name: "", nativeLanguage: "", targets: [], levels: {}, interests: [] }),
    );
    expect(payload.nickname).toBe("");
    expect(payload.native_language).toBe("zh");
    expect(payload.target_languages).toEqual([]);
    expect(payload.complete_setup).toBe(true);
  });
});

describe("400 profile_incomplete 的字段 → 步骤映射", () => {
  it("按 missing 跳回对应步骤", () => {
    expect(stepForMissing(["nickname"])).toBe(1);
    expect(stepForMissing(["native_language"])).toBe(1);
    expect(stepForMissing(["target_languages", "levels"])).toBe(2);
    expect(stepForMissing(["interests", "communication_preference"])).toBe(3);
    /* 未知字段兜底回第一步，不会卡住 */
    expect(stepForMissing(["something_else"])).toBe(1);
    expect(stepForMissing([])).toBe(1);
  });
});

describe("createDraft：用已有 profile 预填", () => {
  const profile: Profile = {
    name: "我",
    avatar: "",
    city: "",
    job: "",
    targets: ["en", "zh", "en"],
    level: "能聊几句",
    interests: ["Coffee"],
    weaknesses: [],
    nativeLanguage: "zh",
    levels: { en: "日常对话" },
    gender: "",
  };

  it("忽略占位昵称、过滤母语、去重目标语言", () => {
    const initial = createDraft(profile);
    expect(initial.name).toBe("");
    expect(initial.targets).toEqual(["en"]);
    expect(initial.levels.en).toBe("日常对话");
    expect(initial.communicationPreference).toBe("natural");
  });

  it("保留真实昵称与已设置偏好", () => {
    const initial = createDraft({
      ...profile,
      name: "小林",
      communicationPreference: "occasional",
    });
    expect(initial.name).toBe("小林");
    expect(initial.communicationPreference).toBe("occasional");
  });
});

/** 构造最小可用的 bootstrap 返回 */
function bootstrap(profile: Record<string, unknown>): ClientBootstrap {
  return {
    profile,
    friends: [],
    requests: [],
    conversations: [],
    expressions: [],
    capabilities: {},
    unread_events: 0,
  };
}

describe("bootstrap 映射：首次引导状态", () => {
  it("profile_setup_completed_at 映射为 setupCompleted / setupCompletedAt", () => {
    const done = mapClientBootstrap(
      bootstrap({
        nickname: "小林",
        profile_setup_completed_at: "2026-11-10T09:00:00Z",
        fact_sheet: { communication_preference: "improve" },
      }),
    );
    expect(done.profile.setupCompleted).toBe(true);
    expect(done.profile.setupCompletedAt).toBe("2026-11-10T09:00:00Z");
    expect(done.profile.communicationPreference).toBe("improve");
  });

  it("空字符串表示未完成，非法偏好回落为空", () => {
    const pending = mapClientBootstrap(
      bootstrap({
        nickname: "小林",
        profile_setup_completed_at: "",
        fact_sheet: { communication_preference: "whatever" },
      }),
    );
    expect(pending.profile.setupCompleted).toBe(false);
    expect(pending.profile.setupCompletedAt).toBe("");
    expect(pending.profile.communicationPreference).toBe("");
  });

  it("fact_sheet 里的 communication_preference 三种合法值都保留", () => {
    for (const value of ["natural", "occasional", "improve"] as const) {
      const snapshot = mapClientBootstrap(
        bootstrap({
          nickname: "小林",
          profile_setup_completed_at: "",
          fact_sheet: { communication_preference: value },
        }),
      );
      expect(snapshot.profile.communicationPreference).toBe(value);
    }
  });
});
