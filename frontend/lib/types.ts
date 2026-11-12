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
  // One sentence of the reply. Delivered either as a clip to fetch
  // (`video_url`, no `transport`) or already on the WebRTC stream
  // (`transport: 'webrtc'`, no `video_url`). The server decides per clip.
  // `resend` (URL form only): this sentence was announced on the stream, the
  // stream went down before it had played, and it is being delivered again
  // as a clip under the SAME `chunk_index`. Play it; do not count it as new.
  | {
      type: 'video_chunk'
      chunk_index: number
      total_chunks: number
      video_url?: string
      text: string
      transport?: 'webrtc'
      duration_ms?: number
      resend?: boolean
    }
  | { type: 'video_chunk_end'; sent_chunks: number }
  | { type: 'status'; message: string; stage?: string }
  // `code` is set for machine-readable failures, e.g. 'rate_limited'.
  | { type: 'error'; message: string; code?: string }
  | { type: 'pong' }
  // First frame after a successful connect. Its arrival (like any frame's) is
  // what marks the connection as established. `media` is absent from servers
  // that predate the WebRTC transport; absent and `webrtc: false` mean the
  // same thing — clips only.
  | { type: 'ready'; media?: MediaCapabilities }
  // Server keepalive. Carries nothing; it only has to be accepted.
  | { type: 'ping' }
  | { type: 'tts_fallback'; engine: string; voice_cloned: boolean; message: string }
  | { type: 'interrupted'; message: string }
  // ── WebRTC signalling (only ever sent after a `webrtc_offer`) ──
  | { type: 'webrtc_answer'; sdp: string }
  | { type: 'webrtc_error'; message: string }
  // Which way the NEXT clips will arrive. 'webrtc' once the server's peer
  // connection is up; 'clips' when it failed, closed or was replaced.
  | { type: 'media_transport'; transport: 'webrtc' | 'clips' }
  // The stream went from idle to carrying speech, or back.
  | { type: 'speaking'; active: boolean }

export interface IceServer {
  urls: string | string[]
  username?: string
  credential?: string
}

export interface MediaCapabilities {
  webrtc: boolean
  ice_servers?: IceServer[]
}

/** Signalling frames the client sends. Full SDP both ways; no trickle ICE. */
export type RtcSignal =
  | { type: 'webrtc_offer'; sdp: string }
  // This side's peer connection is gone (closed, failed, or unreachable for
  // too long). The server stops using the stream and re-sends, as clips,
  // whatever it had put there that has not played. Sent promptly: the server
  // takes about 30 s to notice a dead media path by itself.
  | { type: 'webrtc_close' }

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
