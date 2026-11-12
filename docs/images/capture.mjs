// Retake the README screenshots from the running app.
//
//   BASE=http://localhost:3000 API=http://localhost:8000 PHOTO=/path/to/face.jpg \
//     node docs/images/capture.mjs
//
// Needs the app running (frontend + backend), Chrome, and playwright-core.
// See docs/images/README.md.
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'

const BASE = process.env.BASE || 'http://localhost:3000'
const API = process.env.API || 'http://localhost:8000'
const PHOTO = process.env.PHOTO
const NAME = process.env.AVATAR_NAME || 'Mona Lisa'
const CHROME = process.env.CHROME || '/usr/bin/google-chrome'
const OUT = new URL('./raw/', import.meta.url).pathname
if (!PHOTO) throw new Error('Set PHOTO to a portrait you have the right to publish.')
mkdirSync(OUT, { recursive: true })

// A named account, so the header shows a name rather than a guest id.
const creds = {
  email: process.env.DEMO_EMAIL || 'readme-demo@example.dev',
  username: process.env.DEMO_USER || 'readme_demo',
  password: process.env.DEMO_PASSWORD || 'Readme-Dem0-Passw0rd!',
  full_name: 'Demo',
}
await fetch(`${API}/api/v1/users/register`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify(creds),
}) // 409 on a second run is fine
const login = await (
  await fetch(`${API}/api/v1/users/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: creds.email, password: creds.password }),
  })
).json()
if (!login.access_token) throw new Error(`Login failed: ${JSON.stringify(login)}`)
const me = await (
  await fetch(`${API}/api/v1/users/me`, { headers: { authorization: `Bearer ${login.access_token}` } })
).json()

const browser = await chromium.launch({
  executablePath: CHROME,
  headless: true,
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--autoplay-policy=no-user-gesture-required'],
})
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
await ctx.addInitScript(
  ([token, user]) => {
    localStorage.setItem('avatar-system-storage', JSON.stringify({ state: { token, user, handsFree: false }, version: 0 }))
  },
  [login.access_token, me],
)
const page = await ctx.newPage()

// Toasts sit over the header; wait them out so they are not in the picture.
const settle = () => page.waitForTimeout(4500)
const shot = async (name) => {
  await page.screenshot({ path: `${OUT}${name}.png` })
  console.log('captured', name)
}
const nav = async (label) => {
  await page.locator('nav button', { hasText: label }).first().click()
  await page.waitForTimeout(1200)
}
const say = async (text, linger) => {
  const box = page.locator('textarea:visible').first()
  await box.fill(text)
  await box.press('Enter')
  await page
    .waitForFunction(() => {
      const v = document.querySelector('video')
      return v && !v.paused && v.currentTime > 0.2
    }, null, { timeout: 90000 })
    .catch(() => console.warn('the avatar never started speaking for:', text))
  await page.waitForTimeout(linger)
}

await page.goto(BASE, { waitUntil: 'networkidle' })
await settle()
await shot('home')

await nav('Avatars')
const card = page.getByRole('button', { name: new RegExp(NAME, 'i') })
if (!(await card.count())) {
  await page.locator('input[type=file]').first().setInputFiles(PHOTO)
  await page.waitForTimeout(600)
  await page.locator('#avatar-name').fill(NAME)
  await page.getByRole('button', { name: /upload avatar/i }).click()
  await page.waitForTimeout(4000)
}
await card.first().click()
await settle()
await shot('avatars')

await page.getByRole('button', { name: /start conversation/i }).click()
await page.waitForTimeout(3500)
await say(`Hi! Introduce yourself as the ${NAME}, in one short sentence.`, 8000)
await say('In two short sentences and plain words, what makes a portrait feel alive?', 2500)
await shot('chat')
await page.waitForTimeout(12000)

for (const view of ['Voice', 'History', 'Settings']) {
  await nav(view)
  await settle()
  await shot(view.toLowerCase())
}
await browser.close()
console.log(`\nRaw PNGs are in ${OUT} — convert them as described in docs/images/README.md.`)
