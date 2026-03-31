'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trash2, Check, User, Loader2, RefreshCw, Play, Settings2, Save, X } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import Image from 'next/image'

interface AvatarListProps {
  selectedAvatar: string | null
  onSelectAvatar: (avatarId: string) => void
}

const STATUS_CONFIG: Record<string, { label: string; color: string; dot: string }> = {
  ready:      { label: 'Ready',      color: 'text-green-400',  dot: 'bg-green-400' },
  processing: { label: 'Processing', color: 'text-amber-400',  dot: 'bg-amber-400 animate-pulse' },
  failed:     { label: 'Failed',     color: 'text-red-400',    dot: 'bg-red-400' },
  pending:    { label: 'Pending',    color: 'text-gray-400',   dot: 'bg-gray-500' },
}

function AvatarCardSkeleton() {
  return (
    <div className="glass-card rounded-xl overflow-hidden animate-pulse">
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

  const { data: avatars, isLoading, refetch } = useQuery({
    queryKey: ['avatars'],
    queryFn: api.getAvatars,
    refetchInterval: 5000,
  })

  const deleteMutation = useMutation({
    mutationFn: (avatarId: string) => api.deleteAvatar(avatarId),
    onSuccess: () => {
      toast.success('Avatar deleted')
      queryClient.invalidateQueries({ queryKey: ['avatars'] })
    },
    onError: () => toast.error('Failed to delete avatar'),
  })

  const openEditor = (avatar: any) => {
    setDraftPrompt(avatar.avatar_metadata?.system_prompt ?? '')
    setDraftName(avatar.name ?? '')
    setEditingId(avatar.id)
  }

  const savePrompt = async (avatarId: string) => {
    setIsSaving(true)
    try {
      const av = avatars?.find((a: any) => a.id === avatarId)
      const saves: Promise<any>[] = [
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
          <h2 className="text-xl font-bold text-white">Your Avatars</h2>
          <p className="text-sm text-gray-500 mt-0.5">
            {avatars?.length ?? 0} avatar{(avatars?.length ?? 0) !== 1 ? 's' : ''}
          </p>
        </div>
        <button onClick={() => refetch()} className="btn-icon" title="Refresh">
          <RefreshCw size={15} />
        </button>
      </div>

      <div className="divider" />

      {isLoading ? (
        <div className="grid grid-cols-2 gap-4">
          {Array.from({ length: 4 }).map((_, i) => <AvatarCardSkeleton key={i} />)}
        </div>
      ) : !avatars || avatars.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 gap-4 text-center">
          <div className="w-16 h-16 rounded-2xl bg-surface-700/80 flex items-center justify-center border border-white/8">
            <User size={28} className="text-gray-500" />
          </div>
          <div>
            <p className="text-white font-medium">No avatars yet</p>
            <p className="text-gray-500 text-sm mt-1">Upload your first avatar to get started</p>
          </div>
        </div>
      ) : (
        <div className="space-y-3 overflow-y-auto max-h-[28rem] messages-scroll">
          <div className="grid grid-cols-2 gap-4">
            {avatars.map((avatar: any, idx: number) => {
              const isSelected = selectedAvatar === avatar.id
              const status = STATUS_CONFIG[avatar.status] ?? STATUS_CONFIG.pending

              return (
                <div
                  key={avatar.id}
                  onClick={() => onSelectAvatar(avatar.id)}
                  className={`relative rounded-xl overflow-hidden cursor-pointer transition-all duration-300 group
                    ${isSelected
                      ? 'ring-2 ring-primary-500 ring-offset-2 ring-offset-surface-900 shadow-glow-sm scale-[1.02]'
                      : 'hover:scale-[1.02] hover:shadow-glow-sm hover:ring-1 hover:ring-primary-500/40'
                    }`}
                  style={{ animationDelay: `${idx * 0.05}s` }}
                >
                  {/* Image */}
                  <div className="aspect-square relative bg-surface-700 overflow-hidden">
                    {avatar.thumbnail_url || avatar.image_url ? (
                      <Image
                        src={avatar.thumbnail_url || avatar.image_url}
                        alt={avatar.name}
                        fill
                        className="object-cover transition-transform duration-500 group-hover:scale-110"
                      />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center">
                        <User size={40} className="text-gray-600" />
                      </div>
                    )}

                    <div className="absolute inset-0 bg-gradient-to-t from-surface-950/80 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-200" />

                    {isSelected && (
                      <div className="absolute top-2 right-2 w-6 h-6 rounded-full bg-primary-500 flex items-center justify-center shadow-glow-sm animate-scale-in">
                        <Check size={14} className="text-white" />
                      </div>
                    )}

                    {!isSelected && (
                      <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-200">
                        <div className="w-10 h-10 rounded-full bg-primary-600/80 backdrop-blur-sm flex items-center justify-center">
                          <Play size={16} className="text-white ml-0.5" />
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
                      className="absolute top-2 left-2 w-6 h-6 rounded-full bg-red-600/80 backdrop-blur-sm flex items-center justify-center
                                 opacity-0 group-hover:opacity-100 transition-opacity duration-200 hover:bg-red-500"
                      title="Delete avatar"
                    >
                      {deleteMutation.isPending ? (
                        <Loader2 size={11} className="text-white animate-spin" />
                      ) : (
                        <Trash2 size={11} className="text-white" />
                      )}
                    </button>

                    {/* Settings button — only for ready avatars */}
                    {avatar.status === 'ready' && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          editingId === avatar.id ? setEditingId(null) : openEditor(avatar)
                        }}
                        className={`absolute bottom-2 right-2 w-6 h-6 rounded-full backdrop-blur-sm flex items-center justify-center
                                   transition-all duration-200
                                   ${editingId === avatar.id
                                     ? 'bg-primary-600 opacity-100'
                                     : 'bg-surface-800/80 opacity-0 group-hover:opacity-100 hover:bg-primary-600/60'
                                   }`}
                        title="Edit personality"
                      >
                        <Settings2 size={11} className="text-white" />
                      </button>
                    )}
                  </div>

                  {/* Info */}
                  <div className="bg-surface-800/90 px-3 py-2.5 border-t border-white/8">
                    <p className="font-semibold text-sm text-white truncate">{avatar.name}</p>
                    <div className="flex items-center gap-1.5 mt-1">
                      <span className={`w-1.5 h-1.5 rounded-full ${status.dot}`} />
                      <span className={`text-xs ${status.color}`}>{status.label}</span>
                      {avatar.avatar_metadata?.system_prompt && (
                        <span className="text-[10px] text-primary-400 ml-auto">🧠</span>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>

          {/* ── Inline Personality Editor ── */}
          {editingId && (() => {
            const av = avatars.find((a: any) => a.id === editingId)
            if (!av) return null
            return (
              <div className="glass-card rounded-xl p-4 border border-primary-500/30 animate-slide-up">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <Settings2 size={14} className="text-primary-400" />
                    <span className="text-sm font-semibold text-white">
                      Personality — <span className="text-primary-400">{av.name}</span>
                    </span>
                  </div>
                  <button onClick={() => setEditingId(null)} className="btn-icon">
                    <X size={13} />
                  </button>
                </div>

                <div className="space-y-1.5 mb-3">
                  <label className="text-xs font-medium text-gray-400">Display name</label>
                  <input
                    type="text"
                    value={draftName}
                    onChange={(e) => setDraftName(e.target.value)}
                    className="w-full px-3 py-2 rounded-xl bg-surface-700/80 border border-white/10 text-white text-sm
                               placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-primary-500/50
                               focus:border-primary-500/40 transition-all duration-200"
                    placeholder="Avatar name"
                  />
                </div>

                <p className="text-xs text-gray-500 mb-2">
                  System prompt sent to the LLM at the start of every conversation with this avatar.
                </p>

                <textarea
                  value={draftPrompt}
                  onChange={(e) => setDraftPrompt(e.target.value)}
                  placeholder="You are a friendly assistant named Alex. Respond conversationally and keep answers concise…"
                  rows={4}
                  className="w-full px-3 py-2.5 rounded-xl bg-surface-700/80 border border-white/10 text-white text-sm
                             placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-primary-500/50
                             focus:border-primary-500/40 resize-none transition-all duration-200"
                />

                <div className="flex items-center justify-between mt-3">
                  <span className="text-xs text-gray-600">{draftPrompt.length} chars</span>
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
        <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-primary-500/10 border border-primary-500/20 animate-slide-up">
          <Check size={14} className="text-primary-400 flex-shrink-0" />
          <p className="text-sm text-primary-300">Avatar selected — go to Chat to start talking!</p>
        </div>
      )}
    </div>
  )
}
