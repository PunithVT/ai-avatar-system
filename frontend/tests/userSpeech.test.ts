import { describe, expect, it } from 'vitest'
import { ReplyGate, initialTurnState, turnReducer, type TurnState } from '@/lib/chat/turnState'
import {
  initialSpeechState, isPartialTranscription, speechReduce, speechTurnStep,
  type SpeechEvent, type SpeechState, type SpeechTurnEvent,
} from '@/lib/chat/userSpeech'
import type { WsMessage } from '@/lib/types'

const run = (events: SpeechEvent[], from: SpeechState = initialSpeechState) => events.reduce(speechReduce, from)

const STARTED: SpeechEvent = { type: 'started' }
const partial = (text: string): SpeechEvent => ({ type: 'partial', text })

describe('isPartialTranscription', () => {
  it('is true only for final: false', () => {
    expect(isPartialTranscription({ final: false })).toBe(true)
  })

  it.each([
    ['no key — every server before the stream microphone', {}],
    ['final: true', { final: true }],
    ['a falsy value that is not false', { final: 0 } as unknown as { final?: boolean }],
    ['null', { final: null } as unknown as { final?: boolean }],
  ])('is the finished transcript for %s', (_name, frame) => {
    expect(isPartialTranscription(frame)).toBe(false)
  })
})

describe('speechReduce', () => {
  it('shows the user as speaking from started to stopped', () => {
    expect(run([STARTED]).speaking).toBe(true)
    expect(run([STARTED, { type: 'stopped', reason: 'turn' }]).speaking).toBe(false)
    expect(run([STARTED, { type: 'stopped', reason: 'ignored' }]).speaking).toBe(false)
  })

  it('each running transcript replaces the one before', () => {
    const s = run([STARTED, partial('The birch'), partial('The birch canoe'), partial('The birch canoe slid')])
    expect(s.pending).toBe('The birch canoe slid')
  })

  it('a blank running transcript is no bubble', () => {
    expect(run([STARTED, partial('   ')]).pending).toBeNull()
    expect(run([STARTED, partial('The'), partial('')]).pending).toBeNull()
  })

  it('keeps the pending text between `stopped, turn` and the final transcript', () => {
    const s = run([STARTED, partial('The birch canoe'), { type: 'stopped', reason: 'turn' }])
    expect(s).toEqual({ speaking: false, pending: 'The birch canoe', open: true, awaitingFinal: true })
    // A last running transcript can still land in that window.
    expect(run([partial('The birch canoe slid')], s).pending).toBe('The birch canoe slid')
  })

  it('the final transcript removes the pending text', () => {
    const s = run([STARTED, partial('The birch canoe'), { type: 'stopped', reason: 'turn' }, { type: 'final' }])
    expect(s).toEqual(initialSpeechState)
  })

  it('`ignored` removes the pending text', () => {
    expect(run([STARTED, partial('uh'), { type: 'stopped', reason: 'ignored' }])).toEqual(initialSpeechState)
  })

  it('a `stopped` without a reason is not a turn', () => {
    expect(run([STARTED, partial('uh'), { type: 'stopped', reason: undefined }])).toEqual(initialSpeechState)
  })

  it('drops a running transcript that arrives after the final one', () => {
    const done = run([STARTED, partial('The birch'), { type: 'stopped', reason: 'turn' }, { type: 'final' }])
    expect(run([partial('The birch canoe')], done)).toBe(done)
  })

  it('drops a running transcript that arrives after `ignored`, or with no utterance open', () => {
    const ignored = run([STARTED, { type: 'stopped', reason: 'ignored' }])
    expect(run([partial('late')], ignored).pending).toBeNull()
    expect(run([partial('unannounced')]).pending).toBeNull()
  })

  it('a new utterance starts clean', () => {
    const s = run([STARTED, partial('first'), STARTED])
    expect(s).toEqual({ speaking: true, pending: null, open: true, awaitingFinal: false })
  })

  it('reset clears everything — socket dropped, the stream microphone went away', () => {
    expect(run([STARTED, partial('The birch'), { type: 'reset' }])).toEqual(initialSpeechState)
    expect(run([partial('late')], run([STARTED, { type: 'reset' }])).pending).toBeNull()
  })

  it('returns the same object when nothing changed (no re-render)', () => {
    expect(speechReduce(initialSpeechState, { type: 'reset' })).toBe(initialSpeechState)
    expect(speechReduce(initialSpeechState, { type: 'final' })).toBe(initialSpeechState)
    const s = run([STARTED, partial('same')])
    expect(speechReduce(s, partial('same'))).toBe(s)
  })
})

