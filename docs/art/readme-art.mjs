/**
 * Generates the README artwork in docs/images/ from the Zetta 3D brand tokens:
 *   banner-dark.svg, banner-light.svg      the header
 *   social-preview.png                     1280×640 card for the GitHub repository settings (needs Chrome)
 *
 *   node docs/art/readme-art.mjs
 *
 * The same system as the Unity plugin's and the CLI's README headers. Fonts (Figtree, Geist, Geist Mono; SIL Open
 * Font License) are embedded so the SVGs look the same on GitHub as in the brand kit. Blue is a fill, a dot or
 * text — never a stroke. The screenshots beside them come from docs/art/screenshots.sh.
 */
import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const OUT = join(HERE, '..', 'images')
const font = (file) => readFileSync(join(HERE, 'fonts', file)).toString('base64')
const FONTS = `
  @font-face { font-family: 'Figtree'; font-weight: 400 900; src: url(data:font/woff2;base64,${font('figtree-latin.woff2')}) format('woff2'); }
  @font-face { font-family: 'Geist'; font-weight: 400 700; src: url(data:font/woff2;base64,${font('geist-latin.woff2')}) format('woff2'); }
  @font-face { font-family: 'Geist Mono'; font-weight: 400 600; src: url(data:font/woff2;base64,${font('geist-mono-latin.woff2')}) format('woff2'); }`

const T = {
  ink: '#15130F',
  inkSoft: '#2A2620',
  umber: '#55514A',
  stone: '#C2BCAE',
  sand: '#DFDACE',
  linen: '#EFEBE2',
  cream: '#F7F4EE',
  paper: '#FAF8F4',
  signal: '#1677FF',
  signalDeep: '#0E58C0',
  signalLight: '#69B1FF',
  success: '#3C7A5B',
}

const DISPLAY = `'Figtree', 'Helvetica Neue', Arial, sans-serif`
const SANS = `'Geist', -apple-system, 'Segoe UI', Arial, sans-serif`
const MONO = `'Geist Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace`

/** The three-plane Zetta mark, scaled into a box at (x, y). */
function mark(x, y, size, fill) {
  const s = size / 200
  return `<g transform="translate(${x} ${y}) scale(${s})" fill="${fill}">
    <path d="M 26 48 L 122 28 L 174 48 L 78 68 Z"/>
    <path d="M 26 100 L 122 80 L 174 100 L 78 120 Z"/>
    <path d="M 26 152 L 122 132 L 174 152 L 78 172 Z"/>
  </g>`
}

const dots = (id, color, opacity) =>
  `<pattern id="${id}" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1.1" fill="${color}" opacity="${opacity}"/></pattern>`

const esc = (text) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

// ── Banner ──────────────────────────────────────────────────────────────────

