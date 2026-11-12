import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  AvatarTransport, CLIP_HOLD_MS, isNewSentence, type ClipQueue, type StreamView,
} from '@/lib/chat/avatarTransport'
import { ChunkSequencer } from '@/lib/chat/chunkSequencer'

/** The clip player without its <video>: the real sequencer, played by hand. */
class FakeClips implements ClipQueue {
  private sequencer = new ChunkSequencer()
  enqueued: string[] = []
  /** Every clip that actually reached the screen, in order. */
  played: string[] = []
  stops = 0
  enqueue(url: string) {
    this.enqueued.push(url)
    const clip = this.sequencer.enqueue(url)
    if (clip) this.played.push(clip.url)
    return clip !== null
  }
  stop() {
    this.stops += 1
    this.sequencer.stop()
  }
  isActive() {
    return this.sequencer.hasWork
  }
  /** The clip on screen finished. */
  finish() {
    const token = this.sequencer.activeToken
    if (token === null) return
    const next = this.sequencer.advance(token)
    if (next && next !== 'idle') this.played.push(next.url)
  }
}

function setup(live = false) {
  const clips = new FakeClips()
  const views: StreamView[] = []
  const transport = new AvatarTransport(clips, (view) => views.push(view))
  if (live) transport.setLive(true)
  return { clips, views, transport }
}

const onStream = { transport: 'webrtc' as const, text: 'Hello.', duration_ms: 900 }
const byUrl = (url: string) => ({ video_url: url, text: 'Hello.' })
const numbered = (chunk_index: number, url: string) => ({ video_url: url, chunk_index })
const resent = (chunk_index: number, url: string) => ({ video_url: url, chunk_index, resend: true })
const IDLE = { live: true, speaking: false, suppressed: false }

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('AvatarTransport on clips (the default)', () => {
  it('passes URL chunks straight to the clip queue, as before', () => {
    const { clips, transport, views } = setup()
    expect(transport.enqueue(byUrl('a.mp4'))).toBe(true) // started playback
    expect(transport.enqueue(byUrl('b.mp4'))).toBe(false) // queued behind it
    expect(clips.enqueued).toEqual(['a.mp4', 'b.mp4'])
    expect(transport.isActive()).toBe(true)
    clips.finish()
    clips.finish()
    expect(transport.isActive()).toBe(false)
    expect(views).toEqual([]) // no stream state ever changes
  })

  it('stop drops the clip queue and nothing else', () => {
    const { clips, transport, views } = setup()
    transport.enqueue(byUrl('a.mp4'))
    transport.stop()
    expect(clips.stops).toBe(1)
    expect(transport.isActive()).toBe(false)
    expect(transport.view).toEqual({ live: false, speaking: false, suppressed: false })
    expect(views).toEqual([])
  })

  it('ignores speaking events when no stream is live', () => {
    const { transport, views } = setup()
    transport.setSpeaking(true)
    expect(transport.isActive()).toBe(false)
    expect(transport.view.speaking).toBe(false)
    expect(views).toEqual([])
  })

  it('has nothing to play for a stream chunk it has no stream for', () => {
    // We fell back; the server had not caught up when it sent this one.
    const { clips, transport } = setup()
    expect(transport.enqueue(onStream)).toBe(false)
    expect(clips.enqueued).toEqual([])
    expect(transport.isActive()).toBe(false)
  })

  it('ignores a chunk with neither a URL nor a transport', () => {
    const { clips, transport } = setup()
    expect(transport.enqueue({})).toBe(false)
    expect(clips.enqueued).toEqual([])
  })
})

