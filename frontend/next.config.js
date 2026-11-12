/** @type {import('next').NextConfig} */

// Sent on every response. No Content-Security-Policy here yet: the app
// connects to a separately-hosted API and WebSocket whose origins are only
// known at build time, and a CSP that is wrong breaks the product outright —
// it needs its own change, with a report-only rollout.
const SECURITY_HEADERS = [
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  // Nothing embeds this app; refuse to be framed (clickjacking).
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  // The app records audio; it uses nothing else.
  { key: 'Permissions-Policy', value: 'microphone=(self), camera=(), geolocation=(), interest-cohort=()' },
]

const nextConfig = {
  reactStrictMode: true,
  // Emit .next/standalone — a self-contained server the Docker image runs
  // without shipping the full node_modules tree.
  output: 'standalone',
  images: {
    // Images are served as-is; the /_next/image optimizer is not used.
    //
    // Every image in this app is a user's avatar, served by the backend (or
    // its bucket) already resized to a 256 px thumbnail and a 512 px portrait.
    // Routing them through the optimizer made THIS server fetch them, and it
    // refused: Next will not fetch from a private address, which is exactly
    // what the backend is on a single host or a compose network. In a
    // production build every avatar thumbnail came back 400 — invisible in
    // development, where optimisation was already off. Serving them directly
    // also removes a server-side fetch of user-influenced URLs, and the
    // allowlist of remote hosts that had to be kept in step with deployment.
    unoptimized: true,
  },
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000',
    // Empty unless set: lib/api.ts then derives the WebSocket URL from the API URL.
    NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL || '',
  },
  async headers() {
    return [{ source: '/:path*', headers: SECURITY_HEADERS }]
  },
};

module.exports = nextConfig;
