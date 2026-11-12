import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  MIC_ACK_TIMEOUT_MS, MIC_HIDDEN_RELEASE_MS, MicSession, initialMicState, micModeOf, micReduce,
  type MicEffect, type MicEvent, type MicMode, type MicPhase, type MicState,
} from '@/lib/chat/micMode'

// ── The reducer ────────────────────────────────────────────────────────────

/** Feed events in order; returns the final state and every effect, in order. */
function run(events: MicEvent[], from: MicState = initialMicState) {
  let state = from
  const effects: MicEffect[] = []
  for (const event of events) {
    const step = micReduce(state, event)
    state = step.state
    effects.push(...step.effects)
  }
  return { state, effects }
}

const kinds = (effects: MicEffect[]) =>
  effects.map((e) => (e.type === 'send' ? `send:${e.active}` : e.type))

const WANT: MicEvent = { type: 'wanted', on: true }
const UNWANT: MicEvent = { type: 'wanted', on: false }
const READY_MIC: MicEvent = { type: 'ready', mic: true }
const LIVE: MicEvent = { type: 'stream', live: true }
const DOWN: MicEvent = { type: 'stream', live: false }
const LISTENING: MicEvent = { type: 'mic-state', listening: true }
const NOT_LISTENING: MicEvent = { type: 'mic-state', listening: false }
const ATTACHED: MicEvent = { type: 'track-attached' }
const FAILED: MicEvent = { type: 'track-failed' }
const RELEASED: MicEvent = { type: 'local-released' }

/** A stream that offers the microphone, live, before hands-free is switched on. */
const STREAM_UP: MicEvent[] = [READY_MIC, LIVE]
/** Hands-free on a live stream, all the way to the server listening. */
const TO_STREAM: MicEvent[] = [...STREAM_UP, WANT, ATTACHED, LISTENING]

/** A state in each phase, reached the way the reducer itself gets there. */
function inPhase(phase: MicPhase): MicState {
  const paths: Record<MicPhase, MicEvent[]> = {
    off: STREAM_UP,
    local: [WANT],
    releasing: [WANT, ...STREAM_UP],
    acquiring: [...STREAM_UP, WANT],
    announced: [...STREAM_UP, WANT, ATTACHED],
    stream: TO_STREAM,
  }
  const { state } = run(paths[phase])
  expect(state.phase).toBe(phase)
  return state
}

const PHASES: MicPhase[] = ['off', 'local', 'releasing', 'acquiring', 'announced', 'stream']
const ON_STREAM: MicPhase[] = ['acquiring', 'announced', 'stream']
const TOLD: MicPhase[] = ['announced', 'stream']