/**
 * The transcript as ChatInterface assembles it from server frames: the
 * running transcript goes to the pending bubble, the final one to the
 * message list, and only the final one touches the reply gate and the turn.
 */
function transcript() {
  const gate = new ReplyGate()
  const view = {
    messages: [] as string[],
    speech: initialSpeechState,
    turn: initialTurnState as TurnState,
    gate,
  }
  const receive = (...frames: WsMessage[]) => {
    for (const frame of frames) {
      if (frame.type === 'transcription' && isPartialTranscription(frame)) {
        view.speech = speechReduce(view.speech, { type: 'partial', text: frame.text })
        continue
      }
      if (!gate.admit(frame)) continue
      if (frame.type === 'transcription') {
        view.speech = speechReduce(view.speech, { type: 'final' })
        view.messages.push(frame.text)
        view.turn = turnReducer(view.turn, { type: 'transcription' })
      } else if (frame.type === 'user_speech') {
        view.speech = speechReduce(
          view.speech,
          frame.state === 'started' ? { type: 'started' } : { type: 'stopped', reason: frame.reason },
        )
      } else if (frame.type === 'token') {
        view.turn = turnReducer(view.turn, { type: 'token', token: frame.token })
      }
    }
  }
  /** Everything on screen that reads as the user's words. */
  const bubbles = () => [...view.messages, ...(view.speech.pending ? [`(pending) ${view.speech.pending}`] : [])]
  return { view, receive, bubbles }
}

const started: WsMessage = { type: 'user_speech', state: 'started' }
const stoppedTurn: WsMessage = { type: 'user_speech', state: 'stopped', reason: 'turn' }
const stoppedIgnored: WsMessage = { type: 'user_speech', state: 'stopped', reason: 'ignored' }
const running = (text: string): WsMessage => ({ type: 'transcription', text, final: false })

describe('a spoken turn on the stream microphone', () => {
  it('is one bubble throughout: pending while spoken, a message once final', () => {
    const { receive, bubbles } = transcript()
    receive(started)
    expect(bubbles()).toEqual([])

    receive(running('Glue the'))
    expect(bubbles()).toEqual(['(pending) Glue the'])
    receive(running('Glue the sheet'))
    expect(bubbles()).toEqual(['(pending) Glue the sheet'])

    receive(stoppedTurn)
    expect(bubbles()).toEqual(['(pending) Glue the sheet'])

    receive({ type: 'transcription', text: 'Glue the sheet to the dark blue background.' })
    expect(bubbles()).toEqual(['Glue the sheet to the dark blue background.'])
  })

  it('accepts final: true as the finished transcript', () => {
    const { receive, bubbles } = transcript()
    receive(started, running('Glue'), stoppedTurn, { type: 'transcription', text: 'Glue the sheet.', final: true })
    expect(bubbles()).toEqual(['Glue the sheet.'])
  })

  it('a running transcript that arrives after the final one does not bring a bubble back', () => {
    const { receive, bubbles } = transcript()
    receive(
      started, running('Glue the'), stoppedTurn,
      { type: 'transcription', text: 'Glue the sheet.' },
      running('Glue the sheet'),
    )
    expect(bubbles()).toEqual(['Glue the sheet.'])
  })

  it('an ignored utterance leaves nothing behind', () => {
    const { receive, bubbles, view } = transcript()
    receive(started, running('uh'), stoppedIgnored)
    expect(bubbles()).toEqual([])
    expect(view.speech).toEqual(initialSpeechState)
  })

  it('two turns in a row are two messages, never a stale pending bubble', () => {
    const { receive, bubbles } = transcript()
    receive(started, running('one'), stoppedTurn, { type: 'transcription', text: 'One.' })
    receive(started)
    expect(bubbles()).toEqual(['One.'])
    receive(running('two'), stoppedTurn, { type: 'transcription', text: 'Two.' })
    expect(bubbles()).toEqual(['One.', 'Two.'])
  })

  it('a running transcript never touches the turn: no typing dots, no reset of streamed text', () => {
    const { receive, view } = transcript()
    receive({ type: 'token', token: 'Still ' }, { type: 'token', token: 'talking' })
    const before = view.turn
    receive(started, running('wait'), running('wait a moment'))
    expect(view.turn).toBe(before)
    expect(view.turn.streamingContent).toBe('Still talking')
    expect(view.turn.isTyping).toBe(false)
  })

  it('a running transcript does not reopen the reply gate; the final one does', () => {
    const { receive, view } = transcript()
    view.gate.close() // the user talked over a reply
    receive(started, running('wait'))
    expect(view.gate.isOpen).toBe(false)
    // The old reply's tokens, still in flight, stay out.
    receive({ type: 'token', token: 'stale' })
    expect(view.turn.streamingContent).toBe('')

    receive(stoppedTurn, { type: 'transcription', text: 'Wait a moment.' })
    expect(view.gate.isOpen).toBe(true)
  })

  it('against a server without the stream microphone, a transcription is the message it always was', () => {
    const { receive, bubbles, view } = transcript()
    receive({ type: 'transcription', text: 'Hello there.' })
    expect(bubbles()).toEqual(['Hello there.'])
    expect(view.speech).toBe(initialSpeechState)
    expect(view.turn.isTyping).toBe(true)
  })
})

