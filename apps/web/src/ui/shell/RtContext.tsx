// RtProvider (M15-FR-035, §6.6.1): one RtClient for the page lifetime (net/rt factory, M11), initialised with the token of
// net/api.ts and the tier of stores/perf; StrictMode double effects never leave two workers alive (close() on cleanup,
// init again on the second mount). The provider also attaches the UI consumers of the client: the connection and
// session view (ui/shell/connView.ts), the event bridge (ui/notify/eventBridge.ts, 4 Hz) and the one-red figures.
// A new epoch after the first one raises one toast ("simulation restarted from the scenario start") and prunes the
// selection once the roster follows (AWR-14 §7.7).
import * as React from 'react'
import { reasonText, t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { createRtClient, type ConnState, type RtClient } from '@/net/rt'
import { getToken } from '@/net/api'
import { perfStore } from '@/stores/perf'
import { bindSafety } from '@/stores/safety'
import { bindAgents } from '@/stores/agents'
import { bindJobs } from '@/stores/jobs'
import { selectionStore } from '@/stores/selection'
import { timeline, timelineStore, timelineTrack } from '@/stores/timeline'
import { alarms } from '@/ui/notify/alarms'
import { labels, viewCube } from '@/viewport/facade'
import { installEventBridge } from '@/ui/notify/eventBridge'
import { installRedFigures } from '@/ui/notify/redFigures'
import { installServerPerf } from '@/ui/hud/serverPerf'
import { connViewStore, installConnView, useConnView } from './connView'

const RtContext = React.createContext<RtClient | null>(null)
let agentsBinding: { setPanelOpen(open: boolean): void } | null = null

/** AGENTS panel visibility: while open the agents store subscribes agent/tasks and agent/+/status (M14-to-M15) */
export function setAgentsPanelOpen(open: boolean): void {
  agentsBinding?.setPanelOpen(open)
}

export function rtUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/api/rt`
}

/** (re)initialise the client with a token (also used after "request control" issued a new operator token) */
export function initRt(client: RtClient, token: string): void {
  const p = perfStore.getState()
  client.init({ url: rtUrl(), token, tier: p.tier ?? 'S', deviceClass: p.deviceClass ?? 'software' })
}

export function RtProvider({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(() => createRtClient())
  React.useEffect(() => {
    let alive = true
    // replay open, seek and close each start a new epoch: only live epoch changes (restart, scenario reset) are news
    let modeChangedAt = Number.NEGATIVE_INFINITY
    const offMode = timelineStore.subscribe((s, prev) => {
      if (s.mode !== prev.mode || (s.playback === null) !== (prev.playback === null)) modeChangedAt = performance.now()
    })
    const offView = installConnView(client, {
      onEpochChange: (from, to) => {
        const tl = timelineStore.getState()
        if (tl.mode === 'replay' || tl.playback !== null || connViewStore.getState().mode === 'replay' || performance.now() - modeChangedAt < 3000) return
        notify('sim:epoch', 'info', t('conn.epochChanged', { from, to }))
      },
    })
    // localised viewport texts (M06-to-M15: label formatter for FlightState short names, hold and zone kinds; ViewCube
    // face and direction names); the viewport itself carries no CJK literals (INT-1)
    labels.setFormatter((fs, sub) => sub === 'hold' ? t('label.hold') : sub === 'zone.nofly' ? t('label.zone.nofly')
      : sub === 'zone.restricted' ? t('label.zone.restricted') : t(`fs.short.${(fs & 0x1f) <= 13 ? fs & 0x1f : 0}`))
    viewCube.setLabels({ E: t('viewcube.E'), W: t('viewcube.W'), N: t('viewcube.N'), S: t('viewcube.S'), U: t('viewcube.U'),
      D: t('viewcube.D'), view: t('viewcube.view') })
    const offBridge = installEventBridge(client)
    const offRed = installRedFigures()
    const offPerf = installServerPerf(client)
    // domain stores that need the page client (INT-1: M09-to-M15 item 1, M14-to-M15); the timeline and mission stores
    // bind themselves from the overlay phase
    const offSafety = bindSafety(client, selectionStore)
    // reconstruction job rows follow job.state and job.progress (<= 4 Hz store writes, D1-AC-22; FX-WEB2)
    const offJobs = bindJobs(client)
    const agents = bindAgents(client)
    agentsBinding = agents
    // timeline notices (M12-to-M15 item 4): stores never import ui, the toast goes through this callback
    // one red of the Timeline figure: acknowledged critical markers leave the arbitration (M12-to-M15 item 9)
    timelineTrack.isAcked = (seq) => alarms.isSeqAcked(seq)
    timeline.setNotifier((n) => {
      const text = n.key.startsWith('reason.') && n.code ? reasonText(n.code).short : t(n.key)
      notify(`timeline:${n.key}`, n.level, text)
    })
    void getToken().then((token) => {
      if (alive) initRt(client, token)
    })
    return () => {
      alive = false
      offView()
      offMode()
      offBridge()
      offRed()
      offPerf()
      offSafety()
      offJobs()
      timeline.setNotifier(null)
      timelineTrack.isAcked = () => false
      agents.dispose()
      if (agentsBinding === agents) agentsBinding = null
      client.close()
    }
  }, [client])
  return <RtContext.Provider value={client}>{children}</RtContext.Provider>
}

export function useRt(): RtClient {
  const c = React.useContext(RtContext)
  if (!c) throw new Error('useRt outside RtProvider')
  return c
}

/** connection state of the page client (the UI store follows onConnState; no polling) */
export function useConnState(): ConnState {
  return useConnView((s) => s.conn)
}
export const connStateNow = (): ConnState => connViewStore.getState().conn
