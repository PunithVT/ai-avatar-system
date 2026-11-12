import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ICE_GATHER_CAP_MS, MAX_RTC_OFFERS, RTC_DISCONNECT_GRACE_MS, RTC_LIVE_TIMEOUT_MS, RtcSession, initialRtcState, rtcReduce,
  transportOf, type RtcEffect, type RtcEvent, type RtcPeer, type RtcPeerEvents, type RtcPhase,
  type RtcState,
} from '@/lib/chat/rtcSession'
import type { IceServer, MediaCapabilities, RtcSignal } from '@/lib/types'

const ICE: IceServer[] = [{ urls: ['stun:stun.example:3478'] }]
const OFFERED: MediaCapabilities = { webrtc: true, ice_servers: ICE }

// Rest args, not a default: `ready(undefined)` has to mean "no media field".
const ready = (...args: [media?: MediaCapabilities, supported?: boolean]): RtcEvent =>
  ({ type: 'ready', media: args.length === 0 ? OFFERED : args[0], supported: args[1] ?? true })

/** A state in each phase, as the reducer itself would have produced it. */
function inPhase(phase: RtcPhase, offers = 1): RtcState {
  const base: RtcState = { phase, offers, offerSent: false, iceServers: ICE }
  switch (phase) {
    case 'off':
      return initialRtcState
    case 'offering':
      return { ...base, offerSent: true }
    case 'answered':
    case 'live':
      return { ...base, offerSent: true }
    case 'fallen-back':
      return { ...base, offers: MAX_RTC_OFFERS }
  }
}

const PHASES: RtcPhase[] = ['off', 'offering', 'answered', 'live', 'fallen-back']
const WITH_PEER: RtcPhase[] = ['offering', 'answered', 'live']
// The retry tells the server too: its replacement offer takes up to
// ICE_GATHER_CAP_MS to leave, and the server must not use the old stream
// in the meantime.
const RETRY: RtcEffect[] = [{ type: 'close', notify: true }, { type: 'open', iceServers: ICE }]
const GIVE_UP: RtcEffect[] = [{ type: 'close', notify: true }]

