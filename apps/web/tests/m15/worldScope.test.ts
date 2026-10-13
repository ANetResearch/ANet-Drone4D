// D2.2-W1 world isolation (Node part): the world scope (stores/worldScope) gates the ingestion of realtime data to the
// world the view shows and resets every world-bound store when the world, the realtime source or the stream's world
// changes; the surface binding derives the source key and the view's world (app/shell/worldScope); the targets, perception
// and field samples, tasking, fleet, selection, sensors, mission and safety stores are empty after a switch from synthcity
// to a static city and ignore another world's stream, and take data again once the view is back on the show's world.
import { beforeEach, describe, expect, it } from 'vitest'
import { TARGET_LITE32 as L } from '@awr/contracts/layouts'
import type { NestStatus } from '@awr/contracts/types'
import { targetsModel } from '@/engine/targets'
import { fieldModel } from '@/engine/perception'
import * as sensorsEngine from '@/engine/sensors'
import type { DataMsg, RtClient, RtEvent } from '@/net/rt'
import { onWorldReset, worldLive, worldScope, worldScopeNow } from '@/stores/worldScope'
import { attachTargetsFeed, targetsActions, targetsFeedTick, targetsStore } from '@/stores/targets'
import { attachPerceptionFeed, perceptionActions, perceptionFeedIngest, perceptionFeedTick, perceptionStore } from '@/stores/perception'
import { attachTaskingFeed, taskingFeedTick, taskingStore } from '@/stores/tasking'
import { fleetStore, summariseFleet, type FleetSource } from '@/stores/fleet'
import { selection, selectionStore } from '@/stores/selection'
import { sensorsFeedTick, sensorsStore } from '@/stores/sensors'
import { ingestMissionData, flushMission, missionStore } from '@/stores/mission'
import { bindSafety, flushSafety, safetyStore } from '@/stores/safety'
import { sourceKeyOf, viewWorldOf } from '@/app/shell/worldScope'

class FakeRt {
  subs: { topic: string; rate: number }[] = []
  data: ((m: DataMsg) => void)[] = []
  ev: ((b: readonly RtEvent[]) => void)[] = []
  status = 'LIVE'
  serverInfo = { role: 'viewer', worldId: 'synthcity' }
  roster = { version: 1, size: 0, targetIdOf: (n: number) => `person-${String(n + 1).padStart(2, '0')}`, agentNoOf: () => -1, get: () => undefined }
  subscribe(topic: string, o: { rate: number } = { rate: 1 }) {
    this.subs.push({ topic, rate: o.rate })
    return () => {
      this.subs = this.subs.filter((t) => t.topic !== topic)
    }
  }
  onData(cb: (m: DataMsg) => void) {
    this.data.push(cb)
    return () => {
      this.data = this.data.filter((c) => c !== cb)
    }
  }
  onEvents(cb: (b: readonly RtEvent[]) => void) {
    this.ev.push(cb)
    return () => {
      this.ev = this.ev.filter((c) => c !== cb)
    }
  }
  push(topic: string, data: unknown): void {
    for (const cb of [...this.data]) cb({ topic, channelId: 1, seq: 1, tSimMs: 100, data })
  }
  events(b: RtEvent[]): void {
    for (const cb of [...this.ev]) cb(b)
  }
}
const rtOf = (r: FakeRt): RtClient => r as unknown as RtClient

/** n TargetLite32 rows: target_no k at (40 k, 0, 0), class person, enabled */
function rows(n: number): Uint8Array {
  const b = new Uint8Array(n * 32)
  const dv = new DataView(b.buffer)
  for (let k = 0; k < n; k++) {
    const o = k * 32
    dv.setUint16(o + L.AGENT_NO, k, true)
    dv.setUint8(o + L.CLASS, 1)
    dv.setUint8(o + L.STATE, 0x80)
    dv.setFloat32(o + L.POS, 40 * k, true)
    dv.setUint8(o + L.N_VOL, 2)
  }
  return b
}
const ev = (seq: number, type: string, data: Record<string, unknown>, uav?: string): RtEvent =>
  ({ seq, t_sim_ns: seq * 1e9, type, level: 3, data, ...(uav ? { uav } : {}) }) as RtEvent