describe('AvatarTransport on the stream', () => {
  it('a stream chunk does not touch the clip queue', () => {
    const { clips, transport } = setup(true)
    expect(transport.enqueue(onStream)).toBe(true)
    expect(transport.enqueue(onStream)).toBe(true)
    expect(clips.enqueued).toEqual([])
    expect(clips.stops).toBe(0)
  })

  it('speaking events, not chunks, say whether the avatar is talking', () => {
    const { transport, views } = setup(true)
    transport.enqueue(onStream)
    expect(transport.isActive()).toBe(false)

    transport.setSpeaking(true)
    expect(transport.isActive()).toBe(true)
    expect(transport.view.speaking).toBe(true)

    transport.setSpeaking(true) // a repeat changes nothing
    transport.setSpeaking(false)
    expect(transport.isActive()).toBe(false)
    expect(views.map((v) => v.speaking)).toEqual([false, true, false]) // setLive, true, false
  })

  it('a URL chunk still plays while the stream is live', () => {
    // The server could not put this sentence on the stream.
    const { clips, transport } = setup(true)
    expect(transport.enqueue(byUrl('fallback.mp4'))).toBe(true)
    expect(clips.enqueued).toEqual(['fallback.mp4'])
    expect(transport.isActive()).toBe(true)
    clips.finish()
    expect(transport.isActive()).toBe(false)
  })

  it('mixes both within one reply', () => {
    const { clips, transport } = setup(true)
    transport.enqueue(onStream)
    transport.setSpeaking(true)
    transport.setSpeaking(false)
    transport.enqueue(byUrl('two.mp4'))
    expect(transport.isActive()).toBe(true)
    clips.finish()
    transport.enqueue(onStream)
    transport.setSpeaking(true)
    expect(transport.isActive()).toBe(true)
    expect(clips.enqueued).toEqual(['two.mp4'])
  })

  it('stop silences the stream at once and holds until the server has flushed', () => {
    const { clips, transport } = setup(true)
    transport.setSpeaking(true)
    transport.enqueue(byUrl('over.mp4'))

    transport.stop()
    expect(clips.stops).toBe(1)
    expect(transport.view).toEqual({ live: true, speaking: false, suppressed: true })
    // Not "active" any more: the echo threshold drops and a second barge-in
    // is not sent for speech the user can no longer hear.
    expect(transport.isActive()).toBe(false)

    transport.setSpeaking(false) // the flush
    expect(transport.view).toEqual({ live: true, speaking: false, suppressed: false })

    transport.setSpeaking(true) // the next turn
    expect(transport.view.speaking).toBe(true)
  })

  it('`interrupted` releases a stop even if speaking:false has not arrived', () => {
    const { transport } = setup(true)
    transport.setSpeaking(true)
    transport.stop()
    transport.confirmStopped()
    expect(transport.view).toEqual({ live: true, speaking: false, suppressed: false })
    transport.setSpeaking(false) // the late event is a no-op
    expect(transport.view).toEqual({ live: true, speaking: false, suppressed: false })
  })

  it('`interrupted` first, then the local stop it triggers: nothing is left suppressed', () => {
    // The chat handles `interrupted` by calling stop(). Either order must
    // end with the stream audible for the next reply.
    const { transport } = setup(true)
    transport.setSpeaking(true)
    transport.confirmStopped()
    transport.stop()
    expect(transport.view.suppressed).toBe(false)
    transport.setSpeaking(true)
    expect(transport.isActive()).toBe(true)
  })

  it('stopping an idle stream suppresses nothing', () => {
    // Every new turn calls stop(). With nothing to flush the server sends no
    // confirmation, so a suppression here would swallow the next reply.
    const { transport, views } = setup(true)
    const before = views.length
    transport.stop()
    expect(transport.view.suppressed).toBe(false)
    expect(views).toHaveLength(before)
    transport.setSpeaking(true)
    expect(transport.view.speaking).toBe(true)
  })

  it('the next accepted stream chunk releases a stop the server never confirmed', () => {
    const { transport } = setup(true)
    transport.setSpeaking(true)
    transport.stop()
    transport.stop() // twice is safe
    expect(transport.enqueue(onStream)).toBe(true)
    expect(transport.view).toEqual({ live: true, speaking: true, suppressed: false })
  })

  it('losing the stream clears its speaking state, and clips carry on', () => {
    const { clips, transport } = setup(true)
    transport.setSpeaking(true)
    transport.stop()
    transport.setLive(false)
    expect(transport.view).toEqual({ live: false, speaking: false, suppressed: false })
    expect(transport.isActive()).toBe(false)

    transport.setSpeaking(true) // a straggler from the dead stream
    expect(transport.isActive()).toBe(false)

    expect(transport.enqueue(byUrl('next.mp4'))).toBe(true)
    expect(clips.enqueued).toEqual(['next.mp4'])
  })

  it('a new stream starts idle', () => {
    const { transport } = setup(true)
    transport.setSpeaking(true)
    transport.setLive(false)
    transport.setLive(true)
    expect(transport.view).toEqual({ live: true, speaking: false, suppressed: false })
  })

  it('reaches whichever clip queue it is bound to', () => {
    const { transport } = setup(true)
    const next = new FakeClips()
    const seen: StreamView[] = []
    transport.bind(next, (view) => seen.push(view))
    transport.enqueue(byUrl('a.mp4'))
    transport.setSpeaking(true)
    expect(next.enqueued).toEqual(['a.mp4'])
    expect(seen).toHaveLength(1)
  })
})

