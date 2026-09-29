// AWR 前端构建配置（M00 统一，AWR-03 ADR-037；来源 .cache/research/n05/trial/vite.config.ts）
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

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
  plugins: [react(), tailwindcss()],
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
            { name: 'ui', test: /[\\/]src[\\/]ui[\\/]/ },
            { name: 'engine', test: /[\\/]src[\\/]engine[\\/]/ },
            { name: 'net', test: /[\\/]src[\\/]net[\\/]/ },
          ],
        },
      },
    },
  },
})
