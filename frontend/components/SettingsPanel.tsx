'use client'

import { useState } from 'react'
import { Save, Loader2, User, KeyRound, Trash2 } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'
import type { ApiError } from '@/lib/types'

export function SettingsPanel() {
  const { user, setAuth, token, clearAuth } = useStore()
  const [fullName, setFullName] = useState(user?.full_name || '')
  const [username, setUsername] = useState(user?.username || '')
  const [email, setEmail] = useState(user?.email || '')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [savingProfile, setSavingProfile] = useState(false)
  const [savingPassword, setSavingPassword] = useState(false)

  // Guests are real server-side accounts now, so trust the flag the API
  // returns rather than sniffing the token or a hardcoded user id.
  const isGuest = user?.is_guest === true || token === 'guest'

  const saveProfile = async () => {
    if (isGuest) {
      toast.error('Sign in with a real account to edit your profile')
      return
    }
    setSavingProfile(true)
    try {
      const update: Record<string, string> = {}
      if (fullName !== (user?.full_name || '')) update.full_name = fullName
      if (username && username !== user?.username) update.username = username
      if (email && email !== user?.email) update.email = email
      if (Object.keys(update).length === 0) {
        toast('Nothing to update', { icon: 'ℹ️' })
        return
      }
      const updated = await api.updateProfile(update)
      if (token) setAuth(token, updated)
      toast.success('Profile updated')
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not save profile')
    } finally {
      setSavingProfile(false)
    }
  }

  const changePassword = async () => {
    if (isGuest) {
      toast.error('Sign in with a real account to change your password')
      return
    }
    if (newPassword.length < 8) {
      toast.error('Password must be at least 8 characters')
      return
    }
    if (newPassword !== confirmPassword) {
      toast.error('Passwords do not match')
      return
    }
    setSavingPassword(true)
    try {
      await api.updateProfile({ password: newPassword })
      setNewPassword('')
      setConfirmPassword('')
      toast.success('Password updated')
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not change password')
    } finally {
      setSavingPassword(false)
    }
  }

  return (
    <div className="max-w-3xl mx-auto px-6 py-10 animate-fade-in">
      <div className="mb-8">
        <h1 className="text-3xl font-black gradient-text mb-2">Settings</h1>
        <p className="text-gray-400">Manage your account and preferences.</p>
      </div>

      {isGuest && (
        <div className="card-glow mb-6 flex items-start gap-3">
          <User size={16} className="text-amber-400 mt-0.5 flex-shrink-0" />
          <div>
            <p className="text-sm text-white font-semibold">You&apos;re signed in as a guest.</p>
            <p className="text-xs text-gray-400 mt-1">Sign out and register an account to save your profile and access multi-device sync.</p>
          </div>
        </div>
      )}

      {/* Profile card */}
      <div className="card flex flex-col gap-5">
        <div className="flex items-center gap-2">
          <User size={16} className="text-primary-400" />
          <h2 className="text-xl font-bold text-white">Profile</h2>
        </div>
        <div className="divider" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-gray-300">Full name</label>
            <input
              type="text"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              className="input-field"
              placeholder="Your name"
              disabled={isGuest}
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-gray-300">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="input-field"
              placeholder="username"
              disabled={isGuest}
            />
          </div>
          <div className="space-y-1.5 md:col-span-2">
            <label className="text-sm font-medium text-gray-300">Email</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="input-field"
              placeholder="you@example.com"
              disabled={isGuest}
            />
          </div>
        </div>
        <button
          onClick={saveProfile}
          disabled={savingProfile || isGuest}
          className="btn-primary w-full md:w-auto md:self-end"
        >
          {savingProfile ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
          Save changes
        </button>
      </div>

      {/* Password card */}
      <div className="card flex flex-col gap-5 mt-6">
        <div className="flex items-center gap-2">
          <KeyRound size={16} className="text-primary-400" />
          <h2 className="text-xl font-bold text-white">Password</h2>
        </div>
        <div className="divider" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-gray-300">New password</label>
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="input-field"
              placeholder="At least 8 characters"
              disabled={isGuest}
              autoComplete="new-password"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-gray-300">Confirm new password</label>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="input-field"
              placeholder="Repeat your password"
              disabled={isGuest}
              autoComplete="new-password"
            />
          </div>
        </div>
        <button
          onClick={changePassword}
          disabled={savingPassword || isGuest || !newPassword || !confirmPassword}
          className="btn-primary w-full md:w-auto md:self-end"
        >
          {savingPassword ? <Loader2 size={15} className="animate-spin" /> : <KeyRound size={15} />}
          Update password
        </button>
      </div>

      {/* Danger zone */}
      <div className="card flex flex-col gap-5 mt-6 border border-red-500/20">
        <div className="flex items-center gap-2">
          <Trash2 size={16} className="text-red-400" />
          <h2 className="text-xl font-bold text-white">Danger zone</h2>
        </div>
        <div className="divider" />
        <p className="text-sm text-gray-400">
          Sign out of this device. Your avatars, voices, and conversations remain on the server.
        </p>
        <button
          onClick={() => {
            api.logout()
            clearAuth()
            toast('Signed out', { icon: '👋' })
          }}
          className="btn-secondary w-full md:w-auto md:self-end"
        >
          Sign out
        </button>
      </div>
    </div>
  )
}