describe('ReplyGate and running transcripts', () => {
  it('admits a running transcript without opening', () => {
    const gate = new ReplyGate()
    gate.close()
    expect(gate.admit({ type: 'transcription', final: false })).toBe(true)
    expect(gate.isOpen).toBe(false)
    expect(gate.admit({ type: 'token' })).toBe(false)
  })

  it.each([[undefined], [true]])('opens on a finished transcript (final: %s)', (final) => {
    const gate = new ReplyGate()
    gate.close()
    expect(gate.admit({ type: 'transcription', final })).toBe(true)
    expect(gate.isOpen).toBe(true)
  })

  it('is unmoved by the stream microphone’s own frames', () => {
    const gate = new ReplyGate()
    gate.close()
    for (const type of ['mic_state', 'user_speech']) expect(gate.admit({ type })).toBe(true)
    expect(gate.isOpen).toBe(false)
  })
})

// ── The microphone turn: every way out of "waiting for the transcript" ─────

describe('speechTurnStep', () => {
  const speech = (state: 'started' | 'stopped', reason: 'turn' | 'ignored' | undefined, stream: boolean): SpeechTurnEvent =>
    ({ type: 'user-speech', state, reason, stream })

  it('`stopped, turn` on the stream microphone begins a turn that is pending its transcript', () => {
    expect(speechTurnStep(false, speech('stopped', 'turn', true))).toEqual({
      pending: true, speech: { type: 'stopped', reason: 'turn' }, turn: 'begin',
    })
  })

  it.each([[true], [false]])('a non-turn `stopped` abandons a pending turn whatever the mode (stream: %s)', (stream) => {
    for (const reason of ['ignored', undefined] as const) {
      expect(speechTurnStep(true, speech('stopped', reason, stream))).toEqual({
        pending: false, speech: { type: 'stopped', reason: 'ignored' }, turn: 'abandon',
      })
    }
  })

  it('a non-turn `stopped` with nothing pending touches no turn — not a typed one, not an uploaded one', () => {
    for (const stream of [true, false]) {
      expect(speechTurnStep(false, speech('stopped', 'ignored', stream)).turn).toBeNull()
    }
  })

  it('`started` and `stopped, turn` are dropped once the stream microphone is not the mode', () => {
    for (const pending of [true, false]) {
      expect(speechTurnStep(pending, speech('started', undefined, false))).toEqual({ pending, speech: null, turn: null })
      expect(speechTurnStep(pending, speech('stopped', 'turn', false))).toEqual({ pending, speech: null, turn: null })
    }
  })

  it('`started` over a pending turn ends it: what follows is about the new utterance', () => {
    expect(speechTurnStep(true, speech('started', undefined, true))).toEqual({
      pending: false, speech: { type: 'started' }, turn: 'barge-in',
    })
  })

  it.each(['stop', 'interrupted', 'error'] as const)('%s clears a pending turn: bubble and spinner', (type) => {
    expect(speechTurnStep(true, { type })).toEqual({ pending: false, speech: { type: 'reset' }, turn: 'abandon' })
  })

  it.each(['stop', 'interrupted', 'error', 'turn-sent'] as const)(
    '%s with nothing pending leaves an utterance in progress alone',
    (type) => {
      expect(speechTurnStep(false, { type })).toEqual({ pending: false, speech: null, turn: null })
    },
  )

  it('a turn of the user’s own takes the bubble but keeps the spinner — it is the new turn’s now', () => {
    expect(speechTurnStep(true, { type: 'turn-sent' })).toEqual({ pending: false, speech: { type: 'reset' }, turn: null })
  })

  it.each(['mic-left', 'socket-dropped'] as const)('%s always clears the utterance, and a pending turn with it', (type) => {
    expect(speechTurnStep(true, { type })).toEqual({ pending: false, speech: { type: 'reset' }, turn: 'abandon' })
    expect(speechTurnStep(false, { type })).toEqual({ pending: false, speech: { type: 'reset' }, turn: null })
  })

  it('the final transcript ends the wait and leaves the turn running', () => {
    expect(speechTurnStep(true, { type: 'final' })).toEqual({ pending: false, speech: { type: 'final' }, turn: null })
  })
})

