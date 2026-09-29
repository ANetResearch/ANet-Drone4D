#!/usr/bin/env node
// use-mobile codemod (M15-FR-083): the upstream hook mirrors matchMedia into state with a synchronous setState inside
// useEffect (oxlint react/set-state-in-effect) and returns false on the first render. Replace it with a
// useSyncExternalStore subscription: same export, same breakpoint, correct first render, no cascading render.
// The hook lives in <uiDir>/../../hooks (aliases.hooks = @/ui/hooks). Idempotent (marker).
// Usage: node tools/shadcn/codemods/use-mobile.mjs [--dry] [uiDir]
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { UI_DIR } from './_lib.mjs'

const MARKER = '// M15 use-mobile: useSyncExternalStore'

export function transform(src) {
  if (src.includes(MARKER)) return src
  const bp = /const MOBILE_BREAKPOINT = (\d+)/.exec(src)
  if (!bp || !/export function useIsMobile\(\)/.test(src)) throw new Error('use-mobile codemod: upstream structure moved (M15 §11 K3)')
  return `${MARKER}
import * as React from "react"

const MOBILE_BREAKPOINT = ${bp[1]}
const QUERY = \`(max-width: \${MOBILE_BREAKPOINT - 1}px)\`

function subscribe(onChange: () => void) {
  const mql = window.matchMedia(QUERY)
  mql.addEventListener("change", onChange)
  return () => mql.removeEventListener("change", onChange)
}

const snapshot = () => window.innerWidth < MOBILE_BREAKPOINT
const serverSnapshot = () => false

export function useIsMobile() {
  return React.useSyncExternalStore(subscribe, snapshot, serverSnapshot)
}
`
}

export function run(dir = UI_DIR, { dry = false } = {}) {
  const p = join(dir, '..', '..', 'hooks', 'use-mobile.ts')
  if (!existsSync(p)) return []
  const src = readFileSync(p, 'utf8')
  const out = transform(src)
  if (out === src) return []
  if (!dry) writeFileSync(p, out)
  return ['use-mobile']
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`use-mobile codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
