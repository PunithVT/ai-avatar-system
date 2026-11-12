import type { WsMessage } from '@/lib/types'

/**
 * The token is missing, invalid or expired. Not retryable. Only this — the
 * server used to send it for an unknown session and for its own database
 * errors too, which made it unsafe to act on.
 */
export const WS_AUTH_REJECT_CODE = 4401
/**
 * The session does not exist or belongs to someone else — typically the
 * conversation was deleted from another tab. Not retryable, and nothing to
 * do with the user's credentials.
 */
export const WS_GONE_CODE = 4404
/** Standard "try again later" — the server is at its connection cap. */
export const WS_AT_CAPACITY_CODE = 1013
/**
 * The server closed this socket because a newer one opened the same session
 * — another tab, or this tab's own reconnect. Reconnecting would just evict
 * the newer socket in turn, and the two would trade places forever.
 */
export const WS_SUPERSEDED_CODE = 4409
export const MAX_WS_RECONNECT_ATTEMPTS = 6
/**
 * How long an open socket may stay silent before it is treated as
 * established anyway. The server sends a `ready` frame right after a
 * successful connect, so this only matters against a server that does not.
 */
export const WS_ESTABLISHED_FALLBACK_MS = 2000

export type SocketStatus = 'connecting' | 'connected' | 'disconnected'

/**
 * Why reconnecting stopped: the token was rejected, the conversation no
 * longer exists, the session was taken over by another connection, or the
 * retries ran out.
 */
export type StallReason = 'auth' | 'gone' | 'replaced' | 'lost'

export interface SessionSocketCallbacks {
  onStatus(status: SocketStatus): void
  /**
   * The transport opened and frames can be sent. NOT proof of a usable
   * connection: the server accepts before it refuses (so that its close code
   * reaches the browser), which means this fires for refused connections
   * too. Use it only to replay per-connection settings.
   */
  onOpen(): void
  /**
   * The server is really talking to us (see `establish`). `fresh` is false
   * when this is a reconnect.
   */
  onEstablished?(fresh: boolean): void
  onMessage(message: WsMessage): void
  /** The live socket closed. Fires before any reconnect is scheduled. */
  onDrop(code: number): void
  /** Reconnecting has been given up on; the UI should offer a manual retry. */
  onStalled(reason: StallReason): void
  /** The server is at its connection cap; a retry is already scheduled. */
  onBusy?(): void
}

type SocketCtor = new (url: string) => WebSocket

/**
 * One chat session's WebSocket, with reconnect.
 *
 * The rule that makes this safe is that only the CURRENT socket may touch
 * state. Every handler first checks it still belongs to `this.ws`; a socket
 * that has been replaced or deliberately closed is inert. Without that check
 * a superseded socket's late `onclose` scheduled a second reconnect loop
 * alongside the live connection — and since the server keys connections by
 * session id, the two then kept closing each other.
 *
 * Plain class, no React: reconnect timing is exactly the kind of logic that
 * is easy to get subtly wrong and awkward to test through a component.
 */
export class SessionSocket {
  private ws: WebSocket | null = null
  private sessionId: string | null = null
  private attempts = 0
  private timer: ReturnType<typeof setTimeout> | null = null
  private establishTimer: ReturnType<typeof setTimeout> | null = null
  /** The current socket has been confirmed as a working connection. */
  private established = false
  /** A connection has been established since the last connect()/reconnectNow(). */
  private everEstablished = false
  private busyAnnounced = false

  constructor(
    private readonly buildUrl: (sessionId: string) => string,
    private readonly callbacks: SessionSocketCallbacks,
    private readonly Socket: SocketCtor = WebSocket,
  ) {}

  get isOpen(): boolean {
    return this.ws !== null && this.ws.readyState === 1 /* OPEN */
  }

  /** Attach to a session. Replaces any existing connection. */
  connect(sessionId: string): void {
    this.sessionId = sessionId
    this.attempts = 0
    this.everEstablished = false
    this.busyAnnounced = false
    this.clearTimer()
    this.open()
  }

  /** Retry immediately, forgetting the backoff. */
  reconnectNow(): void {
    if (!this.sessionId) return
    this.attempts = 0
    this.everEstablished = false
    this.busyAnnounced = false
    this.clearTimer()
    this.open()
  }

