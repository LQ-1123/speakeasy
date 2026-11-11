/**
 * 青少年门禁的纯逻辑（与服务端 `/api/v3/teen/status` 的 `access` 对齐）。
 *
 * 服务端 reason：
 * - `age_unverified`：需要完成年龄核验（未知年龄在门禁开启时只能注册/帮助）；
 * - `guardian_required`：14–17 岁需要先绑定监护人；
 * - `curfew`：22:00–06:00 停用（`retry_after_s` 为解禁秒数）；
 * - `daily_limit`：当日 60 分钟用完；
 * - `persona_blocked`：监护人限制了该人物。
 */

export type TeenReason =
  | "age_unverified"
  | "guardian_required"
  | "curfew"
  | "daily_limit"
  | "persona_blocked"
  | "";

export interface TeenAccessLike {
  allowed?: boolean;
  reason?: string;
  reminder?: boolean;
  retry_after_s?: number;
}

const BLOCKING: ReadonlySet<string> = new Set([
  "age_unverified",
  "guardian_required",
  "curfew",
  "daily_limit",
  "persona_blocked",
]);

export function teenBlockReason(access: TeenAccessLike | null | undefined): TeenReason {
  if (!access || access.allowed !== false) return "";
  const reason = String(access.reason || "");
  return BLOCKING.has(reason) ? (reason as TeenReason) : "";
}

export function teenBlockText(reason: TeenReason, retryAfterS = 0): string {
  if (reason === "age_unverified") return "请先完成年龄核验。内测阶段可联系运营协助。";
  if (reason === "guardian_required") return "14–17 岁使用前需要家长/监护人完成绑定。";
  if (reason === "curfew") {
    const hours = Math.max(1, Math.round((retryAfterS || 0) / 3600));
    return `青少年模式：现在是休息时间（22:00–06:00），约 ${hours} 小时后恢复。`;
  }
  if (reason === "daily_limit") return "今天的 60 分钟已经用完，明天再来吧。";
  if (reason === "persona_blocked") return "监护人对这个人物的访问做了限制。";
  return "";
}

/** 连续使用 30 分钟提醒（只弹一次，不强制断开）。 */
export function teenReminderDue(
  access: TeenAccessLike | null | undefined,
  lastReminderAtMs: number,
  nowMs: number,
  cooldownMs = 30 * 60 * 1000,
): boolean {
  if (!access || access.allowed !== true || !access.reminder) return false;
  return nowMs - lastReminderAtMs >= cooldownMs;
}

export function isAdultGroup(group: string | undefined): boolean {
  return group === "adult";
}
