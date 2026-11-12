import type { IceServer, MediaCapabilities, RtcSignal } from '@/lib/types'

/**
 * Negotiating the WebRTC media transport, with no browser in it.
 *
 * The avatar can arrive two ways: a clip per sentence (always available) or
 * one continuous stream (opt-in on the server). The stream is strictly an
 * upgrade. Clips are what the server sends until it has announced otherwise,
 * so every state here other than `live` means "play clips", and every failure
 * lands there without anything having to be undone.
 *
 *   off          nothing negotiated: the server did not offer WebRTC, the
 *                browser cannot do it, or the socket is down
 *   offering     a peer connection exists; the offer is being built or is
 *                waiting for its answer
 *   answered     the answer is applied; waiting for the server to say its
 *                side is connected
 *   live         the server announced `media_transport: webrtc`
 *   fallen-back  WebRTC was tried on this socket and given up on
 *
 * The reducer is pure — it returns the next state and the effects to run —
 * and `RtcSession` runs those effects against an injected peer connection.
 */

export type RtcPhase = 'off' | 'offering' | 'answered' | 'live' | 'fallen-back'

/**
 * Offers per socket connection: the first, plus one retry. The retry covers a
 * transient failure (a `webrtc_error`, an ICE failure after a network change)
 * without letting a server that can never connect be asked forever.
 */
export const MAX_RTC_OFFERS = 2
/** Longest wait for ICE candidates before sending the offer with what there is. */
export const ICE_GATHER_CAP_MS = 3000
/**
 * How long a negotiation may take, from creating the peer connection to the
 * server announcing the stream, before it is abandoned.
 */
export const RTC_LIVE_TIMEOUT_MS = 20_000
/**
 * How long the peer connection may report `disconnected` before it is given
 * up on. The browser says `disconnected` a few seconds after packets stop and
 * often recovers by itself; `failed` only follows some 30 s later, and so
 * does the server's own check. For all of that time the server would keep
 * putting sentences on a stream nobody receives, so we do not wait for it.
 */
export const RTC_DISCONNECT_GRACE_MS = 3000

export interface RtcState {
  phase: RtcPhase
  /** Offers started on this socket connection. */
  offers: number
  /** The current offer has reached the server, so an answer may be for it. */
  offerSent: boolean
  iceServers: IceServer[]
}

export const initialRtcState: RtcState = { phase: 'off', offers: 0, offerSent: false, iceServers: [] }

export type RtcEvent =
  /** The socket's first frame. `supported` = this browser has RTCPeerConnection. */
  | { type: 'ready'; media: MediaCapabilities | undefined; supported: boolean }
  | { type: 'offer-sent' }
  /** The offer could not be created or sent. */
  | { type: 'offer-failed' }
  | { type: 'answer'; sdp: string }
  /** The browser rejected the answer. */
  | { type: 'answer-failed' }
  | { type: 'webrtc-error' }
  | { type: 'media-transport'; transport: 'webrtc' | 'clips' }
  /** RTCPeerConnection.connectionState changed. */
  | { type: 'peer-state'; state: string }
  /** The peer connection stayed `disconnected` for RTC_DISCONNECT_GRACE_MS. */
  | { type: 'peer-lost' }
  /** The negotiation did not reach `live` in time. */
  | { type: 'timeout' }
  | { type: 'socket-closed' }

export type RtcEffect =
  /** Create a peer connection and send an offer. */
  | { type: 'open'; iceServers: IceServer[] }
  | { type: 'apply-answer'; sdp: string }
  /** Tear the peer connection down. `notify` = tell the server first. */
  | { type: 'close'; notify: boolean }

export interface RtcStep {
  state: RtcState
  effects: RtcEffect[]
}

/** Which way clips are arriving. Only the server's announcement makes it 'webrtc'. */
export function transportOf(state: RtcState): 'webrtc' | 'clips' {
  return state.phase === 'live' ? 'webrtc' : 'clips'
}

