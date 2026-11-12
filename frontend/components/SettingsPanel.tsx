'use client'

import { useState } from 'react'
import { Save, Loader2, User, KeyRound, Trash2, UserPlus } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'
import { errorMessage } from '@/lib/errors'
import { emailProblem, passwordProblem as passwordRuleProblem, usernameProblem } from '@/lib/validation'

export function SettingsPanel() {
  const user = useStore((s) => s.user)
  const token = useStore((s) => s.token)
  const setAuth = useStore((s) => s.setAuth)
  const clearAuth = useStore((s) => s.clearAuth)

  // Guests are real server-side accounts, so trust the flag the API returns
  // rather than sniffing the token or a hardcoded user id.
  const isGuest = user?.is_guest === true

  const [fullName, setFullName] = useState(user?.full_name || '')
  // A guest's generated username and placeholder email are not theirs to keep.
  const [username, setUsername] = useState(isGuest ? '' : user?.username || '')
  const [email, setEmail] = useState(isGuest ? '' : user?.email || '')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [savingProfile, setSavingProfile] = useState(false)
  const [savingPassword, setSavingPassword] = useState(false)

  const passwordProblem = (): string | null => {
    const rule = passwordRuleProblem(newPassword)
    if (rule) return rule
    if (newPassword !== confirmPassword) return 'Passwords do not match'
    return null
  }

  const saveProfile = async () => {
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
      // Checked here so a slip gets a sentence, not the server's raw
      // validation output.
      const invalid = (update.username !== undefined && usernameProblem(update.username))
        || (update.email !== undefined && emailProblem(update.email))
      if (invalid) {
        toast.error(invalid)
        return
      }
      const updated = await api.updateProfile(update)
      if (token) setAuth(token, updated)
      toast.success('Profile updated')
    } catch (err: unknown) {
      toast.error(errorMessage(err, 'Could not save profile'))
    } finally {
      setSavingProfile(false)
    }
  }

  const changePassword = async () => {
    const problem = passwordProblem()
    if (problem) {
      toast.error(problem)
      return
    }
    setSavingPassword(true)
    try {
      await api.updateProfile({ password: newPassword, current_password: currentPassword })
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      toast.success('Password updated')
    } catch (err: unknown) {
      // 403 carries "Current password is incorrect" — show it verbatim.
      toast.error(errorMessage(err, 'Could not change password'))
    } finally {
      setSavingPassword(false)
    }
  }

  /**
   * Turn the guest account into a real one. The server does this when a
   * guest sets an email and a password; the avatars, voices and history made
   * as a guest stay attached because it is the same account.
   */
  const createAccount = async () => {
    // Email and password always travel together: the server refuses to turn
    // a guest into an account on a password alone.
    const problem = emailProblem(email)
      || (username.trim() ? usernameProblem(username) : null)
      || passwordProblem()
    if (problem) {
      toast.error(problem)
      return
    }
    setSavingProfile(true)
    try {
      const update: Parameters<typeof api.updateProfile>[0] = { email: email.trim(), password: newPassword }
      if (username.trim()) update.username = username.trim()
      if (fullName.trim()) update.full_name = fullName.trim()
      const updated = await api.updateProfile(update)
      if (token) setAuth(token, updated)
      setNewPassword('')
      setConfirmPassword('')
      toast.success('Account created — your data is saved', { icon: '🎉' })
    } catch (err: unknown) {
      toast.error(errorMessage(err, 'Could not create the account'))
    } finally {
      setSavingProfile(false)
    }
  }

  const signOut = () => {
    // Clearing auth also leaves this view: the page resets itself whenever
    // the signed-in identity changes.
    api.logout()
    clearAuth()
    toast('Signed out', { icon: '👋' })
  }

  const nameField = (
    <div className="space-y-1.5">
      <label htmlFor="settings-fullname" className="text-sm font-medium text-gray-700">Full name</label>
      <input
        id="settings-fullname"
        type="text"
        value={fullName}
        onChange={(e) => setFullName(e.target.value)}
        className="input-field"
        placeholder="Your name"
        autoComplete="name"
      />
    </div>
  )
  const usernameField = (
    <div className="space-y-1.5">
      <label htmlFor="settings-username" className="text-sm font-medium text-gray-700">Username</label>
      <input
        id="settings-username"
        type="text"
        value={username}
        onChange={(e) => setUsername(e.target.value)}
        className="input-field"
        placeholder={isGuest ? 'Optional' : 'username'}
        autoComplete="username"
      />
    </div>
  )
  const emailField = (
    <div className="space-y-1.5 md:col-span-2">
      <label htmlFor="settings-email" className="text-sm font-medium text-gray-700">Email</label>
      <input
        id="settings-email"
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        className="input-field"
        placeholder="you@example.com"
        autoComplete="email"
      />
    </div>
  )
  const newPasswordFields = (
    <>
      <div className="space-y-1.5">
        <label htmlFor="settings-new-password" className="text-sm font-medium text-gray-700">
          {isGuest ? 'Password' : 'New password'}
        </label>
        <input
          id="settings-new-password"
          type="password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          className="input-field"
          placeholder="At least 8 characters"
          autoComplete="new-password"
        />
      </div>
      <div className="space-y-1.5">
        <label htmlFor="settings-confirm-password" className="text-sm font-medium text-gray-700">
          {isGuest ? 'Confirm password' : 'Confirm new password'}
        </label>
        <input
          id="settings-confirm-password"
          type="password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          className="input-field"
          placeholder="Repeat your password"
          autoComplete="new-password"
        />
      </div>
    </>
  )

  return (
    <div className="mx-auto max-w-3xl animate-fade-in px-6 py-10">
      <div className="mb-8">
        <h1 className="mb-2 text-3xl text-gray-900">Settings</h1>
        <p className="text-gray-500">Manage your account and preferences.</p>
      </div>

      {isGuest ? (
        <div className="card flex flex-col gap-5">
          <div className="flex items-center gap-2">
            <UserPlus size={16} className="text-primary-600" aria-hidden="true" />
            <h2 className="text-xl text-gray-900">Create your account</h2>
          </div>
          <div className="divider" />
          <div className="card-tint flex items-start gap-3 p-4">
            <User size={16} className="mt-0.5 shrink-0 text-warning-600" aria-hidden="true" />
            <p className="text-sm text-gray-700">
              You&apos;re signed in as a guest. Guest data is deleted after 48 hours of inactivity —
              add an email and password to keep your avatars, voices and conversations.
            </p>
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {emailField}
            {usernameField}
            {nameField}
            {newPasswordFields}
          </div>
          <button
            onClick={createAccount}
            disabled={savingProfile || !email || !newPassword || !confirmPassword}
            className="btn-primary w-full md:w-auto md:self-end"
          >
            {savingProfile ? <Loader2 size={15} className="animate-spin" /> : <UserPlus size={15} />}
            Create account
          </button>
        </div>
      ) : (
        <>
          <div className="card flex flex-col gap-5">
            <div className="flex items-center gap-2">
              <User size={16} className="text-primary-600" aria-hidden="true" />
              <h2 className="text-xl text-gray-900">Profile</h2>
            </div>
            <div className="divider" />
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {nameField}
              {usernameField}
              {emailField}
            </div>
            <button
              onClick={saveProfile}
              disabled={savingProfile}
              className="btn-primary w-full md:w-auto md:self-end"
            >
              {savingProfile ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
              Save changes
            </button>
          </div>

          <div className="card mt-6 flex flex-col gap-5">
            <div className="flex items-center gap-2">
              <KeyRound size={16} className="text-primary-600" aria-hidden="true" />
              <h2 className="text-xl text-gray-900">Password</h2>
            </div>
            <div className="divider" />
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <div className="space-y-1.5 md:col-span-2">
                <label htmlFor="settings-current-password" className="text-sm font-medium text-gray-700">
                  Current password
                </label>
                <input
                  id="settings-current-password"
                  type="password"
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  className="input-field"
                  placeholder="Your current password"
                  autoComplete="current-password"
                />
              </div>
              {newPasswordFields}
            </div>
            <button
              onClick={changePassword}
              disabled={savingPassword || !currentPassword || !newPassword || !confirmPassword}
              className="btn-primary w-full md:w-auto md:self-end"
            >
              {savingPassword ? <Loader2 size={15} className="animate-spin" /> : <KeyRound size={15} />}
              Update password
            </button>
          </div>
        </>
      )}

      <div className="card mt-6 flex flex-col gap-5 border-error-600/20">
        <div className="flex items-center gap-2">
          <Trash2 size={16} className="text-error-600" aria-hidden="true" />
          <h2 className="text-xl text-gray-900">Sign out</h2>
        </div>
        <div className="divider" />
        <p className="text-sm text-gray-500">
          {isGuest
            ? 'Signing out of a guest session abandons it — you will not be able to get back to this data.'
            : 'Sign out of this device. Your avatars, voices, and conversations remain on the server.'}
        </p>
        <button onClick={signOut} className="btn-secondary w-full md:w-auto md:self-end">
          Sign out
        </button>
      </div>
    </div>
  )
}
