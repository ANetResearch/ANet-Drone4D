// Toast merger (M15-FR-089; AWR-14 §11.4, §11.6): alarms become toasts keyed by their merge key (source:type:reason),
// so a storm of the same event updates one toast (count and subjects) instead of adding more; the bridge calls it once
// per flush, which bounds updates of one toast to 4 Hz. Timeouts: info 4 s, warning 6 s, critical sticky. Command
// results are toasted by the command tracker (ui/actions/commands.ts), not here; info events toast only for the few
// types of AWR-14 §11.6 that need it (scenario restarted).
import { t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { sanitizeText } from '@/lib/sanitize'
import type { RtEvent } from '@/net/rt'
import type { AlarmItem } from './alarms'
import { describeEvent } from './severity'

const SUBJECTS_SHOWN = 3
const INFO_TOAST = new Set(['sim.reset', 'sim.restarted', 'lease.preempted', 'seat.takeover', 'env.presets_mismatch'])

/** "p600-01, p600-02, p600-03 and 34 more" */
export function subjectsText(subjects: readonly string[]): string {
  if (!subjects.length) return ''
  const shown = subjects.slice(0, SUBJECTS_SHOWN).map((s) => sanitizeText(s, 64)).join(t('common.listSep'))
  const more = subjects.length - SUBJECTS_SHOWN
  return more > 0 ? t('toast.subjectsMore', { list: shown, more }) : shown
}

/** toast of an alarm item (created or updated in place) */
export function toastAlarm(a: AlarmItem): void {
  const level = a.severity === 'critical' ? 'critical' : 'warning'
  const title = a.subjects.length > 1 ? t('toast.merged', { n: a.subjects.length, text: a.text }) : a.text
  const parts: string[] = []
  if (a.subjects.length > 1) parts.push(subjectsText(a.subjects))
  if (a.count > 1) parts.push(t('toast.count', { count: a.count }))
  notify(`alarm:${a.key}`, level, title, parts.join(' · ') || undefined)
}

/** info events that still deserve a toast (never cmd.*: the command tracker owns those) */
export function toastInfoEvent(e: RtEvent): void {
  if (!INFO_TOAST.has(e.type)) return
  notify(`event:${e.type}`, e.type === 'lease.preempted' || e.type === 'seat.takeover' ? 'warning' : 'info', describeEvent(e))
}

/** alarms whose toasts are owned elsewhere */
export const skipAlarmToast = (a: AlarmItem): boolean => a.source === 'cmd'
