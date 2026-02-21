'use client'

import { useEffect, useRef } from 'react'

interface WaveformVisualizerProps {
  /** Pass an active MediaStream to render live microphone input */
  stream?: MediaStream | null
  /** If no stream, renders a static decorative waveform */
  staticBars?: number
  /** Bar color – supports Tailwind gradient strings via CSS vars or hex */
  color?: string
  height?: number
  className?: string
  active?: boolean
}

export function WaveformVisualizer({
  stream,
  staticBars = 40,
  height = 48,
  className = '',
  active = false,
}: WaveformVisualizerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const animRef = useRef<number | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    if (stream) {
      // Live mode
      const audioCtx = new AudioContext()
      const analyser = audioCtx.createAnalyser()
      analyser.fftSize = 128
      analyser.smoothingTimeConstant = 0.8
      audioCtx.createMediaStreamSource(stream).connect(analyser)
      analyserRef.current = analyser
      audioCtxRef.current = audioCtx

      const draw = () => {
        const W = canvas.width
        const H = canvas.height
        const data = new Uint8Array(analyser.frequencyBinCount)
        analyser.getByteFrequencyData(data)

        ctx.clearRect(0, 0, W, H)

        const barCount = Math.min(40, data.length)
        const gap = 3
        const barW = (W - gap * (barCount - 1)) / barCount

        for (let i = 0; i < barCount; i++) {
          const val = data[i] / 255
          const barH = Math.max(3, val * H)
          const x = i * (barW + gap)
          const y = (H - barH) / 2

          const grad = ctx.createLinearGradient(0, y, 0, y + barH)
          grad.addColorStop(0, 'rgba(168,85,247,0.9)')
          grad.addColorStop(1, 'rgba(59,130,246,0.7)')
          ctx.fillStyle = grad
          ctx.beginPath()
          ctx.roundRect(x, y, barW, barH, 2)
          ctx.fill()
        }

        animRef.current = requestAnimationFrame(draw)
      }
      draw()
    } else {
      // Static / idle decorative mode
      const W = canvas.width
      const H = canvas.height
      const bars = staticBars
      const gap = 3
      const barW = (W - gap * (bars - 1)) / bars

      let frame = 0
      const drawStatic = () => {
        ctx.clearRect(0, 0, W, H)

        for (let i = 0; i < bars; i++) {
          const phase = (frame / 60 + i / bars) * Math.PI * 2
          const amp = active
            ? (Math.sin(phase) * 0.4 + 0.5) * (Math.sin(phase * 1.7) * 0.2 + 0.8)
            : 0.08

          const barH = Math.max(3, amp * H)
          const x = i * (barW + gap)
          const y = (H - barH) / 2

          const alpha = active ? 0.7 + amp * 0.3 : 0.2
          const grad = ctx.createLinearGradient(0, y, 0, y + barH)
          grad.addColorStop(0, `rgba(168,85,247,${alpha})`)
          grad.addColorStop(1, `rgba(59,130,246,${alpha * 0.7})`)
          ctx.fillStyle = grad
          ctx.beginPath()
          ctx.roundRect(x, y, barW, barH, 2)
          ctx.fill()
        }

        frame++
        animRef.current = requestAnimationFrame(drawStatic)
      }
      drawStatic()
    }

    return () => {
      if (animRef.current) cancelAnimationFrame(animRef.current)
      audioCtxRef.current?.close()
    }
  }, [stream, active, staticBars])

  return (
    <canvas
      ref={canvasRef}
      width={320}
      height={height}
      className={`w-full ${className}`}
      style={{ height }}
    />
  )
}

/** Compact inline waveform used in chat UI (CSS-only, no canvas) */
export function InlineWaveform({ active }: { active: boolean }) {
  return (
    <div className="flex items-center gap-0.5" style={{ height: 24 }}>
      {Array.from({ length: 8 }, (_, i) => (
        <div
          key={i}
          className="w-1 rounded-full"
          style={{
            background: 'linear-gradient(to top, #7c3aed, #3b82f6)',
            height: active ? undefined : '4px',
            minHeight: '4px',
            animation: active ? `waveform 1.2s ease-in-out ${i * 0.1}s infinite` : 'none',
          }}
        />
      ))}
    </div>
  )
}
