'use client'

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { MIC_CONSTRAINTS, closeContext, openAnalyser, readLevel, stopStream } from '@/lib/chat/audio'
import { MicSession, type MicMode } from '@/lib/chat/micMode'
import type { MicSignal, WsMessage } from '@/lib/types'

interface Options {
  /** The user has hands-free switched on. */
  wanted: boolean
  /** The WebRTC stream is live. */
  live: boolean
  /**
   * Put a track on the stream's audio sender, or take it off (null).
   * Resolves false if there is no live stream that can carry it.
   */
  setTrack(track: MediaStreamTrack | null): Promise<boolean>
  /** Send a frame on the chat socket. False if it was not open. */
  send(signal: MicSignal): boolean
  /** The server reports the user speaking. Read every frame, for the meter. */
  isSpeaking(): boolean
  /** Live 0-100 input level for the meter (0 unless the user is speaking). */
  levelRef: RefObject<number>
}

export interface StreamMic {
  /** Which hands-free implementation is in charge right now. */
  mode: MicMode
  /** Run the browser detector (useHandsFree's `enabled`). */
  localEnabled: boolean
  /** The same as `mode === 'stream'`, read synchronously — for event handlers. */
  isStream(): boolean
  /** Feed every frame from the server; the ones about the microphone are acted on. */
  handleFrame(message: WsMessage): void
  /** The socket closed: nothing can be listening on its stream any more. */
  handleSocketClosed(): void
}

/** What an attached microphone holds open, so letting go is one place. */
interface Held {
  stream: MediaStream
  track: MediaStreamTrack
  ctx: AudioContext | null
  raf: number
}

/**
 * Hands-free, whichever way it runs: decides between the browser detector
 * and the stream microphone (MicSession) and performs the stream half —
 * getUserMedia, the track onto the peer connection, `mic active` on the
 * socket. The browser detector itself stays in useHandsFree, switched by
 * `localEnabled`.
 *
 * In stream mode this hook does no turn-taking at all. The analyser it opens
 * on the microphone feeds the level meter and nothing else.
 */
export function useStreamMic(options: Options): StreamMic {
  const [mode, setMode] = useState<MicMode>('off')
  const [localEnabled, setLocalEnabled] = useState(false)
  // Bumped on every stop, so the effect that reports the detector's release
  // runs even when a stop and a start cancel out within one render.
  const [stopCount, setStopCount] = useState(0)

  const optionsRef = useRef(options)
  useLayoutEffect(() => {
    optionsRef.current = options
  })

  const sessionRef = useRef<MicSession | null>(null)

  // Created in an effect, like the peer connection's session, so that
  // StrictMode's throwaway first mount shuts its own one down. Declared
  // first: the effects below feed it, and on unmount everything is let go
  // before they run their own cleanups.
  useEffect(() => {
    let held: Held | null = null
    // Bumped by every acquire and every detach. A getUserMedia or a
    // replaceTrack that resolves for a superseded generation releases what
    // it got and reports nothing.
    let generation = 0

    const release = () => {
      generation += 1
      const current = held
      held = null
      if (!current) return
      cancelAnimationFrame(current.raf)
      closeContext(current.ctx)
      current.track.onended = null
      // Off the sender first, so the last thing the server receives is not
      // an abruptly ended track. The browser queues this behind any
      // replaceTrack still in flight, so null always wins.
      optionsRef.current.setTrack(null).catch(() => {})
      stopStream(current.stream)
      optionsRef.current.levelRef.current = 0
    }

    /** Display only: the level bar, while the server says the user is speaking. */
    const startMeter = (current: Held) => {
      try {
        // The same light smoothing the browser detector uses, so the bar
        // moves the same way in both modes.
        const { ctx, analyser } = openAnalyser(current.stream, 256, 0.2)
        current.ctx = ctx
        const buffer = new Uint8Array(analyser.frequencyBinCount)
        let showing = false
        const paint = () => {
          const { isSpeaking, levelRef } = optionsRef.current
          if (isSpeaking()) {
            levelRef.current = readLevel(analyser, buffer)
            showing = true
          } else if (showing) {
            levelRef.current = 0
            showing = false
          }
          current.raf = requestAnimationFrame(paint)
        }
        current.raf = requestAnimationFrame(paint)
      } catch {
        /* no meter; the microphone itself is unaffected */
      }
    }

    const session: MicSession = new MicSession({
      startLocal: () => setLocalEnabled(true),
      stopLocal: () => {
        setLocalEnabled(false)
        setStopCount((n) => n + 1)
      },
      acquire: () => {
        const gen = ++generation
        const failed = () => {
          if (gen !== generation) return
          release()
          session.trackFailed()
        }
        if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
          failed()
          return
        }
        // No toast on failure here: the fallback is the browser detector,
        // which asks for the same microphone and explains what is wrong if
        // it cannot have it either.
        navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS).then(
          async (stream) => {
            // The permission prompt can sit for seconds. If hands-free was
            // switched off or the stream died meanwhile, release — do not
            // attach after teardown and leave a microphone nobody stops.
            if (gen !== generation) {
              stopStream(stream)
              return
            }
            const [track] = stream.getAudioTracks()
            if (!track) {
              stopStream(stream)
              failed()
              return
            }
            const current: Held = { stream, track, ctx: null, raf: 0 }
            held = current
            // Unplugged, or revoked from the browser's site settings.
            track.onended = failed
            const attached = await optionsRef.current.setTrack(track).catch(() => false)
            if (gen !== generation) return
            if (!attached) {
              failed()
              return
            }
            startMeter(current)
            session.trackAttached()
          },
          failed,
        )
      },
      detach: release,
      send: (active) => optionsRef.current.send({ type: 'mic', active }),
      onChange: setMode,
    })
    sessionRef.current = session
    return () => {
      sessionRef.current = null
      // Track off the stream and stopped, the server told, the detector
      // switched off.
      session.shutdown()
    }
  }, [])

  const { wanted, live } = options
  useEffect(() => {
    sessionRef.current?.setWanted(wanted)
  }, [wanted])
  useEffect(() => {
    sessionRef.current?.setLive(live)
  }, [live])

  // The browser detector releases its recorder and microphone in its effect
  // cleanup. React runs every cleanup of a commit before any effect body, so
  // by the time this runs with `localEnabled` false, it has.
  useEffect(() => {
    if (!localEnabled) sessionRef.current?.localReleased()
  }, [localEnabled, stopCount])

  useEffect(() => {
    const onVisibility = () => sessionRef.current?.setTabHidden(document.visibilityState === 'hidden')
    onVisibility()
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  return useMemo<StreamMic>(
    () => ({
      mode,
      localEnabled,
      isStream: () => sessionRef.current?.mode === 'stream',
      handleFrame: (message) => {
        if (message.type === 'ready') {
          // Strictly `true`, and only with the stream it would ride on.
          sessionRef.current?.ready(message.media?.webrtc === true && message.media.mic === true)
        } else if (message.type === 'mic_state') {
          sessionRef.current?.micState(message.listening === true)
        }
      },
      handleSocketClosed: () => sessionRef.current?.socketClosed(),
    }),
    [mode, localEnabled],
  )
}
