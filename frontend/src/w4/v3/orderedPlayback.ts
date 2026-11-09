export class OrderedPlayback<T> {
  private pending = new Map<number, T>();
  private nextIndex = 0;
  private playing = false;
  private generation = 0;
  private active = false;

  constructor(private readonly play: (item: T, index: number) => Promise<void>, private readonly onIdle?: () => void) {}

  get isBusy(): boolean { return this.active || this.pending.size > 0; }

  enqueue(index: number, item: T): void {
    if (index < this.nextIndex) return;
    this.pending.set(index, item);
    void this.drain();
  }

  invalidate(): void {
    this.generation += 1;
    this.pending.clear();
    this.nextIndex = 0;
    this.playing = false;
    this.active = false;
  }

  private async drain(): Promise<void> {
    if (this.playing) return;
    this.playing = true;
    const generation = this.generation;
    try {
      while (generation === this.generation && this.pending.has(this.nextIndex)) {
        const index = this.nextIndex;
        const item = this.pending.get(index)!;
        this.pending.delete(index);
        this.active = true;
        await this.play(item, index);
        this.active = false;
        if (generation !== this.generation) return;
        this.nextIndex += 1;
      }
    } finally {
      if (generation === this.generation) { this.playing = false; this.active = false; if (!this.pending.size) this.onIdle?.(); }
    }
  }
}
