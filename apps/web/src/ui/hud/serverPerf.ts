// perf/server view (AWR-17 §6.5; M11-FR-085): the 1 Hz server summary delivered on the control path (RtClient.onData)
// kept in one mutable object for the Perf panel's "server" and "network" cards (C-class bound text; no store writes).
// The topic is part of the default subscription set registered by the viewport (M06 drones layer).
import type { RtClient } from '@/net/rt'
import { LfRing } from '@/ui/lf/series'

export const serverPerf = {
  version: 0,
  atMs: Number.NaN,
  api: { cpuPct: Number.NaN, tickAgeP99Ms: Number.NaN, loopLagP99Ms: Number.NaN, nClients: 0 },
  sim: { cpuPct: Number.NaN, rtf: Number.NaN, stepP99Us: Number.NaN, stepMaxUs: Number.NaN, nActive: 0, kernel: '' },
  mine: { fps: Number.NaN, creditSkips: 0, srttMs: Number.NaN, kbps: Number.NaN },
  stepP99: new LfRing(),
  rtf: new LfRing(),
}

const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : Number.NaN)

/** fold one perf/server payload (exported for tests) */
export function foldServerPerf(d: Record<string, unknown>, connId: string | null, nowMs = performance.now()): void {
  const api = (d.api ?? {}) as Record<string, unknown>
  const sim = (d.sim ?? {}) as Record<string, unknown>
  const clients = Array.isArray(d.clients) ? (d.clients as Record<string, unknown>[]) : []
  serverPerf.api.cpuPct = num(api.cpu_pct)
  serverPerf.api.tickAgeP99Ms = num(api.tick_age_p99_ms)
  serverPerf.api.loopLagP99Ms = num(api.loop_lag_p99_ms)
  serverPerf.api.nClients = num(api.n_clients) || clients.length
  serverPerf.sim.cpuPct = num(sim.cpu_pct)
  serverPerf.sim.rtf = num(sim.rtf)
  serverPerf.sim.stepP99Us = num(sim.step_p99_us)
  serverPerf.sim.stepMaxUs = num(sim.step_max_us)
  serverPerf.sim.nActive = num(sim.n_active) || 0
  serverPerf.sim.kernel = typeof sim.kernel === 'string' ? sim.kernel : ''
  const mine = clients.find((c) => c.conn_id === connId) ?? (clients.length === 1 ? clients[0] : undefined)
  if (mine) {
    serverPerf.mine.fps = num(mine.fps)
    serverPerf.mine.creditSkips = num(mine.credit_skips) || 0
    serverPerf.mine.srttMs = num(mine.srtt_ms)
    serverPerf.mine.kbps = num(mine.kbps)
  }
  if (Number.isFinite(serverPerf.sim.stepP99Us)) serverPerf.stepP99.push(nowMs, serverPerf.sim.stepP99Us / 1000)
  if (Number.isFinite(serverPerf.sim.rtf)) serverPerf.rtf.push(nowMs, serverPerf.sim.rtf)
  serverPerf.atMs = nowMs
  serverPerf.version++
}

export function installServerPerf(rt: RtClient): () => void {
  return rt.onData((m) => {
    if (m.topic !== 'perf/server' || !m.data || typeof m.data !== 'object') return
    foldServerPerf(m.data as Record<string, unknown>, rt.serverInfo?.connId ?? null)
  })
}
