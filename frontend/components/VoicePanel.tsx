'use client'

import { useState, useRef, useCallback, useEffect } from 'react'
import {
  Mic, MicOff, Play, Pause, Trash2, Upload, Check, Loader2,
  Volume2, Wand2, Music, AlertCircle, PlusCircle, RefreshCw,
} from 'lucide-react'
import { toast } from 'react-hot-toast'
import type { VoiceApiResponse, ApiError } from '@/lib/types'

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

const PRESET_VOICES: VoiceProfile[] = [
  { id: 'default-en', name: 'Alex (English)', language: 'en', duration: 0, createdAt: new Date(), isDefault: true },
  { id: 'default-warm', name: 'Jordan (Warm)', language: 'en', duration: 0, createdAt: new Date(), isDefault: true },
  { id: 'default-deep', name: 'Morgan (Deep)', language: 'en', duration: 0, createdAt: new Date(), isDefault: true },
  { id: 'default-es', name: 'Sofia (Spanish)', language: 'es', duration: 0, createdAt: new Date(), isDefault: true },
]

const LANG_FLAGS: Record<string, string> = {
  en: '🇺🇸', es: '🇪🇸', fr: '🇫🇷', de: '🇩🇪', zh: '🇨🇳', ja: '🇯🇵', pt: '🇧🇷', hi: '🇮🇳',
}

function WaveformBar({ active, height }: { active: boolean; height: number }) {
  return (
    <div
      className="w-1 rounded-full transition-all duration-100"
      style={{
        height: active ? `${height}px` : '4px',
        background: 'linear-gradient(to top, #7c3aed, #3b82f6)',
        minHeight: '4px',
        maxHeight: '40px',
      }}
    />
  )
}

