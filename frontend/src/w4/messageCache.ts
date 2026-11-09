import type { Message } from "./model";

/** Merge a current-page refresh without discarding older pages or other threads. */
export function mergeMessages(current: Message[], incoming: Message[], removeIds: string[] = []): Message[] {
  const removed = new Set(removeIds);
  const byId = new Map(current.filter((item) => !removed.has(item.id)).map((item) => [item.id, item]));
  for (const item of incoming) byId.set(item.id, { ...byId.get(item.id), ...item });
  return [...byId.values()].sort((a, b) => a.time.localeCompare(b.time) || a.id.localeCompare(b.id));
}