  /** Close for good. No callbacks fire and nothing reconnects afterwards. */
  close(): void {
    this.sessionId = null
    this.clearTimer()
    this.detach()
  }

  /**
   * Send a JSON frame. Returns false — rather than throwing, as `send()` on
   * a CONNECTING socket does — when the socket is not open, so callers in
   * timers and animation loops cannot be killed by a reconnect in progress.
   */
  send(payload: unknown): boolean {
    if (!this.isOpen || !this.ws) return false
    this.ws.send(JSON.stringify(payload))
    return true
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  private clearEstablishTimer(): void {
    if (this.establishTimer !== null) {
      clearTimeout(this.establishTimer)
      this.establishTimer = null
    }
  }

  /**
   * Mark the current socket as a working connection: reset the backoff,
   * report `connected`.
   *
   * This is deliberately NOT done in `onopen`. The server accepts a socket
   * and then closes it with 4401 / 4404 / 1013 when it is refusing the
   * connection, so `onopen` fires for refusals as well. Resetting the retry
   * counter there meant a server at capacity was retried every second,
   * forever, with a "connected" flash and a toast each time. A frame from
   * the server is the first real evidence of a session.
   */
  private establish(ws: WebSocket): void {
    if (this.ws !== ws || this.established) return
    this.established = true
    this.clearEstablishTimer()
    this.attempts = 0
    this.busyAnnounced = false
    const fresh = !this.everEstablished
    this.everEstablished = true
    this.callbacks.onStatus('connected')
    this.callbacks.onEstablished?.(fresh)
  }

  /** Drop the current socket so that none of its handlers act again. */
  private detach(): void {
    this.clearEstablishTimer()
    const old = this.ws
    this.ws = null
    if (old) {
      try {
        old.close()
      } catch {
        /* already closed */
      }
    }
  }

  private open(): void {
    const sessionId = this.sessionId
    if (!sessionId) return
    this.detach()

    this.established = false
    this.callbacks.onStatus('connecting')
    const ws = new this.Socket(this.buildUrl(sessionId))
    this.ws = ws

    ws.onopen = () => {
      if (this.ws !== ws) return
      this.callbacks.onOpen()
      // Backstop for a server that sends nothing after connecting.
      this.establishTimer = setTimeout(() => {
        this.establishTimer = null
        this.establish(ws)
      }, WS_ESTABLISHED_FALLBACK_MS)
    }

    ws.onmessage = (event: MessageEvent) => {
      if (this.ws !== ws) return
      // Any frame at all — `ready`, a ping, a malformed one — is the server
      // talking to this session.
      this.establish(ws)
      let parsed: WsMessage
      try {
        parsed = JSON.parse(event.data as string)
      } catch {
        return // a malformed frame must not take the handler down
      }
      this.callbacks.onMessage(parsed)
    }

    // `error` is always followed by `close`; the close handler does the work.
    ws.onerror = () => {}

    ws.onclose = (event: CloseEvent) => {
      if (this.ws !== ws) return
      this.ws = null
      this.clearEstablishTimer()
      this.callbacks.onStatus('disconnected')
      this.callbacks.onDrop(event.code)

      if (event.code === WS_AUTH_REJECT_CODE) {
        this.callbacks.onStalled('auth')
        return
      }
      if (event.code === WS_GONE_CODE) {
        this.callbacks.onStalled('gone')
        return
      }
      if (event.code === WS_SUPERSEDED_CODE) {
        this.callbacks.onStalled('replaced')
        return
      }
      if (!this.sessionId) return
      if (this.attempts >= MAX_WS_RECONNECT_ATTEMPTS) {
        this.callbacks.onStalled('lost')
        return
      }
      // Tell the user once per outage rather than on every retry.
      if (event.code === WS_AT_CAPACITY_CODE && !this.busyAnnounced) {
        this.busyAnnounced = true
        this.callbacks.onBusy?.()
      }
      const delay = Math.min(1000 * 2 ** this.attempts, 30_000)
      this.attempts += 1
      this.timer = setTimeout(() => {
        this.timer = null
        this.open()
      }, delay)
    }
  }
}
