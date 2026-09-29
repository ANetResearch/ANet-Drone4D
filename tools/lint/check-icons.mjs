#!/usr/bin/env node
// Icon lint (AWR-15 §7; ADR-030; AWR-18 §13.1). Scope: apps/web/{src,tests,perf,dev}/**.
//   ICON-01 import of lucide-react (anywhere)
//   ICON-02 `import { icons } from 'lucide'`, namespace or default import of lucide (breaks tree shaking)
//   ICON-03 lucide names that are aliases rather than canonical names (lucide 1.x demoted Home, AlertTriangle, Loader2 ...)
//   ICON-04 semantic icon keys used in source (<Icon name="tl.play">, icon: 'tl.play') that are not in ui/icons/registry.ts
// Canonical names come from node_modules/lucide (file name in PascalCase); aliases from its iconsAndAliases export table.
// Until M15 delivers ui/icons/registry.ts, ICON-04 is skipped with a note.
// Usage: node tools/lint/check-icons.mjs [files...]
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { ROOT, Reporter, WEB_SRC, isCode, isMain, lineCol, readText, run, stripComments, walk } from './_common.mjs'

const REGISTRY = `${WEB_SRC}/ui/icons/registry.ts`
const pascal = (kebab) => kebab.split('-').map((s) => s.charAt(0).toUpperCase() + s.slice(1)).join('')

export function lucideNames(root = ROOT) {
  const dir = join(root, 'node_modules', 'lucide', 'dist', 'esm', 'icons')
  const canonical = new Set()
  const aliasOf = new Map()
  if (!existsSync(dir)) return null
  for (const n of readdirSync(dir)) if (n.endsWith('.mjs')) canonical.add(pascal(n.slice(0, -4)))
  const table = join(root, 'node_modules', 'lucide', 'dist', 'esm', 'iconsAndAliases.mjs')
  if (existsSync(table)) {
    const src = readFileSync(table, 'utf8')
    for (const m of src.matchAll(/export \{([^}]+)\} from '\.\/icons\/([a-z0-9-]+)\.mjs'/g)) {
      const canon = pascal(m[2])
      for (const part of m[1].split(',')) {
        const name = part.replace(/default as/, '').trim()
        if (name && name !== canon) aliasOf.set(name, canon)
      }
    }
  }
  return { canonical, aliasOf }
}

export function registryKeys(text) {
  const keys = new Set()
  for (const m of text.matchAll(/['"]([a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+)['"]\s*:/g)) keys.add(m[1])
  return keys
}

const LUCIDE_IMPORT = /\bimport\s+((?:(?!\bfrom\b|;|\bimport\b)[\s\S])*?)\s+from\s+['"]lucide(?:\/[^'"]*)?['"]/g
const USE_KEY = /<(?:Icon|StateIcon)\b[^>]*?\bname=\{?['"]([^'"]+)['"]|\bicon(?:Key)?\s*[:=]\s*\{?['"]([a-z][a-z0-9]*(?:\.[a-z0-9-]+)+)['"]/g

export function checkFile(f, text, R, ctx) {
  if (!isCode(f)) return
  const src = stripComments(text)
  for (const m of src.matchAll(/(?:from|import|require\()\s*['"]lucide-react(?:\/[^'"]*)?['"]/g)) {
    const [l, c] = lineCol(src, m.index)
    R.add(f, l, c, 'ICON-01', 'lucide-react is forbidden (ADR-030); use ui/icons (Icon, StateIcon, lucide-compat)')
  }
  for (const m of src.matchAll(LUCIDE_IMPORT)) {
    const [l, c] = lineCol(src, m.index)
    const clause = m[1]
    if (/^\*\s+as\s+/.test(clause) || /^[A-Za-z_$][\w$]*\s*(,|$)/.test(clause)) {
      R.add(f, l, c, 'ICON-02', 'namespace or default import of lucide; import named IconNodes only')
      continue
    }
    const names = (/\{([\s\S]*)\}/.exec(clause)?.[1] ?? '').split(',').map((s) => s.trim().split(/\s+as\s+/)[0]).filter(Boolean)
    for (const n of names) {
      if (n === 'icons') R.add(f, l, c, 'ICON-02', "`import { icons } from 'lucide'` pulls every icon; import named IconNodes")
      else if (ctx.lucide && n !== 'createElement' && !/^type\s/.test(n)) {
        if (ctx.lucide.aliasOf.has(n)) R.add(f, l, c, 'ICON-03', `${n} is an alias; use the canonical name ${ctx.lucide.aliasOf.get(n)}`)
        else if (!ctx.lucide.canonical.has(n) && /^[A-Z]/.test(n)) R.add(f, l, c, 'ICON-03', `${n} is not an icon of lucide ${ctx.lucideVersion ?? ''}`.trim())
      }
    }
  }
  if (ctx.keys && f !== REGISTRY) {
    for (const m of src.matchAll(USE_KEY)) {
      const key = m[1] ?? m[2]
      if (!ctx.keys.has(key)) {
        const [l, c] = lineCol(src, m.index)
        R.add(f, l, c, 'ICON-04', `icon key "${key}" is not registered in ui/icons/registry.ts`)
      }
    }
  }
}

if (isMain(import.meta.url)) {
  await run('check-icons', async () => {
    const explicit = process.argv.slice(2).filter((a) => !a.startsWith('--'))
    const files = explicit.length ? explicit : ['apps/web/src', 'apps/web/tests', 'apps/web/perf', 'apps/web/dev'].flatMap((d) => walk(d, isCode))
    const R = new Reporter('check-icons')
    const lucide = lucideNames()
    if (!lucide) R.note('node_modules/lucide not installed; ICON-03 skipped (run make setup)')
    let lucideVersion = null
    try {
      lucideVersion = JSON.parse(readFileSync(join(ROOT, 'node_modules', 'lucide', 'package.json'), 'utf8')).version
    } catch {
      // version only decorates messages
    }
    let keys = null
    if (existsSync(join(ROOT, REGISTRY))) keys = registryKeys(readText(REGISTRY))
    else R.note(`${REGISTRY} does not exist yet (M15); ICON-04 skipped`)
    for (const f of files) {
      const t = readText(f)
      if (t !== null) checkFile(f, t, R, { lucide, lucideVersion, keys })
    }
    R.finish({ files: files.length, registry_keys: keys ? keys.size : null, lucide: lucideVersion })
  })
}
