import { afterEach, expect, it, vi } from "vitest";
import { fetchMessages, requestMessageSuggestion } from "./api";

afterEach(() => vi.unstubAllGlobals());
it("maps failed audio as terminal while queued/generating retain pending state", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ messages: ["queued", "generating", "failed", "ready"].map((status) => ({ id: status, friend_id: "f", sender: "friend", msg_type: "voice", transcript: "Hello", audio_status: status, audio_error: status === "failed" ? "deadline_exceeded" : "", audio_url: status === "ready" ? "/api/v2/audio/a" : "" })) }))));
  const messages = (await fetchMessages("f"))!;
  expect(messages.map((message) => message.audioStatus)).toEqual(["queued", "generating", "failed", "ready"]);
  expect(messages.map((message) => message.audioPending)).toEqual([true, true, false, false]);
  expect(messages[2].audioError).toBe("deadline_exceeded");
});
it("requests polishing only on explicit POST and maps the returned message", async () => {
  const fetch = vi.fn(async () => new Response(JSON.stringify({ message: { id: "m", sender: "user", friend_id: "f", msg_type: "text", transcript: "I like read", suggestion: { better: "I like reading", why: "Use a gerund" } } })));
  vi.stubGlobal("fetch", fetch);
  const result = await requestMessageSuggestion("m", "f");
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/v2/messages/m/suggestion"), expect.objectContaining({ method: "POST" }));
  expect(result?.suggestion?.better).toBe("I like reading");
});
