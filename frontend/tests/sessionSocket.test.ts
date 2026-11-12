import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  MAX_WS_RECONNECT_ATTEMPTS, SessionSocket, WS_AT_CAPACITY_CODE, WS_AUTH_REJECT_CODE,
  WS_ESTABLISHED_FALLBACK_MS, WS_GONE_CODE, WS_SUPERSEDED_CODE, type SessionSocketCallbacks,
} from '@/lib/chat/sessionSocket'

/** Stand-in for the browser WebSocket: records itself and lets a test fire events. */
class FakeSocket {
  static instances: FakeSocket[] = []
  readyState = 0 // CONNECTING
  sent: string[] = []
  closedByClient = false
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null

  constructor(readonly url: string) {
    FakeSocket.instances.push(this)
  }
  send(data: string) {
    if (this.readyState !== 1) throw new Error('InvalidStateError')
    this.sent.push(data)
  }
  close() {
    this.closedByClient = true
    this.readyState = 3
  }
  // ── test controls ──
  open() {
    this.readyState = 1
    this.onopen?.()
  }
  /** A successful connect as the real server does it: accept, then `ready`. */
  establish() {
    this.open()
    this.receive(JSON.stringify({ type: 'ready' }))
  }
  /**
   * A refusal as the real server does it: ACCEPT, then close with the code.
   * (It has to accept first — a close before accept reaches the browser as
   * an anonymous 1006.) So `onopen` fires for refused connections too.
   */
  refuse(code: number) {
    this.open()
    this.drop(code)
  }
  receive(data: string) {
    this.onmessage?.({ data })
  }
  drop(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code })
  }
}

function setup() {
  const callbacks = {
    onStatus: vi.fn(),
    onOpen: vi.fn(),
    onEstablished: vi.fn(),
    onMessage: vi.fn(),
    onDrop: vi.fn(),
    onStalled: vi.fn(),
    onBusy: vi.fn(),
  } satisfies SessionSocketCallbacks
  const socket = new SessionSocket((sid) => `ws://test/${sid}`, callbacks, FakeSocket as never)
  const latest = () => FakeSocket.instances[FakeSocket.instances.length - 1]
  return { socket, callbacks, latest }
}

