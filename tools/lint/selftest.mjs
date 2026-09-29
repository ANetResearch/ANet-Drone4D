#!/usr/bin/env node
// Self test of the lint rules on synthetic inputs (no real repository files). Colour literals and forbidden
// characters are assembled at runtime so this file itself passes no-hex and no-emoji.
// Usage: node tools/lint/selftest.mjs   (exit 0 when every expectation holds)
import { checkFile as emoji } from './no-emoji.mjs'
import { checkFile as hex } from './no-hex.mjs'
import { checkSource as lf, checkPalette, docPalette, contrast } from './lint-lf.mjs'
import { checkFile as motion, checkLoops } from './motion-lint.mjs'
import { checkFile as icons, lucideNames, registryKeys } from './check-icons.mjs'
import { checkFile as raw } from './no-raw-controls.mjs'
import { checkSources as brand } from './check-brand.mjs'
import { checkImports } from './check-deps.mjs'
import { checkName } from './check-units.mjs'
import { checkSource as perfFlags } from '../ci/check-perf-flags.mjs'
import { readText } from './_common.mjs'

class Collect {
  constructor() {
    this.v = []
  }
  add(file, line, col, rule, msg) {
    this.v.push({ file, line, col, rule, msg })
  }
  note() {}
}
let failures = 0
function expect(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) {
    failures++
    console.error(`FAIL ${name}: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`)
  }
}
const rules = (fn) => {
  const R = new Collect()
  fn(R)
  return R.v.map((x) => x.rule)
}
const H = '#'
const SRC = 'apps/web/src'

// no-emoji
const smile = String.fromCodePoint(0x1f600)
const check = String.fromCodePoint(0x2705)
const play = String.fromCodePoint(0x25b6)
const vs16 = String.fromCodePoint(0xfe0f)
expect('emoji', rules((R) => emoji('x.ts', `a ${smile} b ${check} c ${play} d ${vs16} e`, R)), ['EMOJI-01', 'GLYPH-01', 'GLYPH-01', 'EMOJI-01'])
expect('emoji allowed arrows and box drawing', rules((R) => emoji('x.md', 'left ← up ↑ right → down ↓ tree ├── x × deg °', R)), [])

// no-hex
expect('hex in python', rules((R) => hex('python/awr/x.py', `c = "${H}ff0000"\n# issue ${H}123 and ${H}add\n`, R)), ['VIS-L-01'])
expect('hex exempt theme.css', rules((R) => hex(`${SRC}/styles/theme.css`, `--g50: ${H}F2F3F5;`, R)), [])
expect('keywords in css', rules((R) => hex(`${SRC}/ui/x.css`, '.a { color: white; background: transparent; border-color: currentColor }', R)), ['VIS-L-02'])
expect('keywords in tsx', rules((R) => hex(`${SRC}/ui/x.tsx`, `<div className="bg-black/50 text-foreground" style={{ color: 'red' }} />`, R)), ['VIS-L-02', 'VIS-L-02'])

// lint-lf
expect('backdrop and arbitrary values', rules((R) => lf(`${SRC}/ui/panels/a.tsx`,
  '<div className="backdrop-blur-sm text-[13px] p-[3px] rounded-[6px] text-(--lf-mut) text-(--lf-faint)" />', R)), ['VIS-L-03', 'LF-PAL-03', 'LF-TXT-01', 'LF-TXT-01', 'LF-TXT-01'])
expect('shadcn exempt from LF-TXT-01', rules((R) => lf(`${SRC}/ui/components/ui/badge.tsx`, '<span className="text-[0.625rem]" />', R)), [])
expect('chart code', rules((R) => lf(`${SRC}/ui/lf/Spark.ts`, "const r = Math.random(); el.innerHTML = ''\n<g id=\"a\"/><g id=\"a\"/>", R)),
  ['LF-CHART-01', 'LF-CHART-01', 'LF-CHART-01'])
const doc = docPalette(readText('docs/15-视觉设计规范与色卡.md') ?? '')
expect('palette tokens from AWR-15', Object.keys(doc).length, 18)
{
  const R = new Collect()
  const rep = checkPalette(doc, R, 'doc', 0.92)
  expect('palette passes', R.v.map((x) => x.rule), [])
  expect('cvd r500 min', rep.cvd.r500, { min: 10, against: 'g500' })
  const bad = { ...doc, r400: doc.r500 }
  const R2 = new Collect()
  checkPalette(bad, R2, 'doc', 0.8)
  expect('palette failures detected', [...new Set(R2.v.map((x) => x.rule))].sort(), ['LF-PAL-03', 'LF-PAL-05'])
  expect('contrast white on black', Math.round(contrast([1, 1, 1], [0, 0, 0]) * 10) / 10, 21)
}

// motion-lint
{
  const loops = []
  const got = rules((R) => {
    motion(`${SRC}/ui/a.css`, '.a { transition: opacity 200ms ease; transition-timing-function: cubic-bezier(0.2, 0, 0, 1) !important; }', R, loops)
    motion(`${SRC}/ui/b.tsx`, '<div className="duration-300 ease-[cubic-bezier(1,0,0,1)] transition-all animate-spin" />', R, loops)
    motion(`${SRC}/lib/tokens/motion.gen.ts`, "export const quick = '150ms'", R, loops)
    motion(`${SRC}/ui/c.tsx`, "el.animate(kf, { duration: 250, easing: 'ease-out' }); <i className='animate-pulse animate-ping'/>", R, loops)
    checkLoops(loops, R)
  })
  expect('motion rules', [...new Set(got)].sort(), ['MOT-01', 'MOT-02', 'MOT-03', 'MOT-04'])
}

