'use client'

import { Wifi, WifiOff } from 'lucide-react'
import { useStore } from '@/store/useStore'

export function ConnectionStatus() {
  const wsConnected = useStore((s) => s.wsConnected)

  return (
    <div className="flex items-center gap-2 text-sm">
      {wsConnected ? (
        <>
          <Wifi size={14} className="text-success-600" />
          <span className="text-success-600">Connected</span>
        </>
      ) : (
        <>
          <WifiOff size={14} className="text-error-600" />
          <span className="text-error-600">Disconnected</span>
        </>
      )}
    </div>
  )
}