describe('micReduce', () => {
  describe('against a server without the stream microphone', () => {
    it.each([
      ['a server that predates the field', [{ type: 'ready', mic: false }] as MicEvent[]],
      ['a live stream that does not offer it', [{ type: 'ready', mic: false }, LIVE] as MicEvent[]],
      ['no socket yet', [] as MicEvent[]],
    ])('hands-free is the browser detector and nothing else: %s', (_name, before) => {
      const on = run([...before, WANT])
      expect(on.state.phase).toBe('local')
      expect(kinds(on.effects)).toEqual(['start-local'])

      const off = run([UNWANT], on.state)
      expect(off.state.phase).toBe('off')
      expect(kinds(off.effects)).toEqual(['stop-local'])
    })

    it('never sends a mic frame, acquires or detaches, whatever happens', () => {
      const noise: MicEvent[] = [
        { type: 'ready', mic: false }, WANT, LIVE, LISTENING, NOT_LISTENING, ATTACHED, FAILED, RELEASED,
        { type: 'ack-timeout' }, { type: 'hidden' }, { type: 'visible' }, DOWN, LIVE,
        { type: 'socket-closed' }, { type: 'ready', mic: false }, LIVE, UNWANT, WANT, UNWANT,
      ]
      const { effects } = run(noise)
      // `mic_state listening: true` from a server that never offered the
      // microphone is answered with `active: false`; leave that one frame out
      // and check that it is the only thing that ever goes to such a server.
      const sent = kinds(effects).filter((k) => k !== 'start-local' && k !== 'stop-local')
      expect(sent).toEqual(['send:false'])
    })

    it('a server that never sends mic_state is never sent a mic frame', () => {
      const quiet: MicEvent[] = [
        { type: 'ready', mic: false }, WANT, LIVE, RELEASED, { type: 'hidden' }, { type: 'visible' }, DOWN, LIVE,
        { type: 'socket-closed' }, { type: 'ready', mic: false }, UNWANT, WANT, { type: 'shutdown' },
      ]
      expect(kinds(run(quiet).effects).filter((k) => !k.endsWith('-local'))).toEqual([])
    })
  })

  describe('going onto the stream', () => {
    it('needs all three: hands-free on, a live stream, and media.mic', () => {
      expect(run([WANT, LIVE]).state.phase).toBe('local')
      expect(run([WANT, READY_MIC]).state.phase).toBe('local')
      expect(run([READY_MIC, LIVE]).state.phase).toBe('off')
      expect(run([READY_MIC, LIVE, WANT]).state.phase).toBe('acquiring')
    })

    it('acquires, attaches, announces, and is `stream` only once the server confirms', () => {
      const modes: MicMode[] = []
      let state = initialMicState
      const effects: string[][] = []
      for (const event of TO_STREAM) {
        const step = micReduce(state, event)
        state = step.state
        modes.push(micModeOf(state))
        effects.push(kinds(step.effects))
      }
      expect(effects).toEqual([[], [], ['acquire'], ['send:true'], []])
      expect(modes).toEqual(['off', 'off', 'off', 'off', 'stream'])
      expect(state.phase).toBe('stream')
    })

    it('does not announce before the track is on the stream', () => {
      const { effects } = run([...STREAM_UP, WANT])
      expect(kinds(effects)).toEqual(['acquire'])
    })

    it('from the browser detector: stops it, and waits for the release before touching the microphone', () => {
      const local = run([WANT])
      const stopping = run(STREAM_UP, local.state)
      expect(stopping.state.phase).toBe('releasing')
      expect(kinds(stopping.effects)).toEqual(['stop-local'])
      expect(micModeOf(stopping.state)).toBe('off')

      // Anything but the release leaves it waiting.
      for (const event of [ATTACHED, LISTENING, { type: 'ack-timeout' }, { type: 'visible' }] as MicEvent[]) {
        const step = micReduce(stopping.state, event)
        expect(step.state.phase).toBe('releasing')
        expect(kinds(step.effects).filter((k) => k !== 'send:false')).toEqual([])
      }

      const released = run([RELEASED], stopping.state)
      expect(released.state.phase).toBe('acquiring')
      expect(kinds(released.effects)).toEqual(['acquire'])
    })

    it('a detector switched off and straight back on for the stream is still waited for', () => {
      // local → off leaves the detector told to stop, not stopped.
      const { state, effects } = run([WANT, UNWANT, ...STREAM_UP, WANT])
      expect(kinds(effects)).toEqual(['start-local', 'stop-local'])
      expect(state.phase).toBe('releasing')
      expect(kinds(run([RELEASED], state).effects)).toEqual(['acquire'])
    })

    it('a release reported while the detector is meant to be running is not believed', () => {
      const local = run([WANT, RELEASED])
      expect(local.state.localHeld).toBe(true)
      expect(run(STREAM_UP, local.state).state.phase).toBe('releasing')
    })

    it('goes back to the detector if the stream is lost while waiting for the release', () => {
      const { state, effects } = run([WANT, ...STREAM_UP, DOWN])
      expect(state.phase).toBe('local')
      // Stopped and started again within one step: the detector never let go.
      expect(kinds(effects)).toEqual(['start-local', 'stop-local', 'start-local'])
    })

    it('ignores the server saying it listens when nothing was announced — and tells it to stop', () => {
      for (const phase of PHASES.filter((p) => !TOLD.includes(p))) {
        const step = micReduce(inPhase(phase), LISTENING)
        expect(step.state.phase).toBe(phase)
        expect(kinds(step.effects)).toEqual(['send:false'])
        expect(micModeOf(step.state)).not.toBe('stream')
      }
    })

    it('a repeated confirmation changes nothing', () => {
      const step = micReduce(inPhase('stream'), LISTENING)
      expect(step.state.phase).toBe('stream')
      expect(step.effects).toEqual([])
    })
  })

  describe('falling back to the browser detector', () => {
    it.each([
      ['the microphone is refused', 'acquiring', FAILED, ['detach', 'start-local']],
      ['the track ends after it was announced', 'announced', FAILED, ['detach', 'send:false', 'start-local']],
      ['the track ends while listening', 'stream', FAILED, ['detach', 'send:false', 'start-local']],
      ['the server refuses', 'announced', NOT_LISTENING, ['detach', 'send:false', 'start-local']],
      ['the server stops listening unasked', 'stream', NOT_LISTENING, ['detach', 'send:false', 'start-local']],
      ['the server never acknowledges', 'announced', { type: 'ack-timeout' }, ['detach', 'send:false', 'start-local']],
      ['the stream goes down while acquiring', 'acquiring', DOWN, ['detach', 'start-local']],
      ['the stream goes down while announced', 'announced', DOWN, ['detach', 'send:false', 'start-local']],
      ['the stream goes down while listening', 'stream', DOWN, ['detach', 'send:false', 'start-local']],
    ] as [string, MicPhase, MicEvent, string[]][])('when %s', (_name, phase, event, expected) => {
      const step = micReduce(inPhase(phase), event)
      expect(step.state.phase).toBe('local')
      expect(step.state.wanted).toBe(true)
      // The stream lets go — track off, server told — BEFORE the detector starts.
      expect(kinds(step.effects)).toEqual(expected)
    })

    it.each(ON_STREAM)('when the socket closes in %s — with nothing said to a dead socket', (phase) => {
      const step = micReduce(inPhase(phase), { type: 'socket-closed' })
      expect(step.state.phase).toBe('local')
      expect(kinds(step.effects)).toEqual(['detach', 'start-local'])
    })

    it.each(ON_STREAM)('when a new socket connects in %s — its server was never told anything', (phase) => {
      const step = micReduce(inPhase(phase), READY_MIC)
      expect(step.state.phase).toBe('local')
      expect(kinds(step.effects)).toEqual(['detach', 'start-local'])
    })

    it('does not go straight back to a stream that just failed', () => {
      for (const event of [FAILED, NOT_LISTENING] as MicEvent[]) {
        const { state } = run([event, RELEASED, { type: 'visible' }, LIVE, ATTACHED], inPhase('stream'))
        expect(state.phase).toBe('local')
        expect(state.blocked).toBe(true)
      }
    })

    it('an acknowledgement timeout only matters while one is awaited', () => {
      for (const phase of PHASES.filter((p) => p !== 'announced')) {
        const state = inPhase(phase)
        expect(micReduce(state, { type: 'ack-timeout' })).toEqual({ state, effects: [] })
      }
    })

    it('a late attach or failure for an attempt already given up is ignored', () => {
      for (const phase of PHASES.filter((p) => !ON_STREAM.includes(p))) {
        const state = inPhase(phase)
        expect(micReduce(state, FAILED)).toEqual({ state, effects: [] })
        expect(micReduce(state, ATTACHED)).toEqual({ state, effects: [] })
      }
      // Attached twice: the second is not a second announcement.
      const announced = inPhase('announced')
      expect(micReduce(announced, ATTACHED)).toEqual({ state: announced, effects: [] })
    })
  })

  describe('coming back to the stream', () => {
    it('when the stream comes back after going down', () => {
      const fellBack = run([DOWN], inPhase('stream'))
      expect(fellBack.state.phase).toBe('local')

      const back = run([LIVE], fellBack.state)
      expect(back.state.phase).toBe('releasing')
      expect(kinds(back.effects)).toEqual(['stop-local'])

      const again = run([RELEASED, ATTACHED, LISTENING, LISTENING], back.state)
      expect(kinds(again.effects)).toEqual(['acquire', 'send:true'])
      // The acknowledgement of the `active: false` sent on the way down is
      // not waited for on the new stream, so one confirmation is enough.
      expect(run([RELEASED, ATTACHED, LISTENING], back.state).state.phase).toBe('stream')
      expect(again.state.phase).toBe('stream')
    })

    it('after a refusal: on the next stream, not on this one', () => {
      const refused = run([NOT_LISTENING], inPhase('stream'))
      expect(refused.state.blocked).toBe(true)
      const cycled = run([DOWN, LIVE], refused.state)
      expect(cycled.state.blocked).toBe(false)
      expect(cycled.state.phase).toBe('releasing')
    })

    it('after a reconnect that offers it again', () => {
      const dropped = run([{ type: 'socket-closed' }], inPhase('stream'))
      expect(dropped.state.phase).toBe('local')
      const back = run([READY_MIC, LIVE, RELEASED, ATTACHED, LISTENING], dropped.state)
      expect(back.state.phase).toBe('stream')
      expect(kinds(back.effects)).toEqual(['stop-local', 'acquire', 'send:true'])
    })

    it('stays on the detector after a reconnect to a server that no longer offers it', () => {
      const { state } = run([{ type: 'socket-closed' }, { type: 'ready', mic: false }, LIVE], inPhase('stream'))
      expect(state.phase).toBe('local')
    })

    it('switching hands-free off and on is another try', () => {
      const refused = run([NOT_LISTENING], inPhase('stream'))
      const retried = run([UNWANT, RELEASED, WANT], refused.state)
      expect(retried.state.phase).toBe('acquiring')
    })
  })

  describe('turning hands-free off', () => {
    it.each([
      ['off', []],
      ['local', ['stop-local']],
      ['releasing', []],
      // Nothing was announced, so there is nothing for the server to stop.
      ['acquiring', ['detach']],
      ['announced', ['detach', 'send:false']],
      ['stream', ['detach', 'send:false']],
    ] as [MicPhase, string[]][])('from %s', (phase, expected) => {
      const before = inPhase(phase)
      const step = micReduce({ ...before, wanted: true }, UNWANT)
      expect(step.state.phase).toBe('off')
      expect(micModeOf(step.state)).toBe('off')
      expect(kinds(step.effects)).toEqual(expected)
      // Nothing is started on the way out, in any state.
      expect(kinds(step.effects)).not.toContain('start-local')
      expect(kinds(step.effects)).not.toContain('acquire')
      expect(kinds(step.effects)).not.toContain('send:true')
    })

    it.each(PHASES)('shutdown from %s lets go of everything the same way', (phase) => {
      const before = inPhase(phase)
      const off = micReduce({ ...before, wanted: true }, UNWANT)
      const shut = micReduce(before, { type: 'shutdown' })
      expect(kinds(shut.effects)).toEqual(before.wanted ? kinds(off.effects) : [])
      expect(shut.state).toMatchObject({ phase: 'off', wanted: false, live: false, serverMic: false, unacked: 0 })
    })

    it('stays off whatever arrives afterwards', () => {
      const off = run([UNWANT], inPhase('stream')).state
      const later = run([LISTENING, ATTACHED, RELEASED, DOWN, LIVE, { type: 'visible' }, { type: 'ack-timeout' }], off)
      expect(later.state.phase).toBe('off')
      // The stray `listening: true` is answered; nothing else happens.
      expect(kinds(later.effects)).toEqual(['send:false'])
    })
  })

  describe('a tab hidden for a long time', () => {
    const HIDDEN: MicEvent = { type: 'hidden' }
    const VISIBLE: MicEvent = { type: 'visible' }

    it.each(TOLD)('hands over to the browser detector, from %s: track off, server told, then the detector', (phase) => {
      const step = micReduce(inPhase(phase), HIDDEN)
      // The same order as any other stream → local hand-over.
      expect(kinds(step.effects)).toEqual(['detach', 'send:false', 'start-local'])
      expect(step.state.phase).toBe('local')
      expect(micModeOf(step.state)).toBe('local')
    })

    it('abandons an acquire in flight for the detector, with nothing to tell the server', () => {
      const step = micReduce(inPhase('acquiring'), HIDDEN)
      expect(kinds(step.effects)).toEqual(['detach', 'start-local'])
      expect(step.state.phase).toBe('local')
    })

    it('a detector still letting go is started again, not waited for', () => {
      const step = micReduce(inPhase('releasing'), HIDDEN)
      expect(kinds(step.effects)).toEqual(['start-local'])
      expect(step.state.phase).toBe('local')
    })

    it.each(PHASES.filter((p) => p !== 'off'))('hands-free that is on is never left with nobody listening, from %s', (phase) => {
      const { state } = micReduce(inPhase(phase), HIDDEN)
      expect(state.wanted).toBe(true)
      expect(micModeOf(state)).toBe('local')
    })

    it('goes back onto the stream when the tab returns — detector released first', () => {
      const hidden = micReduce(inPhase('stream'), HIDDEN).state
      const back = micReduce(hidden, VISIBLE)
      expect(kinds(back.effects)).toEqual(['stop-local'])
      expect(back.state.phase).toBe('releasing')
      // Nothing is asked of the microphone until the detector has let go.
      // The first acknowledgement is of the `active: false` sent on hiding.
      const resumed = run([RELEASED, ATTACHED, NOT_LISTENING, LISTENING], back.state)
      expect(kinds(resumed.effects)).toEqual(['acquire', 'send:true'])
      expect(resumed.state.phase).toBe('stream')
    })

    it('does not go back if the stream is no longer eligible when the tab returns', () => {
      const hidden = micReduce(inPhase('stream'), HIDDEN).state
      for (const gone of [[DOWN], [{ type: 'socket-closed' }], [{ type: 'socket-closed' }, { type: 'ready', mic: false }, LIVE]] as MicEvent[][]) {
        const back = run([...gone, VISIBLE], hidden)
        expect(back.state.phase).toBe('local')
        // The detector was running all along and is not restarted.
        expect(back.effects).toEqual([])
      }
    })

    it('does not go back to a stream microphone that had failed before the tab was hidden', () => {
      const refused = run([NOT_LISTENING], inPhase('stream')).state
      const back = run([HIDDEN, VISIBLE], refused)
      expect(back.state.phase).toBe('local')
      expect(back.effects).toEqual([])
    })

    it('leaves a running browser detector alone, and does not move it onto the stream', () => {
      const hiddenLocal = run([WANT, HIDDEN])
      expect(hiddenLocal.state.phase).toBe('local')
      const offered = run(STREAM_UP, hiddenLocal.state)
      expect(offered.state.phase).toBe('local')
      expect(kinds(offered.effects)).toEqual([])
      // …until the tab is back.
      expect(run([VISIBLE], offered.state).state.phase).toBe('releasing')
    })

    it('stays on the detector if the stream dies while hidden', () => {
      const step = micReduce(micReduce(inPhase('stream'), HIDDEN).state, DOWN)
      expect(step.state.phase).toBe('local')
      expect(step.effects).toEqual([])
    })

    it('hands-free switched on in a long-hidden tab is the browser detector, as without the stream microphone', () => {
      const { state, effects } = run([...STREAM_UP, HIDDEN, WANT])
      expect(state.phase).toBe('local')
      expect(kinds(effects)).toEqual(['start-local'])
      // …and the stream microphone once the tab is looked at.
      const back = run([VISIBLE, RELEASED], state)
      expect(kinds(back.effects)).toEqual(['stop-local', 'acquire'])
    })

    it('switching hands-free off while hidden stops the detector and nothing else', () => {
      const hidden = micReduce(inPhase('stream'), HIDDEN).state
      const off = micReduce(hidden, UNWANT)
      expect(kinds(off.effects)).toEqual(['stop-local'])
      expect(off.state.phase).toBe('off')
    })
  })

  describe('acknowledgements', () => {
    it('an acknowledgement of an older frame does not count as the answer to the latest', () => {
      // on (true), off (false), on (true) — before any acknowledgement.
      const quick = run([UNWANT, WANT, ATTACHED], inPhase('announced'))
      expect(quick.state).toMatchObject({ phase: 'announced', unacked: 3 })

      // true, false: both about frames that have since been superseded.
      const stale = run([LISTENING, NOT_LISTENING], quick.state)
      expect(stale.state).toMatchObject({ phase: 'announced', unacked: 1 })
      expect(stale.effects).toEqual([])

      expect(run([LISTENING], stale.state).state.phase).toBe('stream')
    })

    it('the acknowledgement of a stop is not mistaken for the server stopping on its own', () => {
      // stream → hidden (false sent, detector on) → visible → detector
      // released → attached (true sent).
      const resumed = run([{ type: 'hidden' }, { type: 'visible' }, RELEASED, ATTACHED], inPhase('stream'))
      expect(resumed.state).toMatchObject({ phase: 'announced', unacked: 2 })
      const acked = run([NOT_LISTENING, LISTENING], resumed.state)
      expect(acked.state.phase).toBe('stream')
      expect(acked.state.blocked).toBe(false)
    })

    it('once confirmed, any `listening: false` is the server stopping', () => {
      const { state } = run([NOT_LISTENING], inPhase('stream'))
      expect(state).toMatchObject({ phase: 'local', blocked: true })
    })

    it('`listening: false` while nothing is announced changes nothing', () => {
      for (const phase of PHASES.filter((p) => !TOLD.includes(p))) {
        const state = inPhase(phase)
        const step = micReduce(state, NOT_LISTENING)
        expect(step.state.phase).toBe(phase)
        expect(step.effects).toEqual([])
      }
    })

    it('a timeout writes off what was outstanding', () => {
      const { state } = run([{ type: 'ack-timeout' }], inPhase('announced'))
      // Only the `active: false` it just sent.
      expect(state.unacked).toBe(1)
    })
  })
})

