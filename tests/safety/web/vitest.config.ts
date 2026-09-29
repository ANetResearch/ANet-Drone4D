// M09 前端 store 单元测试的独立 Vitest 配置（tests/safety/web 位于 apps/web 之外；集成阶段可并入 apps/web 的 unit 项目）。
// 运行：cd apps/web && npx vitest run --config ../../tests/safety/web/vitest.config.ts
import { fileURLToPath } from 'node:url'
import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from '../../../apps/web/vite.config.ts'

const WEB = fileURLToPath(new URL('../../../apps/web', import.meta.url))
const HERE = fileURLToPath(new URL('.', import.meta.url))

export default mergeConfig(viteConfig, defineConfig({
  root: WEB,
  // tsconfigPaths 只作用于 apps/web/tsconfig.json 覆盖的文件；本目录在其之外，显式给出 `@/` 别名
  resolve: { alias: [{ find: /^@\//, replacement: `${WEB}/src/` }] },
  server: { fs: { allow: [HERE, WEB] } },
  test: { name: 'm09-web', dir: HERE, include: ['**/*.test.ts'], environment: 'node' },
}))
