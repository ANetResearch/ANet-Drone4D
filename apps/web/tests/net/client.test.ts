// RtClient facade (M11-FR-094, M11-AC-036): in-process RtHost with FakeSource and real timers; swapFrame slot exchange,
// reference-counted subscriptions, roster view, calls with every result and the final Promise, events, fleet summary.
import { afterEach, describe, expect, it } from 'vitest'
import { createRtClient, type RtClientImpl } from '@/net/rt/client'
import type { CallResult, RtEvent } from '@/net/rt/types'

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

let client: RtClientImpl | null = null
afterEach(() => {
  client?.close()
  client = null
})

async function pump(c: RtClientImpl, ms: number, until?: () => boolean): Promise<void> {
  const end = performance.now() + ms
  while (performance.now() < end) {
    c.swapFrame()
    if (until?.()) return
    await sleep(16)
  }
}

describe('RtClient with FakeSource', () => {
  it('connects, swaps slots, merges subscriptions and closes goto', async () => {
    const c = createRtClient({ inProcess: true, search: '?source=fake&fakeN=1&fakeStart=10,10,80' })
    client = c
    const states: string[] = []
    c.onConnState((s) => states.push(s))
    const events: RtEvent[] = []
    c.onEvents((b) => events.push(...b))
    c.init({ url: 'ws://unused/api/rt', token: '', tier: 'S', deviceClass: 'software' })
    const off1 = c.subscribe('swarm/state', { rate: 10 })
    const off2 = c.subscribe('swarm/state', { rate: 20 }) // same topic: one subscription at the highest rate
    const off3 = c.subscribe('fleet/roster', { rate: 10 })
    const off4 = c.subscribe('event', { rate: 0, mode: 'all' })
    await pump(c, 3000, () => c.status === 'LIVE' && c.roster.size > 0 && c.swarm.n > 0)
    expect(c.status).toBe('LIVE')
    expect(states).toContain('SYNCING')
    expect(c.roster.idOf(0)).toBe('p600-01')
    expect(c.roster.agentNoOf('p600-01')).toBe(0)
    expect(c.serverInfo?.worldId).toBe('shenzhen')
    expect(c.swarm.n).toBe(1)
    expect(c.swarm.pos[2]).toBeCloseTo(80, 3)
    const seen: string[] = []
    const h = c.call('uav/p600-01/cmd/goto', { pos: [20, 5, 85], speed_mps: 12 })
    h.onResult((r) => seen.push(r.status))
    let fin: CallResult | null = null
    void h.result.then((r) => {
      fin = r
    })
    await pump(c, 8000, () => fin !== null)
    expect(fin).not.toBeNull()
    expect(fin!.status).toBe('succeeded')
    expect(seen).toEqual(['accepted', 'running', 'succeeded'])
    await pump(c, 400)
    expect(Math.hypot(c.swarm.pos[0] - 20, c.swarm.pos[1] - 5, c.swarm.pos[2] - 85)).toBeLessThan(0.6)
    expect(events.some((e) => e.type === 'cmd.succeeded')).toBe(true)
    off1()
    off2()
    off3()
    off4()
    expect(c.counters.swaps).toBeGreaterThan(5)
  })

  it('rejects calls before init', async () => {
    const c = createRtClient({ inProcess: true })
    const r = await c.call('uav/x/cmd/hover', {}).result
    expect([r.status, r.code]).toEqual(['rejected', 213])
  })
})
