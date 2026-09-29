// Playwright configuration for the M06 functional specs run during parallel development (no shared vite preview:
// every spec starts perf/m06/server.ts on a free port and serves M06_DIST). Same browser settings as
// apps/web/playwright.config.ts (M16 §6.7.1): Chrome for Testing 151, flag set C1 (SwiftShader WebGL2), 1280 x 720,
// DPR 1, one worker. Performance cases are not run from here (AWR-18 §3).
import { defineConfig } from '@playwright/test'

const CHROME = process.env.PW_CHROME ?? `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`
const C1 = ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']

export default defineConfig({
  workers: 1,
  fullyParallel: false,
  timeout: 180_000,
  reporter: [['list']],
  outputDir: '../../../../runs/playwright-m06',
  projects: [
    {
      name: 'm06', testDir: '.', testMatch: /.*\.spec\.ts$/,
      use: { viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1, headless: true, launchOptions: { executablePath: CHROME, args: C1 } },
      metadata: { flags: 'C1' },
    },
  ],
})
