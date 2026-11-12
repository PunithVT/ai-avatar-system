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
  /**
   * Something loud began — start capturing so the opening syllable is not
   * lost, but do NOT act on it yet: it may still turn out to be a cough.
   */
  | 'speech-start'
  /**
   * The sound has now lasted `minSpeechMs`, so it is real speech. This is the
   * moment to barge in on the avatar. Acting on `speech-start` instead meant a
   * door slam or a key clack cancelled the reply, and the detector then
   * classified the noise as `speech-abort`, leaving nothing in its place.
   * Emitted at most once per turn, always before `speech-end`.
   */
  | 'speech-confirmed'
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
  /**
   * Extra margin while the avatar's own audio is playing. Browser echo
   * cancellation is good but not perfect; what leaks through sits just above
   * the ambient floor, and without this it reads as the user talking.
   */
  playbackMargin?: number
  /**
   * Longest gap between samples the detector will still reason from. Past
   * it, the samples are too far apart to tell speech from silence — see
   * `push`.
   */
  maxSampleGapMs?: number
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
  playbackMargin: 14,
  // Normal sampling is every ~30 ms. A background tab's timers are throttled
  // to about one tick a second, which is longer than the silence hangover.
  maxSampleGapMs: 400,
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
  /** Whether `speech-confirmed` has been emitted for the current turn. */
  private confirmed = false
  /** True while the avatar is audible — see `playbackMargin`. */
  private outputActive = false
  private lastPushAt: number | null = null

  constructor(options: VadOptions = {}) {
    this.opts = { ...DEFAULTS, ...options }
  }

  /** Level above which audio counts as speech, given the ambient floor. */
  get threshold(): number {
    const boost = this.outputActive ? this.opts.playbackMargin : 0
    return Math.max(this.opts.minThreshold, this.noiseFloor + this.opts.thresholdMargin + boost)
  }

  /**
   * Tell the detector whether the avatar is currently audible. While it is,
   * the bar for "speech" is raised by `playbackMargin` and the ambient floor
   * stops adapting, so residual echo neither triggers a turn nor drags the
   * floor up behind it.
   */
  setOutputActive(active: boolean): void {
    this.outputActive = active
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
    this.confirmed = false
    this.lastPushAt = null
  }

  /**
   * Abandon an in-progress turn without re-calibrating. For when the caller
   * knows the capture is unusable (the tab was suspended mid-utterance) and
   * has already discarded the audio.
   */
  cancelTurn(): void {
    if (this.state === 'speech') this.state = 'idle'
    this.confirmed = false
  }

  /**
   * Feed one sample. Returns a turn boundary, or null if nothing changed.
   *
   * `now` is passed in rather than read from the clock so the caller can
   * drive it from rAF timestamps and tests can drive it deterministically.
   */
  push(level: number, now: number): VadEvent | null {
    if (this.startedAt === null) this.startedAt = now

    // Samples that arrive far apart (a throttled background tab delivers
    // about one a second) cannot support a decision. The hangover is 900 ms:
    // with one sample per second, a single reading that happened to land in
    // the dip between two words looked like 900 ms of silence and ended the
    // turn mid-sentence, sending fragments as separate turns. So across a
    // coarse gap the detector neither starts a turn nor ends a confirmed one
    // on silence; it holds what it has until samples are dense again. The
    // length cap still applies, so a held turn cannot run on indefinitely.
    const coarse = this.lastPushAt !== null && now - this.lastPushAt > this.opts.maxSampleGapMs
    this.lastPushAt = now
    if (coarse && this.state !== 'calibrating') {
      if (this.state === 'speech') {
        if (!this.confirmed) {
          // A turn that had not yet been confirmed as speech has nothing
          // behind it but one loud moment. Holding it would eventually send
          // whatever the recorder picked up in the meantime.
          this.state = 'idle'
          return 'speech-abort'
        }
        if (now - this.speechStartedAt >= this.opts.maxUtteranceMs) {
          this.state = 'idle'
          this.awaitingSilence = true
          return 'speech-end'
        }
        // Unobserved time is not silence: restart the hangover from here.
        this.lastLoudAt = now
      }
      return null
    }

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
        this.confirmed = false
        return 'speech-start'
      }
      // Let the floor drift with the room while nobody is talking. Slow EMA:
      // fast enough to follow an air conditioner starting up, slow enough
      // that it can't creep up over a quiet speaker and swallow them.
      if (!this.outputActive) this.noiseFloor = this.noiseFloor * 0.95 + level * 0.05
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
      if (!this.confirmed && now - this.speechStartedAt >= this.opts.minSpeechMs) {
        this.confirmed = true
        return 'speech-confirmed'
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
