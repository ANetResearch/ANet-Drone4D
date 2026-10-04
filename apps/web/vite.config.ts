// AWR 前端构建配置（M00 统一，AWR-03 ADR-037；来源 .cache/research/n05/trial/vite.config.ts）
import { copyFileSync, mkdirSync, rmSync } from 'node:fs'
import { resolve } from 'node:path'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 公开演示构建（ADR-083；M15-FR-120；AWR-19 §3.7）：VITE_AWR_DEMO=public。默认构建不受影响；与测试开关互斥（生产包中不得
// 残留演示以外的测试开关）。入口换成 src/app/demo/entry.tsx（"/" 为不加载 three 的落地页，其余路由按需加载应用），
// dist/bench（UrbanScene3D 城市的 flight60 夹具，禁止再分发）不随演示构建发布，落地页媒体从 docs/media 复制到 dist/demo。
const DEMO = process.env.VITE_AWR_DEMO === 'public'
if (process.env.VITE_AWR_DEMO && !DEMO) throw new Error(`VITE_AWR_DEMO=${process.env.VITE_AWR_DEMO}: only "public" is defined (ADR-083)`)
if (DEMO && process.env.VITE_AWR_TEST_SWITCHES === '1') throw new Error('VITE_AWR_DEMO=public cannot be combined with VITE_AWR_TEST_SWITCHES=1 (ADR-083)')
const LANDING_MODULES = /[\\/]src[\\/](app[\\/]demo[\\/]Landing\.tsx|app[\\/]i18n[\\/]|ui[\\/]components[\\/]ui[\\/](button|badge|card|separator|table|toggle-group|toggle)\.tsx|ui[\\/]icons[\\/](Icon|registry|custom|types)\.tsx?|ui[\\/]brand[\\/]assets\.ts|ui[\\/]testing[\\/]uxProbe\.ts|lib[\\/](utils|demo|testSwitches)\.ts)|vite[\\/]preload-helper/
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
  plugins: [react(), tailwindcss(), ...(DEMO ? [demoPlugin()] : [])],
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
            { name: 'three', test: /node_modules[\\/]three[\\/]/ },
            { name: 'react', test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/ },
            { name: 'r3f', test: /node_modules[\\/]@react-three[\\/]/ },
            // demo build: exactly the modules of the landing page (and, recursively, their dependencies not captured above)
            // form their own chunk; otherwise the landing page would import the whole ui chunk and, through it, the
            // engine and three (ADR-083)
            ...(DEMO ? [{ name: 'landing', test: LANDING_MODULES }] : []),
            { name: 'ui', test: /[\\/]src[\\/]ui[\\/]/ },
            { name: 'engine', test: /[\\/]src[\\/]engine[\\/]/ },
            { name: 'net', test: /[\\/]src[\\/]net[\\/]/ },
          ],
        },
      },
    },
  },
})
