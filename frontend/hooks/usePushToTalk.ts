'use client'

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { toast } from 'react-hot-toast'
import {
  MIC_CONSTRAINTS, closeContext, createRecorder, micErrorMessage, micUnavailableReason,
  openAnalyser, readLevel, stopStream,
} from '@/lib/chat/audio'

interface Options {
  /** A finished recording, ready to send. */
  onBlob(blob: Blob): void
  /** Live 0-100 input level, written every frame for the meter to read. */
  levelRef: RefObject<number>
}

/**
 * Tap-to-record. Owns the microphone for exactly as long as a recording
 * lasts, and releases it on every exit — including unmount, which the inline
 * version missed: navigating away mid-recording left the browser's mic
 * indicator lit until the tab closed.
 */
export function usePushToTalk({ onBlob, levelRef }: Options) {
  const [isRecording, setIsRecording] = useState(false)

  const onBlobRef = useRef(onBlob)
  useLayoutEffect(() => {
    onBlobRef.current = onBlob
  })

  // Everything held open by a recording, so teardown is one place.
  const session = useRef<{
    stream: MediaStream
    ctx: AudioContext
    recorder: MediaRecorder
    raf: number
    discard: boolean
  } | null>(null)
  // Bumped on every start and on unmount. A getUserMedia that resolves for a
  // superseded generation releases its stream instead of orphaning it (a
  // double-click on the mic used to leave one stream running with no owner).
  const generation = useRef(0)

  const teardown = useCallback(() => {
    const s = session.current
    session.current = null
    if (!s) return
    cancelAnimationFrame(s.raf)
    closeContext(s.ctx)
    stopStream(s.stream)
    levelRef.current = 0
  }, [levelRef])

  const start = useCallback(async () => {
    const reason = micUnavailableReason()
    if (reason) {
      toast.error(reason)
      return
    }
    if (session.current) return
    const gen = ++generation.current

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS)
    } catch (err) {
      if (gen === generation.current) toast.error(micErrorMessage(err))
      return
    }
    if (gen !== generation.current) {
      stopStream(stream)
      return
    }

    const { ctx, analyser } = openAnalyser(stream)
    const buffer = new Uint8Array(analyser.frequencyBinCount)
    const recorder = createRecorder(stream)
    const chunks: Blob[] = []
    const current = { stream, ctx, recorder, raf: 0, discard: false }

    const meter = () => {
      levelRef.current = readLevel(analyser, buffer)
      current.raf = requestAnimationFrame(meter)
    }
    current.raf = requestAnimationFrame(meter)

    recorder.ondataavailable = (e) => chunks.push(e.data)
    recorder.onstop = () => {
      const discard = current.discard
      if (session.current === current) teardown()
      else {
        cancelAnimationFrame(current.raf)
        closeContext(ctx)
        stopStream(stream)
      }
      if (!discard) onBlobRef.current(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }))
    }

    session.current = current
    recorder.start()
    setIsRecording(true)
  }, [levelRef, teardown])

  const stop = useCallback(() => {
    const s = session.current
    if (s && s.recorder.state === 'recording') s.recorder.stop()
    setIsRecording(false)
  }, [])

  useEffect(() => {
    return () => {
      generation.current += 1
      const s = session.current
      if (s) {
        // Leaving, not finishing a thought: throw the audio away.
        s.discard = true
        if (s.recorder.state === 'recording') s.recorder.stop()
        teardown()
      }
    }
  }, [teardown])

  return { isRecording, start, stop }
}