function hasPeer(state: RtcState): boolean {
  return state.phase === 'offering' || state.phase === 'answered' || state.phase === 'live'
}

/** Drop entries a peer connection would throw on, rather than lose the stream to one. */
function usableIceServers(servers: unknown): IceServer[] {
  if (!Array.isArray(servers)) return []
  return servers.filter((s): s is IceServer => {
    const urls = (s as IceServer | null)?.urls
    return typeof urls === 'string' || (Array.isArray(urls) && urls.every((u) => typeof u === 'string'))
  })
}

/** The current attempt failed: retry once per socket, otherwise settle on clips. */
function fail(state: RtcState): RtcStep {
  if (state.offers < MAX_RTC_OFFERS) {
    return {
      state: { ...state, phase: 'offering', offers: state.offers + 1, offerSent: false },
      // Tell the server now, although the new offer would replace the old
      // peer connection there anyway. That offer only leaves after ICE
      // gathering (up to ICE_GATHER_CAP_MS), and until the server hears
      // otherwise it keeps putting sentences on the stream we just closed.
      // `webrtc_close` makes it send those — and the ones not yet played —
      // as clips instead. It is a no-op there if the stream is already gone.
      effects: [{ type: 'close', notify: true }, { type: 'open', iceServers: state.iceServers }],
    }
  }
  return {
    state: { ...state, phase: 'fallen-back', offerSent: false },
    effects: [{ type: 'close', notify: true }],
  }
}

/** Abandon WebRTC for this socket without a retry. */
function giveUp(state: RtcState): RtcStep {
  return {
    state: { ...state, phase: 'fallen-back', offerSent: false },
    effects: [{ type: 'close', notify: true }],
  }
}

export function rtcReduce(state: RtcState, event: RtcEvent): RtcStep {
  const ignored: RtcStep = { state, effects: [] }

  switch (event.type) {
    case 'ready': {
      // `ready` is the first frame of a socket connection, so whatever was
      // negotiated before belonged to another socket — the server has closed
      // its end of that already. Start over, with a fresh retry budget.
      const effects: RtcEffect[] = hasPeer(state) ? [{ type: 'close', notify: false }] : []
      if (event.media?.webrtc !== true || !event.supported) {
        return { state: initialRtcState, effects }
      }
      const iceServers = usableIceServers(event.media.ice_servers)
      return {
        state: { phase: 'offering', offers: 1, offerSent: false, iceServers },
        effects: [...effects, { type: 'open', iceServers }],
      }
    }

    case 'offer-sent':
      return state.phase === 'offering' ? { state: { ...state, offerSent: true }, effects: [] } : ignored

    case 'offer-failed':
      return state.phase === 'offering' ? fail(state) : ignored

    case 'answer':
      // Only an answer to an offer that was actually sent. Anything else is
      // unsolicited and must not be applied to a peer connection.
      if (state.phase !== 'offering' || !state.offerSent) return ignored
      return { state: { ...state, phase: 'answered' }, effects: [{ type: 'apply-answer', sdp: event.sdp }] }

    case 'answer-failed':
      return state.phase === 'answered' ? fail(state) : ignored

    case 'webrtc-error':
      return hasPeer(state) ? fail(state) : ignored

    case 'media-transport':
      if (event.transport === 'webrtc') {
        // Believed only when it can be about the peer connection we hold:
        // one whose answer has been applied.
        return state.phase === 'answered' ? { state: { ...state, phase: 'live' }, effects: [] } : ignored
      }
      // 'clips' while offering is the server reporting that the PREVIOUS peer
      // connection was replaced by this offer — expected, not a failure.
      return state.phase === 'answered' || state.phase === 'live' ? fail(state) : ignored

    case 'peer-state':
      // 'disconnected' is not a failure by itself: ICE often recovers from it
      // on its own. RtcSession times how long it lasts and reports
      // `peer-lost` if that is too long.
      if (event.state !== 'failed' && event.state !== 'closed') return ignored
      return hasPeer(state) ? fail(state) : ignored

    case 'peer-lost':
      return hasPeer(state) ? fail(state) : ignored

    case 'timeout':
      // No retry. The server may still be working on this offer, and its late
      // answer would be indistinguishable from the answer to a second one.
      return state.phase === 'offering' || state.phase === 'answered' ? giveUp(state) : ignored

    case 'socket-closed':
      // Nothing to tell the server: the socket is gone, and it closes its
      // peer connection when the socket does.
      return {
        state: initialRtcState,
        effects: hasPeer(state) ? [{ type: 'close', notify: false }] : [],
      }
  }
}

