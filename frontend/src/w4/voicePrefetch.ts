import type { Message } from "./model";

/** Keep speculative work small even if a caller accidentally passes a full history. */
export function selectVoicePrefetch(messages: Message[], visible: number[]): Message[] {
  const indices = new Set([...visible.slice(0, 12), ...Array.from({ length: Math.min(4, messages.length) }, (_, i) => Math.max(0, messages.length - 4) + i)].filter((i) => i >= 0));
  return [...indices].sort((a, b) => a - b).map((i) => messages[i]).filter((m) => m?.kind === "voice" && Boolean(m.audio));
}

type Work = { url: string; generation: number; resolve: (value: string | null) => void; controller: AbortController };

/** Memory-only private audio. Never share blobs or pending tasks between sessions. */
export class VoicePrefetchCache {
  private owner = "";
  private generation = 0;
  private cached = new Map<string, string>();
  private inflight = new Map<string, Promise<string | null>>();
  private failed = new Map<string, number>();
  private queue: Work[] = [];
  private active = new Set<Work>();

  constructor(private readonly download: (url: string, signal: AbortSignal) => Promise<Blob>) {}

  clear(): void {
    this.generation += 1;
    for (const local of this.cached.values()) URL.revokeObjectURL(local);
    this.cached.clear(); this.failed.clear(); this.inflight.clear();
    for (const task of this.queue.splice(0)) task.resolve(null);
    for (const task of this.active) task.controller.abort();
  }

  private scope(owner: string): void {
    if (this.owner === owner) return;
    this.clear(); this.owner = owner;
  }

  get(url: string, owner: string): string | null {
    this.scope(owner);
    return this.cached.get(url) ?? null;
  }

  forget(url: string): void {
    const local = this.cached.get(url);
    this.cached.delete(url); this.failed.delete(url);
    if (local) URL.revokeObjectURL(local);
  }

  prefetch(url: string, owner: string): Promise<string | null> {
    this.scope(owner);
    if (!url || !owner) return Promise.resolve(null);
    const cached = this.cached.get(url);
    if (cached) return Promise.resolve(cached);
    if ((this.failed.get(url) ?? 0) > Date.now()) return Promise.resolve(null);
    const running = this.inflight.get(url);
    if (running) return running;
    if (this.queue.length >= 16) return Promise.resolve(null);
    let resolve!: (value: string | null) => void;
    const promise = new Promise<string | null>((done) => { resolve = done; });
    this.inflight.set(url, promise);
    this.queue.push({ url, generation: this.generation, resolve, controller: new AbortController() });
    this.pump();
    return promise;
  }

  private pump(): void {
    while (this.active.size < 2 && this.queue.length) {
      const task = this.queue.shift()!;
      this.active.add(task);
      void this.run(task);
    }
  }

  private async run(task: Work): Promise<void> {
    let local: string | null = null;
    const timeout = setTimeout(() => task.controller.abort(), 15_000);
    try {
      const blob = await this.download(task.url, task.controller.signal);
      if (task.generation !== this.generation || task.controller.signal.aborted) return;
      if (!blob.size || !blob.type.startsWith("audio/")) throw new Error("Invalid audio");
      local = URL.createObjectURL(blob);
      if (this.cached.size >= 24) this.forget(this.cached.keys().next().value!);
      this.cached.set(task.url, local);
      this.failed.delete(task.url);
    } catch {
      if (task.generation === this.generation) {
        if (this.failed.size >= 24) this.failed.delete(this.failed.keys().next().value!);
        this.failed.set(task.url, Date.now() + 15_000);
      }
    } finally {
      clearTimeout(timeout);
      this.active.delete(task);
      if (task.generation === this.generation) this.inflight.delete(task.url);
      task.resolve(local);
      this.pump();
    }
  }
}
