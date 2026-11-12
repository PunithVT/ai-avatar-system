import type { ChatMessage } from '@/lib/types'

/** A message as the transcript renders it. */
export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  emotion?: string
  /**
   * The id is the real database id, so the message can be edited and deleted
   * through the API. Messages created live start without it and gain it when
   * `reconcilePersisted` matches them to the server's copy.
   */
  persisted?: boolean
}

export function detectEmotion(text: string): string {
  const lower = text.toLowerCase()
  if (/\b(haha|lol|funny|laugh|joke|hilarious)\b/.test(lower)) return 'happy'
  if (/\b(angry|mad|furious|annoyed|hate)\b/.test(lower)) return 'angry'
  if (/\b(sad|cry|miss|lonely|depressed|unhappy)\b/.test(lower)) return 'sad'
  if (/\b(wow|amazing|awesome|incredible|fantastic|great)\b/.test(lower)) return 'excited'
  if (/\b(think|wonder|curious|how|why|what|interesting)\b/.test(lower)) return 'curious'
  return 'neutral'
}

// Class names live here as whole literals so Tailwind's content scan (which
// includes lib/) keeps them.
export const EMOTION_CONFIG: Record<string, { label: string; className: string }> = {
  happy:   { label: '😄 Happy',   className: 'badge-yellow' },
  angry:   { label: '😠 Angry',   className: 'badge-red' },
  sad:     { label: '😢 Sad',     className: 'badge-brand' },
  excited: { label: '🤩 Excited', className: 'badge-brand' },
  curious: { label: '🤔 Curious', className: 'badge-brand' },
  neutral: { label: '😊 Neutral', className: 'badge-gray' },
}

let localSeq = 0

/**
 * Id for a message that exists only in this tab so far. A counter rather than
 * `Date.now()`: a transcription and the reply's first event can land in the
 * same millisecond, and two rows with one React key corrupt the list.
 */
export function localMessageId(): string {
  localSeq += 1
  return `local-${localSeq}`
}

export function liveMessage(role: Message['role'], content: string): Message {
  return {
    id: localMessageId(),
    role,
    content,
    timestamp: new Date(),
    emotion: detectEmotion(content),
  }
}

function isTranscriptRole(m: ChatMessage): m is ChatMessage & { role: 'user' | 'assistant' } {
  return m.role === 'user' || m.role === 'assistant'
}

export function fromServer(messages: ChatMessage[]): Message[] {
  return messages.filter(isTranscriptRole).map((m) => ({
    id: m.id,
    role: m.role,
    content: m.content,
    timestamp: new Date(m.created_at),
    emotion: detectEmotion(m.content),
    persisted: true,
  }))
}

/**
 * Give live messages their database ids once the server has stored them.
 *
 * Matching is by role and content (ignoring surrounding whitespace — the
 * server stores text stripped, while a streamed reply or a typed message may
 * not be), in order, against server rows not already claimed. `server` need
 * only be the tail of the conversation: rows before it are simply not
 * candidates. It only ever upgrades a local message in place — it never
 * drops or reorders one — because the server may not have persisted the
 * newest reply yet when this runs, and losing a visible message would be far
 * worse than leaving it without an edit button for one more turn.
 */
export function reconcilePersisted(local: Message[], server: ChatMessage[]): Message[] {
  const claimed = new Set(local.filter((m) => m.persisted).map((m) => m.id))
  const available = server.filter((m) => isTranscriptRole(m) && !claimed.has(m.id))
  let changed = false

  const next = local.map((m) => {
    if (m.persisted) return m
    const wanted = m.content.trim()
    const idx = available.findIndex((s) => s.role === m.role && s.content.trim() === wanted)
    if (idx === -1) return m
    const [match] = available.splice(idx, 1)
    changed = true
    return { ...m, id: match.id, persisted: true }
  })

  return changed ? next : local
}
