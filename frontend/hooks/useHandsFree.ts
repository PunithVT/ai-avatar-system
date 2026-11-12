'use client'

import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { toast } from 'react-hot-toast'
import { VoiceActivityDetector } from '@/lib/vad'
import {
  MIC_CONSTRAINTS, closeContext, createRecorder, createTicker, micErrorMessage,
  micUnavailableReason, openAnalyser, readLevel, stopStream,
} from '@/lib/chat/audio'

interface Options {
  enabled: boolean
  /** A completed utterance, ready to send. */
  onBlob(blob: Blob): void
  /** The user is really speaking over the avatar — cut it off. */
  onBargeIn(): void
  /** Is the avatar audible right now? Read every tick. */
  isOutputActive(): boolean
  /** Hands-free could not start; the caller should switch the mode off. */
  onFailed(): void
  /** Live 0-100 input level for the meter (0 unless a turn is being captured). */
  levelRef: RefObject<number>
}

// How often the detector samples the mic. A timer, not requestAnimationFrame:
// rAF stops entirely in a background tab, which froze the detector
// mid-utterance while the recorder kept running. The timer runs in a worker
// where one is available (see createTicker), because a page's own timers are
// throttled to about one tick a second in a background tab — too coarse to
// detect anything. When ticks do arrive that far apart the detector holds
// rather than guesses (VadOptions.maxSampleGapMs).
const TICK_MS = 30
// A gap this long between ticks means the tab was suspended or heavily
// throttled. Whatever was being captured across it is not a clean utterance.
const STALL_MS = 2_000

/**
 * Open-mic mode: turns are detected from voice activity instead of a button.
 *
 * Holds one microphone stream for the lifetime of the mode rather than
 * acquiring per turn — re-prompting getUserMedia on every utterance would be
 * slow and would re-trigger the browser's permission UI.
 */
export function useHandsFree({ enabled, onBlob, onBargeIn, isOutputActive, onFailed, levelRef }: Options) {
  // True between a detected speech start and its end — drives the REC row.
  const [isCapturing, setIsCapturing] = useState(false)

  const callbacks = useRef({ onBlob, onBargeIn, isOutputActive, onFailed })
  useLayoutEffect(() => {
    callbacks.current = { onBlob, onBargeIn, isOutputActive, onFailed }
  })

  useEffect(() => {
    if (!enabled) return

    const reason = micUnavailableReason()
    if (reason) {
      toast.error(reason)
      callbacks.current.onFailed()
      return
    }

    // getUserMedia can sit on a permission prompt for seconds. If the mode is
    // switched off in that window the resolved stream must be released, not
    // assigned after teardown and left running with nothing to stop it.
    let cancelled = false
    let stream: MediaStream | null = null
    let ctx: AudioContext | null = null
    let stopTicker: (() => void) | null = null
    let recorder: MediaRecorder | null = null
    // Set when a turn is abandoned, so the recorder's onstop discards it
    // instead of shipping a fragment to STT.
    let discard = false

    const endCapture = (throwAway: boolean) => {
      discard = throwAway
      if (recorder && recorder.state === 'recording') recorder.stop()
      recorder = null
      levelRef.current = 0
      setIsCapturing(false)
    }

    navigator.mediaDevices
      .getUserMedia(MIC_CONSTRAINTS)
      .then((granted) => {
        if (cancelled) {
          stopStream(granted)
          return
        }
        stream = granted
        // Light smoothing. The analyser's default (0.8) averages each reading
        // with the previous ones, so the level takes ~270 ms to fall back
        // after a sound stops. That tail made a 120 ms cough read as 400 ms
        // of "speech" — long enough to pass minSpeechMs and interrupt the
        // avatar, which is exactly what the detector exists to prevent.
        const opened = openAnalyser(granted, 256, 0.2)
        ctx = opened.ctx
        const analyser = opened.analyser
        const buffer = new Uint8Array(analyser.frequencyBinCount)
        const vad = new VoiceActivityDetector()
        let lastTick = performance.now()

        const beginCapture = () => {
          const chunks: Blob[] = []
          const rec = createRecorder(granted)
          rec.ondataavailable = (e) => chunks.push(e.data)
          rec.onstop = () => {
            const throwAway = discard
            discard = false
            if (!throwAway) {
              callbacks.current.onBlob(new Blob(chunks, { type: rec.mimeType || 'audio/webm' }))
            }
          }
          rec.start()
          recorder = rec
          setIsCapturing(true)
        }

        stopTicker = createTicker(TICK_MS, () => {
          const now = performance.now()
          const gap = now - lastTick
          lastTick = now

          if (gap > STALL_MS) {
            // The tab was suspended. The detector missed everything in
            // between, so an utterance in progress has no trustworthy end —
            // and the recorder has been running the whole time. Drop it
            // rather than upload minutes of audio the server will reject.
            if (recorder) endCapture(true)
            vad.cancelTurn()
            return
          }

          vad.setOutputActive(callbacks.current.isOutputActive())
          const level = readLevel(analyser, buffer)
          levelRef.current = vad.isSpeaking ? level : 0

          // Each handler runs inside try/catch: one throwing must not stop
          // the interval, or hands-free stays "on" while no longer listening.
          try {
            switch (vad.push(level, now)) {
              case 'speech-start':
                // Start capturing so the first syllable isn't lost — but do
                // not interrupt the avatar yet; this may be a cough.
                beginCapture()
                break
              case 'speech-confirmed':
                callbacks.current.onBargeIn()
                break
              case 'speech-end':
                endCapture(false)
                break
              case 'speech-abort':
                endCapture(true)
                break
            }
          } catch {
            endCapture(true)
            vad.cancelTurn()
          }
        })
      })
      .catch((err) => {
        if (cancelled) return
        toast.error(micErrorMessage(err))
        callbacks.current.onFailed()
      })

    return () => {
      cancelled = true
      stopTicker?.()
      // Switching the mode off mid-utterance means the user is leaving, not
      // finishing a thought — discard rather than send.
      endCapture(true)
      stopStream(stream)
      closeContext(ctx)
    }
  }, [enabled, levelRef])

  return { isCapturing }
}
