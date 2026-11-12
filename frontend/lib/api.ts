import axios, { AxiosError, InternalAxiosRequestConfig } from 'axios'
import { useStore } from '@/store/useStore'
import type { ChatMessage } from '@/lib/types'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

const STORAGE_KEY = 'avatar-system-storage'

function readToken(): string | null {
  if (typeof window === 'undefined') return null
  // The store is the source of truth once it has loaded. localStorage is
  // only consulted before that: child effects run before the provider's
  // rehydrate, so a request fired on mount can precede it.
  const state = useStore.getState()
  if (state.hasHydrated) return state.token
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (!stored) return null
    const { state } = JSON.parse(stored)
    const token = state?.token
    // Guests hold a real JWT now (POST /api/v1/users/guest), so there is no
    // longer a synthetic 'guest' sentinel to filter out. The old sentinel is
    // still discarded so a browser holding stale persisted state re-registers
    // as a guest instead of sending an unusable token forever.
    if (!token || token === 'guest') return null
    return token
  } catch {
    return null
  }
}

// Create axios instance with defaults
const apiClient = axios.create({
  baseURL: API_URL,
  timeout: 30000,
  // Send the httpOnly auth cookie the backend sets on login. Combined with
  // the bearer header (kept for cross-origin API use), this lets the browser
  // authenticate via the XSS-safe cookie. Requires the backend's CORS
  // allow_credentials=true + explicit origin (both already configured).
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Request interceptor — attach auth token
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = readToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

// Response interceptor — handle 401 globally
apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    // A 401 on a request that carried our token means the token is no longer
    // good (expired after 24 h, or a guest account that was reaped). Clear it
    // from the STORE: that is what the UI renders from, so the sign-in gate
    // appears. Removing only the localStorage entry, as this used to, left
    // the stale token in memory — the app kept showing empty lists and
    // "Failed…" toasts, and the next store write persisted the token back.
    //
    // A 401 without a token is just a failed login and changes nothing.
    if (error.response?.status === 401 && error.config?.headers?.Authorization) {
      useStore.getState().clearAuth()
    }
    return Promise.reject(error)
  }
)

