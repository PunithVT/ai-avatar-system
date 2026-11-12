'use client'

import { useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { ChunkSequencer, type Clip } from '@/lib/chat/chunkSequencer'

/**
 * The clip transport: a queue of per-sentence MP4s swapped onto one <video>.
 *
 * One of the two halves behind AvatarPlayer (see useAvatarPlayer); the other
 * is the WebRTC stream. The chat talks to AvatarPlayer, never to this.
 */
export interface ClipPlayer {
  /** Queue a clip. Returns true if playback was idle and this clip started it. */
  enqueue(url: string): boolean
  /** Stop now: pause, drop the queue, release the media. Always safe to call. */
  stop(): void
  setMuted(muted: boolean): void
  muted: boolean
  /** A clip is on screen (render state). */
  isPlaying: boolean
  /** Playing or queued, read synchronously — for timers and event handlers. */
  isActive(): boolean
  /**
   * The browser would not play sound without a tap, so clips are playing
   * muted. Cleared by `setMuted(false)` called from a user gesture.
   */
  audioBlocked: boolean
}

export function useChunkPlayer(
  videoRef: RefObject<HTMLVideoElement | null>,
  preloadRef: RefObject<HTMLVideoElement | null>,
): ClipPlayer {
  const [isPlaying, setIsPlaying] = useState(false)
  const [muted, setMutedState] = useState(false)
  const mutedRef = useRef(false)
  const [audioBlocked, setAudioBlockedState] = useState(false)
  const audioBlockedRef = useRef(false)

  const controller = useMemo(() => {
    const sequencer = new ChunkSequencer()

    // The clip a muted pause has already been retried for (see onPolicyPause).
    let resumedToken: number | null = null

    function setAudioBlocked(next: boolean) {
      if (audioBlockedRef.current === next) return
      audioBlockedRef.current = next
      setAudioBlockedState(next)
    }

    function release(el: HTMLVideoElement | null) {
      if (!el) return
      el.pause()
      // removeAttribute + load() returns the element to its empty state
      // without the `error` event that assigning src = '' fires — which
      // would arrive later and be mistaken for the next clip failing.
      el.removeAttribute('src')
      el.load()
    }

    function start(clip: Clip) {
      setIsPlaying(true)
      const video = videoRef.current
      if (!video) return
      video.src = clip.url
      // Always START muted. Browsers allow muted autoplay unconditionally,
      // while unmuted autoplay is refused once the LLM + TTS + animation
      // pipeline has outlasted the brief window after the user's gesture.
      // In Chromium, unmuting an element that is already playing needs no
      // gesture once the page has been interacted with, so the user's choice
      // is restored as soon as play() resolves. WebKit instead PAUSES a clip
      // un-muted without a gesture — handled in onPolicyPause below. Once
      // that has happened, later clips stay muted until the user taps.
      video.muted = true
      video
        .play()
        .then(() => {
          if (sequencer.activeToken === clip.token) {
            video.muted = mutedRef.current || audioBlockedRef.current
          }
        })
        // A rejected play() must not strand the queue. It carries its own
        // clip's token, so if the `error` event already advanced past this
        // clip the rejection is stale and does nothing.
        .catch(() => signal(clip.token))

      const upcoming = sequencer.peek()
      if (upcoming && preloadRef.current) preloadRef.current.src = upcoming
    }

    function signal(token: number | null) {
      if (token === null) return
      const next = sequencer.advance(token)
      if (next === null) return
      if (next === 'idle') setIsPlaying(false)
      else start(next)
    }

    return {
      enqueue(url: string): boolean {
        const clip = sequencer.enqueue(url)
        if (clip) {
          start(clip)
          return true
        }
        // Already playing — warm the cache for whichever clip is next.
        const upcoming = sequencer.peek()
        const preload = preloadRef.current
        if (upcoming && preload && preload.getAttribute('src') !== upcoming) preload.src = upcoming
        return false
      },
      stop() {
        sequencer.stop()
        setIsPlaying(false)
        release(videoRef.current)
        release(preloadRef.current)
      },
      isActive: () => sequencer.hasWork,
      /** The clip on screen ended or failed. */
      signalActive: () => signal(sequencer.activeToken),
      /**
       * The element paused while a clip was meant to be playing, and not
       * because we stopped it (stop() clears the active token first) or
       * because it finished.
       *
       * The usual cause is WebKit's autoplay policy pausing a clip the
       * moment it is un-muted outside a user gesture. Neither `ended` nor
       * `error` follows, so the queue used to sit there forever. Skipping
       * the clip would not help — every later one would be paused the same
       * way — so fall back to what IS allowed: play it muted, and ask for a
       * tap to restore sound.
       */
      onPolicyPause(video: HTMLVideoElement) {
        const token = sequencer.activeToken
        if (token === null || video.ended || video.error) return
        // `pause` is delivered asynchronously. By now the element may already
        // be playing again — typically the pause belonged to a stop() and a
        // new clip has started since. Only a clip that is paused NOW counts.
        if (!video.paused) return
        if (!video.muted) {
          setAudioBlocked(true)
          video.muted = true
          video.play().catch(() => signal(token))
          return
        }
        // Paused although already muted, so sound is not the reason. Try
        // once more; if this clip pauses again, move on rather than strand
        // the rest of the reply.
        if (resumedToken === token) {
          signal(token)
          return
        }
        resumedToken = token
        video.play().catch(() => signal(token))
      },
      /** The user asked for sound, from a gesture. */
      enableAudio(video: HTMLVideoElement | null) {
        setAudioBlocked(false)
        if (!video) return
        video.muted = false
        const token = sequencer.activeToken
        if (token !== null && video.paused && !video.ended) {
          video.play().catch(() => signal(token))
        }
      },
    }
  }, [videoRef, preloadRef])

  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const onEnded = () => controller.signalActive()
    // A 404, an expired presigned URL or an undecodable clip fires `error`,
    // not `ended`. `video.error` is cleared when a new source starts loading,
    // so a late event about a clip that has since been replaced is ignored.
    const onError = () => {
      if (video.error) controller.signalActive()
    }
    const onPause = () => controller.onPolicyPause(video)
    video.addEventListener('ended', onEnded)
    video.addEventListener('error', onError)
    video.addEventListener('pause', onPause)
    return () => {
      video.removeEventListener('ended', onEnded)
      video.removeEventListener('error', onError)
      video.removeEventListener('pause', onPause)
      // Leaving mid-reply must not leave the clip playing with no UI.
      controller.stop()
    }
  }, [controller, videoRef])

  return useMemo<ClipPlayer>(
    () => ({
      enqueue: controller.enqueue,
      stop: controller.stop,
      isActive: controller.isActive,
      setMuted(next: boolean) {
        mutedRef.current = next
        setMutedState(next)
        const video = videoRef.current
        if (!next) {
          // Called from a click or key press, which is exactly the gesture
          // the autoplay policy wants: restore sound, and resume a clip the
          // policy paused.
          controller.enableAudio(video)
          return
        }
        // While a clip is still in its muted-autoplay start, leave the
        // element alone; the play() continuation applies the choice.
        if (video && !video.paused) video.muted = true
      },
      muted,
      isPlaying,
      audioBlocked,
    }),
    [controller, muted, isPlaying, audioBlocked, videoRef],
  )
}
