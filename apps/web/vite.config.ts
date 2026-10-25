// AWR 前端构建配置（M00 统一，AWR-03 ADR-037；来源 .cache/research/n05/trial/vite.config.ts）
import { execFileSync } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, statSync } from 'node:fs'
import { dirname, posix, resolve } from 'node:path'
import { build, defineConfig, type HtmlTagDescriptor, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { LANDING_I18N_KEYS, LANDING_LAB_I18N_KEYS } from './src/app/demo/landingKeys.ts'

// 公开演示构建（ADR-083；M15-FR-120；AWR-19 §3.7）：VITE_AWR_DEMO=public。默认构建不受影响；与测试开关互斥（生产包中不得
// 残留演示以外的测试开关）。入口换成 src/app/demo/entry.tsx（"/" 为不加载 three 的落地页，其余路由按需加载应用），
// dist/bench（UrbanScene3D 城市的 flight60 夹具，禁止再分发）不随演示构建发布，落地页媒体从 docs/media 复制到 dist/demo。
const DEMO = process.env.VITE_AWR_DEMO === 'public'
if (process.env.VITE_AWR_DEMO && !DEMO) throw new Error(`VITE_AWR_DEMO=${process.env.VITE_AWR_DEMO}: only "public" is defined (ADR-083)`)
if (DEMO && process.env.VITE_AWR_TEST_SWITCHES === '1') throw new Error('VITE_AWR_DEMO=public cannot be combined with VITE_AWR_TEST_SWITCHES=1 (ADR-083)')
// D2.2-L1: the landing page modules are app/demo/Landing*.ts(x) (header, sections, content, backdrop host, its own
// trimmed LandingI18n.ts); the mobile menu adds sheet.tsx and its icon shim. The voxel renderer app/demo/voxelField.ts
// stays out on purpose (its own lazy chunk). D2.2-FX-landing: the shared app/i18n module (zh-CN.json + en.json, ~2500
// keys) is no longer part of this group — Landing*.tsx reads its own virtual:awr-landing-i18n instead (landingI18nPlugin
// below), so the full bilingual dictionary stays out of the landing chunk; the lazily loaded start Sheet still reads
// app/i18n directly and gets it in its own chunk, the way it always has.
const LANDING_MODULES = /[\\/]src[\\/](app[\\/]demo[\\/]Landing\w*\.tsx?|app[\\/]lab-public[\\/]flags\.ts|ui[\\/]components[\\/]ui[\\/](button|badge|card|separator|sheet|table|toggle-group|toggle)\.tsx|ui[\\/]icons[\\/](Icon|registry|custom|types|lucide-compat)\.tsx?|ui[\\/]brand[\\/]assets\.ts|ui[\\/]testing[\\/]uxProbe\.ts|lib[\\/](utils|demo|testSwitches)\.ts)|vite[\\/]preload-helper|virtual:awr-landing-i18n/
const DEMO_MEDIA = ['demo-flight.webp', 'screenshot-hero.jpg', 'screenshot-streaming.jpg', 'screenshot-weather.jpg', 'screenshot-swarm.jpg',
  'screenshot-follow.jpg', 'screenshot-perf.jpg']

// D3 公开边界（扩展点 15，AWR-05 §12.3 第 15 处、§7.9、ADR-129；AWR-14 §21.2.3、§21.14；D3-AC-01、D3-AC-21）。两个构建期开关，缺省均为 0：
// - VITE_AWR_LAB_ONLY=1：`/lab/` 独立静态构建（公开只读样例，base = /lab/），入口换成 src/app/lab-public/entry.tsx，数据只来自
//   /lab-static/；内部工作区页面、bench 与实时客户端在构建期剔除（labPublicPlugin 的模块守卫），与演示构建、测试开关互斥。
// - VITE_AWR_LAB_PUBLIC=1：D2 演示构建显示"事件沙盘"入口（链接到 /lab/ 静态构建，lab-public/flags.ts LAB_PUBLIC_ENTRY）。
// 公开演示构建（VITE_AWR_DEMO=public）在任一开关下都不含内部工作区：labStripPlugin 在构建期把 D3 新增的内部模块（routes/lab*、
// ui/lab、stores/lab、net/bench.ts、i18n/lab 分片与 lab.* 文案键）从路由表、词典与 Tailwind 候选扫描中剔除，并从契约原因码表剔除 D3
// 新增的码（stripD3Reasons），开关为 0 时产物与
// D2 标签构建逐字节一致（tools/bench/d3/scan_public.py d2-parity 对拍）。
export function envSwitch(name: string, v: string | undefined): boolean {
  if (v === undefined || v === '' || v === '0') return false
  if (v === '1') return true
  throw new Error(`${name}=${v}: expected 0 or 1 (extension point 15)`)
}
const LAB_ONLY = envSwitch('VITE_AWR_LAB_ONLY', process.env.VITE_AWR_LAB_ONLY)
const LAB_PUBLIC = envSwitch('VITE_AWR_LAB_PUBLIC', process.env.VITE_AWR_LAB_PUBLIC)
if (LAB_ONLY && DEMO) throw new Error('VITE_AWR_LAB_ONLY=1 cannot be combined with VITE_AWR_DEMO=public: /lab/ is its own static build (ADR-129)')
if (LAB_ONLY && process.env.VITE_AWR_TEST_SWITCHES === '1') throw new Error('VITE_AWR_LAB_ONLY=1 cannot be combined with VITE_AWR_TEST_SWITCHES=1 (ADR-083)')
/** /lab/ 静态构建的 base（与 src/app/lab-public/flags.ts LAB_BASE 相同） */
export const LAB_BASE_PATH = '/lab/'
if (LAB_PUBLIC && !DEMO) throw new Error('VITE_AWR_LAB_PUBLIC=1 only applies to the public demo build (VITE_AWR_DEMO=public)')

// 场景目标引擎的实验编排界面（m23/scene-target-foundations；tools/deploy/targetlab）：VITE_AWR_TARGET_LIFE=1 只用于私有验收部署。
// 公开演示构建缺省（0）在构建期把生活群体生成器、行为预演与定制编排（含计费模型服务入口）的模块替换为空实现（TARGET_LIFE_STUBS），
// 从词典剔除其文案键与实验模板名（stripTargetLifeKeys），产物中不含 /target-life/ 路由与模型服务名称（K9 与 k9-d3 扫描）。
// 内部构建（未设 VITE_AWR_DEMO）不受影响；服务端路由另由 AWR_TARGET_LIFE_ENABLE 控制（缺省 404）。
const TARGET_LIFE = envSwitch('VITE_AWR_TARGET_LIFE', process.env.VITE_AWR_TARGET_LIFE)
if (TARGET_LIFE && !DEMO) throw new Error('VITE_AWR_TARGET_LIFE=1 only applies to the public demo build (internal builds always carry the target-life UI)')
/** 公开演示构建中替换为空实现的实验编排模块（相对 src/ 的路径 → 替换源码；导出名与原模块相同） */
export const TARGET_LIFE_STUBS: Readonly<Record<string, string>> = {
  'ui/panels/targets/LifeBuilder.tsx': 'export function LifeBuilder(_props) { return null }\n',
  'ui/panels/targets/BehaviorPreview.tsx': 'export function BehaviorPreview(_props) { return null }\n',
  'ui/sandbox/lifeApi.ts': 'const off = () => Promise.reject(new Error("target life disabled"))\nexport const lifeApi = { rehearsal: off, scene: off, preview: off }\n',
}
export function targetLifeStub(file: string): string | null {
  const f = file.split('\\').join('/')
  for (const [rel, code] of Object.entries(TARGET_LIFE_STUBS)) if (f.endsWith(`/src/${rel}`)) return code
  return null
}
/** 实验编排界面的文案键前缀与实验模板（T7–T13）的模板名键 */
export const TARGET_LIFE_KEY_RE = /^(targets\.(lifeBuilder|lifeClass|lifeVehicle)\.|targets\.rehearsal\.(?!noRecipe$)|sandbox\.template\.t(7|8|9|1[0-3])_)/
export function stripTargetLifeKeys(json: string): string {
  const d = JSON.parse(json) as Record<string, unknown>
  return JSON.stringify(Object.fromEntries(Object.entries(d).filter(([k]) => !TARGET_LIFE_KEY_RE.test(k))), null, 1)
}

/** D3 内部工作区的源码（只在内部构建）：任何公开构建（演示构建与 /lab/ 静态构建）都不得加载 */
export const LAB_INTERNAL_RE = /[\\/]src[\\/](app[\\/]routes[\\/]lab[^\\/]*\.tsx$|app[\\/]routes[\\/]lab[\\/]|app[\\/]i18n[\\/]lab[\\/]|ui[\\/]lab[\\/]|stores[\\/]lab[\\/]|net[\\/]bench\.ts$)/
/** /lab/ 公开只读构建的源码：演示构建不得加载（两个公开构建互不包含；开关与路径常量 flags.ts 除外，演示构建的入口链接读它） */
export const LAB_PUBLIC_SRC_RE = /[\\/]src[\\/]app[\\/]lab-public[\\/](?!flags\.ts$)/
/**
 * /lab/ 公开只读构建不得加载的模块（AWR-14 §21.2.3 规则 1：写入口与内部页面以构建期常量剔除；§21.14.2：只发起 /lab/ 与
 * /lab-static/ 的 GET、0 个 WS）：bench 客户端、实时与 D1/D2 接口客户端、应用路由表与页面、演示落地页、身份与会话、内部
 * 工作区页面及其写入口（editor、fleet、eval、runs、提交与排行榜）
 */
export const LAB_PUBLIC_FORBIDDEN_RE = new RegExp([
  String.raw`[\\/]src[\\/]net[\\/]`,
  String.raw`[\\/]src[\\/]app[\\/](routes|router|demo|shell|boot|query|power|providers)[\\/]`,
  String.raw`[\\/]src[\\/]main\.tsx$`,
  String.raw`[\\/]src[\\/]stores[\\/](identity|sandboxSession|capacity|jobs)\.ts$`,
  String.raw`[\\/]src[\\/]ui[\\/]lab[\\/](Lab\w+\.tsx|board\.ts|submissions\.ts|useBenchSession\.ts|(editor|fleet|eval|runs)[\\/])`,
].join('|'))

/** D3 新增、只在内部构建出现的源码目录与文件（相对 apps/web）：演示构建的 Tailwind 候选扫描不读它们，CSS 与 D2 一致 */
export const LAB_TAILWIND_EXCLUDE: readonly string[] = ['src/ui/lab', 'src/app/routes/lab', 'src/app/routes/lab*.tsx', 'src/app/i18n/lab',
  'src/app/lab-public', 'src/stores/lab', 'src/net/bench.ts', 'tests/lab']

/** 应用词典去掉 lab.* 键（内部工作区文案；D2 词典没有这些键，键序不变） */
export function stripLabKeys(json: string): string {
  const d = JSON.parse(json) as Record<string, unknown>
  return JSON.stringify(Object.fromEntries(Object.entries(d).filter(([k]) => !k.startsWith('lab.'))), null, 1)
}

/**
 * import.meta.glob 的路由收集在演示构建中排除 lab 路由：`'../routes/*.tsx'` 改写为 `['../routes/*.tsx', '!../routes/lab*.tsx',
 * '!../routes/lab/**']`；只收集 lab 目录（`routes/lab/`、`i18n/lab/`）的 glob 改写为空对象。返回 null 表示无需改写
 */
export function stripLabGlobs(code: string): string | null {
  let changed = false
  const out = code.replace(/import\.meta\.glob(<[^>]*>)?\(\s*(['"])([^'"]+)\2/g, (all, _g: string | undefined, q: string, pat: string) => {
    const slash = pat.search(/[*{[]/)
    const dir = slash < 0 ? pat : pat.slice(0, pat.lastIndexOf('/', slash) + 1)
    if (/(^|\/)lab\//.test(dir)) {
      changed = true
      return `import.meta.glob${_g ?? ''}(${q}${dir}__awr_lab_stripped__/*.none${q}`
    }
    if (!/(^|\/)routes\/$/.test(dir)) return all
    changed = true
    return `import.meta.glob${_g ?? ''}([${q}${pat}${q}, ${q}!${dir}lab*.tsx${q}, ${q}!${dir}lab/**${q}]`
  })
  return changed ? out : null
}

/** D3 引擎新增（engine/actors：磁带解码与可行走高度，演示构建中无引用者）：演示构建从引擎门面去掉它们的再导出，fzstd 不进入产物 */
export const ENGINE_ACTORS_RE = /[\\/]src[\\/]engine[\\/]actors[\\/]/
export function stripEngineActors(code: string): string | null {
  const out = code.replace(/^export\b[^;\n]*?\bfrom\s+['"]\.\/actors\/[^'"]*['"];?[ \t]*\r?\n?/gm, '')
  return out === code ? null : out
}

/**
 * D3 新增的原因码与关闭码（AWR-17 §18.12；reasons.json 的 d1 = V0.3）：原因码 600–699（世界、事件、物理、传感、Agent 与 bench 子区段）、
 * Agent WS 关闭码 4460–4469。D2 演示构建不产生也不解释这些码，但契约生成物 gen/ts/reasons.ts 把它们与 D2 码放在同一张表里，
 * 演示构建的 rt.worker 与 ui 两个 chunk 因此各大约 13 KB（K02 遗留，D3-AC-01 demo-parity）。演示构建在构建期从该模块剔除它们，
 * 剩余文本与 D2 标签的生成物逐字节相同（tools/bench/d3/scan_public.py d2-parity 对拍）。
 */
export const CONTRACT_REASONS_RE = /[\\/]packages[\\/]contracts[\\/]gen[\\/]ts[\\/]reasons\.ts$/
export const D3_REASON_CODES: readonly [number, number] = [600, 699]
export const D3_WS_CLOSE_CODES: readonly [number, number] = [4460, 4469]
const inRange = (n: number, [lo, hi]: readonly [number, number]): boolean => n >= lo && n <= hi

/**
 * 从 gen/ts/reasons.ts（tools/contracts/gen.mjs 的 genReasons 输出）去掉 D3 码：`Reason` 的 `  NAME: code,` 行、`REASONS` 的
 * `  code: { code: code, … },` 行与 `WS_CLOSE` 单行对象中的 D3 关闭码；其余行与键序不变。返回 null 表示无需改写
 */
export function stripD3Reasons(code: string): string | null {
  let out = code.replace(/^ {2}[A-Z][A-Z0-9_]*: (\d+),\r?\n/gm, (line, n: string) => (inRange(Number(n), D3_REASON_CODES) ? '' : line))
  out = out.replace(/^ {2}(\d+): \{ code: \1, [^\n]*\},\r?\n/gm, (line, n: string) => (inRange(Number(n), D3_REASON_CODES) ? '' : line))
  out = out.replace(/^(export const WS_CLOSE = \{ )([^}\n]*)( \} as const)/m, (_all, head: string, body: string, tail: string) =>
    head + body.split(', ').filter((e) => !inRange(Number(e.split(': ')[1]), D3_WS_CLOSE_CODES)).join(', ') + tail)
  return out === code ? null : out
}

function labStripPlugin(): Plugin {
  let root = '.'
  return {
    name: 'awr-lab-strip',
    apply: 'build',
    enforce: 'pre',
    configResolved(c) {
      root = c.root
    },
    transform(code, id) {
      const file = id.split('?')[0]
      if (/[\\/]src[\\/]app[\\/]i18n[\\/](zh-CN|en)\.json$/.test(file)) {
        const out = stripLabKeys(code)
        return { code: TARGET_LIFE ? out : stripTargetLifeKeys(out), map: null }
      }
      if (/[\\/]src[\\/]styles[\\/]index\.css$/.test(file)) {
        const rel = (p: string): string => posix.relative(posix.dirname(file.split('\\').join('/')), posix.join(root.split('\\').join('/'), p))
        return { code: `${code}\n${LAB_TAILWIND_EXCLUDE.map((p) => `@source not "${rel(p)}";`).join('\n')}\n`, map: null }
      }
      if (CONTRACT_REASONS_RE.test(file)) {
        const s = stripD3Reasons(code)
        return s === null ? null : { code: s, map: null }
      }
      if (/[\\/]src[\\/]engine[\\/]index\.ts$/.test(file)) {
        const s = stripEngineActors(code)
        return s === null ? null : { code: s, map: null }
      }
      if (/[\\/]src[\\/].*\.tsx?$/.test(file) && code.includes('import.meta.glob')) {
        const s = stripLabGlobs(code)
        return s === null ? null : { code: s, map: null }
      }
      return null
    },
    load(id) {
      const file = id.split('?')[0]
      if (LAB_INTERNAL_RE.test(file)) this.error(`awr-lab-strip: D3 internal workspace module ${file} in a public demo build (extension point 15)`)
      if (ENGINE_ACTORS_RE.test(file)) this.error(`awr-lab-strip: D3 engine module ${file} in the demo build (extension point 15)`)
      if (LAB_PUBLIC_SRC_RE.test(file)) this.error(`awr-lab-strip: /lab/ public build module ${file} in the demo build (ADR-129)`)
      if (!TARGET_LIFE) {
        const stub = targetLifeStub(file)
        if (stub !== null) return stub
      }
      return null
    },
  }
}

/** /lab/ 静态构建复制的品牌资源（public/brand 的子集；构建不复制 public/，其中的 bench 夹具与模型不随公开构建发布） */
export const LAB_BRAND_FILES: readonly string[] = ['anet-logo.svg', 'avatar-96.png', 'favicon-16.png', 'favicon-32.png']
export const LAB_PUBLIC_HTML = 'src/app/lab-public/index.html'

/**
 * /lab/ 公开构建的产物扫描字符串（与 tools/bench/d3/scan_public.py PROFILES['lab'] 相同；D3-AC-21 原文五项、bench 基址、ADR-129 的
 * bench 路径与 AWR-14 §21.14.2 规则 1 的旁证）
 */
export const LAB_SCAN_STRINGS: readonly string[] = ['/leaderboard', '/submissions', '/board', '/registry', '/api/bench/v1/', '/api/bench',
  '/api/bench/v1/runs', '/api/bench/v1/events', '/api/bench/v1/session', '/api/', 'new WebSocket', 'EventSource(']
/** 第三方 Wasm（锁定版本的 Rerun Web Viewer，F07 公开样例页的数据检视面板）：构建时重映射其中的源码路径与文档地址 */
export const LAB_VENDOR_WASM_RE = /(^|\/)re_viewer_bg(-[\w-]+)?\.wasm$/

/** 字节串查找（Node Buffer 的原生实现；构建期在 Node 中运行） */
const indexOfBytes = (hay: Uint8Array, needle: Uint8Array, from: number): number =>
  Buffer.from(hay.buffer, hay.byteOffset, hay.byteLength).indexOf(needle, from)

/**
 * 第三方 Wasm 的等长字符串重映射（F07；D3-AC-21"公开构建产物中 /registry 等字符串为 0"）。Rerun Web Viewer 的 Wasm 数据段含 Rust
 * panic 位置的源码绝对路径（`/home/runner/.cargo/registry/src/…`、`…/src/api/…`，相当于未使用 rustc --remap-path-prefix）与帮助文本
 * 中的文档地址，命中扫描字符串而与接口路由无关。逐处等长替换，不改变任何偏移与长度（数据段按指针 + 长度引用字符串）：
 *   主机名（`://registry.…`）→ 大写（主机名不区分大小写，地址仍可用）；
 *   http(s) 地址的路径段 → 前导 `/` 改为 `?`（地址仍可打开，落到上一级页面）；
 *   其他（源码路径）→ 段名首字母大写（只出现在 panic 消息中）。
 * 返回新数组与替换次数；处理后扫描字符串必须为 0，否则抛错（构建失败），由 tests/lab/labPublicSamples.test.ts 以锁定版本的 Wasm 核对。
 */
export function remapVendorStrings(src: Uint8Array, needles: readonly string[] = LAB_SCAN_STRINGS): { bytes: Uint8Array; replaced: number } {
  const out = new Uint8Array(src)
  const enc = new TextEncoder()
  const isUrlByte = (b: number): boolean => b > 0x20 && b < 0x7f && b !== 0x22 && b !== 0x27 && b !== 0x3c && b !== 0x3e && b !== 0x60
  let replaced = 0
  for (let pass = 0; pass < 4; pass++) {
    let changed = false
    for (const n of needles) {
      const nb = enc.encode(n)
      for (let i = indexOfBytes(out, nb, 0); i >= 0; i = indexOfBytes(out, nb, i + 1)) {
        // 向前找同一 URL 的 "://"（只跨 URL 字符）
        let k = i - 1
        while (k >= 0 && isUrlByte(out[k]!) && i - k < 512) k--
        const head = new TextDecoder().decode(out.subarray(k + 1, i + 1))
        const scheme = /^.*?https?:\/\/$/.test(head) ? 'host' : /https?:\/\/[^/]+(\/|$)/.test(head) ? 'path' : null
        if (scheme === 'host' || (out[i] === 0x2f && out[i - 1] === 0x2f && i >= 1 && out[i - 2] === 0x3a)) {
          for (let j = i; j < i + nb.length; j++) if (out[j]! >= 0x61 && out[j]! <= 0x7a) out[j] = out[j]! - 0x20
        } else if (scheme === 'path' && out[i] === 0x2f) {
          out[i] = 0x3f
        } else {
          // 首个字母大写；无字母（如 "EventSource("）时把首字节改为同宽的下划线
          const j = nb.findIndex((b) => b >= 0x61 && b <= 0x7a)
          if (j >= 0) out[i + j] = out[i + j]! - 0x20
          else out[i] = 0x5f
        }
        replaced++
        changed = true
      }
    }
    if (!changed) break
  }
  for (const n of needles) if (indexOfBytes(out, enc.encode(n), 0) >= 0) throw new Error(`remapVendorStrings: ${n} remains`)
  return { bytes: out, replaced }
}

function labPublicPlugin(): Plugin {
  let outDir = 'dist'
  let root = '.'
  return {
    name: 'awr-lab-public',
    configResolved(c) {
      root = c.root
      outDir = resolve(c.root, c.build.outDir)
    },
    transformIndexHtml: {
      order: 'pre',
      // the application's index.html (boot mask with the capacity line, src/main.tsx) is replaced by the template of this build
      handler: () => readFileSync(resolve(root, LAB_PUBLIC_HTML), 'utf8'),
    },
    load(id) {
      const file = id.split('?')[0]
      if (LAB_PUBLIC_FORBIDDEN_RE.test(file)) this.error(`awr-lab-public: ${file} is not allowed in the /lab/ public read-only build (AWR-14 §21.2.3 rule 1)`)
      return null
    },
    generateBundle(_o, bundle) {
      // 第三方 Wasm 的源码路径与文档地址等长重映射（remapVendorStrings；F07 公开样例页引入数据检视面板）
      for (const a of Object.values(bundle)) {
        if (a.type !== 'asset' || !LAB_VENDOR_WASM_RE.test(a.fileName) || typeof a.source === 'string') continue
        a.source = remapVendorStrings(a.source).bytes
      }
    },
    closeBundle() {
      mkdirSync(resolve(outDir, 'brand'), { recursive: true })
      for (const f of LAB_BRAND_FILES) copyFileSync(resolve(root, 'public/brand', f), resolve(outDir, 'brand', f))
    },
  }
}

function demoPlugin(): Plugin {
  let outDir = 'dist'
  let root = '.'
  return {
    name: 'awr-demo-public',
    apply: 'build',
    configResolved(c) {
      root = c.root
      outDir = resolve(c.root, c.build.outDir)
    },
    transformIndexHtml: {
      order: 'pre',
      handler(html) {
        return html
          .replace('/src/main.tsx', '/src/app/demo/entry.tsx')
          .replace('<title>ANet Drone4D</title>', '<title>ANet Drone4D · Public Demo</title>')
          // "/" is the landing page: the boot mask of the app stays hidden there (painted before any script runs)
          .replace('</style>', '      html[data-landing] #boot-mask { display: none; }\n    </style>\n    <script>if (location.pathname === "/") document.documentElement.setAttribute("data-landing", "")</script>')
      },
    },
    closeBundle() {
      rmSync(resolve(outDir, 'bench'), { recursive: true, force: true })
      mkdirSync(resolve(outDir, 'demo'), { recursive: true })
      for (const f of DEMO_MEDIA) copyFileSync(resolve(root, '../../docs/media', f), resolve(outDir, 'demo', f))
    },
  }
}

// 构建信息（ADR-118；AWR-04 §15.4；17 §17.20.1）：产品版本取 package.json（与 pyproject.toml 相等，tools/lint/check-version.mjs
// 在 make lint 中校验），通道、提交、构建时刻与发布 id 取构建环境 AWR_BUILD_CHANNEL、AWR_BUILD_SHA、AWR_BUILD_DIRTY、
// AWR_BUILD_TIME、AWR_RELEASE（tools/deploy/emax/build-local.sh 导出）；未设置时只读调用 git（没有 git 时为 nogit），构建时刻取
// SOURCE_DATE_EPOCH（可复现构建）或当前时刻，发布 id 为 local-<commit>。产出三处：常量 __AWR_BUILD__（lib/build.ts 读取）、
// index.html 的 <meta name="awr-build"> 与启动屏 .boot-version 的静态文本（任何模块脚本执行之前即可见）、dist/build-info.json。
export interface AwrBuildInfo {
  version: string
  channel: 'demo' | 'local' | 'ci'
  commit: string
  dirty: boolean
  built_at: string
  release: string
}
export const BUILD_CHANNELS = ['demo', 'local', 'ci'] as const
export const BUILD_RE = {
  version: /^\d+\.\d+\.\d+$/,
  commit: /^(?:[0-9a-f]{10}|nogit)$/,
  built_at: /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/,
  // 部署包 <UTC 日期>-<UTC 时刻>-<提交>[-dirty]（没有 git 的构建机为 nogit），本机构建 local-<提交>
  release: /^(?:\d{8}-\d{6}-(?:[0-9a-f]{10}|nogit)(?:-dirty)?|local-(?:[0-9a-f]{10}|nogit))$/,
}

type GitFn = (args: string[]) => string | null
const gitCli: GitFn = (args) => {
  try {
    return execFileSync('git', args, { cwd: import.meta.dirname, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()
  } catch {
    return null
  }
}

function envBool(v: string | undefined): boolean | null {
  if (v === undefined || v === '') return null
  if (/^(1|true|yes)$/i.test(v)) return true
  if (/^(0|false|no)$/i.test(v)) return false
  throw new Error(`AWR_BUILD_DIRTY=${v}: expected true or false`)
}

/** collect the build info (pure apart from `git`; throws on malformed environment values) */
export function collectBuildInfo(env: Record<string, string | undefined>, opts: { demo: boolean; version: string; git?: GitFn; now?: Date }): AwrBuildInfo {
  const git = opts.git ?? gitCli
  const version = opts.version
  if (!BUILD_RE.version.test(version)) throw new Error(`package.json version ${version}: expected X.Y.Z (ADR-118)`)
  const ch = env.AWR_BUILD_CHANNEL || (opts.demo ? 'demo' : env.CI ? 'ci' : 'local')
  if (!(BUILD_CHANNELS as readonly string[]).includes(ch)) throw new Error(`AWR_BUILD_CHANNEL=${ch}: expected demo, local or ci`)
  let commit = (env.AWR_BUILD_SHA || git(['rev-parse', '--short=10', 'HEAD']) || 'nogit').toLowerCase()
  if (commit !== 'nogit') commit = commit.slice(0, 10)
  if (!BUILD_RE.commit.test(commit)) throw new Error(`AWR_BUILD_SHA=${commit}: expected 10 hex digits or nogit`)
  const dirtyEnv = envBool(env.AWR_BUILD_DIRTY)
  const dirty = dirtyEnv ?? (commit !== 'nogit' && (git(['status', '--porcelain']) ?? '') !== '')
  let builtAt = env.AWR_BUILD_TIME
  if (!builtAt) {
    const epoch = env.SOURCE_DATE_EPOCH ? Number(env.SOURCE_DATE_EPOCH) * 1000 : (opts.now ?? new Date()).getTime()
    if (!Number.isFinite(epoch)) throw new Error(`SOURCE_DATE_EPOCH=${env.SOURCE_DATE_EPOCH}: not a number`)
    builtAt = new Date(epoch).toISOString().replace(/\.\d{3}Z$/, 'Z')
  }
  if (!BUILD_RE.built_at.test(builtAt)) throw new Error(`AWR_BUILD_TIME=${builtAt}: expected YYYY-MM-DDTHH:MM:SSZ`)
  const release = env.AWR_RELEASE || `local-${commit}`
  if (!BUILD_RE.release.test(release)) throw new Error(`AWR_RELEASE=${release}: expected <YYYYMMDD>-<HHMMSS>-<commit>[-dirty] or local-<commit>`)
  return { version, channel: ch as AwrBuildInfo['channel'], commit, dirty, built_at: builtAt, release }
}

/** boot-screen text: `V0.2.0-demo · 3f9a1c2 · 2026-10-12` (`-dirty` after the commit; `nogit` without git) */
export function buildDisplay(b: AwrBuildInfo): string {
  const c = b.commit === 'nogit' ? 'nogit' : b.commit.slice(0, 7)
  return `V${b.version}-${b.channel} · ${c}${b.dirty ? '-dirty' : ''} · ${b.built_at.slice(0, 10)}`
}

/** content of `<meta name="awr-build">`: `0.2.0-demo+3f9a1c2d4e` (`-dirty` appended) */
export function buildMeta(b: AwrBuildInfo): string {
  return `${b.version}-${b.channel}+${b.commit}${b.dirty ? '-dirty' : ''}`
}

const escapeHtml = (s: string): string => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

/** the index.html placeholder `<div class="boot-version"></div>` filled with the display text */
export function fillBootVersion(html: string, b: AwrBuildInfo): string {
  return html.replace(/(<div class="boot-version"[^>]*>)[^<]*(<\/div>)/, `$1${escapeHtml(buildDisplay(b))}$2`)
}

interface ChunkLike {
  type: 'chunk' | 'asset'
  fileName: string
  isEntry?: boolean
  facadeModuleId?: string | null
  imports?: string[]
  code?: string
}

/** byte manifest of the application path (M15-FR-201 bundle segment; request of WP-12): the entry chunk and the chunk of
 *  src/main.tsx (a dynamic import of the demo entry) with their static imports, `{chunks: [{href, bytes}], total}` */
export function bootManifest(bundle: Record<string, ChunkLike>, base = '/'): { chunks: { href: string; bytes: number }[]; total: number } {
  const chunks = Object.values(bundle).filter((c) => c.type === 'chunk')
  const byName = new Map(chunks.map((c) => [c.fileName, c]))
  const start = chunks.filter((c) => c.isEntry || /[\\/]src[\\/]main\.tsx$/.test(c.facadeModuleId ?? ''))
  const seen = new Set<string>()
  const stack = start.map((c) => c.fileName)
  while (stack.length > 0) {
    const f = stack.pop() as string
    if (seen.has(f)) continue
    seen.add(f)
    for (const i of byName.get(f)?.imports ?? []) if (!seen.has(i)) stack.push(i)
  }
  const out = [...seen].sort().map((f) => ({ href: base + f, bytes: Buffer.byteLength(byName.get(f)?.code ?? '', 'utf8') }))
  return { chunks: out, total: out.reduce((a, c) => a + c.bytes, 0) }
}

function buildInfoPlugin(info: AwrBuildInfo): Plugin {
  let root = '.'
  let base = '/'
  return {
    name: 'awr-build-info',
    config() {
      return { define: { __AWR_BUILD__: JSON.stringify(info) } }
    },
    configResolved(c) {
      root = c.root
      base = c.base
    },
    transformIndexHtml: {
      order: 'post',
      handler(html, ctx) {
        const tags: HtmlTagDescriptor[] = [{ tag: 'meta', attrs: { name: 'awr-build', content: buildMeta(info) }, injectTo: 'head' }]
        // PWA manifest (M15, AWR-04 §15.8.7): linked only once public/manifest.webmanifest exists (a 404 would log a console error)
        // (not in the /lab/ build: the manifest sits outside /lab/, and that build only requests /lab/ and /lab-static/)
        if (!LAB_ONLY && existsSync(resolve(root, 'public', 'manifest.webmanifest'))) {
          tags.push({ tag: 'link', attrs: { rel: 'manifest', href: '/manifest.webmanifest' }, injectTo: 'head' })
        }
        let out = fillBootVersion(html, info)
        if (ctx.bundle) {
          // a classic script after the stylesheet link would hold the parser until the stylesheet arrives (script-blocking
          // style sheet), and with it the boot mask markup the early capacity line writes into (D2-FX-R2-web, D2-AC-41):
          // the manifest goes right after the charset meta, before every stylesheet
          const m = bootManifest(ctx.bundle as unknown as Record<string, ChunkLike>, base)
          const at = out.indexOf('<meta charset="UTF-8" />')
          if (at < 0) throw new Error('awr-build-info: index.html has no charset meta')
          const end = at + '<meta charset="UTF-8" />'.length
          out = `${out.slice(0, end)}\n    <script>window.__AWR_BOOT_MANIFEST=${JSON.stringify(m)}</script>${out.slice(end)}`
        }
        return { html: out, tags }
      },
    },
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'build-info.json', source: `${JSON.stringify(info, null, 1)}\n` })
    },
  }
}

const PKG_VERSION = (JSON.parse(readFileSync(resolve(import.meta.dirname, 'package.json'), 'utf8')) as { version: string }).version
const BUILD_INFO = collectBuildInfo(process.env, { demo: DEMO || LAB_ONLY, version: PKG_VERSION })

// /sandbox/api 独立分块（M15-FR-221："独立分块，不加载 three 与引擎"；WP-12 请求第 1 条）：演示构建的入口在 /sandbox/api 动态导入
// app/demo/apiDocsEntry.tsx。按目录分组时它的依赖落进 ui 分块，ui 分块又静态导入 r3f 与 three，因此这里在配置时算出该入口的
// 运行期静态导入闭包（跳过 `import type` 与只含类型的导入、动态 import 与 node_modules），把闭包中不属于落地页分块的模块单独
// 分成 apidocs 组（放在 ui 组之前），zustand 与 use-sync-external-store 单独分成 state 组（放在 r3f 组之前）。应用路径的行为不变：
// 应用同样引用 apidocs 分块中的共享模块。
const SRC_DIR = resolve(import.meta.dirname, 'src')
const RESOLVE_EXT = ['', '.ts', '.tsx', '/index.ts', '/index.tsx']

/** specifiers of the runtime static imports and re-exports of one module (type-only imports are erased by the compiler) */
export function runtimeImports(text: string): string[] {
  const out: string[] = []
  for (const m of text.matchAll(/^\s*(?:import|export)\s+(?!type\b)([^;'"]*?)\s+from\s+['"]([^'"]+)['"]/gm)) {
    const clause = m[1].trim()
    const named = /^\{([^}]*)\}$/.exec(clause)
    if (named && named[1].split(',').map((x) => x.trim()).filter(Boolean).every((x) => x.startsWith('type '))) continue
    out.push(m[2])
  }
  for (const m of text.matchAll(/^\s*import\s+['"]([^'"]+)['"]/gm)) out.push(m[1])
  return out
}

/** runtime static import closure (absolute paths) of a module under src/ */
export function staticClosure(entry: string, srcDir = SRC_DIR): Set<string> {
  const resolveSpec = (from: string, spec: string): string | null => {
    const base = spec.startsWith('@/') ? resolve(srcDir, spec.slice(2)) : spec.startsWith('.') ? resolve(dirname(from), spec) : null
    if (base === null) return null
    for (const e of RESOLVE_EXT) {
      const p = base + e
      if (existsSync(p) && statSync(p).isFile()) return p
    }
    return null
  }
  const seen = new Set<string>()
  const stack = [resolve(srcDir, entry)]
  while (stack.length > 0) {
    const f = stack.pop() as string
    if (seen.has(f) || !existsSync(f)) continue
    seen.add(f)
    if (!/\.(ts|tsx)$/.test(f)) continue
    for (const spec of runtimeImports(readFileSync(f, 'utf8'))) {
      const p = resolveSpec(f, spec)
      if (p !== null && !seen.has(p)) stack.push(p)
    }
  }
  return seen
}

const APIDOCS_MODULES = DEMO ? staticClosure('app/demo/apiDocsEntry.tsx') : new Set<string>()
const apidocsGroup = (id: string): boolean => {
  const file = id.split('?')[0]
  return APIDOCS_MODULES.has(file) && !LANDING_MODULES.test(file)
}

// R3F 9.8.1 在每个 store 里 new THREE.Clock()，three r183 起 Clock 已弃用、构造时打印一次弃用告警（FX-UBO，ADR-086）。
// 只把 @react-three/fiber 自身模块对 'three' 的导入解析到 src/viewport/r3fThree.ts（three 原样再导出，Clock 换成字段与
// 语义相同、不告警的 R3fClock）；其余模块与该文件自身对 'three' 的导入不受影响。vite dev 与 Vitest 浏览器模式的依赖
// 预构建不经过插件，告警只在这两处保留。
const R3F_THREE = resolve(import.meta.dirname, 'src/viewport/r3fThree.ts')
function r3fClockPlugin(): Plugin {
  return {
    name: 'awr-r3f-clock',
    enforce: 'pre',
    resolveId(source, importer) {
      if (source === 'three' && importer && /[\\/]@react-three[\\/]fiber[\\/]/.test(importer)) return R3F_THREE
      return null
    },
  }
}

// 同源着色器共用程序（D2-FX-R2-web，D2-AC-01；ADR-007 的着色器预热）：three r186 的 WebGLNodesHandler 以节点图的缓存键作为程序
// 缓存键，而节点缺省的 customCacheKey() 是实例 id，因此两个结构相同、生成的 GLSL 逐字相同的节点材质（同一工厂分别创建的线、
// 四边形线、雨雪材质）各自编译链接一个程序：synthcity 演示场景揭开前链接 41 个程序，其中 8 个与已有程序源码逐字相同，Tier S
// （SwiftShader）每个约 0.2–0.3 s，全部在遮罩下的着色器预热里同步等待。这里只改 WebGLPrograms.acquireProgram 的查表键：
// RawShaderMaterial 路径（节点材质经 WebGLNodesHandler 都走这条路径）以 WebGLProgram 实际读取的全部输入（GLSL 版本、
// SHADER_TYPE、SHADER_NAME、defines、属性 0 的绑定、光源与裁剪平面数、并行编译开关、两段源码）为键，键相同即复用同一个
// WebGLProgram（usedTimes 计数，与内置材质共用程序的机制相同；uniform 按材质逐次上传，UBO 由 UboBinder 按材质的组绑定），
// 其余材质的键不变。只在构建时生效（vite dev 与 Vitest 浏览器模式的依赖预构建不经过插件，结果只是少了去重）；three 升级后
// 匹配不到原文时构建失败，必须重新核对 WebGLProgram 读取的参数。
const THREE_ACQUIRE = '\tfunction acquireProgram( parameters, cacheKey ) {\n\n\t\tlet program = programsMap.get( cacheKey );'
const THREE_ACQUIRE_PATCHED = `\tfunction awrSourceKey( p, cacheKey ) {

\t\tif ( p.isRawShaderMaterial !== true ) return cacheKey;
\t\treturn 'awr-src\\u0001' + [ p.glslVersion, p.shaderType, p.shaderName, generateDefines( p.defines ), p.index0AttributeName, p.hasPositionAttribute,
\t\t\tp.rendererExtensionParallelShaderCompile, p.numSunLights, p.numDirLights, p.numSpotLights, p.numSpotLightMaps, p.numRectAreaLights, p.numPointLights,
\t\t\tp.numHemiLights, p.numSunLightShadows, p.numDirLightShadows, p.numSpotLightShadowsWithMaps, p.numSpotLightShadows, p.numPointLightShadows,
\t\t\tp.numClippingPlanes, p.numClipIntersection, p.vertexShader, p.fragmentShader ].join( '\\u0001' );

\t}

\tfunction acquireProgram( parameters, cacheKey ) {

\t\tcacheKey = awrSourceKey( parameters, cacheKey );
\t\tlet program = programsMap.get( cacheKey );`
export function patchThreePrograms(code: string): string {
  const at = code.indexOf(THREE_ACQUIRE)
  if (at < 0 || code.indexOf(THREE_ACQUIRE, at + 1) >= 0 || !code.includes('function generateDefines( defines )')) {
    throw new Error('awr-three-programs: WebGLPrograms.acquireProgram of three changed; re-check the program key (vite.config.ts)')
  }
  return code.slice(0, at) + THREE_ACQUIRE_PATCHED + code.slice(at + THREE_ACQUIRE.length)
}
function threeProgramsPlugin(): Plugin {
  return {
    name: 'awr-three-programs',
    apply: 'build',
    transform(code, id) {
      if (!/[\\/]node_modules[\\/]three[\\/]build[\\/]three\.module\.js$/.test(id.split('?')[0])) return null
      return { code: patchThreePrograms(code), map: null }
    },
  }
}

// 启动早期模块的文案（D2-FX-R2-web，D2-AC-41）：index.html 的第一个模块脚本 src/app/boot/early.ts 在应用分块下载完成之前
// 发出容量请求并写出容量行，它只带 zh-CN 字典里 capacity.* 的键（约 1 KB，同一份 zh-CN.json，不另写文案），不加载 i18n
// 运行时与两份完整字典。它的依赖在应用里单独成 boot 分块（放在全部分组之前，演示构建的落地页分块照常引用它）。
export const BOOT_I18N_ID = 'virtual:awr-boot-i18n'
export function bootCapacityDict(zh: Readonly<Record<string, string>>): Record<string, string> {
  return Object.fromEntries(Object.entries(zh).filter(([k]) => k.startsWith('capacity.')))
}
function bootI18nPlugin(): Plugin {
  const RESOLVED = `\0${BOOT_I18N_ID}`
  return {
    name: 'awr-boot-i18n',
    resolveId(id) {
      return id === BOOT_I18N_ID ? RESOLVED : null
    },
    load(id) {
      if (id !== RESOLVED) return null
      const zh = JSON.parse(readFileSync(resolve(import.meta.dirname, 'src/app/i18n/zh-CN.json'), 'utf8')) as Record<string, string>
      return `export default ${JSON.stringify(bootCapacityDict(zh))}\n`
    },
  }
}

// 落地页的精简词典（D2.2-FX-landing）：落地页只读 landingKeys.ts 列出的一百多个键，之前却把完整的中英文词典（约 2500
// 键，约 230 KB 原始字节）一起打进了落地页首屏分块（约占该分块 92% 的字节）。这个虚拟模块在构建期按同样的键清单从
// zh-CN.json/en.json 筛出两份小词典，供 src/app/demo/LandingI18n.ts 读取，仓库里的两份完整词典保持唯一数据源，不另外
// 手抄一份（避免走样）；apps/web/tests/m15/landingDict.test.ts 用 LandingContent.ts 真实的 id 复核该键清单有没有漏改。
export const LANDING_I18N_ID = 'virtual:awr-landing-i18n'
export function landingI18nDict(zh: Readonly<Record<string, string>>, en: Readonly<Record<string, string>>,
  keys: readonly string[] = LANDING_I18N_KEYS): { 'zh-CN': Record<string, string>; en: Record<string, string> } {
  const pick = (d: Readonly<Record<string, string>>): Record<string, string> => {
    const out: Record<string, string> = {}
    for (const k of keys) {
      if (!(k in d)) throw new Error(`virtual:awr-landing-i18n: missing key ${k}`)
      out[k] = d[k]
    }
    return out
  }
  return { 'zh-CN': pick(zh), en: pick(en) }
}
function landingI18nPlugin(): Plugin {
  const RESOLVED = `\0${LANDING_I18N_ID}`
  return {
    name: 'awr-landing-i18n',
    resolveId(id) {
      return id === LANDING_I18N_ID ? RESOLVED : null
    },
    load(id) {
      if (id !== RESOLVED) return null
      const zh = JSON.parse(readFileSync(resolve(import.meta.dirname, 'src/app/i18n/zh-CN.json'), 'utf8')) as Record<string, string>
      const en = JSON.parse(readFileSync(resolve(import.meta.dirname, 'src/app/i18n/en.json'), 'utf8')) as Record<string, string>
      // VITE_AWR_LAB_PUBLIC=1: the Event Lab entries of the landing page (landingKeys.ts LANDING_LAB_I18N_KEYS)
      return `export default ${JSON.stringify(landingI18nDict(zh, en, LAB_PUBLIC ? LANDING_LAB_I18N_KEYS : LANDING_I18N_KEYS))}\n`
    },
  }
}
// v0.3.1: the page load meter of the single loading bar (app/boot/loadMeter.ts, no imports) sits in the same small chunk: the
// demo entry starts it before the application chunk, and the application continues it (otherwise it would be bundled
// into the shared ui chunk and the landing page would load the ui chunk)
const BOOT_MODULES = /[\\/]src[\\/]app[\\/](boot[\\/](capacityText|loadMeter)\.ts|i18n[\\/]plural\.ts)$|virtual:awr-boot-i18n/
// 早期模块内联（D2-FX-R2-web，D2-AC-41）：模块脚本要等 HTML 解析结束（domInteractive，被样式表与内联脚本阻塞，本机冷启动
// 实测 0.3–1.1 s）才执行，而 vite 又把 index.html 里的全部模块脚本合成一个入口，因此 src/app/boot/early.ts 在构建时单独打成
// IIFE（同一份 TS 源码、boot 文案与 capacityText，不另写逻辑），作为 <head> 里第一个内联经典脚本插在 charset 之后、样式表
// 之前：HTML 一解析到它就发出容量请求，应答在到达的那个任务里写出容量行。开发服务器插入源文件的模块脚本。
const EARLY_ENTRY = resolve(import.meta.dirname, 'src/app/boot/early.ts')
const CHARSET_META = '<meta charset="UTF-8" />'
/** the inline early script right after the charset meta (before every stylesheet and module script) */
export function insertEarlyInline(html: string, code: string): string {
  const at = html.indexOf(CHARSET_META)
  if (at < 0) throw new Error('awr-early-inline: index.html has no charset meta')
  const safe = code.replace(/<\/script/gi, '<\\/script')
  const end = at + CHARSET_META.length
  return `${html.slice(0, end)}\n    <script data-awr-early="">${safe.trim()}</script>${html.slice(end)}`
}
async function buildEarlyInline(info: AwrBuildInfo): Promise<string> {
  const out = await build({
    configFile: false, logLevel: 'warn', root: import.meta.dirname, publicDir: false,
    resolve: { alias: { '@': resolve(import.meta.dirname, 'src') } },
    define: { __AWR_BUILD__: JSON.stringify(info) },
    plugins: [bootI18nPlugin()],
    build: {
      write: false, minify: true, target: 'es2023', copyPublicDir: false, emptyOutDir: false, reportCompressedSize: false,
      lib: { entry: EARLY_ENTRY, formats: ['iife'], name: 'awrEarlyBoot', fileName: () => 'early.js' },
    },
  })
  const res = Array.isArray(out) ? out[0] : out
  const chunk = 'output' in res ? res.output.find((o) => o.type === 'chunk') : undefined
  if (!chunk || chunk.type !== 'chunk') throw new Error('awr-early-inline: no IIFE chunk')
  return chunk.code
}
function earlyInlinePlugin(info: AwrBuildInfo): Plugin {
  let code: Promise<string> | null = null
  return {
    name: 'awr-early-inline',
    transformIndexHtml: {
      order: 'post',
      async handler(html, ctx) {
        if (!ctx.bundle) {
          const at = html.indexOf('<script type="module"')
          if (at < 0) return html
          return `${html.slice(0, at)}<script type="module" src="/src/app/boot/early.ts"></script>\n    ${html.slice(at)}`
        }
        code ??= buildEarlyInline(info)
        return insertEarlyInline(html, await code)
      },
    },
  }
}

// 跨源隔离（AWR-11 TECH-FR-007）：dev 与 preview 都返回 COOP same-origin 与 COEP require-corp
const COI_HEADERS = {
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Embedder-Policy': 'require-corp',
}

// 端口规划（AWR-19 OPS-FR-002）：api 8000、vite 5173、preview 4173，AWR_PORT_OFFSET = k 时各加 10·k
const offset = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0
const apiTarget = `http://127.0.0.1:${8000 + 10 * offset}`
// D3 bench-api（AWR-17 §18.3；内部工作区 /lab/**）：缺省本机 18650，可由 AWR_BENCH_TARGET 指向其它实例
const benchTarget = process.env.AWR_BENCH_TARGET ?? 'http://127.0.0.1:18650'

// 代理表（AWR-03 §3.3）：/api（含 /api/rt 的 WS 升级）与 /worlds/<id>/**、/worlds/_shared/** 数据路径转发到 api 进程。
// /assets/ 是 Vite 构建产物前缀，不作代理（AWR-03 §3.3、AWR-16 F-09、R2-42）；/worlds 本身是前端路由（World Hub，
// M15-FR-003），因此 /worlds 只以正则匹配带子路径的数据请求。preview 显式使用同一张表（默认会继承 server.proxy）。
const PROXY = {
  '/api/sandbox': { target: `http://127.0.0.1:${8002 + 10 * offset}`, ws: true },
  '/api/bench': { target: benchTarget }, // 须在 /api 之前（前缀按声明序匹配）
  '/api': { target: apiTarget, ws: true },
  '^/worlds/.+': { target: apiTarget },
  '^/vehicles/.+': { target: apiTarget }, // vehicle model glb (AWR-17 §5; INT-1)
}

export default defineConfig({
  // /lab/ build: no early inline module (its capacity request is an API call) and no public/ copy (labPublicPlugin copies the brand files)
  plugins: [...(DEMO ? [labStripPlugin()] : []), r3fClockPlugin(), threeProgramsPlugin(), bootI18nPlugin(), landingI18nPlugin(),
    ...(LAB_ONLY ? [] : [earlyInlinePlugin(BUILD_INFO)]), react(), tailwindcss(), buildInfoPlugin(BUILD_INFO), ...(DEMO ? [demoPlugin()] : []),
    ...(LAB_ONLY ? [labPublicPlugin()] : [])],
  ...(LAB_ONLY ? { base: LAB_BASE_PATH, publicDir: false as const } : {}),
  resolve: { tsconfigPaths: true },
  server: {
    host: '127.0.0.1',
    port: 5173 + 10 * offset,
    strictPort: true,
    headers: COI_HEADERS,
    proxy: PROXY,
  },
  preview: {
    host: '127.0.0.1',
    port: 4173 + 10 * offset,
    strictPort: true,
    headers: COI_HEADERS,
    proxy: PROXY,
  },
  // 演示构建的 Worker（rt.worker）单独打包，同样经 labStripPlugin（其中的契约原因码表剔除 D3 码，扩展点 15）
  worker: { format: 'es', ...(DEMO ? { plugins: () => [labStripPlugin()] } : {}) },
  build: {
    target: 'es2023',
    rolldownOptions: {
      output: {
        // AWR-18 §6.3 第 3 条：按 three、react、r3f、ui、engine、net 分组
        codeSplitting: {
          groups: [
            // the early boot module's dependencies (capacity texts, plural formatter, capacity keys): one small chunk that
            // the first module script of index.html loads before the application chunks (D2-FX-R2-web, D2-AC-41)
            { name: 'boot', test: BOOT_MODULES },
            { name: 'three', test: /node_modules[\\/]three[\\/]/ },
            // React's own useSyncExternalStore shim sits with React (D2.2-L1): Base UI's Dialog store (the landing page's
            // mobile menu Sheet) needs it on the first screen, which may load only index, landing, react and the runtime
            { name: 'react', test: /node_modules[\\/](react|react-dom|scheduler|use-sync-external-store)[\\/]/ },
            // stores (zustand) are shared by the open interface page and the application, not part of r3f (WP-12 request)
            { name: 'state', test: /node_modules[\\/]zustand[\\/]/ },
            { name: 'r3f', test: /node_modules[\\/]@react-three[\\/]/ },
            // demo build: exactly the modules of the landing page (and, recursively, their dependencies not captured above)
            // form their own chunk; otherwise the landing page would import the whole ui chunk and, through it, the
            // engine and three (ADR-083)
            ...(DEMO ? [{ name: 'landing', test: LANDING_MODULES }] : []),
            // demo build: the open interface page /sandbox/api without three, r3f, ui and engine (M15-FR-221)
            ...(DEMO ? [{ name: 'apidocs', test: apidocsGroup }] : []),
            { name: 'ui', test: /[\\/]src[\\/]ui[\\/]/ },
            { name: 'engine', test: /[\\/]src[\\/]engine[\\/]/ },
            { name: 'net', test: /[\\/]src[\\/]net[\\/]/ },
          ],
        },
      },
    },
  },
})
