// M19-AC-025 (Node part), D2-AC-52, D2-AC-53: the perception store subscribes field/{kind} @1 for the field view,
// field/volumes @1 while volumes or the acoustic / visual view are on, target/{selected}/quant @2 (phone @1) while a target
// is selected; switching the view swaps the subscription; the phone applies every other profile (0.5 Hz); the decoded
// profiles and volumes reach engine/perception fieldModel, the readings and the legend are mirrored in the store and equal
// the topic payload; a new connection drops the old arrays; no write requests (viewer safe).
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'
import { fieldModel } from '@/engine/perception'
import type { ConnState, DataMsg, RtClient, RtEvent } from '@/net/rt'
import {
  attachPerceptionFeed, fieldTopics, perceptionActions, perceptionFeedIngest, perceptionFeedTick, perceptionStore, readoutRows, setPerceptionClock,
} from '@/stores/perception'
import { targetsStore } from '@/stores/targets'

const FIELD = JSON.parse(readFileSync(join(import.meta.dirname, 'field_cases.json'), 'utf8')) as { profiles: { kind: string; frame: unknown; delta: unknown }[];
  volumes: { frame: unknown } }
const QUANT = JSON.parse(readFileSync(join(import.meta.dirname, 'quant_cases.json'), 'utf8')) as { soa: { topic: unknown; expanded: { pairs: unknown[] } } }

class FakeRt {
  subs: { topic: string; rate: number }[] = []
  calls = 0
  conn: ((s: ConnState) => void) | null = null
  subscribe(topic: string, o: { rate: number }) {
    this.subs.push({ topic, rate: o.rate })
    return () => {
      this.subs = this.subs.filter((t) => t.topic !== topic)
    }
  }
  onData(_cb: (m: DataMsg) => void) {
    return () => {}
  }
  onEvents(_cb: (b: readonly RtEvent[]) => void) {
    return () => {}
  }
  onConnState(cb: (s: ConnState) => void) {
    this.conn = cb
    return () => {}
  }
  call() {
    this.calls++
    throw new Error('no writes')
  }
}

const msg = (topic: string, data: unknown): DataMsg => ({ topic, channelId: 1, seq: 1, tSimMs: 0, data })
const topics = (rt: FakeRt) => rt.subs.map((s) => `${s.topic}@${s.rate}`).sort()

describe('perception store: field view and readings', () => {
  let now = 0
  beforeEach(() => {
    perceptionActions.setFieldView('none')
    perceptionActions.setVolumes('off')
    perceptionActions.setDeviceClass('desktop')
    targetsStore.setState({ selected: null })
    now = 0
    setPerceptionClock(() => now)
  })

  it('subscriptions follow the field view, volumes and the selected target', () => {
    const rt = new FakeRt()
    const off = attachPerceptionFeed(() => rt as unknown as RtClient)
    perceptionFeedTick(rt as unknown as RtClient)
    expect(topics(rt)).toEqual([])
    perceptionActions.setFieldView('thermal')
    perceptionFeedTick(rt as unknown as RtClient)
    expect(topics(rt)).toEqual(['field/thermal@1'])
    perceptionActions.cycleFieldView()                                   // thermal -> radar (Shift+V)
    perceptionFeedTick(rt as unknown as RtClient)
    expect(topics(rt)).toEqual(['field/radar@1'])
    perceptionActions.setFieldView('acoustic')
    perceptionFeedTick(rt as unknown as RtClient)
    expect(topics(rt)).toEqual(['field/acoustic@1', 'field/volumes@1'])  // acoustic shells need the volume tables
    targetsStore.setState({ selected: 'person-01' })
    perceptionFeedTick(rt as unknown as RtClient)
    expect(topics(rt)).toEqual(['field/acoustic@1', 'field/volumes@1', 'target/person-01/quant@2'])
    perceptionActions.setDeviceClass('phone')
    perceptionFeedTick(rt as unknown as RtClient)
    expect(topics(rt)).toContain('target/person-01/quant@1')
    perceptionActions.setFieldView('none')
    perceptionActions.setVolumes('all')
    perceptionFeedTick(rt as unknown as RtClient)
    expect(topics(rt)).toEqual(['field/volumes@1', 'target/person-01/quant@1'])
    off()
    expect(rt.subs).toEqual([])
    expect(rt.calls).toBe(0)
  })

  it('profiles reach fieldModel (phone 0.5 Hz), legend and readings are mirrored', () => {
    const rt = new FakeRt()
    const off = attachPerceptionFeed(() => rt as unknown as RtClient)
    const ac = FIELD.profiles.find((p) => p.kind === 'acoustic')!
    perceptionActions.setFieldView('acoustic')
    targetsStore.setState({ selected: 'person-01' })
    perceptionFeedTick(rt as unknown as RtClient)
    const v0 = fieldModel.version
    perceptionFeedIngest.data(msg('field/acoustic', ac.frame))
    perceptionFeedIngest.data(msg('field/volumes', FIELD.volumes.frame))
    perceptionFeedIngest.data(msg('target/person-01/quant', QUANT.soa.topic))
    perceptionFeedIngest.data(msg('target/dog-01/quant', QUANT.soa.topic))  // not the selected target: ignored
    perceptionFeedTick(rt as unknown as RtClient)
    expect(fieldModel.version).toBeGreaterThan(v0)
    expect(fieldModel.profiles.get('acoustic')?.uavs.length).toBe(2)
    expect(fieldModel.volumes?.targets.size).toBe(2)
    const s = perceptionStore.getState()
    expect(s.legend?.unit).toBe('dba')
    expect(s.legend?.bands).toEqual([20, 30, 40, 50, 60])
    expect(s.quant?.pairs).toEqual(QUANT.soa.expanded.pairs)             // readout card data equals the topic
    expect(readoutRows(s.quant).length).toBe(12)
    // phone: a second profile within 2 s is not applied
    perceptionActions.setDeviceClass('phone')
    now = 1000
    perceptionFeedIngest.data(msg('field/acoustic', ac.frame))          // applies (first after the class change keeps the old time)
    const v1 = fieldModel.version
    now = 1500
    perceptionFeedIngest.data(msg('field/acoustic', ac.delta))
    expect(fieldModel.version).toBe(v1)
    now = 4000
    perceptionFeedIngest.data(msg('field/acoustic', ac.delta))
    expect(fieldModel.version).toBe(v1 + 1)
    // a new connection drops the arrays (revisions restart with a new sim-core)
    perceptionFeedIngest.conn('SYNCING')
    expect(fieldModel.profiles.size).toBe(0)
    expect(fieldModel.volumes).toBeNull()
    off()
  })

  it('fieldTopics is pure', () => {
    expect([...fieldTopics({ fieldView: 'visual', volumesMode: 'off', deviceClass: 'tablet' }, 'x-01')]).toEqual([
      ['field/visual', 1], ['field/volumes', 1], ['target/x-01/quant', 2]])
    expect([...fieldTopics({ fieldView: 'radar', volumesMode: 'off', deviceClass: 'desktop' }, null)]).toEqual([['field/radar', 1]])
  })
})