/** the shared show of synthcity, live (view synthcity, source shared, stream synthcity) */
function onShow(): void {
  worldScope.setView('synthcity', 'shared')
  worldScope.setStreamWorld('synthcity')
}
/** in-app switch to a static city of the demo build: the source goes first (surface), then the opened world (canvas) */
function toStatic(world: string): void {
  worldScope.setView(worldScopeNow().world, null)
  worldScope.setView(world, null)
}

describe('world scope (D2.2-W1)', () => {
  beforeEach(() => worldScope.clearForTests())

  it('is open while unbound and binds at start-up without a reset', () => {
    expect(worldLive()).toBe(true)
    let n = 0
    const off = onWorldReset('test', () => n++)
    worldScope.setView('synthcity', 'shared')
    expect(n).toBe(0)
    expect(worldScopeNow()).toMatchObject({ bound: true, live: false, foreign: false, epoch: 0 })
    worldScope.setStreamWorld('synthcity')
    expect(n).toBe(0) // the first serverInfo of a source clears nothing
    expect(worldLive()).toBe(true)
    off()
  })

  it('resets on a world switch, a source switch and a stream that changes world, never on a closed link of the same source', () => {
    onShow()
    const seen: string[] = []
    const off = onWorldReset('test', (s) => seen.push(`${s.source}|${s.world}|${s.streamWorld}|${s.live}`))
    worldScope.setView('synthcity', 'shared') // power saving closed and reopened the link: the scope does not change
    worldScope.setStreamWorld('synthcity')
    expect(seen).toEqual([])
    toStatic('shenzhen')
    expect(seen).toEqual(['null|synthcity|null|false', 'null|shenzhen|null|false'])
    expect(worldScopeNow().foreign).toBe(true)
    worldScope.setStreamWorld('synthcity') // no source: a late serverInfo is ignored
    expect(worldScopeNow().streamWorld).toBe(null)
    worldScope.setView('shenzhen', 'shared')
    worldScope.setView('synthcity', 'shared')
    expect(worldLive()).toBe(false) // waiting for the serverInfo of the new connection
    worldScope.setStreamWorld('synthcity')
    expect(worldLive()).toBe(true)
    seen.length = 0
    worldScope.setView('synthcity', 'sandbox:sb-abcdefghij') // a sandbox of the same world is another source
    expect(seen).toEqual(['sandbox:sb-abcdefghij|synthcity|null|false'])
    worldScope.setStreamWorld('synthcity')
    seen.length = 0
    worldScope.setStreamWorld('shenzhen') // sandbox world switch: the stream moved before the view
    expect(seen).toEqual(['sandbox:sb-abcdefghij|synthcity|shenzhen|false'])
    expect(worldScopeNow()).toMatchObject({ live: false, foreign: true })
    off()
  })

  it('derives the source key and the view world from the surface', () => {
    // D2.2-W2: the shared key carries the world (synthcity's live show and each city's recorded show are different
    // realtime sources) so switching between two shown worlds reconnects and resets instead of looking unchanged
    expect(sourceKeyOf({ surface: 'shared', sid: null, worldId: 'synthcity' })).toBe('shared:synthcity')
    expect(sourceKeyOf({ surface: 'shared', sid: null, worldId: 'shenzhen' })).toBe('shared:shenzhen')
    expect(sourceKeyOf({ surface: 'local', sid: null, worldId: 'shenzhen' })).toBe('local')
    expect(sourceKeyOf({ surface: 'sandbox', sid: 'sb-abcdefghij', worldId: 'synthcity' })).toBe('sandbox:sb-abcdefghij')
    expect(sourceKeyOf({ surface: 'static', sid: null, worldId: 'shanghai' })).toBe(null)
    expect(sourceKeyOf({ surface: 'sandbox-gone', sid: 'sb-abcdefghij', worldId: null })).toBe(null)
    expect(viewWorldOf('synthcity', 'shenzhen')).toBe('synthcity') // the opened world wins until the canvas switches
    expect(viewWorldOf(null, 'shenzhen')).toBe('shenzhen') // no viewport (UI-only builds)
  })
})

