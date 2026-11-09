import { afterEach, expect, it, vi } from "vitest";
import { EventBatch } from "./eventBatch";

afterEach(() => vi.useRealTimers());
it("coalesces 1000 replay events into one refresh batch and deduplicates repeated ids", () => {
  vi.useFakeTimers();
  const flush = vi.fn();
  const batch = new EventBatch(flush);
  for (let index = 0; index < 1000; index += 1) batch.add({ id: `event-${index % 100}`, sequence: index, type: "message", friend_id: "active" });
  expect(flush).not.toHaveBeenCalled();
  vi.advanceTimersByTime(300);
  expect(flush).toHaveBeenCalledOnce();
  expect(flush.mock.calls[0][0]).toHaveLength(100);
  batch.stop();
});
