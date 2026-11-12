'use client'

import { useState, useRef, useCallback, useEffect, type ChangeEvent } from 'react'
import {
  Mic, MicOff, Play, Pause, Trash2, Upload, Check, Loader2,
  Volume2, Wand2, Music, AlertCircle, PlusCircle, RefreshCw,
} from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import {
  MIC_CONSTRAINTS, closeContext, createRecorder, micErrorMessage, micUnavailableReason,
  openAnalyser, stopStream,
} from '@/lib/chat/audio'
import { errorMessage } from '@/lib/errors'
import type { VoiceApiResponse } from '@/lib/types'

interface VoiceProfile {
  id: string
  name: string
  language: string
  duration: number
  createdAt: Date
  isDefault: boolean
}

const SUPPORTED_LANGUAGES: { code: string; label: string }[] = [
  { code: 'en', label: '🇺🇸 English' },
  { code: 'es', label: '🇪🇸 Spanish' },
  { code: 'fr', label: '🇫🇷 French' },
  { code: 'de', label: '🇩🇪 German' },
  { code: 'zh', label: '🇨🇳 Chinese' },
  { code: 'ja', label: '🇯🇵 Japanese' },
  { code: 'pt', label: '🇧🇷 Portuguese' },
  { code: 'hi', label: '🇮🇳 Hindi' },
  { code: 'it', label: '🇮🇹 Italian' },
  { code: 'ko', label: '🇰🇷 Korean' },
]

/**
 * "No cloned voice": the avatar speaks with the server's built-in voice.
 *
 * This replaces four named presets (Alex, Jordan, Morgan, Sofia) that existed
 * only in this file. The backend had no voices with those ids, so they all
 * sounded identical and assigning one could only fail. One honest entry says
 * what actually happens. Selecting it unassigns the avatar's voice.
 */
export const DEFAULT_VOICE_ID = 'default'

const DEFAULT_VOICE: VoiceProfile = {
  id: DEFAULT_VOICE_ID,
  name: 'Default voice',
  language: 'en',
  duration: 0,
  createdAt: new Date(0),
  isDefault: true,
}

const LANG_FLAGS: Record<string, string> = {
  en: '🇺🇸', es: '🇪🇸', fr: '🇫🇷', de: '🇩🇪', zh: '🇨🇳', ja: '🇯🇵', pt: '🇧🇷', hi: '🇮🇳', it: '🇮🇹', ko: '🇰🇷',
}

const MAX_RECORDING_SECS = 30
const IDLE_WAVE = Array<number>(20).fill(4)

function WaveformBar({ height }: { height: number }) {
  return (
    <div
      className="w-1 rounded-full bg-primary-600 transition-all duration-100"
      style={{ height: `${height}px`, minHeight: '4px', maxHeight: '40px' }}
    />
  )
}

interface VoicePanelProps {
  /** Called with a cloned voice's id, or DEFAULT_VOICE_ID to clear the voice. */
  onVoiceSelect?: (voiceId: string) => void
  /** The voice currently assigned to the selected avatar, if any. */
  assignedVoiceId?: string | null
}

