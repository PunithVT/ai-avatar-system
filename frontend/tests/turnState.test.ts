import { describe, expect, it } from 'vitest'
import { initialTurnState, turnReducer, type TurnAction, type TurnState } from '@/lib/chat/turnState'

const run = (actions: TurnAction[], from: TurnState = initialTurnState) => actions.reduce(turnReducer, from)

describe('turnReducer', () => {
  it('walks a normal text turn', () => {
    let s = run([{ type: 'text-sent' }])
    expect(s).toMatchObject({ isProcessing: true, isTyping: true, streamingContent: '', latencyMs: null })

    s = run([{ type: 'token', token: 'Hel' }, { type: 'token', token: 'lo' }], s)
    expect(s).toMatchObject({ isTyping: false, streamingContent: 'Hello' })

    s = run([{ type: 'assistant-message' }], s)
    // The spinner holds after the text is complete: video is still rendering.
    expect(s).toMatchObject({ isProcessing: true, streamingContent: '' })

    s = run([{ type: 'first-chunk', latencyMs: 1800 }], s)
    expect(s).toMatchObject({ isProcessing: false, latencyMs: 1800 })
  })

  it('shows typing dots once a spoken turn is transcribed', () => {
    const s = run([{ type: 'audio-sent' }, { type: 'transcription' }])
    expect(s).toMatchObject({ isProcessing: true, isTyping: true })
  })

  it('raises the overlay for a status note before any chunk plays', () => {
    const s = run([{ type: 'status', message: 'Thinking…', playing: false }])
    expect(s).toMatchObject({ isProcessing: true, statusMsg: 'Thinking…' })
  })

  it('does not cover a playing avatar with a later status note', () => {
    // The server sends "Animating…" before every sentence.
    const playing = run([{ type: 'text-sent' }, { type: 'first-chunk', latencyMs: 900 }])
    const s = run([{ type: 'status', message: 'Animating…', playing: true }], playing)
    expect(s).toBe(playing)
  })

  it('releases the spinner when the turn ends with zero chunks', () => {
    // No avatar image, or every chunk failed: nothing will ever start playing.
    const s = run([{ type: 'text-sent' }, { type: 'assistant-message' }, { type: 'chunks-end', playing: false }])
    expect(s.isProcessing).toBe(false)
  })

  it('leaves state alone when chunks end while one is playing', () => {
    const playing = run([{ type: 'text-sent' }, { type: 'first-chunk', latencyMs: 900 }])
    expect(run([{ type: 'chunks-end', playing: true }], playing)).toBe(playing)
  })

  it('clears everything in flight on error', () => {
    const s = run([{ type: 'text-sent' }, { type: 'token', token: 'partial' }, { type: 'error' }])
    expect(s).toMatchObject({ isProcessing: false, isTyping: false, streamingContent: '' })
  })

  it('clears everything in flight on reset (stop, barge-in, dropped socket)', () => {
    // The dropped-socket case used to leave the spinner up indefinitely.
    const s = run([{ type: 'text-sent' }, { type: 'token', token: 'partial' }, { type: 'reset' }])
    expect(s).toMatchObject({ isProcessing: false, isTyping: false, streamingContent: '' })
  })

  it('keeps the last latency across a reset', () => {
    const s = run([{ type: 'first-chunk', latencyMs: 1200 }, { type: 'reset' }])
    expect(s.latencyMs).toBe(1200)
  })

  it('clears the latency when a new turn starts', () => {
    const s = run([{ type: 'first-chunk', latencyMs: 1200 }, { type: 'audio-sent' }])
    expect(s.latencyMs).toBeNull()
  })
})

// ── reply gate ──────────────────────────────────────────────────────────────
import { ReplyGate } from '@/lib/chat/turnState'

/**
 * Replays server frames through the gate the way ChatInterface does and
 * returns what would be on screen: finished bubbles plus the live one.
 */
function transcript(
  gate: ReplyGate,
  frames: Array<{ type: string; stage?: string; token?: string; content?: string }>,
  start: { bubbles?: string[]; live?: string } = {},
) {
  const bubbles = [...(start.bubbles ?? [])]
  let live = start.live ?? ''
  for (const f of frames) {
    if (!gate.admit(f)) continue
    if (f.type === 'token') live += f.token ?? ''
    if (f.type === 'message') {
      bubbles.push(f.content ?? '')
      live = ''
    }
  }
  return { bubbles, live }
}

