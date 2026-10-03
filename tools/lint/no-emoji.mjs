#!/usr/bin/env node
// EMOJI-01 and GLYPH-01 (AWR-03 §8.4 note 4, D1-AC-20; AWR-18 §13.1).
// EMOJI-01: characters with Extended_Pictographic=Yes or Emoji_Presentation=Yes, and U+FE0F.
// GLYPH-01: forbidden glyph blocks U+25A0-U+25FF, U+2600-U+26FF, U+2700-U+27BF, U+2194-U+21FF.
// Arrows U+2190-U+2193, box drawing and mathematical symbols are allowed.
// Scope: AWR-18 §13.2 include set, plus docs/03-*.md, docs/1[0-9]-*.md, docs/modules/**, docs/README.md, and (ADR-079) docs/impl/**,
// the root README.md, README.zh-CN.md, CONTRIBUTING.md, SECURITY.md, THIRD_PARTY_NOTICES.md, NOTICE, CITATION.cff and .github/**.
// Usage: node tools/lint/no-emoji.mjs [--no-docs] [files...]
import { Reporter, isBinaryPath, isMain, lineCol, readText, run, scopeFiles } from './_common.mjs'

const EMOJI = /[\p{Extended_Pictographic}\p{Emoji_Presentation}\u{FE0F}]/gu
const GLYPH = /[\u{25A0}-\u{25FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2194}-\u{21FF}]/u

export function scanText(text) {
  const hits = []
  for (const m of text.matchAll(EMOJI)) hits.push({ index: m.index, ch: m[0], rule: GLYPH.test(m[0]) ? 'GLYPH-01' : 'EMOJI-01' })
  const glyphAll = /[\u{25A0}-\u{25FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2194}-\u{21FF}]/gu
  const seen = new Set(hits.map((h) => h.index))
  for (const m of text.matchAll(glyphAll)) if (!seen.has(m.index)) hits.push({ index: m.index, ch: m[0], rule: 'GLYPH-01' })
  return hits.sort((a, b) => a.index - b.index)
}

const cp = (s) => 'U+' + s.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')

export function checkFile(f, text, R) {
  for (const h of scanText(text)) {
    const [line, col] = lineCol(text, h.index)
    R.add(f, line, col, h.rule, `${cp(h.ch)} is not allowed (${h.rule === 'EMOJI-01' ? 'emoji' : 'forbidden glyph block'}); use text or an icon key`)
  }
}

if (isMain(import.meta.url)) {
  await run('no-emoji', async () => {
    const args = process.argv.slice(2)
    const docs = !args.includes('--no-docs')
    const explicit = args.filter((a) => !a.startsWith('--'))
    const files = explicit.length ? explicit : scopeFiles({ docs, filter: (r) => !isBinaryPath(r) })
    const R = new Reporter('no-emoji')
    let scanned = 0
    for (const f of files) {
      const text = readText(f)
      if (text === null) continue
      scanned++
      checkFile(f, text, R)
    }
    R.finish({ files: scanned })
  })
}
