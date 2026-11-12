/**
 * The account rules the server enforces (backend/app/schemas.py), checked
 * here first so the user gets a sentence instead of a raw pattern error.
 * Keep the numbers and the pattern in step with that file.
 */

export const PASSWORD_MIN = 8
export const PASSWORD_MAX = 128
export const USERNAME_MIN = 3
export const USERNAME_MAX = 39
const USERNAME_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$/
// Deliberately loose: one @, something either side, a dot in the domain. The
// server is the authority; this only catches the obvious slips.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

/** Each returns a message to show, or null when the value is acceptable. */

export function emailProblem(email: string): string | null {
  const value = email.trim()
  if (!value) return 'Enter an email address'
  if (!EMAIL_PATTERN.test(value)) return 'That email address does not look right'
  return null
}

export function usernameProblem(username: string): string | null {
  const value = username.trim()
  if (value.length < USERNAME_MIN) return `Username must be at least ${USERNAME_MIN} characters`
  if (value.length > USERNAME_MAX) return `Username must be at most ${USERNAME_MAX} characters`
  if (!USERNAME_PATTERN.test(value)) {
    return 'Username can use letters, numbers, hyphens and underscores, and must start and end with a letter or number'
  }
  return null
}

export function passwordProblem(password: string): string | null {
  if (password.length < PASSWORD_MIN) return `Password must be at least ${PASSWORD_MIN} characters`
  if (password.length > PASSWORD_MAX) return `Password must be at most ${PASSWORD_MAX} characters`
  return null
}
