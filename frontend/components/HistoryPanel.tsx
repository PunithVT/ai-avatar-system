'use client'

import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Clock, MessageCircle, Trash2, Play, RefreshCw, Search, Loader2,
  Download, Pencil, Check, X, Sparkles,
} from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ChatMessage, SessionSummary, Avatar } from '@/lib/types'

interface HistoryPanelProps {
  /** Called when the user clicks "Open" — receives the avatar to resume against. */
  onResume: (avatarId: string, sessionId: string) => void
}

interface ConversationSummary {
  id: string
  session_id: string
  title: string | null
  summary?: string | null
  message_count: number
  created_at: string
}

function timeAgo(iso: string): string {
  const then = new Date(iso).getTime()
  const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (diffSec < 60) return `${diffSec}s ago`
  const m = Math.floor(diffSec / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 30) return `${d}d ago`
  return new Date(iso).toLocaleDateString()
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export function HistoryPanel({ onResume }: HistoryPanelProps) {
  const queryClient = useQueryClient()
  const [query, setQuery] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [messagesById, setMessagesById] = useState<Record<string, ChatMessage[]>>({})
  const [loadingMessagesId, setLoadingMessagesId] = useState<string | null>(null)
  const [renameTarget, setRenameTarget] = useState<{ convId: string; sessionId: string } | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [summarizingId, setSummarizingId] = useState<string | null>(null)

  const { data: sessions, isLoading, refetch } = useQuery<SessionSummary[]>({
    queryKey: ['sessions'],
    queryFn: api.getSessions,
    refetchOnWindowFocus: false,
  })

  const { data: avatars } = useQuery<Avatar[]>({
    queryKey: ['avatars'],
    queryFn: api.getAvatars,
    refetchOnWindowFocus: false,
  })

  const { data: conversations } = useQuery<ConversationSummary[]>({
    queryKey: ['conversations'],
    queryFn: api.listConversations,
    refetchOnWindowFocus: false,
  })

  const avatarMap = useMemo(() => {
    const m: Record<string, Avatar> = {}
    for (const a of avatars || []) m[a.id] = a
    return m
  }, [avatars])

  const convoBySession = useMemo(() => {
    const m: Record<string, ConversationSummary> = {}
    for (const c of conversations || []) {
      // If a session has multiple conversations, keep the most recent (first because backend sorts desc)
      if (!m[c.session_id]) m[c.session_id] = c
    }
    return m
  }, [conversations])

  const filtered = useMemo(() => {
    const list = sessions || []
    if (!query.trim()) return list
    const q = query.toLowerCase()
    return list.filter(s => {
      const av = avatarMap[s.avatar_id]
      const convo = convoBySession[s.id]
      const hay = `${av?.name || ''} ${convo?.title || ''} ${s.id}`.toLowerCase()
      return hay.includes(q)
    })
  }, [sessions, avatarMap, convoBySession, query])

  const toggleExpand = async (sessionId: string) => {
    if (expandedId === sessionId) {
      setExpandedId(null)
      return
    }
    setExpandedId(sessionId)
    if (!messagesById[sessionId]) {
      setLoadingMessagesId(sessionId)
      try {
        const msgs = await api.getMessages(sessionId)
        setMessagesById(prev => ({ ...prev, [sessionId]: msgs }))
      } catch {
        toast.error('Could not load messages')
      } finally {
        setLoadingMessagesId(null)
      }
    }
  }

  const handleDelete = async (sessionId: string) => {
    if (!window.confirm('Delete this conversation? This cannot be undone.')) return
    setBusy(sessionId)
    try {
      await api.deleteSession(sessionId)
      toast.success('Conversation deleted')
      setMessagesById(prev => {
        const next = { ...prev }
        delete next[sessionId]
        return next
      })
      if (expandedId === sessionId) setExpandedId(null)
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    } catch {
      toast.error('Could not delete conversation')
    } finally {
      setBusy(null)
    }
  }

  const handleExport = async (sessionId: string) => {
    setBusy(sessionId)
    try {
      const blob = await api.exportSession(sessionId)
      downloadBlob(blob, `session-${sessionId.slice(0, 8)}.json`)
      toast.success('Exported')
    } catch {
      toast.error('Could not export conversation')
    } finally {
      setBusy(null)
    }
  }

  const handleSummarize = async (convId: string) => {
    setSummarizingId(convId)
    try {
      await api.summarizeConversation(convId)
      toast.success('Summary generated', { icon: '✨' })
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    } catch {
      toast.error('Could not summarize — backend or LLM unavailable')
    } finally {
      setSummarizingId(null)
    }
  }

  const handleStartRename = (convId: string, sessionId: string, currentTitle: string | null) => {
    setRenameTarget({ convId, sessionId })
    setRenameValue(currentTitle || '')
  }

  const handleSaveRename = async () => {
    if (!renameTarget) return
    const title = renameValue.trim()
    if (!title) {
      toast.error('Title cannot be empty')
      return
    }
    setBusy(renameTarget.sessionId)
    try {
      await api.renameConversation(renameTarget.convId, title)
      toast.success('Renamed')
      setRenameTarget(null)
      setRenameValue('')
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    } catch {
      toast.error('Could not rename')
    } finally {
      setBusy(null)
    }
  }

  // Periodically refresh
  useEffect(() => {
    const t = setInterval(() => refetch(), 30000)
    return () => clearInterval(t)
  }, [refetch])

  return (
    <div className="max-w-4xl mx-auto px-6 py-10 animate-fade-in">
      <div className="mb-8 flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-black text-gray-900 mb-2">Conversation History</h1>
          <p className="text-gray-500">Re-open, review, export, and clean up your past sessions.</p>
        </div>
        <button onClick={() => refetch()} className="btn-icon" title="Refresh" aria-label="Refresh">
          <RefreshCw size={15} />
        </button>
      </div>

      <div className="relative mb-6">
        <Search size={15} className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-500" />
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by avatar name, conversation title, or session id…"
          className="input-field pl-11"
          aria-label="Search conversations"
        />
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 size={28} className="animate-spin text-primary-400" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="card flex flex-col items-center justify-center py-16 gap-4 text-center">
          <div className="w-16 h-16 rounded-2xl bg-gray-100 flex items-center justify-center border border-gray-200">
            <MessageCircle size={28} className="text-gray-500" />
          </div>
          <div>
            <p className="text-gray-900 font-medium">No conversations yet</p>
            <p className="text-gray-500 text-sm mt-1">
              {query ? 'Nothing matches that search.' : 'Start a chat with an avatar to see it here.'}
            </p>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((s) => {
            const av = avatarMap[s.avatar_id]
            const convo = convoBySession[s.id]
            const isExpanded = expandedId === s.id
            const msgs = messagesById[s.id]
            const isRenaming = renameTarget?.sessionId === s.id
            const isBusy = busy === s.id
            const title = convo?.title || av?.name || 'Untitled conversation'
            return (
              <div key={s.id} className="card rounded-2xl overflow-hidden border border-gray-200">
                <div className="flex items-center gap-4 px-5 py-4">
                  <div className="w-12 h-12 rounded-xl overflow-hidden bg-gray-100 flex-shrink-0 flex items-center justify-center">
                    {av?.thumbnail_url || av?.image_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={av.thumbnail_url || av.image_url}
                        alt={av.name}
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <MessageCircle size={20} className="text-gray-500" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    {isRenaming ? (
                      <div className="flex items-center gap-2">
                        <input
                          autoFocus
                          type="text"
                          value={renameValue}
                          onChange={(e) => setRenameValue(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') handleSaveRename()
                            if (e.key === 'Escape') { setRenameTarget(null); setRenameValue('') }
                          }}
                          maxLength={200}
                          className="input-field py-1.5 text-sm"
                          aria-label="Conversation title"
                        />
                        <button
                          onClick={handleSaveRename}
                          className="btn-icon text-success-600"
                          aria-label="Save title"
                          disabled={isBusy}
                        >
                          {isBusy ? <Loader2 size={13} className="animate-spin" /> : <Check size={14} />}
                        </button>
                        <button
                          onClick={() => { setRenameTarget(null); setRenameValue('') }}
                          className="btn-icon"
                          aria-label="Cancel rename"
                        >
                          <X size={14} />
                        </button>
                      </div>
                    ) : (
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-gray-900 truncate">{title}</span>
                        <span className={`badge text-xs ${
 s.status === 'active' ? 'badge-green' :
 s.status === 'paused' ? 'badge-amber' :
 'badge-gray'
 }`}>
                          {s.status}
                        </span>
                      </div>
                    )}
                    <div className="flex items-center gap-2 mt-1 text-xs text-gray-500">
                      <Clock size={11} />
                      <span>{timeAgo(s.started_at)}</span>
                      {av && <><span>·</span><span>{av.name}</span></>}
                      {convo && <><span>·</span><span>{convo.message_count} msgs</span></>}
                      <span>·</span>
                      <span className="font-mono">{s.id.slice(0, 8)}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    {convo && !isRenaming && (
                      <button
                        onClick={() => handleStartRename(convo.id, s.id, convo.title)}
                        className="btn-icon"
                        title="Rename conversation"
                        aria-label="Rename conversation"
                      >
                        <Pencil size={13} />
                      </button>
                    )}
                    {convo && !isRenaming && (
                      <button
                        onClick={() => handleSummarize(convo.id)}
                        className="btn-icon"
                        title={convo.summary ? 'Regenerate AI summary' : 'Generate AI summary'}
                        aria-label="Summarize conversation with AI"
                        disabled={summarizingId === convo.id}
                      >
                        {summarizingId === convo.id ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
                      </button>
                    )}
                    <button
                      onClick={() => handleExport(s.id)}
                      className="btn-icon"
                      title="Export as JSON"
                      aria-label="Export conversation"
                      disabled={isBusy}
                    >
                      {isBusy ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
                    </button>
                    <button
                      onClick={() => toggleExpand(s.id)}
                      className="btn-icon"
                      title="Preview messages"
                      aria-label="Toggle message preview"
                      aria-expanded={isExpanded}
                    >
                      {loadingMessagesId === s.id ? <Loader2 size={13} className="animate-spin" /> : <MessageCircle size={13} />}
                    </button>
                    {av && (
                      <button
                        onClick={() => onResume(s.avatar_id, s.id)}
                        className="btn-primary text-xs px-3 py-1.5 rounded-lg"
                        title="Open in chat"
                      >
                        <Play size={12} />
                        Open
                      </button>
                    )}
                    <button
                      onClick={() => handleDelete(s.id)}
                      className="btn-icon text-gray-500 hover:text-error-600"
                      title="Delete conversation"
                      aria-label="Delete conversation"
                      disabled={isBusy}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>

                {convo?.summary && !isExpanded && (
                  <div className="border-t border-gray-200 px-5 py-3 bg-primary-500/5 flex items-start gap-2">
                    <Sparkles size={12} className="text-primary-400 mt-0.5 flex-shrink-0" />
                    <p className="text-xs text-gray-700 leading-relaxed">{convo.summary}</p>
                  </div>
                )}
                {isExpanded && (
                  <div className="border-t border-gray-200 px-5 py-4 bg-white">
                    {convo?.summary && (
                      <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-lg bg-primary-500/10 border border-primary-500/20">
                        <Sparkles size={12} className="text-primary-400 mt-0.5 flex-shrink-0" />
                        <p className="text-xs text-gray-700 leading-relaxed">{convo.summary}</p>
                      </div>
                    )}
                    {!msgs ? (
                      <p className="text-sm text-gray-500">Loading…</p>
                    ) : msgs.length === 0 ? (
                      <p className="text-sm text-gray-500">No messages in this session.</p>
                    ) : (
                      <div className="space-y-2 max-h-72 overflow-y-auto messages-scroll">
                        {msgs.map((m) => (
                          <div key={m.id} className="flex gap-2 text-sm">
                            <span className={`font-mono text-xs px-1.5 py-0.5 rounded ${
 m.role === 'user' ? 'bg-primary-700/40 text-primary-200' : 'bg-primary-700/40 text-primary-200'
 }`}>
                              {m.role === 'user' ? 'YOU' : 'AI'}
                            </span>
                            <span className="text-gray-800 flex-1 leading-relaxed whitespace-pre-wrap">{m.content}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
