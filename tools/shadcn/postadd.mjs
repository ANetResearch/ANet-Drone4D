#!/usr/bin/env node
// postadd (M15-FR-083, NFR-019; d04 §6 item 4): run after every `shadcn add` (tools/shadcn/install.mjs calls it).
//   1. all codemods in a fixed order (each idempotent; see tools/shadcn/PATCHES.md);
//   2. dependency check: lucide-react must not be declared anywhere (ADR-030); it is removed from the manifests if found;
//   3. `tsc --noEmit -p apps/web/tsconfig.json` (skipped with --no-tsc).
// --check : dry run; exit 1 if any codemod would still change a file (M15-AC-045 "second run diff is 0").
import { spawnSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { ROOT, UI_DIR } from './codemods/_lib.mjs'
import * as icons from './codemods/icons.mjs'
import * as cn from './codemods/cn.mjs'
import * as motion from './codemods/motion.mjs'
import * as colors from './codemods/colors.mjs'
import * as zLayers from './codemods/z-layers.mjs'
import * as destructive from './codemods/destructive-alpha.mjs'
import * as sidebar from './codemods/sidebar-overlay.mjs'
import * as accordion from './codemods/accordion-single-chevron.mjs'
import * as tabs from './codemods/tabs-indicator.mjs'
import * as toggleGroup from './codemods/toggle-group-indicator.mjs'
import * as popover from './codemods/popover-anchor.mjs'
import * as ts6133 from './codemods/scroll-area-ts6133.mjs'
import * as useMobile from './codemods/use-mobile.mjs'
import * as fontHeading from './codemods/font-heading.mjs'

export const CODEMODS = [
  ['icons', icons], ['cn', cn], ['motion', motion], ['colors', colors], ['z-layers', zLayers], ['destructive-alpha', destructive],
  ['sidebar-overlay', sidebar], ['accordion-single-chevron', accordion], ['tabs-indicator', tabs],
  ['toggle-group-indicator', toggleGroup], ['popover-anchor', popover], ['scroll-area-ts6133', ts6133],
  ['use-mobile', useMobile], ['font-heading', fontHeading],
]

function dropLucideReact(dry) {
  const found = []
  for (const m of ['package.json', 'apps/web/package.json']) {
    const p = join(ROOT, m)
    const pj = JSON.parse(readFileSync(p, 'utf8'))
    let hit = false
    for (const sect of ['dependencies', 'devDependencies', 'optionalDependencies']) {
      if (pj[sect]?.['lucide-react']) {
        hit = true
        delete pj[sect]['lucide-react']
      }
    }
    if (hit) {
      found.push(m)
      if (!dry) writeFileSync(p, JSON.stringify(pj, null, 2) + '\n')
    }
  }
  return found
}

export function runAll({ dry = false, dir = UI_DIR } = {}) {
  const report = []
  for (const [name, mod] of CODEMODS) {
    const changed = mod.run(dir, { dry })
    report.push([name, changed])
  }
  return report
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const check = process.argv.includes('--check')
  const report = runAll({ dry: check })
  let pending = 0
  for (const [name, changed] of report) {
    pending += changed.length
    console.log(`postadd ${name}: ${changed.length ? changed.join(', ') : '-'}`)
  }
  const lucide = dropLucideReact(check)
  if (lucide.length) console.log(`postadd: lucide-react ${check ? 'declared in' : 'removed from'} ${lucide.join(', ')} (ADR-030); run npm install to sync the lock`)
  if (check) {
    if (pending || lucide.length) {
      console.error(`postadd --check: ${pending} pending codemod change(s)`)
      process.exit(1)
    }
    console.log('postadd --check: idempotent, nothing to change')
    process.exit(0)
  }
  if (!process.argv.includes('--no-tsc')) {
    const r = spawnSync('npx', ['--no-install', 'tsc', '-p', 'apps/web/tsconfig.json', '--noEmit'], { cwd: ROOT, stdio: 'inherit' })
    if (r.status !== 0) process.exit(r.status ?? 1)
  }
}
