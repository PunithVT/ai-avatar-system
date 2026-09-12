/**
 * Voice activity detection for hands-free mode.
 *
 * Deliberately free of browser APIs: it consumes a stream of (level,
 * timestamp) samples and emits turn boundaries. That keeps the decision logic
 * — which is all timing and thresholds, and easy to get subtly wrong —
 * separable from microphone plumbing and verifiable against a synthetic
 * trace.
 *
 * Levels are on the same 0-100 scale the recording meter already produces
 * (mean of `getByteFrequencyData`, doubled and clamped).
 *
 * The threshold adapts rather than being fixed. A constant that works at a
 * desk fails in a cafe and fires continuously next to a fan, so the detector
 * tracks the ambient floor while nobody is talking and listens for speech a
 * margin above it.
 */

export type VadEvent =
  /** Speech began — start capturing. */
  | 'speech-start'
  /** Speech ended normally — stop capturing and send. */
  | 'speech-end'
  /**
   * What looked like speech was too short to be real (a cough, a door, a
   * keyboard). Stop capturing and throw the audio away rather than shipping
   * a fragment to STT and billing an LLM turn for it.
   */
  | 'speech-abort'

export interface VadOptions {
  /** Continuous silence that ends a turn. Long enough to survive the pause mid-sentence. */
  silenceHangoverMs?: number
  /** Minimum speech before a turn counts as real. Filters out impulse noise. */
  minSpeechMs?: number
  /** How far above the ambient floor counts as speech. */
  thresholdMargin?: number
  /** Absolute floor, so a silent room doesn't trigger on electrical noise. */
  minThreshold?: number
  /** Ambient sampling period before listening starts. */
  calibrationMs?: number
  /** Hard cap on one turn, so a stuck-open mic can't record forever. */
  maxUtteranceMs?: number
}

const DEFAULTS: Required<VadOptions> = {
  // 900ms: long enough to ride out "I think... maybe we should", short enough
  // that the reply doesn't feel laggy.
  silenceHangoverMs: 900,
  minSpeechMs: 300,
  thresholdMargin: 12,
  minThreshold: 8,
  calibrationMs: 700,
  maxUtteranceMs: 30_000,
}

type State = 'calibrating' | 'idle' | 'speech'

export class VoiceActivityDetector {
  private readonly opts: Required<VadOptions>
  private state: State = 'calibrating'
  private noiseFloor = 0
  private startedAt: number | null = null
  private speechStartedAt = 0
  private lastLoudAt = 0
  /**
   * Set when a turn was cut short by the length cap. Blocks the next turn
   * until the input actually goes quiet: without it, audio that is loud
   * continuously — a mic stuck open, a speaker left next to the laptop —
   * ends a turn on the cap and instantly starts another, emitting a turn
   * every maxUtteranceMs forever.
   */
  private awaitingSilence = false

  constructor(options: VadOptions = {}) {
    this.opts = { ...DEFAULTS, ...options }
  }

  /** Level above which audio counts as speech, given the ambient floor. */
  get threshold(): number {
    return Math.max(this.opts.minThreshold, this.noiseFloor + this.opts.thresholdMargin)
  }

  get isCalibrating(): boolean {
    return this.state === 'calibrating'
  }

  get isSpeaking(): boolean {
    return this.state === 'speech'
  }

  /** Forget any in-progress turn and re-measure the ambient floor. */
  reset(): void {
    this.state = 'calibrating'
    this.noiseFloor = 0
    this.startedAt = null
    this.speechStartedAt = 0
    this.lastLoudAt = 0
    this.awaitingSilence = false
  }

  /**
   * Feed one sample. Returns a turn boundary, or null if nothing changed.
   *
   * `now` is passed in rather than read from the clock so the caller can
   * drive it from rAF timestamps and tests can drive it deterministically.
   */
  push(level: number, now: number): VadEvent | null {
    if (this.startedAt === null) this.startedAt = now

    if (this.state === 'calibrating') {
      // Track the loudest ambient sample seen: sizing the floor to the peak
      // rather than the mean stops an intermittent noise (a fan cycling, a
      // passing car) from sitting above a mean-derived threshold and holding
      // the detector permanently "in speech".
      this.noiseFloor = Math.max(this.noiseFloor, level)
      if (now - this.startedAt >= this.opts.calibrationMs) this.state = 'idle'
      return null
    }

    const loud = level >= this.threshold

    if (this.state === 'idle') {
      if (this.awaitingSilence) {
        // Hold until the input drops, then allow turns again.
        if (!loud) this.awaitingSilence = false
        return null
      }
      if (loud) {
        this.state = 'speech'
        this.speechStartedAt = now
        this.lastLoudAt = now
        return 'speech-start'
      }
      // Let the floor drift with the room while nobody is talking. Slow EMA:
      // fast enough to follow an air conditioner starting up, slow enough
      // that it can't creep up over a quiet speaker and swallow them.
      this.noiseFloor = this.noiseFloor * 0.95 + level * 0.05
      return null
    }

    // state === 'speech'
    if (loud) {
      this.lastLoudAt = now
      if (now - this.speechStartedAt >= this.opts.maxUtteranceMs) {
        this.state = 'idle'
        this.awaitingSilence = true
        return 'speech-end'
      }
      return null
    }

    if (now - this.lastLoudAt < this.opts.silenceHangoverMs) return null

    // Silence has outlasted the hangover: the turn is over. Measure speech to
    // the last loud sample, not to now — the trailing silence is not speech.
    const spoken = this.lastLoudAt - this.speechStartedAt
    this.state = 'idle'
    return spoken >= this.opts.minSpeechMs ? 'speech-end' : 'speech-abort'
  }
}
