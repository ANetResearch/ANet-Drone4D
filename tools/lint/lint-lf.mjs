#!/usr/bin/env node
// lieflat visual language lint (AWR-15 §12; AWR-18 §13.1).
//   VIS-L-03    backdrop-filter, backdrop-blur, supports-backdrop-filter (apps/web/src/**, shadcn sources included)
//   LF-TXT-01   arbitrary font size, spacing and radius values outside ui/components/ui/** (text-[13px], p-[3px], rounded-[6px])
//   LF-CHART-01 chart code (ui/lf/**): entropy sources, subtree rebuilds and constant element ids (AST check in lf-chart.mjs)
//   --palette   LF-PAL-01..05: raw token values equal AWR-15 §3.1 and §3.2, ordinal ramps, WCAG contrast of text roles,
//               CVD (Machado 2009 severity 1.0, OKLab dE x100) and HUD worst background (VIS-AC-001, 002, 003, 028)
// The expected palette is read from docs/15 (the tables are the source of truth); this tool contains no colour literals.
// When apps/web/src/styles/theme.css does not exist yet, LF-PAL-01 is skipped and the other palette checks run on the
// documented values.
// Usage: node tools/lint/lint-lf.mjs [--palette] [--palette-only] [files...]
import { existsSync } from 'node:fs'
import { Reporter, WEB_SRC, inShadcn, isCode, isMain, isStyle, lineCol, readText, run, stripComments, walk } from './_common.mjs'
import { checkChart } from './lf-chart.mjs'

const THEME = `${WEB_SRC}/styles/theme.css`
const DOC15 = 'docs/15-视觉设计规范与色卡.md'

