import { expect, it } from "vitest";
import { mergeMessages } from "./messageCache";
import type { Message } from "./model";

const message = (id: string, time: string): Message => ({ id, time, friendId: "friend", from: "friend", kind: "text", text: id });
it("refreshes recent messages without losing earlier pages and replaces optimistic sends", () => {
  const current = [message("old", "2026-01-01"), message("recent", "2026-01-02"), message("pending", "2026-01-03")];
  const merged = mergeMessages(current, [{ ...message("recent", "2026-01-02"), read: true }, message("sent", "2026-01-03")], ["pending"]);
  expect(merged.map((item) => item.id)).toEqual(["old", "recent", "sent"]);
  expect(merged[1].read).toBe(true);
});

it("keeps a confirmed voice bubble while the deferred audio is still missing", () => {
  const voice = { ...message("m1", "2026-01-01"), kind: "voice" as const, durationMs: 3200, audio: undefined };
  const refreshed = { ...message("m1", "2026-01-01"), kind: "text" as const, durationMs: 0 };
  const merged = mergeMessages([voice], [refreshed]);
  expect(merged[0].kind).toBe("voice");
  expect(merged[0].durationMs).toBe(3200);
});

it("clears pending audio when generation fails instead of preserving a permanent spinner", () => {
  const pending = { ...message("m1", "2026-01-01"), kind: "voice" as const, audioPending: true, audioStatus: "generating" as const };
  const failed = { ...pending, audioStatus: "failed" as const, audioPending: false, audioError: "deadline_exceeded" };
  expect(mergeMessages([pending], [failed])[0].audioPending).toBe(false);
});