describe('a clip that arrives while the stream is speaking', () => {
  it('waits for the stream to finish its sentence, then plays', () => {
    const { clips, transport } = setup(true)
    transport.enqueue(onStream)
    transport.setSpeaking(true)

    // Still true: the avatar IS talking, so the spinner must go.
    expect(transport.enqueue(numbered(1, 'one.mp4'))).toBe(true)
    expect(clips.enqueued).toEqual([]) // not started on top of the stream
    expect(transport.view.speaking).toBe(true)
    expect(transport.isActive()).toBe(true)

    transport.setSpeaking(false)
    expect(clips.played).toEqual(['one.mp4'])
    expect(transport.isActive()).toBe(true)
    clips.finish()
    expect(transport.isActive()).toBe(false)
  })

  it('keeps the reply in order: stream sentence, then every clip as it arrived', () => {
    const { clips, transport } = setup(true)
    transport.enqueue(onStream)
    transport.setSpeaking(true)
    transport.enqueue(numbered(1, 'one.mp4'))
    transport.enqueue(numbered(2, 'two.mp4'))
    expect(clips.enqueued).toEqual([])

    transport.setSpeaking(false)
    transport.enqueue(numbered(3, 'three.mp4')) // after the stream: straight to the queue
    clips.finish()
    clips.finish()
    clips.finish()
    expect(clips.played).toEqual(['one.mp4', 'two.mp4', 'three.mp4'])
  })

  it('the listener hears the stream go idle with the clip already queued', () => {
    // useAvatarPlayer silences the stream while clips are active; it must
    // not see a moment in between where neither is.
    const clips = new FakeClips()
    const activeAtChange: boolean[] = []
    const transport = new AvatarTransport(clips, () => activeAtChange.push(clips.isActive()))
    transport.setLive(true)
    transport.setSpeaking(true)
    transport.enqueue(numbered(0, 'a.mp4'))
    transport.setSpeaking(false)
    expect(activeAtChange.at(-1)).toBe(true)
  })

  it('is played when the stream goes down instead of finishing', () => {
    const { clips, transport } = setup(true)
    transport.setSpeaking(true)
    transport.enqueue(numbered(4, 'four.mp4'))
    transport.setLive(false)
    expect(clips.played).toEqual(['four.mp4'])
    expect(transport.view).toEqual({ live: false, speaking: false, suppressed: false })
  })

  it('does not wait for ever: a missing speaking:false costs 10 s, not the reply', () => {
    const { clips, transport } = setup(true)
    transport.setSpeaking(true)
    transport.enqueue(numbered(1, 'one.mp4'))
    transport.enqueue(numbered(2, 'two.mp4'))

    vi.advanceTimersByTime(CLIP_HOLD_MS - 1)
    expect(clips.enqueued).toEqual([])
    vi.advanceTimersByTime(1)
    expect(clips.enqueued).toEqual(['one.mp4', 'two.mp4'])

    // Having given up on this stretch of speech, later clips are not made to
    // wait another 10 s each…
    expect(transport.enqueue(numbered(3, 'three.mp4'))).toBe(false) // queued behind `one`
    expect(clips.enqueued).toEqual(['one.mp4', 'two.mp4', 'three.mp4'])

    // …but the next stretch is waited for as usual.
    transport.setSpeaking(false)
    transport.setSpeaking(true)
    transport.enqueue(numbered(4, 'four.mp4'))
    expect(clips.enqueued).toEqual(['one.mp4', 'two.mp4', 'three.mp4'])
  })

  it('the wait is timed from the first clip held, and ends with it', () => {
    const { clips, transport } = setup(true)
    transport.setSpeaking(true)
    transport.enqueue(numbered(1, 'one.mp4'))
    transport.setSpeaking(false) // released; the timer goes with it
    clips.finish()
    transport.setSpeaking(true)
    vi.advanceTimersByTime(CLIP_HOLD_MS - 1)
    transport.enqueue(numbered(2, 'two.mp4'))
    vi.advanceTimersByTime(CLIP_HOLD_MS - 1)
    expect(clips.enqueued).toEqual(['one.mp4']) // the first wait's timer did not release it
    vi.advanceTimersByTime(1)
    expect(clips.enqueued).toEqual(['one.mp4', 'two.mp4'])
  })

  it('is dropped by a stop, and never plays afterwards', () => {
    const { clips, transport } = setup(true)
    transport.setSpeaking(true)
    transport.enqueue(numbered(1, 'one.mp4'))
    transport.stop()
    expect(transport.isActive()).toBe(false)
    transport.setSpeaking(false)
    vi.advanceTimersByTime(CLIP_HOLD_MS * 2)
    expect(clips.enqueued).toEqual([])
  })

  it('is dropped when the server reports the reply interrupted', () => {
    const { clips, transport } = setup(true)
    transport.setSpeaking(true)
    transport.enqueue(numbered(1, 'one.mp4'))
    transport.confirmStopped()
    transport.setSpeaking(false)
    vi.advanceTimersByTime(CLIP_HOLD_MS * 2)
    expect(clips.enqueued).toEqual([])
    expect(transport.isActive()).toBe(false)
  })

  it('plays at once when the stream is live but idle, or stopped and waiting for its flush', () => {
    const { clips, transport } = setup(true)
    expect(transport.enqueue(numbered(0, 'idle.mp4'))).toBe(true)
    expect(clips.played).toEqual(['idle.mp4'])
    clips.finish()

    // A stop silenced the stream; what is left on it is not wanted, so a
    // clip of the next reply has nothing to wait behind.
    transport.setSpeaking(true)
    transport.stop()
    transport.beginReply()
    expect(transport.enqueue(numbered(0, 'next.mp4'))).toBe(true)
    expect(clips.played).toEqual(['idle.mp4', 'next.mp4'])
  })

  it('on clips nothing is ever held', () => {
    const { clips, transport } = setup()
    transport.setSpeaking(true) // ignored: no stream
    expect(transport.enqueue(numbered(0, 'a.mp4'))).toBe(true)
    expect(clips.enqueued).toEqual(['a.mp4'])
    vi.advanceTimersByTime(CLIP_HOLD_MS * 2)
    expect(clips.enqueued).toEqual(['a.mp4'])
  })
})