function banner(theme) {
  const dark = theme === 'dark'
  const bg = dark ? T.ink : T.cream
  const fg = dark ? T.paper : T.ink
  const muted = dark ? T.stone : T.umber
  const card = dark ? '#1E1B16' : T.paper
  const cardLine = dark ? '#34302A' : T.sand
  const well = dark ? '#262219' : T.linen
  const W = 1280
  const H = 420

  // The Zetta 3D sidebar on the right: the account, a prompt on its way to a model, and the jobs it is following.
  const x = 742
  const y = 48
  const w = 476
  const h = 324
  const job = (cy, colour, title, status) => `
    <circle cx="${x + 38}" cy="${cy - 4}" r="4" fill="${colour}"/>
    <text x="${x + 52}" y="${cy}" font-family="${SANS}" font-size="13.5" fill="${fg}">${esc(title)}</text>
    <text x="${x + w - 34}" y="${cy}" text-anchor="end" font-family="${SANS}" font-size="13" fill="${muted}">${esc(status)}</text>`
  const sidebar = `<g>
    <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="14" fill="${card}" stroke="${cardLine}"/>
    ${mark(x + 18, y + 14, 22, fg)}
    <text x="${x + 46}" y="${y + 30}" font-family="${SANS}" font-weight="600" font-size="14" fill="${fg}">Zetta 3D</text>
    <text x="${x + w - 20}" y="${y + 30}" text-anchor="end" font-family="${SANS}" font-size="13" fill="${muted}">Pro plan · 2,450 credits</text>
    <line x1="${x}" y1="${y + 46}" x2="${x + w}" y2="${y + 46}" stroke="${cardLine}"/>

    <text x="${x + 20}" y="${y + 74}" font-family="${SANS}" font-weight="600" font-size="14.5" fill="${fg}">Generate 3D</text>
    <rect x="${x + 20}" y="${y + 86}" width="${w - 40}" height="34" rx="8" fill="${well}"/>
    <text x="${x + 34}" y="${y + 108}" font-family="${SANS}" font-size="14" fill="${fg}">A pirate treasure chest, stylized</text>
    <rect x="${x + 20}" y="${y + 130}" width="${(w - 40) / 2}" height="30" rx="8" fill="${T.signal}"/>
    <text x="${x + 20 + (w - 40) / 4}" y="${y + 150}" text-anchor="middle" font-family="${SANS}" font-weight="600" font-size="13" fill="#FFFFFF">Zetta N2.5</text>
    <text x="${x + 20 + (3 * (w - 40)) / 4}" y="${y + 150}" text-anchor="middle" font-family="${SANS}" font-size="13" fill="${muted}">Zetta N1</text>
    <rect x="${x + 20}" y="${y + 170}" width="${w - 40}" height="36" rx="18" fill="${fg}"/>
    <text x="${x + w / 2}" y="${y + 193}" text-anchor="middle" font-family="${SANS}" font-weight="600" font-size="14" fill="${bg}">Generate · 25 credits</text>

    <rect x="${x + 20}" y="${y + 222}" width="${w - 40}" height="84" rx="12" fill="${well}"/>
    <text x="${x + 34}" y="${y + 244}" font-family="${SANS}" font-weight="600" font-size="12.5" fill="${muted}">Jobs</text>
    ${job(y + 268, T.signal, 'Sci-fi helmet', 'Generating…')}
    ${job(y + 292, T.success, 'Wooden barrel', 'Imported at the 3D cursor')}
  </g>`

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Zetta 3D for Blender: AI 3D models inside Blender">
  <title>Zetta 3D for Blender</title>
  <defs>
    <style>${FONTS}</style>
    ${dots('dots', dark ? T.paper : T.ink, dark ? 0.07 : 0.08)}
    <radialGradient id="glow" cx="0.78" cy="0.45" r="0.55">
      <stop offset="0" stop-color="${T.signal}" stop-opacity="${dark ? 0.22 : 0.12}"/>
      <stop offset="1" stop-color="${T.signal}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="${W}" height="${H}" rx="20" fill="${bg}"/>
  <rect width="${W}" height="${H}" rx="20" fill="url(#dots)"/>
  <rect width="${W}" height="${H}" rx="20" fill="url(#glow)"/>

  ${mark(64, 56, 54, fg)}
  <text x="128" y="94" font-family="${DISPLAY}" font-weight="700" font-size="30" letter-spacing="-0.6" fill="${fg}">Zetta 3D</text>
  <rect x="262" y="70" width="124" height="30" rx="8" fill="${T.signal}"/>
  <text x="324" y="91" text-anchor="middle" font-family="${MONO}" font-weight="600" font-size="16" fill="#FFFFFF">for Blender</text>

  <text x="64" y="196" font-family="${DISPLAY}" font-weight="800" font-size="52" letter-spacing="-1.8" fill="${fg}">AI 3D models and pictures,</text>
  <text x="64" y="256" font-family="${DISPLAY}" font-weight="800" font-size="52" letter-spacing="-1.8" fill="${fg}">right inside Blender.</text>
  <text x="64" y="308" font-family="${SANS}" font-size="20" fill="${muted}">Generate, finish and import 3D models without leaving</text>
  <text x="64" y="338" font-family="${SANS}" font-size="20" fill="${muted}">Blender, and chain the tools into pipelines.</text>

  ${sidebar}
</svg>
`
}

writeFileSync(join(OUT, 'banner-dark.svg'), banner('dark'))
writeFileSync(join(OUT, 'banner-light.svg'), banner('light'))

// Social preview: the dark banner, centred on a 1280×640 card.
const social = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 640" width="1280" height="640">
  <rect width="1280" height="640" fill="${T.ink}"/>
  <image href="data:image/svg+xml;base64,${Buffer.from(banner('dark')).toString('base64')}" x="0" y="110" width="1280" height="420"/>
</svg>`
const chrome = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', 'google-chrome', 'chromium'].find((c) =>
  c.startsWith('/') ? existsSync(c) : spawnSync('which', [c]).status === 0)
if (chrome) {
  const tmp = join(OUT, '.social.svg')
  writeFileSync(tmp, social)
  spawnSync(chrome, ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=1',
    `--screenshot=${join(OUT, 'social-preview.png')}`, '--window-size=1280,640', '--virtual-time-budget=2000', `file://${tmp}`],
    { stdio: 'ignore', timeout: 60_000 })
  rmSync(tmp, { force: true })
}
console.log(`Wrote docs/images: banner-dark.svg, banner-light.svg${chrome ? ', social-preview.png' : ' (no Chrome: skipped social-preview.png)'}`)