// ── Running the effects ────────────────────────────────────────────────────

/** The slice of RTCPeerConnection the session uses. */
export interface RtcPeer {
  addTransceiver(kind: 'audio' | 'video', init: { direction: 'recvonly' }): void
  createOffer(): Promise<{ type: 'offer'; sdp?: string }>
  setLocalDescription(description: { type: 'offer'; sdp?: string }): Promise<void>
  setRemoteDescription(description: { type: 'answer'; sdp: string }): Promise<void>
  /** The local description's SDP, including the candidates gathered so far. */
  localSdp(): string | null
  iceGatheringState(): string
  close(): void
}

export interface RtcPeerEvents<Track> {
  onTrack(track: Track): void
  onConnectionState(state: string): void
  onIceGatheringState(state: string): void
}

export interface RtcEnvironment<Track> {
  /** Does this browser have RTCPeerConnection? */
  supported(): boolean
  createPeer(iceServers: IceServer[], events: RtcPeerEvents<Track>): RtcPeer
  /** Send a signalling frame. False if the socket was not open. */
  send(signal: RtcSignal): boolean
  /** An incoming track: put it on the stage. */
  attach(track: Track): void
  /** Stop every attached track and clear the stage. */
  detach(): void
  /** The phase changed. */
  onChange(state: RtcState): void
}

/**
 * Owns one peer connection at a time and drives it from the reducer.
 *
 * The same rule as SessionSocket: only the CURRENT peer may touch state.
 * Every continuation checks it still belongs to `this.peer`, so a peer that
 * was replaced by a retry or a reconnect is inert — its late `failed` cannot
 * take down the connection that replaced it.
 */
export class RtcSession<Track = unknown> {
  private state: RtcState = initialRtcState
  private peer: RtcPeer | null = null
  private deadline: ReturnType<typeof setTimeout> | null = null
  /** Running while the current peer reports `disconnected`. */
  private disconnectTimer: ReturnType<typeof setTimeout> | null = null
  private announced: RtcPhase = 'off'
  private disposed = false

  constructor(private readonly env: RtcEnvironment<Track>) {}

  get phase(): RtcPhase {
    return this.state.phase
  }

  get transport(): 'webrtc' | 'clips' {
    return transportOf(this.state)
  }

  /** The socket's `ready` frame arrived: a new connection, a new negotiation. */
  ready(media: MediaCapabilities | undefined): void {
    this.dispatch({ type: 'ready', media, supported: this.env.supported() })
  }

  answer(sdp: string): void {
    this.dispatch({ type: 'answer', sdp })
  }

  serverError(): void {
    this.dispatch({ type: 'webrtc-error' })
  }

  mediaTransport(transport: 'webrtc' | 'clips'): void {
    this.dispatch({ type: 'media-transport', transport })
  }

  socketClosed(): void {
    this.dispatch({ type: 'socket-closed' })
  }

