// Long Animation Frame attribution outside the loop (M06-FR-074; AWR-18 §6.3 item 3; D1-AC-06). Owner: M06.
// PerformanceObserver({type: 'long-animation-frame', buffered: true}): every entry counts loaf.count and blockingMs;
// scripts that are not the loop callback and whose sourceURL belongs to our chunks are summed, > 50 ms counts
// loaf.oursOver50 when the entry started after the reveal (entry.startTime >= load.revealAt: Chrome delivers observer
// callbacks as low-priority tasks, so the shader zoo frame under the mask can arrive after the reveal, FX2-R2). Entries
// that started before the last __perf.reset() (loaf.sinceMs) are ignored. The loop callback is recognised by time, not by name (production builds mangle
// names): invokerType 'frame-request-callback' with a startTime within 0.5 ms of a loop callback start (loop.cbStarts);
// the loop frame itself is attributed by frameSampler (phases minus render). Our chunks: the entry chunk and the
// engine, net and ui groups of vite.config.ts (asset names index-*, engine-*, net-*, ui-*; dev server /src/**);
// three, react, r3f and other vendor chunks are not ours. The 8 worst entries are kept in loaf.worst.
import { cbStarts } from '../loop'
import { pushLoafWorst, type AwrPerf } from './probe'
import { sampler } from './frameSampler'

interface LoafScript { invokerType?: string; invoker?: string; sourceURL?: string; startTime: number; duration: number }
interface LoafEntry { duration: number; blockingDuration?: number; startTime: number; renderStart?: number; styleAndLayoutStart?: number; scripts?: LoafScript[] }

const OURS = /\/assets\/(?:index|engine|net|ui)-[\w-]+\.js(?:$|\?)|\/src\/(?:engine|net|ui|viewport|app|stores|lib)\//

/** true for a script URL of our own code (same origin) */
export function isOurScript(url: string | undefined, origin: string): boolean {
  if (!url) return false
  if (origin && !url.startsWith(origin)) return false
  return OURS.test(url)
}

/** true when the script is the engine loop callback (attributed per frame by frameSampler) */
export function isLoopCallback(s: LoafScript): boolean {
  if (s.invokerType !== 'frame-request-callback') return false
  for (let i = 0; i < cbStarts.length; i++) if (Math.abs(cbStarts[i] - s.startTime) <= 0.5) return true
  return false
}

/** attribution of one entry: our script ms outside the loop callback */
export function oursOutsideLoop(e: LoafEntry, origin: string): { ours: number; invoker: string } {
  let ours = 0
  let invoker = ''
  for (const s of e.scripts ?? []) {
    if (isLoopCallback(s) || !isOurScript(s.sourceURL, origin)) continue
    ours += s.duration
    if (!invoker) invoker = s.invoker ?? ''
  }
  return { ours, invoker }
}

export function observeLoaf(p: AwrPerf): () => void {
  if (typeof PerformanceObserver === 'undefined' || !(PerformanceObserver.supportedEntryTypes ?? []).includes('long-animation-frame')) return () => {}
  const origin = typeof location !== 'undefined' ? location.origin : ''
  const po = new PerformanceObserver((list) => {
    for (const raw of list.getEntries()) {
      const e = raw as unknown as LoafEntry
      if (e.startTime < p.loaf.sinceMs) continue
      p.loaf.count++
      p.loaf.blockingMs += e.blockingDuration ?? 0
      const a = oursOutsideLoop(e, origin)
      const renderMs = e.renderStart && e.renderStart > 0 ? e.startTime + e.duration - e.renderStart : 0
      if (sampler.revealed && p.load.revealAt > 0 && e.startTime >= p.load.revealAt && a.ours > 50) p.loaf.oursOver50++
      pushLoafWorst(p, e.duration, a.ours, renderMs, a.invoker)
    }
  })
  try {
    po.observe({ type: 'long-animation-frame', buffered: true })
  } catch {
    return () => {}
  }
  return () => po.disconnect()
}