export function VoicePanel({ onVoiceSelect, assignedVoiceId }: VoicePanelProps = {}) {
  const [voices, setVoices] = useState<VoiceProfile[]>([DEFAULT_VOICE])
  // Highlight follows the avatar's real assignment rather than defaulting to
  // the first row, which showed a selection that matched nothing.
  const [pickedVoice, setPickedVoice] = useState<string | null>(null)
  const selectedVoice = pickedVoice ?? assignedVoiceId ?? DEFAULT_VOICE_ID

  const [isRecording, setIsRecording] = useState(false)
  const [recordingTime, setRecordingTime] = useState(0)
  const [audioBlob, setAudioBlob] = useState<Blob | null>(null)
  const [audioUrl, setAudioUrl] = useState<string | null>(null)
  const [isPlaying, setIsPlaying] = useState(false)
  const [isCloning, setIsCloning] = useState(false)
  const [newVoiceName, setNewVoiceName] = useState('')
  const [newVoiceLang, setNewVoiceLang] = useState('en')
  const [waveHeights, setWaveHeights] = useState<number[]>(IDLE_WAVE)
  const [step, setStep] = useState<'select' | 'record' | 'name'>('select')
  const [recordMode, setRecordMode] = useState<'mic' | 'file'>('mic')

  // Voice library preview state — playback of an existing custom voice's WAV
  const [previewingId, setPreviewingId] = useState<string | null>(null)
  const previewAudioRef = useRef<HTMLAudioElement | null>(null)
  const previewUrlRef = useRef<string | null>(null)

  const sampleAudioRef = useRef<HTMLAudioElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  // Everything a live recording holds open, so there is one teardown.
  const recording = useRef<{
    stream: MediaStream
    ctx: AudioContext
    recorder: MediaRecorder
    raf: number
    timer: ReturnType<typeof setInterval>
    discard: boolean
  } | null>(null)
  const mounted = useRef(true)

  // Load persisted custom voices from backend on mount
  useEffect(() => {
    api.listVoices()
      .then((data: VoiceApiResponse[]) => {
        if (!mounted.current || !Array.isArray(data)) return
        const custom: VoiceProfile[] = data.map((v) => ({
          id: v.id,
          name: v.name,
          language: v.language || 'en',
          duration: v.duration || 0,
          createdAt: v.created_at ? new Date(v.created_at) : new Date(),
          isDefault: false,
        }))
        setVoices([DEFAULT_VOICE, ...custom])
      })
      .catch(() => {
        if (mounted.current) toast.error('Could not load your voices')
      })
  }, [])

  // ── Sample (the recording being named) ───────────────────────────────────

  /**
   * Replace the sample awaiting a name. Also drops the <audio> element built
   * for the previous one: it was created once and reused, so after
   * "Re-record" the Play button played the OLD take while the NEW one was
   * what got cloned.
   */
  const setSample = useCallback((blob: Blob | null) => {
    sampleAudioRef.current?.pause()
    sampleAudioRef.current = null
    setIsPlaying(false)
    setAudioBlob(blob)
    setAudioUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous)
      return blob ? URL.createObjectURL(blob) : null
    })
  }, [])

  const togglePlay = () => {
    if (!audioUrl) return
    if (!sampleAudioRef.current) {
      const audio = new Audio(audioUrl)
      audio.onended = () => setIsPlaying(false)
      sampleAudioRef.current = audio
    }
    if (isPlaying) {
      sampleAudioRef.current.pause()
      setIsPlaying(false)
    } else {
      sampleAudioRef.current.play().then(
        () => setIsPlaying(true),
        () => toast.error('Could not play the sample'),
      )
    }
  }

  // ── Library preview ──────────────────────────────────────────────────────

  const stopPreview = useCallback(() => {
    if (previewAudioRef.current) {
      previewAudioRef.current.pause()
      previewAudioRef.current = null
    }
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current)
      previewUrlRef.current = null
    }
    setPreviewingId(null)
  }, [])

  const playPreview = useCallback(async (voiceId: string) => {
    if (previewingId === voiceId) {
      stopPreview()
      return
    }
    stopPreview()
    try {
      const blob = await api.fetchVoicePreviewBlob(voiceId)
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)
      audio.onended = stopPreview
      audio.onerror = () => {
        toast.error('Could not play preview')
        stopPreview()
      }
      previewUrlRef.current = url
      previewAudioRef.current = audio
      setPreviewingId(voiceId)
      await audio.play()
    } catch {
      toast.error('Could not load preview')
      stopPreview()
    }
  }, [previewingId, stopPreview])

  // ── Recording ────────────────────────────────────────────────────────────

  /** Release the mic, analyser, meter loop and timer. Idempotent. */
  const releaseRecording = useCallback(() => {
    const r = recording.current
    recording.current = null
    if (!r) return
    cancelAnimationFrame(r.raf)
    clearInterval(r.timer)
    closeContext(r.ctx)
    stopStream(r.stream)
  }, [])

  const stopRecording = useCallback(() => {
    const r = recording.current
    if (r && r.recorder.state === 'recording') r.recorder.stop()
    setIsRecording(false)
  }, [])

  const startRecording = useCallback(async () => {
    const reason = micUnavailableReason()
    if (reason) {
      toast.error(reason)
      return
    }
    if (recording.current) return

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS)
    } catch (err) {
      toast.error(micErrorMessage(err))
      return
    }
    // The panel was left (or a second start raced this one) while the
    // permission prompt was up: hand the mic straight back.
    if (!mounted.current || recording.current) {
      stopStream(stream)
      return
    }

    const { ctx, analyser } = openAnalyser(stream, 64)
    const bins = new Uint8Array(analyser.frequencyBinCount)
    const recorder = createRecorder(stream)
    const chunks: Blob[] = []
    const startedAt = Date.now()

    const current = { stream, ctx, recorder, raf: 0, timer: undefined as never, discard: false }

    const animateWave = () => {
      analyser.getByteFrequencyData(bins)
      setWaveHeights(Array.from({ length: 20 }, (_, i) => {
        const val = bins[Math.floor((i / 20) * bins.length)] || 0
        return Math.max(4, Math.min(40, val * 0.4))
      }))
      current.raf = requestAnimationFrame(animateWave)
    }
    current.raf = requestAnimationFrame(animateWave)

    // Elapsed time comes from the clock, not from counting ticks: interval
    // callbacks are throttled in a background tab, which made the counter
    // (and the 30-second cap that hung off it) run slow.
    current.timer = setInterval(() => {
      const elapsed = Math.floor((Date.now() - startedAt) / 1000)
      setRecordingTime(Math.min(elapsed, MAX_RECORDING_SECS))
      if (elapsed >= MAX_RECORDING_SECS && recorder.state === 'recording') {
        recorder.stop()
        setIsRecording(false)
      }
    }, 250) as never

    recorder.ondataavailable = (e) => chunks.push(e.data)
    recorder.onstop = () => {
      const discard = current.discard
      if (recording.current === current) releaseRecording()
      if (discard || !mounted.current) return
      setWaveHeights(IDLE_WAVE)
      setSample(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }))
      setStep('name')
    }

    recording.current = current
    recorder.start()
    setIsRecording(true)
    setRecordingTime(0)
  }, [releaseRecording, setSample])

  // Leaving the panel mid-recording must release the microphone. Nothing did
  // before, so the browser's mic indicator stayed on until the tab closed.
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      const r = recording.current
      if (r) {
        r.discard = true
        if (r.recorder.state === 'recording') r.recorder.stop()
      }
      releaseRecording()
      sampleAudioRef.current?.pause()
      previewAudioRef.current?.pause()
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
    }
  }, [releaseRecording])

  // Revoke the sample's object URL when it is replaced or the panel unmounts.
  useEffect(() => () => {
    if (audioUrl) URL.revokeObjectURL(audioUrl)
  }, [audioUrl])

  const handleFileUpload = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    if (!file.type.startsWith('audio/')) {
      toast.error('Please select an audio file (MP3, WAV, M4A, OGG…)')
      return
    }
    if (file.size > 20 * 1024 * 1024) {
      toast.error('File must be under 20 MB')
      return
    }
    setSample(file)
    setRecordingTime(0)
    setStep('name')
  }

  // ── Library actions ──────────────────────────────────────────────────────

  const selectVoice = (id: string) => {
    setPickedVoice(id)
    onVoiceSelect?.(id)
  }

  const cloneVoice = async () => {
    if (!audioBlob || !newVoiceName.trim()) {
      toast.error('Please record audio and give the voice a name')
      return
    }
    setIsCloning(true)
    try {
      const data = await api.cloneVoice(audioBlob, newVoiceName.trim(), newVoiceLang)

      const newProfile: VoiceProfile = {
        id: data.id,
        name: data.name,
        language: data.language || newVoiceLang,
        duration: data.duration || recordingTime,
        createdAt: data.created_at ? new Date(data.created_at) : new Date(),
        isDefault: false,
      }
      setVoices((v) => [...v, newProfile])
      selectVoice(newProfile.id)
      toast.success(`Voice "${newProfile.name}" cloned!`, { icon: '🎙️' })
      setSample(null)
      setNewVoiceName('')
      setNewVoiceLang('en')
      setStep('select')
    } catch (err: unknown) {
      toast.error(errorMessage(err, 'Voice cloning failed — check that the server is running'))
    } finally {
      setIsCloning(false)
    }
  }

  const deleteVoice = async (voice: VoiceProfile) => {
    if (!window.confirm(`Delete the voice "${voice.name}"? This cannot be undone.`)) return
    if (previewingId === voice.id) stopPreview()
    try {
      await api.deleteVoice(voice.id)
    } catch {
      // Report the failure. This used to be swallowed and followed by a
      // "Voice removed" toast, and the voice reappeared on the next reload.
      toast.error('Could not delete the voice')
      return
    }
    setVoices((v) => v.filter((x) => x.id !== voice.id))
    if (selectedVoice === voice.id) selectVoice(DEFAULT_VOICE_ID)
    toast.success('Voice removed')
  }

  const fmtTime = (s: number) =>
    `${Math.floor(s / 60).toString().padStart(2, '0')}:${Math.floor(s % 60).toString().padStart(2, '0')}`

  const active = voices.find((x) => x.id === selectedVoice) ?? DEFAULT_VOICE
  const customCount = voices.length - 1

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
      {/* ── Voice Library ── */}
      <div className="card flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-xl text-gray-900">Voice Library</h2>
            <p className="mt-0.5 text-sm text-gray-500">
              {customCount === 0 ? 'No cloned voices yet' : `${customCount} cloned voice${customCount === 1 ? '' : 's'}`}
            </p>
          </div>
          <button onClick={() => setStep('record')} className="btn-primary px-4 py-2 text-sm">
            <PlusCircle size={15} aria-hidden="true" />
            Clone Voice
          </button>
        </div>

        <div className="divider" />

        <ul className="messages-scroll max-h-96 space-y-2 overflow-y-auto p-1">
          {voices.map((voice) => {
            const isSelected = selectedVoice === voice.id
            const isPreviewing = previewingId === voice.id
            return (
              <li
                key={voice.id}
                className={`group relative flex items-center gap-3 rounded-lg border px-4 py-3 transition-colors duration-150
 ${isSelected ? 'border-primary-600 bg-primary-50' : 'border-gray-200 bg-white hover:bg-gray-50'}`}
              >
                {/* Selection is a real button covering the row, so it works
                    from the keyboard; the row's own actions sit above it. */}
                <button
                  type="button"
                  onClick={() => selectVoice(voice.id)}
                  aria-pressed={isSelected}
                  aria-label={`Use voice ${voice.name}`}
                  className="absolute inset-0 z-10 rounded-lg"
                />

                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-white text-lg" aria-hidden="true">
                  {voice.isDefault ? '🔈' : LANG_FLAGS[voice.language] || '🎙️'}
                </div>

                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold text-gray-900">{voice.name}</p>
                  <div className="mt-0.5 flex flex-wrap items-center gap-2">
                    {voice.isDefault ? (
                      <span className="badge-gray">Built-in · not cloned</span>
                    ) : (
                      <>
                        <span className="badge-brand">Custom · {fmtTime(voice.duration)}</span>
                        <span className="text-[11px] text-gray-500">
                          {voice.createdAt.toLocaleDateString([], { month: 'short', day: 'numeric' })}
                        </span>
                      </>
                    )}
                  </div>
                </div>

                {!voice.isDefault && (
                  <button
                    onClick={() => playPreview(voice.id)}
                    className={`relative z-20 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border shadow-xs transition-colors
 ${isPreviewing
   ? 'border-primary-600 bg-primary-600 text-white'
   : 'border-gray-200 bg-white text-gray-600 hover:bg-gray-50 hover:text-gray-900'}`}
                    title={isPreviewing ? 'Stop preview' : 'Preview voice sample'}
                    aria-label={isPreviewing ? `Stop preview of ${voice.name}` : `Preview ${voice.name}`}
                  >
                    {isPreviewing ? <Pause size={13} /> : <Play size={13} />}
                  </button>
                )}

                {!voice.isDefault && (
                  <button
                    onClick={() => deleteVoice(voice)}
                    className="relative z-20 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-white text-gray-600 shadow-xs transition-colors hover:bg-error-50 hover:text-error-700"
                    aria-label={`Delete voice ${voice.name}`}
                    title="Delete voice"
                  >
                    <Trash2 size={13} />
                  </button>
                )}

                {isSelected && (
                  <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary-600" aria-hidden="true">
                    <Check size={13} className="text-white" />
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      </div>

      {/* ── Right Panel ── */}
      <div className="flex flex-col gap-4">
        {step === 'select' && (
          <div className="card flex animate-fade-in flex-col gap-5">
            <h2 className="text-xl text-gray-900">Active Voice</h2>
            <div className="divider" />
            <div className="flex flex-col items-center gap-4 py-6 text-center">
              <div className="flex h-20 w-20 items-center justify-center rounded-xl border border-gray-200 bg-gray-50 text-4xl" aria-hidden="true">
                {active.isDefault ? '🔈' : LANG_FLAGS[active.language] || '🎙️'}
              </div>
              <div>
                <p className="font-display text-2xl text-gray-900">{active.name}</p>
                <div className="mt-2 flex items-center justify-center gap-2">
                  {active.isDefault
                    ? <span className="badge-gray">Built-in voice</span>
                    : <span className="badge-brand">Custom Clone</span>}
                </div>
                {active.isDefault && (
                  <p className="mx-auto mt-3 max-w-xs text-sm text-gray-500">
                    The avatar speaks with the server&apos;s standard voice. Clone one to make it sound like a specific person.
                  </p>
                )}
              </div>
              <div className="mt-2 flex flex-wrap justify-center gap-2">
                {!active.isDefault && (
                  <button onClick={() => playPreview(active.id)} className="btn-secondary">
                    {previewingId === active.id ? <><Pause size={16} /> Stop</> : <><Play size={16} /> Preview</>}
                  </button>
                )}
                <button onClick={() => setStep('record')} className="btn-secondary">
                  <Wand2 size={16} aria-hidden="true" />
                  Clone a New Voice
                </button>
              </div>
            </div>
          </div>
        )}

        {step === 'record' && (
          <div className="card flex animate-fade-in flex-col gap-5">
            <div className="flex items-center justify-between">
              <h2 className="text-xl text-gray-900">Voice Sample</h2>
              <button
                onClick={() => {
                  // Cancelling mid-recording discards the take and frees the mic.
                  const r = recording.current
                  if (r) r.discard = true
                  stopRecording()
                  setWaveHeights(IDLE_WAVE)
                  setStep('select')
                }}
                className="btn-ghost text-sm"
              >
                Cancel
              </button>
            </div>
            <div className="divider" />

            <div className="flex gap-1 rounded-lg border border-gray-200 bg-white p-1" role="group" aria-label="Sample source">
              {(['mic', 'file'] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setRecordMode(mode)}
                  disabled={isRecording}
                  aria-pressed={recordMode === mode}
                  className={`flex flex-1 items-center justify-center gap-1.5 rounded-md py-2 text-sm font-medium transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-50
 ${recordMode === mode ? 'bg-gray-100 text-gray-900' : 'text-gray-500 hover:bg-gray-50 hover:text-gray-900'}`}
                >
                  {mode === 'mic' ? <><Mic size={14} /> Record Mic</> : <><Upload size={14} /> Upload File</>}
                </button>
              ))}
            </div>

            <div className="card-tint flex items-start gap-3 rounded-lg px-4 py-3">
              <AlertCircle size={16} className="mt-0.5 shrink-0 text-gray-500" aria-hidden="true" />
              <p className="text-sm text-gray-700">
                {recordMode === 'mic'
                  ? <>Record at least <strong className="text-gray-900">10 seconds</strong> of clear speech. Read naturally — avoid background noise.</>
                  : <>Upload an audio file (MP3, WAV, M4A, OGG) with at least <strong className="text-gray-900">10 seconds</strong> of clear speech.</>}
              </p>
            </div>

            {recordMode === 'mic' ? (
              <div className="flex flex-col items-center gap-6 py-4">
                <button
                  onClick={isRecording ? stopRecording : startRecording}
                  aria-label={isRecording ? 'Stop recording' : 'Start recording'}
                  aria-pressed={isRecording}
                  className={`relative flex h-24 w-24 items-center justify-center rounded-full text-white transition-colors duration-200
 ${isRecording ? 'bg-error-600 hover:bg-error-700' : 'bg-primary-600 shadow-xs hover:bg-primary-700'}`}
                >
                  {isRecording && (
                    <span className="absolute inset-0 animate-ping rounded-full border-2 border-error-600" aria-hidden="true" />
                  )}
                  {isRecording ? <MicOff size={36} /> : <Mic size={36} />}
                </button>

                <div className="text-center" aria-live="off">
                  <p className={`font-mono text-3xl tabular-nums ${isRecording ? 'text-error-600' : 'text-gray-500'}`}>
                    {fmtTime(recordingTime)}
                    {isRecording && <span className="ml-2 text-base text-gray-500">/ {fmtTime(MAX_RECORDING_SECS)}</span>}
                  </p>
                  <p className="mt-1 text-sm text-gray-500">
                    {isRecording ? 'Recording — tap to stop' : 'Tap the mic to start'}
                  </p>
                </div>

                {isRecording && (
                  <div className="flex h-10 items-center gap-1" aria-hidden="true">
                    {waveHeights.map((h, i) => <WaveformBar key={i} height={h} />)}
                  </div>
                )}

                {isRecording && (
                  <div
                    className="h-1.5 w-full max-w-xs overflow-hidden rounded-full bg-gray-200"
                    role="progressbar"
                    aria-label="Recording time used"
                    aria-valuemin={0}
                    aria-valuemax={MAX_RECORDING_SECS}
                    aria-valuenow={recordingTime}
                  >
                    <div
                      className="h-full rounded-full bg-primary-600 transition-all duration-300"
                      style={{ width: `${(recordingTime / MAX_RECORDING_SECS) * 100}%` }}
                    />
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-col items-center gap-4 py-4">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="audio/*"
                  className="hidden"
                  onChange={handleFileUpload}
                  aria-label="Choose an audio file"
                />
                <button
                  onClick={() => fileInputRef.current?.click()}
                  className="flex w-full max-w-xs cursor-pointer flex-col items-center gap-3 rounded-xl border-2 border-dashed border-gray-300 py-10 text-gray-600 transition-colors duration-200 hover:border-gray-400 hover:bg-gray-50 hover:text-gray-900"
                >
                  <Upload size={36} className="opacity-60" aria-hidden="true" />
                  <span className="text-sm font-medium">Click to browse audio file</span>
                  <span className="text-xs text-gray-500">MP3, WAV, M4A, OGG · max 20 MB</span>
                </button>
              </div>
            )}
          </div>
        )}

        {step === 'name' && audioUrl && (
          <div className="card flex animate-fade-in flex-col gap-5">
            <div className="flex items-center justify-between">
              <h2 className="text-xl text-gray-900">Name Your Voice</h2>
              <button
                onClick={() => { setSample(null); setRecordingTime(0); setStep('record') }}
                className="btn-ghost text-sm"
              >
                <RefreshCw size={14} aria-hidden="true" /> Re-record
              </button>
            </div>
            <div className="divider" />

            <div className="flex items-center gap-4 rounded-lg border border-gray-200 bg-gray-50 px-4 py-3">
              <button
                onClick={togglePlay}
                aria-label={isPlaying ? 'Pause playback' : 'Play sample'}
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-primary-600 text-white shadow-xs transition-colors hover:bg-primary-700"
              >
                {isPlaying ? <Pause size={18} /> : <Play size={18} className="ml-0.5" />}
              </button>
              <div className="flex-1" aria-hidden="true">
                <div className="flex items-center gap-1">
                  {Array.from({ length: 32 }).map((_, i) => (
                    <div
                      key={i}
                      className="w-0.5 rounded-full bg-gray-400"
                      // Deterministic static waveform — a smooth sine pattern
                      // instead of impure Math.random() in render.
                      style={{ height: `${4 + Math.round(Math.abs(Math.sin(i * 0.6)) * 20)}px` }}
                    />
                  ))}
                </div>
              </div>
              {recordingTime > 0 && (
                <div className="flex items-center gap-1.5 text-xs text-gray-500">
                  <Volume2 size={12} aria-hidden="true" />
                  <span className="tabular-nums">{fmtTime(recordingTime)}</span>
                </div>
              )}
            </div>

            <div className="space-y-1.5">
              <label htmlFor="voice-name" className="text-sm font-medium text-gray-700">Voice Name</label>
              <input
                id="voice-name"
                type="text"
                value={newVoiceName}
                onChange={(e) => setNewVoiceName(e.target.value)}
                className="input-field"
                placeholder="e.g. My Voice, Character A…"
                maxLength={40}
                autoFocus
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="voice-language" className="text-sm font-medium text-gray-700">Language spoken in sample</label>
              <select
                id="voice-language"
                value={newVoiceLang}
                onChange={(e) => setNewVoiceLang(e.target.value)}
                className="input-field"
              >
                {SUPPORTED_LANGUAGES.map((l) => (
                  <option key={l.code} value={l.code}>{l.label}</option>
                ))}
              </select>
            </div>

            <button
              onClick={cloneVoice}
              disabled={isCloning || !newVoiceName.trim()}
              className="btn-primary w-full py-3 text-base"
            >
              {isCloning ? (
                <>
                  <Loader2 size={18} className="animate-spin" />
                  Cloning Voice…
                </>
              ) : (
                <>
                  <Wand2 size={18} />
                  Clone This Voice
                </>
              )}
            </button>

            {isCloning && (
              <p className="text-center text-xs text-gray-500">Extracting voice characteristics…</p>
            )}
          </div>
        )}

        <div className="card flex animate-slide-up items-start gap-3">
          <Music size={16} className="mt-0.5 shrink-0 text-gray-400" aria-hidden="true" />
          <div>
            <p className="mb-1 text-sm font-semibold text-gray-900">How voice cloning works</p>
            <p className="text-xs leading-relaxed text-gray-500">
              Record 10–30 seconds of your voice reading naturally, or upload a longer file. Chatterbox
              Multilingual extracts a speaker embedding and applies it as the avatar&apos;s voice during
              TTS synthesis. 23 languages supported.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
