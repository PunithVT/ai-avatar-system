import { describe, expect, it } from 'vitest'
import { VoiceActivityDetector, type VadEvent, type VadOptions } from '@/lib/vad'

const STEP = 20 // ms between samples — roughly the hook's 30 ms tick

/** Drive a detector with a level trace and collect the events it emits. */
class Trace {
  readonly vad: VoiceActivityDetector
  readonly events: { event: VadEvent; at: number }[] = []
  now = 0

  constructor(options?: VadOptions) {
    this.vad = new VoiceActivityDetector(options)
  }

  /** Feed `level` for `ms` milliseconds. */
  feed(level: number, ms: number): this {
    const end = this.now + ms
    while (this.now < end) {
      const event = this.vad.push(level, this.now)
      if (event) this.events.push({ event, at: this.now })
      this.now += STEP
    }
    return this
  }

  /** Quiet room for the calibration window. */
  calibrate(ambient = 2): this {
    return this.feed(ambient, 800)
  }

  get names(): VadEvent[] {
    return this.events.map((e) => e.event)
  }
}

const QUIET = 2
const SPEECH = 40

describe('VoiceActivityDetector', () => {
  it('emits nothing while calibrating', () => {
    const t = new Trace().feed(SPEECH, 600)
    expect(t.names).toEqual([])
    expect(t.vad.isCalibrating).toBe(true)
  })

  it('detects a normal utterance: start, confirmed, end', () => {
    const t = new Trace().calibrate().feed(SPEECH, 1000).feed(QUIET, 1200)
    expect(t.names).toEqual(['speech-start', 'speech-confirmed', 'speech-end'])
  })

  it('confirms speech only after minSpeechMs, not on the first loud frame', () => {
    const t = new Trace().calibrate()
    const startedAt = t.now
    t.feed(SPEECH, 1000)
    const start = t.events.find((e) => e.event === 'speech-start')!
    const confirmed = t.events.find((e) => e.event === 'speech-confirmed')!
    expect(start.at).toBe(startedAt)
    expect(confirmed.at - start.at).toBeGreaterThanOrEqual(300)
    expect(confirmed.at - start.at).toBeLessThan(300 + 2 * STEP)
  })

  it('emits speech-confirmed at most once per turn', () => {
    const t = new Trace().calibrate().feed(SPEECH, 4000).feed(QUIET, 1200)
    expect(t.names.filter((n) => n === 'speech-confirmed')).toHaveLength(1)
  })

  it('aborts a sound shorter than 300 ms, and never confirms it', () => {
    // A cough: this is the case barge-in must NOT fire on.
    const t = new Trace().calibrate().feed(SPEECH, 150).feed(QUIET, 1200)
    expect(t.names).toEqual(['speech-start', 'speech-abort'])
  })

  it('rides out a mid-sentence pause shorter than the hangover', () => {
    const t = new Trace().calibrate()
      .feed(SPEECH, 800)
      .feed(QUIET, 600) // "I think…" — under the 900 ms hangover
      .feed(SPEECH, 800)
      .feed(QUIET, 1200)
    expect(t.names).toEqual(['speech-start', 'speech-confirmed', 'speech-end'])
  })

  it('splits two utterances separated by more than the hangover', () => {
    const t = new Trace().calibrate()
      .feed(SPEECH, 800).feed(QUIET, 1200)
      .feed(SPEECH, 800).feed(QUIET, 1200)
    expect(t.names).toEqual([
      'speech-start', 'speech-confirmed', 'speech-end',
      'speech-start', 'speech-confirmed', 'speech-end',
    ])
  })

  it('caps a turn at maxUtteranceMs and waits for silence before the next', () => {
    const t = new Trace({ maxUtteranceMs: 2000 }).calibrate()
    // Continuously loud for far longer than the cap: a mic stuck open.
    t.feed(SPEECH, 7000)
    expect(t.names).toEqual(['speech-start', 'speech-confirmed', 'speech-end'])
    // Only once the input actually drops can a new turn begin.
    t.feed(QUIET, 200).feed(SPEECH, 600)
    expect(t.names.slice(3)).toEqual(['speech-start', 'speech-confirmed'])
  })

  it('adapts the threshold to a noisy room', () => {
    const quietRoom = new Trace().calibrate(2)
    const noisyRoom = new Trace().calibrate(30)
    expect(noisyRoom.vad.threshold).toBeGreaterThan(quietRoom.vad.threshold)
    // A level that is speech in the quiet room is just the fan in the noisy one.
    quietRoom.feed(36, 600)
    noisyRoom.feed(36, 600)
    expect(quietRoom.names).toContain('speech-start')
    expect(noisyRoom.names).toEqual([])
  })

  it('sizes the floor to the loudest ambient sample, not the mean', () => {
    // An intermittent noise during calibration must end up BELOW the
    // threshold, or it pins the detector in "speech" forever.
    const t = new Trace()
    t.feed(2, 300).feed(25, 60).feed(2, 440)
    expect(t.vad.threshold).toBeGreaterThan(25)
    t.feed(25, 600)
    expect(t.names).toEqual([])
  })

  it('lets the floor follow the room while idle', () => {
    const t = new Trace().calibrate(2)
    const before = t.vad.threshold
    t.feed(10, 3000) // air conditioning starts; still under the threshold
    expect(t.vad.threshold).toBeGreaterThan(before)
    expect(t.names).toEqual([])
  })

  describe('while the avatar is audible', () => {
    it('raises the threshold by playbackMargin', () => {
      const t = new Trace().calibrate()
      const base = t.vad.threshold
      t.vad.setOutputActive(true)
      expect(t.vad.threshold).toBe(base + 14)
      t.vad.setOutputActive(false)
      expect(t.vad.threshold).toBe(base)
    })

    it('ignores echo that would otherwise read as speech', () => {
      const echo = 20 // above the normal threshold (2 + 12), below the raised one
      const idle = new Trace().calibrate().feed(echo, 1000)
      expect(idle.names).toContain('speech-start')

      const playing = new Trace().calibrate()
      playing.vad.setOutputActive(true)
      playing.feed(echo, 1000)
      expect(playing.names).toEqual([])
    })

    it('still detects a real voice over the avatar', () => {
      const t = new Trace().calibrate()
      t.vad.setOutputActive(true)
      t.feed(SPEECH, 600)
      expect(t.names).toEqual(['speech-start', 'speech-confirmed'])
    })

    it('does not let echo drag the ambient floor up', () => {
      const t = new Trace().calibrate()
      const base = t.vad.threshold
      t.vad.setOutputActive(true)
      t.feed(20, 5000)
      t.vad.setOutputActive(false)
      expect(t.vad.threshold).toBe(base)
    })
  })

  it('cancelTurn abandons the turn without re-calibrating', () => {
    const t = new Trace().calibrate().feed(SPEECH, 500)
    expect(t.vad.isSpeaking).toBe(true)
    t.vad.cancelTurn()
    expect(t.vad.isSpeaking).toBe(false)
    expect(t.vad.isCalibrating).toBe(false)
    // The abandoned turn produces no end/abort later…
    t.feed(QUIET, 1500)
    expect(t.names).toEqual(['speech-start', 'speech-confirmed'])
    // …and the next utterance is detected normally.
    t.feed(SPEECH, 600)
    expect(t.names.slice(2)).toEqual(['speech-start', 'speech-confirmed'])
  })

  it('reset re-measures the ambient floor', () => {
    const t = new Trace().calibrate(2).feed(SPEECH, 500)
    t.vad.reset()
    expect(t.vad.isCalibrating).toBe(true)
    expect(t.vad.isSpeaking).toBe(false)
  })

  describe('with coarse samples (a throttled background tab)', () => {
    /** Feed one sample per `stepMs`, following `levelAt(elapsedMs)`. */
    function feedCoarse(t: Trace, ms: number, stepMs: number, levelAt: (elapsed: number) => number) {
      const start = t.now
      while (t.now < start + ms) {
        const event = t.vad.push(levelAt(t.now - start), t.now)
        if (event) t.events.push({ event, at: t.now })
        t.now += stepMs
      }
    }

    it('does not end a turn because one sparse sample landed in a gap between words', () => {
      // A hidden tab's timers tick about once a second — longer than the
      // 900 ms hangover. One reading in an inter-word dip used to look like
      // 900 ms of silence, ending the turn mid-sentence.
      const t = new Trace().calibrate()
      t.feed(SPEECH, 600) // the user starts talking with the tab in front
      expect(t.names).toEqual(['speech-start', 'speech-confirmed'])

      // Tab goes to the background: continuous speech, sampled once a second,
      // with every other sample happening to fall in a quiet dip.
      feedCoarse(t, 12_000, 1000, (elapsed) => ((elapsed / 1000) % 2 === 0 ? QUIET : SPEECH))
      expect(t.names).toEqual(['speech-start', 'speech-confirmed'])
      expect(t.vad.isSpeaking).toBe(true)

      // Back in front: dense samples again, and real silence ends it normally.
      t.feed(SPEECH, 300).feed(QUIET, 1200)
      expect(t.names).toEqual(['speech-start', 'speech-confirmed', 'speech-end'])
    })

    it('never confirms or sends a turn on sparse samples alone', () => {
      // Loud readings a second apart could be unrelated noises; that must not
      // be enough to interrupt the avatar or ship a recording. The one start
      // that dense sampling had already allowed is abandoned, and nothing
      // further begins while the samples stay sparse.
      const t = new Trace().calibrate()
      t.feed(QUIET, 200)
      feedCoarse(t, 6000, 1000, () => SPEECH)
      expect(t.names).toEqual(['speech-start', 'speech-abort'])
      expect(t.vad.isSpeaking).toBe(false)
    })

    it('still enforces the length cap on a turn held across coarse samples', () => {
      const t = new Trace({ maxUtteranceMs: 5000 }).calibrate()
      t.feed(SPEECH, 600)
      feedCoarse(t, 8000, 1000, () => QUIET)
      expect(t.names).toEqual(['speech-start', 'speech-confirmed', 'speech-end'])
      expect(t.vad.isSpeaking).toBe(false)
    })

    it('a single late tick does not cost the turn its ending', () => {
      // One hiccup (a long GC pause, a busy main thread), then normal ticks.
      const t = new Trace().calibrate()
      t.feed(SPEECH, 600)
      t.now += 700 // one gap just over the limit
      t.feed(QUIET, 1200)
      expect(t.names).toEqual(['speech-start', 'speech-confirmed', 'speech-end'])
    })
  })
})
