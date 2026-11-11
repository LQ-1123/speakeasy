import { expect, it } from "vitest";

import {
  teenBlockReason,
  teenBlockText,
  teenReminderDue,
  isAdultGroup,
} from "./teenGuard";

it("blocks only known reasons when allowed === false", () => {
  expect(teenBlockReason({ allowed: true })).toBe("");
  expect(teenBlockReason(null)).toBe("");
  expect(teenBlockReason({ allowed: false })).toBe("");
  expect(teenBlockReason({ allowed: false, reason: "curfew" })).toBe("curfew");
  expect(teenBlockReason({ allowed: false, reason: "guardian_required" })).toBe("guardian_required");
  expect(teenBlockReason({ allowed: false, reason: "unknown_reason" })).toBe("");
});

it("block copy explains the situation", () => {
  expect(teenBlockText("age_unverified")).toContain("年龄核验");
  expect(teenBlockText("guardian_required")).toContain("监护人");
  expect(teenBlockText("curfew", 3 * 3600)).toContain("3 小时");
  expect(teenBlockText("daily_limit")).toContain("60 分钟");
  expect(teenBlockText("persona_blocked")).toContain("限制");
});

it("reminder only fires when allowed and after cooldown", () => {
  expect(teenReminderDue({ allowed: true, reminder: false }, 0, 1000)).toBe(false);
  expect(teenReminderDue({ allowed: false, reminder: true }, 0, 1000)).toBe(false);
  expect(teenReminderDue({ allowed: true, reminder: true }, 0, 1000)).toBe(false);
  const now = 40 * 60 * 1000;
  expect(teenReminderDue({ allowed: true, reminder: true }, 0, now)).toBe(true);
  expect(teenReminderDue({ allowed: true, reminder: true }, now, now + 1000)).toBe(false);
});

it("adult age group is the only monetizable group", () => {
  expect(isAdultGroup("adult")).toBe(true);
  expect(isAdultGroup("teen")).toBe(false);
  expect(isAdultGroup(undefined)).toBe(false);
});
