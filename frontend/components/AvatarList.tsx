'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trash2, Check, User, Loader2, RefreshCw, Play, Settings2, Save, X, Mic2, MicOff, CloudOff } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import Image from 'next/image'
import type { Avatar } from '@/lib/types'

interface AvatarListProps {
  selectedAvatar: string | null
  onSelectAvatar: (avatarId: string) => void
}

const STATUS_CONFIG: Record<string, { label: string; color: string; dot: string }> = {
  ready:      { label: 'Ready',      color: 'text-success-600',  dot: 'bg-success-500' },
  processing: { label: 'Processing', color: 'text-warning-600',  dot: 'bg-warning-500 animate-pulse' },
  failed:     { label: 'Failed',     color: 'text-error-600',    dot: 'bg-error-400' },
  pending:    { label: 'Pending',    color: 'text-gray-500',   dot: 'bg-gray-500' },
}

function AvatarCardSkeleton() {
  return (
    <div className="card overflow-hidden p-0">
      <div className="aspect-square skeleton" />
      <div className="p-3 space-y-2">
        <div className="h-4 skeleton rounded w-3/4" />
        <div className="h-3 skeleton rounded w-1/2" />
      </div>
    </div>
  )
}

export function AvatarList({ selectedAvatar, onSelectAvatar }: AvatarListProps) {
  const queryClient = useQueryClient()
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draftPrompt, setDraftPrompt] = useState('')
  const [draftName, setDraftName] = useState('')
  const [isSaving, setIsSaving] = useState(false)

  const { data: avatars, isLoading, isError, refetch } = useQuery<Avatar[]>({
    queryKey: ['avatars'],
    queryFn: api.getAvatars,
    // Poll only while something is still being processed. Polling every
    // five seconds forever kept hitting the API from an idle tab.
    refetchInterval: (query) =>
      query.state.data?.some((a) => a.status === 'processing' || a.status === 'pending') ? 5000 : false,
  })

  const deleteMutation = useMutation({
    mutationFn: (avatarId: string) => api.deleteAvatar(avatarId),
    onSuccess: () => {
      toast.success('Avatar deleted')
      queryClient.invalidateQueries({ queryKey: ['avatars'] })
    },
    onError: () => toast.error('Failed to delete avatar'),
  })

  const unsetVoiceMutation = useMutation({
    mutationFn: (avatarId: string) => api.unsetAvatarVoice(avatarId),
    onSuccess: () => {
      toast.success('Voice unassigned')
      queryClient.invalidateQueries({ queryKey: ['avatars'] })
    },
    onError: () => toast.error('Failed to unassign voice'),
  })

  const openEditor = (avatar: Avatar) => {
    setDraftPrompt(avatar.avatar_metadata?.system_prompt ?? '')
    setDraftName(avatar.name ?? '')
    setEditingId(avatar.id)
  }

  const savePrompt = async (avatarId: string) => {
    setIsSaving(true)
    try {
      const av = avatars?.find((a: Avatar) => a.id === avatarId)
      const saves: Promise<void>[] = [
        api.setAvatarMetadata(avatarId, { system_prompt: draftPrompt.trim() }),
      ]
      if (draftName.trim() && draftName.trim() !== av?.name) {
        saves.push(api.renameAvatar(avatarId, draftName.trim()))
      }
      await Promise.all(saves)
      queryClient.invalidateQueries({ queryKey: ['avatars'] })
      toast.success('Saved', { icon: '✅' })
      setEditingId(null)
    } catch {
      toast.error('Failed to save')
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <div className="card flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold text-gray-900">Your Avatars</h2>
          <p className="text-sm text-gray-500 mt-0.5">
            {avatars?.length ?? 0} avatar{(avatars?.length ?? 0) !== 1 ? 's' : ''}
          </p>
        </div>
        <button onClick={() => refetch()} className="btn-icon" title="Refresh" aria-label="Refresh avatars">
          <RefreshCw size={15} />
        </button>
      </div>

      <div className="divider" />

      {isLoading ? (
        <div className="grid grid-cols-2 gap-4">
          {Array.from({ length: 4 }).map((_, i) => <AvatarCardSkeleton key={i} />)}
        </div>
      ) : isError && !avatars ? (
        // A failed request is not an empty library. Showing "No avatars yet"
        // here told people their uploads were gone when the API was down.
        <div className="flex flex-col items-center justify-center gap-4 py-16 text-center" role="alert">
          <div className="flex h-16 w-16 items-center justify-center rounded-xl border border-error-600/20 bg-error-50">
            <CloudOff size={28} className="text-error-600" aria-hidden="true" />
          </div>
          <div>
            <p className="font-medium text-gray-900">Couldn&apos;t load your avatars</p>
            <p className="mt-1 text-sm text-gray-500">The server didn&apos;t respond. Check that it is running, then retry.</p>
          </div>
          <button onClick={() => refetch()} className="btn-secondary">
            <RefreshCw size={14} aria-hidden="true" />
            Retry
          </button>
        </div>
      ) : !avatars || avatars.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 gap-4 text-center">
          <div className="w-16 h-16 rounded-2xl bg-gray-100 flex items-center justify-center border border-gray-200">
            <User size={28} className="text-gray-500" />
          </div>
          <div>
            <p className="text-gray-900 font-medium">No avatars yet</p>
            <p className="text-gray-500 text-sm mt-1">Upload your first avatar to get started</p>
          </div>
        </div>
      ) : (
        <div className="space-y-3 overflow-y-auto max-h-[28rem] messages-scroll">
          <div className="grid grid-cols-2 gap-4">
            {avatars.map((avatar: Avatar) => {
              const isSelected = selectedAvatar === avatar.id
              const status = STATUS_CONFIG[avatar.status] ?? STATUS_CONFIG.pending

              return (
                <div
                  key={avatar.id}
                  className={`group relative overflow-hidden rounded-xl border bg-white transition-shadow duration-200
 ${isSelected
 ? 'border-primary-600 shadow-ring'
 : 'border-gray-200 shadow-xs hover:shadow-sm'
 }`}
                >
                  {/* Selection is a real button covering the card, so it is
                      reachable and operable from the keyboard. The actions
                      below sit above it (z-20) as siblings — a button cannot
                      legally contain other buttons. */}
                  <button
                    type="button"
                    onClick={() => onSelectAvatar(avatar.id)}
                    aria-pressed={isSelected}
                    aria-label={`Select avatar ${avatar.name}`}
                    className="absolute inset-0 z-10 rounded-xl focus-visible:ring-inset"
                  />
                  {/* Image */}
                  <div className="aspect-square relative bg-gray-100 overflow-hidden">
                    {(avatar.thumbnail_url || avatar.image_url) ? (
                      <Image
                        src={(avatar.thumbnail_url || avatar.image_url) as string}
                        alt={avatar.name}
                        fill
                        sizes="(min-width: 1024px) 280px, 45vw"
                        className="object-cover"
                      />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center">
                        <User size={40} className="text-gray-500" />
                      </div>
                    )}

                    {isSelected && (
                      <div className="absolute right-2 top-2 flex h-6 w-6 animate-scale-in items-center justify-center rounded-full bg-primary-600 shadow-xs">
                        <Check size={14} className="text-white" aria-hidden="true" />
                      </div>
                    )}

                    {!isSelected && (
                      <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-200">
                        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-ink/70 backdrop-blur-sm">
                          <Play size={16} className="ml-0.5 text-white" aria-hidden="true" />
                        </div>
                      </div>
                    )}

                    {/* Delete button */}
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        if (window.confirm('Delete this avatar?')) {
                          deleteMutation.mutate(avatar.id)
                        }
                      }}
                      className="absolute left-2 top-2 z-20 flex h-7 w-7 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-600 opacity-0 shadow-xs
 transition-opacity duration-200 hover:bg-error-50 hover:text-error-700 focus-visible:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100"
                      title="Delete avatar"
                      aria-label={`Delete avatar ${avatar.name}`}
                    >
                      {deleteMutation.isPending && deleteMutation.variables === avatar.id ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        <Trash2 size={12} />
                      )}
                    </button>

                    {/* Settings button — only for ready avatars */}
                    {avatar.status === 'ready' && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          editingId === avatar.id ? setEditingId(null) : openEditor(avatar)
                        }}
                        className={`absolute bottom-2 right-2 z-20 flex h-7 w-7 items-center justify-center rounded-full border shadow-xs
 transition-opacity duration-200 focus-visible:opacity-100 [@media(hover:none)]:opacity-100
 ${editingId === avatar.id
 ? 'border-primary-600 bg-primary-600 text-white opacity-100'
 : 'border-gray-200 bg-white text-gray-600 opacity-0 hover:bg-gray-50 group-hover:opacity-100'
 }`}
                        title="Edit personality"
                        aria-label={`Edit personality of ${avatar.name}`}
                        aria-expanded={editingId === avatar.id}
                      >
                        <Settings2 size={12} />
                      </button>
                    )}
                  </div>

                  {/* Info */}
                  <div className="border-t border-gray-200 bg-white px-3 py-2.5">
                    <p className="font-semibold text-sm text-gray-900 truncate">{avatar.name}</p>
                    <div className="flex items-center gap-1.5 mt-1">
                      <span className={`w-1.5 h-1.5 rounded-full ${status.dot}`} />
                      <span className={`text-xs ${status.color}`}>{status.label}</span>
                      <div className="ml-auto flex items-center gap-1.5">
                        {avatar.avatar_metadata?.system_prompt && (
                          <span title="Custom personality" aria-label="Has custom personality" className="text-[10px]">🧠</span>
                        )}
                        {avatar.voice_id ? (
                          <button
                            onClick={(e) => {
                              e.stopPropagation()
                              if (window.confirm('Unassign the cloned voice from this avatar?')) {
                                unsetVoiceMutation.mutate(avatar.id)
                              }
                            }}
                            className="relative z-20 flex items-center gap-0.5 rounded-sm p-1 text-primary-700 transition-colors hover:text-error-700"
                            title="Voice attached — click to unassign"
                            aria-label="Unassign voice from this avatar"
                          >
                            <Mic2 size={11} />
                          </button>
                        ) : (
                          <span title="No custom voice" aria-label="No custom voice" className="text-[10px] text-gray-500 flex items-center gap-0.5">
                            <MicOff size={11} />
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>

          {/* ── Inline Personality Editor ── */}
          {editingId && (() => {
            const av = avatars.find((a: Avatar) => a.id === editingId)
            if (!av) return null
            return (
              <div className="card-tint animate-slide-up rounded-xl p-4">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <Settings2 size={14} className="text-primary-600" />
                    <span className="text-sm font-semibold text-gray-900">
                      Personality — <span className="text-primary-700">{av.name}</span>
                    </span>
                  </div>
                  <button onClick={() => setEditingId(null)} className="btn-icon h-8 w-8" aria-label="Close personality editor">
                    <X size={13} />
                  </button>
                </div>

                <div className="space-y-1.5 mb-3">
                  <label htmlFor="avatar-display-name" className="text-xs font-medium text-gray-600">Display name</label>
                  <input
                    id="avatar-display-name"
                    type="text"
                    value={draftName}
                    onChange={(e) => setDraftName(e.target.value)}
                    className="input-field"
                    placeholder="Avatar name"
                  />
                </div>

                <label htmlFor="avatar-system-prompt" className="mb-2 block text-xs text-gray-600">
                  System prompt sent to the LLM at the start of every conversation with this avatar.
                </label>

                <textarea
                  id="avatar-system-prompt"
                  value={draftPrompt}
                  onChange={(e) => setDraftPrompt(e.target.value)}
                  placeholder="You are a friendly assistant named Alex. Respond conversationally and keep answers concise…"
                  rows={4}
                  className="input-field resize-none"
                />

                <div className="flex items-center justify-between mt-3">
                  <span className="text-xs text-gray-500">{draftPrompt.length} chars</span>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setEditingId(null)}
                      className="btn-ghost text-sm px-3 py-1.5"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => savePrompt(editingId)}
                      disabled={isSaving}
                      className="btn-primary text-sm px-4 py-1.5 rounded-lg"
                    >
                      {isSaving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
                      Save
                    </button>
                  </div>
                </div>
              </div>
            )
          })()}
        </div>
      )}

      {selectedAvatar && (
        <div className="flex animate-slide-up items-center gap-2 rounded-md bg-primary-50 px-3 py-2 ring-1 ring-inset ring-primary-600/[.12]">
          <Check size={14} className="shrink-0 text-primary-700" aria-hidden="true" />
          <p className="text-sm text-primary-700">Avatar selected — go to Chat to start talking!</p>
        </div>
      )}
    </div>
  )
}
