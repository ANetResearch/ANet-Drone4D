// Product version single source (AWR-04 §15.4.1, ADR-118; D2.1 WP-16). The product version `X.Y.Z` lives in pyproject.toml
// ([project].version) and apps/web/package.json (version); both must be equal and of the common PEP 440 / SemVer subset
// X.Y.Z. The workspace root package.json follows the same value. python/awr/__init__.py (`__version__`) is not checked: the
// World Package generator records it in every world (generator.version, part of contentVersion), so it stays at the
// generator's version until M03 decouples the two. The build injects the product version into the boot screen,
// __AWR_BUILD__ and build-info.json (apps/web/vite.config.ts buildInfoPlugin) and the deploy kit into RELEASE
// (tools/deploy/emax/build-local.sh).
//   VER-01 a version that is missing or not X.Y.Z
//   VER-02 a version that differs from pyproject.toml
// Usage: node tools/lint/check-version.mjs   (prints the version on success)
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { ROOT, Reporter, isMain, lineCol, run } from './_common.mjs'

export const VERSION_RE = /^\d+\.\d+\.\d+$/
export const SOURCES = ['pyproject.toml', 'apps/web/package.json', 'package.json']

/** the version declared by one source text: { value, index } or null (index: offset of the value, for line and column) */
export function extractVersion(file, text) {
  let m
  if (file.endsWith('pyproject.toml')) {
    // only the [project] table: the first `version = "…"` after the header and before the next table
    const start = text.search(/^\[project\]\s*$/m)
    if (start < 0) return null
    const rest = text.slice(start)
    const next = rest.slice(1).search(/^\[/m)
    const body = next < 0 ? rest : rest.slice(0, next + 1)
    m = /^version\s*=\s*"([^"]*)"/m.exec(body)
    return m ? { value: m[1], index: start + m.index + m[0].indexOf('"') + 1 } : null
  }
  if (file.endsWith('package.json')) {
    m = /^ {2}"version":\s*"([^"]*)"/m.exec(text)
    return m ? { value: m[1], index: m.index + m[0].lastIndexOf('"', m[0].length - 2) + 1 } : null
  }
  m = /^__version__\s*=\s*"([^"]*)"/m.exec(text)
  return m ? { value: m[1], index: m.index + m[0].indexOf('"') + 1 } : null
}

/** pure check of [{ file, text }] (the first entry is the reference); returns the reference version or null */
export function checkVersions(sources, R) {
  let ref = null
  for (const { file, text } of sources) {
    const v = extractVersion(file, text)
    if (!v) {
      R.add(file, 1, 1, 'VER-01', 'no product version found (ADR-118: X.Y.Z in pyproject.toml and apps/web/package.json)')
      continue
    }
    const [line, col] = lineCol(text, v.index)
    if (!VERSION_RE.test(v.value)) {
      R.add(file, line, col, 'VER-01', `version "${v.value}" is not X.Y.Z`)
      continue
    }
    if (ref === null) ref = v.value
    else if (v.value !== ref) R.add(file, line, col, 'VER-02', `version "${v.value}" differs from pyproject.toml "${ref}"; bump all of ${SOURCES.join(', ')} together`)
  }
  return ref
}

if (isMain(import.meta.url)) {
  await run('check-version', async () => {
    const R = new Reporter('check-version')
    const sources = SOURCES.filter((f) => existsSync(join(ROOT, f))).map((f) => ({ file: f, text: readFileSync(join(ROOT, f), 'utf8') }))
    const ref = checkVersions(sources, R)
    if (ref && R.violations.length === 0) console.log(ref)
    R.finish({ version: ref, files: sources.length })
  })
}
