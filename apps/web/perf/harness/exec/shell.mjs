// Shell executor (M16-FR-042 `shell`): chaos and smoke scripts; exit 0 passes, 6 / 11 / 14 are environment not met.
import { resolveCmd, runLogged, statusOfExit, substitute } from './common.mjs'

export async function runShell(c, o) {
  const [cmd, args] = resolveCmd(substitute(c.cmd, { runDir: o.runDir, base: o.base ?? '', ...o.params }))
  const env = { ...process.env, ...(o.env ?? {}), AWR_PERF_LOCK_HELD: process.env.AWR_PERF_LOCK_HELD ?? 'ex', AWR_PERF_RUN_DIR: o.runDir }
  const r = await runLogged(cmd, args, { env, runDir: o.runDir, name: 'shell', timeoutS: o.timeoutS })
  return { ...statusOfExit(r.code, r.timedOut), anomaly: false }
}
