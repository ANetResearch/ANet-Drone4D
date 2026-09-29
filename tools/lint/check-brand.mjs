#!/usr/bin/env node
// Brand checks (AWR-15 §4.8; ADR-032; AWR-18 §13.1). BRAND-04 (rendered sizes and computed styles) is the Playwright
// spec brand.spec.ts owned by M15; this tool implements BRAND-01..03:
//   BRAND-01 every file in apps/web/public/brand/ matches brand.lock.json (sha256), and anet-logo.svg is byte-identical to
//            refs/design/ANet/docs/media/anet-logo.svg (not recoloured)
//   BRAND-02 brand assets are referenced only from the AWR-15 §4.2 placements (AppHeader, loading overlay, about dialog,
//            empty states, report cover, favicon in index.html); the badge must not be rendered inside [data-viewport]
//   BRAND-03 no avatars.githubusercontent.com anywhere under apps/web
// Until M15 delivers public/brand/brand.lock.json the lock comparison is reported as a note, not a failure
// (use --strict to fail).
// Usage: node tools/lint/check-brand.mjs [--strict]
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { ROOT, Reporter, isBinaryPath, isMain, lineCol, readText, run, stripComments, walk } from './_common.mjs'

const BRAND_DIR = 'apps/web/public/brand'
const LOCK = `${BRAND_DIR}/brand.lock.json`
const REF_LOGO = 'refs/design/ANet/docs/media/anet-logo.svg'
const ASSET_RE = /(?:\/brand\/[\w.-]*|anet-logo\.svg|avatar-(?:96|460)\.png|anet-avatar[\w.-]*)/g
// AWR-15 §4.2 placements (file name patterns under apps/web/src) plus index.html for the favicon
const PLACEMENTS = [/\/ui\/layout\/AppHeader[^/]*$/, /LoadingOverlay[^/]*$/, /LoadingMask[^/]*$/, /AboutDialog[^/]*$/, /EmptyState[^/]*$/,
  /ReportCover[^/]*$/, /\/ui\/brand\/[^/]+$/, /^apps\/web\/index\.html$/]
const sha256 = (buf) => createHash('sha256').update(buf).digest('hex')
// Module specifiers of import / export / import() are code paths (for example `@/ui/brand/PanelEmpty`), not asset
// references; they count only when the specifier itself names an image file (request M15-to-M00 item 3).
const SPEC_RE = /\b(?:from|import)\s*\(?\s*(['"])([^'"\n]+)\1/g
const ASSET_FILE_RE = /\.(?:svg|png|jpe?g|webp|gif|ico)(?:\?[^'"]*)?$/i
function moduleSpecRanges(src) {
  const out = []
  for (const m of src.matchAll(SPEC_RE)) {
    if (ASSET_FILE_RE.test(m[2])) continue
    const start = m.index + m[0].lastIndexOf(m[1] + m[2])
    out.push([start, start + m[2].length + 2])
  }
  return out
}

export function checkSources(files, R, read = readText) {
  for (const f of files) {
    const text = read(f)
    if (text === null) continue
    const src = /\.(ts|tsx|js|jsx|mjs|css)$/.test(f) ? stripComments(text, { css: f.endsWith('.css') }) : text
    for (const m of src.matchAll(/avatars\.githubusercontent\.com/g)) {
      const [l, c] = lineCol(src, m.index)
      R.add(f, l, c, 'BRAND-03', 'remote avatar URL; brand assets are hosted locally in public/brand (ADR-032)')
    }
    if (!f.startsWith('apps/web/src/') && f !== 'apps/web/index.html') continue
    const placed = PLACEMENTS.some((re) => re.test(f))
    const specs = moduleSpecRanges(src)
    for (const m of src.matchAll(ASSET_RE)) {
      if (specs.some(([a, b]) => m.index >= a && m.index < b)) continue
      const [l, c] = lineCol(src, m.index)
      if (!placed) R.add(f, l, c, 'BRAND-02', `brand asset referenced outside the AWR-15 §4.2 placements (${m[0]})`)
    }
    if (placed && /data-viewport/.test(src) && /anet-logo\.svg/.test(src)) {
      const i = src.search(/data-viewport/)
      const [l, c] = lineCol(src, i)
      R.add(f, l, c, 'BRAND-02', 'the brand badge must not be rendered inside [data-viewport]')
    }
  }
}

export function checkLock(R, strict) {
  const dir = join(ROOT, BRAND_DIR)
  if (!existsSync(dir)) {
    R.note(`${BRAND_DIR} does not exist yet (M15); BRAND-01 skipped`)
    return
  }
  const assets = readdirSync(dir).filter((n) => !n.startsWith('.') && n !== 'brand.lock.json')
  if (!existsSync(join(ROOT, LOCK))) {
    if (assets.length && strict) R.add(LOCK, 1, 1, 'BRAND-01', 'brand.lock.json is missing (sha256, source and date per asset, AWR-15 §4.1)')
    else R.note(`${LOCK} not delivered yet (M15); ${assets.length} asset(s) unchecked`)
  } else {
    let lock
    try {
      lock = JSON.parse(readFileSync(join(ROOT, LOCK), 'utf8'))
    } catch (e) {
      R.add(LOCK, 1, 1, 'BRAND-01', `brand.lock.json is not valid JSON (${e.message})`)
      return
    }
    const entries = Array.isArray(lock) ? lock : Array.isArray(lock.files) ? lock.files : Object.entries(lock.files ?? lock).map(([name, v]) => ({ name, ...(typeof v === 'object' ? v : { sha256: v }) }))
    const byName = new Map(entries.map((e) => [e.name ?? e.file ?? e.path, e]))
    for (const n of assets) {
      const e = byName.get(n) ?? byName.get(`${BRAND_DIR}/${n}`) ?? byName.get(`public/brand/${n}`)
      const got = sha256(readFileSync(join(dir, n)))
      if (!e) R.add(`${BRAND_DIR}/${n}`, 1, 1, 'BRAND-01', 'asset is not listed in brand.lock.json')
      else if (String(e.sha256).toLowerCase() !== got) R.add(`${BRAND_DIR}/${n}`, 1, 1, 'BRAND-01', `sha256 ${got.slice(0, 12)} differs from brand.lock.json`)
    }
    for (const k of byName.keys()) {
      const base = String(k).split('/').pop()
      if (!assets.includes(base)) R.add(LOCK, 1, 1, 'BRAND-01', `locked asset ${base} is missing from ${BRAND_DIR}`)
    }
  }
  const logo = join(dir, 'anet-logo.svg')
  if (existsSync(logo) && existsSync(join(ROOT, REF_LOGO)) && sha256(readFileSync(logo)) !== sha256(readFileSync(join(ROOT, REF_LOGO)))) {
    R.add(`${BRAND_DIR}/anet-logo.svg`, 1, 1, 'BRAND-01', `anet-logo.svg differs from ${REF_LOGO} (recoloured or re-exported)`)
  }
}

if (isMain(import.meta.url)) {
  await run('check-brand', async () => {
    const strict = process.argv.includes('--strict')
    const R = new Reporter('check-brand')
    checkLock(R, strict)
    const files = walk('apps/web', (r) => !isBinaryPath(r) && !r.startsWith('apps/web/public/') && /\.(ts|tsx|js|jsx|mjs|css|html|json|md)$/.test(r))
    checkSources(files, R)
    R.finish({ files: files.length })
  })
}
