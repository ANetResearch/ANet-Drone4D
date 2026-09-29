// RtClient (in-process worker engine) against the real gateway over a real WebSocket (Node 22 global WebSocket):
// handshake and layout check, LIVE on SNAPSHOT, roster and swarm, perf/server with this connection's conn_id, takeoff
// accepted/running/succeeded with events, a fleet/cmd batch, and a client-side drop that reconnects to the same
// gateway session with resume. Opt-in: M11_GATEWAY=http://127.0.0.1:<port> of a running api (for example
// `.venv/bin/python -m awr.api.inproc --port 8061 --n 2`, or `make run`); skipped otherwise (the parallel phase does not
// start backends from the unit project).
import { afterAll, describe, expect, it } from 'vitest'
import { RtClientImpl } from '@/net/rt/client'
import { batchSummaryOf } from '@/net/rt/batch'
import type { CallResult, DataMsg, RtEvent } from '@/net/rt/types'

const GW = process.env.M11_GATEWAY ?? ''
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))
let client: RtClientImpl | null = null
afterAll(() => client?.close())

async function pump(c: RtClientImpl, ms: number, until: () => boolean): Promise<boolean> {
  const end = performance.now() + ms
  while (performance.now() < end) {
    c.swapFrame()
    if (until()) return true
    await sleep(16)
  }
  return until()
}

async function token(role: 'operator' | 'viewer'): Promise<string> {
  const r = await fetch(`${GW}/api/auth/token`, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ role, client: 'm11-integration', principal_hint: 'MELEVENNODEINTEGRATIONTEST' }) })
  if (r.status === 409) return token('viewer')
  expect(r.status).toBe(200)
  return ((await r.json()) as { token: string }).token
}

describe.skipIf(!GW)('real gateway (M11_GATEWAY)', () => {
  it('handshake, data, commands, batch and reconnect with resume', async () => {
    const tok = await token('operator')
    const c = new RtClientImpl({ inProcess: true, search: '', refreshToken: () => token('operator') })
    client = c
    const events: RtEvent[] = []
    const data: DataMsg[] = []
    c.onEvents((b) => events.push(...b))
    c.onData((m) => data.push(m))
    c.init({ url: `${GW.replace(/^http/, 'ws')}/api/rt`, token: tok, tier: 'S', deviceClass: 'software' })
    for (const [t, rate] of [['fleet/roster', 10], ['swarm/state', 10], ['perf/server', 1]] as const) c.subscribe(t, { rate })
    c.subscribe('event', { rate: 0, mode: 'all' })
    expect(await pump(c, 15_000, () => c.status === 'LIVE' && c.roster.size > 0 && c.swarm.n > 0)).toBe(true)
    const si = c.serverInfo!
    expect(si.worldId).toBeTruthy()
    expect(si.layouts['awr.SwarmLite32.v1']).toBeTruthy()
    const id = c.roster.entries()[0].id
    // perf/server lists this connection (the first sample can predate it: the SNAPSHOT is the cached latest value)
    const mine = (): boolean => data.some((m) => m.topic === 'perf/server'
      && ((m.data as { clients?: { conn_id: string }[] }).clients ?? []).some((x) => x.conn_id === si.connId))
    expect(await pump(c, 5000, mine)).toBe(true)
    if (si.role === 'viewer') return // someone else holds the seat: read-only checks only
    // takeoff (skip when already airborne from an earlier run)
    const h = c.call(`uav/${id}/cmd/takeoff`, { alt_m: 3 }, { timeoutMs: 5000 })
    const seq: string[] = []
    h.onResult((r) => seq.push(r.status))
    let fin: CallResult | null = null
    void h.result.then((r) => {
      fin = r
    })
    expect(await pump(c, 20_000, () => fin !== null)).toBe(true)
    if (fin!.status === 'succeeded') {
      expect(seq).toEqual(['accepted', 'running', 'succeeded'])
      expect(events.some((e) => e.type.startsWith('cmd.') && e.cid === h.id)).toBe(true)
    } else expect([fin!.status, fin!.code]).toEqual(['rejected', 105])
    // batch: hover on all
    const b = c.callBatch('hover', '*')
    const rs: CallResult[] = []
    b.onResult((r) => rs.push(r))
    let bfin: CallResult | null = null
    void b.result.then((r) => {
      bfin = r
    })
    await pump(c, 10_000, () => bfin !== null)
    expect(rs.length).toBeGreaterThan(0)
    const sum = batchSummaryOf(rs[0])
    if (sum) expect(sum.accepted_n + sum.rejected_n).toBe(c.roster.size)
    else expect(rs[0].code).toBeGreaterThan(0) // gateway without batch support answers with a reason code
    // drop the socket from the client side: reconnect to the same session with resume
    const sid = si.sessionId
    const lastSeq = Math.max(0, ...events.map((e) => e.seq))
    c.localHost!.socket!.close(4000)
    expect(await pump(c, 5000, () => c.status === 'RECONNECTING' || c.status === 'CONNECTING')).toBe(true)
    expect(await pump(c, 15_000, () => c.status === 'LIVE')).toBe(true)
    expect(c.serverInfo!.sessionId).toBe(sid)
    expect(c.connInfo.error).toBeUndefined()
    // resume: global event seqs stay strictly increasing across the reconnect (no duplicates, no reordering)
    await pump(c, 1000, () => false)
    const seqs = events.map((e) => e.seq)
    for (let i = 1; i < seqs.length; i++) expect(seqs[i]).toBeGreaterThan(seqs[i - 1])
    expect(lastSeq).toBeGreaterThanOrEqual(0)
  }, 90_000)
})
