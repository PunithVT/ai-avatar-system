'use client'

import { useState, type CSSProperties } from 'react'
import { AvatarUpload } from '@/components/AvatarUpload'
import { AvatarList } from '@/components/AvatarList'
import { ChatInterface } from '@/components/ChatInterface'
import { VoicePanel } from '@/components/VoicePanel'
import {
  Camera,
  MessageCircle,
  Mic2,
  Sparkles,
  Zap,
  Globe,
  Shield,
  Play,
  ChevronRight,
  Activity,
  Brain,
  AudioWaveform,
} from 'lucide-react'

const FEATURES = [
  {
    icon: Brain,
    title: 'LLM-Powered Intelligence',
    description: 'Claude & GPT-4 drive natural conversations with context-aware responses.',
    color: 'from-purple-500 to-pink-500',
    glow: 'rgba(168,85,247,0.3)',
  },
  {
    icon: AudioWaveform,
    title: 'Voice Cloning',
    description: 'Clone any voice from a short sample and apply it to your avatar.',
    color: 'from-blue-500 to-cyan-500',
    glow: 'rgba(59,130,246,0.3)',
  },
  {
    icon: Activity,
    title: 'Lip-Sync Animation',
    description: 'MuseTalk V1.5 produces photorealistic lip-sync animated responses.',
    color: 'from-emerald-500 to-teal-500',
    glow: 'rgba(16,185,129,0.3)',
  },
  {
    icon: Zap,
    title: 'Real-Time Streaming',
    description: 'WebSocket pipeline delivers audio, text and video with minimal latency.',
    color: 'from-amber-500 to-orange-500',
    glow: 'rgba(245,158,11,0.3)',
  },
  {
    icon: Globe,
    title: 'Multi-Language',
    description: 'Whisper STT + XTTS v2 TTS support 18+ languages out of the box.',
    color: 'from-indigo-500 to-blue-500',
    glow: 'rgba(99,102,241,0.3)',
  },
  {
    icon: Shield,
    title: 'Privacy-First',
    description: 'Run everything locally or on your own infrastructure. No data leaves.',
    color: 'from-rose-500 to-pink-500',
    glow: 'rgba(244,63,94,0.3)',
  },
]

const STATS = [
  { value: '18+', label: 'Languages' },
  { value: '<200ms', label: 'Latency' },
  { value: '3', label: 'LLM Backends' },
  { value: '100%', label: 'Local Option' },
]

type View = 'home' | 'avatars' | 'chat' | 'voice'