interface VoicePanelProps {
  onVoiceSelect?: (voiceId: string) => void
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export function VoicePanel({ onVoiceSelect }: VoicePanelProps = {}) {
  const [voices, setVoices] = useState<VoiceProfile[]>(PRESET_VOICES)
  const [selectedVoice, setSelectedVoice] = useState<string>(PRESET_VOICES[0].id)
  const [isRecording, setIsRecording] = useState(false)
  const [recordingTime, setRecordingTime] = useState(0)
  const [audioBlob, setAudioBlob] = useState<Blob | null>(null)
  const [audioUrl, setAudioUrl] = useState<string | null>(null)
  const [isPlaying, setIsPlaying] = useState(false)
  const [isCloning, setIsCloning] = useState(false)
  const [newVoiceName, setNewVoiceName] = useState('')
  const [newVoiceLang, setNewVoiceLang] = useState('en')
  const [waveHeights, setWaveHeights] = useState<number[]>(Array(20).fill(4))
  const [step, setStep] = useState<'select' | 'record' | 'name'>('select')
  const [recordMode, setRecordMode] = useState<'mic' | 'file'>('mic')

  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const timerRef = useRef<NodeJS.Timeout | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const animFrameRef = useRef<number | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const MAX_RECORDING_SECS = 30

  // Load persisted custom voices from backend on mount
  useEffect(() => {
    fetch(`${API_BASE}/api/v1/voices/`)
      .then(r => r.json())
      .then((data: VoiceApiResponse[]) => {
        if (!Array.isArray(data) || data.length === 0) return
        const custom: VoiceProfile[] = data.map((v: VoiceApiResponse) => ({
          id: v.id,
          name: v.name,
          language: v.language || 'en',
          duration: v.duration || 0,
          createdAt: v.created_at ? new Date(v.created_at) : new Date(),
          isDefault: false,
        }))
        setVoices([...PRESET_VOICES, ...custom])
      })
      .catch(() => { /* backend may not be up yet */ })
  }, [])

  const startRecording = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })

      const audioCtx = new AudioContext()
      const analyser = audioCtx.createAnalyser()
      analyser.fftSize = 64
      audioCtx.createMediaStreamSource(stream).connect(analyser)
      analyserRef.current = analyser

      const animateWave = () => {
        const data = new Uint8Array(analyser.frequencyBinCount)
        analyser.getByteFrequencyData(data)
        const bars = Array.from({ length: 20 }, (_, i) => {
          const val = data[Math.floor((i / 20) * data.length)] || 0
          return Math.max(4, Math.min(40, val * 0.4))
        })
        setWaveHeights(bars)
        animFrameRef.current = requestAnimationFrame(animateWave)
      }
      animateWave()

      const recorder = new MediaRecorder(stream)
      const chunks: Blob[] = []
      recorder.ondataavailable = (e) => chunks.push(e.data)
      recorder.onstop = () => {
        cancelAnimationFrame(animFrameRef.current!)
        setWaveHeights(Array(20).fill(4))
        stream.getTracks().forEach(t => t.stop())
        audioCtx.close()
        const blob = new Blob(chunks, { type: 'audio/webm' })
        setAudioBlob(blob)
        setAudioUrl(URL.createObjectURL(blob))
        setStep('name')
      }

      recorder.start()
      mediaRecorderRef.current = recorder
      setIsRecording(true)
      setRecordingTime(0)

      timerRef.current = setInterval(() => {
        setRecordingTime(t => {
          if (t + 1 >= MAX_RECORDING_SECS) {
            stopRecording()
            return t
          }
          return t + 1
        })
      }, 1000)
    } catch {
      toast.error('Microphone access denied')
    }
  }, [])

  const stopRecording = useCallback(() => {
    mediaRecorderRef.current?.stop()
    if (timerRef.current) clearInterval(timerRef.current)
    setIsRecording(false)
  }, [])

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    if (!file.type.startsWith('audio/')) {
      toast.error('Please select an audio file (MP3, WAV, M4A, OGG…)')
      return
    }
    if (file.size > 20 * 1024 * 1024) {
      toast.error('File must be under 20 MB')
      return
    }
    setAudioBlob(file)
    setAudioUrl(URL.createObjectURL(file))
    setRecordingTime(0)
    setStep('name')
  }

  const togglePlay = () => {
    if (!audioUrl) return
    if (!audioRef.current) {
      audioRef.current = new Audio(audioUrl)
      audioRef.current.onended = () => setIsPlaying(false)
    }
    if (isPlaying) {
      audioRef.current.pause()
      setIsPlaying(false)
    } else {
      audioRef.current.play()
      setIsPlaying(true)
    }
  }

  const cloneVoice = async () => {
    if (!audioBlob || !newVoiceName.trim()) {
      toast.error('Please record audio and give the voice a name')
      return
    }
    setIsCloning(true)
    try {
      const formData = new FormData()
      formData.append('audio', audioBlob, 'voice_sample.webm')
      formData.append('name', newVoiceName.trim())
      formData.append('language', newVoiceLang)

      const res = await fetch(`${API_BASE}/api/v1/voices/clone`, {
        method: 'POST',
        body: formData,
      })

      if (!res.ok) {
        let detail = 'Voice cloning failed'
        try {
          const body = await res.json()
          detail = body.detail || detail
        } catch {}
        throw new Error(detail)
      }
      const data = await res.json()

      const newProfile: VoiceProfile = {
        id: data.id,
        name: data.name,
        language: data.language || newVoiceLang,
        duration: data.duration || recordingTime,
        createdAt: data.created_at ? new Date(data.created_at) : new Date(),
        isDefault: false,
      }
      setVoices(v => [...v, newProfile])
      setSelectedVoice(newProfile.id)
      onVoiceSelect?.(newProfile.id)
      toast.success(`Voice "${newProfile.name}" cloned!`, { icon: '🎙️' })
      // Reset
      setAudioBlob(null)
      setAudioUrl(null)
      setNewVoiceName('')
      setNewVoiceLang('en')
      setStep('select')
    } catch (err: unknown) {
      toast.error((err as ApiError)?.message || 'Voice cloning failed — check backend is running')
    } finally {
      setIsCloning(false)
    }
  }

  const deleteVoice = async (id: string) => {
    try {
      await fetch(`${API_BASE}/api/v1/voices/${id}`, { method: 'DELETE' })
    } catch { /* ignore network errors */ }
    setVoices(v => v.filter(x => x.id !== id))
    if (selectedVoice === id) {
      setSelectedVoice(PRESET_VOICES[0].id)
      onVoiceSelect?.(PRESET_VOICES[0].id)
    }
    toast.success('Voice removed')
  }

  const fmtTime = (s: number) => `${Math.floor(s / 60).toString().padStart(2, '0')}:${(s % 60).toString().padStart(2, '0')}`

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* ── Voice Library ── */}
      <div className="card flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-xl font-bold text-white">Voice Library</h2>
            <p className="text-sm text-gray-500 mt-0.5">{voices.length} voices available</p>
          </div>
          <button
            onClick={() => setStep('record')}
            className="btn-primary px-4 py-2 text-sm"
          >
            <PlusCircle size={15} />
            Clone Voice
          </button>
        </div>

        <div className="divider" />

        <div className="space-y-2 overflow-y-auto max-h-96 messages-scroll">
          {voices.map((voice) => {
            const isSelected = selectedVoice === voice.id
            return (
              <div
                key={voice.id}
                onClick={() => { setSelectedVoice(voice.id); onVoiceSelect?.(voice.id) }}
                className={`flex items-center gap-3 px-4 py-3 rounded-xl cursor-pointer transition-all duration-200 group
                  ${isSelected
                    ? 'bg-primary-500/15 border border-primary-500/40 shadow-glow-sm'
                    : 'bg-surface-700/40 border border-white/6 hover:bg-surface-700/70 hover:border-primary-500/20'
                  }`}
              >
                {/* Voice icon */}
                <div className={`w-10 h-10 rounded-xl flex items-center justify-center text-lg flex-shrink-0
                  ${voice.isDefault ? 'bg-surface-600' : 'bg-gradient-to-br from-primary-700/60 to-accent-700/40'}`}
                >
                  {LANG_FLAGS[voice.language] || '🎙️'}
                </div>

                <div className="flex-1 min-w-0">
                  <p className="font-semibold text-sm text-white truncate">{voice.name}</p>
                  <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                    {voice.isDefault ? (
                      <span className="badge-blue text-xs">Preset</span>
                    ) : (
                      <>
                        <span className="badge-purple text-xs">Custom · {fmtTime(voice.duration)}</span>
                        {voice.createdAt && (
                          <span className="text-[10px] text-gray-600">
                            {new Date(voice.createdAt).toLocaleDateString([], { month: 'short', day: 'numeric' })}
                          </span>
                        )}
                      </>
                    )}
                  </div>
                </div>

                {isSelected ? (
                  <div className="w-6 h-6 rounded-full bg-primary-500 flex items-center justify-center flex-shrink-0">
                    <Check size={13} className="text-white" />
                  </div>
                ) : !voice.isDefault ? (
                  <button
                    onClick={(e) => { e.stopPropagation(); deleteVoice(voice.id) }}
                    className="w-7 h-7 rounded-lg bg-red-600/0 hover:bg-red-600/30 flex items-center justify-center
                               text-gray-600 hover:text-red-400 opacity-0 group-hover:opacity-100
                               transition-all duration-200 flex-shrink-0"
                  >
                    <Trash2 size={13} />
                  </button>
                ) : null}
              </div>
            )
          })}
        </div>
      </div>

      {/* ── Right Panel ── */}
      <div className="flex flex-col gap-4">

        {/* Select step — show selected voice info */}
        {step === 'select' && (
          <div className="card flex flex-col gap-5 animate-fade-in">
            <h2 className="text-xl font-bold text-white">Active Voice</h2>
            <div className="divider" />
            {(() => {
              const v = voices.find(x => x.id === selectedVoice)
              if (!v) return null
              return (
                <div className="flex flex-col items-center text-center gap-4 py-6">
                  <div className="w-20 h-20 rounded-2xl bg-gradient-to-br from-primary-700/40 to-accent-700/30 border border-white/10 flex items-center justify-center text-4xl">
                    {LANG_FLAGS[v.language] || '🎙️'}
                  </div>
                  <div>
                    <p className="text-2xl font-black text-white">{v.name}</p>
                    <div className="flex items-center justify-center gap-2 mt-2">
                      {v.isDefault ? <span className="badge-blue">Preset Voice</span> : <span className="badge-purple">Custom Clone</span>}
                    </div>
                  </div>
                  <button
                    onClick={() => setStep('record')}
                    className="btn-secondary mt-2"
                  >
                    <Wand2 size={16} />
                    Clone a New Voice
                  </button>
                </div>
              )
            })()}
          </div>
        )}

        {/* Record step */}
        {step === 'record' && (
          <div className="card flex flex-col gap-5 animate-fade-in">
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-bold text-white">Voice Sample</h2>
              <button onClick={() => setStep('select')} className="btn-ghost text-sm">Cancel</button>
            </div>
            <div className="divider" />

            {/* Tab switcher: mic vs file */}
            <div className="flex gap-1 p-1 rounded-xl bg-surface-800/80 border border-white/8">
              {(['mic', 'file'] as const).map(mode => (
                <button
                  key={mode}
                  onClick={() => setRecordMode(mode)}
                  className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg text-sm font-medium transition-all duration-200
                    ${recordMode === mode
                      ? 'bg-gradient-to-r from-primary-600/80 to-accent-600/80 text-white shadow-glow-sm'
                      : 'text-gray-400 hover:text-white'
                    }`}
                >
                  {mode === 'mic' ? <><Mic size={14} /> Record Mic</> : <><Upload size={14} /> Upload File</>}
                </button>
              ))}
            </div>

            {/* Instructions */}
            <div className="flex items-start gap-3 px-4 py-3 rounded-xl bg-accent-500/10 border border-accent-500/20">
              <AlertCircle size={16} className="text-accent-400 mt-0.5 flex-shrink-0" />
              <p className="text-sm text-gray-300">
                {recordMode === 'mic'
                  ? <>Record at least <strong className="text-white">5 seconds</strong> of clear speech. Read naturally — avoid background noise.</>
                  : <>Upload an audio file (MP3, WAV, M4A, OGG) with at least <strong className="text-white">5 seconds</strong> of clear speech.</>
                }
              </p>
            </div>

            {recordMode === 'mic' ? (
              /* Big record button */
              <div className="flex flex-col items-center gap-6 py-4">
                <button
                  onClick={isRecording ? stopRecording : startRecording}
                  className={`relative w-24 h-24 rounded-full flex items-center justify-center transition-all duration-300
                    ${isRecording
                      ? 'bg-red-600 shadow-[0_0_40px_rgba(239,68,68,0.5)] scale-110'
                      : 'bg-gradient-to-br from-primary-600 to-accent-600 hover:shadow-glow hover:scale-105'
                    }`}
                >
                  {isRecording && (
                    <span className="absolute inset-0 rounded-full border-2 border-red-400 animate-ping" />
                  )}
                  {isRecording ? <MicOff size={36} className="text-white" /> : <Mic size={36} className="text-white" />}
                </button>

                <div className="text-center">
                  <p className={`text-3xl font-mono font-black ${isRecording ? 'text-red-400' : 'text-gray-500'}`}>
                    {fmtTime(recordingTime)}
                    {isRecording && <span className="text-base ml-2 text-gray-500">/ {fmtTime(MAX_RECORDING_SECS)}</span>}
                  </p>
                  <p className="text-sm text-gray-500 mt-1">
                    {isRecording ? 'Recording — tap to stop' : 'Tap the mic to start'}
                  </p>
                </div>

                {isRecording && (
                  <div className="flex items-center gap-1">
                    {waveHeights.map((h, i) => <WaveformBar key={i} active={isRecording} height={h} />)}
                  </div>
                )}

                {isRecording && (
                  <div className="w-full max-w-xs h-1.5 rounded-full bg-surface-600 overflow-hidden">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-primary-500 to-accent-500 transition-all duration-1000"
                      style={{ width: `${(recordingTime / MAX_RECORDING_SECS) * 100}%` }}
                    />
                  </div>
                )}
              </div>
            ) : (
              /* File upload */
              <div className="flex flex-col items-center gap-4 py-4">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="audio/*"
                  className="hidden"
                  onChange={handleFileUpload}
                />
                <button
                  onClick={() => fileInputRef.current?.click()}
                  className="w-full max-w-xs flex flex-col items-center gap-3 py-10 rounded-2xl border-2 border-dashed
                             border-white/15 hover:border-primary-500/50 hover:bg-primary-500/5
                             text-gray-400 hover:text-white transition-all duration-200 cursor-pointer"
                >
                  <Upload size={36} className="opacity-60" />
                  <span className="text-sm font-medium">Click to browse audio file</span>
                  <span className="text-xs text-gray-600">MP3, WAV, M4A, OGG · max 20 MB</span>
                </button>
              </div>
            )}
          </div>
        )}

        {/* Name step */}
        {step === 'name' && audioUrl && (
          <div className="card flex flex-col gap-5 animate-fade-in">
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-bold text-white">Name Your Voice</h2>
              <button
                onClick={() => { setStep('record'); setAudioBlob(null); setAudioUrl(null) }}
                className="btn-ghost text-sm"
              >
                <RefreshCw size={14} /> Re-record
              </button>
            </div>
            <div className="divider" />

            {/* Playback */}
            <div className="flex items-center gap-4 px-4 py-3 rounded-xl bg-surface-700/60 border border-white/8">
              <button
                onClick={togglePlay}
                className="w-10 h-10 rounded-xl bg-gradient-to-br from-primary-600 to-accent-600 flex items-center justify-center flex-shrink-0 hover:shadow-glow transition-all"
              >
                {isPlaying ? <Pause size={18} className="text-white" /> : <Play size={18} className="text-white ml-0.5" />}
              </button>
              <div className="flex-1">
                <div className="flex items-center gap-1">
                  {Array.from({ length: 32 }).map((_, i) => (
                    <div
                      key={i}
                      className="w-0.5 rounded-full bg-primary-500/60"
                      style={{ height: `${Math.random() * 20 + 4}px` }}
                    />
                  ))}
                </div>
              </div>
              <div className="flex items-center gap-1.5 text-xs text-gray-500">
                <Volume2 size={12} />
                <span>{fmtTime(recordingTime)}</span>
              </div>
            </div>

            {/* Name input */}
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-gray-300">Voice Name</label>
              <input
                type="text"
                value={newVoiceName}
                onChange={(e) => setNewVoiceName(e.target.value)}
                className="input-field"
                placeholder="e.g. My Voice, Character A…"
                maxLength={40}
                autoFocus
              />
            </div>

            {/* Language selector */}
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-gray-300">Language spoken in sample</label>
              <select
                value={newVoiceLang}
                onChange={(e) => setNewVoiceLang(e.target.value)}
                className="input-field appearance-none"
              >
                {SUPPORTED_LANGUAGES.map(l => (
                  <option key={l.code} value={l.code}>{l.label}</option>
                ))}
              </select>
            </div>

            {/* Clone button */}
            <button
              onClick={cloneVoice}
              disabled={isCloning || !newVoiceName.trim()}
              className="btn-primary w-full py-3.5 text-base rounded-xl"
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
              <p className="text-xs text-center text-gray-500 animate-pulse">
                Extracting voice characteristics · Training speaker model…
              </p>
            )}
          </div>
        )}

        {/* Info card */}
        <div className="card-glow flex items-start gap-3 animate-slide-up">
          <Music size={16} className="text-primary-400 mt-0.5 flex-shrink-0" />
          <div>
            <p className="text-sm font-semibold text-white mb-1">How voice cloning works</p>
            <p className="text-xs text-gray-500 leading-relaxed">
              Record 5–30 seconds of your voice reading naturally. The system extracts a speaker embedding
              using XTTS v2 and applies it as the avatar's voice during TTS synthesis.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
