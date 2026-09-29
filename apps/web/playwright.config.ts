// Playwright configuration (M16 §6.7.1; AWR-18 §10). Owner: M16 (M16 §14 item 24; first version by M15-S, taken over).
//   * workers 1: performance and gate runs are serial (PR-12);
//   * project `perf`: testDir apps/web/perf (M16 root specs and the module directories perf/m<nn>/);
//     project `e2e`: testDir tests/e2e at the repository root (cross-module integration specs, AWR-03 §4.3);
//   * both launch the local Chrome for Testing 151 ($PW_CHROME, rev 1234) with flag set C1 (SwiftShader WebGL2); the Tier A
//     feature matrix case switches to C2 through the harness; the C3 flags (vsync off) never appear here (lint PERF-01);
//   * viewport 1280 x 720 CSS at DPR 1 (g02 §2.1); every run gets a fresh temporary profile (Playwright contexts).
// Performance cases run only through perf/harness/run.mjs (AWR-18 §3), which sets AWR_PERF_BASE, AWR_PERF_CASE_PARAMS and
// AWR_PERF_RUN_DIR and wraps the runner in `taskset -c 2-6` (PR-6). `npx playwright test` is for debugging and smoke
// specs; the web server is then `vite preview` of the current dist/ (build it first; test hooks need
// VITE_AWR_TEST_SWITCHES=1).
import { defineConfig } from '@playwright/test'

const CHROME = process.env.PW_CHROME ?? `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`
const C1 = ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']
const C2 = [...C1, '--enable-unsafe-webgpu', '--enable-features=Vulkan', '--use-webgpu-adapter=swiftshader']
const flags = process.env.AWR_PERF_FLAGS === 'C2' ? C2 : C1
const offset = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0
const PORT = 4173 + 10 * offset
const BASE = process.env.AWR_PERF_BASE ?? `http://127.0.0.1:${PORT}`

const browser = {
  baseURL: BASE,
  viewport: { width: 1280, height: 720 },
  deviceScaleFactor: 1,
  headless: true,
  launchOptions: { executablePath: CHROME, args: flags },
}

export default defineConfig({
  workers: 1,
  fullyParallel: false,
  timeout: 180_000,
  reporter: [['list']],
  outputDir: '../../runs/playwright',
  // under the harness the system under test is AWR_PERF_BASE (api or static server); no preview server is started then
  webServer: process.env.AWR_PERF_BASE ? undefined : {
    command: `npx vite preview --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: BASE,
    reuseExistingServer: true,
    timeout: 60_000,
  },
  projects: [
    { name: 'perf', testDir: './perf', testMatch: /.*\.spec\.ts$/, use: browser, metadata: { flags: process.env.AWR_PERF_FLAGS ?? 'C1' } },
    { name: 'e2e', testDir: '../../tests/e2e', testMatch: /.*\.spec\.ts$/, use: browser, metadata: { flags: process.env.AWR_PERF_FLAGS ?? 'C1' } },
  ],
})
