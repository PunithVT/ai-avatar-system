import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface User {
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

  /**
   * False until the persisted state has been read back from localStorage.
   * The store starts empty on every load (see `skipHydration` below), so
   * "no token" before this flips means "not known yet", not "signed out" —
   * rendering the sign-in gate on that basis flashed it at every signed-in
   * user on every reload.
   */
  hasHydrated: boolean

  // Hands-free: mic stays open and turns are detected by voice activity
  // instead of tapping record. Persisted because it is a standing preference
  // about how someone talks to the avatar, not per-session state.
  handsFree: boolean
  setHandsFree: (on: boolean) => void

  // Whether the chat's WebSocket is open (nav status badge).
  wsConnected: boolean
  setWsConnected: (connected: boolean) => void
}

export const useStore = create<AppState>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      setAuth: (token, user) => set({ token, user }),
      clearAuth: () => set({ token: null, user: null }),

      hasHydrated: false,

      handsFree: false,
      setHandsFree: (on) => set({ handsFree: on }),

      wsConnected: false,
      setWsConnected: (connected) => set({ wsConnected: connected }),
    }),
    {
      name: 'avatar-system-storage',
      // Don't auto-rehydrate from localStorage during client module init —
      // that happens synchronously, BEFORE React's first client render, so
      // if a persisted value differs from the default used in the
      // server-rendered HTML, the client's first render mismatches it and
      // React throws "Hydration failed". Rehydrating manually after mount
      // (see QueryProvider) means the first client render matches the
      // server, and the persisted value applies in a normal update instead.
      skipHydration: true,
      partialize: (state) => ({
        token: state.token,
        user: state.user,
        handsFree: state.handsFree,
      }),
    }
  )
)
