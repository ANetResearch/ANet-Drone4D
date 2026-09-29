// Vitest 5 项目（M00 统一，AWR-18 §8.4）：unit（Node）、browser（本机 Chrome 151 + SwiftShader）、bench（*.bench.ts）
import { defineConfig, mergeConfig } from 'vitest/config'
import { playwright } from '@vitest/browser-playwright'
import viteConfig from './vite.config.ts'

// 本机 Chrome for Testing 151（rev 1234），不在线下载（AWR-11 TECH-FR-006）
export const CHROME =
  process.env.PW_CHROME ?? `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`

// 组合 C1（WebGL2 over SwiftShader）；WebGPU 用例在各自文件中追加 C2 标志（AWR-11 §4.17、AWR-18 §10）
const C1_ARGS = ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      projects: [
        {
          extends: true,
          test: {
            name: 'unit',
            environment: 'node',
            include: ['tests/**/*.test.ts'],
            exclude: ['tests/**/*.browser.test.ts', 'tests/**/*.browser.test.tsx'],
          },
        },
        {
          extends: true,
          test: {
            name: 'browser',
            include: ['tests/**/*.browser.test.ts', 'tests/**/*.browser.test.tsx'],
            browser: {
              enabled: true,
              headless: true,
              provider: playwright({ launchOptions: { executablePath: CHROME, args: C1_ARGS } }),
              instances: [{ browser: 'chromium' }],
            },
          },
        },
      ],
      benchmark: { include: ['tests/**/*.bench.ts'] },
    },
  }),
)
