/**
 * The user's utterance while the SERVER is doing the listening (the stream
 * microphone): is the user speaking, and what has been heard so far.
 *
 * With the browser detector the client knows all of this itself — it made
 * the recording. On the stream microphone it only knows what the server
 * reports: `user_speech` for the boundaries, and `transcription` frames with
 * `final: false` for a running transcript.
 *
 * The running transcript is provisional. It is shown as a pending bubble
 * that is NOT a message: it has no id, is not counted, exported or
 * reconciled, and each one replaces the one before. The message is created
 * once, by the final `transcription`, exactly as it is for an uploaded
 * recording — so nothing here can produce a second bubble for one utterance.
 */

export interface SpeechState {
  /** Between `started` and `stopped`: the user is talking. */
  speaking: boolean
  /** The running transcript of the utterance in progress, if any. */
  pending: string | null
  /**
   * An utterance is open: started, and neither finished by its final
   * transcript nor dropped. Running transcripts are only taken while it is,
   * so one that arrives late cannot bring a bubble back.
   */
  open: boolean
  /** The server said `stopped, turn`: its final transcript is on the way. */
  awaitingFinal: boolean
}

export const initialSpeechState: SpeechState = { speaking: false, pending: null, open: false, awaitingFinal: false }

export type SpeechEvent =
  | { type: 'started' }
  /** `turn` = being transcribed and answered; anything else = dropped. */
  | { type: 'stopped'; reason: 'turn' | 'ignored' | undefined }
  /** A `transcription` with `final: false`. */
  | { type: 'partial'; text: string }
  /** The final `transcription` (the caller creates the message). */
  | { type: 'final' }
  /** The utterance cannot complete: socket dropped, the stream microphone went away, the turn failed. */
  | { type: 'reset' }

/** Is this `transcription` frame a running transcript rather than the finished one? */
export function isPartialTranscription(frame: { final?: boolean }): boolean {
  // Strictly false. A frame without the key is the final transcript — the
  // only kind a server without the stream microphone ever sends.
  return frame.final === false
}

export function speechReduce(state: SpeechState, event: SpeechEvent): SpeechState {
  switch (event.type) {
    case 'started':
      return { speaking: true, pending: null, open: true, awaitingFinal: false }

    case 'partial': {
      if (!state.open) return state
      const pending = event.text.trim() ? event.text : null
      return pending === state.pending ? state : { ...state, pending }
    }

    case 'stopped':
      // The pending bubble stays for a turn: the final transcript replaces
      // it, and removing it in between would make the text blink.
      if (event.reason === 'turn') return { ...state, speaking: false, awaitingFinal: true }
      return initialSpeechState

    case 'final':
    case 'reset':
      return state === initialSpeechState ? state : initialSpeechState
  }
}

// ── The turn an utterance becomes ──────────────────────────────────────────

/**
 * Everything that bears on a microphone turn between the server taking the
 * utterance (`stopped, turn` — the spinner goes up, the pending bubble
 * stays) and its final transcript. `pending` below is exactly that window.
 *
 * It is one function because that window has many ways out and each one
 * used to be a separate call site: the two that were missed (Stop, and an
 * `ignored` arriving after the mode had changed) left a spinner or a bubble
 * with nothing coming to clear it.
 */
export type SpeechTurnEvent =
  /** A `user_speech` frame. `stream` = the stream microphone is the mode right now. */
  | { type: 'user-speech'; state: 'started' | 'stopped'; reason?: 'turn' | 'ignored'; stream: boolean }
  /** The final `transcription`. */
  | { type: 'final' }
  /** An `error` frame. */
  | { type: 'error' }
  /** The server's `interrupted`. */
  | { type: 'interrupted' }
  /** The user pressed Stop. */
  | { type: 'stop' }
  /** The user sent a turn of their own: typed, or a recording uploaded. */
  | { type: 'turn-sent' }
  /** The stream microphone is no longer the mode: fallback, hands-free off, a long-hidden tab. */
  | { type: 'mic-left' }
  | { type: 'socket-dropped' }

export interface SpeechTurnStep {
  /** A microphone turn is waiting for its final transcript. */
  pending: boolean
  /** What to feed `speechReduce`, if anything. */
  speech: SpeechEvent | null
  /**
   * What to do to the turn on screen:
   *   barge-in  speech was confirmed: end a reply that is running or playing
   *   begin     the utterance is with the server: spinner up, as for an upload
   *   abandon   the microphone turn will not happen: spinner down
   */
  turn: 'barge-in' | 'begin' | 'abandon' | null
}

const RESET: SpeechEvent = { type: 'reset' }

export function speechTurnStep(pending: boolean, event: SpeechTurnEvent): SpeechTurnStep {
  const unchanged: SpeechTurnStep = { pending, speech: null, turn: null }
  /** The pending turn is gone, and with it the bubble and the spinner it put up. */
  const dropped: SpeechTurnStep = { pending: false, speech: RESET, turn: 'abandon' }

  switch (event.type) {
    case 'user-speech':
      if (event.state === 'stopped' && event.reason !== 'turn') {
        // In EVERY mode. This is the one frame that undoes a turn already
        // begun, and the server sends it for a turn it had accepted before
        // the microphone went away — so by the time it arrives the mode may
        // well have changed, and dropping it there left the spinner up and
        // the composer disabled with nothing else on its way.
        return { pending: false, speech: { type: 'stopped', reason: 'ignored' }, turn: pending ? 'abandon' : null }
      }
      // Only the stream microphone is reported this way. A `started` or a
      // `stopped, turn` that arrives after it was given up belongs to an
      // utterance the server has discarded.
      if (!event.stream) return unchanged
      if (event.state === 'started') {
        // Speech over a turn still waiting for its transcript: the server
        // cancels that turn (and keeps its words for this utterance), so it
        // is no longer pending — the `interrupted` that follows must not be
        // taken for news about the utterance that has just begun.
        return { pending: false, speech: { type: 'started' }, turn: 'barge-in' }
      }
      return { pending: true, speech: { type: 'stopped', reason: 'turn' }, turn: 'begin' }

    case 'final':
      // The message replaces the bubble; the turn carries on.
      return { pending: false, speech: { type: 'final' }, turn: null }

    case 'turn-sent':
      // The server cancels the microphone turn for the new one, without a
      // word about it. The bubble goes; the spinner now belongs to the new
      // turn, and a late `ignored` or `interrupted` must not take it down.
      return pending ? { pending: false, speech: RESET, turn: null } : unchanged

    case 'error':
    case 'interrupted':
    case 'stop':
      // A cancelled microphone turn sends neither a transcript nor
      // `ignored`, so these are the last that will be heard of it. Without a
      // pending turn they are not about the microphone at all — and an
      // utterance still being spoken is left alone: its own `stopped` comes.
      return pending ? dropped : unchanged

    case 'mic-left':
    case 'socket-dropped':
      // The server discards what it was hearing, and cancels a turn whose
      // transcript it has not sent. If that transcript was already on its
      // way it still becomes the message, and the server's next `status`
      // puts the spinner back.
      return { pending: false, speech: RESET, turn: pending ? 'abandon' : null }
  }
}
