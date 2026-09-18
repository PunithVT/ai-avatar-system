'use client'

import { useState, type CSSProperties } from 'react'
import dynamic from 'next/dynamic'
import { AvatarUpload } from '@/components/AvatarUpload'
import { AvatarList } from '@/components/AvatarList'
import { ConnectionStatus } from '@/components/ui/ConnectionStatus'
import { AuthModal } from '@/components/AuthModal'
import { api } from '@/lib/api'
import { toast } from 'react-hot-toast'
import { useStore } from '@/store/useStore'

// Heavy panels (chat WebSocket pipeline, voice cloning recorder, history
// list with TanStack queries, settings form) load on-demand instead of
// shipping their JS in the home-page bundle. Cuts the initial chunk size
// by ~150 KB and lets the marketing landing page paint sooner.
const ChatInterface = dynamic(
  () => import('@/components/ChatInterface').then(m => m.ChatInterface),
  { ssr: false, loading: () => <PanelLoader label="Connecting…" /> },
)
const VoicePanel = dynamic(
  () => import('@/components/VoicePanel').then(m => m.VoicePanel),
  { ssr: false, loading: () => <PanelLoader label="Loading voice studio…" /> },
)
const HistoryPanel = dynamic(
  () => import('@/components/HistoryPanel').then(m => m.HistoryPanel),
  { ssr: false, loading: () => <PanelLoader label="Loading history…" /> },
)
const SettingsPanel = dynamic(
  () => import('@/components/SettingsPanel').then(m => m.SettingsPanel),
  { ssr: false, loading: () => <PanelLoader label="Loading settings…" /> },
)

function PanelLoader({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center py-20 text-gray-500 text-sm">
      <span className="inline-block w-2 h-2 rounded-full bg-primary-500 animate-pulse mr-2" />
      {label}
    </div>
  )
}
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
  History,
  Settings,
} from 'lucide-react'

const FEATURES = [
  {
    icon: Brain,
    title: 'LLM-Powered Intelligence',
    description: 'Claude & GPT-4 drive natural conversations with context-aware, cached prompts.',
    color: ' ',
    glow: 'rgba(168,85,247,0.3)',
  },
  {
    icon: AudioWaveform,
    title: 'Voice Cloning',
    description: 'Chatterbox Multilingual clones any voice from a 10-second sample in 23 languages.',
    color: ' ',
    glow: 'rgba(59,130,246,0.3)',
  },
  {
    icon: Activity,
    title: 'Lip-Sync Animation',
    description: 'MuseTalk V1.5 produces photorealistic lip-sync video aligned to the spoken audio.',
    color: ' ',
    glow: 'rgba(16,185,129,0.3)',
  },
  {
    icon: Zap,
    title: 'Streaming Pipeline',
    description: 'WebSocket streams tokens, audio, and video chunk-by-chunk for low first-byte latency.',
    color: ' ',
    glow: 'rgba(245,158,11,0.3)',
  },
  {
    icon: Globe,
    title: 'Multi-Language',
    description: 'Whisper STT + Chatterbox TTS support 23 languages end-to-end.',
    color: ' ',
    glow: 'rgba(99,102,241,0.3)',
  },
  {
    icon: Shield,
    title: 'Privacy-First',
    description: 'Self-host everything — your photos, voices, and conversations stay on your infra.',
    color: ' ',
    glow: 'rgba(244,63,94,0.3)',
  },
]

const STATS = [
  { value: '23', label: 'Languages' },
  { value: '<200ms', label: 'First-byte latency' },
  { value: '2', label: 'LLM backends' },
  { value: '100%', label: 'Self-hostable' },
]

type View = 'home' | 'avatars' | 'chat' | 'voice' | 'history' | 'settings'

