import { afterEach, expect, it, vi } from "vitest";
import { AudioPoller, CoalescedRefresh, eventRefreshTargets, reconnectDelay } from "./clientRefresh";

afterEach(() => vi.useRealTimers());

it("coalesces an event burst during an active request into one follow-up", async () => {
  let finish: (() => void) | undefined;
  const refresh = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  const coalesced = new CoalescedRefresh(refresh);
  const pending = coalesced.run();
  for (let i = 0; i < 50; i += 1) void coalesced.run();
  expect(refresh).toHaveBeenCalledOnce();
  finish!();
  await Promise.resolve();
  expect(refresh).toHaveBeenCalledTimes(2);
  finish!();
  await pending;
  coalesced.stop();
});

it("refreshes only the matching active thread for audio events", () => {
  const audio = { id: "1", sequence: 1, type: "audio_ready", friend_id: "a", payload: { message_id: "m" } };
  expect(eventRefreshTargets([audio], "a")).toEqual({ friends: false, thread: "a" });
  expect(eventRefreshTargets([audio], "b")).toEqual({ friends: false, thread: null });
  expect(eventRefreshTargets([{ ...audio, type: "message" }], "a")).toEqual({ friends: true, thread: "a" });
  expect(eventRefreshTargets([{ ...audio, type: "post_comment" }], "a")).toEqual({ friends: false, thread: null });
});

it("backs off audio fallback polls and stops at thirty seconds or a terminal status", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn(async () => true);
  const poller = new AudioPoller(fetch);
  poller.start("f");
  await vi.advanceTimersByTimeAsync(30_000);
  expect(fetch).toHaveBeenCalledTimes(5);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(fetch).toHaveBeenCalledTimes(5);
  const terminal = vi.fn(async () => false);
  const done = new AudioPoller(terminal);
  done.start("f");
  await vi.advanceTimersByTimeAsync(30_000);
  expect(terminal).toHaveBeenCalledOnce();
  poller.stop(); done.stop();
});

it("jitters reconnect delays within a capped window", () => {
  expect(reconnectDelay(0, () => 0)).toBe(500);
  expect(reconnectDelay(0, () => 1)).toBe(1000);
  expect(reconnectDelay(100, () => 1)).toBe(20_000);
});
