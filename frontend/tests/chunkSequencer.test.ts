import { describe, expect, it } from 'vitest'
import { ChunkSequencer, type Clip } from '@/lib/chat/chunkSequencer'

describe('ChunkSequencer', () => {
  it('starts the first clip immediately and queues the rest', () => {
    const q = new ChunkSequencer()
    const first = q.enqueue('a.mp4')
    expect(first).toMatchObject({ url: 'a.mp4' })
    expect(q.enqueue('b.mp4')).toBeNull()
    expect(q.isPlaying).toBe(true)
    expect(q.pending).toBe(1)
    expect(q.peek()).toBe('b.mp4')
  })

  it('plays clips in the order they arrived', () => {
    const q = new ChunkSequencer()
    const a = q.enqueue('a.mp4')!
    q.enqueue('b.mp4')
    q.enqueue('c.mp4')
    const b = q.advance(a.token) as Clip
    expect(b.url).toBe('b.mp4')
    const c = q.advance(b.token) as Clip
    expect(c.url).toBe('c.mp4')
    expect(q.advance(c.token)).toBe('idle')
    expect(q.isPlaying).toBe(false)
  })

  it('advances exactly once when one failure is reported twice', () => {
    // A bad clip fires the `error` event AND rejects play(). Both carry the
    // same clip's token; the second must not skip the good clip that follows.
    const q = new ChunkSequencer()
    const bad = q.enqueue('bad.mp4')!
    q.enqueue('good.mp4')
    q.enqueue('last.mp4')

    const good = q.advance(bad.token) as Clip
    expect(good.url).toBe('good.mp4')
    expect(q.advance(bad.token)).toBeNull() // the duplicate signal
    expect(q.activeToken).toBe(good.token)
    expect(q.pending).toBe(1)
  })

  it('ignores a signal about a clip from before stop()', () => {
    const q = new ChunkSequencer()
    const old = q.enqueue('old.mp4')!
    q.stop()
    const fresh = q.enqueue('new.mp4')!
    // The aborted play() of the old clip rejects late.
    expect(q.advance(old.token)).toBeNull()
    expect(q.activeToken).toBe(fresh.token)
  })

  it('stop() drops the queue and goes idle', () => {
    const q = new ChunkSequencer()
    q.enqueue('a.mp4')
    q.enqueue('b.mp4')
    q.stop()
    expect(q.isPlaying).toBe(false)
    expect(q.pending).toBe(0)
    expect(q.hasWork).toBe(false)
  })

  it('restarts after an underrun', () => {
    // Rendering slower than playback: the queue drains, then a clip arrives.
    const q = new ChunkSequencer()
    const a = q.enqueue('a.mp4')!
    expect(q.advance(a.token)).toBe('idle')
    const b = q.enqueue('b.mp4')
    expect(b).toMatchObject({ url: 'b.mp4' })
    expect(b!.token).not.toBe(a.token)
  })

  it('never reuses a token', () => {
    const q = new ChunkSequencer()
    const tokens = new Set<number>()
    for (let i = 0; i < 5; i++) {
      const clip = q.enqueue(`${i}.mp4`)!
      tokens.add(clip.token)
      q.advance(clip.token)
    }
    expect(tokens.size).toBe(5)
  })
})
