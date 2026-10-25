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
import { checkEntry as release, MAX_BYTES, deploySecret } from './check-release.mjs'
import { checkVersions } from './check-version.mjs'
import { checkText as copy, applyRegister } from './copy-lint.mjs'
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
// LF-CHART-01 (lf-chart.mjs, AST): entropy, subtree rebuilds and constant ids in ui/lf/**; comments and other folders are not chart code
expect('chart code', rules((R) => lf(`${SRC}/ui/lf/Spark.tsx`, [
  '// Math.random() in a comment is fine',
  'const r = Math.random(); const u = globalThis.crypto.randomUUID(); el.innerHTML = ""; host.replaceChildren()',
  'g.setAttribute("id", "x"); g.id = `y`',
  'export const A = () => <svg><g id="a" /><g id={"b"} /><g id={uid} /></svg>',
].join('\n'), R)), ['LF-CHART-01', 'LF-CHART-01', 'LF-CHART-01', 'LF-CHART-01', 'LF-CHART-01', 'LF-CHART-01', 'LF-CHART-01', 'LF-CHART-01'])
expect('chart code: pure jitter and useId pass', rules((R) => lf(`${SRC}/ui/lf/Ok.tsx`,
  'import { rnd } from "./rnd"\nexport function B() { const id = React.useId(); return <g id={id} opacity={rnd(1, 2)} /> }', R)), [])
expect('chart rule only in ui/lf', rules((R) => lf(`${SRC}/ui/panels/x.ts`, 'const r = Math.random()', R)), [])
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

