'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import {
  Send, Mic, MicOff, Video, Loader2, Volume2, VolumeX,
  Sparkles, Clock, Copy, RotateCcw, Wand2,
  MessageCircle, Zap, Activity, Download, Globe,
  Pencil, Trash2, Check, X, Keyboard, Plug, Square,
} from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { toast } from 'react-hot-toast'
import { api, buildSessionWsUrl } from '@/lib/api'
import { useStore } from '@/store/useStore'
import type { Avatar, ChatMessage, WsMessage } from '@/lib/types'

const WS_AUTH_REJECT_CODE = 4401  // matches backend close code for auth/ownership failure
const MAX_WS_RECONNECT_ATTEMPTS = 6

const CHAT_LANGUAGES = [
  { code: 'en', label: 'EN' }, { code: 'es', label: 'ES' }, { code: 'fr', label: 'FR' },
  { code: 'de', label: 'DE' }, { code: 'zh', label: 'ZH' }, { code: 'ja', label: 'JA' },
  { code: 'pt', label: 'PT' }, { code: 'hi', label: 'HI' }, { code: 'it', label: 'IT' },
  { code: 'ko', label: 'KO' },
]

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  emotion?: string
  // `persisted` means this message came back from the server (so it has a
  // real DB id and supports edit/delete). Optimistic locally-created
  // messages don't get the action menu until they're re-fetched.
  persisted?: boolean
}

interface VideoChunk {
  url: string
  text: string
}