export default function Home() {
  const [selectedAvatar, setSelectedAvatar] = useState<string | null>(null)
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [view, setView] = useState<View>('home')

  const handleSelectAvatar = (id: string) => {
    setSelectedAvatar(id)
  }

  const handleStartChat = () => {
    if (selectedAvatar) setView('chat')
  }

  return (
    <div className="min-h-screen">
      {/* ── Navigation ── */}
      <nav className="fixed top-0 left-0 right-0 z-50 h-16">
        <div className="h-full mx-auto max-w-7xl px-6 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-primary-500 to-accent-600 flex items-center justify-center shadow-glow-sm">
              <Sparkles size={16} className="text-white" />
            </div>
            <span className="font-bold text-lg gradient-text">AvatarAI</span>
          </div>

          <div className="flex items-center gap-1 p-1 rounded-xl bg-surface-800/80 backdrop-blur-xl border border-white/8">
            {[
              { id: 'home' as View, icon: Sparkles, label: 'Home' },
              { id: 'avatars' as View, icon: Camera, label: 'Avatars' },
              { id: 'voice' as View, icon: Mic2, label: 'Voice' },
              { id: 'chat' as View, icon: MessageCircle, label: 'Chat', disabled: !selectedAvatar },
            ].map(({ id, icon: Icon, label, disabled }) => (
              <button
                key={id}
                onClick={() => !disabled && setView(id)}
                disabled={disabled || undefined}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200
                  ${view === id
                    ? 'bg-gradient-to-r from-primary-600/80 to-accent-600/80 text-white shadow-glow-sm'
                    : 'text-gray-400 hover:text-white hover:bg-white/5 disabled:opacity-30 disabled:cursor-not-allowed'
                  }`}
              >
                <Icon size={14} />
                {label}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1.5 text-xs text-gray-400">
              <span className="status-dot online" />
              <span>System Online</span>
            </div>
          </div>
        </div>
        {/* nav glass blur border */}
        <div className="absolute inset-0 -z-10 bg-surface-900/70 backdrop-blur-xl border-b border-white/6" />
      </nav>

      <main className="pt-16">
        {/* ── HOME VIEW ── */}
        {view === 'home' && (
          <div className="animate-fade-in">
            {/* Hero */}
            <section className="relative flex flex-col items-center justify-center min-h-[calc(100vh-4rem)] px-6 text-center overflow-hidden">
              {/* Aurora background */}
              <div className="absolute inset-0 overflow-hidden pointer-events-none">
                <div className="absolute -top-40 -left-40 w-96 h-96 bg-primary-600/20 rounded-full blur-3xl animate-float" />
                <div className="absolute -top-20 -right-40 w-80 h-80 bg-accent-600/15 rounded-full blur-3xl animate-float" style={{ animationDelay: '2s' }} />
                <div className="absolute bottom-20 left-1/4 w-72 h-72 bg-primary-800/20 rounded-full blur-3xl animate-float" style={{ animationDelay: '4s' }} />
                <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[600px] rounded-full opacity-5"
                  style={{ background: 'radial-gradient(circle, #a855f7 0%, transparent 70%)' }} />
              </div>

              {/* Badge */}
              <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-primary-500/10 border border-primary-500/30 mb-8 animate-slide-up">
                <Sparkles size={14} className="text-primary-400" />
                <span className="text-sm text-primary-300 font-medium">Next-Gen AI Avatar Platform</span>
              </div>

              {/* Headline */}
              <h1 className="text-6xl md:text-7xl lg:text-8xl font-black leading-none mb-6 tracking-tight animate-slide-up" style={{ animationDelay: '0.1s' }}>
                <span className="gradient-text">Talk to</span>
                <br />
                <span className="text-white">Any Face,</span>
                <br />
                <span className="gradient-text-gold">Any Voice.</span>
              </h1>

              <p className="max-w-2xl text-lg md:text-xl text-gray-400 mb-10 leading-relaxed animate-slide-up" style={{ animationDelay: '0.2s' }}>
                Upload a photo, clone a voice, and have real-time AI-powered conversations with
                photorealistic lip-sync animations. Powered by Claude, Whisper, and MuseTalk.
              </p>

              {/* CTAs */}
              <div className="flex flex-wrap items-center justify-center gap-4 animate-slide-up" style={{ animationDelay: '0.3s' }}>
                <button
                  onClick={() => setView('avatars')}
                  className="btn-primary text-base px-8 py-3.5 rounded-2xl group"
                >
                  <Play size={18} className="group-hover:scale-110 transition-transform" />
                  Get Started Free
                  <ChevronRight size={16} className="group-hover:translate-x-1 transition-transform" />
                </button>
                <button
                  onClick={() => setView('voice')}
                  className="btn-secondary text-base px-8 py-3.5 rounded-2xl"
                >
                  <Mic2 size={18} />
                  Clone a Voice
                </button>
              </div>

              {/* Stats */}
              <div className="flex flex-wrap items-center justify-center gap-8 mt-16 animate-slide-up" style={{ animationDelay: '0.4s' }}>
                {STATS.map(({ value, label }) => (
                  <div key={label} className="text-center">
                    <div className="text-3xl font-black gradient-text">{value}</div>
                    <div className="text-sm text-gray-500 mt-1">{label}</div>
                  </div>
                ))}
              </div>
            </section>

            {/* Features */}
            <section className="px-6 pb-24 max-w-7xl mx-auto">
              <div className="text-center mb-14">
                <h2 className="text-4xl font-black mb-4">
                  Everything you need to build
                  <span className="gradient-text"> avatar experiences</span>
                </h2>
                <p className="text-gray-400 text-lg max-w-2xl mx-auto">
                  A complete stack — from voice cloning to lip-sync video — running locally or in the cloud.
                </p>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {FEATURES.map(({ icon: Icon, title, description, color, glow }) => (
                  <div
                    key={title}
                    className="feature-card group"
                    style={{ '--glow': glow } as CSSProperties}
                  >
                    <div className={`w-12 h-12 rounded-xl bg-gradient-to-br ${color} flex items-center justify-center mb-4 shadow-lg group-hover:scale-110 transition-transform duration-300`}>
                      <Icon size={22} className="text-white" />
                    </div>
                    <h3 className="font-bold text-lg text-white mb-2">{title}</h3>
                    <p className="text-gray-400 text-sm leading-relaxed">{description}</p>
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}

        {/* ── AVATAR VIEW ── */}
        {view === 'avatars' && (
          <div className="max-w-7xl mx-auto px-6 py-10 animate-fade-in">
            <div className="mb-8">
              <h1 className="text-3xl font-black gradient-text mb-2">Avatar Studio</h1>
              <p className="text-gray-400">Upload photos and manage your avatar collection.</p>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
              <AvatarUpload />
              <AvatarList
                selectedAvatar={selectedAvatar}
                onSelectAvatar={handleSelectAvatar}
              />
            </div>
            {selectedAvatar && (
              <div className="mt-8 flex justify-center">
                <button
                  onClick={handleStartChat}
                  className="btn-primary text-lg px-10 py-4 rounded-2xl group"
                >
                  <MessageCircle size={20} />
                  Start Conversation
                  <ChevronRight size={18} className="group-hover:translate-x-1 transition-transform" />
                </button>
              </div>
            )}
          </div>
        )}

        {/* ── VOICE VIEW ── */}
        {view === 'voice' && (
          <div className="max-w-4xl mx-auto px-6 py-10 animate-fade-in">
            <div className="mb-8">
              <h1 className="text-3xl font-black gradient-text mb-2">Voice Studio</h1>
              <p className="text-gray-400">Clone voices and manage your voice library.</p>
            </div>
            <VoicePanel sessionId={activeSessionId} />
          </div>
        )}

        {/* ── CHAT VIEW ── */}
        {view === 'chat' && selectedAvatar && (
          <div className="max-w-7xl mx-auto px-6 py-10 animate-fade-in">
            <div className="mb-6">
              <h1 className="text-3xl font-black gradient-text mb-2">Live Conversation</h1>
              <p className="text-gray-400">Talk to your AI avatar in real time.</p>
            </div>
            <ChatInterface avatarId={selectedAvatar} onSessionCreated={setActiveSessionId} />
          </div>
        )}

        {/* Redirect if no avatar selected for chat */}
        {view === 'chat' && !selectedAvatar && (
          <div className="max-w-7xl mx-auto px-6 py-10 text-center">
            <p className="text-gray-400 mb-4">Please select an avatar first.</p>
            <button onClick={() => setView('avatars')} className="btn-primary">
              <Camera size={18} />
              Go to Avatar Studio
            </button>
          </div>
        )}
      </main>
    </div>
  )
}