describe('world-bound stores are cleared on a switch and ignore another world (D2.2-W1)', () => {
  beforeEach(() => {
    worldScope.clearForTests()
    onShow()
  })

  it('targets: rows, selection, placement preview and alarms of synthcity never reach shenzhen', () => {
    const rt = new FakeRt()
    const detach = attachTargetsFeed(() => rtOf(rt))
    rt.push('swarm/target/state', rows(3))
    rt.events([ev(1, 'target.discovered', { target: 'person-01', uav: 'p600-03', kind: 'visual' })])
    targetsFeedTick(rtOf(rt))
    targetsActions.select('person-02')
    targetsActions.setPlacing('person')
    expect(targetsStore.getState()).toMatchObject({ count: 3, selected: 'person-02', placing: 'person' })
    expect(targetsStore.getState().alarms).toHaveLength(1)
    toStatic('shenzhen')
    let s = targetsStore.getState()
    expect(s).toMatchObject({ count: 0, items: [], selected: null, detail: null, placing: null, alarms: [] })
    expect(targetsModel.rows.n).toBe(0)
    // a late message of the closed show and a foreign stream are not taken
    rt.push('swarm/target/state', rows(2))
    rt.events([ev(2, 'target.discovered', { target: 'person-02', uav: 'p600-03', kind: 'visual' })])
    targetsFeedTick(rtOf(rt))
    s = targetsStore.getState()
    expect(s.count).toBe(0)
    expect(s.alarms).toEqual([])
    for (const w of ['suzhou', 'newyork']) {
      worldScope.setView(w, null)
      rt.push('swarm/target/state', rows(3))
      targetsFeedTick(rtOf(rt))
      expect(targetsStore.getState().count).toBe(0)
    }
    // back on the show's world: the stream is taken again
    worldScope.setView('newyork', 'shared')
    worldScope.setView('synthcity', 'shared')
    worldScope.setStreamWorld('synthcity')
    rt.push('swarm/target/state', rows(3))
    targetsFeedTick(rtOf(rt))
    expect(targetsStore.getState().count).toBe(3)
    detach()
  })

  it('perception: frames, captures, detections, field samples and readings go; nothing is subscribed for another world', () => {
    const rt = new FakeRt()
    const detach = attachPerceptionFeed(() => rtOf(rt))
    selection.select(['p600-03'])
    perceptionActions.setFieldView('acoustic')
    perceptionActions.setVolumes('all')
    perceptionFeedTick(rtOf(rt))
    expect(rt.subs.map((x) => x.topic).sort()).toEqual(['field/acoustic', 'field/volumes', 'uav/p600-03/perception'])
    perceptionFeedIngest.events([ev(1, 'perception.capture', { uav: 'p600-03', sensor: 'eo', target: 'person-01', level_to: 'R' })])
    fieldModel.quant.set('person-01', { target: 'person-01' } as never)
    perceptionFeedTick(rtOf(rt))
    expect(perceptionStore.getState().captures).toHaveLength(1)
    toStatic('shenzhen')
    perceptionFeedTick(rtOf(rt))
    const s = perceptionStore.getState()
    expect(s).toMatchObject({ uav: null, frame: null, hud: null, captured: [], captures: [], detections: [], quant: null, legend: null })
    expect(fieldModel.profiles.size).toBe(0)
    expect(fieldModel.volumes).toBe(null)
    expect(fieldModel.quant.size).toBe(0)
    expect(rt.subs).toEqual([]) // the interest-driven server generates nothing for this page
    // the view preferences stay
    expect(s.fieldView).toBe('acoustic')
    expect(s.volumesMode).toBe('all')
    perceptionFeedIngest.events([ev(2, 'perception.capture', { uav: 'p600-03', sensor: 'eo', target: 'person-02', level_to: 'R' })])
    perceptionFeedTick(rtOf(rt))
    expect(perceptionStore.getState().captures).toEqual([])
    perceptionActions.setFieldView('none')
    perceptionActions.setVolumes('off')
    detach()
  })

  it('tasking: nests, tasks and relay statistics of synthcity go', () => {
    const rt = new FakeRt()
    const detach = attachTaskingFeed(() => rtOf(rt))
    rt.push('nest/nest-01/status', { id: 'nest-01', state: 'READY' } as unknown as NestStatus)
    rt.push('task/t-1/status', { tid: 't-1', type: 'relay_watch', state: 'RUNNING', revision: 1 })
    taskingFeedTick(rtOf(rt))
    expect(Object.keys(taskingStore.getState().nestStatus)).toEqual(['nest-01'])
    expect(taskingStore.getState().tasks).toHaveLength(1)
    toStatic('shenzhen')
    taskingFeedTick(rtOf(rt))
    expect(taskingStore.getState()).toMatchObject({ nests: [], nestStatus: {}, tasks: [], plans: {}, preview: null, selected: null })
    rt.push('task/t-2/status', { tid: 't-2', type: 'relay_watch', state: 'RUNNING', revision: 1 })
    taskingFeedTick(rtOf(rt))
    expect(taskingStore.getState().tasks).toEqual([])
    detach()
  })

  it('fleet: the rows are empty while the client still holds the show\'s last sample', () => {
    const swarm = { n: 2, seq: 1, tSimMs: 0, version: 1, agentNo: Uint16Array.of(1, 2), fs: new Uint8Array(2), battery: Uint8Array.of(90, 80),
      flags: new Uint8Array(2), ctrl: new Uint8Array(2), pos: new Float32Array(6), vel: new Float32Array(6), quat: new Float32Array(8) }
    const src = { swarm, roster: { version: 1 }, status: 'LIVE' } as unknown as FleetSource
    summariseFleet(src, 0)
    expect(fleetStore.getState().n).toBe(2)
    toStatic('shenzhen')
    expect(fleetStore.getState().n).toBe(0)
    swarm.version++
    summariseFleet(src, 10)
    expect(fleetStore.getState().n).toBe(0)
  })

  it('selection and hover go with the world', () => {
    selection.select(['p600-03', 'p600-04'])
    selection.setHover('p600-05')
    toStatic('shenzhen')
    expect(selectionStore.getState()).toMatchObject({ ids: [], primary: null, hover: null })
  })

  it('sensors: detections and the engine cache go; detections of another world are not taken', () => {
    sensorsEngine.onDetectEvent({ type: 'sensor.detect', seq: 1, t_sim_ns: 1e9, data: { target_id: 'person-01', uav: 'p600-03', state: 'confirmed', conf: 0.9 } })
    sensorsFeedTick(null, null)
    expect(sensorsStore.getState().detections).toHaveLength(1)
    toStatic('shenzhen')
    expect(sensorsStore.getState().detections).toEqual([])
    expect(sensorsEngine.detectionsList()).toEqual([])
    expect(sensorsEngine.ingestFrame({ hdr: { swarmN: 0 } } as never)).toBe(0)
  })

  it('mission and safety: rows and alarms of synthcity go and later messages are not taken', () => {
    ingestMissionData({ topic: 'mission/m-1/status', channelId: 1, seq: 1, tSimMs: 0,
      data: { mid: 'm-1', state: 'RUNNING', revision: 1, t_ns: 1, progress: 0.5, tracks: [] } })
    flushMission()
    expect(missionStore.getState().rows.size).toBe(1)
    toStatic('shenzhen')
    expect(missionStore.getState().rows.size).toBe(0)
    ingestMissionData({ topic: 'mission/m-2/status', channelId: 1, seq: 1, tSimMs: 0, data: { mid: 'm-2', state: 'RUNNING', revision: 1, t_ns: 1 } })
    flushMission()
    expect(missionStore.getState().rows.size).toBe(0)

    worldScope.setView('synthcity', 'shared')
    worldScope.setStreamWorld('synthcity')
    const rt = new FakeRt()
    const off = bindSafety(rt, selectionStore, () => 0)
    rt.events([ev(3, 'safety.alarm', { code: 'GEOFENCE_BREACH', level: 3 }, 'p600-03')])
    flushSafety(1000)
    expect(safetyStore.getState().alarms.length).toBeGreaterThan(0)
    toStatic('suzhou')
    expect(safetyStore.getState()).toMatchObject({ alarms: [], toasts: [], unackedCritical: 0 })
    rt.events([ev(4, 'safety.alarm', { code: 'GEOFENCE_BREACH', level: 3 }, 'p600-04')])
    flushSafety(2000)
    expect(safetyStore.getState().alarms).toEqual([])
    off()
  })
})
