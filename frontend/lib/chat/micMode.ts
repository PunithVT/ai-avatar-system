/**
 * Which hands-free implementation is in charge, with no browser in it.
 *
 * Hands-free is one switch for the user and two mechanisms underneath:
 *
 *   local   the browser detects speech from loudness, records the utterance
 *           and uploads it (useHandsFree). Always available.
 *   stream  the microphone goes up the WebRTC stream and the SERVER decides
 *           the turns. Only on a live stream, only when the server offered
 *           it (`ready.media.mic`), and only once it has confirmed that it
 *           is listening.
 *
 * Both at once would answer every sentence twice, so exactly one may hold
 * the microphone for turn-taking, and a change of hands is ordered: the one
 * letting go has let go before the other is told to start. `stream` is
 * strictly an upgrade — every failure, and every state that is not
 * confirmed `stream`, leaves the user on `local` (or briefly on neither,
 * while the microphone changes hands), never on both.
 *
 * Phases:
 *
 *   off        hands-free is off, or nothing has been started yet
 *   local      the browser detector is running
 *   releasing  the browser detector was told to stop; waiting for it to
 *              have released the recorder and the microphone
 *   acquiring  asking for the microphone and putting its track on the stream
 *   announced  the track is on the stream and the server has been told
 *              (`mic active: true`); waiting for `mic_state listening: true`
 *   stream     the server confirmed it is listening
 *
 * There is deliberately no phase in which hands-free is on and nothing is
 * listening for longer than a hand-over takes. A tab hidden for a long time
 * gives up the stream microphone, but it gives it up TO the browser
 * detector, by the same ordered hand-over as any other fallback.
 *
 * The reducer is pure — it returns the next state and the effects to run —
 * and `MicSession` runs those effects against an injected environment.
 */

export type MicMode = 'off' | 'local' | 'stream'

export type MicPhase = 'off' | 'local' | 'releasing' | 'acquiring' | 'announced' | 'stream'

/** How long the server has to acknowledge `mic active: true`. */
export const MIC_ACK_TIMEOUT_MS = 4000
/**
 * How long the tab may be hidden before the stream microphone is taken off
 * and the browser detector takes over. Long enough that glancing at another
 * tab does not change who is listening mid-conversation; short enough that
 * a tab left behind is not sending a room's audio to the server for the
 * rest of the day. With the browser detector nothing leaves the browser
 * until it has heard an utterance — which is what a hidden tab does without
 * the stream microphone, too.
 */
export const MIC_HIDDEN_RELEASE_MS = 60_000

export interface MicState {
  phase: MicPhase
  /** The user has hands-free switched on. */
  wanted: boolean
  /** The server has announced the WebRTC stream. */
  live: boolean
  /** This socket's `ready.media.mic` was true. */
  serverMic: boolean
  /** The tab has been hidden for MIC_HIDDEN_RELEASE_MS. */
  hidden: boolean
  /**
   * The stream microphone was tried on this stream and did not work, or the
   * server stopped listening unasked. Not tried again until the stream comes
   * back or the user switches hands-free off and on: without this, a server
   * that refuses would be asked again the moment the fallback settled.
   */
  blocked: boolean
  /**
   * The browser detector has been started and has not yet reported that it
   * released the microphone. Outlives the `local` phase, which is the point:
   * "told to stop" is not "stopped".
   */
  localHeld: boolean
  /**
   * `mic` frames sent whose `mic_state` has not come back. The server
   * acknowledges each in order, so with more than one outstanding the next
   * acknowledgement is for an older frame and says nothing about now.
   */
  unacked: number
}

export const initialMicState: MicState = {
  phase: 'off',
  wanted: false,
  live: false,
  serverMic: false,
  hidden: false,
  blocked: false,
  localHeld: false,
  unacked: 0,
}

