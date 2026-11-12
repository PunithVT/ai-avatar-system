import type { MediaCapabilities, WsMessage } from '@/lib/types'

/**
 * One avatar output over two transports, with no DOM in it.
 *
 * A sentence reaches the browser either as a clip to fetch and play, or
 * already mixed into the continuous WebRTC stream. The server chooses per
 * sentence, so both can occur within one reply; this decides what each chunk
 * means for playback and answers "is the avatar talking?" for both.
 *
 * The two outputs share no clock. The stream plays on the server's, the
 * moment a sentence is queued there; a clip plays on the browser's, the
 * moment it is handed to the clip queue. What keeps a reply in order across
 * them is here: a clip that arrives while the stream is still speaking waits
 * for it (see `enqueue`). The other direction cannot be repaired in a browser
 * — a live stream cannot be delayed — so the server never moves a reply from
 * clips to the stream part-way through.
 *
 * On the stream nothing local knows when speech starts or ends — the element
 * plays the idle face and silence in between, without a pause or an `ended`.
 * The server's `speaking` events are the only source for that.
 */

/** What the rest of the chat needs to know about a `video_chunk`. */
export interface AvatarChunk {
  video_url?: string
  transport?: 'webrtc'
  /** The sentence's position in its reply. Restarts at 0 every reply. */
  chunk_index?: number
  /**
   * This sentence was announced on the stream, the stream went down before
   * it had played, and here it is again as a clip. Same `chunk_index`.
   */
  resend?: boolean
}

/**
 * Is this chunk a sentence the chat has not been told about before?
 *
 * A re-sent chunk is not: it was announced once already, on the stream, and
 * has come back by URL only because the stream died first. It has to be
 * played, but anything that counts sentences or times the first one (the
 * latency badge) must not see it as another.
 */
export function isNewSentence(chunk: AvatarChunk): boolean {
  return chunk.resend !== true
}

/**
 * Longest a clip waits for the stream to stop speaking. The wait normally
 * ends with `speaking: false`; this is for the one that never arrives, so a
 * lost event costs a pause and not the rest of the reply.
 */
export const CLIP_HOLD_MS = 10_000

/** The per-sentence clip queue (useChunkPlayer). */
export interface ClipQueue {
  /** Returns true if playback was idle and this clip started it. */
  enqueue(url: string): boolean
  stop(): void
  isActive(): boolean
}

export interface StreamView {
  /** The server has announced the stream; clips arrive on it. */
  live: boolean
  /** The stream is carrying speech the user should hear. */
  speaking: boolean
  /**
   * The user stopped the avatar while it was speaking on the stream. The
   * stream keeps carrying that speech until the server has flushed it, so it
   * is kept silent and out of sight until the server confirms.
   */
  suppressed: boolean
}

export class AvatarTransport {
  private live = false
  /** The server's view: the stream has queued speech. */
  private serverSpeaking = false
  private suppressed = false
  /** Clips waiting for the stream to stop speaking, in arrival order. */
  private held: string[] = []
  private holdTimer: ReturnType<typeof setTimeout> | null = null
  /**
   * The wait ran out with the stream still "speaking". Until that changes,
   * clips play at once: holding the next one too would stall the reply
   * another full wait for every sentence.
   */
  private holdExpired = false
  /** `chunk_index` of every clip of this reply already taken by URL. */
  private playedByUrl = new Set<number>()

  constructor(
    private clips: ClipQueue,
    private onChange: (view: StreamView) => void = () => {},
  ) {}

  /**
   * Point at a different clip queue and listener. For React, where both are
   * recreated by renders while this object has to outlive them.
   */
  bind(clips: ClipQueue, onChange: (view: StreamView) => void): void {
    this.clips = clips
    this.onChange = onChange
  }

  get view(): StreamView {
    return { live: this.live, speaking: this.serverSpeaking && !this.suppressed, suppressed: this.suppressed }
  }

  /** The negotiated transport changed (from RtcSession). */
  setLive(live: boolean): void {
    if (this.live === live) return
    this.live = live
    // `speaking` describes one stream. A stream that just went away is not
    // speaking, and a new one starts idle.
    this.serverSpeaking = false
    this.suppressed = false
    this.holdExpired = false
    // Whatever was waiting on the stream has nothing left to wait for. Played,
    // not dropped: when the stream dies these are sentences still owed.
    this.release()
    this.emit()
  }

  /** The server's `speaking` event. Meaningless without a live stream. */
  setSpeaking(active: boolean): void {
    if (!this.live) return
    if (this.serverSpeaking === active) return
    this.serverSpeaking = active
    this.holdExpired = false
    if (!active) {
      // Transitions alternate, so after a local stop the next one is the
      // stream going idle: the flush has happened.
      this.suppressed = false
      // Before the listener hears about it, so that it sees the clip queue
      // busy and keeps the stream silent under the clip.
      this.release()
    }
    this.emit()
  }

  /** The server's `video_chunk_start`: a new reply, numbered from 0 again. */
  beginReply(): void {
    this.playedByUrl.clear()
  }

