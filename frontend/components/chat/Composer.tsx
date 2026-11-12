'use client'

import { useEffect, useRef, type RefObject } from 'react'
import { Mic, MicOff, Radio, Send, Square } from 'lucide-react'

// Static, varied bar heights; the CSS `waveform` animation supplies motion.
const WAVE_HEIGHTS = [10, 18, 8, 22, 14, 20, 6, 16]

function WaveformBars() {
  return (
    <div className="flex h-6 items-center gap-0.5" aria-hidden="true">
      {WAVE_HEIGHTS.map((height, i) => (
        <div
          key={i}
          className="w-1 animate-waveform rounded-full bg-primary-600"
          style={{ height: `${height}px`, animationDelay: `${i * 0.1}s` }}
        />
      ))}
    </div>
  )
}

/**
 * Input level meter. Reads the level from a ref and writes the bar's width
 * directly, once per frame — the level changes 60 times a second, and routing
 * that through React state re-rendered the entire chat on every frame.
 */
function MicLevelBar({ levelRef }: { levelRef: RefObject<number> }) {
  const fillRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    let raf = 0
    const paint = () => {
      if (fillRef.current) fillRef.current.style.width = `${levelRef.current}%`
      raf = requestAnimationFrame(paint)
    }
    raf = requestAnimationFrame(paint)
    return () => cancelAnimationFrame(raf)
  }, [levelRef])
  return (
    <div className="h-1 overflow-hidden rounded-full bg-gray-100" aria-hidden="true">
      <div ref={fillRef} className="voice-level h-full" style={{ width: 0 }} />
    </div>
  )
}

interface Props {
  value: string
  onChange(value: string): void
  onSend(): void
  /** Stop the reply in flight (generation and playback). */
  onStop(): void
  /** A reply is being generated or spoken — the send button becomes Stop. */
  isBusy: boolean
  isProcessing: boolean
  isRecording: boolean
  onToggleRecording(): void
  handsFree: boolean
  onToggleHandsFree(): void
  /**
   * Hands-free is running on the live stream — the server hears the
   * microphone and decides the turns — rather than on the browser detector.
   */
  handsFreeLive?: boolean
  levelRef: RefObject<number>
}

const ROUND_BUTTON =
  'relative flex h-10 w-10 shrink-0 items-center justify-center rounded-md transition-colors duration-150'
const ROUND_IDLE = 'border border-gray-300 bg-white text-gray-600 shadow-xs hover:bg-gray-50 hover:text-gray-900'

export function Composer({
  value, onChange, onSend, onStop, isBusy, isProcessing, isRecording,
  onToggleRecording, handsFree, onToggleHandsFree, handsFreeLive = false, levelRef,
}: Props) {
  return (
    <div className="border-t border-gray-200 px-4 py-3">
      {isRecording && (
        <div className="mb-3 space-y-2 px-1">
          <MicLevelBar levelRef={levelRef} />
          <div className="flex items-center gap-2">
            <span className="font-mono text-eyebrow uppercase text-error-600">Rec</span>
            <WaveformBars />
            <span className="ml-auto text-xs text-gray-500">
              {handsFree ? 'Listening — pause when you’re done' : 'Tap stop when done'}
            </span>
          </div>
        </div>
      )}
      {handsFree && !isRecording && (
        <div className="mb-3 flex items-center gap-2 px-1">
          <span className="font-mono text-eyebrow uppercase text-primary-600">
            {handsFreeLive ? 'Hands-free (live)' : 'Hands-free'}
          </span>
          <span className="ml-auto text-xs text-gray-500">
            {isBusy ? 'Speak to interrupt' : 'Just start talking'}
          </span>
        </div>
      )}

      <div className="flex items-end gap-2">
        <button
          onClick={onToggleRecording}
          disabled={isProcessing || handsFree}
          aria-label={isRecording ? 'Stop recording' : 'Start voice recording'}
          aria-pressed={isRecording}
          title={handsFree ? 'Hands-free is listening — turns are detected automatically' : undefined}
          className={`${ROUND_BUTTON} disabled:cursor-not-allowed disabled:opacity-40
 ${isRecording ? 'bg-error-600 text-white hover:bg-error-700' : ROUND_IDLE}`}
        >
          {isRecording ? <MicOff size={18} /> : <Mic size={18} />}
        </button>

        <button
          onClick={onToggleHandsFree}
          aria-label={
            !handsFree
              ? 'Turn on hands-free listening'
              : handsFreeLive
                ? 'Turn off hands-free listening (live)'
                : 'Turn off hands-free listening'
          }
          aria-pressed={handsFree}
          title={
            !handsFree
              ? 'Hands-free: listen continuously'
              : handsFreeLive
                ? 'Hands-free (live) on — just talk'
                : 'Hands-free on — just talk'
          }
          className={`${ROUND_BUTTON} ${handsFree ? 'bg-primary-600 text-white hover:bg-primary-700' : ROUND_IDLE}`}
        >
          <Radio size={18} />
          {handsFree && !isRecording && (
            <span className="absolute -right-1 -top-1 h-2.5 w-2.5 animate-pulse-soft rounded-full border-2 border-white bg-success-500" />
          )}
        </button>

        <div className="relative flex-1">
          <label htmlFor="chat-input" className="sr-only">Message your avatar</label>
          <textarea
            id="chat-input"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                onSend()
              }
            }}
            placeholder={isRecording ? 'Recording…' : 'Message your avatar… (Enter to send)'}
            disabled={isProcessing || isRecording}
            rows={1}
            className="input-field max-h-32 resize-none overflow-y-auto [field-sizing:content]"
          />
        </div>

        {isBusy ? (
          <button
            onClick={onStop}
            aria-label="Stop generating"
            title="Stop"
            className={`${ROUND_BUTTON} bg-error-600 text-white hover:bg-error-700`}
          >
            <Square size={16} fill="currentColor" />
          </button>
        ) : (
          <button
            onClick={onSend}
            disabled={!value.trim() || isRecording}
            aria-label="Send message"
            className={`${ROUND_BUTTON} bg-primary-600 text-white shadow-xs hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-40`}
          >
            <Send size={18} />
          </button>
        )}
      </div>

      <p className="mt-2 text-center text-xs text-gray-500">
        Shift+Enter for new line · Mic for voice ·{' '}
        <kbd className="rounded-xs bg-gray-100 px-1 py-0.5 text-gray-600">?</kbd> for shortcuts
      </p>
    </div>
  )
}
