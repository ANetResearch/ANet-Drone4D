// URL switches of the viewport (AWR-18 §9.5; ADR-044; M06-FR-011, FR-059, FR-080). Owner: M06.
// Test switches (?tier=A|B|S, ?allowFallback=1, ?perfInject=busyMs:n|renderBusyMs:n, ?selftest=nofix, ?bench=layers)
// exist only in dev and VITE_AWR_TEST_SWITCHES=1 builds: every branch reading them sits behind the build-time constant
// TEST_SWITCHES, so plain production bundles carry neither the parser nor the identifiers (M06-AC-010). The renderer
// preference ?rb=webgpu and the flight60 driver ?bench=flight60&scene=&source= are product switches (both builds).
import { TEST_SWITCHES } from '@/lib/testSwitches'
import type { Tier } from '@/engine'

export interface Forced { tier?: Tier; allowFallback?: boolean; perfInject?: string; selftestNoFix?: boolean }

function search(): URLSearchParams {
  return new URLSearchParams(typeof location !== 'undefined' ? location.search : '')
}

/** effective test switches (null in production builds and when none is given) */
export function forcedFlags(q: URLSearchParams = search()): Forced | null {
  if (!TEST_SWITCHES) return null
  const f: Forced = {}
  const t = q.get('tier')
  if (t === 'A' || t === 'B' || t === 'S') f.tier = t
  if (q.get('allowFallback') === '1') f.allowFallback = true
  const inj = q.get('perfInject')
  if (inj) f.perfInject = inj
  if (q.get('selftest') === 'nofix') f.selftestNoFix = true
  return f.tier || f.allowFallback || f.perfInject || f.selftestNoFix ? f : null
}

/** busyMs:n and renderBusyMs:n of ?perfInject= (test builds) */
export function parseInject(s: string | undefined): { busyMs: number; renderBusyMs: number } {
  const out = { busyMs: 0, renderBusyMs: 0 }
  if (!TEST_SWITCHES || !s) return out
  for (const part of s.split(',')) {
    const [k, v] = part.split(':')
    const n = Number(v)
    if (!Number.isFinite(n) || n < 0) continue
    if (k === 'busyMs') out.busyMs = n
    else if (k === 'renderBusyMs') out.renderBusyMs = n
  }
  return out
}

/** renderer preference: ?rb=webgpu (settings page later, D1-ext) */
export function preference(q: URLSearchParams = search()): 'auto' | 'webgpu' {
  return q.get('rb') === 'webgpu' ? 'webgpu' : 'auto'
}

export interface BenchSwitch { mode: '' | 'flight60' | 'layers'; scene: 'pc' | 'full' | ''; city: string | null; source: string | null }
/** ?bench=flight60 (both builds) and ?bench=layers (test builds) */
export function benchSwitch(q: URLSearchParams = search()): BenchSwitch {
  const b = q.get('bench')
  const scene = q.get('scene')
  const mode = b === 'flight60' ? 'flight60' : TEST_SWITCHES && b === 'layers' ? 'layers' : ''
  return { mode, scene: scene === 'pc' || scene === 'full' ? scene : mode ? 'full' : '', city: q.get('city'), source: q.get('source') }
}
