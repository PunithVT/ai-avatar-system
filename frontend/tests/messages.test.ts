import { describe, expect, it } from 'vitest'
import { detectEmotion, fromServer, liveMessage, localMessageId, reconcilePersisted } from '@/lib/chat/messages'
import type { ChatMessage } from '@/lib/types'

const server = (id: string, role: ChatMessage['role'], content: string): ChatMessage => ({
  id, role, content, created_at: '2026-11-12T10:00:00Z',
})

describe('localMessageId', () => {
  it('is unique even within the same millisecond', () => {
    const ids = new Set(Array.from({ length: 100 }, () => localMessageId()))
    expect(ids.size).toBe(100)
  })
})

describe('fromServer', () => {
  it('keeps user and assistant rows, marked persisted', () => {
    const out = fromServer([server('1', 'user', 'hi'), server('2', 'system', 'summary'), server('3', 'assistant', 'hello')])
    expect(out.map((m) => m.id)).toEqual(['1', '3'])
    expect(out.every((m) => m.persisted)).toBe(true)
  })
})

describe('reconcilePersisted', () => {
  it('gives live messages their database ids', () => {
    const local = [liveMessage('user', 'hi'), liveMessage('assistant', 'hello there')]
    const out = reconcilePersisted(local, [server('db-1', 'user', 'hi'), server('db-2', 'assistant', 'hello there')])
    expect(out.map((m) => [m.id, m.persisted])).toEqual([['db-1', true], ['db-2', true]])
  })

  it('never drops a message the server has not stored yet', () => {
    // The reply is persisted after its video finishes; a refetch can beat it.
    const local = [liveMessage('user', 'hi'), liveMessage('assistant', 'still rendering')]
    const out = reconcilePersisted(local, [server('db-1', 'user', 'hi')])
    expect(out).toHaveLength(2)
    expect(out[0]).toMatchObject({ id: 'db-1', persisted: true })
    expect(out[1].persisted).toBeUndefined()
    expect(out[1].content).toBe('still rendering')
  })

  it('matches repeated content to distinct rows, in order', () => {
    const local = [liveMessage('user', 'ok'), liveMessage('assistant', 'sure'), liveMessage('user', 'ok')]
    const out = reconcilePersisted(local, [
      server('db-1', 'user', 'ok'), server('db-2', 'assistant', 'sure'), server('db-3', 'user', 'ok'),
    ])
    expect(out.map((m) => m.id)).toEqual(['db-1', 'db-2', 'db-3'])
  })

  it('does not hand an already-claimed id to a second message', () => {
    const local = [
      { ...liveMessage('user', 'ok'), id: 'db-1', persisted: true },
      liveMessage('user', 'ok'),
    ]
    const out = reconcilePersisted(local, [server('db-1', 'user', 'ok')])
    expect(out[1].persisted).toBeUndefined()
  })

  it('matches despite surrounding whitespace on either side', () => {
    // The server stores text stripped. A typed message with a trailing space
    // (mobile autocomplete adds one) or a streamed reply ending in a newline
    // used to stay without an id — and so without Edit and Delete — for good.
    const local = [liveMessage('user', 'hello there '), liveMessage('assistant', '\nHi!\n')]
    const out = reconcilePersisted(local, [server('u1', 'user', 'hello there'), server('a1', 'assistant', 'Hi!')])
    expect(out.map((m) => m.id)).toEqual(['u1', 'a1'])
    expect(out.every((m) => m.persisted)).toBe(true)
    // What the user sees is not rewritten.
    expect(out[0].content).toBe('hello there ')
  })

  it('works from just the tail of a long conversation', () => {
    // Past the first page the server response no longer starts at row 0.
    // Only the rows it does contain are candidates; nothing else is disturbed.
    const old = fromServer(Array.from({ length: 300 }, (_, i) => server(`old-${i}`, i % 2 ? 'assistant' : 'user', `m${i}`)))
    const local = [...old, liveMessage('user', 'newest question'), liveMessage('assistant', 'newest answer')]
    const tail = [
      server('old-298', 'user', 'm298'), server('old-299', 'assistant', 'm299'),
      server('new-u', 'user', 'newest question'), server('new-a', 'assistant', 'newest answer'),
    ]
    const out = reconcilePersisted(local, tail)
    expect(out).toHaveLength(302)
    expect(out.slice(-2).map((m) => m.id)).toEqual(['new-u', 'new-a'])
    expect(out.slice(0, 300)).toEqual(old)
  })

  it('does not match across roles', () => {
    const local = [liveMessage('assistant', 'hello')]
    expect(reconcilePersisted(local, [server('db-1', 'user', 'hello')])[0].persisted).toBeUndefined()
  })

  it('returns the same array when nothing changed, so React can skip the render', () => {
    const local = [liveMessage('user', 'unsent')]
    expect(reconcilePersisted(local, [])).toBe(local)
  })
})

describe('detectEmotion', () => {
  it.each([
    ['haha that is funny', 'happy'],
    ['I hate this', 'angry'],
    ['I feel lonely', 'sad'],
    ['wow, amazing', 'excited'],
    ['why is that?', 'curious'],
    ['The meeting is at noon.', 'neutral'],
  ])('%s → %s', (text, expected) => {
    expect(detectEmotion(text)).toBe(expected)
  })
})
