// AWR 前端构建配置（M00 统一，AWR-03 ADR-037；来源 .cache/research/n05/trial/vite.config.ts）
import { execFileSync } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, statSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { build, defineConfig, type HtmlTagDescriptor, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { LANDING_I18N_KEYS } from './src/app/demo/landingKeys.ts'

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
const LANDING_MODULES = /[\\/]src[\\/](app[\\/]demo[\\/]Landing\w*\.tsx?|ui[\\/]components[\\/]ui[\\/](button|badge|card|separator|sheet|table|toggle-group|toggle)\.tsx|ui[\\/]icons[\\/](Icon|registry|custom|types|lucide-compat)\.tsx?|ui[\\/]brand[\\/]assets\.ts|ui[\\/]testing[\\/]uxProbe\.ts|lib[\\/](utils|demo|testSwitches)\.ts)|vite[\\/]preload-helper|virtual:awr-landing-i18n/
const DEMO_MEDIA = ['demo-flight.webp', 'screenshot-hero.jpg', 'screenshot-streaming.jpg', 'screenshot-weather.jpg', 'screenshot-swarm.jpg',
  'screenshot-follow.jpg', 'screenshot-perf.jpg']

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
  return {
    name: 'awr-build-info',
    config() {
      return { define: { __AWR_BUILD__: JSON.stringify(info) } }
    },
    configResolved(c) {
      root = c.root
    },
    transformIndexHtml: {
      order: 'post',
      handler(html, ctx) {
        const tags: HtmlTagDescriptor[] = [{ tag: 'meta', attrs: { name: 'awr-build', content: buildMeta(info) }, injectTo: 'head' }]
        // PWA manifest (M15, AWR-04 §15.8.7): linked only once public/manifest.webmanifest exists (a 404 would log a console error)
        if (existsSync(resolve(root, 'public', 'manifest.webmanifest'))) {
          tags.push({ tag: 'link', attrs: { rel: 'manifest', href: '/manifest.webmanifest' }, injectTo: 'head' })
        }
        let out = fillBootVersion(html, info)
        if (ctx.bundle) {
          // a classic script after the stylesheet link would hold the parser until the stylesheet arrives (script-blocking
          // style sheet), and with it the boot mask markup the early capacity line writes into (D2-FX-R2-web, D2-AC-41):
          // the manifest goes right after the charset meta, before every stylesheet
          const m = bootManifest(ctx.bundle as unknown as Record<string, ChunkLike>)
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
const BUILD_INFO = collectBuildInfo(process.env, { demo: DEMO, version: PKG_VERSION })

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
      return `export default ${JSON.stringify(landingI18nDict(zh, en))}\n`
    },
  }
}
const BOOT_MODULES = /[\\/]src[\\/]app[\\/](boot[\\/]capacityText\.ts|i18n[\\/]plural\.ts)$|virtual:awr-boot-i18n/
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

// 代理表（AWR-03 §3.3）：/api（含 /api/rt 的 WS 升级）与 /worlds/<id>/**、/worlds/_shared/** 数据路径转发到 api 进程。
// /assets/ 是 Vite 构建产物前缀，不作代理（AWR-03 §3.3、AWR-16 F-09、R2-42）；/worlds 本身是前端路由（World Hub，
// M15-FR-003），因此 /worlds 只以正则匹配带子路径的数据请求。preview 显式使用同一张表（默认会继承 server.proxy）。
const PROXY = {
  '/api': { target: apiTarget, ws: true },
  '^/worlds/.+': { target: apiTarget },
  '^/vehicles/.+': { target: apiTarget }, // vehicle model glb (AWR-17 §5; INT-1)
}

export default defineConfig({
  plugins: [r3fClockPlugin(), threeProgramsPlugin(), bootI18nPlugin(), landingI18nPlugin(), earlyInlinePlugin(BUILD_INFO), react(), tailwindcss(), buildInfoPlugin(BUILD_INFO), ...(DEMO ? [demoPlugin()] : [])],
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
  worker: { format: 'es' },
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