const tok = (token: string) => ({ type: 'token', token })

describe('ReplyGate', () => {
  it('lets a normal reply through', () => {
    const gate = new ReplyGate()
    const out = transcript(gate, [
      { type: 'status', stage: 'llm' }, tok('Hel'), tok('lo'), { type: 'message', content: 'Hello' },
    ])
    expect(out).toEqual({ bubbles: ['Hello'], live: '' })
  })

  it('stop → late tokens → interrupted: the tail is not shown as a second bubble', () => {
    const gate = new ReplyGate()
    // "Sure, the first thing" was on screen and committed when Stop was pressed.
    gate.close()
    const out = transcript(gate, [tok(' you'), tok(' need is'), { type: 'interrupted' }], {
      bubbles: ['Sure, the first thing'],
    })
    expect(out).toEqual({ bubbles: ['Sure, the first thing'], live: '' })
    expect(gate.isOpen).toBe(true)
  })

  it('new turn while the old one streams: old tokens are dropped, the new reply is clean', () => {
    const gate = new ReplyGate()
    gate.close() // the follow-up was sent; the partial old reply was committed
    const out = transcript(gate, [
      tok(' old'), tok(' tail'),            // still in flight from the old turn
      { type: 'interrupted' },              // server cancelled it…
      { type: 'status', stage: 'llm' },     // …and started the new one
      tok('New'), tok(' reply'),
    ], { bubbles: ['Partial old reply'] })
    expect(out).toEqual({ bubbles: ['Partial old reply'], live: 'New reply' })
  })

  it('new turn after the old one had finished: no `interrupted` ever comes, the reply still shows', () => {
    // The server only sends `interrupted` when it actually cancelled a turn.
    // A gate that waited for it would swallow this whole reply.
    const gate = new ReplyGate()
    gate.close()
    const out = transcript(gate, [
      { type: 'status', stage: 'llm' }, tok('Fresh'), tok(' answer'), { type: 'message', content: 'Fresh answer' },
    ])
    expect(out).toEqual({ bubbles: ['Fresh answer'], live: '' })
  })

  it('a spoken turn reopens on its transcription stage', () => {
    const gate = new ReplyGate()
    gate.close()
    const out = transcript(gate, [
      tok(' stale'),
      { type: 'status', stage: 'transcription' },
      { type: 'transcription' },
      { type: 'status', stage: 'llm' },
      tok('Hi'),
    ])
    expect(out.live).toBe('Hi')
  })

  it('does not reopen on frames the old turn may still be sending', () => {
    const gate = new ReplyGate()
    gate.close()
    for (const frame of [
      { type: 'status', stage: 'animation' }, { type: 'status' }, { type: 'video_chunk' },
      { type: 'video_chunk_end' }, { type: 'tts_fallback' }, { type: 'ping' }, { type: 'error' },
    ]) {
      expect(gate.admit(frame)).toBe(true) // these frames are still handled…
      expect(gate.isOpen).toBe(false)      // …but they prove nothing about the old turn
    }
    expect(gate.admit(tok('stale'))).toBe(false)
    expect(gate.admit({ type: 'message' })).toBe(false)
  })

  it('also reopens at the start of the new turn\'s video, as a second signal', () => {
    const gate = new ReplyGate()
    gate.close()
    expect(transcript(gate, [{ type: 'video_chunk_start' }, tok('ok')]).live).toBe('ok')
  })

  it('a stop with no reply in flight is undone by the next turn', () => {
    const gate = new ReplyGate()
    gate.close() // Stop pressed while only video was playing: no `interrupted` follows
    gate.close() // then a new turn
    expect(transcript(gate, [{ type: 'status', stage: 'llm' }, tok('ok')]).live).toBe('ok')
  })

  it('reset() reopens it for a new connection', () => {
    const gate = new ReplyGate()
    gate.close()
    gate.reset()
    expect(gate.admit(tok('x'))).toBe(true)
  })
})
