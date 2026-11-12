'use client'

import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react'
import { Activity, Download, MessageCircle, Zap } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'
import { useChatSocket } from '@/hooks/useChatSocket'
import { useChatSession } from '@/hooks/useChatSession'
import { useAvatarPlayer } from '@/hooks/useAvatarPlayer'
import { usePushToTalk } from '@/hooks/usePushToTalk'
import { useHandsFree } from '@/hooks/useHandsFree'
import { isNewSentence } from '@/lib/chat/avatarTransport'
import { ReplyGate, initialTurnState, turnReducer } from '@/lib/chat/turnState'
import {
  EMOTION_CONFIG, detectEmotion, fromServer, liveMessage, reconcilePersisted, type Message,
} from '@/lib/chat/messages'
import { AvatarStage } from '@/components/chat/AvatarStage'
import { MessageList } from '@/components/chat/MessageList'
import { Composer } from '@/components/chat/Composer'
import { ShortcutsDialog } from '@/components/chat/ShortcutsDialog'
import type { Avatar, RtcSignal, WsMessage } from '@/lib/types'

interface ChatInterfaceProps {
  avatarId: string
  /** When set, resume this existing session (from history) instead of creating
   *  a fresh one — the backend rehydrates the prior messages on connect. */
  resumeSessionId?: string
  /**
   * Leave this conversation and begin a fresh one. Offered when the server
   * reports that the session no longer exists (deleted from another tab).
   */
  onStartNew?: () => void
}

/** Rows re-fetched before the known end when pairing live messages with ids. */
const RECONCILE_OVERLAP = 40

function isTextField(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  const tag = el?.tagName?.toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || Boolean(el?.isContentEditable)
}

/**
 * The live conversation view. This component only wires the pieces together;
 * each concern has one owner:
 *
 *   useChatSession  — create / resume / end the session
 *   useChatSocket   — the WebSocket and its reconnects
 *   useAvatarPlayer — everything the avatar plays, clips or live stream
 *   usePushToTalk / useHandsFree — the microphone
 *   turnReducer     — spinner, typing dots, streaming text, latency
 */
