'use client'

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { useChunkPlayer } from '@/hooks/useChunkPlayer'
import { useRtcPlayer } from '@/hooks/useRtcPlayer'
import {
  AvatarTransport, routeMediaFrame, type AvatarChunk, type ClipQueue, type StreamView,
} from '@/lib/chat/avatarTransport'
import type { RtcSignal, WsMessage } from '@/lib/types'

/**
 * The avatar's media output, as the rest of the chat sees it.
 *
 * Deliberately small, and the same whichever transport is live: a queue of
 * per-sentence clips swapped onto one <video> (useChunkPlayer), or a single
 * continuous WebRTC stream in a second <video> beneath it (useRtcPlayer).
 * Nothing outside these hooks should reach for either element.
 */
export interface AvatarPlayer {
  /**
   * A sentence is ready. Returns true if it is now playing — a clip that
   * started the queue, or any sentence that went onto the live stream.
   */
  enqueue(chunk: AvatarChunk): boolean
  /**
   * Stop now: drop the clip queue and silence the stream. Always safe to
   * call. Local only — the caller sends `stop` / `interrupt`, which is what
   * makes the server flush the stream.
   */
  stop(): void
  setMuted(muted: boolean): void
  muted: boolean
  /** The avatar is talking (render state). */
  isPlaying: boolean
  /** Talking or about to, read synchronously — for timers and event handlers. */
  isActive(): boolean
  /**
   * The browser would not play sound without a tap, so the avatar is playing
   * muted. Cleared by `setMuted(false)` called from a user gesture.
   */
  audioBlocked: boolean
  /** How sentences are arriving right now. */
  transport: 'clips' | 'webrtc'
  /** A clip is on screen. */
  clipOnScreen: boolean
  /** The stream is on screen (its idle face counts). */
  streamOnScreen: boolean
  /** Feed every frame from the server; the ones about media are acted on. */
  handleFrame(message: WsMessage): void
  /** The socket closed: the peer connection that rode on it goes too. */
  handleSocketClosed(): void
  /**
   * Put the microphone on the live stream, or take it off (null). Resolves
   * false when there is no live stream that can carry one. The track stays
   * the caller's: nothing here ever stops it.
   */
  setMicTrack(track: MediaStreamTrack | null): Promise<boolean>
}

const NO_STREAM: StreamView = { live: false, speaking: false, suppressed: false }
const NO_CLIPS: ClipQueue = { enqueue: () => false, stop: () => {}, isActive: () => false }

/**
 * One player over both transports.
 *
 * A sentence the server delivers by URL while the stream is live (it could
 * not put that one on the stream) plays in the clip element, which sits above
 * the stream. AvatarTransport holds such a clip back until the stream has
 * stopped speaking, so the reply stays in order; and the stream is silenced
 * for as long as a clip is playing, so that even when that wait gives up the
 * two can never be heard together.
 *
 * Clips re-sent after the stream died (`resend`) arrive after the fallback
 * has already detached the stream and taken it off screen.
 */
export function useAvatarPlayer(
  videoRef: RefObject<HTMLVideoElement | null>,
  preloadRef: RefObject<HTMLVideoElement | null>,
  streamRef: RefObject<HTMLVideoElement | null>,
  /** Send a frame on the chat socket. False if it was not open. */
  send: (signal: RtcSignal) => boolean,
): AvatarPlayer {
  const clips = useChunkPlayer(videoRef, preloadRef)
  const [view, setView] = useState<StreamView>(NO_STREAM)

  // The user's mute choice, readable from handlers before the next render.
  const mutedRef = useRef(false)

  // One instance for the life of the component. The clip player and the
  // listener it is wired to are re-bound after every render (below), so it
  // always reaches the current ones.
  const transport = useMemo(() => new AvatarTransport(NO_CLIPS), [])

  // Unmount: a clip held back for the stream must not start, on a timer,
  // after the elements are gone. Declared before the stream's own cleanup,
  // which would otherwise release it to the clip queue on the way out.
  useEffect(() => () => transport.stop(), [transport])

  const rtc = useRtcPlayer(streamRef, {
    send,
    onLive: (live) => transport.setLive(live),
  })
  const { setSilenced, enableAudio } = rtc

  useLayoutEffect(() => {
    transport.bind(clips, (next) => {
      setView(next)
      // Not left to the effect below: a stop has to silence the stream now,
      // not a render later.
      setSilenced(mutedRef.current || next.suppressed || clips.isActive())
    })
  })

  // The stream is silent while the user has muted it, while a stop is waiting
  // for the server's flush, and while a clip is playing over it.
  const { muted, isPlaying: clipOnScreen, setMuted: setClipsMuted, isActive: clipsActive } = clips
  useEffect(() => {
    setSilenced(muted || view.suppressed || clipOnScreen)
  }, [setSilenced, muted, view.suppressed, clipOnScreen])

  const setMuted = useCallback(
    (next: boolean) => {
      mutedRef.current = next
      setClipsMuted(next)
      setSilenced(next || transport.view.suppressed || clipsActive())
      // Called from a click or key press — the gesture the autoplay policy
      // wants — so a blocked stream can be given its sound back here.
      if (!next) enableAudio()
    },
    [setClipsMuted, setSilenced, enableAudio, transport, clipsActive],
  )

  const { ready, answer, serverError, mediaTransport, socketClosed, setMicTrack } = rtc
  const handleFrame = useCallback(
    (message: WsMessage) => routeMediaFrame(message, { ready, answer, serverError, mediaTransport }, transport),
    [ready, answer, serverError, mediaTransport, transport],
  )

  return useMemo<AvatarPlayer>(
    () => ({
      enqueue: (chunk) => transport.enqueue(chunk),
      stop: () => transport.stop(),
      isActive: () => transport.isActive(),
      setMuted,
      muted,
      isPlaying: clipOnScreen || view.speaking,
      audioBlocked: clips.audioBlocked || (view.live && rtc.audioBlocked),
      transport: view.live ? 'webrtc' : 'clips',
      clipOnScreen,
      streamOnScreen: view.live && rtc.hasPicture && !view.suppressed,
      handleFrame,
      handleSocketClosed: socketClosed,
      setMicTrack,
    }),
    [
      transport, setMuted, muted, clipOnScreen, view, clips.audioBlocked, rtc.audioBlocked,
      rtc.hasPicture, handleFrame, socketClosed, setMicTrack,
    ],
  )
}
