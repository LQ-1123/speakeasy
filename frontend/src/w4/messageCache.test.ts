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
