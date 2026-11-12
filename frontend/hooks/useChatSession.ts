'use client'

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'

interface Options {
  avatarId: string
  /** Resume this existing session (from history) instead of creating one. */
  resumeSessionId?: string
  /** The session exists and can be connected to. */
  onReady(sessionId: string): void
  onFailed(): void
  /** Runs first on teardown, before the session is ended — close the socket here. */
  onTeardown(): void
}

/**
 * Create a chat session on mount (or adopt one being resumed) and end it on
 * unmount.
 *
 * Two things this has to get right that the inline version did not:
 *
 *   * Unmounting before `createSession` resolves. The cleanup used to run
 *     while the session id was still unknown, and the request's completion
 *     then opened a socket for a component that no longer existed — an orphan
 *     that reconnected forever and a session nobody ever ended. A `cancelled`
 *     flag now makes a late completion end the session and stop.
 *   * React StrictMode, which mounts every effect twice in development. The
 *     start is deferred a tick so the first (immediately torn down) mount
 *     never issues a request at all, instead of creating a throwaway session.
 *
 * Only sessions created here are ended on unmount; leaving a resumed
 * conversation must not mark the user's existing session as finished.
 */
export function useChatSession({ avatarId, resumeSessionId, onReady, onFailed, onTeardown }: Options) {
  const [sessionId, setSessionId] = useState<string | null>(null)

  const callbacks = useRef({ onReady, onFailed, onTeardown })
  useLayoutEffect(() => {
    callbacks.current = { onReady, onFailed, onTeardown }
  })

  useEffect(() => {
    let cancelled = false
    let createdId: string | null = null

    const timer = setTimeout(async () => {
      if (resumeSessionId) {
        setSessionId(resumeSessionId)
        callbacks.current.onReady(resumeSessionId)
        return
      }
      try {
        const data = await api.createSession(avatarId)
        if (cancelled) {
          api.endSession(data.id).catch(() => {})
          return
        }
        createdId = data.id
        setSessionId(data.id)
        callbacks.current.onReady(data.id)
      } catch {
        if (!cancelled) callbacks.current.onFailed()
      }
    }, 0)

    return () => {
      cancelled = true
      clearTimeout(timer)
      callbacks.current.onTeardown()
      if (createdId) api.endSession(createdId).catch(() => {})
    }
  }, [avatarId, resumeSessionId])

  return sessionId
}