describe('rtcReduce', () => {
  describe('ready', () => {
    it('offers when the server offers WebRTC and the browser can do it', () => {
      const step = rtcReduce(initialRtcState, ready())
      expect(step.state).toEqual({ phase: 'offering', offers: 1, offerSent: false, iceServers: ICE })
      expect(step.effects).toEqual([{ type: 'open', iceServers: ICE }])
    })

    it.each([
      ['a server that predates the field', undefined],
      ['webrtc: false', { webrtc: false, ice_servers: ICE }],
      ['a truthy value that is not true', { webrtc: 'yes' } as unknown as MediaCapabilities],
    ])('stays off for %s', (_name, media) => {
      const step = rtcReduce(initialRtcState, ready(media))
      expect(step.state).toBe(initialRtcState)
      expect(step.effects).toEqual([])
    })

    it('stays off when the browser has no RTCPeerConnection', () => {
      const step = rtcReduce(initialRtcState, ready(OFFERED, false))
      expect(step.state).toBe(initialRtcState)
      expect(step.effects).toEqual([])
    })

    it('offers without ICE servers when none are configured', () => {
      const step = rtcReduce(initialRtcState, ready({ webrtc: true }))
      expect(step.effects).toEqual([{ type: 'open', iceServers: [] }])
    })

    it('drops ICE entries a peer connection would throw on', () => {
      const servers = [{ urls: 'stun:a' }, { username: 'no-urls' }, null, { urls: [1] }, { urls: ['turn:b'], username: 'u', credential: 'c' }]
      const step = rtcReduce(initialRtcState, ready({ webrtc: true, ice_servers: servers as never }))
      expect(step.state.iceServers).toEqual([{ urls: 'stun:a' }, { urls: ['turn:b'], username: 'u', credential: 'c' }])
    })

    it.each(PHASES)('starts over from %s, with a fresh retry budget', (phase) => {
      // `ready` is the first frame of a NEW socket connection.
      const step = rtcReduce(inPhase(phase, MAX_RTC_OFFERS), ready())
      expect(step.state).toEqual({ phase: 'offering', offers: 1, offerSent: false, iceServers: ICE })
      const closeFirst: RtcEffect[] = WITH_PEER.includes(phase) ? [{ type: 'close', notify: false }] : []
      expect(step.effects).toEqual([...closeFirst, { type: 'open', iceServers: ICE }])
    })

    it.each(WITH_PEER)('closes the old peer when the new connection does not offer WebRTC (%s)', (phase) => {
      const step = rtcReduce(inPhase(phase), ready(undefined))
      expect(step.state).toBe(initialRtcState)
      expect(step.effects).toEqual([{ type: 'close', notify: false }])
    })
  })

  // Every event that is not `ready`, in every phase. `same` = ignored.
  type Expectation = 'same' | 'retry' | 'give-up' | { phase: RtcPhase; effects: RtcEffect[]; offerSent?: boolean }
  const table: Array<[string, RtcEvent, Record<RtcPhase, Expectation>]> = [
    ['offer-sent', { type: 'offer-sent' }, {
      off: 'same', offering: { phase: 'offering', effects: [], offerSent: true }, answered: 'same', live: 'same', 'fallen-back': 'same',
    }],
    ['offer-failed', { type: 'offer-failed' }, {
      off: 'same', offering: 'retry', answered: 'same', live: 'same', 'fallen-back': 'same',
    }],
    ['answer', { type: 'answer', sdp: 'ANSWER' }, {
      off: 'same', offering: { phase: 'answered', effects: [{ type: 'apply-answer', sdp: 'ANSWER' }] }, answered: 'same', live: 'same', 'fallen-back': 'same',
    }],
    ['answer-failed', { type: 'answer-failed' }, {
      off: 'same', offering: 'same', answered: 'retry', live: 'same', 'fallen-back': 'same',
    }],
    ['webrtc-error', { type: 'webrtc-error' }, {
      off: 'same', offering: 'retry', answered: 'retry', live: 'retry', 'fallen-back': 'same',
    }],
    ['media-transport webrtc', { type: 'media-transport', transport: 'webrtc' }, {
      off: 'same', offering: 'same', answered: { phase: 'live', effects: [] }, live: 'same', 'fallen-back': 'same',
    }],
    ['media-transport clips', { type: 'media-transport', transport: 'clips' }, {
      off: 'same', offering: 'same', answered: 'retry', live: 'retry', 'fallen-back': 'same',
    }],
    ['peer failed', { type: 'peer-state', state: 'failed' }, {
      off: 'same', offering: 'retry', answered: 'retry', live: 'retry', 'fallen-back': 'same',
    }],
    ['peer closed', { type: 'peer-state', state: 'closed' }, {
      off: 'same', offering: 'retry', answered: 'retry', live: 'retry', 'fallen-back': 'same',
    }],
    ['peer disconnected', { type: 'peer-state', state: 'disconnected' }, {
      off: 'same', offering: 'same', answered: 'same', live: 'same', 'fallen-back': 'same',
    }],
    // …but `disconnected` that lasts is (RtcSession keeps the time).
    ['peer lost', { type: 'peer-lost' }, {
      off: 'same', offering: 'retry', answered: 'retry', live: 'retry', 'fallen-back': 'same',
    }],
    ['peer connecting', { type: 'peer-state', state: 'connecting' }, {
      off: 'same', offering: 'same', answered: 'same', live: 'same', 'fallen-back': 'same',
    }],
    ['peer connected', { type: 'peer-state', state: 'connected' }, {
      off: 'same', offering: 'same', answered: 'same', live: 'same', 'fallen-back': 'same',
    }],
    ['timeout', { type: 'timeout' }, {
      off: 'same', offering: 'give-up', answered: 'give-up', live: 'same', 'fallen-back': 'same',
    }],
  ]

  describe.each(table)('%s', (_name, event, expected) => {
    it.each(PHASES)('in %s', (phase) => {
      const before = inPhase(phase)
      const step = rtcReduce(before, event)
      const want = expected[phase]
      if (want === 'same') {
        expect(step.state).toBe(before)
        expect(step.effects).toEqual([])
      } else if (want === 'retry') {
        expect(step.state).toEqual({ phase: 'offering', offers: 2, offerSent: false, iceServers: ICE })
        expect(step.effects).toEqual(RETRY)
      } else if (want === 'give-up') {
        expect(step.state.phase).toBe('fallen-back')
        expect(step.effects).toEqual(GIVE_UP)
      } else {
        expect(step.state.phase).toBe(want.phase)
        expect(step.effects).toEqual(want.effects)
        if (want.offerSent !== undefined) expect(step.state.offerSent).toBe(want.offerSent)
      }
    })
  })

  describe('socket-closed', () => {
    it.each(PHASES)('returns to off from %s', (phase) => {
      const step = rtcReduce(inPhase(phase), { type: 'socket-closed' })
      expect(step.state).toBe(initialRtcState)
      // Nothing to tell a server whose socket is gone.
      expect(step.effects).toEqual(WITH_PEER.includes(phase) ? [{ type: 'close', notify: false }] : [])
    })
  })

  describe('the once-plus-one-retry rule', () => {
    const failures: Array<[string, RtcPhase, RtcEvent]> = [
      ['the offer could not be made', 'offering', { type: 'offer-failed' }],
      ['webrtc_error', 'offering', { type: 'webrtc-error' }],
      ['the answer was rejected', 'answered', { type: 'answer-failed' }],
      ['the server reverted to clips', 'answered', { type: 'media-transport', transport: 'clips' }],
      ['the peer connection failed while connecting', 'answered', { type: 'peer-state', state: 'failed' }],
      ['the peer connection failed while live', 'live', { type: 'peer-state', state: 'failed' }],
      ['the server dropped a live stream', 'live', { type: 'media-transport', transport: 'clips' }],
      ['webrtc_error while live', 'live', { type: 'webrtc-error' }],
      ['the peer stayed disconnected while live', 'live', { type: 'peer-lost' }],
      ['the peer stayed disconnected before the announcement', 'answered', { type: 'peer-lost' }],
    ]

    it.each(failures)('retries once when %s', (_name, phase, event) => {
      const step = rtcReduce(inPhase(phase, 1), event)
      expect(step.state).toMatchObject({ phase: 'offering', offers: 2, offerSent: false })
      expect(step.effects).toEqual(RETRY)
    })

    it.each(failures)('settles on clips when the retry also ends with: %s', (_name, phase, event) => {
      const step = rtcReduce(inPhase(phase, MAX_RTC_OFFERS), event)
      expect(step.state.phase).toBe('fallen-back')
      expect(step.effects).toEqual(GIVE_UP)
      expect(step.effects.some((e) => e.type === 'open')).toBe(false)
    })

    it('never opens more than two peers on one socket, whatever arrives', () => {
      const noise: RtcEvent[] = [
        { type: 'offer-sent' }, { type: 'webrtc-error' }, { type: 'offer-sent' }, { type: 'answer', sdp: 'A' },
        { type: 'media-transport', transport: 'webrtc' }, { type: 'peer-state', state: 'failed' },
        { type: 'webrtc-error' }, { type: 'offer-failed' }, { type: 'answer', sdp: 'B' },
        { type: 'media-transport', transport: 'webrtc' }, { type: 'media-transport', transport: 'clips' },
        { type: 'peer-state', state: 'failed' }, { type: 'timeout' }, { type: 'answer-failed' },
      ]
      let state = initialRtcState
      let opens = 0
      for (const event of [ready(), ...noise, ...noise]) {
        const step = rtcReduce(state, event)
        opens += step.effects.filter((e) => e.type === 'open').length
        state = step.state
      }
      expect(opens).toBe(MAX_RTC_OFFERS)
      expect(state.phase).toBe('fallen-back')
    })

    it('a reconnect gets its two offers back', () => {
      const spent = inPhase('fallen-back')
      const closed = rtcReduce(spent, { type: 'socket-closed' }).state
      const again = rtcReduce(closed, ready())
      expect(again.state).toMatchObject({ phase: 'offering', offers: 1 })
      expect(rtcReduce(again.state, { type: 'offer-failed' }).effects).toEqual(RETRY)
    })
  })

  it('ignores an answer nobody asked for', () => {
    // offering, but the offer has not left yet
    const building: RtcState = { phase: 'offering', offers: 1, offerSent: false, iceServers: ICE }
    const step = rtcReduce(building, { type: 'answer', sdp: 'X' })
    expect(step.state).toBe(building)
    expect(step.effects).toEqual([])
  })

  it('reports webrtc only once the server has announced it', () => {
    const walk: Array<[RtcEvent, 'webrtc' | 'clips']> = [
      [ready(), 'clips'],
      [{ type: 'offer-sent' }, 'clips'],
      [{ type: 'answer', sdp: 'A' }, 'clips'],
      // Our own peer connection being up is not the server's announcement.
      [{ type: 'peer-state', state: 'connected' }, 'clips'],
      [{ type: 'media-transport', transport: 'webrtc' }, 'webrtc'],
      [{ type: 'media-transport', transport: 'clips' }, 'clips'],
    ]
    let state = initialRtcState
    for (const [event, transport] of walk) {
      state = rtcReduce(state, event).state
      expect(transportOf(state)).toBe(transport)
    }
  })

  it.each(PHASES.filter((p) => p !== 'live'))('is on clips in %s', (phase) => {
    expect(transportOf(inPhase(phase))).toBe('clips')
  })
})