/**
 * ChatInterface's handling of a turn, with the player left out: the frames
 * it receives and the things the user can do, applied to the same reducers
 * through the same `speechTurnStep`.
 */
function chat() {
  const gate = new ReplyGate()
  const view = {
    messages: [] as string[],
    speech: initialSpeechState,
    turn: initialTurnState as TurnState,
    /** speechTurnRef */
    pending: false,
    /** awaitingReplyRef */
    awaitingReply: false,
    /** streamMic.isStream() */
    stream: true,
  }
  const turn = (action: Parameters<typeof turnReducer>[1]) => {
    view.turn = turnReducer(view.turn, action)
  }
  const speechTurn = (event: SpeechTurnEvent) => {
    const step = speechTurnStep(view.pending, event)
    view.pending = step.pending
    if (step.speech) view.speech = speechReduce(view.speech, step.speech)
    if (step.turn === 'abandon') {
      view.awaitingReply = false
      turn({ type: 'reset' })
      return null
    }
    return step.turn
  }
  const beginTurn = () => {
    gate.close()
    view.awaitingReply = true
  }
  const endTurnLocally = () => turn({ type: 'reset' })

  const receive = (...frames: WsMessage[]) => {
    for (const frame of frames) {
      if (frame.type === 'transcription' && isPartialTranscription(frame)) {
        view.speech = speechReduce(view.speech, { type: 'partial', text: frame.text })
        continue
      }
      if (!gate.admit(frame)) continue
      switch (frame.type) {
        case 'transcription':
          speechTurn({ type: 'final' })
          view.messages.push(frame.text)
          turn({ type: 'transcription' })
          break
        case 'user_speech': {
          const next = speechTurn({ type: 'user-speech', state: frame.state, reason: frame.reason, stream: view.stream })
          if (next === 'barge-in' && view.awaitingReply) {
            view.awaitingReply = false
            endTurnLocally()
            gate.close()
          } else if (next === 'begin') {
            beginTurn()
            turn({ type: 'audio-sent' })
          }
          break
        }
        case 'status':
          turn({ type: 'status', message: frame.message, playing: false })
          break
        case 'error':
          speechTurn({ type: 'error' })
          view.awaitingReply = false
          turn({ type: 'error' })
          break
        case 'interrupted':
          speechTurn({ type: 'interrupted' })
          if (!view.awaitingReply) endTurnLocally()
          break
        case 'video_chunk_end':
          view.awaitingReply = false
          turn({ type: 'chunks-end', playing: false })
          break
      }
    }
  }
  const user = {
    stop() {
      speechTurn({ type: 'stop' })
      view.awaitingReply = false
      endTurnLocally()
      gate.close()
    },
    sendText(text: string) {
      speechTurn({ type: 'turn-sent' })
      beginTurn()
      view.messages.push(text)
      turn({ type: 'text-sent' })
    },
    sendRecording() {
      speechTurn({ type: 'turn-sent' })
      beginTurn()
      turn({ type: 'audio-sent' })
    },
  }
  /**
   * The stream microphone stops being the mode. `isStream()` is false at
   * once; the effect that reacts to the mode runs with the next render, so
   * a frame can arrive in between (`settled: false`).
   */
  const leaveStream = ({ settled = true } = {}) => {
    view.stream = false
    if (settled) speechTurn({ type: 'mic-left' })
  }
  const dropSocket = () => {
    speechTurn({ type: 'socket-dropped' })
    view.awaitingReply = false
    gate.reset()
    turn({ type: 'reset' })
  }
  /** Nothing of a microphone turn is left on screen. */
  const expectCleared = () => {
    expect(view.speech.pending).toBeNull()
    expect(view.speech.speaking).toBe(false)
    expect(view.pending).toBe(false)
    expect(view.turn.isProcessing).toBe(false)
    expect(view.turn.isTyping).toBe(false)
    expect(view.awaitingReply).toBe(false)
  }
  return { view, receive, user, leaveStream, dropSocket, expectCleared }
}

