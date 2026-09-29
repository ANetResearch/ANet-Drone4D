// Test build of the web app for the M15 UI specs (M15 NFR-018): the regular vite.config.ts with
// VITE_AWR_TEST_SWITCHES=1 into M15_DIST (a private output; parallel agents share dist/). With --stub-viewport the
// WorldCanvas module (M06) is replaced by perf/m15/helpers/WorldCanvasStub.tsx, so the shell can be built and exercised in
// UI-only mode (`?viewport=off`) while the viewport sources are mid-change. Usage:
//   M15_DIST=/tmp/dist-m15 node perf/m15/helpers/build-ui.mjs [--stub-viewport]
import { resolve } from 'node:path'
import { build, loadConfigFromFile, mergeConfig } from 'vite'

const WEB = resolve(import.meta.dirname, '..', '..', '..')
const OUT = process.env.M15_DIST ?? resolve(WEB, 'dist')
const STUB = resolve(import.meta.dirname, 'WorldCanvasStub.tsx')
process.env.VITE_AWR_TEST_SWITCHES = '1'
process.chdir(WEB)
const loaded = await loadConfigFromFile({ command: 'build', mode: 'production' }, resolve(WEB, 'vite.config.ts'))
const stub = process.argv.includes('--stub-viewport')
const plugins = stub ? [{ name: 'm15-stub-viewport', enforce: 'pre', resolveId: (id) => (id === '@/viewport/WorldCanvas' ? STUB : null) }] : []
await build(mergeConfig(loaded?.config ?? {}, { configFile: false, root: WEB, logLevel: 'warn', build: { outDir: OUT, emptyOutDir: true }, plugins }))
console.log(`built ${stub ? 'UI-only (stub viewport) ' : ''}test build into ${OUT}`)