export default function Home() {
  const { isAuthenticated, user, clearAuth } = useStore()
  const [selectedAvatar, setSelectedAvatar] = useState<string | null>(null)
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  // Session id to RESUME (set only when opening from history). Distinct from
  // activeSessionId (reported back after a session starts) so it can key the
  // ChatInterface without remounting mid-conversation when a fresh session id
  // arrives.
  const [resumeSessionId, setResumeSessionId] = useState<string | null>(null)
  const [view, setView] = useState<View>('home')

  const handleVoiceSelect = async (voiceId: string) => {
    if (!selectedAvatar) {
      toast('Select an avatar first to assign this voice', { icon: '💡' })
      return
    }
    try {
      await api.setAvatarVoice(selectedAvatar, voiceId)
      toast.success('Voice assigned to avatar', { icon: '🎙️' })
    } catch {
      toast.error('Failed to assign voice')
    }
  }

  const handleSelectAvatar = (id: string) => {
    setSelectedAvatar(id)
    setResumeSessionId(null)  // picking an avatar starts a fresh conversation
  }

  const handleStartChat = () => {
    if (selectedAvatar) {
      setResumeSessionId(null)  // "Start Conversation" = fresh session
      setView('chat')
    }
  }

  const handleResumeFromHistory = (avatarId: string, sessionId: string) => {
    setSelectedAvatar(avatarId)
    setResumeSessionId(sessionId)  // resume this exact conversation
    setView('chat')
  }

  const navItems: { id: View; icon: typeof Sparkles; label: string; disabled?: boolean }[] = [
    { id: 'home', icon: Sparkles, label: 'Home' },
    { id: 'avatars', icon: Camera, label: 'Avatars' },
    { id: 'voice', icon: Mic2, label: 'Voice' },
    { id: 'chat', icon: MessageCircle, label: 'Chat', disabled: !selectedAvatar },
    { id: 'history', icon: History, label: 'History' },
    { id: 'settings', icon: Settings, label: 'Settings' },
  ]

  return (
    <div className="min-h-screen">
      {/* ── Auth gate ── */}
      {!isAuthenticated() && <AuthModal />}

      {/* ── Navigation ── */}
      <nav className="fixed top-0 left-0 right-0 z-50 h-16">
        <div className="h-full mx-auto max-w-7xl px-6 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center shadow-xs">
              <Sparkles size={16} className="text-gray-900" />
            </div>
            <span className="font-bold text-lg text-gray-900">AvatarAI</span>
          </div>

          <div className="flex items-center gap-1 p-1 rounded-xl bg-white backdrop-blur-xl border border-gray-200 overflow-x-auto">
            {navItems.map(({ id, icon: Icon, label, disabled }) => (
              <button
                key={id}
                onClick={() => !disabled && setView(id)}
                disabled={disabled || undefined}
                aria-current={view === id ? 'page' : undefined}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 flex-shrink-0
 ${view === id
 ? ' text-gray-900 shadow-xs'
 : 'text-gray-500 hover:text-gray-900 hover:bg-white disabled:opacity-30 disabled:cursor-not-allowed'
 }`}
              >
                <Icon size={14} />
                <span className="hidden sm:inline">{label}</span>
              </button>
            ))}
          </div>

          <div className="flex items-center gap-3">
            <ConnectionStatus />
            {user && (
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-500 hidden sm:block">{user.username}</span>
                <button
                  onClick={() => { api.logout(); clearAuth() }}
                  className="text-xs text-gray-500 hover:text-error-600 transition-colors px-2 py-1 rounded-lg hover:bg-error-50"
                  title="Sign out"
                >
                  Sign out
                </button>
              </div>
            )}
          </div>
        </div>
        {/* nav card blur border */}
        <div className="absolute inset-0 -z-10 bg-white backdrop-blur-xl border-b border-gray-200" />
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
                <div className="absolute -top-20 -right-40 w-80 h-80 bg-primary-600/15 rounded-full blur-3xl animate-float" style={{ animationDelay: '2s' }} />
                <div className="absolute bottom-20 left-1/4 w-72 h-72 bg-primary-800/20 rounded-full blur-3xl animate-float" style={{ animationDelay: '4s' }} />
                <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[600px] rounded-full opacity-5" />
              </div>

              {/* Badge */}
              <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-primary-500/10 border border-primary-500/30 mb-8 animate-slide-up">
                <Sparkles size={14} className="text-primary-400" />
                <span className="text-sm text-primary-300 font-medium">Next-Gen AI Avatar Platform</span>
              </div>

              {/* Headline */}
              <h1 className="text-6xl md:text-7xl lg:text-8xl font-black leading-none mb-6 tracking-tight animate-slide-up" style={{ animationDelay: '0.1s' }}>
                <span className="text-gray-900">Talk to</span>
                <br />
                <span className="text-gray-900">Any Face,</span>
                <br />
                <span className="text-gray-900">Any Voice.</span>
              </h1>

              <p className="max-w-2xl text-lg md:text-xl text-gray-500 mb-10 leading-relaxed animate-slide-up" style={{ animationDelay: '0.2s' }}>
                Upload a photo, clone a voice, and have real-time AI-powered conversations with
                photorealistic lip-sync animations. Powered by Claude, Whisper, Chatterbox, and MuseTalk.
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
                    <div className="text-3xl font-black text-gray-900">{value}</div>
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
                  <span className="text-gray-900"> avatar experiences</span>
                </h2>
                <p className="text-gray-500 text-lg max-w-2xl mx-auto">
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
                    <div className={`w-12 h-12 rounded-xl ${color} flex items-center justify-center mb-4 shadow-lg group-hover:scale-110 transition-transform duration-300`}>
                      <Icon size={22} className="text-gray-900" />
                    </div>
                    <h3 className="font-bold text-lg text-gray-900 mb-2">{title}</h3>
                    <p className="text-gray-500 text-sm leading-relaxed">{description}</p>
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
              <h1 className="text-3xl font-black text-gray-900 mb-2">Avatar Studio</h1>
              <p className="text-gray-500">Upload photos and manage your avatar collection.</p>
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
              <h1 className="text-3xl font-black text-gray-900 mb-2">Voice Studio</h1>
              <p className="text-gray-500">Clone voices and manage your voice library.</p>
            </div>
            <VoicePanel onVoiceSelect={handleVoiceSelect} />
          </div>
        )}

        {/* ── CHAT VIEW ── */}
        {view === 'chat' && selectedAvatar && (
          <div className="max-w-7xl mx-auto px-6 py-10 animate-fade-in">
            <div className="mb-6">
              <h1 className="text-3xl font-black text-gray-900 mb-2">Live Conversation</h1>
              <p className="text-gray-500">Talk to your AI avatar in real time.</p>
            </div>
            <ChatInterface
              key={`${selectedAvatar}:${resumeSessionId ?? 'new'}`}
              avatarId={selectedAvatar}
              resumeSessionId={resumeSessionId ?? undefined}
              onSessionCreated={setActiveSessionId}
            />
          </div>
        )}

        {/* Redirect if no avatar selected for chat */}
        {view === 'chat' && !selectedAvatar && (
          <div className="max-w-7xl mx-auto px-6 py-10 text-center">
            <p className="text-gray-500 mb-4">Please select an avatar first.</p>
            <button onClick={() => setView('avatars')} className="btn-primary">
              <Camera size={18} />
              Go to Avatar Studio
            </button>
          </div>
        )}

        {/* ── HISTORY VIEW ── */}
        {view === 'history' && (
          <HistoryPanel onResume={handleResumeFromHistory} />
        )}

        {/* ── SETTINGS VIEW ── */}
        {view === 'settings' && (
          <SettingsPanel />
        )}
      </main>
    </div>
  )
}
