import type { ClientEvent } from "./clientEvents";

export class EventBatch {
  private pending = new Map<string, ClientEvent>();
  private timer?: ReturnType<typeof setTimeout>;
  constructor(private readonly flush: (events: ClientEvent[]) => void) {}
  add(event: ClientEvent): void {
    this.pending.set(event.id, event);
    if (!this.timer) this.timer = setTimeout(() => {
      this.timer = undefined;
      const events = [...this.pending.values()];
      this.pending.clear();
      this.flush(events);
    }, 300);
  }
  stop(): void { clearTimeout(this.timer); this.timer = undefined; this.pending.clear(); }
}