describe('chunks re-sent after the stream died', () => {
  it('play as clips, in order, once the stream is gone', () => {
    const { clips, transport } = setup(true)
    transport.beginReply()
    // Sentences 0-2 were announced on the stream; 0 was being spoken.
    expect(transport.enqueue({ ...onStream, chunk_index: 0 })).toBe(true)
    expect(transport.enqueue({ ...onStream, chunk_index: 1 })).toBe(true)
    expect(transport.enqueue({ ...onStream, chunk_index: 2 })).toBe(true)
    transport.setSpeaking(true)

    transport.setLive(false) // the fallback
    expect(transport.isActive()).toBe(false)

    // Same indexes as the stream chunks: not duplicates, they never played.
    expect(transport.enqueue(resent(0, 'r0.mp4'))).toBe(true)
    expect(transport.enqueue(resent(1, 'r1.mp4'))).toBe(false)
    expect(transport.enqueue(resent(2, 'r2.mp4'))).toBe(false)
    expect(transport.enqueue(numbered(3, 'three.mp4'))).toBe(false) // the rest of the reply
    clips.finish()
    clips.finish()
    clips.finish()
    expect(clips.played).toEqual(['r0.mp4', 'r1.mp4', 'r2.mp4', 'three.mp4'])
  })

  it('never start under a stream that is still speaking', () => {
    // The fallback normally comes first. If a re-send overtakes it, the clip
    // waits for the stream to go — it is not played over it.
    const { clips, transport } = setup(true)
    transport.enqueue({ ...onStream, chunk_index: 0 })
    transport.setSpeaking(true)
    transport.enqueue(resent(0, 'r0.mp4'))
    expect(clips.enqueued).toEqual([])
    transport.setLive(false)
    expect(clips.played).toEqual(['r0.mp4'])
  })

  it('play although this side had already dropped the stream chunk', () => {
    // We fell back first; the server had not heard when it announced these.
    const { clips, transport } = setup()
    expect(transport.enqueue({ ...onStream, chunk_index: 4 })).toBe(false)
    expect(transport.enqueue(resent(4, 'r4.mp4'))).toBe(true)
    expect(clips.played).toEqual(['r4.mp4'])
  })

  it('are not new sentences', () => {
    expect(isNewSentence(resent(2, 'r2.mp4'))).toBe(false)
    expect(isNewSentence(numbered(2, 'two.mp4'))).toBe(true)
    expect(isNewSentence({ ...onStream, chunk_index: 2 })).toBe(true)
    // Only a literal `true` marks a re-send.
    expect(isNewSentence({ video_url: 'a.mp4', resend: 'yes' as unknown as boolean })).toBe(true)
  })
})

