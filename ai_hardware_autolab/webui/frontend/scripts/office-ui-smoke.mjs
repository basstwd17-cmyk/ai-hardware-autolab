// node scripts/office-ui-smoke.mjs [output-directory]
// Uses the existing workspace Playwright installation; no production service.
import { createServer } from 'vite'
import react from '@vitejs/plugin-react'
import { createRequire } from 'node:module'
import { readFile, mkdir } from 'node:fs/promises'
import { dirname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? resolve(root, '../../../agent-platform/node_modules/playwright'))
const assets = resolve(root, '../assets')
const output = resolve(process.argv[2] ?? resolve(root, '../../../tmp/office-ui-smoke'))
await mkdir(output, { recursive: true })
const server = await createServer({ configFile: false, root, plugins: [react()], server: { host: '127.0.0.1', port: 18789, strictPort: true } })
let browser
try {
  await server.listen()
  browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1600, height: 1050 }, deviceScaleFactor: 1 })
  const errors = []
  page.on('pageerror', (error) => { errors.push(String(error)); console.error(String(error)) })
  await page.route('**/office-assets/**', async (route) => {
    const path = resolve(assets, decodeURIComponent(new URL(route.request().url()).pathname.slice('/office-assets/'.length)))
    assert.ok(path.startsWith(assets + sep))
    await route.fulfill({ body: await readFile(path), contentType: path.endsWith('.json') ? 'application/json' : 'image/png' })
  })
  // Hard fail if the offline fixture ever tries to talk to a real backend.
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => { errors.push('unexpected API request'); return route.abort() })
  await page.addInitScript(() => {
    let seed = 12
    Math.random = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296 }
  })
  await page.goto('http://127.0.0.1:18789/tests/office-smoke.html')
  await page.locator('.live-notes article').first().waitFor()
  assert.equal(await page.locator('.live-notes article').count(), 5)
  assert.match(await page.locator('.live-notes').innerText(), /테스트 명령 실패/)
  assert.match(await page.locator('.live-notes').innerText(), /AM335x PRU timing/)
  await page.locator('#history-stress[data-caught="true"]').waitFor({ timeout: 5000 })
  await page.waitForFunction(() => [...document.images].every((image) => image.complete))
  await page.waitForTimeout(800)
  await page.screenshot({ path: resolve(output, 'working.png') })
  await page.locator('#panel').click()
  await page.locator('#panel').click()
  await page.locator('.live-notes article').first().waitFor()
  assert.equal(await page.locator('.live-notes article').count(), 5)
  await page.clock.install()
  await page.locator('#rest').click()
  await page.clock.runFor(30000)
  await page.screenshot({ path: resolve(output, 'lounge.png') })
  await page.clock.runFor(20000)
  await page.screenshot({ path: resolve(output, 'lounge-later.png') })
  await page.locator('#work').click()
  await page.clock.runFor(20000)
  await page.screenshot({ path: resolve(output, 'returned.png') })
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ ok: true, notes: 5, emptySocketHistory: true, remount: true, fastCursorHistory: true, errors, output }))
} finally {
  if (browser) await browser.close()
  await server.close()
}
