/**
 * Lightweight Core Web Vitals reporter.
 *
 * Captures LCP (Largest Contentful Paint), CLS (Cumulative Layout Shift),
 * and INP (Interaction to Next Paint) using native PerformanceObserver —
 * no third-party dependency. Emits a single console log per metric when
 * the page becomes hidden so we don't fragment data with per-tick reports.
 *
 * For production, swap the `report()` body to POST to your analytics
 * endpoint or call a third-party SDK (Datadog RUM, Sentry, etc.).
 */

type VitalName = 'LCP' | 'CLS' | 'INP'
interface Vital {
  name: VitalName
  value: number
  rating: 'good' | 'needs-improvement' | 'poor'
}

// Thresholds match web.dev's "good / needs improvement / poor" buckets
// (Aug 2024 update — INP replaced FID as the responsiveness metric).
const THRESHOLDS: Record<VitalName, [number, number]> = {
  LCP: [2500, 4000],
  CLS: [0.1, 0.25],
  INP: [200, 500],
}

function rateVital(name: VitalName, value: number): Vital['rating'] {
  const [good, poor] = THRESHOLDS[name]
  return value <= good ? 'good' : value <= poor ? 'needs-improvement' : 'poor'
}

function report(v: Vital): void {
  // In dev, log to console. Replace with `fetch('/api/v1/analytics/vitals', …)`
  // when you have an analytics endpoint.
  if (process.env.NODE_ENV !== 'production') {
    console.info(`[vitals] ${v.name}=${v.value.toFixed(2)} (${v.rating})`)
  }
}

interface LayoutShiftEntry extends PerformanceEntry {
  value: number
  hadRecentInput: boolean
}

interface EventPerformanceEntry extends PerformanceEntry {
  interactionId?: number
}

/**
 * Initialize observers. Returns a teardown function — call it from the
 * effect's cleanup.
 *
 * The teardown is not optional housekeeping. React StrictMode mounts, unmounts
 * and remounts effects in development, so without it every observer and the
 * visibilitychange listener register twice and each metric is reported twice.
 * That is merely noisy against a console, and actively wrong once `report()`
 * POSTs to an analytics endpoint, as the docstring above suggests.
 *
 * Safe in SSR — returns a no-op if `window` is undefined.
 */
export function initWebVitals(): () => void {
  if (typeof window === 'undefined' || !('PerformanceObserver' in window)) {
    return () => {}
  }

  const observers: PerformanceObserver[] = []

  // ── LCP — biggest above-the-fold element. Reported on visibilitychange.
  let lcpValue = 0
  try {
    const lcpObserver = new PerformanceObserver((list) => {
      const entries = list.getEntries()
      const last = entries[entries.length - 1] as PerformanceEntry & { renderTime?: number; loadTime?: number }
      lcpValue = last?.renderTime ?? last?.loadTime ?? last?.startTime ?? 0
    })
    observers.push(lcpObserver)
    lcpObserver.observe({ type: 'largest-contentful-paint', buffered: true })
  } catch { /* not supported */ }

  // ── CLS — sum of layout shifts grouped into 5s/1s sessions.
  let clsValue = 0
  let clsSession = 0
  let clsSessionStart = 0
  let clsLastEntry = 0
  try {
    const clsObserver = new PerformanceObserver((list) => {
      for (const entry of list.getEntries() as LayoutShiftEntry[]) {
        if (entry.hadRecentInput) continue // ignore user-initiated shifts
        const t = entry.startTime
        if (t - clsLastEntry > 1000 || t - clsSessionStart > 5000) {
          clsSession = 0
          clsSessionStart = t
        }
        clsSession += entry.value
        clsLastEntry = t
        if (clsSession > clsValue) clsValue = clsSession
      }
    })
    observers.push(clsObserver)
    clsObserver.observe({ type: 'layout-shift', buffered: true })
  } catch { /* not supported */ }

  // ── INP — longest interaction-to-paint duration (replaces FID).
  let inpValue = 0
  try {
    const eventObserver = new PerformanceObserver((list) => {
      for (const entry of list.getEntries() as EventPerformanceEntry[]) {
        if (entry.interactionId && entry.duration > inpValue) {
          inpValue = entry.duration
        }
      }
    })
    observers.push(eventObserver)
    eventObserver.observe({ type: 'event', buffered: true, durationThreshold: 16 } as PerformanceObserverInit)
  } catch { /* not supported */ }

  // Emit when the user leaves the page — single, reliable report point.
  const flush = () => {
    if (lcpValue > 0) report({ name: 'LCP', value: lcpValue, rating: rateVital('LCP', lcpValue) })
    if (clsValue > 0) report({ name: 'CLS', value: clsValue, rating: rateVital('CLS', clsValue) })
    if (inpValue > 0) report({ name: 'INP', value: inpValue, rating: rateVital('INP', inpValue) })
  }
  const onVisibilityChange = () => {
    if (document.visibilityState === 'hidden') flush()
  }
  document.addEventListener('visibilitychange', onVisibilityChange)

  return () => {
    document.removeEventListener('visibilitychange', onVisibilityChange)
    for (const o of observers) {
      try {
        o.disconnect()
      } catch {
        /* already gone */
      }
    }
  }
}