// ── RtcSession: the effects, against a fake peer connection ───────────────

type Track = { id: string }

class FakePeer implements RtcPeer {
  calls: string[] = []
  closed = false
  gathering = 'new'
  local: string | null = null
  remote: string | null = null
  failCreateOffer = false
  failRemote = false

  constructor(readonly iceServers: IceServer[], readonly events: RtcPeerEvents<Track>) {}

  addTransceiver(kind: 'audio' | 'video', init: { direction: 'recvonly' }) {
    this.calls.push(`transceiver:${kind}:${init.direction}`)
  }
  async createOffer() {
    this.calls.push('createOffer')
    if (this.failCreateOffer) throw new Error('no offer')
    return { type: 'offer' as const, sdp: 'OFFER-NO-CANDIDATES' }
  }
  async setLocalDescription() {
    this.calls.push('setLocalDescription')
    this.local = 'OFFER-NO-CANDIDATES'
    this.gathering = 'gathering'
  }
  async setRemoteDescription(description: { type: 'answer'; sdp: string }) {
    this.calls.push(`setRemoteDescription:${description.type}`)
    if (this.failRemote) throw new Error('bad answer')
    this.remote = description.sdp
  }
  localSdp() {
    return this.local
  }
  iceGatheringState() {
    return this.gathering
  }
  close() {
    this.closed = true
  }
  // ── test controls ──
  finishGathering() {
    this.gathering = 'complete'
    this.local = 'OFFER-WITH-CANDIDATES'
    this.events.onIceGatheringState('complete')
  }
  connectionState(state: string) {
    this.events.onConnectionState(state)
  }
}

