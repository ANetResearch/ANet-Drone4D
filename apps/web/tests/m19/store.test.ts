// M19-FR-028 (Node part): the perception store follows the selection primary (or an explicit follow), subscribes its
// `uav/{id}/perception` at 5 Hz and releases it on change, mirrors the frame and the HUD line, keeps the recent detections
// and at most one capture per target per 30 s, and sends no write request (viewer safe, D2-AC-37).
import { beforeEach, describe, expect, it } from 'vitest'
import type { DataMsg, RtClient, RtEvent } from '@/net/rt'
import {
  attachPerceptionFeed, captureDue, detectionOf, levelHistory, perceptionActions, perceptionFeedIngest, perceptionFeedTick, perceptionStore,
} from '@/stores/perception'
import { selection } from '@/stores/selection'

class FakeRt {
  subs: { topic: string; rate: number }[] = []
  calls = 0
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
  call() {
    this.calls++
    throw new Error('no writes')
  }
}

const frame = (uav: string, t: number) => ({
  uav, t_sim_ns: t, sensors: [{ sensor: 'eo', kind: 'eo', targets: [{ target: 'person-01', level: 'R', n_eff: 15.1, n_req: 14, range_m: 600, gsd_m: 0.03,
    limiting: null, captured: true }] }], hud: { sensor: 'eo', target: 'person-01', n_eff: 15.1, n_req: 14, limiting: null },
})
const ev = (seq: number, type: string, data: Record<string, unknown>): RtEvent => ({ seq, t_sim_ns: seq * 1e9, type, level: 1, data })

describe('perception store', () => {
  beforeEach(() => {
    perceptionActions.follow(null)
    perceptionActions.resetDedup()
    perceptionActions.clearDetections()
    selection.select([])
  })

  it('follows the selection primary and an explicit follow; one subscription at 5 Hz', () => {
    const rt = new FakeRt()
    const off = attachPerceptionFeed(() => rt as unknown as RtClient)
    selection.select(['uav-02'])
    perceptionFeedTick(rt as unknown as RtClient)
    expect(rt.subs).toEqual([{ topic: 'uav/uav-02/perception', rate: 5 }])
    perceptionActions.follow('uav-03')
    perceptionFeedTick(rt as unknown as RtClient)
    expect(rt.subs).toEqual([{ topic: 'uav/uav-03/perception', rate: 5 }])
    perceptionFeedIngest.data({ topic: 'uav/uav-03/perception', channelId: 1, seq: 1, tSimMs: 1000, data: frame('uav-03', 1e9) })
    perceptionFeedIngest.data({ topic: 'uav/uav-02/perception', channelId: 2, seq: 1, tSimMs: 1000, data: frame('uav-02', 2e9) })
    perceptionFeedTick(rt as unknown as RtClient)
    const s = perceptionStore.getState()
    expect(s.uav).toBe('uav-03')
    expect(s.frame?.t_sim_ns).toBe(1e9)
    expect(s.hud?.text).toBe('15.1 / 14 px')
    expect(s.captured).toEqual([{ sensor: 'eo', target: 'person-01' }])
    off()
    expect(rt.subs).toEqual([])
    expect(rt.calls).toBe(0)
  })

  it('detections and 30 s capture de-duplication per target', () => {
    const rt = new FakeRt()
    const off = attachPerceptionFeed(() => rt as unknown as RtClient)
    perceptionFeedIngest.events([
      ev(1, 'perception.level', { uav: 'uav-01', sensor: 'eo', target: 'person-01', level_from: 'none', level_to: 'R', n_eff: 15, n_req: 14 }),
      ev(2, 'perception.capture', { uav: 'uav-01', sensor: 'eo', target: 'person-01', level_to: 'R' }),
      ev(3, 'perception.capture', { uav: 'uav-02', sensor: 'eo', target: 'person-01', level_to: 'R' }),
      ev(4, 'perception.detect', { uav: 'uav-01', sensor: 'radar', sensor_kind: 'radar', track_id: 7, level: 'D' }),
      ev(5, 'target.discovered', { target: 'person-01' }),
    ])
    perceptionFeedTick(rt as unknown as RtClient)
    const s = perceptionStore.getState()
    expect(s.detections.map((d) => d.type)).toEqual(['perception.level', 'perception.capture', 'perception.capture', 'perception.detect'])
    expect(s.captures.length).toBe(1)
    expect(s.detections[3].target).toBeNull()
    expect(s.detections[3].levelTo).toBe('D')
    expect(levelHistory(s, 'person-01')).toEqual([{ tS: 1, level: 2 }])
    off()
  })

  it('captureDue is a 30 s window per target; detectionOf ignores other events', () => {
    expect(captureDue('t1', 0)).toBe(true)
    expect(captureDue('t1', 29_999)).toBe(false)
    expect(captureDue('t2', 29_999)).toBe(true)
    expect(captureDue('t1', 30_000)).toBe(true)
    expect(detectionOf(ev(1, 'target.state', {}))).toBeNull()
  })
})
