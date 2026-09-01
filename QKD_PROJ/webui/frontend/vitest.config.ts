import { defineConfig } from 'vitest/config'

/**
 * Separate from `vite.config.ts` on purpose: the dev server's proxy is about
 * reaching a running backend, and these tests deliberately reach nothing. They
 * cover the pure halves of the client contract -- where the token comes from,
 * and what the socket refuses to apply -- which is the part that has to be
 * right whether or not a daemon is up.
 */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
