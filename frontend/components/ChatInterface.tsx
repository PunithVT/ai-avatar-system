'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import {
  Send, Mic, MicOff, Video, Loader2, Volume2, VolumeX,
  Sparkles, Clock, Copy, RotateCcw, Wand2,
  MessageCircle, Zap, Activity,
} from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  emotion?: string
}

interface VideoChunk {
  url: string
  text: string
}

interface ChatInterfaceProps {
  avatarId: string
  onSessionCreated?: (sessionId: string) => void
}

function detectEmotion(text: string): string {
  const lower = text.toLowerCase()
  if (/\b(haha|lol|funny|laugh|joke|hilarious)\b/.test(lower)) return 'happy'
  if (/\b(angry|mad|furious|annoyed|hate)\b/.test(lower)) return 'angry'
  if (/\b(sad|cry|miss|lonely|depressed|unhappy)\b/.test(lower)) return 'sad'
  if (/\b(wow|amazing|awesome|incredible|fantastic|great)\b/.test(lower)) return 'excited'
  if (/\b(think|wonder|curious|how|why|what|interesting)\b/.test(lower)) return 'curious'
  return 'neutral'
}

const EMOTION_CONFIG: Record<string, { label: string; color: string; bg: string }> = {
  happy:   { label: '😄 Happy',   color: 'text-yellow-300', bg: 'bg-yellow-500/20 border-yellow-500/30' },
  angry:   { label: '😠 Angry',   color: 'text-red-300',    bg: 'bg-red-500/20 border-red-500/30' },
  sad:     { label: '😢 Sad',     color: 'text-blue-300',   bg: 'bg-blue-500/20 border-blue-500/30' },
  excited: { label: '🤩 Excited', color: 'text-purple-300', bg: 'bg-purple-500/20 border-purple-500/30' },
  curious: { label: '🤔 Curious', color: 'text-cyan-300',   bg: 'bg-cyan-500/20 border-cyan-500/30' },
  neutral: { label: '😊 Neutral', color: 'text-gray-300',   bg: 'bg-gray-500/20 border-gray-500/30' },
}

function WaveformBars({ active }: { active: boolean }) {
  return (
    <div className="flex items-center gap-0.5 h-6">
      {Array.from({ length: 8 }).map((_, i) => (
        <div
          key={i}
          className="w-1 rounded-full"
          style={{
            height: active ? `${Math.random() * 20 + 4}px` : '4px',
            background: 'linear-gradient(to top, #7c3aed, #3b82f6)',
            transition: 'height 0.15s ease',
            animation: active ? 'waveform 1.2s ease-in-out infinite' : 'none',
            animationDelay: `${i * 0.1}s`,
          }}
        />
      ))}
    </div>
  )
}

