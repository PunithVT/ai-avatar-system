export interface Avatar {
  id: string
  name: string
  status: 'ready' | 'processing' | 'failed' | 'pending'
  thumbnail_url?: string
  image_url?: string
  s3_key?: string
  avatar_metadata?: {
    system_prompt?: string
  }
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
}

export type WsMessageType =
  | 'token'
  | 'transcription'
  | 'message'
  | 'video_chunk_start'
  | 'video_chunk'
  | 'video_chunk_end'
  | 'status'
  | 'error'

export interface WsMessage {
  type: WsMessageType
  token?: string
  text?: string
  content?: string
  total_chunks?: number
  chunk_index?: number
  video_url?: string
  message?: string
  stage?: string
}

export interface VoiceApiResponse {
  id: string
  name: string
  language: string
  duration: number
  created_at?: string
}

export interface ApiError {
  response?: {
    data?: {
      detail?: string
    }
  }
  message?: string
}
