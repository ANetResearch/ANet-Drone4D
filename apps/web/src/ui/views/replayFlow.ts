// Entering and leaving replay (AWR-14 §5.4 "进入确认", "退出"; M12 §8.8; 12 §4.11 P01, P08): the live clock is paused
// first (a replay opens only while the live session is PAUSED, otherwise 117 CLOCK_CONSTRAINT), then playback{open};
// deep links /world/:id/replay/:run?seg=&t= arrive as /world/:id?replay=<run>&seg=&t= (route redirect) and open once
// the session can write. Leaving sends playback{close}; the server returns to live and stays PAUSED.
import { t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { updateSearch } from '@/app/router/router'
import { TIME_STATE } from '@/engine'
import { timeline, timelineStore } from '@/stores/timeline'
import { connViewStore, isOnline, timeView } from '@/ui/shell/connView'

const RUN_ID = /^r\d{8}-\d{6}-[0-9a-f]{4}$/

/** why this session cannot open a replay now (i18n key), or null */
export function replayDeniedKey(): string | null {
  const c = connViewStore.getState()
  if (!isOnline(c.conn)) return 'hint.offline'
  if (c.role === 'viewer' || c.seat !== 'held') return 'hint.replaySeat'
  return null
}

const wait = (ms: number) => new Promise<void>((ok) => setTimeout(ok, ms))

/** pause the live clock (when playing), then open the recording; resolves true when the replay is open */
export async function enterReplay(run: string, seg = 0, tS?: number): Promise<boolean> {
  const denied = replayDeniedKey()
  if (denied) {
    notify('replay:denied', 'warning', t(denied))
    return false
  }
  // a deep link runs right after connecting: wait for the first TIME before reading the live clock (the timeline store
  // still says STOPPED until its first 4 Hz tick, and skipping the pause then ends in 117 CLOCK_CONSTRAINT; FX-GW)
  const known = Date.now() + 5000
  while (Date.now() < known && !Number.isFinite(timeView.atMs)) await wait(100)
  // the server is already replaying (a reload of a replay deep link, or another seat opened it before): the same
  // recording only seeks; another one is closed first (open while a replay is open is 105 STATE)
  const cur = timelineStore.getState()
  if (cur.mode === 'replay' && cur.playback && cur.playback.status !== 'error') {
    if (cur.runId === run && cur.segment === seg) {
      if (tS !== undefined && Number.isFinite(tS)) timeline.seek(tS)
      return true
    }
    await timeline.closeReplay()
  }
  const live = () => (Number.isFinite(timeView.atMs) ? timeView.state : timelineStore.getState().state4)
  if (timelineStore.getState().mode === 'live' && live() !== TIME_STATE.PAUSED && live() !== TIME_STATE.STOPPED) {
    timeline.pause()
    const end = Date.now() + 5000
    while (Date.now() < end && live() !== TIME_STATE.PAUSED) await wait(100)
  }
  const ok = await timeline.openReplay(run, seg, tS)
  if (ok) notify('replay:open', 'info', t('replay.opened', { run }))
  return ok
}

export async function leaveReplay(): Promise<void> {
  await timeline.closeReplay()
  notify('replay:close', 'info', t('replay.closed'))
}

/** redirect target of /world/:id/replay/:run (?seg, ?t kept) */
export function replayRedirect(p: Record<string, string>, search: URLSearchParams): string {
  const q = new URLSearchParams()
  if (p.run && RUN_ID.test(p.run)) q.set('replay', p.run)
  const seg = search.get('seg')
  if (seg && /^\d{1,4}$/.test(seg)) q.set('seg', seg)
  const tt = search.get('t')
  if (tt && /^\d+(\.\d+)?$/.test(tt)) q.set('t', tt)
  return `/world/${p.id}${q.toString() ? `?${q.toString()}` : ''}`
}

let deepLinkDone = ''
/** consume ?replay=&seg=&t= once the session may open a replay (called from the sandbox on search changes) */
export async function consumeReplayDeepLink(search: URLSearchParams): Promise<void> {
  const run = search.get('replay')
  if (!run || !RUN_ID.test(run) || deepLinkDone === run) return
  const end = Date.now() + 15_000
  while (Date.now() < end && replayDeniedKey() === 'hint.offline') await wait(250)
  deepLinkDone = run
  const seg = Number(search.get('seg') ?? '0') || 0
  const tS = search.has('t') ? Number(search.get('t')) : undefined
  const ok = await enterReplay(run, seg, tS !== undefined && Number.isFinite(tS) ? tS : undefined)
  // the parameters are one-shot: drop them from the address bar (the replay state lives on the server)
  updateSearch({ replay: null, seg: null, t: null })
  if (!ok) deepLinkDone = ''
}
