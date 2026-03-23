'use client'

import { Wifi, WifiOff } from 'lucide-react'
import { useStore } from '@/store/useStore'

export function ConnectionStatus() {
  const wsConnected = useStore((s) => s.wsConnected)

  return (
    <div className="flex items-center gap-2 text-sm">
      {wsConnected ? (
        <>
          <Wifi size={14} className="text-green-500" />
          <span className="text-green-600 dark:text-green-400">Connected</span>
        </>
      ) : (
        <>
          <WifiOff size={14} className="text-red-500" />
          <span className="text-red-600 dark:text-red-400">Disconnected</span>
        </>
      )}
    </div>
  )
}
