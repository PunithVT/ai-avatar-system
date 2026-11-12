/**
 * Ordering logic for the avatar's video chunks, with no DOM in it.
 *
 * The server sends one clip per sentence; they must play in order, and a clip
 * that fails must be skipped exactly once. That second part is what the
 * tokens are for. A media element reports one failure through several
 * channels — an `error` event AND a rejected `play()` promise — and each used
 * to advance the queue, so one bad clip skipped the good one after it.
 *
 * Every clip handed out carries a token. A signal about a clip is only acted
 * on while that clip is still the active one; anything else is stale and
 * ignored.
 */

export interface Clip {
  url: string
  token: number
}

export class ChunkSequencer {
  private queue: string[] = []
  private nextToken = 1
  private active: number | null = null

  /** A clip is on screen (or loading to be). */
  get isPlaying(): boolean {
    return this.active !== null
  }

  /** Token of the clip on screen, or null when idle. */
  get activeToken(): number | null {
    return this.active
  }

  get pending(): number {
    return this.queue.length
  }

  /** Playing or about to: used for "is the avatar talking?" decisions. */
  get hasWork(): boolean {
    return this.active !== null || this.queue.length > 0
  }

  /** The clip that will play after the current one, for preloading. */
  peek(): string | undefined {
    return this.queue[0]
  }

  /**
   * Add a clip. Returns it (with a token) if playback was idle and it should
   * start now; null if it was queued behind the one playing.
   */
  enqueue(url: string): Clip | null {
    this.queue.push(url)
    if (this.active !== null) return null
    return this.take()
  }

  /**
   * The clip with this token finished or failed. Returns the next clip to
   * start, 'idle' if the queue is drained, or null if the signal was stale
   * (about a clip that is no longer active) and must be ignored.
   */
  advance(token: number): Clip | 'idle' | null {
    if (token !== this.active) return null
    this.active = null
    return this.take() ?? 'idle'
  }

  /** Drop everything. Signals about the clip that was playing become stale. */
  stop(): void {
    this.queue = []
    this.active = null
  }

  private take(): Clip | null {
    const url = this.queue.shift()
    if (url === undefined) return null
    const clip = { url, token: this.nextToken++ }
    this.active = clip.token
    return clip
  }
}