export function ChatInterface({ avatarId, resumeSessionId, onStartNew }: ChatInterfaceProps) {
  const handsFree = useStore((s) => s.handsFree)
  const setHandsFree = useStore((s) => s.setHandsFree)

  const [messages, setMessages] = useState<Message[]>([])
  const [inputText, setInputText] = useState('')
  const [language, setLanguage] = useState('en')
  const [showShortcuts, setShowShortcuts] = useState(false)
  const [sessionFailed, setSessionFailed] = useState(false)
  const [turn, dispatch] = useReducer(turnReducer, initialTurnState)

  const videoRef = useRef<HTMLVideoElement>(null)
  const preloadRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<HTMLVideoElement>(null)
  // WebRTC signalling rides the chat socket, which is created further down
  // (it needs the message handler, which needs the player).
  const sendSignalRef = useRef<(signal: RtcSignal) => boolean>(() => false)
  const sendSignal = useCallback((signal: RtcSignal) => sendSignalRef.current(signal), [])
  const player = useAvatarPlayer(videoRef, preloadRef, streamRef, sendSignal)

  // Mic level, written by the capture hooks and painted by the meter without
  // passing through React state.
  const levelRef = useRef(0)
  const sessionIdRef = useRef<string | null>(null)
  const languageRef = useRef('en')
  const sendTimeRef = useRef(0)
  // Synchronous mirror of turn.streamingContent, so the text streamed so far
  // can be committed from an event handler without waiting for a render.
  const streamingRef = useRef('')
  // Chunks are only accepted between a `video_chunk_start` and the turn being
  // stopped. A chunk already in flight when Stop is pressed would otherwise
  // start playing a reply the user just cancelled.
  const acceptChunksRef = useRef(false)
  const firstChunkSeenRef = useRef(false)
  // A new turn has been sent and its reply has not completed. The server's
  // `interrupted` for the PREVIOUS turn arrives in this window and must not
  // clear the new turn's spinner.
  const awaitingReplyRef = useRef(false)
  // Shut from the moment the user ends a reply until the server shows it has
  // caught up — see ReplyGate. Keeps the old reply's in-flight tokens out of
  // the transcript and out of the next reply.
  const replyGateRef = useRef<ReplyGate | null>(null)
  if (replyGateRef.current === null) replyGateRef.current = new ReplyGate()
  const replyGate = replyGateRef.current
  // How many of the session's rows the server is known to hold, so the
  // post-turn id lookup can ask for the tail instead of the whole history.
  const serverRowsRef = useRef(0)

  // ── Avatar still ─────────────────────────────────────────────────────────
  const { data: avatars, isError: avatarsFailed } = useQuery<Avatar[]>({
    queryKey: ['avatars'],
    queryFn: api.getAvatars,
  })
  const avatar = avatars?.find((a) => a.id === avatarId)
  const avatarImageUrl = avatar?.thumbnail_url || avatar?.image_url || null
  useEffect(() => {
    if (avatarsFailed) toast.error('Could not load avatar image')
  }, [avatarsFailed])

  // ── Ending a turn ────────────────────────────────────────────────────────

  /**
   * Keep whatever the avatar had said so far as a (truncated) message. The
   * server keeps the partial reply in history after an interrupt, so the
   * transcript should too.
   */
  const commitPartialReply = useCallback(() => {
    const partial = streamingRef.current.trim()
    streamingRef.current = ''
    if (partial) setMessages((prev) => [...prev, liveMessage('assistant', partial)])
  }, [])

  /** Stop the reply in flight: playback, queue, spinner. Safe to call twice. */
  const endTurnLocally = useCallback(() => {
    acceptChunksRef.current = false
    player.stop()
    commitPartialReply()
    dispatch({ type: 'reset' })
  }, [player, commitPartialReply])

  // ── Persisted ids for live messages ──────────────────────────────────────
  const reconcileTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scheduleReconcile = useCallback(() => {
    if (reconcileTimerRef.current) clearTimeout(reconcileTimerRef.current)
    reconcileTimerRef.current = setTimeout(async () => {
      const sid = sessionIdRef.current
      if (!sid) return
      try {
        // Only the tail: the rows being looked for are the newest ones. The
        // margin re-covers recent rows in case an earlier lookup ran before
        // the server had stored them.
        let skip = Math.max(0, serverRowsRef.current - RECONCILE_OVERLAP)
        let server = await api.getMessages(sid, skip)
        if (server.length === 0 && skip > 0) {
          // Fewer rows than expected (messages were deleted): start over.
          skip = 0
          server = await api.getMessages(sid, 0)
        }
        if (sessionIdRef.current !== sid) return
        serverRowsRef.current = skip + server.length
        setMessages((prev) => reconcilePersisted(prev, server))
      } catch {
        /* non-critical: messages simply stay without edit/delete for now */
      }
    }, 800)
  }, [])
  useEffect(() => () => {
    if (reconcileTimerRef.current) clearTimeout(reconcileTimerRef.current)
  }, [])

  // ── Server events ────────────────────────────────────────────────────────
  const handleMessage = (data: WsMessage) => {
    // Transport negotiation and the stream's `speaking` state. Before the
    // gate: none of it is reply text, and all of it must be seen.
    player.handleFrame(data)

    // Text from a reply the user has already stopped or talked over.
    if (!replyGate.admit(data)) return

    switch (data.type) {
      case 'token':
        streamingRef.current += data.token
        dispatch({ type: 'token', token: data.token })
        break

      case 'transcription':
        streamingRef.current = ''
        setMessages((prev) => [...prev, liveMessage('user', data.text)])
        dispatch({ type: 'transcription' })
        break

      case 'message':
        streamingRef.current = ''
        setMessages((prev) => [...prev, liveMessage('assistant', data.content)])
        dispatch({ type: 'assistant-message' })
        break

      case 'video_chunk_start':
        acceptChunksRef.current = true
        break

      case 'video_chunk': {
        if (!acceptChunksRef.current) break
        const startedPlayback = player.enqueue(data)
        if (startedPlayback) {
          // A re-sent chunk (the stream died before playing it) releases the
          // spinner like any other, but it is not the reply's first sentence
          // for the latency badge: that one was announced on the stream.
          const first = !firstChunkSeenRef.current && isNewSentence(data)
          if (isNewSentence(data)) firstChunkSeenRef.current = true
          dispatch({
            type: 'first-chunk',
            latencyMs: first && sendTimeRef.current ? Date.now() - sendTimeRef.current : turn.latencyMs,
          })
        }
        break
      }

      case 'video_chunk_end':
        awaitingReplyRef.current = false
        // Zero chunks (every one failed, or no avatar image) means nothing
        // will ever start playing — this is what releases the spinner.
        dispatch({ type: 'chunks-end', playing: player.isActive() })
        scheduleReconcile()
        break

      case 'status':
        dispatch({ type: 'status', message: data.message, playing: player.isActive() })
        break

      case 'error':
        awaitingReplyRef.current = false
        toast.error(data.message)
        commitPartialReply()
        dispatch({ type: 'error' })
        break

      case 'tts_fallback':
        // Voice cloning was silently lost for this reply — say so, once.
        toast(data.message, { icon: '⚠️', duration: 5000 })
        break

      case 'interrupted':
        // The server cancelled a turn — or, on the live stream, cut off
        // speech still playing after its turn had finished, so there may be
        // no turn in flight here at all; both branches are safe to run then.
        // If we have already sent the next one,
        // this is about the previous turn: stop its playback and keep its
        // partial text, but leave the new turn's spinner alone.
        if (awaitingReplyRef.current) {
          player.stop()
          commitPartialReply()
        } else {
          endTurnLocally()
        }
        break

      case 'pong':
      case 'ping':
      case 'ready':
      // Handled by the player, above.
      case 'webrtc_answer':
      case 'webrtc_error':
      case 'media_transport':
      case 'speaking':
        break
    }
  }

  const socket = useChatSocket({
    onMessage: handleMessage,
    // The server resets language to English on every connect, so a reconnect
    // would silently switch a Spanish conversation's STT and TTS back.
    onOpen: (send) => {
      send({ type: 'set_language', language: languageRef.current })
    },
    // The server cancels the turn when the socket drops and nothing is
    // resent, so a spinner left up here would never clear. Clips already
    // delivered keep playing — they are plain URLs and still valid. A live
    // stream does not survive: its peer connection belongs to this socket.
    onDrop: () => {
      player.handleSocketClosed()
      awaitingReplyRef.current = false
      replyGate.reset()
      commitPartialReply()
      dispatch({ type: 'reset' })
    },
  })
  const { send: socketSend } = socket
  useLayoutEffect(() => {
    sendSignalRef.current = socketSend
  }, [socketSend])

  // ── Session ──────────────────────────────────────────────────────────────
  const sessionId = useChatSession({
    avatarId,
    resumeSessionId,
    onReady: (sid) => {
      sessionIdRef.current = sid
      socket.connect(sid)
      // Restore the visible transcript (empty for a fresh session).
      api.getMessages(sid)
        .then((prev) => {
          if (sessionIdRef.current !== sid) return
          serverRowsRef.current = prev.length
          setMessages((current) => (current.length > 0 ? reconcilePersisted(current, prev) : fromServer(prev)))
        })
        .catch(() => { /* history is non-critical */ })
    },
    onFailed: () => {
      setSessionFailed(true)
      toast.error('Failed to start session')
    },
    onTeardown: () => {
      sessionIdRef.current = null
      // disconnect() closes silently (no onDrop), so say it here.
      player.handleSocketClosed()
      socket.disconnect()
    },
  })

  // ── Sending ──────────────────────────────────────────────────────────────

  /** Common to typed and spoken turns: clear the stage for the new reply. */
  const beginTurn = () => {
    // Stop, not just hide: hiding the element left the previous sentence's
    // audio playing under the idle image.
    acceptChunksRef.current = false
    player.stop()
    commitPartialReply()
    replyGate.close()
    firstChunkSeenRef.current = false
    awaitingReplyRef.current = true
    sendTimeRef.current = Date.now()
  }

  const sendText = () => {
    const text = inputText.trim()
    if (!text || !sessionId) return
    // The trimmed text, for both: the server stores it stripped, and the
    // bubble has to match what was stored or it can never be paired with its
    // database row (which is what gives it Edit and Delete).
    if (!socket.send({ type: 'text', text })) {
      toast.error('Not connected to the avatar yet — try again in a moment.')
      return
    }
    beginTurn()
    setMessages((prev) => [...prev, liveMessage('user', text)])
    setInputText('')
    dispatch({ type: 'text-sent' })
  }

  /**
   * Ship a captured utterance. Shared by push-to-talk and hands-free so the
   * two cannot drift on what "sending a turn" means.
   */
  const sendAudioBlob = (blob: Blob) => {
    if (blob.size === 0) return
    const reader = new FileReader()
    reader.onloadend = () => {
      const base64 = typeof reader.result === 'string' ? reader.result.split(',')[1] : ''
      if (!base64) return
      if (!socket.send({ type: 'audio', audio: base64 })) {
        // Previously dropped without a word, so the user waited on a reply
        // that was never requested.
        toast.error('Not connected — your recording was not sent.')
        return
      }
      beginTurn()
      dispatch({ type: 'audio-sent' })
    }
    reader.readAsDataURL(blob)
  }

  const stopGeneration = () => {
    socket.send({ type: 'stop' })
    awaitingReplyRef.current = false
    endTurnLocally()
    replyGate.close()
  }

  const changeLanguage = (code: string) => {
    setLanguage(code)
    languageRef.current = code
    socket.send({ type: 'set_language', language: code })
  }

  // ── Microphone ───────────────────────────────────────────────────────────
  const pushToTalk = usePushToTalk({ onBlob: sendAudioBlob, levelRef })
  const handsFreeMic = useHandsFree({
    enabled: handsFree,
    onBlob: sendAudioBlob,
    // Only called once the detector has confirmed real speech. Cutting the
    // avatar off on the first loud frame meant a cough cancelled the reply.
    onBargeIn: () => {
      if (!player.isActive()) return
      socket.send({ type: 'interrupt' })
      awaitingReplyRef.current = false
      endTurnLocally()
      replyGate.close()
    },
    isOutputActive: player.isActive,
    onFailed: () => setHandsFree(false),
    levelRef,
  })
  const isRecording = pushToTalk.isRecording || handsFreeMic.isCapturing

  // ── Transcript actions ───────────────────────────────────────────────────
  const copyMessage = useCallback((content: string) => {
    navigator.clipboard?.writeText(content).then(
      () => toast.success('Copied!', { duration: 1500 }),
      () => toast.error('Could not copy'),
    )
  }, [])

  // Both edit and delete update optimistically and, on failure, undo ONLY
  // their own change. Restoring a snapshot of the whole list would discard
  // every message that arrived while the request was in flight.
  const saveEditMessage = useCallback(async (id: string, next: string) => {
    let previous: string | null = null
    setMessages((prev) => prev.map((m) => {
      if (m.id !== id) return m
      previous = m.content
      return { ...m, content: next, emotion: detectEmotion(next) }
    }))
    try {
      await api.editMessage(id, next)
      toast.success('Message updated', { duration: 1500 })
    } catch {
      const restore = previous
      if (restore !== null) {
        setMessages((prev) => prev.map((m) => (
          m.id === id ? { ...m, content: restore, emotion: detectEmotion(restore) } : m
        )))
      }
      toast.error('Could not update message')
    }
  }, [])

  const deleteMessage = useCallback(async (id: string) => {
    if (!window.confirm('Delete this message?')) return
    let removed: { message: Message; index: number } | null = null
    setMessages((prev) => {
      const index = prev.findIndex((m) => m.id === id)
      if (index === -1) return prev
      removed = { message: prev[index], index }
      return prev.filter((m) => m.id !== id)
    })
    try {
      await api.deleteMessage(id)
      toast.success('Message deleted', { duration: 1500 })
    } catch {
      const undo = removed as { message: Message; index: number } | null
      if (undo) {
        setMessages((prev) => {
          const next = [...prev]
          next.splice(Math.min(undo.index, next.length), 0, undo.message)
          return next
        })
      }
      toast.error('Could not delete message')
    }
  }, [])

  const exportConversation = () => {
    if (messages.length === 0) return
    const lines = messages.map((m) =>
      `[${m.timestamp.toLocaleTimeString()}] ${m.role === 'user' ? 'You' : 'Avatar'}: ${m.content}`,
    )
    const url = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `conversation-${new Date().toISOString().slice(0, 10)}.txt`
    a.click()
    URL.revokeObjectURL(url)
    toast.success('Conversation exported')
  }

  // ── Keyboard shortcuts (only while the chat view is mounted) ─────────────
  const { muted, setMuted } = player
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Never steal keys from someone typing: Ctrl+E is end-of-line in a
      // macOS text field, and "?" is just a question mark.
      if (isTextField(e.target)) return
      if (e.key === '?' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        setShowShortcuts((open) => !open)
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'e') {
        e.preventDefault()
        setMuted(!muted)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [muted, setMuted])

  const closeShortcuts = useCallback(() => setShowShortcuts(false), [])

  // ── Render ───────────────────────────────────────────────────────────────
  const isBusy = turn.isProcessing || player.isPlaying
  const lastMessage = messages[messages.length - 1]
  const lastEmotion = EMOTION_CONFIG[lastMessage?.emotion || 'neutral'] ?? EMOTION_CONFIG.neutral
  const connectionStatus = sessionFailed ? 'disconnected' : socket.status

  // What a screen reader hears. One polite region carrying the current state
  // — announcing every streamed token would be unusable, so the reply is
  // read once, when it is complete.
  const announcement = connectionStatus !== 'connected'
    ? `Connection ${connectionStatus}`
    : turn.isProcessing
      ? turn.statusMsg
      : lastMessage?.role === 'assistant'
        ? `Avatar: ${lastMessage.content}`
        : ''

  return (
    <div className="grid grid-cols-1 gap-6 lg:h-[calc(100dvh-14rem)] lg:min-h-[32rem] lg:grid-cols-5">
      <div className="sr-only" role="status" aria-live="polite">{announcement}</div>

      {/* ── Avatar ── */}
      <div className="flex flex-col gap-4 lg:col-span-3">
        <AvatarStage
          videoRef={videoRef}
          preloadRef={preloadRef}
          avatarImageUrl={avatarImageUrl}
          streamRef={streamRef}
          showVideo={player.clipOnScreen}
          streamLive={player.streamOnScreen}
          isSpeaking={player.isPlaying}
          transport={player.transport}
          isProcessing={turn.isProcessing}
          statusMsg={turn.statusMsg}
          connectionStatus={connectionStatus}
          stalled={socket.stalled}
          onReconnect={socket.reconnect}
          onStartNew={onStartNew}
          audioBlocked={player.audioBlocked}
          onEnableAudio={() => setMuted(false)}
          language={language}
          onLanguageChange={changeLanguage}
          muted={muted}
          onToggleMute={() => setMuted(!muted)}
          onShowShortcuts={() => setShowShortcuts(true)}
        />

        {lastMessage && (
          <div className="card flex animate-slide-up items-center gap-3 px-4 py-3">
            <Activity size={14} className="shrink-0 text-gray-400" aria-hidden="true" />
            <span className="shrink-0 text-xs text-gray-500">Emotion detected:</span>
            <span className={lastEmotion.className}>{lastEmotion.label}</span>
          </div>
        )}
      </div>

      {/* ── Conversation ── */}
      <div className="card flex h-[70dvh] flex-col overflow-hidden rounded-2xl p-0 lg:col-span-2 lg:h-auto">
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3">
          <div className="flex items-center gap-2">
            <MessageCircle size={16} className="text-gray-400" aria-hidden="true" />
            <h2 className="text-base text-gray-900">Conversation</h2>
          </div>
          <div className="flex items-center gap-3">
            {turn.latencyMs !== null && (
              <div className="flex items-center gap-1 text-xs text-gray-500" title="Time from sending to the first video">
                <Zap size={11} aria-hidden="true" />
                <span className="tabular-nums">{(turn.latencyMs / 1000).toFixed(1)}s</span>
              </div>
            )}
            <span className="text-xs text-gray-500">
              {messages.length} message{messages.length === 1 ? '' : 's'}
            </span>
            {messages.length > 0 && (
              <button
                onClick={exportConversation}
                className="btn-icon h-8 w-8"
                title="Export conversation"
                aria-label="Export conversation as text"
              >
                <Download size={13} />
              </button>
            )}
          </div>
        </div>

        <MessageList
          messages={messages}
          streamingContent={turn.streamingContent}
          isTyping={turn.isTyping}
          onCopy={copyMessage}
          onSaveEdit={saveEditMessage}
          onDelete={deleteMessage}
        />

        <Composer
          value={inputText}
          onChange={setInputText}
          onSend={sendText}
          onStop={stopGeneration}
          isBusy={isBusy}
          isProcessing={turn.isProcessing}
          isRecording={isRecording}
          onToggleRecording={pushToTalk.isRecording ? pushToTalk.stop : pushToTalk.start}
          handsFree={handsFree}
          onToggleHandsFree={() => setHandsFree(!handsFree)}
          levelRef={levelRef}
        />
      </div>

      {showShortcuts && <ShortcutsDialog onClose={closeShortcuts} />}
    </div>
  )
}
