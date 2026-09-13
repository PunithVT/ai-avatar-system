'use client'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { initWebVitals } from '@/lib/vitals'
import { useStore } from '@/store/useStore'

export function QueryProvider({ children }: { children: React.ReactNode }) {
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
  useEffect(() => { useStore.persist.rehydrate() }, [])

  return (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  )
}