  /** The owner is going away. Closes for good; later events do nothing. */
  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    if (this.peer) this.closePeer(true)
    this.state = initialRtcState
  }

  private dispatch(event: RtcEvent): void {
    if (this.disposed) return
    const step = rtcReduce(this.state, event)
    // State first: an effect can fail synchronously and dispatch again.
    this.state = step.state
    for (const effect of step.effects) this.run(effect)
    if (this.state.phase === 'live') this.clearDeadline()
    if (this.announced !== this.state.phase) {
      this.announced = this.state.phase
      this.env.onChange(this.state)
    }
  }

  private run(effect: RtcEffect): void {
    switch (effect.type) {
      case 'open':
        void this.open(effect.iceServers)
        break
      case 'apply-answer':
        void this.applyAnswer(effect.sdp)
        break
      case 'close':
        this.closePeer(effect.notify)
        break
    }
  }

  private async open(iceServers: IceServer[]): Promise<void> {
    let peer: RtcPeer
    let gathered: () => void = () => {}
    try {
      peer = this.env.createPeer(iceServers, {
        onTrack: (track) => {
          if (this.peer === peer) this.env.attach(track)
        },
        onConnectionState: (state) => {
          if (this.peer !== peer) return
          this.watchDisconnect(peer, state)
          this.dispatch({ type: 'peer-state', state })
        },
        onIceGatheringState: (state) => {
          if (state === 'complete') gathered()
        },
      })
    } catch {
      // A malformed ICE server entry makes the constructor throw.
      this.dispatch({ type: 'offer-failed' })
      return
    }
    this.peer = peer
    this.clearDeadline()
    this.deadline = setTimeout(() => {
      this.deadline = null
      if (this.peer === peer) this.dispatch({ type: 'timeout' })
    }, RTC_LIVE_TIMEOUT_MS)

    try {
      peer.addTransceiver('audio', { direction: 'recvonly' })
      peer.addTransceiver('video', { direction: 'recvonly' })
      const offer = await peer.createOffer()
      if (this.peer !== peer) return
      await peer.setLocalDescription(offer)
      if (this.peer !== peer) return

      // No trickle ICE: the offer has to carry its candidates, so wait for
      // gathering — but not indefinitely. An unreachable TURN server keeps it
      // "gathering" for tens of seconds, and the host and STUN candidates
      // found in the first moments are usually all that is needed.
      if (peer.iceGatheringState() !== 'complete') {
        await new Promise<void>((resolve) => {
          const cap = setTimeout(resolve, ICE_GATHER_CAP_MS)
          gathered = () => {
            clearTimeout(cap)
            resolve()
          }
        })
        if (this.peer !== peer) return
      }

      const sdp = peer.localSdp() ?? offer.sdp
      if (!sdp || !this.env.send({ type: 'webrtc_offer', sdp })) throw new Error('offer not sent')
      this.dispatch({ type: 'offer-sent' })
    } catch {
      if (this.peer === peer) this.dispatch({ type: 'offer-failed' })
    }
  }

  private async applyAnswer(sdp: string): Promise<void> {
    const peer = this.peer
    if (!peer) return
    try {
      await peer.setRemoteDescription({ type: 'answer', sdp })
    } catch {
      if (this.peer === peer) this.dispatch({ type: 'answer-failed' })
    }
  }

  /**
   * `disconnected` has to last, without a break, to count: any other state
   * in between (back to `connected`, or on to `failed`, which is handled as
   * its own event) stops the clock.
   */
  private watchDisconnect(peer: RtcPeer, state: string): void {
    if (state !== 'disconnected') {
      this.clearDisconnectTimer()
      return
    }
    if (this.disconnectTimer !== null) return
    this.disconnectTimer = setTimeout(() => {
      this.disconnectTimer = null
      if (this.peer === peer) this.dispatch({ type: 'peer-lost' })
    }, RTC_DISCONNECT_GRACE_MS)
  }

  private closePeer(notify: boolean): void {
    this.clearDeadline()
    this.clearDisconnectTimer()
    const peer = this.peer
    this.peer = null
    if (!peer) return
    if (notify) this.env.send({ type: 'webrtc_close' })
    this.env.detach()
    try {
      peer.close()
    } catch {
      /* already closed */
    }
  }

  private clearDisconnectTimer(): void {
    if (this.disconnectTimer !== null) {
      clearTimeout(this.disconnectTimer)
      this.disconnectTimer = null
    }
  }

  private clearDeadline(): void {
    if (this.deadline !== null) {
      clearTimeout(this.deadline)
      this.deadline = null
    }
  }
}
