/**
 * Microphone plumbing shared by push-to-talk, hands-free and the voice
 * recorder, so the three cannot drift on constraints or container handling.
 */

// Echo cancellation is what makes hands-free viable at all: without it the
// open mic hears the avatar's own reply through the speakers, the detector
// treats it as speech, and the session talks to itself in a loop. Useful for
// push-to-talk too, where it keeps a barge-in from capturing the reply.
export const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
}

/**
 * Why the microphone cannot be used here, or null if it can.
 *
 * `navigator.mediaDevices` is simply undefined on an insecure origin — any
 * http:// page that is not localhost — so the old blanket "Failed to access
 * microphone" told a user on a plain-HTTP deployment nothing about the cause.
 */
export function micUnavailableReason(): string | null {
  if (typeof navigator === 'undefined') return 'Microphone is not available.'
  if (!navigator.mediaDevices?.getUserMedia) {
    const insecure = typeof window !== 'undefined' && !window.isSecureContext
    return insecure
      ? 'Voice input needs a secure connection — open this site over HTTPS.'
      : 'This browser does not support microphone capture.'
  }
  if (typeof MediaRecorder === 'undefined') {
    return 'This browser does not support audio recording.'
  }
  return null
}

/** Human message for a getUserMedia rejection. */
export function micErrorMessage(err: unknown): string {
  const name = (err as { name?: string } | null)?.name
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return 'Microphone permission was denied — allow it in your browser to use voice.'
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') {
    return 'No microphone was found.'
  }
  if (name === 'NotReadableError') {
    return 'The microphone is in use by another application.'
  }
  return 'Could not access the microphone.'
}

const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']

/**
 * Create a recorder in a container this browser can actually produce.
 * Safari records MP4, not WebM; the blob must be labelled with what the
 * recorder really wrote (`recorder.mimeType`), not with an assumption.
 */
export function createRecorder(stream: MediaStream): MediaRecorder {
  const supported = MIME_CANDIDATES.find((t) => MediaRecorder.isTypeSupported?.(t))
  return supported ? new MediaRecorder(stream, { mimeType: supported }) : new MediaRecorder(stream)
}

/** 0-100 input level: mean of the frequency bins, doubled and clamped. */
export function readLevel(analyser: AnalyserNode, buffer: Uint8Array<ArrayBuffer>): number {
  analyser.getByteFrequencyData(buffer)
  let sum = 0
  for (let i = 0; i < buffer.length; i++) sum += buffer[i]
  return Math.min(100, (sum / buffer.length) * 2)
}

/** Open an analyser on a stream. The context is resumed explicitly because
 *  one created outside a user gesture (hands-free restored on load) can start
 *  suspended, which leaves the meter — and the VAD — reading zeros. */
export function openAnalyser(
  stream: MediaStream,
  fftSize = 256,
  smoothing?: number,
): { ctx: AudioContext; analyser: AnalyserNode } {
  const ctx = new AudioContext()
  const analyser = ctx.createAnalyser()
  analyser.fftSize = fftSize
  if (smoothing !== undefined) analyser.smoothingTimeConstant = smoothing
  ctx.createMediaStreamSource(stream).connect(analyser)
  if (ctx.state === 'suspended') ctx.resume().catch(() => {})
  return { ctx, analyser }
}

export function closeContext(ctx: AudioContext | null): void {
  if (ctx && ctx.state !== 'closed') ctx.close().catch(() => {})
}

export function stopStream(stream: MediaStream | null): void {
  stream?.getTracks().forEach((t) => t.stop())
}


/**
 * Call `fn` every `ms`, from a timer that keeps its pace in a background tab.
 * Returns a function that stops it.
 *
 * Browsers clamp a hidden page's own timers to roughly one tick per second.
 * A dedicated worker's timers are not clamped that way, so the worker keeps
 * time and posts a message per tick. If a worker cannot be created (blocked
 * by a content-security policy, or not supported) this falls back to a plain
 * interval, which is correct but coarse while the tab is hidden.
 */
export function createTicker(ms: number, fn: () => void): () => void {
  let worker: Worker | null = null
  let url: string | null = null
  try {
    if (typeof Worker !== 'undefined' && typeof Blob !== 'undefined' && typeof URL !== 'undefined') {
      url = URL.createObjectURL(
        new Blob([`setInterval(function(){postMessage(0)},${Math.max(1, Math.floor(ms))})`], {
          type: 'text/javascript',
        }),
      )
      worker = new Worker(url)
    }
  } catch {
    worker = null
  }

  if (worker) {
    const w = worker
    let fallback: ReturnType<typeof setInterval> | null = null
    w.onmessage = () => fn()
    // A worker that fails to load (CSP) reports it asynchronously.
    w.onerror = () => {
      if (fallback === null) fallback = setInterval(fn, ms)
    }
    return () => {
      w.onmessage = null
      w.onerror = null
      w.terminate()
      if (url) URL.revokeObjectURL(url)
      if (fallback !== null) clearInterval(fallback)
    }
  }

  if (url) URL.revokeObjectURL(url)
  const interval = setInterval(fn, ms)
  return () => clearInterval(interval)
}
