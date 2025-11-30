'use client'

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trash2, Check } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import Image from 'next/image'

interface AvatarListProps {
  selectedAvatar: string | null
  onSelectAvatar: (avatarId: string) => void
}

export function AvatarList({ selectedAvatar, onSelectAvatar }: AvatarListProps) {
  const queryClient = useQueryClient()

  const { data: avatars, isLoading } = useQuery({
    queryKey: ['avatars'],
    queryFn: api.getAvatars,
  })

  const deleteMutation = useMutation({
    mutationFn: (avatarId: string) => api.deleteAvatar(avatarId),
    onSuccess: () => {
      toast.success('Avatar deleted successfully')
      queryClient.invalidateQueries({ queryKey: ['avatars'] })
      if (selectedAvatar) {
        onSelectAvatar('')
      }
    },
    onError: () => {
      toast.error('Failed to delete avatar')
    },
  })

  if (isLoading) {
    return (
      <div className="card">
        <h2 className="text-2xl font-semibold mb-4">Your Avatars</h2>
        <div className="flex justify-center py-8">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-600"></div>
        </div>
      </div>
    )
  }

  return (
    <div className="card">
      <h2 className="text-2xl font-semibold mb-4">Your Avatars</h2>
      
      {!avatars || avatars.length === 0 ? (
        <div className="text-center py-8 text-gray-500">
          <p>No avatars yet. Upload your first avatar!</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4">
          {avatars.map((avatar: any) => (
            <div
              key={avatar.id}
              className={`relative rounded-lg border-2 overflow-hidden cursor-pointer transition-all ${
                selectedAvatar === avatar.id
                  ? 'border-primary-600 ring-2 ring-primary-200'
                  : 'border-gray-200 hover:border-primary-400'
              }`}
              onClick={() => onSelectAvatar(avatar.id)}
            >
              <div className="aspect-square relative">
                <Image
                  src={avatar.thumbnail_url || avatar.image_url}
                  alt={avatar.name}
                  fill
                  className="object-cover"
                />
                
                {selectedAvatar === avatar.id && (
                  <div className="absolute top-2 right-2 bg-primary-600 rounded-full p-1">
                    <Check size={16} className="text-white" />
                  </div>
                )}
                
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    if (window.confirm('Are you sure you want to delete this avatar?')) {
                      deleteMutation.mutate(avatar.id)
                    }
                  }}
                  className="absolute top-2 left-2 bg-red-600 hover:bg-red-700 rounded-full p-1 opacity-0 hover:opacity-100 transition-opacity"
                >
                  <Trash2 size={16} className="text-white" />
                </button>
              </div>
              
              <div className="p-3 bg-white">
                <p className="font-medium text-sm truncate">{avatar.name}</p>
                <p className="text-xs text-gray-500 capitalize">{avatar.status}</p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
