import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface User {
  id: string
  email: string
  username: string
  full_name?: string
  /**
   * Anonymous "Continue as Guest" account. Guests are real, isolated
   * server-side users with a throwaway identity, so this is the only
   * reliable way to tell one from a registered account — do not infer it
   * from the token or a magic user id.
   */
  is_guest?: boolean
}

interface AppState {
  // Auth
  token: string | null
  user: User | null
  setAuth: (token: string, user: User) => void
  clearAuth: () => void
  isAuthenticated: () => boolean

  // Hands-free: mic stays open and turns are detected by voice activity
  // instead of tapping record. Persisted because it is a standing preference
  // about how someone talks to the avatar, not per-session state.
  handsFree: boolean
  setHandsFree: (on: boolean) => void

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
      handsFree: false,
      setHandsFree: (on) => set({ handsFree: on }),


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
        handsFree: state.handsFree,
        selectedAvatarId: state.selectedAvatarId,
        activeSessionId: state.activeSessionId,
      }),
    }
  )
)
