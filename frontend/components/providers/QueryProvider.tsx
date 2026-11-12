'use client'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect, useState, type ReactNode } from 'react'
import { initWebVitals } from '@/lib/vitals'
import { useStore } from '@/store/useStore'

export function QueryProvider({ children }: { children: ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 60 * 1000,
        refetchOnWindowFocus: false,
      },
    },
  }))

  // Boot Core Web Vitals observers after hydration. Returning the teardown
  // matters under StrictMode, which mounts effects twice in development —
  // without it each metric would be observed and reported twice.
  useEffect(() => initWebVitals(), [])

  // useStore has skipHydration:true (see store/useStore.ts) — pull in the
  // persisted theme/auth/session state ourselves, but only after the first
  // client render has matched the server's, so there's no hydration mismatch.
  useEffect(() => {
    // rehydrate() is synchronous for localStorage but typed as possibly async.
    Promise.resolve(useStore.persist.rehydrate()).then(() => {
      useStore.setState({ hasHydrated: true })
    })
  }, [])

  return (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  )
}
