'use client'

import type { RefObject } from 'react'
import { Globe, Keyboard, Plug, Video, Volume2, VolumeX, Wand2 } from 'lucide-react'
import type { SocketStatus, StallReason } from '@/lib/chat/sessionSocket'

export const CHAT_LANGUAGES = [
  { code: 'en', label: 'EN' }, { code: 'es', label: 'ES' }, { code: 'fr', label: 'FR' },
  { code: 'de', label: 'DE' }, { code: 'zh', label: 'ZH' }, { code: 'ja', label: 'JA' },
  { code: 'pt', label: 'PT' }, { code: 'hi', label: 'HI' }, { code: 'it', label: 'IT' },
  { code: 'ko', label: 'KO' },
]

/** The still shown whenever no clip is playing. */
function IdleAvatar({ imageUrl }: { imageUrl: string | null }) {
  if (!imageUrl) {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-4">
        <div className="flex h-20 w-20 animate-pulse-slow items-center justify-center rounded-full border border-gray-200 bg-white">
          <Video size={36} className="text-gray-400" aria-hidden="true" />
        </div>
        <p className="text-sm text-gray-500">Avatar video will appear here</p>
      </div>
    )
  }
  return (
    <div className="absolute inset-0 flex items-center justify-center bg-gray-25">
      {/* A plain <img>: the source is a user upload on the API host, already
          sized by the backend, so next/image's optimizer adds a hop for nothing. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={imageUrl} alt="Avatar, idle" className="relative z-10 h-full w-full object-cover" />
      <div className="absolute bottom-3 left-3 z-30 flex items-center gap-1.5 rounded-full border border-white/15 bg-ink/70 px-2.5 py-1 backdrop-blur-sm">
        <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-success-500" />
        <span className="font-mono text-eyebrow uppercase text-gray-25">Idle</span>
      </div>
    </div>
  )
}

interface Props {
  videoRef: RefObject<HTMLVideoElement | null>
  preloadRef: RefObject<HTMLVideoElement | null>
  avatarImageUrl: string | null
  /** A clip is on screen. */
  showVideo: boolean
  isProcessing: boolean
  statusMsg: string
  connectionStatus: SocketStatus
  stalled: StallReason | null
  onReconnect(): void
  /** Begin a fresh conversation — offered when this one no longer exists. */
  onStartNew?: () => void
  /** The browser refused to play sound without a tap; clips are playing muted. */
  audioBlocked?: boolean
  /** Called from the tap that restores sound. */
  onEnableAudio?: () => void
  language: string
  onLanguageChange(code: string): void
  muted: boolean
  onToggleMute(): void
  onShowShortcuts(): void
}

const STATUS_DOT: Record<SocketStatus, string> = {
  connected: 'online',
  connecting: 'processing',
  disconnected: 'offline',
}

/** The video panel: avatar, processing overlay and the controls under it. */
export function AvatarStage({
  videoRef, preloadRef, avatarImageUrl, showVideo, isProcessing, statusMsg,
  connectionStatus, stalled, onReconnect, onStartNew, audioBlocked, onEnableAudio,
  language, onLanguageChange, muted, onToggleMute, onShowShortcuts,
}: Props) {
  const isSpeaking = showVideo && !isProcessing

  return (
    <div className="card flex flex-col rounded-2xl p-4">
      <div className={`relative aspect-video w-full overflow-hidden rounded-xl bg-gray-25 ${isSpeaking ? 'speaking-ring' : ''}`}>
        {/* Idle still: always mounted, faded out while a clip plays. */}
        <div
          className="absolute inset-0 transition-opacity duration-500"
          style={{ opacity: showVideo ? 0 : 1, zIndex: showVideo ? 0 : 5 }}
        >
          <IdleAvatar imageUrl={avatarImageUrl} />
        </div>

        {/* Source, mute and playback are driven by useChunkPlayer — nothing
            here sets them, so the two cannot disagree. */}
        <video
          ref={videoRef}
          className="absolute inset-0 h-full w-full object-cover transition-opacity duration-500"
          style={{ opacity: showVideo ? 1 : 0, zIndex: showVideo ? 5 : 0 }}
          playsInline
          aria-label="Avatar video"
        />
        {/* Hidden element that buffers the next chunk while the current plays. */}
        <video ref={preloadRef} className="hidden" preload="auto" muted aria-hidden="true" />

        {/* Some browsers (Safari, iOS) pause a clip that is un-muted without a
            tap. The player falls back to playing it muted; this is the tap. */}
        {audioBlocked && showVideo && !isProcessing && (
          <button
            type="button"
            onClick={onEnableAudio}
            className="absolute bottom-3 left-1/2 z-20 flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-gray-900/85 px-3.5 py-2 text-xs font-medium text-white shadow-md transition-colors hover:bg-gray-900"
          >
            <VolumeX size={13} aria-hidden="true" />
            Tap to unmute
          </button>
        )}

        {isProcessing && (
          <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-4 bg-gray-25/85 backdrop-blur-sm">
            <div className="relative">
              <div className="h-16 w-16 animate-spin-slow rounded-full border-2 border-primary-600/20" />
              <div className="absolute inset-2 animate-spin rounded-full border-2 border-transparent border-t-primary-600" />
              <Wand2 className="absolute inset-0 m-auto text-primary-600" size={20} aria-hidden="true" />
            </div>
            <p className="text-sm font-medium text-gray-700">{statusMsg}</p>
          </div>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-2 px-1">
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5 text-xs">
            <span className={`status-dot ${STATUS_DOT[connectionStatus]}`} />
            <span className="capitalize text-gray-500">
              {stalled === 'replaced'
                ? 'Opened in another tab'
                : stalled === 'gone'
                  ? 'This conversation is no longer available'
                  : connectionStatus}
            </span>
          </div>
          {stalled === 'gone' && onStartNew && (
            <button
              onClick={onStartNew}
              className="flex items-center gap-1 rounded-md border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-700 shadow-xs transition-colors hover:bg-gray-50"
            >
              <Plug size={11} aria-hidden="true" />
              Start a new conversation
            </button>
          )}
          {stalled && stalled !== 'auth' && stalled !== 'gone' && (
            <button
              onClick={onReconnect}
              className="flex items-center gap-1 rounded-md border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-700 shadow-xs transition-colors hover:bg-gray-50"
              aria-label={stalled === 'replaced' ? 'Use this conversation here instead' : 'Reconnect to avatar'}
            >
              <Plug size={11} aria-hidden="true" />
              {stalled === 'replaced' ? 'Use here' : 'Reconnect'}
            </button>
          )}
        </div>

        <div className="flex items-center gap-2">
          <label className="flex h-10 items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 text-xs text-gray-600 shadow-xs">
            <Globe size={13} aria-hidden="true" />
            <span className="sr-only">Spoken language</span>
            <select
              value={language}
              onChange={(e) => onLanguageChange(e.target.value)}
              className="cursor-pointer bg-transparent text-xs text-gray-700 focus:outline-none"
            >
              {CHAT_LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>{l.label}</option>
              ))}
            </select>
          </label>

          <button
            onClick={onToggleMute}
            className={`btn-icon ${muted ? 'border-error-600/30 text-error-600' : ''}`}
            title={muted ? 'Unmute (⌘E)' : 'Mute (⌘E)'}
            aria-label={muted ? 'Unmute avatar' : 'Mute avatar'}
            aria-pressed={muted}
          >
            {muted ? <VolumeX size={15} /> : <Volume2 size={15} />}
          </button>
          <button
            onClick={onShowShortcuts}
            className="btn-icon"
            title="Keyboard shortcuts (?)"
            aria-label="Show keyboard shortcuts"
          >
            <Keyboard size={15} />
          </button>
        </div>
      </div>
    </div>
  )
}
