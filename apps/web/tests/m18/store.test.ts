// M18-AC-017, AC-019, AC-020 (Node parts): the targets store mirrors the model (items, count / 30, selected detail), the
// actions send target/add|update|remove with the contract argument shapes, target.discovered events become red candidates
// until acknowledged, the manual warning predicts the entry; the overlay stays within 3 draws, 32 targets, 64 bodies and
// degrades faces -> selected bodies -> no bodies.
import { beforeEach, describe, expect, it } from 'vitest'
import { TARGET_LITE32 as L } from '@awr/contracts/layouts'
import { PAL, TARGETS_CAPS, TargetsOverlay, TargetsModel, targetsModel, type TargetDetail } from '@/engine/targets'
import type { CallResult, DataMsg, RtClient, RtEvent } from '@/net/rt'
import {
  attachTargetsFeed, manualKeepoutWarning, summarize, targetRedCandidate, targetsActions, targetsFeedTick, targetsStore,
} from '@/stores/targets'


/** n rows: target_no k at (40 k, 0, 0), class person, enabled, static */
function rows(n: number, extra: (dv: DataView, o: number, k: number) => void = () => {}): Uint8Array {
  const b = new Uint8Array(n * 32)
  const dv = new DataView(b.buffer)
  for (let k = 0; k < n; k++) {
    const o = k * 32
    dv.setUint16(o + L.AGENT_NO, k, true)
    dv.setUint8(o + L.CLASS, 1)
    dv.setUint8(o + L.STATE, 0x80)
    dv.setFloat32(o + L.POS, 40 * k, true)
    dv.setUint8(o + L.N_VOL, 2)
    extra(dv, o, k)
  }
  return b
}

function detail(id: string, extra: Partial<TargetDetail> = {}): TargetDetail {
  return { id, label: `人-${id}`, class: 'person', awareness: 'UNAWARE', alertness: 0.5, criterion_mode: 'geometric', discovered_count: 0,
    sensitivity: [{ idx: 0, kind: 'visual', shape: 'sphere', radius_cfg_m: 30, radius_eff_m: { p600_mid360: 12 }, enabled: true }], cfg_rev: 1, ...extra }
}

class FakeRt {
  calls: { service: string; args: object }[] = []
  subs: string[] = []
  data: ((m: DataMsg) => void)[] = []
  ev: ((b: readonly RtEvent[]) => void)[] = []
  roster = { targetIdOf: (n: number) => `person-${String(n + 1).padStart(2, '0')}` }
  subscribe(topic: string) {
    this.subs.push(topic)
    return () => {
      this.subs = this.subs.filter((t) => t !== topic)
    }
  }
  onData(cb: (m: DataMsg) => void) {
    this.data.push(cb)
    return () => {}
  }
  onEvents(cb: (b: readonly RtEvent[]) => void) {
    this.ev.push(cb)
    return () => {}
  }
  call(service: string, args: object) {
    this.calls.push({ service, args })
    const id = (args as { id?: string }).id ?? 'person-09'
    const r: CallResult = { id: 'c1', status: 'succeeded', code: 0, final: true,
      data: { result: { id, cfg: { id, class: 'person', cfg_rev: 1, sensitivity: [] } } } }
    return { id: 'c1', result: Promise.resolve(r), onResult() {}, onProgress() {}, cancel() {} }
  }
}

