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

interface ChatInterfaceProps {
  avatarId: string
  onSessionCreated?: (sessionId: string) => void
}

// Detect emotion from text (lightweight heuristic — server-side model can replace)
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

const LOADING_MESSAGES = [
  'Processing your voice…',
  'Generating response…',
  'Animating avatar…',
  'Rendering lip-sync…',
  'Almost ready…',
]

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
            animationDelay: `${i * 0.1}s`,
            animation: active ? 'waveform 1.2s ease-in-out infinite' : 'none',
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

export function ChatInterface({ avatarId, onSessionCreated }: ChatInterfaceProps) {
  const [messages, setMessages] = useState<Message[]>([])
  const [inputText, setInputText] = useState('')
  const [isRecording, setIsRecording] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [ws, setWs] = useState<WebSocket | null>(null)
  const [currentVideo, setCurrentVideo] = useState<string | null>(null)
  const [isProcessing, setIsProcessing] = useState(false)
  const [isMuted, setIsMuted] = useState(false)
  const [loadingMsg, setLoadingMsg] = useState(LOADING_MESSAGES[0])
  const [isTyping, setIsTyping] = useState(false)
  const [connectionStatus, setConnectionStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting')
  const [recordingLevel, setRecordingLevel] = useState(0)

  const videoRef = useRef<HTMLVideoElement>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const loadingIntervalRef = useRef<NodeJS.Timeout | null>(null)
  const levelAnimRef = useRef<number | null>(null)

  // Auto-scroll
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])

  useEffect(scrollToBottom, [messages, isTyping, scrollToBottom])

  // Rotate loading messages
  useEffect(() => {
    if (isProcessing) {
      let i = 0
      loadingIntervalRef.current = setInterval(() => {
        i = (i + 1) % LOADING_MESSAGES.length
        setLoadingMsg(LOADING_MESSAGES[i])
      }, 1800)
    } else {
      if (loadingIntervalRef.current) clearInterval(loadingIntervalRef.current)
    }
    return () => { if (loadingIntervalRef.current) clearInterval(loadingIntervalRef.current) }
  }, [isProcessing])

  // Create session
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
      const data = JSON.parse(event.data)
      handleWebSocketMessage(data)
    }

    websocket.onerror = () => {
      setConnectionStatus('disconnected')
      toast.error('Connection error')
    }

    websocket.onclose = () => {
      setConnectionStatus('disconnected')
    }

    setWs(websocket)
  }

  const handleWebSocketMessage = (data: any) => {
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
        setIsProcessing(false)
        toast.dismiss('processing')
        break

      case 'video':
        setCurrentVideo(data.video_url)
        setIsProcessing(false)
        if (videoRef.current && !isMuted) {
          videoRef.current.src = data.video_url
          videoRef.current.play()
        }
        break

      case 'status':
        setIsProcessing(true)
        break

      case 'error':
        toast.error(data.message)
        setIsProcessing(false)
        setIsTyping(false)
        break
    }
  }

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
  }

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })

      // Set up audio level analyser
      const audioCtx = new AudioContext()
      const analyser = audioCtx.createAnalyser()
      analyser.fftSize = 256
      const source = audioCtx.createMediaStreamSource(stream)
      source.connect(analyser)
      audioContextRef.current = audioCtx
      analyserRef.current = analyser

      const updateLevel = () => {
        const data = new Uint8Array(analyser.frequencyBinCount)
        analyser.getByteFrequencyData(data)
        const avg = data.reduce((a, b) => a + b, 0) / data.length
        setRecordingLevel(Math.min(100, avg * 2))
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

  const copyMessage = (content: string) => {
    navigator.clipboard.writeText(content)
    toast.success('Copied!', { duration: 1500 })
  }

  useEffect(() => {
    createSessionMutation.mutate()
    return () => { ws?.close() }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="grid grid-cols-1 lg:grid-cols-5 gap-6 h-[calc(100vh-10rem)]">
      {/* ── Video Panel (3 cols) ── */}
      <div className="lg:col-span-3 flex flex-col gap-4">
        <div className="card-glow flex-1 relative overflow-hidden rounded-2xl group">
          {/* Glow border animation when video playing */}
          {currentVideo && !isProcessing && (
            <div className="absolute inset-0 rounded-2xl neon-border pointer-events-none z-10 animate-glow" />
          )}

          {/* Video / Placeholder */}
          <div className="aspect-video w-full bg-surface-950 rounded-xl overflow-hidden relative">
            {currentVideo ? (
              <video
                ref={videoRef}
                className="w-full h-full object-cover"
                autoPlay
                playsInline
                muted={isMuted}
                loop={false}
              />
            ) : (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-4">
                <div className="w-20 h-20 rounded-full bg-gradient-to-br from-primary-600/30 to-accent-600/20 flex items-center justify-center border border-white/10 animate-pulse-slow">
                  <Video size={36} className="text-primary-400" />
                </div>
                <p className="text-gray-500 text-sm">Avatar video will appear here</p>
              </div>
            )}

            {/* Processing overlay */}
            {isProcessing && (
              <div className="absolute inset-0 bg-surface-950/80 backdrop-blur-sm flex flex-col items-center justify-center gap-4 z-20">
                <div className="relative">
                  <div className="w-16 h-16 rounded-full border-2 border-primary-500/30 animate-spin-slow" />
                  <div className="absolute inset-2 rounded-full border-2 border-t-primary-400 border-r-transparent border-b-transparent border-l-transparent animate-spin" />
                  <Wand2 className="absolute inset-0 m-auto text-primary-400" size={20} />
                </div>
                <p className="text-sm text-gray-300 font-medium animate-pulse">{loadingMsg}</p>
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
              {currentVideo && (
                <button
                  onClick={() => {
                    setCurrentVideo(null)
                    if (videoRef.current) videoRef.current.src = ''
                  }}
                  className="btn-icon"
                  title="Reset video"
                >
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

      {/* ── Chat Panel (2 cols) ── */}
      <div className="lg:col-span-2 flex flex-col glass-card rounded-2xl overflow-hidden p-0">
        {/* Header */}
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

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4 messages-scroll">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full gap-4 py-12 text-center">
              <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-primary-600/20 to-accent-600/10 flex items-center justify-center border border-white/8">
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
                  {/* Avatar bubble */}
                  <div className={`w-7 h-7 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-bold
                    ${isUser
                      ? 'bg-gradient-to-br from-accent-600 to-accent-800'
                      : 'bg-gradient-to-br from-primary-600 to-primary-800'
                    }`}
                  >
                    {isUser ? 'U' : 'AI'}
                  </div>

                  <div className={`max-w-[85%] group ${isUser ? 'items-end' : 'items-start'} flex flex-col gap-1`}>
                    <div
                      className={`relative px-4 py-2.5 rounded-2xl text-sm leading-relaxed
                        ${isUser
                          ? 'bg-gradient-to-br from-primary-700/80 to-accent-700/60 text-white rounded-tr-sm'
                          : 'bg-surface-700/80 border border-white/8 text-gray-200 rounded-tl-sm'
                        }`}
                    >
                      {message.content}
                      <button
                        onClick={() => copyMessage(message.content)}
                        className="absolute -top-2 -right-2 w-6 h-6 rounded-full bg-surface-600 border border-white/10 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                        title="Copy"
                      >
                        <Copy size={10} className="text-gray-400" />
                      </button>
                    </div>

                    {/* Timestamp + emotion */}
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

        {/* Recording level bar */}
        {isRecording && (
          <div className="px-4 pb-2">
            <div className="h-1 rounded-full bg-surface-700 overflow-hidden">
              <div
                className="voice-level h-full"
                style={{ width: `${recordingLevel}%` }}
              />
            </div>
          </div>
        )}

        {/* Input */}
        <div className="border-t border-white/8 px-4 py-3">
          {isRecording && (
            <div className="flex items-center gap-2 mb-3 px-2">
              <span className="text-xs text-red-400 font-medium animate-pulse">REC</span>
              <WaveformBars active={isRecording} />
              <span className="text-xs text-gray-500 ml-auto">Tap stop when done</span>
            </div>
          )}

          <div className="flex gap-2 items-end">
            {/* Mic button */}
            <button
              onClick={isRecording ? stopRecording : startRecording}
              disabled={isProcessing}
              className={`relative flex-shrink-0 w-10 h-10 rounded-xl flex items-center justify-center transition-all duration-200 active:scale-95
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

            {/* Text input */}
            <div className="flex-1 relative">
              <textarea
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    sendMessage()
                  }
                }}
                placeholder={isRecording ? 'Recording…' : 'Message your avatar… (Enter to send)'}
                disabled={isProcessing || isRecording}
                rows={1}
                className="w-full px-4 py-2.5 rounded-xl bg-surface-700/80 border border-white/10 text-white text-sm
                           placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-primary-500/50 focus:border-primary-500/40
                           resize-none transition-all duration-200 disabled:opacity-50
                           [field-sizing:content] max-h-32 overflow-y-auto"
              />
            </div>

            {/* Send button */}
            <button
              onClick={sendMessage}
              disabled={!inputText.trim() || isProcessing || isRecording}
              className="flex-shrink-0 w-10 h-10 rounded-xl bg-gradient-to-br from-primary-600 to-accent-600 flex items-center justify-center text-white
                         hover:shadow-glow disabled:opacity-30 disabled:cursor-not-allowed
                         transition-all duration-200 active:scale-95"
            >
              {isProcessing ? (
                <Loader2 size={18} className="animate-spin" />
              ) : (
                <Send size={18} />
              )}
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
