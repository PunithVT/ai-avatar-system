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

  // Boot Core Web Vitals observers once after hydration.
  useEffect(() => { initWebVitals() }, [])

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
