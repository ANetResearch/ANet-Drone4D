// Degradation and backend notices (M15-FR-097; AWR-14 §7.5; ADR-041): the M06 engine events `governor.step` (dir -1 =
// one degradation sub-step, +1 = one restore) and `backend.notice`. A degradation raises one merged toast (key
// "governor"; the same knob toasts at most once per INPUT.governorToastDedupMs); restoring never toasts (the HUD row just
// disappears); `webgpu.unavailable` and `pointsize.degraded` raise one info toast each per page.
import { hasKey, t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { events } from '@/engine'
import { INPUT } from '@/lib/tokens/input.gen'

const lastByKey = new Map<string, number>()
const noticed = new Set<string>()

/** step text of a governor label key (falls back to the step number) */
export function governorText(key: string | null, step: number): string {
  return key && hasKey(key) ? t(key) : t('hud.degraded', { step })
}

/** handle one governor.step payload (exported for tests); returns true when a toast was raised */
export function onGovernorStep(p: { step: number; dir: number; reasonKey?: string }, nowMs = performance.now()): boolean {
  if (p.dir !== -1) return false
  const key = p.reasonKey ?? `step.${p.step}`
  const last = lastByKey.get(key) ?? Number.NEGATIVE_INFINITY
  if (nowMs - last < INPUT.governorToastDedupMs) return false
  lastByKey.set(key, nowMs)
  notify('governor', 'info', t('governor.toast', { what: governorText(p.reasonKey ?? null, p.step) }), t('governor.toastHint'))
  return true
}

export function installGovernorToasts(): () => void {
  const off1 = events.on<{ step: number; dir: number; reasonKey?: string }>('governor.step', (p) => void onGovernorStep(p))
  const off2 = events.on<string>('backend.notice', (n) => {
    if (typeof n !== 'string' || noticed.has(n)) return
    noticed.add(n)
    const k = `notice.${n}`
    notify(`backend:${n}`, 'info', hasKey(k) ? t(k) : n)
  })
  return () => {
    off1()
    off2()
  }
}
