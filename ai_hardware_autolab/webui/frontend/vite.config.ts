import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Two modes, and they differ in where the token comes from.
 *
 * Built (`npm run build`, served by FastAPI with `--static dist`): the page is
 * on the same origin as the API, and it reads its token out of its own URL.
 *
 * Dev (`npm run dev`): the page is on 5173 and the API is on 8787, so every
 * call is cross-origin and the backend's Origin allowlist would refuse it. The
 * proxy below makes `/api` and `/ws` same-origin again from the browser's
 * point of view. Start the backend with
 * `--dev-origin http://127.0.0.1:5173` so the WebSocket handshake, which the
 * proxy forwards with its own Origin, is allowed too.
 */
const backend = process.env.QKD_WEBUI_BACKEND ?? 'http://127.0.0.1:8787'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: backend, changeOrigin: false },
      '/ws': { target: backend, ws: true, changeOrigin: false },
    },
  },
  build: {
    rollupOptions: { input: { main: 'index.html', knowledge: 'knowledge.html' } },
    outDir: 'dist',
    sourcemap: true,
    // elkjs is most of the bundle and it is a bundled layout engine, not
    // something to trim. The default 500 kB warning is about download cost
    // over a network; this bundle is served from 127.0.0.1 by the same
    // process that holds the daemon connection, so there is no network to be
    // slow. Raised rather than silenced, so a genuinely surprising jump still
    // shows up.
    chunkSizeWarningLimit: 2048,
  },
})
