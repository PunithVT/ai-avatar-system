import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AvatarTransport, routeMediaFrame, type ClipQueue } from '@/lib/chat/avatarTransport'
import { RtcSession, transportOf, type RtcPeer } from '@/lib/chat/rtcSession'
import type { RtcSignal, WsMessage } from '@/lib/types'

/**
 * The two halves wired the way useAvatarPlayer wires them, fed the frames a
 * server sends. Covers what neither half shows alone: that a frame on the
 * socket ends up as the right playback decision.
 */
function setup() {
  const sent: RtcSignal[] = []
  const urls: string[] = []
  const stops = { count: 0 }
  let peers = 0
  const clips: ClipQueue = {
    enqueue: (url) => {
      urls.push(url)
      return urls.length === 1
    },
    stop: () => {
      stops.count += 1
    },
    isActive: () => false,
  }
  const transport = new AvatarTransport(clips)
  const peer: RtcPeer = {
    addTransceiver: () => {},
    setMicTrack: async () => {},
    createOffer: async () => ({ type: 'offer', sdp: 'OFFER' }),
    setLocalDescription: async () => {},
    setRemoteDescription: async () => {},
    localSdp: () => 'OFFER',
    iceGatheringState: () => 'complete',
    close: () => {},
  }
  const session = new RtcSession({
    supported: () => true,
    createPeer: () => {
      peers += 1
      return peer
    },
    send: (signal) => {
      sent.push(signal)
      return true
    },
    attach: () => {},
    detach: () => {},
    onChange: (state) => transport.setLive(transportOf(state) === 'webrtc'),
  })
  const receive = async (...frames: WsMessage[]) => {
    for (const frame of frames) {
      routeMediaFrame(frame, session, transport)
      await vi.advanceTimersByTimeAsync(0)
    }
  }
  /**
   * What ChatInterface does with a frame: the player first, then its own
   * handling of the two frame types that reach the player a second time.
   */
  const chat = async (...frames: WsMessage[]) => {
    for (const frame of frames) {
      await receive(frame)
      if (frame.type === 'video_chunk') transport.enqueue(frame)
      if (frame.type === 'interrupted') transport.stop()
    }
  }
  return { sent, urls, stops, transport, session, receive, chat, peerCount: () => peers }
}

