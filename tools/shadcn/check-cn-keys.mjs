#!/usr/bin/env node
// CN-01 (M15 §6.13; g07 §3.4): every namespace key that an @theme block of apps/web/src/styles/** adds for text, ease,
// blur, shadow, font or transition-duration must be registered in apps/web/src/lib/utils.ts (createCn), otherwise
// cn() resolves class conflicts wrongly (text-hud-sub taken for a colour, text-sm winning over text-hud-title).
// Colour keys are exempt (the colour scale accepts any name). Exit 0 ok, 1 missing registrations.
// Usage: node tools/shadcn/check-cn-keys.mjs
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const STYLES = join(ROOT, 'apps', 'web', 'src', 'styles')
const UTILS = join(ROOT, 'apps', 'web', 'src', 'lib', 'utils.ts')
const NS = { text: 'text', ease: 'ease', blur: 'blur', shadow: 'shadow', font: 'font', 'transition-duration': 'duration' }

function cssFiles(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n)
    return statSync(p).isDirectory() ? cssFiles(p) : n.endsWith('.css') ? [p] : []
  })
}

export function themeKeys(css) {
  const keys = new Map()
  const src = css.replace(/\/\*[\s\S]*?\*\//g, '')
  for (const m of src.matchAll(/@theme(?:\s+(?:static|inline))*\s*\{([\s\S]*?)\n\}/g)) {
    for (const d of m[1].matchAll(/--(text|ease|blur|shadow|font|transition-duration)-([a-z0-9]+(?:-[a-z0-9]+)*)\s*:/g)) {
      if (d[0].includes('--', 2 + d[1].length + 1)) continue
      const ns = NS[d[1]]
      if (!keys.has(ns)) keys.set(ns, new Set())
      keys.get(ns).add(d[2])
    }
  }
  return keys
}

export function registered(ts) {
  const out = new Map()
  const theme = /CN_THEME\s*=\s*\{([\s\S]*?)\}\s*as const/.exec(ts)?.[1] ?? ''
  for (const m of theme.matchAll(/(\w+)\s*:\s*\[([\s\S]*?)\]/g)) out.set(m[1], new Set([...m[2].matchAll(/'([^']+)'/g)].map((x) => x[1])))
  const dur = /CN_DURATIONS\s*=\s*\[([\s\S]*?)\]/.exec(ts)?.[1] ?? ''
  out.set('duration', new Set([...dur.matchAll(/'([^']+)'/g)].map((x) => x[1])))
  return out
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const reg = registered(readFileSync(UTILS, 'utf8'))
  let missing = 0
  for (const f of cssFiles(STYLES)) {
    for (const [ns, set] of themeKeys(readFileSync(f, 'utf8'))) {
      for (const k of set) {
        if (k.includes('--')) continue
        if (!reg.get(ns)?.has(k)) {
          missing++
          console.log(`${relative(ROOT, f)}: CN-01 @theme key ${ns}-${k} is not registered in apps/web/src/lib/utils.ts`)
        }
      }
    }
  }
  if (missing) process.exit(1)
  console.log('check-cn-keys: ok')
}
