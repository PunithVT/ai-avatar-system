'use client'

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { toast } from 'react-hot-toast'
import { api, buildSessionWsUrl } from '@/lib/api'
import { errorStatus } from '@/lib/errors'
import { SessionSocket, type SocketStatus, type StallReason } from '@/lib/chat/sessionSocket'
import { useStore } from '@/store/useStore'
import type { WsMessage } from '@/lib/types'

interface Handlers {
  onMessage(message: WsMessage): void
  /**
   * Fires each time the transport opens, including reconnects — and also for
   * connections the server then refuses. The server keeps per-socket
   * settings (language) only for the life of a connection, so replay them
   * here; `send` is the freshly opened socket.
   */
  onOpen?(send: (payload: unknown) => boolean): void
  /** The connection dropped. Any turn in flight is gone with it. */
  onDrop?(): void
}

export interface ChatSocket {
  status: SocketStatus
  /** Why reconnecting stopped, or null while connected / still retrying. */
  stalled: StallReason | null
  connect(sessionId: string): void
  disconnect(): void
  reconnect(): void
  /** False if the socket was not open and nothing was sent. */
  send(payload: unknown): boolean
}

/**
 * React binding for SessionSocket.
 *
 * Handlers are read through a ref on every event, so they always see the
 * current render's state. The previous inline version memoised the socket's
 * `onmessage` once, which pinned it to the first render's closure — the
 * reason the mute button was ignored for the first chunk of every reply.
 */
export function useChatSocket(handlers: Handlers): ChatSocket {
  const setWsConnected = useStore((s) => s.setWsConnected)
  const [status, setStatus] = useState<SocketStatus>('connecting')
  const [stalled, setStalled] = useState<StallReason | null>(null)
  const clearAuth = useStore((s) => s.clearAuth)

  const handlersRef = useRef(handlers)
  useLayoutEffect(() => {
    handlersRef.current = handlers
  })

  const socketRef = useRef<SessionSocket | null>(null)

  useEffect(() => {
    let alive = true
    const socket = new SessionSocket(buildSessionWsUrl, {
      onStatus: (next) => {
        setStatus(next)
        setWsConnected(next === 'connected')
        if (next === 'connected') setStalled(null)
      },
      // The transport is open: replay per-connection settings. Not yet a
      // working connection — see onEstablished.
      onOpen: () => handlersRef.current.onOpen?.((payload) => socket.send(payload)),
      onEstablished: (fresh) => {
        if (fresh) toast.success('Connected to avatar!', { icon: '✨' })
      },
      onMessage: (message) => handlersRef.current.onMessage(message),
      onDrop: () => handlersRef.current.onDrop?.(),
      onStalled: (reason) => {
        setStalled(reason)
        if (reason === 'auth') {
          // The server says the token is no good. Do not take a close code's
          // word for it: signing out discards the token, and for a guest the
          // token is the ONLY way back into the account. Ask the API. A 401
          // there is conclusive; anything else means the credentials are
          // fine and it is this conversation that cannot be reached.
          api.getProfile().then(
            () => {
              if (alive) setStalled('gone')
            },
            (err: unknown) => {
              if (!alive) return
              if (errorStatus(err) === 401) {
                toast.error('Your session has expired — please sign in again.')
                // Dropping the token brings up the sign-in gate, which also
                // unmounts this chat. (The API client clears it too when the
                // request carried a bearer token; this covers cookie-only.)
                clearAuth()
              } else {
                // Could not even ask — treat it as a lost connection, which
                // offers a manual retry and leaves the credentials alone.
                setStalled('lost')
                toast.error('Lost connection to avatar.')
              }
            },
          )
        } else if (reason === 'lost') {
          toast.error('Lost connection to avatar.')
        }
        // 'replaced' and 'gone' are deliberately quiet: the user opened or
        // deleted this conversation somewhere else, which is not an error.
        // The stage says what happened and what to do next.
      },
      onBusy: () => toast('The server is busy — retrying…', { icon: '⏳' }),
    })
    socketRef.current = socket
    return () => {
      alive = false
      socket.close()
      socketRef.current = null
      setWsConnected(false)
    }
  }, [setWsConnected, clearAuth])

  const connect = useCallback((sessionId: string) => socketRef.current?.connect(sessionId), [])
  const disconnect = useCallback(() => socketRef.current?.close(), [])
  const reconnect = useCallback(() => {
    setStalled(null)
    socketRef.current?.reconnectNow()
  }, [])
  const send = useCallback((payload: unknown) => socketRef.current?.send(payload) ?? false, [])

  return { status, stalled, connect, disconnect, reconnect, send }
}