// ---------------------------------------------------------------- source rules
const BACKDROP = /backdrop-filter|backdrop-blur|supports-backdrop-filter|backdropFilter|WebkitBackdropFilter/g
const ARB_TEXT = /(?<![\w-])(?:[a-z0-9-]+:)*-?text-\[(?!color:|var\()[^\]\s]*\d[^\]\s]*\]/g
const ARB_SPACE = /(?<![\w-])(?:[a-z0-9-]+:)*-?(?:p[xytrblse]?|m[xytrblse]?|gap(?:-[xy])?|space-[xy])-\[[^\]\s]+\]/g
const ARB_RADIUS = /(?<![\w-])(?:[a-z0-9-]+:)*rounded(?:-(?:t|r|b|l|tl|tr|br|bl|s|e|ss|se|es|ee))?-\[[^\]\s]+\]/g
const FAINT_TEXT = /(?<![\w-])(?:[a-z0-9-]+:)*text-(?:\(|\[var\()--lf-(?:faint|floor)\b|\bcolor\s*:\s*var\(--lf-(?:faint|floor)\)/g

export function checkSource(f, text, R) {
  if (!f.startsWith(WEB_SRC + '/') || !(isCode(f) || isStyle(f) || f.endsWith('.html'))) return
  const src = stripComments(text, { css: isStyle(f) })
  for (const m of src.matchAll(BACKDROP)) {
    const [l, c] = lineCol(src, m.index)
    R.add(f, l, c, 'VIS-L-03', `${m[0]} is forbidden (AWR-15 §6.5); use the opaque surface tokens`)
  }
  for (const m of src.matchAll(FAINT_TEXT)) {
    const [l, c] = lineCol(src, m.index)
    R.add(f, l, c, 'LF-PAL-03', '--lf-faint and --lf-floor are for non-text marks only (contrast < 3:1)')
  }
  if (!inShadcn(f)) {
    for (const [re, what] of [[ARB_TEXT, 'font size'], [ARB_SPACE, 'spacing'], [ARB_RADIUS, 'radius']]) {
      for (const m of src.matchAll(re)) {
        const [l, c] = lineCol(src, m.index)
        R.add(f, l, c, 'LF-TXT-01', `arbitrary ${what} value ${m[0].trim()}; use the type, spacing and radius tokens (AWR-15 §5.2, §6)`)
      }
    }
  }
  checkChart(f, text, R)
}

// ---------------------------------------------------------------- colour maths (WCAG 2.x, OKLab, Machado 2009)
const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
const lin = (v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
const lum = (rgb) => {
  const [r, g, b] = rgb.map(lin)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
export const contrast = (a, b) => {
  const la = lum(a)
  const lb = lum(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}
/** Alpha blend in gamma-encoded sRGB with 8-bit rounding (as d01 palette.mjs and browsers do for opaque results). */
export const blend = (fg, bg, a) => fg.map((v, i) => Math.round((v * a + bg[i] * (1 - a)) * 255) / 255)
const oklab = (linRgb) => {
  const [r, g, b] = linRgb
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
  return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s, 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s]
}
const MACHADO = {
  normal: null,
  protan: [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
  deutan: [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.01182, 0.04294, 0.968881]],
  tritan: [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.3039]],
}
const simulate = (rgb, kind) => {
  const l = rgb.map(lin)
  const M = MACHADO[kind]
  if (!M) return l
  return M.map((row) => Math.min(1, Math.max(0, row[0] * l[0] + row[1] * l[1] + row[2] * l[2])))
}
export const deltaE = (a, b, kind = 'normal') => {
  const x = oklab(simulate(a, kind))
  const y = oklab(simulate(b, kind))
  return Math.hypot(x[0] - y[0], x[1] - y[1], x[2] - y[2]) * 100
}

// ---------------------------------------------------------------- palette sources
export function docPalette(docText) {
  const out = {}
  const re = /^\|\s*`--(g\d+|white|r\d+)`\s*\|\s*`(#[0-9A-Fa-f]{6})`/gm
  for (const m of docText.matchAll(re)) out[m[1]] = m[2]
  return out
}

export function themePalette(cssText) {
  const out = {}
  for (const m of cssText.matchAll(/--(g\d+|white|r\d+)\s*:\s*(#[0-9A-Fa-f]{6})\s*;/g)) out[m[1]] = m[2]
  const hud = /--hud\s*:\s*([^;]+);/.exec(cssText)
  let hudAlpha = null
  if (hud) {
    const pct = /(\d+(?:\.\d+)?)%\s*\)?\s*$/.exec(hud[1].trim()) ?? /\/\s*(0?\.\d+)\s*\)/.exec(hud[1])
    if (pct) hudAlpha = pct[0].includes('%') ? Number(pct[1]) / 100 : Number(pct[1])
  }
  return { tokens: out, hudAlpha }
}

const WHITE = [1, 1, 1] // pure white of "white 72%" roles; ink is g950

export function checkPalette(P, R, where, hudAlpha) {
  const c = (n) => {
    if (!P[n]) throw new Error(`palette token --${n} missing`)
    return hexRgb(P[n])
  }
  const grays = ['g950', 'g900', 'g850', 'g800', 'g700', 'g600', 'g500', 'g400', 'g300', 'g200', 'g100', 'g50', 'white']
  const report = { contrast: [], cvd: {}, hud: {} }
  const fail = (rule, msg) => R.add(where, 1, 1, rule, msg)
  // LF-PAL-02 ordinal ramps (AWR-15 §3.4, §3.5)
  const ramps = [['dark', ['g500', 'g400', 'g300', 'g200', 'g50'], ['g900', 'g850']], ['light', ['g300', 'g400', 'g500', 'g600', 'g900'], ['g50', 'white']]]
  for (const [name, ramp, bgs] of ramps) {
    for (const bg of bgs) {
      const cr = ramp.map((t) => contrast(c(t), c(bg)))
      for (let i = 1; i < cr.length; i++) {
        if (!(cr[i] > cr[i - 1])) fail('LF-PAL-02', `${name} ramp is not ordinal on ${bg}: ${ramp[i - 1]} ${cr[i - 1].toFixed(2)} >= ${ramp[i]} ${cr[i].toFixed(2)}`)
      }
    }
  }
  // LF-PAL-03 text roles >= 4.5:1 (AWR-15 §3.3, §3.4, VIS-AC-002)
  const ink = c('g950')
  const roles = [
    ['dark foreground', c('g50'), c('g950')], ['dark card', c('g50'), c('g900')], ['dark popover', c('g50'), c('g850')],
    ['dark primary', c('g900'), c('g50')], ['dark muted-foreground', c('g300'), c('g900')], ['dark sidebar', c('g200'), c('g900')],
    ['brand-foreground on brand-solid', c('white'), c('r600')], ['dark brand-text', c('r400'), c('g900')],
    ['dark lf-lab', blend(WHITE, c('g900'), 0.72), c('g900')], ['dark lf-mut', blend(WHITE, c('g900'), 0.56), c('g900')],
    ['light foreground', c('g900'), c('g50')], ['light popover', c('g900'), c('white')], ['light primary', c('g50'), c('g900')],
    ['light muted-foreground', c('g500'), c('g50')], ['light brand-text', c('r700'), c('g50')],
    ['light lf-lab', blend(ink, c('g50'), 0.72), c('g50')], ['light lf-mut', blend(ink, c('g50'), 0.6), c('g50')],
  ]
  for (const [bgName, text, alphas] of [['g900', 'r400', [0.1, 0.15]], ['g850', 'r400', [0.1, 0.15]], ['g50', 'r700', [0.1, 0.15]], ['white', 'r700', [0.1, 0.15]]]) {
    for (const a of alphas) roles.push([`destructive ${text} on ${text}/${Math.round(a * 100)} over ${bgName}`, c(text), blend(c(text), c(bgName), a)])
  }
  for (const [name, fg, bg] of roles) {
    const cr = contrast(fg, bg)
    report.contrast.push([name, Number(cr.toFixed(2))])
    if (cr < 4.5) fail('LF-PAL-03', `${name}: contrast ${cr.toFixed(2)} < 4.5`)
  }
  // LF-PAL-04 CVD: r500 and r400 against every gray, min(protan, deutan) >= 8 (VIS-AC-003)
  for (const red of ['r500', 'r400']) {
    let worst = [Infinity, '']
    for (const g of grays) {
      const d = Math.min(deltaE(c(red), c(g), 'protan'), deltaE(c(red), c(g), 'deutan'))
      if (d < worst[0]) worst = [d, g]
    }
    report.cvd[red] = { min: Number(worst[0].toFixed(1)), against: worst[1] }
    if (worst[0] < 8) fail('LF-PAL-04', `${red} vs ${worst[1]}: CVD dE ${worst[0].toFixed(1)} < 8`)
  }
  // LF-PAL-05 HUD over the worst background g50 (VIS-AC-028)
  const alpha = hudAlpha ?? 0.92
  if (alpha < 0.92) fail('LF-PAL-05', `--hud opacity ${alpha} < 0.92`)
  const hud = blend(c('g900'), c('g50'), alpha)
  const hudChecks = [['g50 text', c('g50'), 13], ['lf-mut', blend(WHITE, hud, 0.56), 4.5], ['r400', c('r400'), 4.5]]
  for (const [name, fg, min] of hudChecks) {
    const cr = contrast(fg, hud)
    report.hud[name] = Number(cr.toFixed(2))
    if (cr < min) fail('LF-PAL-05', `HUD over g50: ${name} contrast ${cr.toFixed(2)} < ${min}`)
  }
  return report
}

if (isMain(import.meta.url)) {
  await run('lint-lf', async () => {
    const args = process.argv.slice(2)
    const palette = args.includes('--palette') || args.includes('--palette-only')
    const R = new Reporter('lint-lf')
    const extra = {}
    if (!args.includes('--palette-only')) {
      const explicit = args.filter((a) => !a.startsWith('--'))
      const files = explicit.length ? explicit : walk(WEB_SRC, (r) => isCode(r) || isStyle(r) || r.endsWith('.html'))
      if (!explicit.length && !files.length) R.note(`${WEB_SRC} has no source files yet; source rules skipped`)
      for (const f of files) {
        const t = readText(f)
        if (t !== null) checkSource(f, t, R)
      }
      extra.files = files.length
    }
    if (palette) {
      const doc = docPalette(readText(DOC15) ?? '')
      if (Object.keys(doc).length < 18) throw new Error(`could not read the 18 palette tokens from ${DOC15}`)
      let P = doc
      let hudAlpha = null
      let where = DOC15
      if (existsSync(THEME)) {
        const th = themePalette(readText(THEME))
        hudAlpha = th.hudAlpha
        where = THEME
        for (const [k, v] of Object.entries(doc)) {
          if (th.tokens[k] === undefined) R.add(THEME, 1, 1, 'LF-PAL-01', `--${k} is not defined as a hex literal`)
          else if (th.tokens[k] !== v) R.add(THEME, 1, 1, 'LF-PAL-01', `--${k} = ${th.tokens[k]} differs from AWR-15 §3.1/§3.2 (${v})`)
        }
        P = { ...doc, ...th.tokens }
        if (hudAlpha === null) R.note('--hud opacity not found in theme.css; LF-PAL-05 uses the documented 0.92')
      } else {
        R.note(`${THEME} does not exist yet (M15); LF-PAL-01 skipped, LF-PAL-02..05 checked on the AWR-15 values`)
      }
      extra.palette = checkPalette(P, R, where, hudAlpha)
    }
    R.finish(extra)
  })
}