  /**
   * The server's `interrupted`: it flushed the stream before sending this,
   * so the stream is idle whether or not `speaking: false` has arrived yet.
   */
  confirmStopped(): void {
    // The clips that were waiting belong to the reply that was just cut off.
    this.dropHeld()
    if (!this.serverSpeaking && !this.suppressed) return
    this.serverSpeaking = false
    this.suppressed = false
    this.holdExpired = false
    this.emit()
  }

  /**
   * A sentence is ready. Returns true when it is now playing (or, on the
   * stream, queued to) and the turn's spinner can be released.
   */
  enqueue(chunk: AvatarChunk): boolean {
    if (chunk.transport === 'webrtc') {
      // Already on the stream; there is nothing to fetch. Without a live
      // stream on this side (we fell back and the server has not caught up)
      // there is nothing to show for it yet: the server re-sends it by URL,
      // marked `resend`, once it learns the stream is gone.
      if (!this.live) return false
      // A chunk is only accepted for a turn that is still wanted, so the
      // server has long since flushed whatever a stop was waiting on.
      if (this.suppressed) {
        this.suppressed = false
        this.emit()
      }
      return true
    }
    // The server delivered this sentence the old way — always when the
    // stream is not live, for a sentence it could not put on a stream that
    // is, and (`resend`) for one that was on a stream that died before
    // playing it. Either way it plays as a clip.
    if (!chunk.video_url) return false
    if (typeof chunk.chunk_index === 'number') {
      // A sentence is played by URL once. The server does not repeat an
      // index within a reply, but a re-send racing the original would.
      if (this.playedByUrl.has(chunk.chunk_index)) return false
      this.playedByUrl.add(chunk.chunk_index)
    }
    // Not on top of the stream. A clip starts the moment it is queued, and
    // the stream cannot be paused, so starting now would cut off the
    // sentence being spoken and silence the ones queued behind it. Later
    // clips wait behind an earlier one, to keep their order.
    const streamTalking = this.live && this.serverSpeaking && !this.suppressed && !this.holdExpired
    if (streamTalking || this.held.length > 0) {
      this.held.push(chunk.video_url)
      if (this.holdTimer === null) {
        this.holdTimer = setTimeout(() => {
          this.holdTimer = null
          this.holdExpired = true
          this.release()
        }, CLIP_HOLD_MS)
      }
      // The avatar is talking — on the stream — so the spinner can go.
      return true
    }
    return this.clips.enqueue(chunk.video_url)
  }

  /** Stop now. The caller sends the interrupt; this is the local half. */
  stop(): void {
    this.dropHeld()
    this.holdExpired = false
    // Chunks are only accepted again after the next `video_chunk_start`,
    // which clears this too; doing it here keeps a stop self-contained.
    this.playedByUrl.clear()
    this.clips.stop()
    if (this.live && this.serverSpeaking && !this.suppressed) {
      this.suppressed = true
      this.emit()
    }
  }

  /** Talking, or about to — on either transport. */
  isActive(): boolean {
    return (this.serverSpeaking && !this.suppressed) || this.clips.isActive() || this.held.length > 0
  }

  /** Hand the waiting clips to the clip queue, in the order they arrived. */
  private release(): void {
    const waiting = this.dropHeld()
    for (const url of waiting) this.clips.enqueue(url)
  }

  private dropHeld(): string[] {
    if (this.holdTimer !== null) {
      clearTimeout(this.holdTimer)
      this.holdTimer = null
    }
    const waiting = this.held
    this.held = []
    return waiting
  }

  private emit(): void {
    this.onChange(this.view)
  }
}

/** Where negotiation frames go: RtcSession, or the hook in front of it. */
export interface MediaSignalling {
  ready(media: MediaCapabilities | undefined): void
  answer(sdp: string): void
  serverError(): void
  mediaTransport(transport: 'webrtc' | 'clips'): void
}

/**
 * Act on a server frame if it is about media; every frame may be passed.
 * `video_chunk` is not handled here — whether a chunk is still wanted is the
 * chat's decision (the user may have stopped the turn it belongs to).
 *
 * `interrupted` may arrive with no turn in flight (the server cut off speech
 * that was still playing on the stream after its turn had finished), and
 * `speaking: false` may follow it rather than precede it. Both orders leave
 * the stream idle and audible for the next reply.
 */
export function routeMediaFrame(message: WsMessage, rtc: MediaSignalling, transport: AvatarTransport): void {
  switch (message.type) {
    case 'ready':
      rtc.ready(message.media)
      break
    case 'webrtc_answer':
      rtc.answer(message.sdp)
      break
    case 'webrtc_error':
      rtc.serverError()
      break
    case 'media_transport':
      rtc.mediaTransport(message.transport)
      break
    case 'video_chunk_start':
      transport.beginReply()
      break
    case 'speaking':
      // Strictly `true`: a malformed frame must not leave the avatar "talking".
      transport.setSpeaking(message.active === true)
      break
    case 'interrupted':
      transport.confirmStopped()
      break
  }
}