// check-icons
{
  const lucide = lucideNames()
  const keys = registryKeys("export const REGISTRY = { 'tl.play': Play, 'env.fog': CloudFog }")
  expect('registry keys', [...keys].sort(), ['env.fog', 'tl.play'])
  const got = rules((R) => {
    icons(`${SRC}/ui/a.tsx`, "import { Home } from 'lucide-react'\nimport { icons } from 'lucide'\nimport * as L from 'lucide'", R, { lucide, keys })
    icons(`${SRC}/ui/icons/registry.ts`, "import { Home, House, TriangleAlert, NotAnIcon } from 'lucide'", R, { lucide, keys })
    icons(`${SRC}/ui/b.tsx`, '<Icon name="tl.play" /><StateIcon name="tl.stop" />', R, { lucide, keys })
  })
  expect('icon rules', got, lucide ? ['ICON-01', 'ICON-02', 'ICON-02', 'ICON-03', 'ICON-03', 'ICON-04'] : ['ICON-01', 'ICON-02', 'ICON-02', 'ICON-04'])
}

// no-raw-controls
{
  const R = new Collect()
  await raw(`${SRC}/ui/panels/a.tsx`, "import { createPortal } from 'react-dom'\nexport const A = () => <div role=\"button\"><button/><Button/><input/></div>", R)
  await raw(`${SRC}/ui/components/ui/button.tsx`, 'export const B = () => <button/>', R)
  await raw(`${SRC}/viewport/ViewCube.tsx`, 'export const V = () => <button/>', R)
  expect('raw controls', R.v.map((x) => x.rule), ['RAW-01', 'RAW-01', 'RAW-01', 'RAW-01'])
}

// PERF-01 (tools/ci/check-perf-flags.mjs): C3 flags in perf cases fail; C1 flags and comments do not
{
  const got = rules((R) => {
    perfFlags('apps/web/perf/a.spec.ts', "const C1 = ['--use-angle=swiftshader']\n// never '--disable-gpu-vsync' here\nargs.push('--disable-gpu-vsync', '--disable-frame-rate-limit')", R)
  })
  expect('perf flags', got, ['PERF-01', 'PERF-01'])
}

// check-brand (sources only; files are synthetic)
{
  const files = new Map([
    [`${SRC}/ui/panels/x.tsx`, "<img src='/brand/anet-logo.svg'/><img src='https://avatars.githubusercontent.com/u/1'/>"],
    [`${SRC}/ui/layout/AppHeader.tsx`, "<img src='/brand/avatar-96.png'/>"],
    [`${SRC}/ui/layout/AboutDialog.tsx`, "<div data-viewport><img src='/brand/anet-logo.svg'/></div>"],
    // module paths under ui/brand are code, not assets; an imported image file still counts
    [`${SRC}/ui/panels/y.tsx`, "import { PanelEmpty } from '@/ui/brand/PanelEmpty'\nexport * from \"../brand/index\"\nconst L = import('@/ui/brand/Lazy')"],
    [`${SRC}/ui/panels/z.tsx`, "import logo from '../../../public/brand/anet-logo.svg?url'"],
  ])
  const got = rules((R) => brand([...files.keys()], R, (f) => files.get(f)))
  expect('brand rules', got, ['BRAND-03', 'BRAND-02', 'BRAND-02', 'BRAND-02'])
}

// check-deps boundaries
{
  const declared = new Map([['react', {}], ['three', {}], ['@msgpack/msgpack', {}]])
  const got = rules((R) => {
    checkImports(`${SRC}/engine/drones/a.ts`, "import { useState } from 'react'\nimport { x } from '../../ui/panels/p'\nimport { decode } from '@msgpack/msgpack'", R, declared)
    checkImports(`${SRC}/net/rt/rt.worker.ts`, "import * as THREE from 'three'", R, declared)
    checkImports(`${SRC}/ui/panels/a.tsx`, "import { Dialog } from '@base-ui/react/dialog'\nimport { AdaptiveDpr } from '@react-three/drei'\nimport c from 'recharts'", R, declared)
  })
  expect('dependency rules', got, ['imports/boundary', 'imports/boundary', 'imports/boundary', 'imports/boundary', 'imports/boundary', 'deps/unlisted',
    'imports/boundary', 'deps/unlisted', 'deps/denied', 'deps/unlisted'])
}

// check-units
expect('units ok', [checkName('speed_mps', 'm/s'), checkName('pos', 'm'), checkName('timeout_ms', 'ms')], [null, null, null])
expect('units bad', [checkName('speed_ms', undefined) !== null, checkName('delay_sec', undefined) !== null, checkName('rate_ms', 'm/s') !== null,
  checkName('radius', 'm') !== null], [true, true, true, true])

if (failures) {
  console.error(`lint selftest: ${failures} failure(s)`)
  process.exit(1)
}
console.log('lint selftest: ok')