export const api = {
  // Expose the token getter so non-axios code (WebSocket) can authenticate
  getToken: readToken,

  // Auth
  register: async (data: { email: string; username: string; password: string; full_name?: string }) => {
    const response = await apiClient.post('/api/v1/users/register', data)
    return response.data
  },

  login: async (email: string, password: string) => {
    const formData = new URLSearchParams()
    formData.append('username', email)
    formData.append('password', password)
    const response = await apiClient.post('/api/v1/users/login', formData, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    })
    return response.data
  },

  /**
   * Start an anonymous session. The backend creates a real (throwaway) user
   * row and returns its JWT, so a guest's avatars and transcripts are scoped
   * to them alone rather than pooled into one shared account.
   */
  createGuest: async () => {
    const response = await apiClient.post('/api/v1/users/guest')
    return response.data
  },

  logout: async () => {
    // Clears the server-set httpOnly cookie. Best-effort — ignore failures.
    try {
      await apiClient.post('/api/v1/users/logout')
    } catch {
      /* network error on logout is non-fatal */
    }
  },

  /**
   * `token` is passed explicitly right after login, before it is in the
   * store. Without it this call relied on the auth cookie alone, and where
   * the cookie is not sent (API on another site, third-party cookies
   * blocked) it 401'd — reported to the user as "Invalid credentials" for
   * credentials that had just been accepted.
   */
  getProfile: async (token?: string) => {
    const response = await apiClient.get(
      '/api/v1/users/me',
      token ? { headers: { Authorization: `Bearer ${token}` } } : undefined,
    )
    return response.data
  },

  /**
   * `current_password` is required by the server whenever a registered user
   * changes their password (403 "Current password is incorrect" otherwise).
   * Guests are exempt: setting an email and password is how a guest account
   * becomes a real one, and the response then carries `is_guest: false`.
   */
  updateProfile: async (data: {
    email?: string
    username?: string
    full_name?: string
    password?: string
    current_password?: string
  }) => {
    const response = await apiClient.put('/api/v1/users/me', data)
    return response.data
  },

  // Avatars
  uploadAvatar: async (formData: FormData) => {
    const response = await apiClient.post('/api/v1/avatars/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return response.data
  },

  getAvatars: async () => {
    const response = await apiClient.get('/api/v1/avatars/')
    return response.data
  },

  deleteAvatar: async (avatarId: string) => {
    const response = await apiClient.delete(`/api/v1/avatars/${avatarId}`)
    return response.data
  },

  setAvatarVoice: async (avatarId: string, voiceId: string) => {
    const response = await apiClient.put(`/api/v1/avatars/${avatarId}/voice?voice_id=${encodeURIComponent(voiceId)}`)
    return response.data
  },

  unsetAvatarVoice: async (avatarId: string) => {
    // Empty voice_id query → backend unassigns
    const response = await apiClient.put(`/api/v1/avatars/${avatarId}/voice`)
    return response.data
  },

  setAvatarMetadata: async (avatarId: string, metadata: Record<string, unknown>) => {
    const response = await apiClient.patch(`/api/v1/avatars/${avatarId}/metadata`, metadata)
    return response.data
  },

  renameAvatar: async (avatarId: string, name: string) => {
    const response = await apiClient.patch(`/api/v1/avatars/${avatarId}/name`, { name })
    return response.data
  },

  // Sessions
  createSession: async (avatarId: string) => {
    const response = await apiClient.post('/api/v1/sessions/create', {
      avatar_id: avatarId,
    })
    return response.data
  },

  getSessions: async () => {
    const response = await apiClient.get('/api/v1/sessions/')
    return response.data
  },

  endSession: async (sessionId: string) => {
    const response = await apiClient.post(`/api/v1/sessions/${sessionId}/end`)
    return response.data
  },

  deleteSession: async (sessionId: string) => {
    const response = await apiClient.delete(`/api/v1/sessions/${sessionId}`)
    return response.data
  },

  exportSession: async (sessionId: string) => {
    const response = await apiClient.get(`/api/v1/sessions/${sessionId}/export`, {
      responseType: 'blob',
    })
    return response.data as Blob
  },

  // Messages
  /**
   * A session's messages from row `skip` to the end, oldest first.
   *
   * The endpoint pages (500 rows at most, 100 by default) and returns the
   * OLDEST rows first, so a plain request stops being useful exactly when a
   * conversation gets long: past the first page the newest messages are
   * never in the response. That silently truncated restored conversations
   * and meant new messages in a long session never received their database
   * ids. This follows pages until a short one.
   */
  getMessages: async (sessionId: string, skip = 0): Promise<ChatMessage[]> => {
    const PAGE = 500
    const MAX_PAGES = 40 // 20,000 rows; a bound, not an expectation
    const all: ChatMessage[] = []
    for (let page = 0; page < MAX_PAGES; page++) {
      const response = await apiClient.get(`/api/v1/messages/session/${sessionId}`, {
        params: { skip: skip + all.length, limit: PAGE },
      })
      const rows: ChatMessage[] = Array.isArray(response.data) ? response.data : []
      all.push(...rows)
      if (rows.length < PAGE) break
    }
    return all
  },

  editMessage: async (messageId: string, content: string) => {
    const response = await apiClient.patch(`/api/v1/messages/${messageId}`, { content })
    return response.data
  },

  deleteMessage: async (messageId: string) => {
    const response = await apiClient.delete(`/api/v1/messages/${messageId}`)
    return response.data
  },

  // Voices
  listVoices: async () => {
    const response = await apiClient.get('/api/v1/voices/')
    return response.data
  },

  cloneVoice: async (audio: Blob, name: string, language: string = 'en') => {
    const formData = new FormData()
    formData.append('audio', audio, 'voice_sample.webm')
    formData.append('name', name)
    formData.append('language', language)
    const response = await apiClient.post('/api/v1/voices/clone', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return response.data
  },

  deleteVoice: async (voiceId: string) => {
    const response = await apiClient.delete(`/api/v1/voices/${voiceId}`)
    return response.data
  },

  // The preview is auth-protected and an <audio> tag cannot send headers, so
  // it is fetched as a blob and played from an object URL.
  fetchVoicePreviewBlob: async (voiceId: string) => {
    const response = await apiClient.get(`/api/v1/voices/${voiceId}/preview`, {
      responseType: 'blob',
    })
    return response.data as Blob
  },

  // Conversations
  listConversations: async () => {
    const response = await apiClient.get('/api/v1/conversations/')
    return response.data
  },

  renameConversation: async (conversationId: string, title: string) => {
    const response = await apiClient.patch(`/api/v1/conversations/${conversationId}/rename`, { title })
    return response.data
  },

  summarizeConversation: async (conversationId: string) => {
    const response = await apiClient.post(`/api/v1/conversations/${conversationId}/summarize`)
    return response.data
  },
}

/**
 * Build a WebSocket URL for a session, appending the JWT as a query parameter
 * (the WebSocket constructor does not let us attach an Authorization header).
 */
export function buildSessionWsUrl(sessionId: string): string {
  const rawUrl = process.env.NEXT_PUBLIC_WS_URL || process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
  const wsBase = rawUrl.replace(/^http/, 'ws')
  const token = readToken()
  const path = `${wsBase}/ws/session/${encodeURIComponent(sessionId)}`
  return token ? `${path}?token=${encodeURIComponent(token)}` : path
}

export { apiClient }