export type MicEvent =
  /** The user switched hands-free on or off. */
  | { type: 'wanted'; on: boolean }
  /** A socket's first frame. `mic` = its `media.mic` was true. */
  | { type: 'ready'; mic: boolean }
  /** The WebRTC stream went live, or stopped being. */
  | { type: 'stream'; live: boolean }
  /** The server's `mic_state`. */
  | { type: 'mic-state'; listening: boolean }
  /** The microphone was granted and its track is on the stream. */
  | { type: 'track-attached' }
  /**
   * The microphone could not be had, could not be put on the stream, or its
   * track ended (the device was unplugged).
   */
  | { type: 'track-failed' }
  /** The browser detector has stopped and released the microphone. */
  | { type: 'local-released' }
  /** No `mic_state` within MIC_ACK_TIMEOUT_MS of announcing. */
  | { type: 'ack-timeout' }
  /** The tab has now been hidden for MIC_HIDDEN_RELEASE_MS. */
  | { type: 'hidden' }
  | { type: 'visible' }
  | { type: 'socket-closed' }
  /** The owner is going away: let go of everything. */
  | { type: 'shutdown' }

export type MicEffect =
  /** Run the browser detector. */
  | { type: 'start-local' }
  /** Stop it. `local-released` follows once the microphone is free. */
  | { type: 'stop-local' }
  /** getUserMedia, then put the track on the stream's audio sender. */
  | { type: 'acquire' }
  /** Take the track off the sender and stop it. Synchronous as far as the microphone goes. */
  | { type: 'detach' }
  /** Send `{type: 'mic', active}`. */
  | { type: 'send'; active: boolean }

export interface MicStep {
  state: MicState
  effects: MicEffect[]
}

/** What the user is actually on. Only a confirmed stream counts as `stream`. */
export function micModeOf(state: MicState): MicMode {
  if (state.phase === 'stream') return 'stream'
  return state.phase === 'local' ? 'local' : 'off'
}

function onStreamPath(phase: MicPhase): boolean {
  return phase === 'acquiring' || phase === 'announced' || phase === 'stream'
}

/** The server has been told to listen, so it has to be told to stop. */
function announced(phase: MicPhase): boolean {
  return phase === 'announced' || phase === 'stream'
}

type Target = 'off' | 'local' | 'stream'

function targetOf(state: MicState): Target {
  if (!state.wanted) return 'off'
  if (!state.live || !state.serverMic || state.blocked) return 'local'
  // The hidden-tab rule exists to take an open microphone off the wire, not
  // to stop hands-free: the user may well be talking to the avatar from
  // another tab. So a long-hidden tab is on the browser detector, exactly
  // as it is against a server without the stream microphone, and nothing is
  // put on the stream until the tab is back. Pausing instead left the
  // switch showing "on" with neither end listening.
  if (state.hidden) return 'local'
  return 'stream'
}

/**
 * Move from the current phase to wherever the inputs now point, leaving the
 * old holder of the microphone before entering the new one.
 *
 * `tell` = there is a server to tell: false only when the socket it was
 * announced on is gone.
 */
function settle(state: MicState, tell = true): MicStep {
  const target = targetOf(state)
  const effects: MicEffect[] = []
  let { phase, localHeld, unacked } = state

  if (onStreamPath(phase) && target !== 'stream') {
    effects.push({ type: 'detach' })
    // A server that was told to listen is always told to stop — even when
    // it has just said it stopped by itself. That `mic_state` may be about
    // an earlier frame than the one it looks like an answer to, and a
    // redundant `active: false` costs nothing, where a missing one leaves
    // both ends running a detector on the same voice.
    if (announced(phase) && tell) {
      effects.push({ type: 'send', active: false })
      unacked += 1
    }
    phase = 'off'
  }
  if (phase === 'local' && target !== 'local') {
    effects.push({ type: 'stop-local' })
    phase = 'off'
  }
  if (phase === 'releasing' && target !== 'stream') phase = 'off'

  switch (target) {
    case 'off':
      break
    case 'local':
      if (phase !== 'local') {
        effects.push({ type: 'start-local' })
        localHeld = true
        phase = 'local'
      }
      break
    case 'stream':
      if (onStreamPath(phase)) break
      if (localHeld) {
        // The detector still has the microphone and a recorder on it.
        // Nothing goes on the stream until it reports them released.
        phase = 'releasing'
      } else {
        effects.push({ type: 'acquire' })
        phase = 'acquiring'
      }
      break
  }

  return { state: { ...state, phase, localHeld, unacked }, effects }
}