interface ChatInterfaceProps {
  avatarId: string
  voiceId?: string
  /** When set, resume this existing session (from history) instead of creating
   *  a fresh one — the backend rehydrates the prior messages on connect. */
  resumeSessionId?: string
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

// Deterministic per-bar heights — a static varied pattern instead of
// Math.random() (which is impure in render and jitters on every re-render).
// The CSS `waveform` animation supplies the live motion when `active`.
const _WAVE_HEIGHTS = [10, 18, 8, 22, 14, 20, 6, 16]

function WaveformBars({ active }: { active: boolean }) {
  return (
    <div className="flex items-center gap-0.5 h-6">
      {Array.from({ length: 8 }).map((_, i) => (
        <div
          key={i}
          className="w-1 rounded-full"
          style={{
            height: active ? `${_WAVE_HEIGHTS[i]}px` : '4px',
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

export function ChatInterface({ avatarId, voiceId, resumeSessionId, onSessionCreated }: ChatInterfaceProps) {
  const setWsConnected = useStore((s) => s.setWsConnected)

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
  // Streaming token accumulator — shown as a live bubble while LLM is generating
  const [streamingContent, setStreamingContent] = useState('')
  const [language, setLanguage] = useState('en')
  const [latencyMs, setLatencyMs] = useState<number | null>(null)

  const sendTimeRef = useRef<number>(0)
  const reconnectAttemptsRef = useRef(0)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const sessionIdRef = useRef<string | null>(null)
  // Same-value ref alongside the `ws` state: the unmount cleanup runs with
  // the FIRST render's closure (deps []), where `ws` is still null — reading
  // through a ref is the only way it sees the live socket.
  const wsRef = useRef<WebSocket | null>(null)
  // True only when THIS mount created the session — so we end it on unmount.
  // A resumed session (opened from history) is left intact when the user leaves.
  const createdHereRef = useRef(false)

  // Edit/delete state. We track by message id (the persisted DB id) so the
  // hover menu can drive the corresponding API call.
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState('')

  // Help/keyboard-shortcuts modal
  const [showShortcuts, setShowShortcuts] = useState(false)

  // True when the reconnect loop has hit MAX_WS_RECONNECT_ATTEMPTS or the
  // backend rejected the handshake. Surfaces a "Reconnect" button in the UI.
  const [reconnectStalled, setReconnectStalled] = useState(false)

  // Video playback state
  const [showVideo, setShowVideo] = useState(false)           // true while a chunk is playing
  const [currentChunkProgress, setCurrentChunkProgress] = useState({ current: 0, total: 0 })

  // Chunk queue — managed via refs to avoid stale closures in event handlers
  const chunkQueueRef = useRef<VideoChunk[]>([])
  const isPlayingRef = useRef(false)

  const videoRef = useRef<HTMLVideoElement>(null)
  // Hidden video element used to preload the next chunk while the current one plays
  const preloadVideoRef = useRef<HTMLVideoElement>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const levelAnimRef = useRef<number | null>(null)

  // ── Fetch avatar image on mount ──────────────────────────────────────────
  useEffect(() => {
    api.getAvatars()
      .then((avatars: Avatar[]) => {
        const av = avatars.find((a: Avatar) => a.id === avatarId)
        if (av) {
          setAvatarImageUrl(av.thumbnail_url || av.image_url || null)
        } else {
          toast.error('Could not load avatar image')
        }
      })
      .catch(() => toast.error('Could not load avatar image'))
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
      const video = videoRef.current
      video.src = next.url
      // Always START muted — browsers always allow muted autoplay, even
      // outside a user gesture. Starting unmuted gets silently rejected by
      // Chrome/Edge once the LLM+TTS+animation pipeline (several seconds) has
      // eaten the brief post-gesture window that permits unmuted autoplay —
      // which would needlessly skip this clip via the catch below. Unmuting an
      // element that's ALREADY playing needs no fresh gesture, so we restore
      // the user's mute choice right after play() resolves.
      video.muted = true
      video.play()
        .then(() => { video.muted = isMuted })
        // A rejected play() (decode error, bad clip) must not strand the
        // queue — skip to the next chunk so playback can't hang on one bad clip.
        .catch(() => {
          if (chunkQueueRef.current.length > 0) playNextChunk()
          else { isPlayingRef.current = false; setShowVideo(false) }
        })
    }
    // Preload the next chunk in queue (if any)
    const upcoming = chunkQueueRef.current[0]
    if (upcoming && preloadVideoRef.current) {
      preloadVideoRef.current.src = upcoming.url
    }
  }, [isMuted])

  // Attach ended + error handlers to the video element. The error handler is
  // critical: a 404 / expired presigned URL / undecodable chunk fires `error`
  // (not `ended`), so without this the whole reply hangs on one bad chunk.
  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const onEnded = () => playNextChunk()
    const onError = () => {
      if (chunkQueueRef.current.length > 0) playNextChunk()
      else { isPlayingRef.current = false; setShowVideo(false) }
    }
    video.addEventListener('ended', onEnded)
    video.addEventListener('error', onError)
    return () => {
      video.removeEventListener('ended', onEnded)
      video.removeEventListener('error', onError)
    }
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
  // Load a session's persisted messages into the transcript (used both on a
  // fresh session — usually empty — and when resuming one from history).
  const loadHistory = useCallback(async (sid: string) => {
    try {
      const prev = await api.getMessages(sid)
      if (Array.isArray(prev) && prev.length > 0) {
        setMessages(prev
          .filter((m: ChatMessage) => m.role === 'user' || m.role === 'assistant')
          .map((m: ChatMessage) => ({
            id: m.id,
            role: m.role as 'user' | 'assistant',
            content: m.content,
            timestamp: new Date(m.created_at),
            emotion: detectEmotion(m.content),
            persisted: true,
          }))
        )
      }
    } catch { /* ignore — history is non-critical */ }
  }, [])

  // Attach to an EXISTING session (resume from history). The backend
  // rehydrates the LLM context on connect; we restore the visible transcript.
  const adoptSession = useCallback((sid: string) => {
    setSessionId(sid)
    sessionIdRef.current = sid
    createdHereRef.current = false
    connectWebSocket(sid)
    onSessionCreated?.(sid)
    loadHistory(sid)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadHistory])

  const createSessionMutation = useMutation({
    mutationFn: () => api.createSession(avatarId),
    onSuccess: async (data) => {
      setSessionId(data.id)
      sessionIdRef.current = data.id
      createdHereRef.current = true
      connectWebSocket(data.id)
      onSessionCreated?.(data.id)
      await loadHistory(data.id)
    },
    onError: () => {
      toast.error('Failed to start session')
      setConnectionStatus('disconnected')
    },
  })

  const connectWebSocket = useCallback((sid: string) => {
    const websocket = new WebSocket(buildSessionWsUrl(sid))
    const wasFreshConnect = reconnectAttemptsRef.current === 0

    websocket.onopen = () => {
      reconnectAttemptsRef.current = 0
      setReconnectStalled(false)
      setConnectionStatus('connected')
      setWsConnected(true)
      if (wasFreshConnect) {
        toast.success('Connected to avatar!', { icon: '✨' })
      }
      if (voiceId) {
        websocket.send(JSON.stringify({ type: 'set_voice', voice_id: voiceId }))
      }
    }
    websocket.onmessage = (event) => {
      handleWebSocketMessage(JSON.parse(event.data))
    }
    websocket.onerror = () => {
      setConnectionStatus('disconnected')
      setWsConnected(false)
    }
    websocket.onclose = (event) => {
      setConnectionStatus('disconnected')
      setWsConnected(false)

      // 4401 = backend rejected the handshake (no token, bad token, or not the
      // session owner). No point reconnecting — surface a clear error instead.
      if (event.code === WS_AUTH_REJECT_CODE) {
        toast.error('Not authorised to join this session — please sign in again.')
        setReconnectStalled(true)
        return
      }

      const sidNow = sessionIdRef.current
      if (!sidNow) return
      if (reconnectAttemptsRef.current >= MAX_WS_RECONNECT_ATTEMPTS) {
        toast.error('Lost connection to avatar.')
        setReconnectStalled(true)
        return
      }
      const delay = Math.min(1000 * 2 ** reconnectAttemptsRef.current, 30000)
      reconnectAttemptsRef.current += 1
      reconnectTimerRef.current = setTimeout(() => connectWebSocket(sidNow), delay)
    }

    wsRef.current = websocket
    setWs(websocket)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voiceId, setWsConnected])

  const handleWebSocketMessage = useCallback((data: WsMessage) => {
    switch (data.type) {
      // Live token stream — accumulate into a streaming bubble
      case 'token':
        setStreamingContent(prev => prev + data.token)
        setIsTyping(false)
        break

      case 'transcription': {
        const text = data.text
        setMessages(prev => [...prev, {
          id: Date.now().toString(),
          role: 'user',
          content: text,
          timestamp: new Date(),
          emotion: detectEmotion(text),
        }])
        setStreamingContent('')
        setIsTyping(true)
        break
      }

      case 'message': {
        const content = data.content
        setStreamingContent('')
        setIsTyping(false)
        setMessages(prev => [...prev, {
          id: Date.now().toString(),
          role: 'assistant',
          content,
          timestamp: new Date(),
          emotion: detectEmotion(content),
        }])
        // Keep isProcessing=true — spinner stays until first video chunk arrives
        break
      }

      case 'video_chunk_start':
        chunkQueueRef.current = []
        setCurrentChunkProgress({ current: 0, total: data.total_chunks })
        break

      case 'video_chunk': {
        const chunk: VideoChunk = { url: data.video_url, text: data.text }
        chunkQueueRef.current.push(chunk)
        setCurrentChunkProgress(prev => ({ current: data.chunk_index + 1, total: prev.total }))
        // First chunk arriving → record latency, clear spinner, start playback
        if (!isPlayingRef.current) {
          if (sendTimeRef.current) setLatencyMs(Date.now() - sendTimeRef.current)
          setIsProcessing(false)
          playNextChunk()
        } else {
          // Already playing — preload this incoming chunk
          const upcoming = chunkQueueRef.current[0]
          if (upcoming && preloadVideoRef.current && preloadVideoRef.current.src !== upcoming.url) {
            preloadVideoRef.current.src = upcoming.url
          }
        }
        break
      }

      case 'video_chunk_end':
        // If nothing ever played (all chunks failed), clear spinner
        if (!isPlayingRef.current) setIsProcessing(false)
        break

      case 'status':
        // The backend sends an "Animating…" status before EVERY sentence
        // chunk, not just the first. Once a chunk has already started playing,
        // re-showing the full-screen spinner on every subsequent chunk just
        // covers the video that's still going — the user sees the spinner
        // "stuck" and never the avatar. Only block the view pre-first-chunk.
        if (!isPlayingRef.current) {
          setIsProcessing(true)
          setStatusMsg(data.message || 'Processing…')
        }
        break

      case 'error':
        toast.error(data.message)
        setIsProcessing(false)
        setIsTyping(false)
        break

      case 'tts_fallback':
        // Emitted once per turn when Chatterbox fails and we fell back to
        // gTTS — voice cloning is silently lost in that case, so warn the
        // user so they know the avatar's voice isn't what they expect.
        toast(data.message, { icon: '⚠️', duration: 5000 })
        break

      case 'interrupted':
        // Backend cancelled the previous turn because the user spoke again
        // (barge-in). Stop playback, clear the buffer, and let the new turn
        // start cleanly. We don't show a toast — barge-in should be silent.
        chunkQueueRef.current = []
        isPlayingRef.current = false
        setShowVideo(false)
        if (videoRef.current) {
          videoRef.current.pause()
          videoRef.current.src = ''
        }
        setIsProcessing(false)
        setIsTyping(false)
        setStreamingContent('')
        break

      case 'pong':
        break
    }
  }, [playNextChunk])

  const sendMessage = () => {
    if (!inputText.trim() || !ws || !sessionId) return
    if (ws.readyState !== WebSocket.OPEN) {
      // send() on a CONNECTING socket throws; on CLOSED it silently drops.
      toast.error('Not connected to the avatar yet — try again in a moment.')
      return
    }
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
    setStreamingContent('')
    setIsProcessing(true)
    setIsTyping(true)
    setLatencyMs(null)
    sendTimeRef.current = Date.now()
    chunkQueueRef.current = []
    isPlayingRef.current = false
    setShowVideo(false)
  }

  // Barge-in: tell the backend to cancel the in-flight turn and stop playback
  // locally right away (don't wait for the round-trip `interrupted` event).
  const stopGeneration = () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'stop' }))
    }
    chunkQueueRef.current = []
    isPlayingRef.current = false
    setShowVideo(false)
    if (videoRef.current) {
      videoRef.current.pause()
      videoRef.current.src = ''
    }
    setIsProcessing(false)
    setIsTyping(false)
    setStreamingContent('')
  }

  const exportConversation = () => {
    if (messages.length === 0) return
    const lines = messages.map(m =>
      `[${new Date(m.timestamp).toLocaleTimeString()}] ${m.role === 'user' ? 'You' : 'Avatar'}: ${m.content}`
    )
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `conversation-${new Date().toISOString().slice(0, 10)}.txt`
    a.click()
    URL.revokeObjectURL(url)
    toast.success('Conversation exported')
  }

  const changeLanguage = (lang: string) => {
    setLanguage(lang)
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'set_language', language: lang }))
    }
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
          if (ws && ws.readyState === WebSocket.OPEN) {
            setLatencyMs(null)
            sendTimeRef.current = Date.now()
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

  const startEditMessage = (m: Message) => {
    setEditingMessageId(m.id)
    setEditDraft(m.content)
  }

  const cancelEditMessage = () => {
    setEditingMessageId(null)
    setEditDraft('')
  }

  const saveEditMessage = async (id: string) => {
    const next = editDraft.trim()
    if (!next) {
      toast.error('Message cannot be empty')
      return
    }
    // Optimistic update — revert if the backend rejects
    const previous = messages
    setMessages(prev => prev.map(m => m.id === id ? { ...m, content: next, emotion: detectEmotion(next) } : m))
    setEditingMessageId(null)
    setEditDraft('')
    try {
      await api.editMessage(id, next)
      toast.success('Message updated', { duration: 1500 })
    } catch {
      setMessages(previous)
      toast.error('Could not update message')
    }
  }

  const deleteMessage = async (id: string) => {
    if (!window.confirm('Delete this message?')) return
    const previous = messages
    setMessages(prev => prev.filter(m => m.id !== id))
    try {
      await api.deleteMessage(id)
      toast.success('Message deleted', { duration: 1500 })
    } catch {
      setMessages(previous)
      toast.error('Could not delete message')
    }
  }

  /** Force an immediate reconnect, cancelling any pending backoff timer. */
  const manualReconnect = useCallback(() => {
    const sid = sessionIdRef.current
    if (!sid) return
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
    reconnectAttemptsRef.current = 0
    setReconnectStalled(false)
    setConnectionStatus('connecting')
    connectWebSocket(sid)
  }, [connectWebSocket])

  // Global keyboard shortcuts — only active while the chat view is mounted.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // ? opens the shortcut cheat-sheet (matches GitHub / Linear convention)
      if (e.key === '?' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        // Don't intercept when typing into an input/textarea
        const t = e.target as HTMLElement | null
        const tag = t?.tagName?.toLowerCase()
        if (tag === 'input' || tag === 'textarea' || t?.isContentEditable) return
        e.preventDefault()
        setShowShortcuts(s => !s)
        return
      }
      // Esc dismisses the modal
      if (e.key === 'Escape' && showShortcuts) {
        setShowShortcuts(false)
      }
      // Cmd/Ctrl+E toggles mute
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'e') {
        e.preventDefault()
        setIsMuted(m => !m)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [showShortcuts])

  useEffect(() => {
    // Resume an existing conversation if asked (history "Open"), otherwise
    // create a fresh session.
    if (resumeSessionId) {
      adoptSession(resumeSessionId)
    } else {
      createSessionMutation.mutate()
    }
    return () => {
      // Cancel any pending reconnect
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
      // Read the session id BEFORE nulling the ref (which stops the
      // reconnect loop). Don't use the `sessionId`/`ws` state here: this
      // cleanup closes over the first render where both were still null,
      // which used to leak the open socket and never end created sessions.
      const sid = sessionIdRef.current
      sessionIdRef.current = null
      wsRef.current?.close()
      wsRef.current = null
      setWsConnected(false)

      // Release media resources so navigating away mid-playback/recording
      // doesn't leak: a still-playing <video>, an open AudioContext, and a
      // running rAF loop all survive unmount otherwise.
      const video = videoRef.current
      if (video) { video.pause(); video.removeAttribute('src'); video.load() }
      if (levelAnimRef.current !== null) cancelAnimationFrame(levelAnimRef.current)
      if (audioContextRef.current && audioContextRef.current.state !== 'closed') {
        audioContextRef.current.close().catch(() => {})
      }

      // Only end sessions WE created — leaving a resumed conversation should
      // not mark the user's existing session as ended.
      if (sid && createdHereRef.current) api.endSession(sid).catch(() => {})
    }
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
            {/* Hidden preload video — buffers the next chunk while current plays */}
            <video ref={preloadVideoRef} className="hidden" preload="auto" muted />

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
              {reconnectStalled && (
                <button
                  onClick={manualReconnect}
                  className="flex items-center gap-1 text-xs text-primary-300 hover:text-white
                             px-2 py-1 rounded-md border border-primary-500/40 hover:bg-primary-500/20
                             transition-colors"
                  title="Reconnect"
                  aria-label="Reconnect to avatar"
                >
                  <Plug size={11} />
                  Reconnect
                </button>
              )}
            </div>

            <div className="flex items-center gap-2">
              {/* Language picker */}
              <div className="flex items-center gap-1 px-2 py-1 rounded-lg bg-surface-700/60 border border-white/10">
                <Globe size={11} className="text-gray-500" />
                <select
                  value={language}
                  onChange={(e) => changeLanguage(e.target.value)}
                  className="bg-transparent text-xs text-gray-300 focus:outline-none cursor-pointer"
                  title="TTS language"
                >
                  {CHAT_LANGUAGES.map(l => (
                    <option key={l.code} value={l.code}>{l.label}</option>
                  ))}
                </select>
              </div>

              {(showVideo || isProcessing) && (
                <button onClick={resetVideo} className="btn-icon" title="Reset video" aria-label="Reset video">
                  <RotateCcw size={15} />
                </button>
              )}
              <button
                onClick={() => setIsMuted(m => !m)}
                className={`btn-icon ${isMuted ? 'text-red-400 border-red-500/30' : ''}`}
                title={isMuted ? 'Unmute (⌘E)' : 'Mute (⌘E)'}
                aria-label={isMuted ? 'Unmute avatar' : 'Mute avatar'}
                aria-pressed={isMuted}
              >
                {isMuted ? <VolumeX size={15} /> : <Volume2 size={15} />}
              </button>
              <button
                onClick={() => setShowShortcuts(true)}
                className="btn-icon"
                title="Keyboard shortcuts (?)"
                aria-label="Show keyboard shortcuts"
              >
                <Keyboard size={15} />
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
          <div className="flex items-center gap-2">
            {latencyMs !== null && (
              <div className="flex items-center gap-1 text-xs text-primary-400">
                <Zap size={11} />
                <span>{(latencyMs / 1000).toFixed(1)}s</span>
              </div>
            )}
            <div className="flex items-center gap-1.5 text-xs text-gray-500">
              <Zap size={12} className="text-primary-400" />
              <span>{messages.length} messages</span>
            </div>
            {messages.length > 0 && (
              <button
                onClick={exportConversation}
                className="btn-icon"
                title="Export conversation"
                aria-label="Export conversation as text"
              >
                <Download size={13} />
              </button>
            )}
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
                    {editingMessageId === message.id ? (
                      // Inline editor — Enter saves, Esc cancels, Shift+Enter newline
                      <div className="w-full flex flex-col gap-1.5">
                        <textarea
                          autoFocus
                          value={editDraft}
                          onChange={(e) => setEditDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                              e.preventDefault()
                              saveEditMessage(message.id)
                            }
                            if (e.key === 'Escape') {
                              e.preventDefault()
                              cancelEditMessage()
                            }
                          }}
                          maxLength={8000}
                          rows={3}
                          className="w-full px-3 py-2 rounded-2xl bg-surface-700/80 border border-primary-500/40
                                     text-white text-sm placeholder:text-gray-600 focus:outline-none
                                     focus:ring-2 focus:ring-primary-500/50 resize-none"
                          aria-label="Edit message"
                        />
                        <div className="flex items-center gap-1.5 justify-end">
                          <button
                            onClick={cancelEditMessage}
                            className="text-xs text-gray-400 hover:text-white px-2 py-1 rounded-md hover:bg-white/5"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={() => saveEditMessage(message.id)}
                            className="text-xs text-white bg-primary-600 hover:bg-primary-500 px-2.5 py-1 rounded-md
                                       flex items-center gap-1"
                          >
                            <Check size={11} />
                            Save
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className={`relative px-4 py-2.5 rounded-2xl text-sm leading-relaxed
                        ${isUser
                          ? 'bg-gradient-to-br from-primary-700/80 to-accent-700/60 text-white rounded-tr-sm'
                          : 'bg-surface-700/80 border border-white/8 text-gray-200 rounded-tl-sm'
                        }`}
                      >
                        {message.content}
                        {/* Hover action menu — only on persisted messages (have a real DB id) */}
                        <div className={`absolute -top-2 ${isUser ? '-left-2' : '-right-2'} flex items-center gap-1
                                         opacity-0 group-hover:opacity-100 transition-opacity`}>
                          <button
                            onClick={() => copyMessage(message.content)}
                            className="w-6 h-6 rounded-full bg-surface-600 border border-white/10
                                       flex items-center justify-center hover:bg-surface-500"
                            title="Copy"
                            aria-label="Copy message"
                          >
                            <Copy size={10} className="text-gray-400" />
                          </button>
                          {message.persisted && (
                            <>
                              <button
                                onClick={() => startEditMessage(message)}
                                className="w-6 h-6 rounded-full bg-surface-600 border border-white/10
                                           flex items-center justify-center hover:bg-surface-500"
                                title="Edit"
                                aria-label="Edit message"
                              >
                                <Pencil size={10} className="text-gray-400" />
                              </button>
                              <button
                                onClick={() => deleteMessage(message.id)}
                                className="w-6 h-6 rounded-full bg-surface-600 border border-white/10
                                           flex items-center justify-center hover:bg-red-600/30"
                                title="Delete"
                                aria-label="Delete message"
                              >
                                <Trash2 size={10} className="text-gray-400" />
                              </button>
                            </>
                          )}
                        </div>
                      </div>
                    )}
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
          {/* Live streaming bubble — shows tokens as they arrive */}
          {streamingContent && (
            <div className="flex gap-2.5 animate-slide-up">
              <div className="w-7 h-7 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-bold
                              bg-gradient-to-br from-primary-600 to-primary-800">
                AI
              </div>
              <div className="max-w-[85%] flex flex-col gap-1 items-start">
                <div className="relative px-4 py-2.5 rounded-2xl rounded-tl-sm text-sm leading-relaxed
                                bg-surface-700/80 border border-primary-500/30 text-gray-200">
                  {streamingContent}
                  <span className="inline-block w-1.5 h-4 bg-primary-400 ml-0.5 align-middle animate-pulse rounded-sm" />
                </div>
              </div>
            </div>
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
              aria-label={isRecording ? 'Stop recording' : 'Start voice recording'}
              aria-pressed={isRecording}
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
                aria-label="Message your avatar"
                disabled={isProcessing || isRecording}
                rows={1}
                className="w-full px-4 py-2.5 rounded-xl bg-surface-700/80 border border-white/10 text-white text-sm
                           placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-primary-500/50
                           focus:border-primary-500/40 resize-none transition-all duration-200 disabled:opacity-50
                           [field-sizing:content] max-h-32 overflow-y-auto"
              />
            </div>

            {(isProcessing || isSpeaking) ? (
              <button
                onClick={stopGeneration}
                aria-label="Stop generating"
                title="Stop"
                className="flex-shrink-0 w-10 h-10 rounded-xl bg-red-600 hover:bg-red-500
                           flex items-center justify-center text-white shadow-[0_0_18px_rgba(239,68,68,0.4)]
                           transition-all duration-200 active:scale-95"
              >
                <Square size={16} fill="currentColor" />
              </button>
            ) : (
              <button
                onClick={sendMessage}
                disabled={!inputText.trim() || isRecording}
                aria-label="Send message"
                className="flex-shrink-0 w-10 h-10 rounded-xl bg-gradient-to-br from-primary-600 to-accent-600
                           flex items-center justify-center text-white hover:shadow-glow
                           disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-200 active:scale-95"
              >
                <Send size={18} />
              </button>
            )}
          </div>

          <p className="text-xs text-gray-600 text-center mt-2">
            Shift+Enter for new line · Mic for voice · <kbd className="px-1 py-0.5 rounded bg-surface-700 text-gray-500">?</kbd> for shortcuts
          </p>
        </div>
      </div>

      {/* ── Keyboard shortcuts modal ─────────────────────────────────────── */}
      {showShortcuts && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-surface-950/80 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-labelledby="kbd-title"
          onClick={() => setShowShortcuts(false)}
        >
          <div
            className="w-full max-w-md mx-4 glass-card rounded-2xl p-6 animate-scale-in"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <Keyboard size={18} className="text-primary-400" />
                <h2 id="kbd-title" className="text-lg font-bold text-white">Keyboard shortcuts</h2>
              </div>
              <button
                onClick={() => setShowShortcuts(false)}
                className="btn-icon"
                aria-label="Close shortcuts"
              >
                <X size={14} />
              </button>
            </div>
            <div className="divider mb-4" />
            <ul className="space-y-2.5 text-sm">
              {[
                { keys: ['Enter'], desc: 'Send message' },
                { keys: ['Shift', 'Enter'], desc: 'New line in message' },
                { keys: ['Esc'], desc: 'Cancel editing a message' },
                { keys: ['⌘', 'E'], desc: 'Mute / unmute avatar' },
                { keys: ['?'], desc: 'Toggle this shortcuts panel' },
              ].map(({ keys, desc }) => (
                <li key={desc} className="flex items-center justify-between gap-3">
                  <span className="text-gray-300">{desc}</span>
                  <span className="flex items-center gap-1">
                    {keys.map((k, i) => (
                      <kbd
                        key={i}
                        className="px-2 py-0.5 rounded-md bg-surface-700 border border-white/10 text-xs text-gray-300 font-mono"
                      >
                        {k}
                      </kbd>
                    ))}
                  </span>
                </li>
              ))}
            </ul>
            <p className="text-xs text-gray-500 mt-4">
              On Windows/Linux, use <kbd className="px-1 py-0.5 rounded bg-surface-700">Ctrl</kbd> in place of <kbd className="px-1 py-0.5 rounded bg-surface-700">⌘</kbd>.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
