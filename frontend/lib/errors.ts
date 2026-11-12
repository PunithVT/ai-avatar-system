/**
 * Turning a failed request into text that is safe to show.
 *
 * FastAPI answers in two shapes. Errors the API raises itself carry a string
 * `detail`. Request-validation failures (HTTP 422) carry an ARRAY of objects:
 *
 *     { "detail": [{ "loc": ["body", "username"], "msg": "String should …" }] }
 *
 * Every error toast used to pass `detail` straight through. Handing the array
 * to the toaster made React throw "Objects are not valid as a React child" —
 * and the toaster sits outside the error boundary, so one mistyped username
 * replaced the whole app with a blank page. Everything that shows a server
 * error goes through here, and this always returns a string.
 */

const FIELD_LABELS: Record<string, string> = {
  email: 'Email',
  username: 'Username',
  password: 'Password',
  current_password: 'Current password',
  full_name: 'Name',
  name: 'Name',
  file: 'File',
  content: 'Message',
}

function fieldLabel(loc: unknown): string | null {
  if (!Array.isArray(loc) || loc.length === 0) return null
  const last = loc[loc.length - 1]
  if (typeof last !== 'string' || last === 'body' || last === 'query') return null
  return FIELD_LABELS[last] ?? last.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
}

function describeItem(item: unknown): string | null {
  if (typeof item === 'string') return item.trim() || null
  if (!item || typeof item !== 'object') return null
  const { msg, loc } = item as { msg?: unknown; loc?: unknown }
  if (typeof msg !== 'string' || !msg.trim()) return null
  // Pydantic prefixes custom validator messages with "Value error, ".
  const text = msg.replace(/^Value error,\s*/i, '').trim()
  const label = fieldLabel(loc)
  return label ? `${label}: ${text}` : text
}

/** `detail` from an API error body as a string, or null if it has none. */
export function detailText(detail: unknown): string | null {
  if (typeof detail === 'string') return detail.trim() || null
  if (Array.isArray(detail)) {
    const parts = detail.map(describeItem).filter((p): p is string => p !== null)
    return parts.length > 0 ? parts.join(' · ') : null
  }
  if (detail && typeof detail === 'object') {
    // Not a shape this API produces, but a nested {msg} or {message} is the
    // obvious thing to try before giving up on it.
    const { msg, message } = detail as { msg?: unknown; message?: unknown }
    if (typeof msg === 'string' && msg.trim()) return msg.trim()
    if (typeof message === 'string' && message.trim()) return message.trim()
  }
  return null
}

/** HTTP status of a failed request, if it got as far as a response. */
export function errorStatus(err: unknown): number | undefined {
  const status = (err as { response?: { status?: unknown } } | null)?.response?.status
  return typeof status === 'number' ? status : undefined
}

/**
 * A message for a failed request: the server's own explanation when it gave
 * one, otherwise `fallback`. Always a string — safe to pass to a toast.
 */
export function errorMessage(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail
  return detailText(detail) ?? fallback
}
