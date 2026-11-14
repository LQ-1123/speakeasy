import { afterEach, expect, it, vi } from "vitest";
import { VoicePrefetchCache, selectVoicePrefetch } from "./voicePrefetch";
import type { Message } from "./model";

afterEach(() => vi.unstubAllGlobals());
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

it("downloads at most two voices, sharing queued and active requests", async () => {
  const finishes: (() => void)[] = [];
  const download = vi.fn(() => new Promise<Blob>((resolve) => finishes.push(() => resolve(new Blob(["audio"], { type: "audio/mp3" })))));
  const cache = new VoicePrefetchCache(download);
  const first = cache.prefetch("a", "owner");
  const duplicate = cache.prefetch("a", "owner");
  const second = cache.prefetch("b", "owner");
  const queued = cache.prefetch("c", "owner");
  expect(download).toHaveBeenCalledTimes(2);
  finishes.shift()!();
  await settle();
  expect(download).toHaveBeenCalledTimes(3);
  finishes.splice(0).forEach((finish) => finish());
  expect(await first).toBe(await duplicate);
  expect(await second).toBeTruthy();
  expect(await queued).toBeTruthy();
});

it("discards late downloads and queued work when the owner/session changes", async () => {
  const finishes: (() => void)[] = [];
  const download = vi.fn(() => new Promise<Blob>((resolve) => finishes.push(() => resolve(new Blob(["audio"], { type: "audio/mp3" })))));
  const cache = new VoicePrefetchCache(download);
  const old = cache.prefetch("same", "user:session1");
  const other = cache.prefetch("other", "user:session1");
  const queued = cache.prefetch("queued", "user:session1");
  expect(cache.get("same", "user:session2")).toBeNull();
  expect(await queued).toBeNull();
  finishes.splice(0).forEach((finish) => finish());
  expect(await old).toBeNull();
  expect(await other).toBeNull();
  expect(download).toHaveBeenCalledTimes(2);
  expect(cache.get("same", "user:session2")).toBeNull();
});

it("bounds queued downloads even when callers submit an entire history", async () => {
  const cache = new VoicePrefetchCache(() => new Promise(() => undefined));
  const work = Array.from({ length: 1000 }, (_, index) => cache.prefetch(String(index), "owner"));
  expect(await work[999]).toBeNull();
  cache.clear();
});

it("prefetches only latest and visible voices in a large conversation", () => {
  const messages = Array.from({ length: 1000 }, (_, index): Message => ({ id: String(index), friendId: "f", from: "friend", kind: "voice", text: "", time: "", audio: `/audio/${index}` }));
  const chosen = selectVoicePrefetch(messages, [5, 6, 7]);
  expect(chosen.map((message) => message.id)).toEqual(["5", "6", "7", "996", "997", "998", "999"]);
  expect(selectVoicePrefetch(messages, [])).toHaveLength(4);
});