function TypingIndicator() {
  return (
    <div className="flex items-end gap-3 animate-slide-up">
      <div className="w-8 h-8 rounded-full bg-gradient-to-br from-primary-600 to-accent-600 flex items-center justify-center flex-shrink-0">
        <Sparkles size={14} className="text-white" />
      </div>
      <div className="glass-card px-4 py-3 rounded-2xl rounded-bl-sm">
        <div className="flex items-center gap-1">
          {[0, 0.2, 0.4].map((delay) => (
            <div
              key={delay}
              className="w-1.5 h-1.5 rounded-full bg-primary-400 animate-bounce"
              style={{ animationDelay: `${delay}s` }}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

// Idle avatar: shows the avatar image with a breathing + glow animation
function IdleAvatar({ imageUrl }: { imageUrl: string | null }) {
  if (!imageUrl) {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-4">
        <div className="w-20 h-20 rounded-full bg-gradient-to-br from-primary-600/30 to-accent-600/20
                        flex items-center justify-center border border-white/10 animate-pulse-slow">
          <Video size={36} className="text-primary-400" />
        </div>
        <p className="text-gray-500 text-sm">Avatar video will appear here</p>
      </div>
    )
  }

  return (
    <div className="absolute inset-0 flex items-center justify-center bg-surface-950">
      {/* Glow ring behind image */}
      <div
        className="absolute w-[70%] aspect-square rounded-full avatar-idle-glow"
        style={{ filter: 'blur(32px)', background: 'radial-gradient(circle, rgba(124,58,237,0.15) 0%, transparent 70%)' }}
      />
      {/* Avatar image with breathing scale */}
      <img
        src={imageUrl}
        alt="Avatar idle"
        className="avatar-idle relative z-10 w-full h-full object-cover"
        style={{ borderRadius: '0.75rem' }}
      />
      {/* Subtle scanline shimmer overlay */}
      <div
        className="absolute inset-0 z-20 pointer-events-none rounded-xl"
        style={{
          background: 'linear-gradient(180deg, transparent 60%, rgba(0,0,0,0.25) 100%)',
        }}
      />
      {/* "Idle" indicator dot */}
      <div className="absolute bottom-3 left-3 z-30 flex items-center gap-1.5 bg-black/40 backdrop-blur-sm
                      px-2 py-1 rounded-full border border-white/10">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
        <span className="text-[10px] text-gray-300 font-medium tracking-wide">IDLE</span>
      </div>
    </div>
  )
}

export function ChatInterface({ avatarId, onSessionCreated }: ChatInterfaceProps) {
  const [messages, setMessages] = useState<Message[]>([])
  const [inputText, setInputText] = useState('')
  const [isRecording, setIsRecording] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [ws, setWs] = useState<WebSocket | null>(null)
  const [isProcessing, setIsProcessing] = useState(false)
  const [isMuted, setIsMuted] = useState(false)
  const [statusMsg, setStatusMsg] = useState('Almost ready…')
  const [isTyping, setIsTyping] = useState(false)
  const [connectionStatus, setConnectionStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting')
  const [recordingLevel, setRecordingLevel] = useState(0)
  const [avatarImageUrl, setAvatarImageUrl] = useState<string | null>(null)

  // Video playback state
  const [showVideo, setShowVideo] = useState(false)           // true while a chunk is playing
  const [currentChunkProgress, setCurrentChunkProgress] = useState({ current: 0, total: 0 })

  // Chunk queue — managed via refs to avoid stale closures in event handlers
  const chunkQueueRef = useRef<VideoChunk[]>([])
  const isPlayingRef = useRef(false)

  const videoRef = useRef<HTMLVideoElement>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const levelAnimRef = useRef<number | null>(null)

  // ── Fetch avatar image on mount ──────────────────────────────────────────
  useEffect(() => {
    api.getAvatars()
      .then((avatars: any[]) => {
        const av = avatars.find((a: any) => a.id === avatarId)
        if (av) {
          setAvatarImageUrl(av.thumbnail_url || av.image_url || null)
        }
      })
      .catch(() => {})
  }, [avatarId])

  // ── Chunk queue player ───────────────────────────────────────────────────
  const playNextChunk = useCallback(() => {
    const next = chunkQueueRef.current.shift()
    if (!next) {
      isPlayingRef.current = false
      setShowVideo(false)
      return
    }
    isPlayingRef.current = true
    setShowVideo(true)
    if (videoRef.current) {
      videoRef.current.src = next.url
      videoRef.current.muted = isMuted
      videoRef.current.play().catch(() => {})
    }
  }, [isMuted])

  // Attach onended handler to video element
  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const handler = () => playNextChunk()
    video.addEventListener('ended', handler)
    return () => video.removeEventListener('ended', handler)
  }, [playNextChunk])

  // Sync muted state to video element
  useEffect(() => {
    if (videoRef.current) videoRef.current.muted = isMuted
  }, [isMuted])

  // Auto-scroll chat
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])
  useEffect(scrollToBottom, [messages, isTyping, scrollToBottom])

  // ── WebSocket ────────────────────────────────────────────────────────────
  const createSessionMutation = useMutation({
    mutationFn: () => api.createSession(avatarId),
    onSuccess: (data) => {
      setSessionId(data.id)
      connectWebSocket(data.id)
      onSessionCreated?.(data.id)
    },
    onError: () => {
      toast.error('Failed to start session')
      setConnectionStatus('disconnected')
    },
  })

  const connectWebSocket = (sid: string) => {
    const wsUrl = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000'
    const websocket = new WebSocket(`${wsUrl}/ws/session/${sid}`)

    websocket.onopen = () => {
      setConnectionStatus('connected')
      toast.success('Connected to avatar!', { icon: '✨' })
    }
    websocket.onmessage = (event) => {
      handleWebSocketMessage(JSON.parse(event.data))
    }
    websocket.onerror = () => {
      setConnectionStatus('disconnected')
      toast.error('Connection error')
    }
    websocket.onclose = () => setConnectionStatus('disconnected')

    setWs(websocket)
  }

  const handleWebSocketMessage = useCallback((data: any) => {
    switch (data.type) {
      case 'transcription':
        setMessages(prev => [...prev, {
          id: Date.now().toString(),
          role: 'user',
          content: data.text,
          timestamp: new Date(),
          emotion: detectEmotion(data.text),
        }])
        setIsTyping(true)
        break

      case 'message':
        setIsTyping(false)
        setMessages(prev => [...prev, {
          id: Date.now().toString(),
          role: 'assistant',
          content: data.content,
          timestamp: new Date(),
          emotion: detectEmotion(data.content),
        }])
        // Keep isProcessing=true — spinner stays until first video chunk arrives
        break

      case 'video_chunk_start':
        chunkQueueRef.current = []
        setCurrentChunkProgress({ current: 0, total: data.total_chunks })
        break

      case 'video_chunk': {
        const chunk: VideoChunk = { url: data.video_url, text: data.text }
        chunkQueueRef.current.push(chunk)
        setCurrentChunkProgress({ current: data.chunk_index + 1, total: data.total_chunks })
        // First chunk arriving → clear processing spinner, start playback
        if (!isPlayingRef.current) {
          setIsProcessing(false)
          playNextChunk()
        }
        break
      }

      case 'video_chunk_end':
        // If nothing ever played (all chunks failed), clear spinner
        if (!isPlayingRef.current) setIsProcessing(false)
        break

      case 'status':
        setIsProcessing(true)
        setStatusMsg(data.message || 'Processing…')
        break

      case 'error':
        toast.error(data.message)
        setIsProcessing(false)
        setIsTyping(false)
        break
    }
  }, [playNextChunk])

  const sendMessage = () => {
    if (!inputText.trim() || !ws || !sessionId) return
    const emotion = detectEmotion(inputText)
    ws.send(JSON.stringify({ type: 'text', text: inputText }))
    setMessages(prev => [...prev, {
      id: Date.now().toString(),
      role: 'user',
      content: inputText,
      timestamp: new Date(),
      emotion,
    }])
    setInputText('')
    setIsProcessing(true)
    setIsTyping(true)
    chunkQueueRef.current = []
    isPlayingRef.current = false
    setShowVideo(false)
  }

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const audioCtx = new AudioContext()
      const analyser = audioCtx.createAnalyser()
      analyser.fftSize = 256
      audioCtx.createMediaStreamSource(stream).connect(analyser)
      audioContextRef.current = audioCtx
      analyserRef.current = analyser

      const updateLevel = () => {
        const data = new Uint8Array(analyser.frequencyBinCount)
        analyser.getByteFrequencyData(data)
        setRecordingLevel(Math.min(100, (data.reduce((a, b) => a + b, 0) / data.length) * 2))
        levelAnimRef.current = requestAnimationFrame(updateLevel)
      }
      updateLevel()

      const mediaRecorder = new MediaRecorder(stream)
      const audioChunks: Blob[] = []
      mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data)
      mediaRecorder.onstop = async () => {
        cancelAnimationFrame(levelAnimRef.current!)
        setRecordingLevel(0)
        audioCtx.close()
        const audioBlob = new Blob(audioChunks, { type: 'audio/webm' })
        const reader = new FileReader()
        reader.onloadend = () => {
          const base64Audio = (reader.result as string).split(',')[1]
          if (ws) {
            ws.send(JSON.stringify({ type: 'audio', audio: base64Audio }))
            setIsProcessing(true)
            chunkQueueRef.current = []
            isPlayingRef.current = false
            setShowVideo(false)
          }
        }
        reader.readAsDataURL(audioBlob)
        stream.getTracks().forEach(t => t.stop())
      }
      mediaRecorder.start()
      mediaRecorderRef.current = mediaRecorder
      setIsRecording(true)
    } catch {
      toast.error('Failed to access microphone')
    }
  }

  const stopRecording = () => {
    mediaRecorderRef.current?.stop()
    setIsRecording(false)
  }

  const resetVideo = () => {
    chunkQueueRef.current = []
    isPlayingRef.current = false
    setShowVideo(false)
    if (videoRef.current) videoRef.current.src = ''
  }

  const copyMessage = (content: string) => {
    navigator.clipboard.writeText(content)
    toast.success('Copied!', { duration: 1500 })
  }

  useEffect(() => {
    createSessionMutation.mutate()
    return () => { ws?.close() }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const isSpeaking = showVideo && !isProcessing

  return (
    <div className="grid grid-cols-1 lg:grid-cols-5 gap-6 h-[calc(100vh-10rem)]">
      {/* ── Video Panel ─────────────────────────────────────────────────── */}
      <div className="lg:col-span-3 flex flex-col gap-4">
        <div className="card-glow flex-1 relative overflow-hidden rounded-2xl group">
          {/* Neon border when speaking */}
          {isSpeaking && (
            <div className="absolute inset-0 rounded-2xl neon-border pointer-events-none z-10 animate-glow" />
          )}

          {/* Main display area */}
          <div className="aspect-video w-full bg-surface-950 rounded-xl overflow-hidden relative">

            {/* ── Idle avatar (always mounted, hidden when video plays) ── */}
            <div
              className="absolute inset-0 transition-opacity duration-500"
              style={{ opacity: showVideo ? 0 : 1, zIndex: showVideo ? 0 : 5 }}
            >
              <IdleAvatar imageUrl={avatarImageUrl} />
            </div>

            {/* ── Video element (mounted always; shown only when playing) ── */}
            <video
              ref={videoRef}
              className="absolute inset-0 w-full h-full object-cover transition-opacity duration-500"
              style={{ opacity: showVideo ? 1 : 0, zIndex: showVideo ? 5 : 0 }}
              autoPlay
              playsInline
              muted={isMuted}
            />

            {/* ── Processing overlay ── */}
            {isProcessing && (
              <div className="absolute inset-0 bg-surface-950/75 backdrop-blur-sm flex flex-col
                              items-center justify-center gap-4 z-20">
                <div className="relative">
                  <div className="w-16 h-16 rounded-full border-2 border-primary-500/30 animate-spin-slow" />
                  <div className="absolute inset-2 rounded-full border-2 border-t-primary-400
                                  border-r-transparent border-b-transparent border-l-transparent animate-spin" />
                  <Wand2 className="absolute inset-0 m-auto text-primary-400" size={20} />
                </div>
                <p className="text-sm text-gray-300 font-medium animate-pulse">{statusMsg}</p>
              </div>
            )}

            {/* ── Chunk progress badge (shows while more chunks are coming) ── */}
            {isSpeaking && currentChunkProgress.total > 1 && (
              <div className="absolute top-3 right-3 z-30 flex items-center gap-1.5 bg-black/50
                              backdrop-blur-sm px-2.5 py-1.5 rounded-full border border-white/10">
                <span className="w-1.5 h-1.5 rounded-full bg-primary-400 animate-pulse" />
                <span className="text-[10px] text-gray-300 font-medium">
                  {currentChunkProgress.current}/{currentChunkProgress.total}
                </span>
              </div>
            )}
          </div>

          {/* Controls bar */}
          <div className="flex items-center justify-between mt-4 px-1">
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1.5 text-xs">
                <span className={`status-dot ${
                  connectionStatus === 'connected' ? 'online'
                  : connectionStatus === 'connecting' ? 'processing'
                  : 'offline'
                }`} />
                <span className="text-gray-400 capitalize">{connectionStatus}</span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              {(showVideo || isProcessing) && (
                <button onClick={resetVideo} className="btn-icon" title="Reset video">
                  <RotateCcw size={15} />
                </button>
              )}
              <button
                onClick={() => setIsMuted(m => !m)}
                className={`btn-icon ${isMuted ? 'text-red-400 border-red-500/30' : ''}`}
                title={isMuted ? 'Unmute' : 'Mute'}
              >
                {isMuted ? <VolumeX size={15} /> : <Volume2 size={15} />}
              </button>
            </div>
          </div>
        </div>

        {/* Emotion bar */}
        {messages.length > 0 && (
          <div className="glass-card px-4 py-3 flex items-center gap-3 rounded-xl animate-slide-up">
            <Activity size={14} className="text-primary-400 flex-shrink-0" />
            <span className="text-xs text-gray-500 flex-shrink-0">Emotion detected:</span>
            <div className="flex flex-wrap gap-2">
              {messages.slice(-1).map(m => {
                const e = m.emotion || 'neutral'
                const cfg = EMOTION_CONFIG[e]
                return (
                  <span key={m.id} className={`badge border ${cfg.bg} ${cfg.color} text-xs`}>
                    {cfg.label}
                  </span>
                )
              })}
            </div>
          </div>
        )}
      </div>

      {/* ── Chat Panel ──────────────────────────────────────────────────── */}
      <div className="lg:col-span-2 flex flex-col glass-card rounded-2xl overflow-hidden p-0">
        <div className="flex items-center justify-between px-5 py-4 border-b border-white/8">
          <div className="flex items-center gap-2">
            <MessageCircle size={16} className="text-primary-400" />
            <span className="font-semibold text-white">Conversation</span>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-gray-500">
            <Zap size={12} className="text-primary-400" />
            <span>{messages.length} messages</span>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4 messages-scroll">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full gap-4 py-12 text-center">
              <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-primary-600/20 to-accent-600/10
                              flex items-center justify-center border border-white/8">
                <Sparkles size={28} className="text-primary-400" />
              </div>
              <div>
                <p className="text-white font-medium mb-1">Start the conversation</p>
                <p className="text-gray-500 text-sm">Type a message or press the mic button</p>
              </div>
            </div>
          ) : (
            messages.map((message, idx) => {
              const isUser = message.role === 'user'
              const emotion = message.emotion || 'neutral'
              const emotionCfg = EMOTION_CONFIG[emotion]
              return (
                <div
                  key={message.id}
                  className={`flex gap-2.5 animate-slide-up ${isUser ? 'flex-row-reverse' : 'flex-row'}`}
                  style={{ animationDelay: `${idx * 0.05}s` }}
                >
                  <div className={`w-7 h-7 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-bold
                    ${isUser
                      ? 'bg-gradient-to-br from-accent-600 to-accent-800'
                      : 'bg-gradient-to-br from-primary-600 to-primary-800'
                    }`}
                  >
                    {isUser ? 'U' : 'AI'}
                  </div>
                  <div className={`max-w-[85%] group ${isUser ? 'items-end' : 'items-start'} flex flex-col gap-1`}>
                    <div className={`relative px-4 py-2.5 rounded-2xl text-sm leading-relaxed
                      ${isUser
                        ? 'bg-gradient-to-br from-primary-700/80 to-accent-700/60 text-white rounded-tr-sm'
                        : 'bg-surface-700/80 border border-white/8 text-gray-200 rounded-tl-sm'
                      }`}
                    >
                      {message.content}
                      <button
                        onClick={() => copyMessage(message.content)}
                        className="absolute -top-2 -right-2 w-6 h-6 rounded-full bg-surface-600
                                   border border-white/10 flex items-center justify-center
                                   opacity-0 group-hover:opacity-100 transition-opacity"
                        title="Copy"
                      >
                        <Copy size={10} className="text-gray-400" />
                      </button>
                    </div>
                    <div className={`flex items-center gap-1.5 px-1 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
                      <Clock size={10} className="text-gray-600" />
                      <span className="text-xs text-gray-600">
                        {new Date(message.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                      </span>
                      {emotion !== 'neutral' && (
                        <span className={`text-xs ${emotionCfg.color}`}>
                          {emotionCfg.label.split(' ')[0]}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              )
            })
          )}
          {isTyping && <TypingIndicator />}
          <div ref={messagesEndRef} />
        </div>

        {isRecording && (
          <div className="px-4 pb-2">
            <div className="h-1 rounded-full bg-surface-700 overflow-hidden">
              <div className="voice-level h-full" style={{ width: `${recordingLevel}%` }} />
            </div>
          </div>
        )}

        <div className="border-t border-white/8 px-4 py-3">
          {isRecording && (
            <div className="flex items-center gap-2 mb-3 px-2">
              <span className="text-xs text-red-400 font-medium animate-pulse">REC</span>
              <WaveformBars active={isRecording} />
              <span className="text-xs text-gray-500 ml-auto">Tap stop when done</span>
            </div>
          )}

          <div className="flex gap-2 items-end">
            <button
              onClick={isRecording ? stopRecording : startRecording}
              disabled={isProcessing}
              className={`relative flex-shrink-0 w-10 h-10 rounded-xl flex items-center justify-center
                transition-all duration-200 active:scale-95
                ${isRecording
                  ? 'bg-red-600 hover:bg-red-500 text-white shadow-[0_0_20px_rgba(239,68,68,0.5)]'
                  : 'bg-surface-700 hover:bg-surface-600 border border-white/10 hover:border-primary-500/40 text-gray-400 hover:text-white'
                }
                ${isProcessing ? 'opacity-40 cursor-not-allowed' : ''}
              `}
            >
              {isRecording ? <MicOff size={18} /> : <Mic size={18} />}
              {isRecording && (
                <span className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-red-500 animate-ping" />
              )}
            </button>

            <div className="flex-1 relative">
              <textarea
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
                }}
                placeholder={isRecording ? 'Recording…' : 'Message your avatar… (Enter to send)'}
                disabled={isProcessing || isRecording}
                rows={1}
                className="w-full px-4 py-2.5 rounded-xl bg-surface-700/80 border border-white/10 text-white text-sm
                           placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-primary-500/50
                           focus:border-primary-500/40 resize-none transition-all duration-200 disabled:opacity-50
                           [field-sizing:content] max-h-32 overflow-y-auto"
              />
            </div>

            <button
              onClick={sendMessage}
              disabled={!inputText.trim() || isProcessing || isRecording}
              className="flex-shrink-0 w-10 h-10 rounded-xl bg-gradient-to-br from-primary-600 to-accent-600
                         flex items-center justify-center text-white hover:shadow-glow
                         disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-200 active:scale-95"
            >
              {isProcessing ? <Loader2 size={18} className="animate-spin" /> : <Send size={18} />}
            </button>
          </div>

          <p className="text-xs text-gray-600 text-center mt-2">
            Shift+Enter for new line · Mic for voice
          </p>
        </div>
      </div>
    </div>
  )
}
