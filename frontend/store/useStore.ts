import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface User {
  id: string
  email: string
  username: string
  full_name?: string
}

interface AppState {
  // Auth
  token: string | null
  user: User | null
  setAuth: (token: string, user: User) => void
  clearAuth: () => void
  isAuthenticated: () => boolean

  // Theme
  theme: 'light' | 'dark'
  toggleTheme: () => void

  // Session
  activeSessionId: string | null
  selectedAvatarId: string | null
  setActiveSession: (sessionId: string | null) => void
  setSelectedAvatar: (avatarId: string | null) => void

  // WebSocket
  wsConnected: boolean
  setWsConnected: (connected: boolean) => void
}

export const useStore = create<AppState>()(
  persist(
    (set, get) => ({
      // Auth
      token: null,
      user: null,
      setAuth: (token, user) => set({ token, user }),
      clearAuth: () => set({ token: null, user: null, activeSessionId: null }),
      isAuthenticated: () => get().token !== null,

      // Theme
      theme: 'light',
      toggleTheme: () =>
        set((state) => ({
          theme: state.theme === 'light' ? 'dark' : 'light',
        })),

      // Session
      activeSessionId: null,
      selectedAvatarId: null,
      setActiveSession: (sessionId) => set({ activeSessionId: sessionId }),
      setSelectedAvatar: (avatarId) => set({ selectedAvatarId: avatarId }),

      // WebSocket
      wsConnected: false,
      setWsConnected: (connected) => set({ wsConnected: connected }),
    }),
    {
      name: 'avatar-system-storage',
      partialize: (state) => ({
        token: state.token,
        user: state.user,
        theme: state.theme,
        selectedAvatarId: state.selectedAvatarId,
      }),
    }
  )
)
