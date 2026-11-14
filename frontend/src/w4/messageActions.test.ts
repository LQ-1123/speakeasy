import { expect, it } from "vitest";
import { canRequestSuggestion } from "./messageActions";
import type { Message } from "./model";

const message: Message = { id: "confirmed", friendId: "f", from: "me", kind: "text", text: "I like read", time: "" };
it("offers explicit polishing for confirmed user text or voice without a suggestion", () => {
  expect(canRequestSuggestion(message)).toBe(true);
  expect(canRequestSuggestion({ ...message, kind: "voice" })).toBe(true);
  for (const change of [{ from: "friend" }, { kind: "ad" }, { kind: "system" }, { kind: "call" }, { failed: true }, { text: "" }, { id: "pending-1", clientId: "c" }, { suggestion: { better: "I like reading", why: "", tag: "自然", saved: false } }] as Partial<Message>[]) {
    expect(canRequestSuggestion({ ...message, ...change })).toBe(false);
  }
});
