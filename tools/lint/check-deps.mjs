#!/usr/bin/env node
// Dependency governance (AWR-11 §7.4, TECH-FR-003/004; AWR-18 §13.1).
//   deps/unlisted    BOM mode: a direct dependency in a manifest or lock is not registered in the BOM.
//                    Fallback mode: a bare import in apps/web/{src,tests,perf,dev} is not declared in apps/web or root package.json.
//   deps/version     declared npm versions must be exact; lock versions must equal the declaration (and the BOM when present).
//   deps/denied      TECH-FR-003 rejected list (AWR-11 §5 X-numbers), npm and PyPI.
//   deps/stale       BOM runtime entries whose last commit is older than 12 months and that have no fallback.
//   imports/boundary TS import boundaries (AWR-11 §3.4, AWR-03 §4.2): lucide-react, @base-ui/react outside ui/components/ui,
//                    @msgpack/msgpack outside net/**, camera-controls outside engine/camera, dev oracles only in dev/oracles,
//                    drei AdaptiveDpr/PerformanceMonitor/CameraControls, engine/** and net/** free of react, zustand, R3F and UI paths,
//                    rt.worker free of react, three and zustand.
// The BOM (tools/ci/bom.json) is optional until it is written: without it the tool runs the fallback checks and says so.
// Usage: node tools/lint/check-deps.mjs [--bom-only]
import { existsSync, readFileSync } from 'node:fs'
import { builtinModules } from 'node:module'
import { join } from 'node:path'
import { ROOT, Reporter, WEB_SRC, isCode, isMain, lineCol, readText, run, stripComments, walk } from './_common.mjs'

const BOM = 'tools/ci/bom.json'
const MANIFESTS = ['package.json', 'apps/web/package.json', 'packages/contracts/package.json']
const EXACT = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/
const DENIED_NPM = {
  'lucide-react': 'X09', recharts: 'X05', echarts: 'X06', 'chart.js': 'X06', sonner: 'X07', motion: 'X08', 'framer-motion': 'X08',
  '@radix-ui/*': 'X10', potree: 'X15', 'three-loader': 'X15', jotai: 'X39', valtio: 'X39', comlink: 'X39', '@tanstack/pacer': 'X39', 'r3f-perf': 'X39',
  next: 'X38', eslint: 'X37', 'typescript-eslint': 'X37', '@typescript-eslint/*': 'X37', msgpackr: 'X26', cesium: 'X01', '@zenoh/zenoh-ts': 'X23',
  '@foxglove/ws-protocol': 'X22', pnpm: 'X46', 'takram-three-atmosphere': 'X43', '@takram/three-atmosphere': 'X43',
}
const DENIED_PY = { redis: 'X30', pyzmq: 'X30', supervisor: 'X29', aioquic: 'X24', pywebtransport: 'X24', airsim: 'X34', rotorpy: 'X33', roslibpy: 'X21' }
const deniedNpm = (name) => DENIED_NPM[name] ?? Object.entries(DENIED_NPM).find(([k]) => k.endsWith('/*') && name.startsWith(k.slice(0, -1)))?.[1]

const pkgName = (spec) => (spec.startsWith('@') ? spec.split('/').slice(0, 2).join('/') : spec.split('/')[0])
const BUILTINS = new Set([...builtinModules, ...builtinModules.map((m) => `node:${m}`)])

function readJson(rel) {
  try {
    return JSON.parse(readFileSync(join(ROOT, rel), 'utf8'))
  } catch {
    return null
  }
}

function lineOf(rel, needle) {
  const t = readText(rel) ?? ''
  const i = t.indexOf(needle)
  return i < 0 ? [1, 1] : lineCol(t, i)
}

