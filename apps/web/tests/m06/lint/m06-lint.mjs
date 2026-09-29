#!/usr/bin/env node
// M06 coding-rule lint (M06 §6.3 rules 2, 3, 6, 8, 9; FR-007, FR-008, FR-020; M06-AC-005, AC-006, AC-018). Owner: M06.
// Run by `make lint-m06` (part of `make lint` through mk/m06.mk). Output `<file>:<line>:<col> <rule> <message>`, exit 1
// on violations. Rules (scope apps/web/src/**, the test-build regression page viewport/dev/** is exempt):
//   M06-L-01 no `material.onBeforeRender =` (WebGLNodesHandler overwrites it)             engine/**, viewport/**
//   M06-L-02 no reads of `info.render.frame` (not a frame number under the handler)       src/**
//   M06-L-03 no synchronous read-back: readRenderTargetPixels( / gl.readPixels(         engine/**, viewport/**
//            (exception: viewport/backend/microbench.ts, 1 px before the reveal)
//   M06-L-04 drei imports only from the ADR-008 white list actually used in D1 (View, Html) src/**
//   M06-L-05 no RenderPipeline, pass(), mrt(), storage textures or compute on the shared path engine/**, viewport/**
//   M06-L-06 no `.internalFormat =` assignments                                          engine/**, viewport/**
//   M06-L-07 no per-object onObjectUpdate on engine hot paths                              engine/**
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = fileURLToPath(new URL('.', import.meta.url))
export const ROOT = resolve(HERE, '../../../../..')
const SRC = 'apps/web/src'
const DREI_OK = new Set(['View', 'Html'])

const inEngine = (f) => f.startsWith(`${SRC}/engine/`)
const inViewport = (f) => f.startsWith(`${SRC}/viewport/`)
const exempt = (f) => f.startsWith(`${SRC}/viewport/dev/`)

function stripComments(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' ')).replace(/(^|[^:'"`\\])\/\/[^\n]*/g, (m, p) => p + ' '.repeat(m.length - p.length))
}
function lineCol(text, index) {
  const pre = text.slice(0, index)
  const line = pre.split('\n').length
  return [line, index - pre.lastIndexOf('\n')]
}

/** violations of one file (repository-relative path, text) */
export function checkText(f, text) {
  const out = []
  if (!f.startsWith(`${SRC}/`) || !/\.(ts|tsx)$/.test(f) || exempt(f)) return out
  const src = stripComments(text)
  const add = (re, rule, msg, when = true) => {
    if (!when) return
    for (const m of src.matchAll(re)) {
      const [l, c] = lineCol(src, m.index)
      out.push({ f, l, c, rule, msg })
    }
  }
  const shared = inEngine(f) || inViewport(f)
  add(/\b(?:material|mat|m|mm|mat\d*)\s*\.\s*onBeforeRender\s*=(?!=)/g, 'M06-L-01', 'material.onBeforeRender is overwritten by WebGLNodesHandler; use object.onBeforeRender', shared)
  add(/\binfo\s*\.\s*render\s*\.\s*frame\b/g, 'M06-L-02', 'info.render.frame is not a frame number under WebGLNodesHandler; use FrameCtx.frameNo')
  const syncOk = f === `${SRC}/viewport/backend/microbench.ts`
  add(/\breadRenderTargetPixels\s*\(/g, 'M06-L-03', 'synchronous readRenderTargetPixels; use RenderBackend.readPixels (async)', shared && !syncOk)
  add(/\bgl\s*\.\s*readPixels\s*\(/g, 'M06-L-03', 'synchronous gl.readPixels; use RenderBackend.readPixels (async)', shared && !syncOk)
  for (const m of src.matchAll(/import\s*(?:type\s*)?\{([^}]*)\}\s*from\s*['"]@react-three\/drei(?:\/[^'"]*)?['"]/g)) {
    for (const raw of m[1].split(',')) {
      const name = raw.trim().replace(/^type\s+/, '').split(/\s+as\s+/)[0]
      if (name && !DREI_OK.has(name)) {
        const [l, c] = lineCol(src, m.index)
        out.push({ f, l, c, rule: 'M06-L-04', msg: `drei ${name} is not on the D1 white list (ADR-008: View, Html)` })
      }
    }
  }
  add(/import\s+\*\s+as\s+\w+\s+from\s*['"]@react-three\/drei['"]/g, 'M06-L-04', 'namespace import of drei (white list: View, Html)')
  add(/\b(?:RenderPipeline|PostProcessing|StorageTexture|Storage3DTexture|StorageBufferAttribute)\b|\bpass\s*\(|\bmrt\s*\(|\bstorageTexture\s*\(|\binstancedArray\s*\(|\.compute\s*\(|\bcomputeAsync\s*\(/g,
    'M06-L-05', 'RenderPipeline, pass(), MRT, storage textures and compute are not allowed on the shared path (M06 §6.3 rule 9)', shared)
  add(/\.internalFormat\s*=(?!=)/g, 'M06-L-06', 'never set texture.internalFormat (M06 §6.3 rule 8)', shared)
  add(/\.onObjectUpdate\s*\(/g, 'M06-L-07', 'per-object onObjectUpdate on an engine hot path; use instance attributes, textures or matrices', inEngine(f))
  return out
}

function walk(dir) {
  const out = []
  for (const e of readdirSync(dir)) {
    const p = join(dir, e)
    const st = statSync(p)
    if (st.isDirectory()) out.push(...walk(p))
    else if (/\.(ts|tsx)$/.test(e)) out.push(p)
  }
  return out
}

export function run(files) {
  const list = files && files.length ? files.map((f) => resolve(f)) : walk(join(ROOT, SRC))
  const all = []
  for (const abs of list) all.push(...checkText(relative(ROOT, abs), readFileSync(abs, 'utf8')))
  return all
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const v = run(process.argv.slice(2))
  for (const x of v) console.log(`${x.f}:${x.l}:${x.c} ${x.rule} ${x.msg}`)
  console.log(v.length ? `m06-lint: ${v.length} violation(s)` : 'm06-lint: ok')
  process.exit(v.length ? 1 : 0)
}
