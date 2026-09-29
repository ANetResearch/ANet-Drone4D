// Session control (M15-FR-037; AWR-14 §6.12, §6.19 rows 1-2): "request control" issues an operator token for the same
// principal (POST /api/auth/token {role: operator, principal_hint}) and hands it to the realtime link with
// `rt.reauthenticate` (M11-net-to-M15 item 7); 409 (116 SEAT_TAKEN) keeps the read-only view and says who holds the seat
// since when (the detail of the 409 body, which getToken does not expose, is why the POST is issued here). The REST token
// cache of net/api.ts is dropped so the next REST call re-issues for the same principal. "release control" calls
// seat/release.
import { reasonText, t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { invalidateToken } from '@/net/api'
import type { RtClient } from '@/net/rt'
import { runService } from '@/ui/actions/vehicleCommands'
import { initRt } from './RtContext'

const HINT_KEY = 'awr.principal_hint'
function hint(): string | undefined {
  try {
    return globalThis.localStorage?.getItem(HINT_KEY) ?? undefined
  } catch {
    return undefined
  }
}

export async function requestControl(rt: RtClient): Promise<boolean> {
  const body: Record<string, unknown> = { role: 'operator', client: 'web/0.1.0' }
  const h = hint()
  if (h) body.principal_hint = h
  try {
    const r = await fetch('/api/auth/token', { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify(body) })
    const j = (await r.json().catch(() => ({}))) as { token?: string; code?: number; detail?: { holder_since_unix_ns?: string | number } }
    if (!r.ok || !j.token) {
      const code = typeof j.code === 'number' ? j.code : r.status === 409 ? 116 : 0
      const since = j.detail?.holder_since_unix_ns !== undefined ? fmt.wallTime(Number(j.detail.holder_since_unix_ns) / 1e6) : '—'
      notify('seat:request', 'warning', code === 116 ? t('control.taken', { since }) : t('control.failed'), code ? `${code} ${reasonText(code).short}` : undefined)
      return false
    }
    invalidateToken()
    if (rt.status === 'IDLE' || rt.status === 'CLOSED') initRt(rt, j.token)
    else rt.reauthenticate(j.token, 'operator')
    notify('seat:request', 'success', t('control.granted'))
    return true
  } catch {
    notify('seat:request', 'warning', t('control.failed'))
    return false
  }
}

export function releaseControl(): void {
  runService('seat:release', 'seat/release', {}, t('control.release'), { toastSuccess: true })
}
