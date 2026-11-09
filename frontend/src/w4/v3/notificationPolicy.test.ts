import { expect, it } from "vitest";
import { notificationFor } from "./notificationPolicy";
const preferences = { messages: true, interactions: true, sound: true, theme: "system" as const };
const event = { id: "message:1", sequence: 1, type: "message", friend_id: "friend" };
it("shows cross-page messages while suppressing the active conversation and read replays", () => {
  expect(notificationFor([event], preferences, { name: "me" }, false)).toMatchObject({ route: "#/chat/friend", sound: true });
  expect(notificationFor([event], preferences, { name: "chat", friendId: "friend" }, false)).toBeNull();
  expect(notificationFor([{ ...event, replay: true }], preferences, { name: "me" }, false)).toBeNull();
  expect(notificationFor([{ ...event, is_read: true }], preferences, { name: "me" }, false)).toBeNull();
});
it("obeys notification preferences and silences sounds during a call", () => {
  expect(notificationFor([event], { ...preferences, messages: false }, { name: "me" }, false)).toBeNull();
  expect(notificationFor([event], preferences, { name: "me" }, true)?.sound).toBe(false);
});
