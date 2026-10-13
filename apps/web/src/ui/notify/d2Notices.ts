// D2 notices (AWR-04 §10.7; M15-FR-219; UX-FR-138, UX-FR-139; AWR-14 §20.11): the bridge hands every D2 event here once
// per flush (<= 4 Hz):
//   * perception.capture -> neutral toast "captured <target> · <uav> <sensor> · <level>", once per target per 30 s
//     (wall clock);
//   * target.discovered -> critical toast with a "confirm" action (sticky until confirmed); the confirmation is the
//     alarm centre's acknowledgement of the same alarm item (target:target.discovered:<target>); unconfirmed discoveries
//     are red candidates of the viewport (rank 5, the latest wins, ui/notify/redFigures.ts);
//   * uav.keepout_guard with clamped -> warning "limited: approaching <target>'s <kind> range", once per target per 5 s;
//   * sandbox.rate with a lower grant -> info "this session reaches xk (<why>)";
//   * session.switched of a sandbox (template, restore, world) -> info.
// The targets' live state (awareness, best level, capture, discovered count) is M18's stores/targets.ts (TargetLite32
// rows), no longer folded from these events here. Texts are sanitised.
import { t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import type { RtClient, RtEvent } from '@/net/rt'
import { confirmDiscovery, discoveryToastKey, noteDiscovery, resetDiscoveries } from './discoveries'
import { onBridgeEvent } from './eventBridge'

export const CAPTURE_DEDUPE_MS = 30_000
export const KEEPOUT_DEDUPE_MS = 5_000

/** wall ms of the last toast per key (capture per target, keepout per target) */
const last = new Map<string, number>()
/** pure de-duplication: true when `key` has not been toasted within `windowMs` (and records it) */
export function dueOnce(key: string, windowMs: number, now = Date.now()): boolean {
  const at = last.get(key)
  if (at !== undefined && now - at < windowMs) return false
  last.set(key, now)
  return true
}
export function resetNoticeDedupe(): void {
  last.clear()
  resetDiscoveries()
}

const str = (v: unknown): string => (typeof v === 'string' ? sanitizeText(v, 64) : '')
const levelText = (v: unknown): string => (v === 'D' || v === 'R' || v === 'I' ? t(`perception.level.${v}`) : str(v))
const kindText = (v: unknown): string => (v === 'visual' || v === 'acoustic' || v === 'radar' || v === 'rf' ? t(`sens.kind.${v}`) : str(v))

/** one D2 event (exported for tests) */
export function noticeOf(e: RtEvent, now = Date.now()): void {
  const d = e.data ?? {}
  switch (e.type) {
    case 'perception.capture': {
      const target = str(d.target)
      if (!target || !dueOnce(`capture:${target}`, CAPTURE_DEDUPE_MS, now)) return
      notify(`perception:capture:${target}`, 'info', t('perception.captured', {
        target, uav: str(d.uav ?? e.uav), sensor: str(d.sensor), level: levelText(d.level_to),
      }))
      return
    }
    case 'target.discovered': {
      const target = str(d.target)
      if (!target) return
      noteDiscovery(target, now)
      const range = typeof d.range_m === 'number' ? fmt.num(d.range_m, 0) : '—'
      notify(discoveryToastKey(target), 'critical', t('target.discovered', { target, uav: str(d.uav ?? e.uav), kind: kindText(d.kind), range }),
        undefined, { label: t('common.confirm'), onClick: () => confirmDiscovery(target), id: `discovered:${target}` })
      return
    }
    case 'uav.keepout_guard': {
      const target = str(d.target)
      if (!target || d.clamped === false || !dueOnce(`keepout:${target}`, KEEPOUT_DEDUPE_MS, now)) return
      notify(`manual:keepout:${target}`, 'warning', t('manual.clamped', { target, kind: kindText(d.kind ?? 'visual') }))
      return
    }
    case 'sandbox.rate': {
      const granted = typeof d.granted === 'number' ? d.granted : null
      const requested = typeof d.requested === 'number' ? d.requested : null
      if (granted === null || requested === null || granted >= requested) return
      notify('sandbox:rate', 'info', t('sandbox.rate.capped', { k: fmt.num(granted, granted < 1 ? 2 : 0), why: t(`sandbox.limitedBy.${limitedKey(d.limited_by)}`) }))
      return
    }
    case 'session.switched': {
      if (d.reason === 'template' && typeof d.template_id === 'string') notify('sandbox:switched', 'info', t('sandbox.switched.template', { template: str(d.template_id) }))
      else if (d.reason === 'world' && typeof d.world_id === 'string') notify('sandbox:switched', 'info', t('sandbox.switched.world', { world: str(d.world_id), template: str(d.template_id) }))
      else if (d.reason === 'restore') notify('sandbox:switched', 'info', t('sandbox.switched.restore'))
      return
    }
    default:
      return
  }
}

export function limitedKey(v: unknown): string {
  return v === 'cpu_budget' || v === 'cost_cap' || v === 'manual_control' || v === 'degraded' ? v : 'none'
}

const NOTICE_EVENTS = new Set(['perception.capture', 'target.discovered', 'uav.keepout_guard', 'sandbox.rate', 'session.switched'])

/** attach to the bridge (RtProvider); the client argument keeps the RtProvider wiring uniform */
export function installCaptureNotices(_rt: RtClient): () => void {
  return onBridgeEvent((e) => {
    if (NOTICE_EVENTS.has(e.type)) noticeOf(e)
  })
}