const chunk = (extra: object): WsMessage =>
  ({ type: 'video_chunk', chunk_index: 0, total_chunks: -1, text: 'Hi.', ...extra }) as WsMessage

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('media frames', () => {
  it('an older server (ready without media) behaves exactly as before', async () => {
    const { sent, urls, transport, receive, peerCount } = setup()
    await receive({ type: 'ready' })
    expect(peerCount()).toBe(0)
    expect(sent).toEqual([])
    expect(transport.view).toEqual({ live: false, speaking: false, suppressed: false })

    // The frames that server does send pass through untouched…
    await receive(
      { type: 'video_chunk_start', total_chunks: -1 }, { type: 'status', message: 'Animating…' },
      { type: 'ping' }, { type: 'interrupted', message: 'stopped' }, { type: 'video_chunk_end', sent_chunks: 1 },
    )
    expect(sent).toEqual([])
    expect(transport.view).toEqual({ live: false, speaking: false, suppressed: false })

    // …and its clips play by URL.
    expect(transport.enqueue({ video_url: 'https://cdn/a.mp4' })).toBe(true)
    expect(urls).toEqual(['https://cdn/a.mp4'])
  })

  it('ready with media.webrtc false sends no offer', async () => {
    const { sent, receive, peerCount } = setup()
    await receive({ type: 'ready', media: { webrtc: false, ice_servers: [] } })
    expect(peerCount()).toBe(0)
    expect(sent).toEqual([])
  })

  it('negotiates, then plays stream chunks without fetching anything', async () => {
    const { sent, urls, transport, receive } = setup()
    await receive({ type: 'ready', media: { webrtc: true, ice_servers: [{ urls: ['stun:s'] }] } })
    expect(sent).toEqual([{ type: 'webrtc_offer', sdp: 'OFFER' }])

    // Until the server announces the stream, clips arrive — and play — as today.
    await receive({ type: 'webrtc_answer', sdp: 'ANSWER' })
    expect(transport.view.live).toBe(false)
    expect(transport.enqueue({ video_url: 'https://cdn/early.mp4' })).toBe(true)

    await receive({ type: 'media_transport', transport: 'webrtc' })
    expect(transport.view.live).toBe(true)

    const frame = chunk({ transport: 'webrtc', duration_ms: 1200 })
    expect(frame.type === 'video_chunk' && transport.enqueue(frame)).toBe(true)
    expect(urls).toEqual(['https://cdn/early.mp4'])

    await receive({ type: 'speaking', active: true })
    expect(transport.isActive()).toBe(true)
    await receive({ type: 'speaking', active: false })
    expect(transport.isActive()).toBe(false)
  })

  it('webrtc_error twice leaves the session on clips with nothing lost', async () => {
    const { sent, urls, transport, session, receive } = setup()
    await receive(
      { type: 'ready', media: { webrtc: true } },
      { type: 'webrtc_error', message: 'aiortc unavailable' },
      { type: 'webrtc_error', message: 'aiortc unavailable' },
    )
    expect(sent.filter((s) => s.type === 'webrtc_offer')).toHaveLength(2)
    expect(session.phase).toBe('fallen-back')
    await receive({ type: 'webrtc_error', message: 'again' })
    expect(sent.filter((s) => s.type === 'webrtc_offer')).toHaveLength(2)

    transport.enqueue({ video_url: 'https://cdn/a.mp4' })
    expect(urls).toEqual(['https://cdn/a.mp4'])
  })

  it('the server dropping the stream mid-reply hands the rest to clips', async () => {
    const { urls, transport, receive } = setup()
    await receive(
      { type: 'ready', media: { webrtc: true } },
      { type: 'webrtc_answer', sdp: 'ANSWER' },
      { type: 'media_transport', transport: 'webrtc' },
      { type: 'speaking', active: true },
      { type: 'media_transport', transport: 'clips' },
    )
    expect(transport.view).toEqual({ live: false, speaking: false, suppressed: false })
    expect(transport.isActive()).toBe(false)
    expect(transport.enqueue({ video_url: 'https://cdn/rest.mp4' })).toBe(true)
    expect(urls).toEqual(['https://cdn/rest.mp4'])
  })

  it('a malformed speaking frame does not leave the avatar talking', async () => {
    const { transport, receive } = setup()
    await receive(
      { type: 'ready', media: { webrtc: true } },
      { type: 'webrtc_answer', sdp: 'ANSWER' },
      { type: 'media_transport', transport: 'webrtc' },
      { type: 'speaking', active: 'yes' } as unknown as WsMessage,
    )
    expect(transport.isActive()).toBe(false)
  })

  const LIVE: WsMessage[] = [
    { type: 'ready', media: { webrtc: true } },
    { type: 'webrtc_answer', sdp: 'ANSWER' },
    { type: 'media_transport', transport: 'webrtc' },
  ]

  it('a stream that dies mid-reply: the unplayed sentences come back as clips, in order', async () => {
    const { urls, sent, transport, chat } = setup()
    await chat(
      ...LIVE,
      { type: 'video_chunk_start', total_chunks: -1 },
      chunk({ chunk_index: 0, transport: 'webrtc', duration_ms: 900 }),
      { type: 'speaking', active: true },
      chunk({ chunk_index: 1, transport: 'webrtc', duration_ms: 900 }),
    )
    expect(transport.isActive()).toBe(true)
    expect(urls).toEqual([])

    // The server's side: stream down, then everything not fully played.
    await chat(
      { type: 'speaking', active: false },
      { type: 'media_transport', transport: 'clips' },
    )
    expect(transport.view.live).toBe(false) // off screen before any re-send plays
    await chat(
      chunk({ chunk_index: 0, video_url: 'https://cdn/0.mp4', resend: true }),
      chunk({ chunk_index: 1, video_url: 'https://cdn/1.mp4', resend: true }),
      chunk({ chunk_index: 2, video_url: 'https://cdn/2.mp4' }),
      { type: 'video_chunk_end', sent_chunks: 3 },
    )
    expect(urls).toEqual(['https://cdn/0.mp4', 'https://cdn/1.mp4', 'https://cdn/2.mp4'])
    // We closed our peer in answer, and said so before offering again.
    expect(sent.map((s) => s.type)).toEqual(['webrtc_offer', 'webrtc_close', 'webrtc_offer'])
  })

  it('a URL chunk while the stream is speaking is not started until it stops', async () => {
    const { urls, transport, chat } = setup()
    await chat(
      ...LIVE,
      { type: 'video_chunk_start', total_chunks: -1 },
      chunk({ chunk_index: 0, transport: 'webrtc', duration_ms: 900 }),
      { type: 'speaking', active: true },
      chunk({ chunk_index: 1, video_url: 'https://cdn/1.mp4' }),
    )
    expect(urls).toEqual([])
    expect(transport.isActive()).toBe(true)
    await chat({ type: 'speaking', active: false })
    expect(urls).toEqual(['https://cdn/1.mp4'])
  })

  it('chunk numbering starts again with each video_chunk_start', async () => {
    const { urls, chat } = setup()
    await chat(
      { type: 'ready' },
      { type: 'video_chunk_start', total_chunks: -1 },
      chunk({ chunk_index: 0, video_url: 'https://cdn/a0.mp4' }),
      chunk({ chunk_index: 0, video_url: 'https://cdn/a0.mp4' }), // a duplicate
      { type: 'video_chunk_end', sent_chunks: 1 },
      { type: 'video_chunk_start', total_chunks: -1 },
      chunk({ chunk_index: 0, video_url: 'https://cdn/b0.mp4' }),
    )
    expect(urls).toEqual(['https://cdn/a0.mp4', 'https://cdn/b0.mp4'])
  })

  it.each([
    ['interrupted, then speaking:false', [{ type: 'interrupted', message: 'x' }, { type: 'speaking', active: false }]],
    ['speaking:false, then interrupted', [{ type: 'speaking', active: false }, { type: 'interrupted', message: 'x' }]],
  ] as Array<[string, WsMessage[]]>)('interrupted with no turn in flight (%s) leaves the stream idle and audible', async (_name, acks) => {
    const { transport, chat } = setup()
    await chat(...LIVE, chunk({ transport: 'webrtc', duration_ms: 900 }), { type: 'speaking', active: true })
    // The turn's video_chunk_end is long past; the stream is still talking.
    await chat(...acks)
    expect(transport.view).toEqual({ live: true, speaking: false, suppressed: false })
    expect(transport.isActive()).toBe(false)

    await chat(
      { type: 'video_chunk_start', total_chunks: -1 },
      chunk({ transport: 'webrtc', duration_ms: 900 }),
      { type: 'speaking', active: true },
    )
    expect(transport.view).toEqual({ live: true, speaking: true, suppressed: false })
  })
})
