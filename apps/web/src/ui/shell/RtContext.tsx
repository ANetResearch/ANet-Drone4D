// RtProvider (M15-FR-035, §6.6.1): one RtClient for the page lifetime (net/rt factory, M11), initialised with the token of
// net/api.ts and the tier of stores/perf; StrictMode double effects never leave two workers alive (close() on cleanup,
// init again on the second mount). The provider also attaches the UI consumers of the client: the connection and
// session view (ui/shell/connView.ts), the event bridge (ui/notify/eventBridge.ts, 4 Hz) and the one-red figures.
// A new epoch after the first one raises one toast ("simulation restarted from the scenario start") and prunes the
// selection once the roster follows (AWR-14 §7.7).
// D2 (ADR-111; M11-to-M15 d2-WP-03 items 1 and 3; AWR-14 §20.2.3): the realtime endpoint follows the run-time surface
// (app/shell/surface.ts): the shared show and the local build connect to /api/rt with the page token, the sandbox surface
// to /api/sandbox/v1/sessions/{sid}/rt with the sandbox token (net/endpoint setEndpoint; renewed tokens reach the client
// through updateToken), static browsing and an unavailable sandbox have no realtime link at all. A surface change only
// closes and re-initialises the client (the canvas is never unmounted); close 4410 ends the sandbox session, close 1012
// `world_switch` reconnects inside the client and the next serverInfo carries the new world.
// D2.1 (ADR-121; AWR-04 §15.7; M15-FR-227): the link also follows the power-save controller (app/power/PowerController.ts):
// `linkOpen` false (saving, or waiting in the viewer queue) closes the client with 1000 and true connects it again; a
// parked sandbox has no rt path (app/shell/surface.ts) but keeps its sandbox REST endpoint. Connection states and close
// codes go to the controller (LIVE completes a resume, 4430 is the server's reclaim, 4431 the full viewer seats),
// serverInfo.power sets the thresholds, and presence goes out through the client when it supports the op (M11 net).
// D2.2-W1: every serverInfo names the world the connected source simulates (stores/worldScope streamWorld): realtime data
// is ingested only while it equals the world the view shows, and the world-bound state resets when it changes.
import * as React from 'react'
import { reasonText, t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { createRtClient, type ConnState, type RtClient } from '@/net/rt'
import { getToken } from '@/net/api'
import { getEndpoint, rtUrl as endpointRtUrl, setEndpoint } from '@/net/endpoint'
import { setShowWorld, surfaceStore, type SurfaceState } from '@/app/shell/surface'
import { bindSandboxRt, onSandboxToken, sandbox, sandboxStore } from '@/ui/sandbox/session'
import { installCaptureNotices } from '@/ui/notify/d2Notices'
import { perfStore } from '@/stores/perf'
import { onWorldReset, worldScope } from '@/stores/worldScope'
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
import { probeRtCalls, UX } from '@/ui/testing/uxProbe'
import { power, powerStore, type Presence } from '@/app/power/PowerController'
import { connViewStore, installConnView, useConnView } from './connView'

const RtContext = React.createContext<RtClient | null>(null)
let agentsBinding: { setPanelOpen(open: boolean): void } | null = null

/** AGENTS panel visibility: while open the agents store subscribes agent/tasks and agent/+/status (M14-to-M15) */
export function setAgentsPanelOpen(open: boolean): void {
  agentsBinding?.setPanelOpen(open)
}

/** realtime URL of the current endpoint (net/endpoint: /api/rt, or the sandbox session's rt path) */
export function rtUrl(): string {
  return endpointRtUrl()
}

/** (re)initialise the client with a token (also used after "request control" issued a new operator token) */
export function initRt(client: RtClient, token: string): void {
  const p = perfStore.getState()
  client.init({ url: rtUrl(), token, tier: p.tier ?? 'S', deviceClass: p.deviceClass ?? 'software' })
}

/**
 * the endpoint key of a surface (null: no realtime link); `linkOpen` false (power saving) closes the link too.
 * D2.2-W2: the shared key carries the world (synthcity's live show and each UrbanScene3D city's recorded show are
 * different realtime sources, app/shell/surface.ts resolveSurface), so switching between two shown worlds reconnects
 * instead of being mistaken for the same endpoint (same rule as stores/worldScope sourceKeyOf).
 */
export function endpointKeyOf(s: Pick<SurfaceState, 'rtPath' | 'surface' | 'sid' | 'worldId'>, linkOpen = true): string | null {
  return s.rtPath && linkOpen ? `${s.surface === 'sandbox' ? `sandbox:${s.sid}` : `shared:${s.worldId ?? ''}`}` : null
}

/** presence through the page client when the net layer has the op (AWR-17 §17.20.4; M11 d2-WP-12c-to-M11 item 1) */
function presenceSink(client: RtClient): (p: Presence) => boolean {
  return (p) => {
    // RtClient.presence (M11, AWR-17 §17.20.4) sends through the worker while the link is open; nothing otherwise
    if (client.status !== 'LIVE' && client.status !== 'DEGRADED') return false
    client.presence({ input_age_s: p.input_age_s, visible: p.visible, power: p.power })
    return true
  }
}

/**
 * connect the client for a surface: sandbox -> the session token and endpoint; shared or local -> the page token;
 * no rt path -> closed. Returns a cleanup that detaches the sandbox binding.
 */
function connectFor(client: RtClient, s: SurfaceState, alive: () => boolean, linkOpen = true): () => void {
  client.close()
  if (s.surface === 'sandbox' && s.sid && (!s.rtPath || !linkOpen)) {
    // parked or power saving: no link, but the sandbox REST endpoint stays (GET session, resume)
    const sid = s.sid
    setEndpoint({ kind: 'sandbox', sid, token: () => sandboxStore.getState().token ?? '' })
    return () => {}
  }
  if (!s.rtPath || !linkOpen) {
    setEndpoint({ kind: 'shared' })
    return () => {}
  }
  if (s.surface === 'sandbox' && s.sid) {
    const sid = s.sid
    setEndpoint({
      kind: 'sandbox', sid,
      token: () => sandboxStore.getState().token ?? '',
      onAuthExpired: async () => ((await sandbox.keepalive()) || sandboxStore.getState().token ? sandboxStore.getState().token : null),
    })
    initRt(client, sandboxStore.getState().token ?? '')
    const offBind = bindSandboxRt(client)
    const offToken = onSandboxToken((tok) => client.updateToken(tok))
    return () => {
      offBind()
      offToken()
    }
  }
  if (getEndpoint().kind !== 'shared') setEndpoint({ kind: 'shared' })
  // D2.2-W2: a city's recorded show is a `fake:` replay URL (lib/demo cityShowRtUrl), not the page's live /api/rt
  // link — REST still goes through the shared show (the endpoint kind above), only the realtime socket differs, and
  // a replay needs no auth token (FakeSource ignores it)
  if (s.rtPath.startsWith('fake:')) {
    const p = perfStore.getState()
    client.init({ url: s.rtPath, token: '', tier: p.tier ?? 'S', deviceClass: p.deviceClass ?? 'software' })
    return () => {}
  }
  const myKey = endpointKeyOf(s, linkOpen)
  void getToken().then((token) => {
    if (alive() && endpointKeyOf(surfaceStore.getState(), powerStore.getState().linkOpen) === myKey) initRt(client, token)
  })
  return () => {}
}

export function RtProvider({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(() => probeRtCalls(createRtClient()))
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
    // the endpoint follows the surface (D2) and the power-save gate (D2.1): connect now and again whenever the key changes
    let key = endpointKeyOf(surfaceStore.getState(), powerStore.getState().linkOpen)
    let offConn = connectFor(client, surfaceStore.getState(), () => alive, powerStore.getState().linkOpen)
    const reconnect = (): void => {
      const s = surfaceStore.getState()
      const open = powerStore.getState().linkOpen
      UX.power.link = open
      const k = endpointKeyOf(s, open)
      if (k === key) return
      key = k
      offConn()
      offConn = connectFor(client, s, () => alive, open)
    }
    const offSurface = surfaceStore.subscribe(reconnect)
    const offPowerLink = powerStore.subscribe((p, prev) => {
      if (p.linkOpen !== prev.linkOpen) reconnect()
    })
    const offPowerConn = client.onConnState((st, info) => power.onConn(st, info.code ?? null))
    power.setPresenceSink(presenceSink(client))
    // the shared show's run world decides which /world/:id is the show and which is static browsing
    const offInfo = client.onServerInfo((si) => {
      worldScope.setStreamWorld(si.worldId || null)
      // D2.2-W2: a city's recorded show replays a real serverInfo byte for byte (its own world, not synthcity's); it
      // must not overwrite which world the live shared show is running, or every other city's surface (including
      // synthcity's) would mis-resolve afterwards (app/shell/surface.ts resolveSurface reads showWorld)
      const surf = surfaceStore.getState()
      const isCityReplay = typeof surf.rtPath === 'string' && surf.rtPath.startsWith('fake:')
      if (getEndpoint().kind === 'shared' && !isCityReplay) setShowWorld(si.worldId || null)
      power.setServerPower(((si as unknown as { power?: Record<string, unknown> }).power) ?? null)
    })
    const offNotices = installCaptureNotices(client)
    // a world or source switch: the client's latest swarm sample belongs to the previous world (the next frame refills it)
    const offWorld = onWorldReset('rt', () => client.clearWorldData?.())
    return () => {
      alive = false
      offWorld()
      offSurface()
      offPowerLink()
      offPowerConn()
      power.setPresenceSink(null)
      offConn()
      offInfo()
      offNotices()
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
