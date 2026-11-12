/**
 * What the UI shows about the turn in flight: the spinner, the typing dots,
 * the live token bubble and the latency badge.
 *
 * Kept as one reducer because these five values move together and used to be
 * five independent `useState`s set from a dozen call sites — which is how a
 * dropped socket came to leave the spinner up forever: `onclose` simply
 * wasn't one of the sites. Every way a turn can end now goes through `reset`.
 */

export interface TurnState {
  /** Blocking overlay on the stage; also disables the composer. */
  isProcessing: boolean
  /** Typing dots in the transcript, before the first token. */
  isTyping: boolean
  statusMsg: string
  /** Tokens received so far for the reply being generated. */
  streamingContent: string
  /** Send → first video chunk, for the latency badge. */
  latencyMs: number | null
}

export const initialTurnState: TurnState = {
  isProcessing: false,
  isTyping: false,
  statusMsg: 'Almost ready…',
  streamingContent: '',
  latencyMs: null,
}

export type TurnAction =
  /** The user sent a typed message. */
  | { type: 'text-sent' }
  /** The user sent a recorded utterance (transcript not known yet). */
  | { type: 'audio-sent' }
  | { type: 'transcription' }
  | { type: 'token'; token: string }
  /** The complete assistant reply arrived. Video is still rendering. */
  | { type: 'assistant-message' }
  /** Server progress note. `playing` = a chunk is already on screen. */
  | { type: 'status'; message: string; playing: boolean }
  /** The first video chunk of the reply started playing. */
  | { type: 'first-chunk'; latencyMs: number | null }
  /** The server finished sending chunks. `playing` = something is on screen. */
  | { type: 'chunks-end'; playing: boolean }
  | { type: 'error' }
  /**
   * The turn is over without a normal ending: the user stopped it, barged in,
   * the server reported `interrupted`, or the socket dropped.
   */
  | { type: 'reset' }

export function turnReducer(state: TurnState, action: TurnAction): TurnState {
  switch (action.type) {
    case 'text-sent':
      return { ...state, isProcessing: true, isTyping: true, streamingContent: '', latencyMs: null }

    case 'audio-sent':
      return { ...state, isProcessing: true, streamingContent: '', latencyMs: null }

    case 'transcription':
      return { ...state, isTyping: true, streamingContent: '' }

    case 'token':
      return { ...state, isTyping: false, streamingContent: state.streamingContent + action.token }

    case 'assistant-message':
      // isProcessing deliberately stays as it is: the spinner holds until the
      // first video chunk, or until `chunks-end` says none is coming.
      return { ...state, isTyping: false, streamingContent: '' }

    case 'status':
      // The server sends "Animating…" before EVERY sentence. Once a chunk is
      // on screen, re-raising the overlay would cover the avatar mid-speech.
      if (action.playing) return state
      return { ...state, isProcessing: true, statusMsg: action.message || 'Processing…' }

    case 'first-chunk':
      return { ...state, isProcessing: false, latencyMs: action.latencyMs }

    case 'chunks-end':
      // Nothing ever played (every chunk failed, or there was no avatar
      // image): without this the spinner would wait for a chunk forever.
      return action.playing ? state : { ...state, isProcessing: false }

    case 'error':
      return { ...state, isProcessing: false, isTyping: false, streamingContent: '' }

    case 'reset':
      return { ...state, isProcessing: false, isTyping: false, streamingContent: '' }
  }
}


/**
 * Decides whether reply text arriving from the server belongs on screen.
 *
 * When the user stops the avatar, talks over it, or sends a new message, the
 * client acts at once — but the server keeps streaming the old reply until
 * it has received and processed that frame. Those tokens are already in
 * flight. Accepted unconditionally, they showed up as a second, split
 * assistant bubble after a Stop, or were glued onto the front of the NEXT
 * reply after a follow-up.
 *
 * So the gate closes the moment the user acts, and reopens at the first
 * frame that can only have been sent after the old turn was cancelled:
 *
 *   `interrupted`                 the server's acknowledgement — but it is
 *                                 only sent when a turn was actually running
 *   `status` (llm/transcription)  the new turn's first frame; this is what
 *   `transcription`               reopens the gate when the old turn had
 *                                 already finished and nothing was cancelled
 *   `video_chunk_start`           sent once, at the very start of a turn,
 *                                 so a late one cannot belong to the old
 *                                 reply — a second signal for the same case
 *
 * Relying on `interrupted` alone would leave the gate shut forever in that
 * second case and swallow the whole next reply. `video_chunk_start` alone
 * would not do either: it is not guaranteed to precede the new turn's first
 * tokens, whereas the `llm` status always does.
 */
export class ReplyGate {
  private open = true

  get isOpen(): boolean {
    return this.open
  }

  /** The user ended the reply in flight; what is still arriving is stale. */
  close(): void {
    this.open = false
  }

  /** A new connection carries nothing from an old turn. */
  reset(): void {
    this.open = true
  }

  /**
   * Feed every server frame, in order. Returns false for reply content
   * (`token`, `message`) that belongs to a turn the user already ended.
   */
  admit(frame: { type: string; stage?: string; final?: boolean }): boolean {
    switch (frame.type) {
      case 'transcription':
        // A running transcript (`final: false`) is the user still talking.
        // It says nothing about the old reply having been cancelled, so it
        // must not let that reply's in-flight tokens back in.
        if (frame.final === false) return true
        this.open = true
        return true
      case 'interrupted':
      case 'video_chunk_start':
        this.open = true
        return true
      case 'status':
        if (frame.stage === 'llm' || frame.stage === 'transcription') this.open = true
        return true
      case 'token':
      case 'message':
        return this.open
      default:
        return true
    }
  }
}
