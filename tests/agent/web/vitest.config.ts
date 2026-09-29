// M14 前端 store 单元测试的独立 Vitest 配置（tests/agent/web 位于 apps/web 之外，与 M09 tests/safety/web 同一做法；
// AWR-03 §4.3 测试目录表中 M14 没有 apps/web/tests 目录）。运行：cd apps/web && npx vitest run --config ../../tests/agent/web/vitest.config.ts
import { fileURLToPath } from 'node:url'
import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from '../../../apps/web/vite.config.ts'

const WEB = fileURLToPath(new URL('../../../apps/web', import.meta.url))
const HERE = fileURLToPath(new URL('.', import.meta.url))

export default mergeConfig(viteConfig, defineConfig({
  root: WEB,
  resolve: { alias: [{ find: /^@\//, replacement: `${WEB}/src/` }] },
  server: { fs: { allow: [HERE, WEB] } },
  test: { name: 'm14-web', dir: HERE, include: ['**/*.test.ts'], environment: 'node' },
}))