export function checkManifests(R, bom) {
  const lock = readJson('package-lock.json')
  const declared = new Map()
  for (const m of MANIFESTS) {
    const pj = readJson(m)
    if (!pj) continue
    for (const sect of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
      for (const [name, ver] of Object.entries(pj[sect] ?? {})) {
        declared.set(name, { ver, manifest: m })
        const [l, c] = lineOf(m, `"${name}"`)
        const x = deniedNpm(name)
        if (x) R.add(m, l, c, 'deps/denied', `${name} is rejected (AWR-11 §5 ${x})`)
        if (sect !== 'peerDependencies' && !EXACT.test(String(ver)) && !String(ver).startsWith('workspace:') && !(name.startsWith('@awr/'))) {
          R.add(m, l, c, 'deps/version', `${name}@${ver}: versions must be exact (no ranges)`)
        }
        const locked = lock?.packages?.[`node_modules/${name}`]
        if (locked && !locked.link && EXACT.test(String(ver)) && locked.version !== ver) {
          R.add(m, l, c, 'deps/version', `${name}: declared ${ver}, package-lock.json has ${locked.version}`)
        }
        if (bom) {
          const e = bom.get(`npm:${name}`)
          if (!e && !name.startsWith('@awr/')) R.add(m, l, c, 'deps/unlisted', `${name} (${m}) is not registered in ${BOM}`)
          else if (e && e.version && e.version !== ver) R.add(m, l, c, 'deps/version', `${name}: expected ${e.version} (BOM), got ${ver}`)
        }
      }
    }
  }
  // transitive packages are reported, not failed: X-numbers reject direct use (cmdk pulls @radix-ui/react-dialog,
  // react-scan pulls eslint); direct imports are caught by imports/boundary and deps/unlisted below
  const transitive = new Set()
  for (const k of Object.keys(lock?.packages ?? {})) {
    const name = k.replace(/^.*node_modules\//, '')
    if (k.includes('node_modules/') && deniedNpm(name) && !declared.has(name)) transitive.add(name)
  }
  if (transitive.size) R.note(`transitive packages on the rejected list (not imported directly): ${[...transitive].sort().join(', ')}`)
  return declared
}

export function checkPython(R, bom) {
  const t = readText('requirements.lock')
  if (t === null) {
    R.note('requirements.lock not found; PyPI checks skipped')
    return
  }
  for (const m of t.matchAll(/^([A-Za-z0-9_.-]+)==([^\s;\\]+)/gm)) {
    const name = m[1].toLowerCase().replace(/_/g, '-')
    const [l, c] = lineCol(t, m.index)
    if (DENIED_PY[name]) R.add('requirements.lock', l, c, 'deps/denied', `${name} is rejected (AWR-11 §5 ${DENIED_PY[name]})`)
    if (bom) {
      const e = bom.get(`pypi:${name}`)
      if (e && e.version && e.version !== m[2]) R.add('requirements.lock', l, c, 'deps/version', `${name}: expected ${e.version} (BOM), locked ${m[2]}`)
    }
  }
}

const RULES = [
  { test: (s) => s === 'lucide-react' || s.startsWith('lucide-react/'), where: () => true, msg: 'lucide-react is forbidden (ADR-030)' },
  { test: (s) => s === '@base-ui/react' || s.startsWith('@base-ui/react/'), where: (f) => !f.startsWith(`${WEB_SRC}/ui/components/ui/`), msg: '@base-ui/react only in ui/components/ui (AWR-03 §4.2)' },
  { test: (s) => s === '@msgpack/msgpack', where: (f) => f.startsWith(`${WEB_SRC}/`) && !f.startsWith(`${WEB_SRC}/net/`), msg: '@msgpack/msgpack only in net/** (AWR-11 §3.4)' },
  { test: (s) => s === 'camera-controls', where: (f) => f.startsWith(`${WEB_SRC}/`) && !f.startsWith(`${WEB_SRC}/engine/camera/`), msg: 'camera-controls only in engine/camera (AWR-11 §3.4)' },
  { test: (s) => s === 'potree-core' || s.startsWith('@voxelkloud/'), where: (f) => !f.startsWith('apps/web/dev/oracles/'), msg: 'dev oracles only in apps/web/dev/oracles (AWR-11 §3.4)' },
  { test: (s) => /^(react|react-dom|zustand|@react-three\/[\w-]+)(\/|$)/.test(s) || /(^|\/)(ui|viewport|stores)\//.test(s) || /^@\/(ui|viewport|stores)(\/|$)/.test(s),
    where: (f) => f.startsWith(`${WEB_SRC}/engine/`) || f.startsWith(`${WEB_SRC}/net/`), msg: 'engine/** and net/** must not import React, zustand, R3F, ui, viewport or stores (AWR-03 §4.2)' },
  { test: (s) => /^(react|react-dom|three|zustand)(\/|$)/.test(s), where: (f) => /\/net\/rt\/[^/]*worker[^/]*\.ts$/.test(f), msg: 'rt.worker must not import react, three or zustand (AWR-11 §3.4)' },
]

export function checkImports(f, text, R, declared) {
  const src = stripComments(text)
  const re = /(?:^|[;\n])\s*(?:import|export)\s+(?:type\s+)?(?:[\s\S]*?\sfrom\s+)?['"]([^'"]+)['"]|\bimport\(\s*['"]([^'"]+)['"]\s*\)|\brequire\(\s*['"]([^'"]+)['"]\s*\)/g
  for (const m of src.matchAll(re)) {
    const spec = m[1] ?? m[2] ?? m[3]
    const idx = m.index + m[0].indexOf(spec)
    const [l, c] = lineCol(src, idx)
    for (const r of RULES) if (r.test(spec) && r.where(f)) R.add(f, l, c, 'imports/boundary', `${spec}: ${r.msg}`)
    const denied = !spec.startsWith('.') && deniedNpm(pkgName(spec))
    if (denied && pkgName(spec) !== 'lucide-react') R.add(f, l, c, 'deps/denied', `${pkgName(spec)} is rejected (AWR-11 §5 ${denied})`)
    const stmt = m[0]
    if (spec === '@react-three/drei') {
      for (const bad of ['AdaptiveDpr', 'PerformanceMonitor', 'CameraControls']) {
        if (new RegExp(`\\b${bad}\\b`).test(stmt)) R.add(f, l, c, 'imports/boundary', `drei ${bad} is excluded from the whitelist (AWR-11 X47)`)
      }
    }
    if (declared && !spec.startsWith('.') && !spec.startsWith('/') && !spec.startsWith('@/') && !BUILTINS.has(spec) && !spec.startsWith('virtual:') &&
        !spec.startsWith('~') && !spec.includes('?')) {
      const name = pkgName(spec)
      if (!declared.has(name) && !BUILTINS.has(name)) R.add(f, l, c, 'deps/unlisted', `${name} is imported but not declared in apps/web or root package.json`)
    }
  }
}

function loadBom(R) {
  if (!existsSync(join(ROOT, BOM))) return null
  const d = readJson(BOM)
  if (!d || !Array.isArray(d.packages)) throw new Error(`${BOM} is not a valid BOM (expected {packages: [...]})`)
  const map = new Map()
  for (const p of d.packages) {
    if (!p.name || !p.ecosystem) {
      R.add(BOM, 1, 1, 'deps/unlisted', `BOM entry without name or ecosystem: ${JSON.stringify(p).slice(0, 80)}`)
      continue
    }
    map.set(`${p.ecosystem}:${p.ecosystem === 'pypi' ? p.name.toLowerCase().replace(/_/g, '-') : p.name}`, p)
  }
  return { map, doc: d }
}

if (isMain(import.meta.url)) {
  await run('check-deps', async () => {
    const R = new Reporter('check-deps')
    const bomOnly = process.argv.includes('--bom-only')
    const bom = loadBom(R)
    if (bomOnly) {
      if (!bom) throw new Error(`${BOM} does not exist; --bom-only needs the BOM`)
      for (const p of bom.doc.packages) {
        for (const k of ['name', 'ecosystem', 'version', 'status']) if (p[k] === undefined) R.add(BOM, 1, 1, 'deps/unlisted', `${p.name ?? '?'}: missing field ${k}`)
      }
      R.finish({ bom: true })
      return
    }
    if (!bom) R.note(`${BOM} not present: BOM checks (deps/unlisted against the BOM, deps/stale) skipped; manifest, lock, denied and boundary checks ran`)
    const declared = checkManifests(R, bom?.map ?? null)
    checkPython(R, bom?.map ?? null)
    if (bom) {
      const year = 365 * 24 * 3600 * 1000
      for (const p of bom.doc.packages) {
        if (p.scope === 'runtime' && p.lastCommit && !p.fallback && Date.now() - Date.parse(p.lastCommit) > year) {
          R.add(BOM, 1, 1, 'deps/stale', `${p.name}: last commit ${p.lastCommit} is older than 12 months and has no fallback`)
        }
      }
    }
    const files = ['apps/web/src', 'apps/web/tests', 'apps/web/perf', 'apps/web/dev'].flatMap((d) => walk(d, isCode))
    for (const f of files) {
      const t = readText(f)
      if (t !== null) checkImports(f, t, R, declared)
    }
    R.finish({ files: files.length, bom: Boolean(bom) })
  })
}
