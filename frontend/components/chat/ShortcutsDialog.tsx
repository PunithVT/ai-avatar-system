'use client'

import { useEffect, useRef } from 'react'
import { Keyboard, X } from 'lucide-react'

const SHORTCUTS = [
  { keys: ['Enter'], desc: 'Send message' },
  { keys: ['Shift', 'Enter'], desc: 'New line in message' },
  { keys: ['Esc'], desc: 'Cancel editing a message' },
  { keys: ['⌘', 'E'], desc: 'Mute / unmute avatar' },
  { keys: ['?'], desc: 'Toggle this shortcuts panel' },
]

export function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null)

  // Move focus in on open, keep it inside while open, and hand it back to
  // whatever opened the dialog on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    closeRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
      // The close button is the only focusable element, so trapping focus
      // is just keeping Tab on it.
      if (e.key === 'Tab') {
        e.preventDefault()
        closeRef.current?.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      previous?.focus?.()
    }
  }, [onClose])

  return (
    <div className="scrim" role="dialog" aria-modal="true" aria-labelledby="kbd-title" onClick={onClose}>
      <div className="card mx-4 w-full max-w-md animate-scale-in rounded-2xl shadow-lg" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Keyboard size={18} className="text-primary-600" aria-hidden="true" />
            <h2 id="kbd-title" className="text-lg text-gray-900">Keyboard shortcuts</h2>
          </div>
          <button ref={closeRef} onClick={onClose} className="btn-icon" aria-label="Close shortcuts">
            <X size={14} />
          </button>
        </div>
        <div className="divider mb-4" />
        <ul className="space-y-2.5 text-sm">
          {SHORTCUTS.map(({ keys, desc }) => (
            <li key={desc} className="flex items-center justify-between gap-3">
              <span className="text-gray-700">{desc}</span>
              <span className="flex items-center gap-1">
                {keys.map((k) => (
                  <kbd key={k} className="rounded-sm border border-gray-200 bg-gray-100 px-2 py-0.5 font-mono text-xs text-gray-700">
                    {k}
                  </kbd>
                ))}
              </span>
            </li>
          ))}
        </ul>
        <p className="mt-4 text-xs text-gray-500">
          On Windows/Linux, use <kbd className="rounded-xs bg-gray-100 px-1 py-0.5">Ctrl</kbd> in place of{' '}
          <kbd className="rounded-xs bg-gray-100 px-1 py-0.5">⌘</kbd>.
        </p>
      </div>
    </div>
  )
}