describe('targets store (M18-AC-017)', () => {
  let rt: FakeRt
  let detach: () => void
  beforeEach(() => {
    targetsModel.reset()
    targetsStore.setState({ items: [], count: 0, selected: null, detail: null, alarms: [], bodyView: 'configured', modelId: null })
    rt = new FakeRt()
    detach?.()
    detach = attachTargetsFeed(() => rt as unknown as RtClient)
  })

  it('subscribes, ingests, summarises', () => {
    expect(rt.subs).toEqual(expect.arrayContaining(['swarm/target/state', 'target/*/detail']))
    for (const cb of rt.data) cb({ topic: 'swarm/target/state', channelId: 1, seq: 1, tSimMs: 100, data: rows(3) })
    for (const cb of rt.data) cb({ topic: 'target/person-02/detail', channelId: 2, seq: 1, tSimMs: 100, data: detail('person-02') })
    const s = summarize()
    expect(s.count).toBe(3)
    expect(s.items.map((i) => i.id)).toEqual(['person-01', 'person-02', 'person-03'])
    expect(s.items[1].label).toBe('人-person-02')
    expect(s.items[0]).toMatchObject({ cls: 'person', aware: 'UNAWARE', enabled: true, level: 'none', pos: [0, 0, 0] })
    targetsActions.select('person-02')
    targetsFeedTick(rt as unknown as RtClient)
    expect(rt.subs).toContain('target/person-02/detail')
    expect(targetsStore.getState().detail?.id).toBe('person-02')
    expect(targetsStore.getState().max).toBe(30)
  })

  it('actions send the contract shapes', async () => {
    const o = await targetsActions.add('dog', [10, 20, null], { label: '巡逻犬' })
    expect(o.ok && o.id === 'person-09').toBe(true)
    expect(rt.calls[0]).toEqual({ service: 'target/add', args: { target: { label: '巡逻犬', class: 'dog', pos: [10, 20, null] } } })
    await targetsActions.update('person-01', { alertness: 0.9 })
    expect(rt.calls[1]).toEqual({ service: 'target/update', args: { id: 'person-01', patch: { alertness: 0.9 } } })
    await targetsActions.remove('person-01')
    expect(rt.calls[2]).toEqual({ service: 'target/remove', args: { id: 'person-01' } })
    expect(targetsModel.cfgs.has('person-09')).toBe(true)
  })

  it('discovered alarms are red candidates until acknowledged', () => {
    for (const cb of rt.ev) cb([{ seq: 7, t_sim_ns: 1, type: 'target.discovered', level: 3, data: { target: 'person-02', uav: 'uav-01', kind: 'visual' } }])
    expect(targetRedCandidate()?.id).toBe('person-02')
    targetsActions.acknowledge('person-02')
    expect(targetRedCandidate()).toBeNull()
  })

  it('manual warning 3 s ahead', () => {
    for (const cb of rt.data) cb({ topic: 'swarm/target/state', channelId: 1, seq: 1, tSimMs: 100, data: rows(1) })
    for (const cb of rt.data) cb({ topic: 'target/person-01/detail', channelId: 2, seq: 1, tSimMs: 100, data: detail('person-01') })
    const w = manualKeepoutWarning([-50, 0, 10], [10, 0, 0])
    expect(w?.id).toBe('person-01')
    expect(Math.abs((w?.tS ?? 9) - (50 - Math.sqrt(30 * 30 - 100)) / 10)).toBeLessThanOrEqual(0.2)
    expect(manualKeepoutWarning([-80, 0, 10], [-10, 0, 0])).toBeNull()
  })
})

describe('targets overlay (M18-AC-020, Node)', () => {
  it('caps, draws, degrade and red', () => {
    const m = new TargetsModel()
    m.resolver = { targetIdOf: (n) => `t-${n}` }
    m.ingestRows(rows(32), 0)
    for (let k = 0; k < 32; k++) {
      m.ingestDetail(`t-${k}`, detail(`t-${k}`, { sensitivity: [0, 1, 2, 3].map((i) => ({ idx: i, kind: i % 2 ? 'acoustic' : 'visual',
        shape: (['sphere', 'hemisphere', 'cylinder', 'sector'] as const)[i], radius_cfg_m: 20, enabled: true, z_min_m: 0, z_max_m: 30, half_angle_deg: 60 })) }))
    }
    const o = new TargetsOverlay()
    expect(o.update(m, { view: 'configured', modelId: null, selectedId: 't-3', redId: 't-5', showBodies: true })).toBe(true)
    expect(o.nTargets).toBe(32)
    expect(o.nBodies).toBe(TARGETS_CAPS.bodies)
    expect(o.drawCount()).toBe(3)
    expect(o.update(m, { view: 'configured', modelId: null, selectedId: 't-3', redId: 't-5', showBodies: true })).toBe(false)
    // red only on the red owner's box (13 segments of target 5 carry palette r500)
    const info = o.boxes.obj.geometry.getAttribute('lineInfo').array as Float32Array
    let red = 0
    for (let k = 0; k < o.boxes.n; k++) if (info[8 * k] === PAL.R500) red++
    expect(red).toBe(13)
    o.level = 1
    o.update(m, { view: 'configured', modelId: null, selectedId: 't-3', redId: null, showBodies: true })
    expect(o.faces.visible).toBe(false)
    expect(o.drawCount()).toBe(2)
    o.level = 2
    o.update(m, { view: 'planning', modelId: null, selectedId: 't-3', redId: null, showBodies: true })
    expect(o.nBodies).toBe(4)
    o.level = 3
    o.update(m, { view: 'configured', modelId: null, selectedId: 't-3', redId: null, showBodies: true })
    expect(o.nBodies).toBe(0)
    expect(o.drawCount()).toBe(1)
    o.dispose()
  })
  it('effective view uses the model radii', () => {
    const m = new TargetsModel()
    m.resolver = { targetIdOf: () => 'p-1' }
    m.ingestRows(rows(1), 0)
    m.ingestDetail('p-1', detail('p-1'))
    expect(m.volumesOfRow(0, 'configured')[0].rM).toBe(30)
    expect(m.volumesOfRow(0, 'effective', 'p600_mid360')[0].rM).toBe(12)
    expect(m.volumesOfRow(0, 'planning')[0].rM).toBeCloseTo(50.06, 6)
  })
})
