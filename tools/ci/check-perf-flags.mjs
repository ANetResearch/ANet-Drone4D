#!/usr/bin/env node
// PERF-01 (AWR-18 §10, §13.1): performance cases must not launch Chromium with the C3 flags (vsync off). With
// --disable-gpu-vsync or --disable-frame-rate-limit rAF is decoupled from presentation and p50 frame intervals of 1-4 ms
// are an artefact (n05 §0 item 8), so every frame-pacing measurement would be invalid. Scope: apps/web/playwright.config.ts
// and apps/web/perf/** (all of it belongs to the `perf` project); comments are ignored.
// Output `<file>:<line>:<col> PERF-01 <message>`; exit 0 pass, 1 violations, 2 tool error; summary in runs/lint/.
// Usage: node tools/ci/check-perf-flags.mjs
import { Reporter, isCode, isMain, lineCol, readText, run, stripComments, walk } from '../lint/_common.mjs'

export const C3_FLAGS = ['--disable-gpu-vsync', '--disable-frame-rate-limit']
const C3_RE = new RegExp(C3_FLAGS.map((f) => f.replace(/-/g, '\\-')).join('|'), 'g')

/** Report every C3 flag in one source file (comments stripped). */
export function checkSource(file, text, R) {
  const src = stripComments(text)
  for (const m of src.matchAll(C3_RE)) {
    const [l, c] = lineCol(src, m.index)
    R.add(file, l, c, 'PERF-01', `C3 flag ${m[0]} in a performance case; use flag set C1 (AWR-18 §10)`)
  }
}

if (isMain(import.meta.url)) {
  await run('check-perf-flags', async () => {
    const R = new Reporter('check-perf-flags')
    const files = [...walk('apps/web', (r) => r === 'apps/web/playwright.config.ts'), ...walk('apps/web/perf', (r) => isCode(r))]
    for (const f of files) {
      const text = readText(f)
      if (text !== null) checkSource(f, text, R)
    }
    R.finish({ files: files.length })
  })
}