describe('the same chunk_index twice by URL', () => {
  it('plays once, whichever of the two is the re-send', () => {
    const { clips, transport } = setup()
    transport.beginReply()
    expect(transport.enqueue(numbered(0, 'a.mp4'))).toBe(true)
    expect(transport.enqueue(resent(0, 'a-again.mp4'))).toBe(false)
    expect(transport.enqueue(resent(1, 'b.mp4'))).toBe(false) // queued
    expect(transport.enqueue(resent(1, 'b-again.mp4'))).toBe(false) // dropped
    expect(transport.enqueue(numbered(1, 'b-third.mp4'))).toBe(false)
    expect(clips.enqueued).toEqual(['a.mp4', 'b.mp4'])
  })

  it('counts a held clip as taken', () => {
    const { clips, transport } = setup(true)
    transport.setSpeaking(true)
    transport.enqueue(numbered(1, 'one.mp4'))
    transport.enqueue(resent(1, 'one-again.mp4'))
    transport.setSpeaking(false)
    expect(clips.enqueued).toEqual(['one.mp4'])
  })

  it('starts again with every reply, and after a stop', () => {
    const { clips, transport } = setup()
    transport.beginReply()
    transport.enqueue(numbered(0, 'first.mp4'))
    clips.finish()
    transport.beginReply() // video_chunk_start
    expect(transport.enqueue(numbered(0, 'second.mp4'))).toBe(true)
    transport.stop()
    expect(transport.enqueue(numbered(0, 'third.mp4'))).toBe(true)
    expect(clips.enqueued).toEqual(['first.mp4', 'second.mp4', 'third.mp4'])
  })

  it('leaves chunks without an index alone', () => {
    const { clips, transport } = setup()
    transport.enqueue(byUrl('a.mp4'))
    transport.enqueue(byUrl('a.mp4'))
    expect(clips.enqueued).toEqual(['a.mp4', 'a.mp4'])
  })
})

/**
 * Every way a stop and the server's two acknowledgements can interleave. The
 * chat calls stop() itself when it sees `interrupted`, so that is part of
 * each walk. Whatever the order: nothing suppressed afterwards, and the next
 * reply is heard.
 */
