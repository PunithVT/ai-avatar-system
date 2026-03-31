'use client'

import { useState } from 'react'
import { Sparkles, Loader2, Eye, EyeOff, UserPlus, LogIn, User } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'

export function AuthModal() {
  const { setAuth } = useStore()
  const [tab, setTab] = useState<'login' | 'register'>('login')
  const [isLoading, setIsLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)

  // Login fields
  const [loginEmail, setLoginEmail] = useState('')
  const [loginPassword, setLoginPassword] = useState('')

  // Register fields
  const [regEmail, setRegEmail] = useState('')
  const [regUsername, setRegUsername] = useState('')
  const [regPassword, setRegPassword] = useState('')
  const [regFullName, setRegFullName] = useState('')

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!loginEmail || !loginPassword) return
    setIsLoading(true)
    try {
      const data = await api.login(loginEmail, loginPassword)
      const profile = await api.getProfile()
      setAuth(data.access_token, profile)
      toast.success(`Welcome back, ${profile.username}!`, { icon: '👋' })
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Invalid credentials')
    } finally {
      setIsLoading(false)
    }
  }

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!regEmail || !regUsername || !regPassword) return
    setIsLoading(true)
    try {
      await api.register({ email: regEmail, username: regUsername, password: regPassword, full_name: regFullName })
      // Auto-login after register
      const data = await api.login(regEmail, regPassword)
      const profile = await api.getProfile()
      setAuth(data.access_token, profile)
      toast.success(`Account created! Welcome, ${profile.username}!`, { icon: '🎉' })
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Registration failed')
    } finally {
      setIsLoading(false)
    }
  }

  const continueAsGuest = () => {
    // Set a synthetic guest user — backend falls back to "demo-user" when no JWT
    setAuth('guest', { id: 'demo-user', email: 'guest@local', username: 'Guest' })
    toast('Continuing as guest — data may not persist', { icon: '👤' })
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-surface-950/90 backdrop-blur-md">
      <div className="w-full max-w-md mx-4 glass-card rounded-2xl p-8 animate-scale-in">
        {/* Logo */}
        <div className="flex flex-col items-center gap-3 mb-8">
          <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-primary-500 to-accent-600 flex items-center justify-center shadow-glow">
            <Sparkles size={22} className="text-white" />
          </div>
          <div className="text-center">
            <h1 className="text-2xl font-black gradient-text">AvatarAI</h1>
            <p className="text-sm text-gray-500 mt-0.5">Sign in to your account</p>
          </div>
        </div>

        {/* Tab switcher */}
        <div className="flex gap-1 p-1 rounded-xl bg-surface-800/80 border border-white/8 mb-6">
          {(['login', 'register'] as const).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg text-sm font-medium transition-all duration-200
                ${tab === t
                  ? 'bg-gradient-to-r from-primary-600/80 to-accent-600/80 text-white shadow-glow-sm'
                  : 'text-gray-400 hover:text-white'
                }`}
            >
              {t === 'login' ? <><LogIn size={14} /> Sign In</> : <><UserPlus size={14} /> Register</>}
            </button>
          ))}
        </div>

        {/* Login form */}
        {tab === 'login' && (
          <form onSubmit={handleLogin} className="space-y-4">
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-gray-300">Email</label>
              <input
                type="email"
                value={loginEmail}
                onChange={e => setLoginEmail(e.target.value)}
                className="input-field"
                placeholder="you@example.com"
                autoComplete="email"
                required
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-gray-300">Password</label>
              <div className="relative">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={loginPassword}
                  onChange={e => setLoginPassword(e.target.value)}
                  className="input-field pr-10"
                  placeholder="••••••••"
                  autoComplete="current-password"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(s => !s)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
                >
                  {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>
            <button type="submit" disabled={isLoading} className="btn-primary w-full py-3 rounded-xl mt-2">
              {isLoading ? <Loader2 size={16} className="animate-spin" /> : <LogIn size={16} />}
              Sign In
            </button>
          </form>
        )}

        {/* Register form */}
        {tab === 'register' && (
          <form onSubmit={handleRegister} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-gray-300">Username</label>
                <input
                  type="text"
                  value={regUsername}
                  onChange={e => setRegUsername(e.target.value)}
                  className="input-field"
                  placeholder="cooluser"
                  required
                />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-gray-300">Full Name</label>
                <input
                  type="text"
                  value={regFullName}
                  onChange={e => setRegFullName(e.target.value)}
                  className="input-field"
                  placeholder="Optional"
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-gray-300">Email</label>
              <input
                type="email"
                value={regEmail}
                onChange={e => setRegEmail(e.target.value)}
                className="input-field"
                placeholder="you@example.com"
                required
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-gray-300">Password</label>
              <div className="relative">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={regPassword}
                  onChange={e => setRegPassword(e.target.value)}
                  className="input-field pr-10"
                  placeholder="Min 8 characters"
                  minLength={8}
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(s => !s)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
                >
                  {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>
            <button type="submit" disabled={isLoading} className="btn-primary w-full py-3 rounded-xl mt-2">
              {isLoading ? <Loader2 size={16} className="animate-spin" /> : <UserPlus size={16} />}
              Create Account
            </button>
          </form>
        )}

        {/* Divider */}
        <div className="flex items-center gap-3 my-5">
          <div className="flex-1 h-px bg-white/10" />
          <span className="text-xs text-gray-600">or</span>
          <div className="flex-1 h-px bg-white/10" />
        </div>

        {/* Guest mode */}
        <button
          onClick={continueAsGuest}
          className="btn-secondary w-full py-2.5 rounded-xl text-sm"
        >
          <User size={15} />
          Continue as Guest
        </button>
        <p className="text-xs text-center text-gray-600 mt-3">
          Guest data is scoped to this browser session only.
        </p>
      </div>
    </div>
  )
}
