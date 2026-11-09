import { describe, expect, it } from "vitest";
import { OrderedPlayback } from "./orderedPlayback";

describe("OrderedPlayback", () => {
  it("announces idle only after every queued segment actually completes", async () => {
    const completions: Array<() => void> = [];
    let idle = 0;
    const queue = new OrderedPlayback<number>(() => new Promise<void>((resolve) => completions.push(resolve)), () => { idle += 1; });
    queue.enqueue(0, 0);
    queue.enqueue(1, 1);
    expect(queue.isBusy).toBe(true);
    completions[0]();
    await Promise.resolve();
    expect(idle).toBe(0);
    completions[1]();
    await Promise.resolve();
    expect(idle).toBe(1);
    expect(queue.isBusy).toBe(false);
  });
  it("waits for missing indexes and plays in order", async () => {
    const played: number[] = [];
    const queue = new OrderedPlayback<number>(async (_item, index) => { played.push(index); });
    queue.enqueue(1, 1);
    queue.enqueue(0, 0);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(played).toEqual([0, 1]);
  });

  it("discards pending old turn audio after invalidation", async () => {
    const played: number[] = [];
    let finish: () => void = () => undefined;
    const queue = new OrderedPlayback<number>(async (_item, index) => {
      played.push(index);
      await new Promise<void>((resolve) => { finish = resolve; });
    });
    queue.enqueue(0, 0);
    queue.enqueue(1, 1);
    queue.invalidate();
    finish();
    queue.enqueue(0, 2);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(played).toEqual([0, 0]);
  });
});