export function micReduce(state: MicState, event: MicEvent): MicStep {
  const ignored: MicStep = { state, effects: [] }

  switch (event.type) {
    case 'wanted':
      if (state.wanted === event.on) return ignored
      // Switching off and on again is the user asking for another try.
      return settle({ ...state, wanted: event.on, blocked: event.on ? state.blocked : false })

    case 'ready':
      // A new socket connection: the stream that rode on the old one is
      // gone, and so is everything the old server knew about the microphone.
      return settle({ ...state, live: false, serverMic: event.mic, blocked: false, unacked: 0 }, false)

    case 'socket-closed':
      return settle({ ...state, live: false, serverMic: false, blocked: false, unacked: 0 }, false)

    case 'stream': {
      if (state.live === event.live) return ignored
      if (event.live) return settle({ ...state, live: true })
      // The stream is down. A new one is a new chance, so the block lifts;
      // and acknowledgements owed for this one are no longer waited for — if
      // they still arrive they find nothing they could change.
      const step = settle({ ...state, live: false, blocked: false })
      return { state: { ...step.state, unacked: 0 }, effects: step.effects }
    }

    case 'mic-state': {
      if (state.unacked > 1) {
        // For a frame sent before the latest one.
        return { state: { ...state, unacked: state.unacked - 1 }, effects: [] }
      }
      const next = { ...state, unacked: 0 }
      if (event.listening) {
        if (state.phase === 'announced') return { state: { ...next, phase: 'stream' }, effects: [] }
        if (state.phase === 'stream') return { state: next, effects: [] }
        // The server is listening and this side is not on the stream — a
        // late acknowledgement of an attempt already given up on. Left
        // alone, both ends would run a detector on the same voice.
        return { state: { ...next, unacked: 1 }, effects: [{ type: 'send', active: false }] }
      }
      if (!announced(state.phase)) return { state: next, effects: [] }
      // Refused, or stopped on its own.
      return settle({ ...next, blocked: true })
    }

    case 'track-attached':
      if (state.phase !== 'acquiring') return ignored
      return {
        state: { ...state, phase: 'announced', unacked: state.unacked + 1 },
        effects: [{ type: 'send', active: true }],
      }

    case 'track-failed':
      return onStreamPath(state.phase) ? settle({ ...state, blocked: true }) : ignored

    case 'local-released':
      // While `local` is the phase the detector is meant to be running, so
      // this can only be a report about an earlier stop.
      if (state.phase === 'local' || !state.localHeld) return ignored
      return settle({ ...state, localHeld: false })

    case 'ack-timeout':
      if (state.phase !== 'announced') return ignored
      // Whatever was outstanding is written off, so the count starts again
      // from the `active: false` this sends.
      return settle({ ...state, blocked: true, unacked: 0 })

    case 'hidden':
      return state.hidden ? ignored : settle({ ...state, hidden: true })

    case 'visible':
      return state.hidden ? settle({ ...state, hidden: false }) : ignored

    case 'shutdown': {
      // `localHeld` survives: the detector reports its release after this.
      const step = settle({ ...state, wanted: false, hidden: false, blocked: false })
      return { state: { ...step.state, live: false, serverMic: false, unacked: 0 }, effects: step.effects }
    }
  }
}

// ── Running the effects ────────────────────────────────────────────────────

export interface MicEnvironment {
  /** Start the browser detector. */
  startLocal(): void
  /**
   * Stop the browser detector. `localReleased()` must be called once its
   * recorder and microphone are released — and not before.
   */
  stopLocal(): void
  /**
   * Ask for the microphone and put its track on the stream. Reports
   * `trackAttached()` or `trackFailed()`; a result for an attempt that was
   * detached in the meantime must not be reported at all.
   */
  acquire(): void
  /** Take the track off the stream and stop it, cancelling an `acquire` in flight. */
  detach(): void
  /** Send `{type: 'mic', active}`. False if the socket was not open. */
  send(active: boolean): boolean
  /** The mode changed. */
  onChange(mode: MicMode): void
}