beforeEach(() => {
  FakeSocket.instances = []
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('SessionSocket', () => {
  it('is connected once the server speaks, not merely once the socket opens', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    expect(latest().url).toBe('ws://test/s1')
    expect(callbacks.onStatus).toHaveBeenLastCalledWith('connecting')

    latest().open()
    expect(callbacks.onOpen).toHaveBeenCalledTimes(1) // settings can be replayed now
    expect(callbacks.onStatus).toHaveBeenLastCalledWith('connecting')
    expect(callbacks.onEstablished).not.toHaveBeenCalled()

    latest().receive(JSON.stringify({ type: 'ready' }))
    expect(callbacks.onStatus).toHaveBeenLastCalledWith('connected')
    expect(callbacks.onEstablished).toHaveBeenCalledWith(true)
    expect(callbacks.onMessage).toHaveBeenCalledWith({ type: 'ready' })
  })

  it('treats a silent but stable socket as connected after a grace period', () => {
    // A server that sends no `ready` frame must not leave the UI on "connecting".
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().open()
    vi.advanceTimersByTime(WS_ESTABLISHED_FALLBACK_MS - 1)
    expect(callbacks.onEstablished).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(callbacks.onStatus).toHaveBeenLastCalledWith('connected')
    expect(callbacks.onEstablished).toHaveBeenCalledTimes(1)
    // …and the frame that eventually arrives does not announce it twice.
    latest().receive(JSON.stringify({ type: 'ping' }))
    expect(callbacks.onEstablished).toHaveBeenCalledTimes(1)
  })

  it('delivers parsed messages and survives a malformed frame', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().open()
    latest().receive('not json{')
    latest().receive(JSON.stringify({ type: 'ping' }))
    expect(callbacks.onMessage).toHaveBeenCalledTimes(1)
    expect(callbacks.onMessage).toHaveBeenCalledWith({ type: 'ping' })
  })

  it('send() returns false instead of throwing while connecting', () => {
    const { socket, latest } = setup()
    socket.connect('s1')
    expect(socket.send({ type: 'interrupt' })).toBe(false)
    latest().open()
    expect(socket.send({ type: 'interrupt' })).toBe(true)
    expect(latest().sent).toEqual(['{"type":"interrupt"}'])
  })

  it('reconnects with exponential backoff, as a non-fresh connection', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().establish()
    latest().drop()
    expect(callbacks.onDrop).toHaveBeenCalledWith(1006)
    expect(FakeSocket.instances).toHaveLength(1)

    vi.advanceTimersByTime(999)
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeSocket.instances).toHaveLength(2)

    latest().drop() // second failure waits 2 s
    vi.advanceTimersByTime(1999)
    expect(FakeSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeSocket.instances).toHaveLength(3)

    latest().establish()
    expect(callbacks.onEstablished).toHaveBeenLastCalledWith(false)

    // A real connection resets the backoff: the next drop waits 1 s again.
    latest().drop()
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(4)
  })

  it('gives up after the retry budget and reports it', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    for (let i = 0; i <= MAX_WS_RECONNECT_ATTEMPTS; i++) {
      latest().drop()
      vi.advanceTimersByTime(30_000)
    }
    expect(callbacks.onStalled).toHaveBeenCalledWith('lost')
    const count = FakeSocket.instances.length
    vi.advanceTimersByTime(120_000)
    expect(FakeSocket.instances).toHaveLength(count)
  })

  it('does not reconnect after an auth rejection, and never reports connected', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().refuse(WS_AUTH_REJECT_CODE)
    vi.advanceTimersByTime(60_000)
    expect(callbacks.onStalled).toHaveBeenCalledWith('auth')
    expect(FakeSocket.instances).toHaveLength(1)
    // The socket did open — the server accepts before refusing — but that
    // must not surface as a connection (it used to flash "Connected!").
    expect(callbacks.onEstablished).not.toHaveBeenCalled()
    expect(callbacks.onStatus).not.toHaveBeenCalledWith('connected')
  })

  it('does not reconnect when the conversation no longer exists', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().refuse(WS_GONE_CODE)
    vi.advanceTimersByTime(60_000)
    expect(callbacks.onStalled).toHaveBeenCalledWith('gone')
    expect(callbacks.onStalled).not.toHaveBeenCalledWith('auth')
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('retries a server-side failure (1011) like any transient drop', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().refuse(1011)
    expect(callbacks.onStalled).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('does not reconnect when another connection took the session over', () => {
    // Reconnecting would evict the newer socket, which would reconnect and
    // evict this one — the two would trade places forever.
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().establish()
    latest().drop(WS_SUPERSEDED_CODE)
    vi.advanceTimersByTime(60_000)
    expect(callbacks.onStalled).toHaveBeenCalledWith('replaced')
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('backs off, announces once and eventually gives up while the server is at capacity', () => {
    // Modelled on the real server: every refusal is accept-then-close(1013).
    // With the retry counter reset in onopen this retried every second,
    // forever, toasting each time.
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')

    const waits: number[] = []
    for (let i = 0; i < MAX_WS_RECONNECT_ATTEMPTS; i++) {
      latest().refuse(WS_AT_CAPACITY_CODE)
      const before = FakeSocket.instances.length
      let waited = 0
      while (FakeSocket.instances.length === before && waited < 60_000) {
        vi.advanceTimersByTime(500)
        waited += 500
      }
      waits.push(waited)
    }
    expect(waits).toEqual([1000, 2000, 4000, 8000, 16000, 30000])
    expect(callbacks.onBusy).toHaveBeenCalledTimes(1)
    expect(callbacks.onEstablished).not.toHaveBeenCalled()
    expect(callbacks.onStatus).not.toHaveBeenCalledWith('connected')

    latest().refuse(WS_AT_CAPACITY_CODE)
    expect(callbacks.onStalled).toHaveBeenCalledWith('lost')
    const count = FakeSocket.instances.length
    vi.advanceTimersByTime(120_000)
    expect(FakeSocket.instances).toHaveLength(count)
  })

  it('announces a later capacity outage again after a real connection in between', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().refuse(WS_AT_CAPACITY_CODE)
    vi.advanceTimersByTime(1000)
    latest().establish()
    latest().refuse(WS_AT_CAPACITY_CODE) // same socket object, new outage
    expect(callbacks.onBusy).toHaveBeenCalledTimes(2)
  })

  it('ignores every event from a superseded socket', () => {
    const { socket, callbacks } = setup()
    socket.connect('s1')
    const first = FakeSocket.instances[0]
    first.open()
    socket.connect('s1') // e.g. a StrictMode remount or a manual reconnect
    const second = FakeSocket.instances[1]
    expect(first.closedByClient).toBe(true)
    callbacks.onDrop.mockClear()
    callbacks.onMessage.mockClear()

    // The old socket's close and a late frame arrive after it was replaced.
    first.drop()
    first.receive(JSON.stringify({ type: 'ping' }))
    vi.advanceTimersByTime(60_000)

    expect(callbacks.onDrop).not.toHaveBeenCalled()
    expect(callbacks.onMessage).not.toHaveBeenCalled()
    expect(FakeSocket.instances).toHaveLength(2) // no second reconnect loop
    second.open()
    expect(socket.isOpen).toBe(true)
  })

  it('close() stops everything, including a pending reconnect', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().open()
    latest().drop()
    socket.close()
    vi.advanceTimersByTime(60_000)
    expect(FakeSocket.instances).toHaveLength(1)

    // And a close() on a live socket fires no callbacks.
    socket.connect('s2')
    latest().open()
    callbacks.onDrop.mockClear()
    callbacks.onStatus.mockClear()
    socket.close()
    latest().drop()
    expect(callbacks.onDrop).not.toHaveBeenCalled()
    expect(callbacks.onStatus).not.toHaveBeenCalled()
    expect(socket.send({})).toBe(false)
  })

  it('reconnectNow() retries immediately and resets the backoff', () => {
    const { socket, callbacks, latest } = setup()
    socket.connect('s1')
    latest().drop(WS_SUPERSEDED_CODE)
    socket.reconnectNow()
    expect(FakeSocket.instances).toHaveLength(2)
    latest().establish()
    // A manual reconnect is announced like a first connection.
    expect(callbacks.onEstablished).toHaveBeenLastCalledWith(true)
  })
})