// ── MicSession: the effects, against a fake browser and a fake server ──────

function setup() {
  const log: string[] = []
  const modes: MicMode[] = []
  const world = {
    socketOpen: true,
    /** The detector has been started and not told to stop. */
    localOn: false,
    /** …or was told to stop but still has the microphone. */
    localHolding: false,
    acquiring: false,
    /** A microphone track is on the stream. */
    track: false,
    /** The server's side: is it listening? Set by the frames it receives. */
    serverListening: false,
    /** `mic_state` frames on their way back. */
    acks: [] as boolean[],
  }
  const session: MicSession = new MicSession({
    startLocal: () => {
      log.push('start-local')
      world.localOn = true
      world.localHolding = true
    },
    stopLocal: () => {
      log.push('stop-local')
      world.localOn = false
    },
    acquire: () => {
      log.push('acquire')
      world.acquiring = true
    },
    detach: () => {
      log.push('detach')
      world.acquiring = false
      world.track = false
    },
    send: (active) => {
      if (!world.socketOpen) return false
      log.push(`send:${active}`)
      world.serverListening = active
      world.acks.push(active)
      return true
    },
    onChange: (mode) => modes.push(mode),
  })
  const act = {
    /** The detector's effect cleanup ran: what the hook reports after a commit. */
    release() {
      if (world.localOn || !world.localHolding) return
      world.localHolding = false
      session.localReleased()
    },
    /** getUserMedia resolved and the track went onto the sender. */
    grant() {
      if (!world.acquiring) return
      world.acquiring = false
      world.track = true
      session.trackAttached()
    },
    deny() {
      if (!world.acquiring) return
      world.acquiring = false
      session.trackFailed()
    },
    /** Deliver the next acknowledgement, or all of them (answers included). */
    ack(all = true) {
      do {
        const next = world.acks.shift()
        if (next === undefined) return
        session.micState(next)
      } while (all)
    },
    /** The server stops listening on its own and says so. */
    serverStops() {
      world.serverListening = false
      world.acks.push(false)
    },
    closeSocket() {
      world.socketOpen = false
      world.serverListening = false
      world.acks = []
      session.socketClosed()
    },
    openSocket(mic: boolean) {
      world.socketOpen = true
      world.serverListening = false
      world.acks = []
      session.ready(mic)
    },
    /** Everything in flight lands. */
    settle() {
      for (let i = 0; i < 6; i++) {
        act.release()
        act.grant()
        act.ack()
      }
    },
  }
  /** hands-free on, on a live stream whose server offered the microphone. */
  const goStream = () => {
    session.ready(true)
    session.setLive(true)
    session.setWanted(true)
    act.settle()
    expect(session.mode).toBe('stream')
  }
  return { session, log, modes, world, act, goStream }
}

