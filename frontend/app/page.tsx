'use client'

import { useState } from 'react'
import { AvatarUpload } from '@/components/AvatarUpload'
import { AvatarList } from '@/components/AvatarList'
import { ChatInterface } from '@/components/ChatInterface'
import { Camera, MessageCircle } from 'lucide-react'

export default function Home() {
  const [selectedAvatar, setSelectedAvatar] = useState<string | null>(null)
  const [view, setView] = useState<'upload' | 'chat'>('upload')

  return (
    <main className="min-h-screen p-8">
      <div className="max-w-7xl mx-auto">
        <header className="mb-8">
          <h1 className="text-4xl font-bold text-gray-900 mb-2">
            AI Avatar System
          </h1>
          <p className="text-gray-600">
            Create and interact with your AI-powered avatar
          </p>
        </header>

        <div className="flex gap-4 mb-6">
          <button
            onClick={() => setView('upload')}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg ${
              view === 'upload' ? 'btn-primary' : 'btn-secondary'
            }`}
          >
            <Camera size={20} />
            Avatar Management
          </button>
          <button
            onClick={() => setView('chat')}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg ${
              view === 'chat' ? 'btn-primary' : 'btn-secondary'
            }`}
            disabled={!selectedAvatar}
          >
            <MessageCircle size={20} />
            Start Conversation
          </button>
        </div>

        {view === 'upload' ? (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
            <AvatarUpload />
            <AvatarList
              selectedAvatar={selectedAvatar}
              onSelectAvatar={setSelectedAvatar}
            />
          </div>
        ) : (
          <ChatInterface avatarId={selectedAvatar!} />
        )}
      </div>
    </main>
  )
}
