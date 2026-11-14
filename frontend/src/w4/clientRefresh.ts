import type { ClientEvent } from "./v3/clientEvents";

/** Coalesce invalidations that arrive during I/O into one subsequent request. */
export class CoalescedRefresh {
  private pending?: Promise<void>;
  private dirty = false;
  private stopped = false;
  constructor(private readonly refresh: () => Promise<void>) {}
  run(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.pending) { this.dirty = true; return this.pending; }
    const task = async () => {
      do {
        this.dirty = false;
        try { await this.refresh(); } catch { /* next event or visibility change retries */ }
      } while (this.dirty && !this.stopped);
    };
    this.pending = task().finally(() => { this.pending = undefined; });
    return this.pending;
  }
  stop(): void { this.stopped = true; this.dirty = false; }
}

const THREAD_EVENTS = new Set(["message", "new_message", "audio_ready", "audio_failed"]);
const FRIEND_EVENTS = new Set(["message", "new_message", "friend_request", "friend_accepted", "friend_removed", "request_accepted", "request_declined"]);
export function eventRefreshTargets(events: ClientEvent[], activeFriend: string | null) {
  return {
    friends: events.some((event) => FRIEND_EVENTS.has(event.type)),
    thread: activeFriend && events.some((event) => event.friend_id === activeFriend && THREAD_EVENTS.has(event.type)) ? activeFriend : null,
  };
}

/** Five fallback reads over 30 seconds; events remain the primary completion signal. */
export class AudioPoller {
  private jobs = new Map<string, { deadline: number; delay: number; timer?: ReturnType<typeof setTimeout> }>();
  constructor(private readonly refresh: (friendId: string) => Promise<boolean>) {}
  start(friendId: string): void {
    const existing = this.jobs.get(friendId);
    if (existing) { existing.deadline = Date.now() + 30_000; return; }
    const job = { deadline: Date.now() + 30_000, delay: 2000, timer: undefined as ReturnType<typeof setTimeout> | undefined };
    this.jobs.set(friendId, job);
    const tick = async () => {
      if (this.jobs.get(friendId) !== job) return;
      let pending = true;
      try { pending = await this.refresh(friendId); } catch { /* bounded retry */ }
      if (this.jobs.get(friendId) !== job) return;
      if (!pending || Date.now() >= job.deadline) { this.jobs.delete(friendId); return; }
      job.delay = Math.min(8000, job.delay * 2);
      job.timer = setTimeout(tick, Math.min(job.delay, job.deadline - Date.now()));
    };
    job.timer = setTimeout(tick, job.delay);
  }
  cancel(friendId: string): void { clearTimeout(this.jobs.get(friendId)?.timer); this.jobs.delete(friendId); }
  stop(): void { for (const friendId of this.jobs.keys()) this.cancel(friendId); }
}

export function reconnectDelay(attempt: number, random = Math.random): number {
  const ceiling = Math.min(20_000, 1000 * 2 ** Math.min(attempt, 5));
  return Math.round(ceiling * (0.5 + random() * 0.5));
}
