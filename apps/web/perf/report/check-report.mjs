#!/usr/bin/env node
// check-report (M16-FR-072; PERF-AC-054; M16-AC-031): compliance of a rendered report.
//   static  report.json passes awr.perf.report.v1; metrics[].key are 18 §11.2 registered names; gate in G2/G3/G4/G5/bench;
//           report.html <= 5 MB, self-contained (no external script, stylesheet or font URL); no emoji and no forbidden
//           glyph in the text (EMOJI-01, GLYPH-01); cover, fidelity block and data-source footer present.
//   browser (default, skipped with --static): in every [data-figure] at most one element filled or painted with the brand
//           red r500; tables follow table.log (no zebra rows: equal backgrounds; [data-num] cells right aligned).
// Exit 0 compliant, 1 violations (listed), 2 usage.
// Usage: node perf/report/check-report.mjs --run <runId|dir> [--static]
import { existsSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { parseArgs } from 'node:util'
import { isRegistered } from '../harness/keys.mjs'
import { runPath } from './build-report.mjs'

const ROOT = resolve(import.meta.dirname, '../../../..')
export const MAX_BYTES = 5 * 1024 * 1024
export const FORBIDDEN = /[\p{Extended_Pictographic}\p{Emoji_Presentation}\u{25A0}-\u{25FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2194}-\u{21FF}]|\u{FE0F}/u

/** visible text of an HTML document (scripts, styles and tags removed, entities decoded) */
export function htmlText(html) {
  return html.replace(/<(script|style)[^>]*>[^]*?<\/\1>/gi, ' ').replace(/<[^>]+>/g, ' ')
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(Number.parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&')
}

export function staticErrors(report, html, htmlBytes, validate) {
  const errs = []
  if (validate && !validate(report)) errs.push(`schema: ${JSON.stringify(validate.errors?.slice(0, 2))}`)
  if (!['G2', 'G3', 'G4', 'G5', 'bench'].includes(report.gate)) errs.push(`gate ${report.gate}`)
  for (const c of report.cases ?? []) for (const m of c.metrics ?? []) if (!isRegistered(m.key)) errs.push(`unregistered metric key ${c.id}:${m.key}`)
  if (htmlBytes > MAX_BYTES) errs.push(`report.html is ${htmlBytes} bytes > 5 MB`)
  const text = htmlText(html)
  const bad = [...text.matchAll(new RegExp(FORBIDDEN.source, 'gu'))].slice(0, 5).map((m) => `U+${m[0].codePointAt(0).toString(16).toUpperCase()}`)
  if (bad.length) errs.push(`EMOJI-01/GLYPH-01: ${bad.join(' ')}`)
  if (/<script\b[^>]*\bsrc=|<link\b[^>]*rel="?stylesheet|url\((['"]?)https?:/i.test(html)) errs.push('report.html is not self-contained')
  for (const [sel, what] of [['data-report-cover', 'cover'], ['data-report-fidelity', 'fidelity block'], ['data-report-footer', 'data-source footer']]) {
    const n = html.match(new RegExp(`\\s${sel}(?=[\\s=>])`, 'g'))?.length ?? 0
    if (!n) errs.push(`missing ${what} [${sel}]`)
    else if (n > 1) errs.push(`${what} [${sel}] appears ${n} times`)
  }
  if (!/科研用途/.test(text) || !/示意/.test(text)) errs.push('footer lacks the research-use or illustrative-coordinate statement')
  if (/id="boot-mask"/.test(html)) errs.push('the application boot mask is still in the static report')
  return errs
}

async function browserErrors(file) {
  const { chromium } = await import('@playwright/test')
  const { FLAGS } = await import('../harness/browser.mjs')
  const { CHROME } = await import('../harness/protocol.mjs')
  const browser = await chromium.launch({ executablePath: CHROME, args: FLAGS.C1 })
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })
    await page.goto(`file://${file}`)
    return await page.evaluate(() => {
      const errs = []
      const probe = document.createElement('i')
      probe.style.color = 'var(--r500)'
      document.body.appendChild(probe)
      const red = getComputedStyle(probe).color
      probe.remove()
      for (const fig of document.querySelectorAll('[data-figure]')) {
        let n = 0
        for (const el of fig.querySelectorAll('*')) {
          const cs = getComputedStyle(el)
          if ((cs.fill === red && el instanceof SVGElement && el.tagName !== 'svg') || cs.backgroundColor === red) n++
        }
        if (n > 1) errs.push(`one-red rule: ${fig.getAttribute('data-figure')} has ${n} brand-red solid elements`)
      }
      for (const t of document.querySelectorAll('table')) {
        const bgs = new Set([...t.querySelectorAll('tbody tr')].map((r) => getComputedStyle(r).backgroundColor))
        if (bgs.size > 1) errs.push(`table.log: zebra rows in ${t.closest('[data-figure]')?.getAttribute('data-figure') ?? 'table'}`)
        for (const td of t.querySelectorAll('td[data-num]')) {
          const a = getComputedStyle(td).textAlign
          if (a !== 'right' && a !== 'end') {
            errs.push(`table.log: numeric cell not right aligned (${a})`)
            break
          }
        }
      }
      return errs
    })
  } finally {
    await browser.close()
  }
}

export async function checkReport(dir, { browser = true } = {}) {
  const report = JSON.parse(readFileSync(join(dir, 'report.json'), 'utf8'))
  const file = join(dir, 'report.html')
  if (!existsSync(file)) return ['report.html missing']
  const html = readFileSync(file, 'utf8')
  const { default: Ajv2020 } = await import('ajv/dist/2020.js')
  const schema = JSON.parse(readFileSync(join(ROOT, 'packages', 'contracts', 'perf', 'perf-report.schema.json'), 'utf8'))
  const errs = staticErrors(report, html, statSync(file).size, new Ajv2020({ strict: false, allErrors: true }).compile(schema))
  if (browser) errs.push(...(await browserErrors(file)))
  return errs
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const { values } = parseArgs({ options: { run: { type: 'string' }, static: { type: 'boolean' } } })
  if (!values.run) {
    console.error('usage: check-report.mjs --run <runId|dir> [--static]')
    process.exit(2)
  }
  checkReport(runPath(values.run), { browser: !values.static }).then((errs) => {
    for (const e of errs) console.log(`report PERF-AC-054 ${e}`)
    console.log(errs.length ? `check-report: ${errs.length} violations` : 'check-report: OK')
    process.exit(errs.length ? 1 : 0)
  }, (e) => {
    console.error(e?.stack ?? e)
    process.exit(2)
  })
}
