export interface Avatar {
  id: string
  name: string
  status: 'ready' | 'processing' | 'failed' | 'pending'
  thumbnail_url?: string
  image_url?: string
  s3_key?: string
  voice_id?: string | null
  avatar_metadata?: {
    system_prompt?: string
    personality?: string
    background_color?: string
    animation_style?: string
  }
  created_at?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
}

export interface SessionSummary {
  id: string
  user_id: string
  avatar_id: string
  status: 'active' | 'paused' | 'ended'
  started_at: string
  ended_at?: string | null
}

// Discriminated union — each WS event has a well-typed payload so the handler
// can rely on field presence without optional-chaining everywhere.
export type WsMessage =
  | { type: 'token'; token: string }
  | { type: 'transcription'; text: string }
  | { type: 'message'; role: 'assistant'; content: string }
  | { type: 'video_chunk_start'; total_chunks: number }
  | { type: 'video_chunk'; chunk_index: number; total_chunks: number; video_url: string; text: string }
  | { type: 'video_chunk_end'; sent_chunks: number }
  | { type: 'status'; message: string; stage?: string }
  // `code` is set for machine-readable failures, e.g. 'rate_limited'.
  | { type: 'error'; message: string; code?: string }
  | { type: 'pong' }
  // First frame after a successful connect. Its arrival (like any frame's) is
  // what marks the connection as established; it carries nothing.
  | { type: 'ready' }
  // Server keepalive. Carries nothing; it only has to be accepted.
  | { type: 'ping' }
  | { type: 'tts_fallback'; engine: string; voice_cloned: boolean; message: string }
  | { type: 'interrupted'; message: string }

export interface VoiceApiResponse {
  id: string
  name: string
  language: string
  duration: number
  created_at?: string
}

export interface ApiError {
  response?: {
    status?: number
    data?: {
      // A string for errors the API raises itself; an ARRAY of
      // {loc, msg, …} for request-validation failures (HTTP 422). Never
      // render it directly — use errorMessage() from lib/errors.
      detail?: unknown
    }
  }
  message?: string
}