/** What must hold after every single step, whatever the order of events. */
function checkInvariants(s: ReturnType<typeof setup>) {
  const { session, world } = s
  // One consumer of the microphone for turn-taking — never both.
  expect(world.localHolding && (world.track || world.acquiring)).toBe(false)
  expect(world.localHolding && world.serverListening).toBe(false)
  // The server is only ever asked to listen to a track that is there.
  if (world.serverListening) expect(world.track).toBe(true)
  switch (session.mode) {
    case 'stream':
      expect(world.track).toBe(true)
      // …unless it has just stopped on its own and the news is in flight.
      expect(world.serverListening || world.acks.includes(false)).toBe(true)
      expect(world.localHolding).toBe(false)
      break
    case 'local':
      expect(world.localOn).toBe(true)
      expect(world.track).toBe(false)
      break
    case 'off':
      break
  }
}

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('MicSession', () => {
  it('against a server without media.mic: the detector, and not one frame', () => {
    const s = setup()
    s.session.ready(false)
    s.session.setLive(true)
    s.session.setWanted(true)
    s.act.settle()
    expect(s.session.mode).toBe('local')
    s.session.setWanted(false)
    s.act.settle()
    expect(s.log).toEqual(['start-local', 'stop-local'])
    expect(s.modes).toEqual(['local', 'off'])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('goes onto the stream in order: acquire, announce, and only then `stream`', () => {
    const s = setup()
    s.session.ready(true)
    s.session.setLive(true)
    s.session.setWanted(true)
    expect(s.log).toEqual(['acquire'])
    s.act.grant()
    expect(s.log).toEqual(['acquire', 'send:true'])
    expect(s.session.mode).toBe('off')
    s.act.ack()
    expect(s.session.mode).toBe('stream')
    expect(s.modes).toEqual(['stream'])
  })

  it('hands over from the detector: released before the microphone is asked for again', () => {
    const s = setup()
    s.session.setWanted(true)
    expect(s.session.mode).toBe('local')
    s.session.ready(true)
    s.session.setLive(true)
    expect(s.log).toEqual(['start-local', 'stop-local'])
    // Granting cannot happen: nothing was acquired while the detector holds on.
    s.act.grant()
    expect(s.world.acquiring).toBe(false)
    s.act.release()
    s.act.grant()
    s.act.ack()
    expect(s.log).toEqual(['start-local', 'stop-local', 'acquire', 'send:true'])
    expect(s.modes).toEqual(['local', 'off', 'stream'])
  })

  it('hands back to the detector: track off and server told before it starts', () => {
    const s = setup()
    s.goStream()
    s.log.length = 0
    s.session.setLive(false)
    expect(s.log).toEqual(['detach', 'send:false', 'start-local'])
    expect(s.session.mode).toBe('local')
    checkInvariants(s)
  })

  it('returns to the stream when it comes back', () => {
    const s = setup()
    s.goStream()
    s.session.setLive(false)
    s.act.settle()
    expect(s.session.mode).toBe('local')
    s.session.setLive(true)
    s.act.settle()
    expect(s.session.mode).toBe('stream')
    expect(s.modes).toEqual(['stream', 'local', 'off', 'stream'])
  })

  it('falls back when the server stops listening unasked, and stays there on this stream', () => {
    const s = setup()
    s.goStream()
    s.act.serverStops()
    s.act.settle()
    expect(s.session.mode).toBe('local')
    expect(s.world.track).toBe(false)
    expect(s.world.serverListening).toBe(false)
  })

  it('falls back when the acknowledgement never comes', () => {
    const s = setup()
    s.session.ready(true)
    s.session.setLive(true)
    s.session.setWanted(true)
    s.act.grant()
    s.world.acks = [] // the server says nothing
    vi.advanceTimersByTime(MIC_ACK_TIMEOUT_MS - 1)
    expect(s.session.phase).toBe('announced')
    vi.advanceTimersByTime(1)
    expect(s.session.mode).toBe('local')
    expect(s.log.slice(-3)).toEqual(['detach', 'send:false', 'start-local'])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not time out once the server has answered', () => {
    const s = setup()
    s.goStream()
    vi.advanceTimersByTime(MIC_ACK_TIMEOUT_MS * 3)
    expect(s.session.mode).toBe('stream')
  })

  it('an announcement that could not be sent is a failure, not a wait', () => {
    const s = setup()
    s.session.ready(true)
    s.session.setLive(true)
    s.session.setWanted(true)
    s.world.socketOpen = false
    s.act.grant()
    expect(s.session.mode).toBe('local')
    expect(s.world.track).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('falls back when the microphone is denied on the stream path', () => {
    const s = setup()
    s.session.ready(true)
    s.session.setLive(true)
    s.session.setWanted(true)
    s.act.deny()
    expect(s.session.mode).toBe('local')
    expect(s.log).toEqual(['acquire', 'detach', 'start-local'])
  })

  it('turning hands-free off releases everything, from every state', () => {
    const stops: ((s: ReturnType<typeof setup>) => void)[] = [
      () => {},
      (s) => s.session.ready(true),
      (s) => s.session.setLive(true), // releasing
      (s) => s.act.release(), // acquiring
      (s) => s.act.grant(), // announced
      (s) => s.act.ack(), // stream
    ]
    for (let upTo = 0; upTo < stops.length; upTo++) {
      const s = setup()
      s.session.setWanted(true)
      for (const step of stops.slice(0, upTo + 1)) step(s)
      const wasTold = s.world.serverListening
      s.log.length = 0
      s.session.setWanted(false)
      expect(s.session.mode).toBe('off')
      expect(s.world.localOn).toBe(false)
      expect(s.world.track).toBe(false)
      expect(s.world.acquiring).toBe(false)
      expect(s.world.serverListening).toBe(false)
      expect(s.log.includes('send:false')).toBe(wasTold)
      // And nothing comes back to life afterwards.
      s.act.settle()
      vi.advanceTimersByTime(MIC_ACK_TIMEOUT_MS * 2)
      expect(s.session.mode).toBe('off')
      expect(s.world.localHolding || s.world.track || s.world.acquiring || s.world.serverListening).toBe(false)
    }
  })

  it('a socket close takes the microphone off the stream; the reconnect puts it back', () => {
    const s = setup()
    s.goStream()
    s.act.closeSocket()
    expect(s.session.mode).toBe('local')
    expect(s.world.track).toBe(false)
    s.act.openSocket(true)
    s.session.setLive(true)
    s.act.settle()
    expect(s.session.mode).toBe('stream')
  })

  describe('a hidden tab', () => {
    it('keeps the stream microphone for a short absence', () => {
      const s = setup()
      s.goStream()
      s.session.setTabHidden(true)
      vi.advanceTimersByTime(MIC_HIDDEN_RELEASE_MS - 1)
      s.session.setTabHidden(false)
      vi.advanceTimersByTime(MIC_HIDDEN_RELEASE_MS)
      expect(s.session.mode).toBe('stream')
      expect(s.world.track).toBe(true)
    })

    it('takes it off after a long one, and puts it back on return', () => {
      const s = setup()
      s.goStream()
      s.log.length = 0
      s.session.setTabHidden(true)
      // A second `hidden` does not push the deadline back.
      vi.advanceTimersByTime(MIC_HIDDEN_RELEASE_MS / 2)
      s.session.setTabHidden(true)
      vi.advanceTimersByTime(MIC_HIDDEN_RELEASE_MS / 2)
      // Handed to the browser detector, in hand-over order — not paused.
      expect(s.log).toEqual(['detach', 'send:false', 'start-local'])
      expect(s.session.mode).toBe('local')
      expect(s.world.localOn).toBe(true)
      expect(s.world.track).toBe(false)
      checkInvariants(s)
      s.act.settle()
      expect(s.world.serverListening).toBe(false)
      expect(s.session.mode).toBe('local')

      s.session.setTabHidden(false)
      s.act.settle()
      expect(s.session.mode).toBe('stream')
      expect(s.modes).toEqual(['stream', 'local', 'off', 'stream'])
      checkInvariants(s)
    })

    it('with hands-free on, somebody is listening whenever things have settled — hidden or not', () => {
      const s = setup()
      s.goStream()
      const steps: (() => void)[] = [
        () => s.session.setTabHidden(true),
        () => vi.advanceTimersByTime(MIC_HIDDEN_RELEASE_MS),
        () => s.session.setLive(false),
        () => s.session.setLive(true),
        () => s.session.setTabHidden(false),
        () => s.session.setTabHidden(true),
        () => vi.advanceTimersByTime(MIC_HIDDEN_RELEASE_MS),
        () => s.act.closeSocket(),
        () => s.act.openSocket(true),
        () => s.session.setLive(true),
        () => s.session.setTabHidden(false),
      ]
      for (const [i, step] of steps.entries()) {
        step()
        s.act.settle()
        expect(s.session.mode, `after step ${i}`).not.toBe('off')
        checkInvariants(s)
      }
      expect(s.session.mode).toBe('stream')
    })

    it('shutdown cancels the clock', () => {
      const s = setup()
      s.goStream()
      s.session.setTabHidden(true)
      s.session.shutdown()
      expect(vi.getTimerCount()).toBe(0)
    })
  })

  it('shutdown lets go, and the session can be driven again (StrictMode)', () => {
    const s = setup()
    s.goStream()
    s.session.shutdown()
    expect(s.session.mode).toBe('off')
    expect(s.world.track).toBe(false)
    expect(s.world.serverListening).toBe(false)
    expect(vi.getTimerCount()).toBe(0)

    s.session.setWanted(true)
    expect(s.session.mode).toBe('local')
  })

  it('announces a mode once, not on every event', () => {
    const s = setup()
    s.session.setWanted(true)
    s.session.setWanted(true)
    s.session.ready(false)
    s.session.micState(false)
    expect(s.modes).toEqual(['local'])
  })

  it('holds its invariants through any order of events', () => {
    // A fixed-seed walk: every run takes the same path, so a failure is
    // reproducible from the step number alone.
    let seed = 0x2f6e2b1
    const random = () => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0
      return seed / 0x100000000
    }
    const s = setup()
    let wanted = false
    let live = false
    const moves: (() => void)[] = [
      () => s.session.setWanted((wanted = !wanted)),
      () => s.session.setWanted((wanted = !wanted)),
      () => s.session.setLive((live = !live)),
      () => s.act.release(),
      () => s.act.release(),
      () => s.act.grant(),
      () => s.act.grant(),
      () => s.act.deny(),
      () => s.act.ack(false),
      () => s.act.ack(false),
      () => s.act.ack(),
      () => s.act.serverStops(),
      () => s.act.closeSocket(),
      () => {
        live = false
        s.session.setLive(false)
        s.act.openSocket(random() < 0.8)
      },
      () => s.session.setTabHidden(true),
      () => s.session.setTabHidden(false),
      () => vi.advanceTimersByTime(MIC_ACK_TIMEOUT_MS),
      () => vi.advanceTimersByTime(MIC_HIDDEN_RELEASE_MS),
      // A track that ended: reported only while one is on the stream.
      () => {
        if (s.world.track) s.session.trackFailed()
      },
    ]
    const trail: string[] = []
    for (let step = 0; step < 20_000; step++) {
      const move = Math.floor(random() * moves.length)
      moves[move]()
      trail.push(`${move}:${s.session.phase}`)
      try {
        checkInvariants(s)
      } catch (err) {
        throw new Error(`step ${step} (${trail.slice(-12).join(' ')}): ${(err as Error).stack}`)
      }
    }

    // And from wherever that ended: off means off.
    s.session.setWanted(false)
    s.act.settle()
    expect(s.session.mode).toBe('off')
    expect(s.world.localHolding || s.world.track || s.world.acquiring).toBe(false)
    if (s.world.socketOpen) expect(s.world.serverListening).toBe(false)
  })

  it('reaches the stream from any reachable state once the conditions hold', () => {
    let seed = 0x51f15e
    const random = () => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0
      return seed / 0x100000000
    }
    for (let round = 0; round < 300; round++) {
      const s = setup()
      const moves: (() => void)[] = [
        () => s.session.setWanted(random() < 0.5),
        () => s.session.setLive(random() < 0.5),
        () => s.act.release(),
        () => s.act.grant(),
        () => s.act.deny(),
        () => s.act.ack(false),
        () => s.act.serverStops(),
        () => s.act.closeSocket(),
        () => s.act.openSocket(random() < 0.5),
        () => vi.advanceTimersByTime(MIC_ACK_TIMEOUT_MS),
      ]
      for (let step = 0; step < 25; step++) moves[Math.floor(random() * moves.length)]()

      // The stream comes back, offering the microphone, with hands-free on.
      s.session.setLive(false)
      s.act.openSocket(true)
      s.session.setLive(true)
      s.session.setTabHidden(false)
      s.session.setWanted(true)
      s.act.settle()
      expect(s.session.mode, `round ${round}`).toBe('stream')
      checkInvariants(s)

      // …and to the detector when it goes.
      s.session.setLive(false)
      s.act.settle()
      expect(s.session.mode, `round ${round}`).toBe('local')
      checkInvariants(s)
    }
  })
})