/** Drives the reducer and owns its two timers. */
export class MicSession {
  private state: MicState = initialMicState
  private ackTimer: ReturnType<typeof setTimeout> | null = null
  private hiddenTimer: ReturnType<typeof setTimeout> | null = null
  private announcedMode: MicMode = 'off'

  constructor(private readonly env: MicEnvironment) {}

  get mode(): MicMode {
    return micModeOf(this.state)
  }

  get phase(): MicPhase {
    return this.state.phase
  }

  setWanted(on: boolean): void {
    this.dispatch({ type: 'wanted', on })
  }

  /** The socket's `ready` frame. */
  ready(mic: boolean): void {
    this.dispatch({ type: 'ready', mic })
  }

  setLive(live: boolean): void {
    this.dispatch({ type: 'stream', live })
  }

  micState(listening: boolean): void {
    this.dispatch({ type: 'mic-state', listening })
  }

  trackAttached(): void {
    this.dispatch({ type: 'track-attached' })
  }

  trackFailed(): void {
    this.dispatch({ type: 'track-failed' })
  }

  localReleased(): void {
    this.dispatch({ type: 'local-released' })
  }

  socketClosed(): void {
    this.dispatch({ type: 'socket-closed' })
  }

  /**
   * The tab was hidden or shown. Hidden has to last, without a break, to
   * count; coming back cancels the clock.
   */
  setTabHidden(hidden: boolean): void {
    if (!hidden) {
      this.clearHiddenTimer()
      this.dispatch({ type: 'visible' })
      return
    }
    if (this.hiddenTimer !== null || this.state.hidden) return
    this.hiddenTimer = setTimeout(() => {
      this.hiddenTimer = null
      this.dispatch({ type: 'hidden' })
    }, MIC_HIDDEN_RELEASE_MS)
  }

  /**
   * The owner is going away: everything is let go and the server is told.
   * The session can be used again afterwards (StrictMode mounts twice).
   */
  shutdown(): void {
    this.clearHiddenTimer()
    this.dispatch({ type: 'shutdown' })
  }

  private dispatch(event: MicEvent): void {
    const step = micReduce(this.state, event)
    // State first: an effect can report back synchronously.
    this.state = step.state
    for (const effect of step.effects) this.run(effect)

    if (this.state.phase !== 'announced') this.clearAckTimer()
    else if (this.ackTimer === null) {
      this.ackTimer = setTimeout(() => {
        this.ackTimer = null
        this.dispatch({ type: 'ack-timeout' })
      }, MIC_ACK_TIMEOUT_MS)
    }

    const mode = micModeOf(this.state)
    if (mode !== this.announcedMode) {
      this.announcedMode = mode
      this.env.onChange(mode)
    }
  }

  private run(effect: MicEffect): void {
    switch (effect.type) {
      case 'start-local':
        this.env.startLocal()
        break
      case 'stop-local':
        this.env.stopLocal()
        break
      case 'acquire':
        this.env.acquire()
        break
      case 'detach':
        this.env.detach()
        break
      case 'send':
        // An announcement that did not leave (the socket closed under it)
        // is not one: without it the server never listens, and waiting out
        // the acknowledgement would leave nobody listening meanwhile.
        if (!this.env.send(effect.active) && effect.active) this.dispatch({ type: 'track-failed' })
        break
    }
  }

  private clearAckTimer(): void {
    if (this.ackTimer !== null) {
      clearTimeout(this.ackTimer)
      this.ackTimer = null
    }
  }

  private clearHiddenTimer(): void {
    if (this.hiddenTimer !== null) {
      clearTimeout(this.hiddenTimer)
      this.hiddenTimer = null
    }
  }
}