describe('after a stop, the next reply is always audible', () => {
  /** The chat's handling of an `interrupted` frame. */
  const interrupted = (transport: AvatarTransport) => {
    transport.confirmStopped() // routeMediaFrame
    transport.stop() // endTurnLocally / the previous-turn branch
  }
  const expectNextReplyHeard = (transport: AvatarTransport) => {
    expect(transport.view).toEqual(IDLE)
    expect(transport.isActive()).toBe(false)
    transport.beginReply()
    expect(transport.enqueue({ ...onStream, chunk_index: 0 })).toBe(true)
    transport.setSpeaking(true)
    expect(transport.view).toEqual({ live: true, speaking: true, suppressed: false })
    expect(transport.isActive()).toBe(true)
  }

  it('stop while speaking -> interrupted -> speaking:false', () => {
    const { transport } = setup(true)
    transport.setSpeaking(true)
    transport.stop()
    expect(transport.view.suppressed).toBe(true)
    interrupted(transport)
    expect(transport.view).toEqual(IDLE)
    transport.setSpeaking(false) // just after: changes nothing
    expectNextReplyHeard(transport)
  })

  it('stop while speaking -> speaking:false -> interrupted', () => {
    const { transport } = setup(true)
    transport.setSpeaking(true)
    transport.stop()
    transport.setSpeaking(false)
    expect(transport.view).toEqual(IDLE)
    interrupted(transport)
    expectNextReplyHeard(transport)
  })

  it('stop when idle, with no acknowledgement at all', () => {
    const { transport } = setup(true)
    transport.stop()
    expectNextReplyHeard(transport)
  })

  it('stop when idle, and the server still says interrupted (a turn was generating)', () => {
    const { transport } = setup(true)
    transport.stop()
    interrupted(transport)
    transport.setSpeaking(false)
    expectNextReplyHeard(transport)
  })

  it('interrupted with no turn in flight and no local stop (speech outlived its turn)', () => {
    const { transport, views } = setup(true)
    transport.setSpeaking(true)
    interrupted(transport)
    expect(transport.view).toEqual(IDLE)
    const before = views.length
    transport.setSpeaking(false)
    expect(views).toHaveLength(before) // the late event is not even a change
    expectNextReplyHeard(transport)
  })

  it('interrupted out of nowhere, on an idle stream', () => {
    const { transport, views } = setup(true)
    const before = views.length
    interrupted(transport)
    transport.setSpeaking(false)
    expect(views).toHaveLength(before)
    expectNextReplyHeard(transport)
  })

  it('a new turn while the old speech is still playing, acknowledged either way round', () => {
    for (const order of ['interrupted-first', 'speaking-first'] as const) {
      const { transport } = setup(true)
      transport.setSpeaking(true)
      transport.stop() // beginTurn
      expect(transport.view).toEqual({ live: true, speaking: false, suppressed: true })
      if (order === 'interrupted-first') {
        interrupted(transport)
        transport.setSpeaking(false)
      } else {
        transport.setSpeaking(false)
        interrupted(transport)
      }
      expectNextReplyHeard(transport)
    }
  })

  it('a new turn whose first sentence arrives before either acknowledgement', () => {
    // The accepted chunk is the proof that the old speech has been flushed.
    const { transport } = setup(true)
    transport.setSpeaking(true)
    transport.stop()
    transport.beginReply()
    expect(transport.enqueue({ ...onStream, chunk_index: 0 })).toBe(true)
    expect(transport.view).toEqual({ live: true, speaking: true, suppressed: false })
  })

  it('a new turn that comes back as clips (the stream died meanwhile)', () => {
    const { clips, transport } = setup(true)
    transport.setSpeaking(true)
    transport.stop()
    transport.setLive(false)
    transport.beginReply()
    expect(transport.enqueue(numbered(0, 'a.mp4'))).toBe(true)
    expect(clips.played).toEqual(['a.mp4'])
    expect(transport.view.suppressed).toBe(false)
  })
})