// check-brand: the /lab/ public build has its own header and boot-mask placements; other lab-public files are not placements
{
  const files = new Map([
    [`${SRC}/app/lab-public/LabHeader.tsx`, "const A = `${import.meta.env.BASE_URL}brand/avatar-96.png`"],
    ['apps/web/src/app/lab-public/index.html', '<img class="boot-badge" src="%BASE_URL%brand/anet-logo.svg" />'],
    [`${SRC}/app/lab-public/App.tsx`, "const A = `${import.meta.env.BASE_URL}brand/avatar-96.png`"],
    [`${SRC}/app/lab-public/pages.tsx`, "<img src='brand/anet-logo.svg'/>"],
    [`${SRC}/app/lab-public/sub/LabHeader.tsx`, "<img src='brand/avatar-96.png'/>"],
  ])
  const got = rules((R) => brand([...files.keys()], R, (f) => files.get(f)))
  expect('brand /lab/ placements', got, ['BRAND-02', 'BRAND-02', 'BRAND-02'])
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

// check-release (ADR-079); token shapes and the local secret value are assembled at runtime so this file passes REL-03
{
  const fakeKey = ['sk', 'proj', 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'].join('-')
  const ghTok = 'gh' + 'p_' + 'a'.repeat(36)
  const pem = ['-----BEGIN', 'PRIVATE KEY-----'].join(' ')
  const local = [{ file: 'svc.env', key: 'SVC_KEY', value: 'local-' + 'value-0123456789' }]
  const got = rules((R) => {
    release('docs/media/a.webp', MAX_BYTES + 1, null, [], R)
    release('docs/media/b.webp', MAX_BYTES, null, [], R)
    release('worlds/shenzhen/world.json', 10, '{}', [], R)
    release('tests/x/city.ply', 10, null, [], R)
    release('packages/contracts/fixtures/world/shenzhen/world.json', 10, '{}', [], R)
    release('a.md', 10, `key ${fakeKey}\n${ghTok}\n${pem}`, [], R)
    release('b.ts', 10, `const k = 'local-value-0123456789'`, local, R)
    release('c.ts', 10, 'const k = "local-value"', local, R)
  })
  expect('release rules', got, ['REL-01', 'REL-02', 'REL-02', 'REL-03', 'REL-03', 'REL-03', 'REL-03'])
}

// REL-04 (ADR-123): the deployment secret is generated at runtime; a short value is ignored; a missing file gives null
{
  const value = 'Dep' + Math.random().toString(36).slice(2, 10) + 'Z9'
  const dep = { source: 'tmp', value }
  const got = rules((R) => {
    release('d.ts', 10, `const p = "${value}"`, [], R, dep)
    release('e.ts', 10, 'const p = "unrelated"', [], R, dep)
    release('f.ts', 10, 'abc12', [], R, { source: 'tmp', value: 'abc12' })
  })
  expect('release deploy secret', got, ['REL-04'])
  expect('deploy secret missing', deploySecret('/nonexistent/awr/admin.secret'), null)
}

// check-version (ADR-118)
{
  const py = (v) => ({ file: 'pyproject.toml', text: `[build-system]\nrequires = []\n\n[project]\nname = "awr"\nversion = "${v}"\n\n[tool.x]\nversion = "9.9.9"\n` })
  const pkg = (f, v) => ({ file: f, text: `{\n  "name": "x",\n  "version": "${v}",\n  "dependencies": {"a": {"version": "1.0.0"}}\n}\n` })
  const init = (v) => ({ file: 'python/awr/__init__.py', text: `"doc"\n\n__version__ = "${v}"\n` })
  const check = (srcs) => {
    let ref
    const got = rules((R) => {
      ref = checkVersions(srcs, R)
    })
    return [ref, got]
  }
  expect('version ok', check([py('0.2.0'), pkg('apps/web/package.json', '0.2.0'), pkg('package.json', '0.2.0'), init('0.2.0')]), ['0.2.0', []])
  expect('version mismatch', check([py('0.2.0'), pkg('apps/web/package.json', '0.1.0'), pkg('package.json', '0.2.0'), init('0.2.0')]), ['0.2.0', ['VER-02']])
  expect('version format', check([py('0.2'), pkg('apps/web/package.json', '0.2.0-rc1')]), [null, ['VER-01', 'VER-01']])
}

// copy-lint (docs/05 §7.10; D3-AC-46, D3-AC-23, D3-AC-53): false positives (whitelist, menu ellipsis, truncation, quotas, code
// and URLs) and misses (every rule in both languages), the register and the first-mention rule; emoji are assembled at runtime
{
  const zh = (t, kind = 'md') => copy('x.zh-CN.md', t, { lang: 'zh', kind }).map((x) => x.rule)
  const en = (t, kind = 'md') => copy('x.md', t, { lang: 'en', kind }).map((x) => x.rule)
  const dict = (obj, lang) => copy(`d.${lang}.json`, JSON.stringify(obj, null, 1), { lang, kind: 'i18n' }).map((x) => `${x.rule}@${x.anchor}`)
  // false positives: none of these may be reported
  expect('copy zh whitelist', zh('请求超时，请稍后重试。数值超过上限，超出范围，超限告警，超配额提示。5 秒后重试，等待 30 秒，三秒内完成刷新。'), [])
  expect('copy zh unique and first', zh('事件编号必须唯一，作为唯一标识。按最佳优先策略排序。首个里程碑为最小基准闭环。'), [])
  expect('copy zh number with conditions', zh('单张 A40 上 720p 相机持续吞吐 ≥ 120 帧/s（RGB、深度，晴天档）。每个沙盒最多 12 架无人机。约 ~5 GB，间距 1~3 m。'), [])
  expect('copy menu ellipsis and truncation', dict({ 'menu.export': '导出数据集…', 'menu.replay': '进入回放…', 'lab.hint': '镜像摘要（sha256:…）。', 'menu.open': 'Open world…' }, 'zh'), [])
  expect('copy en clean', en([
    '## Highlights', '', 'ANet Drone4D renders the world. Drone4D streams it with `a != b` and `Awesome!!` in code.',
    '', '![Banner](docs/media/a.jpg)', '', '> [!NOTE]', '> Images are concept art.', '',
    '```sh', 'echo "Super fast!"', '```', '', 'See https://github.com/ANetResearch/ANet-Drone4D and serve with drone4d.serve or drone4d-camera.',
    '', 'Each sandbox supports up to 12 drones. Super-resolution uses a magic number. Request timed out after 30 s; retry.',
    '', 'Gazebo Sim, FAST-LIVO2, RotorPy, Livox MID-360, PX4, LiDAR, UrbanScene3D, FleetSim, GitHub and ANET_Q16 points.',
    '', 'Throughput is 120 fps at 720p RGB and depth on one A40 GPU (median of 3 runs).',
  ].join('\n')), [])
  expect('copy en labels', dict({ 'a.export': 'Export dataset', 'b.open': 'Open ANet Synthetic City', 'c.quote': 'No targets: add one under "Targets" first',
    'd.version': 'ANet Drone4D · World Runtime {version}', 'e.menu': 'Export…' }, 'en'), [])
  // W-06 (D3 copy review R-06): termination and failure deadlines are parameters, as "超时 30 秒" is in Chinese
  expect('copy en deadlines', en([
    'A queued run is cancelled immediately. A started run terminates within 10 s, and recordings written so far are retained for 14 days.',
    '', 'Started runs terminate within 10 s. Recordings written so far are retained for 14 days.',
    '', 'Data inspection could not open the recording within 10 s. Check that rerun-proxy and the catalog service are available, then retry.',
    '', 'Sessions are cancelled within 30 s of sign-out. The agent did not respond within 5 s.',
  ].join('\n')), [])
  expect('copy delivery names outside the interface', [...zh('| D3 | 事件沙盘 | 开发中 |\n\nD3.x 规划见路线图。'), ...en('Delivered in D2.')], [])
  expect('copy allow directive', en('ANet Drone4D docs.\n\n<!-- copy-lint-allow COPY-08 quoted from the review checklist -->\nDo not write "the first benchmark".'), [])
  // misses: each of these must be reported
  expect('copy zh phrases', zh('超快渲染，秒出结果，黑科技，一键搞定。'), ['COPY-01', 'COPY-01', 'COPY-01', 'COPY-01'])
  expect('copy zh tone', zh('完成啦。太棒了！好的～'), ['COPY-02', 'COPY-02', 'COPY-02'])
  expect('copy zh ellipsis', zh('正在处理……请等待…然后刷新。'), ['COPY-03', 'COPY-03'])
  expect('copy glyphs', zh(`完成 ${String.fromCodePoint(0x1f600)} 状态 ${String.fromCodePoint(0x2705)}`), ['COPY-04', 'COPY-04'])
  expect('copy zh numbers', zh('渲染 120 帧/s。最多 1,000 架无人机。性能提升 40%。首帧 ≤ 5 s 即可完成加载。'), ['COPY-05', 'COPY-05', 'COPY-05', 'COPY-05'])
  expect('copy en numbers', en('ANet Drone4D renders 240 fps. FleetSim runs up to 1,000 drones. It is 3x faster. The world loads in 2 s.'),
    ['COPY-05', 'COPY-05', 'COPY-05', 'COPY-05'])
  expect('copy names', en('ANet-Drone4D uses Fast-LIVO2, Rotorpy, Px4, Lidar, Gazebo sim and Drone 4D.'),
    ['COPY-06', 'COPY-06', 'COPY-06', 'COPY-06', 'COPY-06', 'COPY-06', 'COPY-06'])
  expect('copy first mention', en('Drone4D is a runtime. ANet Drone4D follows.'), ['COPY-06'])
  expect('copy first mention not in dictionaries', dict({ a: 'Drone4D runs' }, 'en'), [])
  expect('copy sentence case', dict({ 'x.export': 'Export Dataset', 'x.title': 'ANet Drone4D Public Demo', 'x.sentence': 'Export the dataset. Then Open it.' }, 'en'),
    ['COPY-07@x.export', 'COPY-07@x.title', 'COPY-07@x.title'])
  expect('copy heading sentence case', en('## Feature Tour\n\n### Reality to world'), ['COPY-07'])
  expect('copy zh superlatives', zh('业界首创，唯一支持，性能最强，业界领先。'), ['COPY-08', 'COPY-08', 'COPY-08', 'COPY-08'])
  expect('copy en superlatives', en('ANet Drone4D is the first open benchmark, industry-leading and state-of-the-art, best-in-class and world-class.'),
    ['COPY-08', 'COPY-08', 'COPY-08', 'COPY-08', 'COPY-08'])
  expect('copy en banned words', en('ANet Drone4D is an awesome, blazing-fast, seamless, cutting-edge runtime.'), ['COPY-01', 'COPY-01', 'COPY-01', 'COPY-01'])
  expect('copy slogans', [...en('<h3>Reality in, swarms out.</h3>\n\nANet Drone4D.'), ...zh('<h3>从真实世界，到无人机集群。</h3>')], ['COPY-09', 'COPY-09'])
  expect('copy terms', zh('在数字沙盘中打开。D3 事件实验室，极速模式，上帝视角，ANet Drone4D 2.0 发布。'), ['COPY-10', 'COPY-10', 'COPY-10', 'COPY-10', 'COPY-10'])
  expect('copy en deadline misses', en('ANet Drone4D is ready within 2 s. Runs complete within 5 s. The view does not stall and loads within 3 s. It stops and renders within 1 s.'),
    ['COPY-05', 'COPY-05', 'COPY-05', 'COPY-05'])
  // W-01 (R-04): delivery names D1 to D4 and D3.x are reported in interface strings; an acceptance number (D3-AC-14) is not one
  expect('copy delivery names in the interface', dict({ 'lab.login.title': 'D3 内部工作区登录', 'lab.budget': '（D2 点预算阶梯）', 'lab.plan': 'D3.x 规划',
    'lab.next': 'Planned for D4.', 'lab.band': 'D3-AC-14 区间', 'lab.ok': '事件沙盘内部工作区登录（V0.3）' }, 'zh'),
    ['COPY-10@lab.login.title', 'COPY-10@lab.budget', 'COPY-10@lab.plan', 'COPY-10@lab.next'])
  expect('copy delivery names in html', copy('p.html', '<html><head><title>D3 事件沙盘</title></head><body><p>交互沙盒</p></body></html>', { lang: 'zh', kind: 'html' })
    .map((x) => x.rule), ['COPY-10'])
  expect('copy sandbox naming by key', dict({ 'jobs.open': '在沙盘中打开', 'lab.title': '事件沙盘' }, 'zh'), ['COPY-10@jobs.open'])
  expect('copy rule filter', copy('r.md', '超快，业界首创。', { lang: 'zh', rules: ['COPY-08'] }).map((x) => x.rule), ['COPY-08'])
  // CITATION.cff: software title and abstract are copy, URLs and third-party references are not
  expect('copy cff', copy('CITATION.cff', 'cff-version: 1.2.0\ntitle: "ANet-Drone4D: A runtime"\nabstract: >-\n  ANet Drone4D is awesome.\nrepository-code: "https://github.com/ANetResearch/ANet-Drone4D"\nreferences:\n  - title: "Awesome!"\n',
    { lang: 'en', kind: 'cff' }).map((x) => x.rule), ['COPY-06', 'COPY-01'])
  // the register: an exception excuses its finding, a stale entry and a malformed entry are reported
  const found = copy('README.md', 'ANet Drone4D is awesome.', { lang: 'en' })
  const reg = { exceptions: [{ file: 'README.md', rule: 'COPY-01', match: 'awesome', anchor: 'ANet Drone4D is awesome.', reason: 'P-1 baseline' },
    { file: 'README.md', rule: 'COPY-05', match: '1,000 drones', reason: 'rewritten since' }], allow: [{ file: 'README.md', rule: 'COPY-02', match: '!', reason: 'x' }] }
  const res = applyRegister(found, reg)
  expect('copy register', [res.reported.length, res.excused.length, res.problems.map((p) => p.rule).sort()], [0, 1, ['COPY-BL-01', 'COPY-BL-01', 'COPY-BL-02']])
}

if (failures) {
  console.error(`lint selftest: ${failures} failure(s)`)
  process.exit(1)
}
console.log('lint selftest: ok')
