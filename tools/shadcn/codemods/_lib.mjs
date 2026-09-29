// Shared helpers for the M15 shadcn codemods (M15-FR-083; tools/shadcn/PATCHES.md).
// Every codemod is idempotent: running it on already processed sources produces no change (M15-AC-045).
// Anchored rewrites fail loudly when the upstream structure moved (M15 §11 K3), unless their marker is present.
import { readFileSync, readdirSync, writeFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
export const UI_DIR = join(ROOT, 'apps', 'web', 'src', 'ui', 'components', 'ui')

export function uiFiles(dir = UI_DIR) {
  if (!existsSync(dir)) return []
  return readdirSync(dir).filter((n) => n.endsWith('.tsx') || n.endsWith('.ts')).sort().map((n) => join(dir, n))
}

/** Apply fn(src, name) to every file; write when changed (unless dry). Returns the list of changed file names. */
export function eachFile(dir, fn, { dry = false, only = null } = {}) {
  const changed = []
  for (const p of uiFiles(dir)) {
    const name = p.split('/').pop().replace(/\.tsx?$/, '')
    if (only && !only.includes(name)) continue
    const src = readFileSync(p, 'utf8')
    const out = fn(src, name)
    if (out !== src) {
      changed.push(name)
      if (!dry) writeFileSync(p, out)
    }
  }
  return changed
}

/** Patch one component file; returns true when it changed. Missing file is not an error (component not installed). */
export function patchFile(dir, name, fn, { dry = false } = {}) {
  const p = join(dir, `${name}.tsx`)
  if (!existsSync(p)) return false
  const src = readFileSync(p, 'utf8')
  const out = fn(src)
  if (out === src) return false
  if (!dry) writeFileSync(p, out)
  return true
}

/** Replace `needle` once; throw when absent (upstream drift). */
export function replaceOnce(src, needle, repl, what) {
  const i = src.indexOf(needle)
  if (i < 0) throw new Error(`${what}: anchor not found (upstream changed?): ${String(needle).slice(0, 80)}`)
  return src.slice(0, i) + repl + src.slice(i + needle.length)
}

/**
 * Visit the className string literals of every element that carries data-slot="<slot>".
 * Handles className="..." and className={cn("...", "...", cond ? "..." : "...", className)} (all literals in the call).
 * visit(classes, slot) returns the new class string.
 */
export function mapSlotClasses(src, slots, visit) {
  const re = /data-slot="([a-z0-9-]+)"/g
  let out = ''
  let last = 0
  for (let m = re.exec(src); m; m = re.exec(src)) {
    const slot = m[1]
    if (!slots.includes(slot)) continue
    const from = m.index + m[0].length
    const nextSlot = src.indexOf('data-slot=', from)
    const limit = Math.min(from + 600, nextSlot < 0 ? src.length : nextSlot)
    const ci = src.indexOf('className=', from)
    if (ci < 0 || ci > limit) continue
    const start = ci + 'className='.length
    let end
    let body
    if (src[start] === '"') {
      end = src.indexOf('"', start + 1) + 1
      body = src.slice(start, end)
      body = '"' + visit(body.slice(1, -1), slot) + '"'
    } else if (src.startsWith('{cn(', start)) {
      end = matchParen(src, start + 3) + 1
      body = src.slice(start, end).replace(/"([^"\n]*)"/g, (_s, c) => '"' + visit(c, slot) + '"')
    } else continue
    out += src.slice(last, start) + body
    last = end
    re.lastIndex = end
  }
  return out + src.slice(last)
}

function matchParen(src, open) {
  let depth = 0
  for (let i = open; i < src.length; i++) {
    const c = src[i]
    if (c === '"' || c === "'" || c === '`') {
      const q = c
      for (i++; i < src.length && src[i] !== q; i++) if (src[i] === '\\') i++
      continue
    }
    if (c === '(') depth++
    else if (c === ')' && --depth === 0) return i
  }
  throw new Error('unbalanced parentheses in className expression')
}

/** Drop class tokens matching re from a space separated class string (keeps single spaces). */
export function dropClasses(classes, re) {
  return classes.split(/\s+/).filter((t) => t && !re.test(t)).join(' ')
}

/** Map class tokens through fn (token => token | null to drop). */
export function mapClasses(classes, fn) {
  return classes.split(/\s+/).filter(Boolean).map(fn).filter(Boolean).join(' ')
}

/** Apply fn to every double-quoted string literal in the file that looks like a Tailwind class list. */
export function mapAllClassStrings(src, fn) {
  return src.replace(/"([^"\n]*)"/g, (s, c) => {
    if (!/[a-z]-|^[a-z]+$/.test(c) || c.includes('/') && c.startsWith('@')) return s
    const out = fn(c)
    return out === c ? s : `"${out}"`
  })
}
