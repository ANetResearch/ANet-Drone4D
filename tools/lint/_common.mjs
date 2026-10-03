// Shared plumbing for tools/lint/*.mjs (M00; AWR-18 §13).
// Output: one line per violation `<file>:<line>:<col> <RULE> <message>`; exit 0 pass, 1 violations, 2 tool error;
// a summary is written to runs/lint/<timestamp>-<tool>.json.
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync, existsSync } from 'node:fs'
import { dirname, isAbsolute, join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

// AWR-18 §13.2 scan scope (D1-AC-20; release-facing documents added by ADR-079)
export const INCLUDE_DIRS = ['apps', 'python', 'packages', 'tools', 'scenarios', 'configs', 'vehicles', 'tests', 'mk']
export const INCLUDE_FILES = ['Makefile']
export const DOC_GLOBS = [/^docs\/03-[^/]*\.md$/, /^docs\/1[0-9]-[^/]*\.md$/, /^docs\/modules\//, /^docs\/README\.md$/, /^docs\/impl\//]
// release-facing documents outside docs/ (ADR-079: the repository front page, contribution and security notes, notices and the
// hosted CI workflow); like the docs they get the emoji and glyph rules only
export const RELEASE_DOCS = ['README.md', 'README.zh-CN.md', 'CONTRIBUTING.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md', 'NOTICE', 'CITATION.cff']
export const RELEASE_DOC_DIRS = ['.github']
const EXCLUDE = [/^docs\/research\//, /^docs\/01-design\.md$/, /^docs\/02-refs\.md$/, /^refs\//, /^\.cache\//, /(^|\/)node_modules\//,
  /^data\//, /^worlds\//, /^runs\//, /(^|\/)__pycache__\//, /(^|\/)\.venv\//, /^apps\/web\/dist\//, /(^|\/)\.vite(-temp)?\//]
const BINARY_EXT = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bin', '.awrrt', '.awrv', '.awsl', '.awtr', '.f32', '.u8', '.glb', '.gltf',
  '.mcap', '.zst', '.gz', '.zip', '.7z', '.ply', '.las', '.laz', '.npy', '.npz', '.pyc', '.so', '.woff', '.woff2', '.ttf', '.otf', '.pdf', '.mp4', '.webm', '.exr'])

export const posix = (p) => p.split(sep).join('/')
export const rel = (abs) => posix(relative(ROOT, abs))
export const excluded = (r) => EXCLUDE.some((re) => re.test(r))
export const isBinaryPath = (r) => {
  const i = r.lastIndexOf('.')
  return i >= 0 && BINARY_EXT.has(r.slice(i).toLowerCase())
}

/** Walk a directory (relative to ROOT) and return repository-relative posix paths of regular files. */
export function walk(dirRel, filter = () => true) {
  const out = []
  const start = join(ROOT, dirRel)
  if (!existsSync(start)) return out
  const stack = [start]
  while (stack.length) {
    const d = stack.pop()
    let names
    try {
      names = readdirSync(d)
    } catch {
      continue
    }
    for (const n of names) {
      const abs = join(d, n)
      const r = rel(abs)
      if (excluded(r) || excluded(r + '/')) continue
      let st
      try {
        st = statSync(abs)
      } catch {
        continue
      }
      if (st.isDirectory()) stack.push(abs)
      else if (st.isFile() && filter(r)) out.push(r)
    }
  }
  return out.sort()
}

/** Files of the AWR-18 §13.2 include set; docs optional. */
export function scopeFiles({ docs = false, filter = () => true } = {}) {
  const files = []
  for (const d of INCLUDE_DIRS) files.push(...walk(d, filter))
  for (const f of INCLUDE_FILES) if (existsSync(join(ROOT, f)) && filter(f)) files.push(f)
  if (docs) {
    files.push(...walk('docs', (r) => DOC_GLOBS.some((re) => re.test(r)) && filter(r)))
    for (const f of RELEASE_DOCS) if (existsSync(join(ROOT, f)) && filter(f)) files.push(f)
    for (const d of RELEASE_DOC_DIRS) files.push(...walk(d, filter))
  }
  return [...new Set(files)].sort()
}

export function readText(r) {
  const buf = readFileSync(isAbsolute(r) ? r : join(ROOT, r))
  if (buf.includes(0)) return null // binary
  return buf.toString('utf8')
}

/** 1-based line and column (in UTF-16 code units + 1) of a string index. */
export function lineCol(text, index) {
  let line = 1
  let last = -1
  for (let i = text.indexOf('\n'); i !== -1 && i < index; i = text.indexOf('\n', i + 1)) {
    line++
    last = i
  }
  return [line, index - last]
}

/** Strip // and /* *\/ comments from JS/TS/CSS while keeping offsets (comments replaced by spaces). */
export function stripComments(src, { css = false } = {}) {
  let out = ''
  let i = 0
  let q = null
  while (i < src.length) {
    const c = src[i]
    const n = src[i + 1]
    if (q) {
      out += c
      if (c === '\\' && q !== '`') {
        out += n ?? ''
        i += 2
        continue
      }
      if (c === q) q = null
      i++
      continue
    }
    if (c === '"' || c === "'" || c === '`') {
      q = c
      out += c
      i++
      continue
    }
    if (c === '/' && n === '*') {
      const e = src.indexOf('*/', i + 2)
      const end = e === -1 ? src.length : e + 2
      out += src.slice(i, end).replace(/[^\n]/g, ' ')
      i = end
      continue
    }
    if (!css && c === '/' && n === '/') {
      const e = src.indexOf('\n', i)
      const end = e === -1 ? src.length : e
      out += ' '.repeat(end - i)
      i = end
      continue
    }
    out += c
    i++
  }
  return out
}

export class Reporter {
  constructor(tool) {
    this.tool = tool
    this.violations = []
    this.notes = []
    this.t0 = Date.now()
  }
  add(file, line, col, rule, msg) {
    this.violations.push({ file, line, col, rule, msg })
  }
  note(msg) {
    this.notes.push(msg)
  }
  /** Print, write runs/lint/<ts>-<tool>.json and exit (0 pass, 1 violations). */
  finish(extra = {}) {
    for (const v of this.violations) console.log(`${v.file}:${v.line}:${v.col} ${v.rule} ${v.msg}`)
    for (const n of this.notes) console.error(`${this.tool}: ${n}`)
    const byRule = {}
    for (const v of this.violations) byRule[v.rule] = (byRule[v.rule] ?? 0) + 1
    const summary = { tool: this.tool, ok: this.violations.length === 0, violations: this.violations.length, by_rule: byRule, notes: this.notes,
      elapsed_ms: Date.now() - this.t0, at: new Date().toISOString(), ...extra, items: this.violations.slice(0, 2000) }
    try {
      const dir = join(process.env.AWR_RUNS_DIR ?? join(ROOT, 'runs'), 'lint')
      mkdirSync(dir, { recursive: true })
      const ts = summary.at.replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z')
      writeFileSync(join(dir, `${ts}-${this.tool}.json`), JSON.stringify(summary, null, 1) + '\n')
    } catch {
      // the summary file is best effort; the exit code carries the result
    }
    const tail = this.violations.length ? `${this.violations.length} violation(s)` : 'ok'
    console.error(`${this.tool}: ${tail} (${summary.elapsed_ms} ms)`)
    process.exit(this.violations.length ? 1 : 0)
  }
}

/** Run main() and map unexpected exceptions to exit code 2 (tool error). */
export async function run(tool, main) {
  try {
    await main()
  } catch (e) {
    console.error(`${tool}: tool error: ${e?.stack ?? e}`)
    process.exit(2)
  }
}

export const WEB_SRC = 'apps/web/src'
export const inWebSrc = (r) => r.startsWith(WEB_SRC + '/')
export const inShadcn = (r) => r.startsWith(WEB_SRC + '/ui/components/ui/')
export const isCode = (r) => /\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/.test(r)
export const isStyle = (r) => /\.(css|scss)$/.test(r)

/** True when the module is the process entry point (so linters can also be imported by selftest.mjs). */
export const isMain = (metaUrl) => {
  const arg = process.argv[1]
  if (!arg) return false
  return fileURLToPath(metaUrl) === (isAbsolute(arg) ? arg : join(process.cwd(), arg))
}