function setup(options: { supported?: boolean; socketOpen?: boolean; createThrows?: boolean } = {}) {
  const peers: FakePeer[] = []
  const sent: RtcSignal[] = []
  const attached: Track[] = []
  const world = {
    socketOpen: options.socketOpen ?? true,
    createThrows: options.createThrows ?? false,
    failCreateOffer: false,
    detached: 0,
  }
  const phases: RtcPhase[] = []
  const session = new RtcSession<Track>({
    supported: () => options.supported ?? true,
    createPeer: (iceServers, events) => {
      if (world.createThrows) throw new Error('InvalidAccessError')
      const peer = new FakePeer(iceServers, events)
      peer.failCreateOffer = world.failCreateOffer
      peers.push(peer)
      return peer
    },
    send: (signal) => {
      if (!world.socketOpen) return false
      sent.push(signal)
      return true
    },
    attach: (track) => attached.push(track),
    detach: () => {
      world.detached += 1
    },
    onChange: (state) => phases.push(state.phase),
  })
  const latest = () => peers[peers.length - 1]
  /** Let the awaits inside the session run. */
  const settle = () => vi.advanceTimersByTimeAsync(0)
  /** ready → offer on the wire. */
  const offer = async () => {
    session.ready(OFFERED)
    await settle()
    latest().finishGathering()
    await settle()
  }
  /** …→ answer applied → server announcement. */
  const goLive = async () => {
    await offer()
    session.answer('ANSWER')
    await settle()
    session.mediaTransport('webrtc')
  }
  return { session, peers, sent, attached, world, phases, latest, settle, offer, goLive }
}

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('RtcSession', () => {
  it('does nothing at all for a server that sends a bare ready', () => {
    // An older server: no `media` field. Nothing may change for it.
    const { session, peers, sent, phases, world } = setup()
    session.ready(undefined)
    expect(peers).toHaveLength(0)
    expect(sent).toEqual([])
    expect(phases).toEqual([])
    expect(world.detached).toBe(0)
    expect(session.phase).toBe('off')
    expect(session.transport).toBe('clips')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does nothing when the server says webrtc: false', () => {
    const { session, peers, sent } = setup()
    session.ready({ webrtc: false, ice_servers: ICE })
    expect(peers).toHaveLength(0)
    expect(sent).toEqual([])
    expect(session.transport).toBe('clips')
  })

  it('does not offer from a browser without RTCPeerConnection', () => {
    const { session, peers, sent } = setup({ supported: false })
    session.ready(OFFERED)
    expect(peers).toHaveLength(0)
    expect(sent).toEqual([])
  })

  it('negotiates: two recvonly transceivers, offer, gather, send, answer, live', async () => {
    const { session, sent, latest, settle, phases } = setup()
    session.ready(OFFERED)
    await settle()
    const peer = latest()
    expect(peer.iceServers).toEqual(ICE)
    expect(peer.calls).toEqual([
      'transceiver:audio:recvonly', 'transceiver:video:recvonly', 'createOffer', 'setLocalDescription',
    ])
    // No trickle ICE: nothing is sent until the candidates are in the SDP.
    expect(sent).toEqual([])

    peer.finishGathering()
    await settle()
    expect(sent).toEqual([{ type: 'webrtc_offer', sdp: 'OFFER-WITH-CANDIDATES' }])
    expect(session.phase).toBe('offering')

    session.answer('ANSWER')
    await settle()
    expect(peer.calls.at(-1)).toBe('setRemoteDescription:answer')
    expect(peer.remote).toBe('ANSWER')
    expect(session.phase).toBe('answered')
    expect(session.transport).toBe('clips')

    peer.connectionState('connected')
    expect(session.transport).toBe('clips') // still the server's call

    session.mediaTransport('webrtc')
    expect(session.transport).toBe('webrtc')
    expect(phases).toEqual(['offering', 'answered', 'live'])
  })

  it('sends the offer with what it has when gathering outlasts the cap', async () => {
    const { session, sent, settle } = setup()
    session.ready(OFFERED)
    await settle()
    await vi.advanceTimersByTimeAsync(ICE_GATHER_CAP_MS - 1)
    expect(sent).toEqual([])
    await vi.advanceTimersByTimeAsync(1)
    expect(sent).toEqual([{ type: 'webrtc_offer', sdp: 'OFFER-NO-CANDIDATES' }])
  })

  it('does not send a second offer when gathering completes after the cap', async () => {
    const { session, sent, latest, settle } = setup()
    session.ready(OFFERED)
    await settle()
    await vi.advanceTimersByTimeAsync(ICE_GATHER_CAP_MS)
    latest().finishGathering()
    await settle()
    expect(sent).toHaveLength(1)
  })

  it('hands incoming tracks to the stage', async () => {
    const { latest, attached, offer } = setup()
    await offer()
    latest().events.onTrack({ id: 'audio' })
    latest().events.onTrack({ id: 'video' })
    expect(attached).toEqual([{ id: 'audio' }, { id: 'video' }])
  })

  it('retries once after webrtc_error, then stays on clips', async () => {
    const { session, peers, sent, latest, settle, offer, world } = setup()
    await offer()
    const first = latest()

    session.serverError()
    expect(first.closed).toBe(true)
    expect(peers).toHaveLength(2)
    await settle()
    latest().finishGathering()
    await settle()
    expect(sent.filter((s) => s.type === 'webrtc_offer')).toHaveLength(2)
    // The server is told about the first peer before it is offered the
    // second, so it never puts a sentence on the one that was closed.
    expect(sent.map((s) => s.type)).toEqual(['webrtc_offer', 'webrtc_close', 'webrtc_offer'])

    session.serverError()
    expect(latest().closed).toBe(true)
    expect(peers).toHaveLength(2)
    expect(session.phase).toBe('fallen-back')
    expect(session.transport).toBe('clips')
    expect(sent.at(-1)).toEqual({ type: 'webrtc_close' })
    expect(world.detached).toBe(2)

    // Nothing brings it back on this socket.
    session.serverError()
    session.answer('LATE')
    session.mediaTransport('webrtc')
    await settle()
    expect(peers).toHaveLength(2)
    expect(session.transport).toBe('clips')
  })

  it('falls back when the peer connection fails while live, after one retry', async () => {
    const { session, peers, latest, settle, goLive, phases } = setup()
    await goLive()
    expect(session.transport).toBe('webrtc')

    latest().connectionState('failed')
    expect(session.transport).toBe('clips') // at once, not after the retry
    expect(peers).toHaveLength(2)
    await settle()
    latest().finishGathering()
    await settle()
    session.answer('ANSWER-2')
    await settle()
    session.mediaTransport('webrtc')
    expect(session.transport).toBe('webrtc')

    latest().connectionState('failed')
    expect(session.phase).toBe('fallen-back')
    expect(peers).toHaveLength(2)
    expect(phases).toEqual(['offering', 'answered', 'live', 'offering', 'answered', 'live', 'fallen-back'])
  })

  it('tells the server at once when a live peer fails, long before the retry offer leaves', async () => {
    const { session, sent, latest, settle, goLive } = setup()
    await goLive()
    const before = sent.length

    latest().connectionState('failed')
    // Synchronously: nothing has been awaited, the new offer is not even built.
    expect(sent.slice(before)).toEqual([{ type: 'webrtc_close' }])
    expect(session.transport).toBe('clips')

    // Gathering for the retry runs into its cap; only then does the offer go.
    await settle()
    await vi.advanceTimersByTimeAsync(ICE_GATHER_CAP_MS - 1)
    expect(sent.slice(before)).toEqual([{ type: 'webrtc_close' }])
    await vi.advanceTimersByTimeAsync(1)
    expect(sent.slice(before).map((s) => s.type)).toEqual(['webrtc_close', 'webrtc_offer'])
  })

  it('sends one close per peer it gives up on, retry and fallback alike', async () => {
    const { session, sent, latest, settle, goLive } = setup()
    await goLive()
    latest().connectionState('failed')
    await settle()
    latest().finishGathering()
    await settle()
    latest().connectionState('failed')
    expect(session.phase).toBe('fallen-back')
    expect(sent.map((s) => s.type)).toEqual(['webrtc_offer', 'webrtc_close', 'webrtc_offer', 'webrtc_close'])
  })

  describe('a peer that goes `disconnected`', () => {
    it('is given up on after 3 s without a break, and the server is told', async () => {
      const { session, sent, peers, latest, goLive } = setup()
      await goLive()
      const before = sent.length

      latest().connectionState('disconnected')
      expect(session.transport).toBe('webrtc') // not yet: it may recover
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS - 1)
      expect(session.transport).toBe('webrtc')
      expect(sent).toHaveLength(before)

      await vi.advanceTimersByTimeAsync(1)
      expect(session.transport).toBe('clips')
      expect(sent.slice(before)).toEqual([{ type: 'webrtc_close' }])
      expect(peers[0].closed).toBe(true)
      expect(peers).toHaveLength(2) // and the one retry is under way
    })

    it('is left alone when it reconnects inside the window', async () => {
      const { session, sent, peers, latest, goLive } = setup()
      await goLive()
      const before = sent.length

      latest().connectionState('disconnected')
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS - 1)
      latest().connectionState('connected')
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS * 2)

      expect(session.transport).toBe('webrtc')
      expect(sent).toHaveLength(before)
      expect(peers).toHaveLength(1)
      expect(peers[0].closed).toBe(false)
    })

    it('counts continuous time only: a flap starts the clock again', async () => {
      const { session, latest, goLive } = setup()
      await goLive()
      latest().connectionState('disconnected')
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS - 1)
      latest().connectionState('connected')
      latest().connectionState('disconnected')
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS - 1)
      expect(session.transport).toBe('webrtc')
      await vi.advanceTimersByTimeAsync(1)
      expect(session.transport).toBe('clips')
    })

    it('a repeated `disconnected` does not push the deadline back', async () => {
      const { session, latest, goLive } = setup()
      await goLive()
      latest().connectionState('disconnected')
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS - 1)
      latest().connectionState('disconnected')
      await vi.advanceTimersByTimeAsync(1)
      expect(session.transport).toBe('clips')
    })

    it('fails once, not twice, when `failed` arrives inside the window', async () => {
      const { session, sent, peers, latest, settle, goLive } = setup()
      await goLive()
      const first = latest()
      first.connectionState('disconnected')
      first.connectionState('failed')
      expect(peers).toHaveLength(2)
      await settle()
      latest().finishGathering()
      await settle()

      // The first peer's clock must not go off against its replacement.
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS * 2)
      expect(session.phase).toBe('offering')
      expect(peers).toHaveLength(2)
      expect(sent.filter((s) => s.type === 'webrtc_close')).toHaveLength(1)
    })

    it('is also given up on before the server has announced the stream', async () => {
      const { session, sent, latest, settle, offer } = setup()
      await offer()
      session.answer('ANSWER')
      await settle()
      latest().connectionState('connected')
      latest().connectionState('disconnected')
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS)
      expect(session.phase).toBe('offering') // the retry
      expect(sent.filter((s) => s.type === 'webrtc_close')).toHaveLength(1)
    })

    it('says nothing after the socket has closed', async () => {
      const { session, sent, latest, goLive } = setup()
      await goLive()
      latest().connectionState('disconnected')
      session.socketClosed()
      const before = sent.length
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS * 2)
      expect(sent).toHaveLength(before)
      expect(session.phase).toBe('off')
    })

    it('settles on clips when the retry is lost the same way', async () => {
      const { session, sent, latest, settle, goLive } = setup()
      await goLive()
      latest().connectionState('disconnected')
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS)
      await settle()
      latest().finishGathering()
      await settle()
      session.answer('ANSWER-2')
      await settle()
      session.mediaTransport('webrtc')
      expect(session.transport).toBe('webrtc')

      latest().connectionState('disconnected')
      await vi.advanceTimersByTimeAsync(RTC_DISCONNECT_GRACE_MS)
      expect(session.phase).toBe('fallen-back')
      expect(sent.at(-1)).toEqual({ type: 'webrtc_close' })
    })
  })

  it('falls back when the server reverts a live stream to clips', async () => {
    const { session, peers, goLive } = setup()
    await goLive()
    session.mediaTransport('clips')
    expect(session.transport).toBe('clips')
    expect(peers[0].closed).toBe(true)
    expect(peers).toHaveLength(2) // the one retry
    session.mediaTransport('clips') // "previous peer replaced": not a failure of the retry
    expect(session.phase).toBe('offering')
  })

  it('retries when the offer cannot be created, and falls back when that fails too', async () => {
    const { session, peers, sent, settle, world } = setup()
    world.failCreateOffer = true
    session.ready(OFFERED)
    await settle()
    expect(peers).toHaveLength(2)
    expect(session.phase).toBe('fallen-back')
    expect(peers.every((p) => p.closed)).toBe(true)
    expect(sent.filter((s) => s.type === 'webrtc_offer')).toEqual([])
  })

  it('falls back when the peer connection cannot even be constructed', () => {
    const { session, peers, phases } = setup({ createThrows: true })
    session.ready(OFFERED)
    expect(peers).toHaveLength(0)
    expect(session.phase).toBe('fallen-back')
    expect(phases).toEqual(['fallen-back'])
  })

  it('falls back when the socket closes before the offer can be sent', async () => {
    const { session, world, latest, settle, peers } = setup()
    session.ready(OFFERED)
    await settle()
    world.socketOpen = false
    latest().finishGathering()
    await settle()
    // Retried once (and that offer cannot be sent either).
    latest().finishGathering()
    await settle()
    expect(peers).toHaveLength(2)
    expect(session.phase).toBe('fallen-back')
  })

  it('retries when the browser rejects the answer', async () => {
    const { session, peers, latest, settle, offer } = setup()
    await offer()
    latest().failRemote = true
    session.answer('GARBAGE')
    await settle()
    expect(peers[0].closed).toBe(true)
    expect(peers).toHaveLength(2)
    expect(session.phase).toBe('offering')
  })

  it('gives up, without a retry, when the stream never goes live', async () => {
    const { session, peers, sent, offer } = setup()
    await offer()
    await vi.advanceTimersByTimeAsync(RTC_LIVE_TIMEOUT_MS)
    expect(session.phase).toBe('fallen-back')
    expect(peers).toHaveLength(1)
    expect(peers[0].closed).toBe(true)
    expect(sent.at(-1)).toEqual({ type: 'webrtc_close' })
    // The answer that finally turns up is not applied to anything.
    session.answer('LATE')
    expect(peers[0].remote).toBeNull()
  })

  it('does not time out a stream that went live', async () => {
    const { session, goLive } = setup()
    await goLive()
    await vi.advanceTimersByTimeAsync(RTC_LIVE_TIMEOUT_MS * 2)
    expect(session.transport).toBe('webrtc')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('tears down when the socket closes, and says nothing to the dead socket', async () => {
    const { session, sent, latest, goLive, world } = setup()
    await goLive()
    const before = sent.length
    session.socketClosed()
    expect(latest().closed).toBe(true)
    expect(world.detached).toBe(1)
    expect(session.phase).toBe('off')
    expect(session.transport).toBe('clips')
    expect(sent).toHaveLength(before)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('negotiates afresh on reconnect and never leaks the old peer', async () => {
    const { session, peers, sent, latest, settle, goLive } = setup()
    await goLive()
    session.socketClosed()
    await goLive()
    expect(peers).toHaveLength(2)
    expect(peers[0].closed).toBe(true)
    expect(peers[1].closed).toBe(false)
    expect(session.transport).toBe('webrtc')
    expect(sent.filter((s) => s.type === 'webrtc_offer')).toHaveLength(2)

    // A `ready` with no close in between (the socket was replaced silently)
    // still closes what the previous connection had.
    session.ready(OFFERED)
    await settle()
    expect(peers).toHaveLength(3)
    expect(peers[1].closed).toBe(true)
    expect(latest().closed).toBe(false)
    expect(peers.filter((p) => !p.closed)).toHaveLength(1)
  })

  it('a reconnect to a server without WebRTC closes the stream and stays on clips', async () => {
    const { session, peers, goLive } = setup()
    await goLive()
    session.ready(undefined)
    expect(peers[0].closed).toBe(true)
    expect(peers).toHaveLength(1)
    expect(session.transport).toBe('clips')
  })

  it('ignores everything a replaced peer says afterwards', async () => {
    const { session, peers, attached, settle, goLive } = setup()
    await goLive()
    const old = peers[0]
    session.socketClosed()
    await goLive()
    old.connectionState('failed')
    old.events.onTrack({ id: 'ghost' })
    await settle()
    expect(session.transport).toBe('webrtc')
    expect(peers).toHaveLength(2)
    expect(attached).toEqual([])
  })

  it('abandons an offer whose socket closed while it was being built', async () => {
    const { session, peers, sent, settle } = setup()
    session.ready(OFFERED)
    session.socketClosed() // before createOffer has even resolved
    await settle()
    await vi.advanceTimersByTimeAsync(ICE_GATHER_CAP_MS)
    expect(peers[0].closed).toBe(true)
    expect(sent).toEqual([])
    expect(session.phase).toBe('off')
  })

  it('dispose closes the peer, tells the server, and goes inert', async () => {
    const { session, peers, sent, goLive, settle, world } = setup()
    await goLive()
    session.dispose()
    expect(peers[0].closed).toBe(true)
    expect(world.detached).toBe(1)
    expect(sent.at(-1)).toEqual({ type: 'webrtc_close' })

    session.ready(OFFERED)
    session.dispose()
    await settle()
    expect(peers).toHaveLength(1)
    expect(sent.filter((s) => s.type === 'webrtc_close')).toHaveLength(1)
  })

  it('dispose with nothing negotiated sends nothing (StrictMode first mount)', () => {
    const { session, sent, world } = setup()
    session.dispose()
    expect(sent).toEqual([])
    expect(world.detached).toBe(0)
  })
})
