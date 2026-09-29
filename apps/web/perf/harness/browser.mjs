// Browser environment of the performance cases (AWR-18 §10; M16 §6.7.1): Chrome for Testing 151 ($PW_CHROME),
// flag set C1 (SwiftShader WebGL2) or C2 (C1 + SwiftShader WebGPU, Tier A functional only), viewport 1280 x 720 at DPR 1,
// a fresh profile per run (Playwright contexts are temporary), and PR-6 CPU partitioning: the Playwright runner and the
// Chromium processes it starts run under `taskset -c 2-6`. The forbidden C3 set is imported from the PERF-01 lint so it
// never appears literally in apps/web/perf/** (tools/ci/check-perf-flags.mjs).
import { spawnSync } from 'node:child_process'
import { C3_FLAGS } from '../../../../tools/ci/check-perf-flags.mjs'

export const FLAGS = {
  C1: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
  C2: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--enable-unsafe-webgpu',
    '--enable-features=Vulkan', '--use-webgpu-adapter=swiftshader'],
}
export const BROWSER_CPUS = '2-6'
export const VIEWPORT = { width: 1280, height: 720 }

/** PERF-E006: a flag list containing any C3 flag is invalid for a performance case */
export function forbiddenFlags(flags) {
  return flags.filter((f) => C3_FLAGS.includes(f))
}

let tasksetOk = null
export function hasTaskset() {
  if (tasksetOk === null) tasksetOk = spawnSync('taskset', ['-c', '0', 'true']).status === 0
  return tasksetOk
}

/** prefix a command with taskset -c <cpus> when available (PR-6); returns [cmd, args] */
export function pinned(cmd, args, cpus = BROWSER_CPUS) {
  return hasTaskset() ? ['taskset', ['-c', cpus, cmd, ...args]] : [cmd, args]
}
