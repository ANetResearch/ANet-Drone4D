// Screen-reader announcements (M15-FR-113 P1; AWR-14 §12.3): one polite `role="status"` region (selection changes and
// connection changes, at most one message per 2 s, the latest wins) and one assertive `role="alert"` region (a new
// unacknowledged critical, at most one message per 5 s). Both are visually hidden; texts come from the i18n keys.
import * as React from 'react'
import { t } from '@/app/i18n'
import { alarmsStore } from '@/ui/notify/alarms'
import { connViewStore } from '@/ui/shell/connView'
import { selectionStore } from '@/stores/selection'

const STATUS_MS = 2000
const ALERT_MS = 5000

function useThrottledText(minMs: number): [string, (s: string) => void] {
  const [text, setText] = React.useState('')
  const last = React.useRef(0)
  const pending = React.useRef<string | null>(null)
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null)
  const push = React.useCallback((s: string) => {
    const now = Date.now()
    const wait = last.current + minMs - now
    if (wait <= 0) {
      last.current = now
      setText(s)
      return
    }
    pending.current = s
    if (!timer.current) {
      timer.current = setTimeout(() => {
        timer.current = null
        last.current = Date.now()
        if (pending.current !== null) setText(pending.current)
        pending.current = null
      }, wait)
    }
  }, [minMs])
  React.useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])
  return [text, push]
}

export function LiveRegion() {
  const [status, pushStatus] = useThrottledText(STATUS_MS)
  const [alert, pushAlert] = useThrottledText(ALERT_MS)
  React.useEffect(() => {
    let primary = selectionStore.getState().primary
    let conn = connViewStore.getState().conn
    let crit = alarmsStore.getState().critUnacked
    const offs = [
      selectionStore.subscribe((s) => {
        if (s.primary === primary) return
        primary = s.primary
        pushStatus(s.primary ? t('live.selected', { id: s.primary, n: s.ids.length }) : t('live.cleared'))
      }),
      connViewStore.subscribe((s) => {
        if (s.conn === conn) return
        conn = s.conn
        pushStatus(t(`conn.${s.conn}`))
      }),
      alarmsStore.subscribe((s) => {
        if (s.critUnacked > crit) pushAlert(t('live.critical', { n: s.critUnacked }))
        crit = s.critUnacked
      }),
    ]
    return () => {
      for (const off of offs) off()
    }
  }, [pushStatus, pushAlert])
  return (
    <>
      <div role="status" aria-live="polite" className="sr-only" data-live="status">{status}</div>
      <div role="alert" aria-live="assertive" className="sr-only" data-live="alert">{alert}</div>
    </>
  )
}