const interrupted: WsMessage = { type: 'interrupted', message: 'Previous response interrupted' }
const failed: WsMessage = { type: 'error', message: 'Audio processing failed' }
const transcribing: WsMessage = { type: 'status', message: 'Transcribing audio…', stage: 'transcription' }
const final = (text: string): WsMessage => ({ type: 'transcription', text })

/** Spoken, taken for a turn, a running transcript on screen, the spinner up. */
function awaitingTranscript() {
  const c = chat()
  c.receive(started, running('Glue the'), stoppedTurn, transcribing, running('Glue the sheet'))
  expect(c.view.speech.pending).toBe('Glue the sheet')
  expect(c.view.turn.isProcessing).toBe(true)
  expect(c.view.awaitingReply).toBe(true)
  expect(c.view.pending).toBe(true)
  return c
}

describe('a microphone turn that never gets its transcript', () => {
  it('a wordless utterance: `ignored` takes the spinner down', () => {
    const c = awaitingTranscript()
    c.receive(stoppedIgnored)
    c.expectCleared()
    expect(c.view.messages).toEqual([])
  })

  it('`ignored` still takes the spinner down when the mode has changed in the meantime', () => {
    const c = awaitingTranscript()
    // Hands-free switched off, the stream dropped, the tab long hidden —
    // and the frame arrives before the mode change has been acted on.
    c.leaveStream({ settled: false })
    c.receive(stoppedIgnored)
    c.expectCleared()
  })

  it('leaving the stream microphone clears the turn by itself; a late `ignored` then changes nothing', () => {
    const c = awaitingTranscript()
    c.leaveStream()
    c.expectCleared()
    const before = { ...c.view }
    c.receive(stoppedIgnored)
    expect(c.view).toEqual(before)
  })

  it('a late `ignored` does not take down the spinner of a turn the user sent afterwards', () => {
    for (const send of [(c: ReturnType<typeof chat>) => c.user.sendText('Never mind.'), (c: ReturnType<typeof chat>) => c.user.sendRecording()]) {
      const c = awaitingTranscript()
      send(c)
      // The bubble of the cancelled utterance goes at once…
      expect(c.view.speech.pending).toBeNull()
      // …and neither frame about the cancelled turn touches the new one.
      c.receive(interrupted, stoppedIgnored)
      expect(c.view.turn.isProcessing).toBe(true)
      expect(c.view.awaitingReply).toBe(true)
    }
  })

  it('Stop before the transcript removes the pending bubble with the spinner', () => {
    const c = awaitingTranscript()
    c.user.stop()
    c.expectCleared()
    // The server's acknowledgement, and anything else late, finds nothing.
    c.receive(interrupted, running('Glue the sheet to'), stoppedIgnored)
    c.expectCleared()
    expect(c.view.messages).toEqual([])
  })

  it('Stop while the user is still speaking leaves the utterance alone', () => {
    const c = chat()
    c.receive(started, running('Glue the'))
    c.user.stop()
    expect(c.view.speech).toMatchObject({ speaking: true, pending: 'Glue the' })
    c.receive(stoppedTurn, final('Glue the sheet.'))
    expect(c.view.messages).toEqual(['Glue the sheet.'])
    expect(c.view.speech).toEqual(initialSpeechState)
  })

  it('`interrupted` before the transcript — the turn was cancelled — clears bubble and spinner', () => {
    const c = awaitingTranscript()
    c.receive(interrupted)
    c.expectCleared()
  })

  it('an error before the transcript clears both, with or without the `ignored` that follows it', () => {
    const c = awaitingTranscript()
    c.receive(failed)
    c.expectCleared()
    c.receive(stoppedIgnored)
    c.expectCleared()
  })

  it('the socket dropping clears both', () => {
    const c = awaitingTranscript()
    c.dropSocket()
    c.expectCleared()
  })

  it.each([
    ['started, then interrupted', [started, interrupted]],
    ['interrupted, then started', [interrupted, started]],
  ] as [string, WsMessage[]][])('the user carries on speaking (%s): one utterance, one message', (_name, frames) => {
    const c = awaitingTranscript()
    c.receive(...frames)
    // The old turn's spinner and bubble are gone; the new utterance is live
    // and was not wiped by the news about the old turn.
    expect(c.view.speech).toMatchObject({ speaking: true, pending: null, open: true })
    expect(c.view.turn.isProcessing).toBe(false)
    expect(c.view.pending).toBe(false)

    c.receive(running('Glue the sheet to the dark'), stoppedTurn, transcribing, final('Glue the sheet to the dark blue background.'))
    expect(c.view.messages).toEqual(['Glue the sheet to the dark blue background.'])
    expect(c.view.speech).toEqual(initialSpeechState)
    expect(c.view.turn.isProcessing).toBe(true)
  })

  it('the transport falls back while the user is speaking: nothing is left listening on screen', () => {
    const c = chat()
    c.receive(started, running('Glue the'))
    c.leaveStream()
    c.expectCleared()
    // What the server still had in flight for that utterance is dropped.
    c.receive(running('Glue the sheet'), stoppedTurn)
    c.expectCleared()
  })

  it('a transcript already on its way when the microphone went still becomes the one message', () => {
    const c = awaitingTranscript()
    c.leaveStream()
    c.receive(final('Glue the sheet.'), { type: 'status', message: 'Thinking…', stage: 'llm' })
    expect(c.view.messages).toEqual(['Glue the sheet.'])
    expect(c.view.speech).toEqual(initialSpeechState)
    // An ordinary turn from here on: the server's status has the spinner.
    expect(c.view.turn.isProcessing).toBe(true)
  })

  it('a transcript that crosses with Stop is one message, and the server’s `interrupted` ends the turn', () => {
    const c = awaitingTranscript()
    c.user.stop()
    c.receive(final('Glue the sheet.'), interrupted)
    expect(c.view.messages).toEqual(['Glue the sheet.'])
    c.expectCleared()
  })

  it('ends cleared after every ordering of what can follow `stopped, turn`', () => {
    type Move = { name: string; run(c: ReturnType<typeof chat>): void; ends: boolean; final?: boolean }
    const moves: Move[] = [
      { name: 'final', run: (c) => c.receive(final('Glue the sheet.')), ends: true, final: true },
      { name: 'ignored', run: (c) => c.receive(stoppedIgnored), ends: true },
      { name: 'error', run: (c) => c.receive(failed), ends: true },
      { name: 'interrupted', run: (c) => c.receive(interrupted), ends: true },
      { name: 'stop', run: (c) => c.user.stop(), ends: true },
      { name: 'mic-left', run: (c) => c.leaveStream(), ends: true },
      { name: 'mic-left (frame first)', run: (c) => c.leaveStream({ settled: false }), ends: false },
      { name: 'drop', run: (c) => c.dropSocket(), ends: true },
      { name: 'partial', run: (c) => c.receive(running('Glue the sheet to')), ends: false },
    ]
    const orderings: Move[][] = []
    const extend = (prefix: Move[]) => {
      if (prefix.some((m) => m.ends)) orderings.push(prefix)
      if (prefix.length === 4) return
      for (const move of moves) if (!prefix.includes(move)) extend([...prefix, move])
    }
    extend([])
    expect(orderings.length).toBeGreaterThan(2000)

    for (const ordering of orderings) {
      const c = awaitingTranscript()
      for (const move of ordering) move.run(c)
      const label = ordering.map((m) => m.name).join(' → ')
      const finals = ordering.filter((m) => m.final).length
      // The provisional bubble never outlives the turn, and the utterance
      // is a message exactly when its final transcript arrived.
      expect(c.view.speech.pending, label).toBeNull()
      expect(c.view.pending, label).toBe(false)
      expect(c.view.messages, label).toHaveLength(finals)
      // Without a transcript there is no turn, so nothing may hold the
      // spinner or the composer. (With one, it is an ordinary turn.)
      if (finals === 0) {
        expect(c.view.turn.isProcessing, label).toBe(false)
        expect(c.view.turn.isTyping, label).toBe(false)
        expect(c.view.awaitingReply, label).toBe(false)
      }
    }
  })

  it('clips, or a server without the stream microphone: none of this is ever in play', () => {
    const c = chat()
    c.leaveStream()
    c.user.sendRecording()
    c.receive(transcribing, interrupted)
    // `interrupted` for the previous turn leaves the new turn's spinner, as before.
    expect(c.view.turn.isProcessing).toBe(true)
    c.receive(final('Hello there.'))
    expect(c.view.messages).toEqual(['Hello there.'])
    expect(c.view.speech).toBe(initialSpeechState)
    c.user.stop()
    c.expectCleared()
  })
})
