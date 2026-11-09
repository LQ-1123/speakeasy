import type { Route } from "../model";
import type { ClientEvent } from "./clientEvents";
import type { ClientPreferences } from "./clientApi";

export function notificationFor(events: ClientEvent[], preferences: ClientPreferences, route: Route, inCall: boolean): { event: ClientEvent; route: string; sound: boolean } | null {
  const event = [...events].reverse().find((item) => {
    if (item.is_read || item.replay) return false;
    if (item.type === "message") return preferences.messages && !(route.name === "chat" && route.friendId === item.friend_id);
    return preferences.interactions;
  });
  if (!event) return null;
  return { event, route: event.type === "message" && event.friend_id ? `#/chat/${encodeURIComponent(event.friend_id)}` : event.type === "friend_request" ? "#/requests" : "#/events", sound: preferences.sound && !inCall };
}
