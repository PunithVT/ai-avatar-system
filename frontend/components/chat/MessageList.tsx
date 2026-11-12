'use client'

import { memo, useEffect, useRef, useState } from 'react'
import { Check, Clock, Copy, Pencil, Sparkles, Trash2 } from 'lucide-react'
import { EMOTION_CONFIG, type Message } from '@/lib/chat/messages'

function Disc({ role }: { role: Message['role'] }) {
  // Ink for the person, the tinted brand surface for the avatar: the two
  // sides stay distinguishable without either being a painted accent block.
  return (
    <div
      aria-hidden="true"
      className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold
 ${role === 'user' ? 'bg-ink text-white' : 'bg-primary-50 text-primary-700 ring-1 ring-inset ring-primary-600/[.12]'}`}
    >
      {role === 'user' ? 'U' : 'AI'}
    </div>
  )
}

const ACTION_BUTTON =
  'flex h-6 w-6 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-500 shadow-xs hover:bg-gray-50 hover:text-gray-900'

interface BubbleProps {
  message: Message
  onCopy(content: string): void
  onSaveEdit(id: string, content: string): void
  onDelete(id: string): void
}

/**
 * One transcript row. Memoised, and owns its own edit draft, so a token
 * arriving for the live reply re-renders only the streaming bubble — not
 * every message above it.
 */
const MessageBubble = memo(function MessageBubble({ message, onCopy, onSaveEdit, onDelete }: BubbleProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const isUser = message.role === 'user'
  const emotion = EMOTION_CONFIG[message.emotion || 'neutral'] ?? EMOTION_CONFIG.neutral

  const startEdit = () => {
    setDraft(message.content)
    setEditing(true)
  }
  const save = () => {
    if (!draft.trim()) return
    onSaveEdit(message.id, draft.trim())
    setEditing(false)
  }

  return (
    <div className={`flex animate-slide-up gap-2.5 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
      <Disc role={message.role} />
      <div className={`group flex max-w-[85%] flex-col gap-1 ${isUser ? 'items-end' : 'items-start'}`}>
        {editing ? (
          // Enter saves, Esc cancels, Shift+Enter inserts a newline.
          <div className="flex w-full flex-col gap-1.5">
            <textarea
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  save()
                }
                if (e.key === 'Escape') {
                  e.preventDefault()
                  setEditing(false)
                }
              }}
              maxLength={8000}
              rows={3}
              className="input-field resize-none"
              aria-label="Edit message"
            />
            <div className="flex items-center justify-end gap-1.5">
              <button onClick={() => setEditing(false)} className="btn-ghost px-2 py-1 text-xs">Cancel</button>
              <button onClick={save} disabled={!draft.trim()} className="btn-primary px-2.5 py-1 text-xs">
                <Check size={11} aria-hidden="true" />
                Save
              </button>
            </div>
          </div>
        ) : (
          <div
            className={`relative whitespace-pre-wrap break-words rounded-xl px-4 py-2.5 text-sm leading-relaxed
 ${isUser
   ? 'rounded-tr-xs bg-primary-600 text-white'
   : 'rounded-tl-xs border border-gray-200 bg-gray-50 text-gray-800'}`}
          >
            {message.content}
            {/* Shown on hover AND on keyboard focus, and always on touch
                devices, where there is no hover to reveal it. */}
            <div
              className={`absolute -top-2 ${isUser ? '-left-2' : '-right-2'} flex items-center gap-1 opacity-0 transition-opacity
 focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100`}
            >
              <button onClick={() => onCopy(message.content)} className={ACTION_BUTTON} title="Copy" aria-label="Copy message">
                <Copy size={10} />
              </button>
              {/* Edit and delete need the database id. */}
              {message.persisted && (
                <>
                  <button onClick={startEdit} className={ACTION_BUTTON} title="Edit" aria-label="Edit message">
                    <Pencil size={10} />
                  </button>
                  <button
                    onClick={() => onDelete(message.id)}
                    className={`${ACTION_BUTTON} hover:text-error-600`}
                    title="Delete"
                    aria-label="Delete message"
                  >
                    <Trash2 size={10} />
                  </button>
                </>
              )}
            </div>
          </div>
        )}
        <div className={`flex items-center gap-1.5 px-1 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
          <Clock size={10} className="text-gray-400" aria-hidden="true" />
          <span className="text-xs text-gray-500">
            {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </span>
          {message.emotion && message.emotion !== 'neutral' && (
            <span className="text-xs" title={emotion.label}>{emotion.label.split(' ')[0]}</span>
          )}
        </div>
      </div>
    </div>
  )
})

function TypingIndicator() {
  return (
    <div className="flex animate-slide-up items-end gap-2.5">
      <Disc role="assistant" />
      <div className="rounded-xl rounded-bl-xs border border-gray-200 bg-gray-50 px-4 py-3">
        <div className="flex items-center gap-1" aria-hidden="true">
          {[0, 0.2, 0.4].map((delay) => (
            <div
              key={delay}
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400"
              style={{ animationDelay: `${delay}s` }}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

interface Props {
  messages: Message[]
  /** Tokens of the reply being generated, shown as a live bubble. */
  streamingContent: string
  isTyping: boolean
  /**
   * What the server has heard so far of an utterance still being spoken.
   * Provisional: not a message, and replaced by one when the turn is taken.
   */
  pendingUserText?: string | null
  onCopy(content: string): void
  onSaveEdit(id: string, content: string): void
  onDelete(id: string): void
}

export function MessageList({
  messages, streamingContent, isTyping, pendingUserText, onCopy, onSaveEdit, onDelete,
}: Props) {
  const endRef = useRef<HTMLDivElement>(null)

  // Follow the conversation, including while a reply streams in. Smooth
  // scrolling is for discrete events only: issuing one per token queues
  // overlapping animations and the view judders.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: streamingContent ? 'auto' : 'smooth', block: 'end' })
  }, [messages, isTyping, streamingContent, pendingUserText])

  return (
    <div className="messages-scroll flex-1 space-y-4 overflow-y-auto px-4 py-4">
      {messages.length === 0 && !streamingContent && !isTyping && !pendingUserText ? (
        <div className="flex h-full flex-col items-center justify-center gap-4 py-12 text-center">
          <div className="flex h-16 w-16 items-center justify-center rounded-xl border border-gray-200 bg-gray-50">
            <Sparkles size={28} className="text-gray-400" aria-hidden="true" />
          </div>
          <div>
            <p className="mb-1 font-medium text-gray-900">Start the conversation</p>
            <p className="text-sm text-gray-500">Type a message or press the mic button</p>
          </div>
        </div>
      ) : (
        messages.map((message) => (
          <MessageBubble
            key={message.id}
            message={message}
            onCopy={onCopy}
            onSaveEdit={onSaveEdit}
            onDelete={onDelete}
          />
        ))
      )}

      {pendingUserText && (
        // Outlined rather than filled, and without a timestamp or actions:
        // it reads as the user's side, but not yet as something that was said.
        <div className="flex flex-row-reverse gap-2.5">
          <Disc role="user" />
          <div className="flex max-w-[85%] flex-col items-end gap-1">
            <div className="whitespace-pre-wrap break-words rounded-xl rounded-tr-xs border border-dashed border-primary-300 bg-primary-50 px-4 py-2.5 text-sm italic leading-relaxed text-primary-700">
              {pendingUserText}
              <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse-soft rounded-xs bg-primary-600 align-middle" aria-hidden="true" />
            </div>
          </div>
        </div>
      )}

      {streamingContent && (
        <div className="flex gap-2.5">
          <Disc role="assistant" />
          <div className="flex max-w-[85%] flex-col items-start gap-1">
            <div className="whitespace-pre-wrap break-words rounded-xl rounded-tl-xs border border-gray-200 bg-gray-50 px-4 py-2.5 text-sm leading-relaxed text-gray-800">
              {streamingContent}
              <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse-soft rounded-xs bg-primary-600 align-middle" aria-hidden="true" />
            </div>
          </div>
        </div>
      )}
      {isTyping && <TypingIndicator />}
      <div ref={endRef} />
    </div>
  )
}
