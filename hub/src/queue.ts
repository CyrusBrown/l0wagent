/** An unbounded FIFO with an awaitable `shift` (the tunnel's per-stream body queue). */
export class AsyncQueue<T> {
  private items: T[] = [];
  private waiters: Array<(v: T) => void> = [];

  push(item: T): void {
    const w = this.waiters.shift();
    if (w) w(item);
    else this.items.push(item);
  }

  shift(): Promise<T> {
    if (this.items.length) return Promise.resolve(this.items.shift() as T);
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  get size(): number {
    return this.items.length;
  }
}

/** A per-member sliding window: at most `limit` hits in any 60 seconds. */
export class SlidingWindow {
  constructor(public hits: number[] = []) {}

  /** Record a hit at `now` (ms). False, without recording, when the limit is reached. */
  allow(limit: number, now: number, windowMs = 60_000): boolean {
    while (this.hits.length && now - this.hits[0] > windowMs) this.hits.shift();
    if (this.hits.length >= limit) return false;
    this.hits.push(now);
    return true;
  }
}
