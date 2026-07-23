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

  // Theme — UI is dark-first; toggle just for the few light-mode users
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
      clearAuth: () =>
        set({
          token: null,
          user: null,
          activeSessionId: null,
          selectedAvatarId: null,
        }),
      isAuthenticated: () => get().token !== null,

      // Theme
      theme: 'dark',
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
      // Don't auto-rehydrate from localStorage during client module init —
      // that happens synchronously, BEFORE React's first client render, so
      // if a persisted value (e.g. theme) differs from the default used in
      // the server-rendered HTML, the client's first render mismatches it
      // and React throws "Hydration failed". Rehydrating manually after
      // mount (see Providers component) means the first client render
      // matches the server, and the persisted value applies in a normal
      // post-hydration update instead.
      skipHydration: true,
      partialize: (state) => ({
        token: state.token,
        user: state.user,
        theme: state.theme,
        selectedAvatarId: state.selectedAvatarId,
        activeSessionId: state.activeSessionId,
      }),
    }
  )
)
