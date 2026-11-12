import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

// Unit tests cover the logic that is deliberately free of the DOM: the voice
// activity detector, the turn-state reducer, chunk sequencing, message
// reconciliation and the socket's reconnect rules. There is no browser test
// framework here — anything that needs a real <video> or microphone is
// checked by hand.
export default defineConfig({
  resolve: {
    alias: { '@': fileURLToPath(new URL('./', import.meta.url)) },
  },
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
  },
})
