// D2.1 network layer (M11-FR-231, M11-AC-218; AWR-17 §17.20.3 to §17.20.5; D2-INT): presence goes out as a control
// message only while the link is open; close 4430 (power saving, after error 518) and 4431 (viewer seats full, after error
// 517) are final for the link (CLOSED, no reconnect; the power-save controller resumes or queues); the identity token rides
// the handshake as the `ident.<token>` subprotocol and a change reconnects at once; shared-show REST requests carry
// `X-AWR-Identity` from the registered provider (sandbox paths do not); raw TargetLite32 rows and blobs reach the data path
// as bytes; serverInfo exposes the D2 sandbox block and the D2.1 power and identity fields.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiGet, setIdentityProvider } from '@/net/api'
import { ChannelTable, K_BYTES, K_DATA } from '@/net/rt/decode'
import { RtClientImpl } from '@/net/rt/client'
import { CLOSE_IDLE_RECLAIMED, CLOSE_VIEWER_SEATS_FULL } from '@/net/rt/session'
import { protocolsFor } from '@/net/rt/transport'
import { ctrlOps, frames, rig } from '../net/harness'

afterEach(() => {
  setIdentityProvider(null)
  vi.unstubAllGlobals()
})

describe('close codes 4430 and 4431 (AWR-17 §17.20.7)', () => {
  for (const code of [CLOSE_IDLE_RECLAIMED, CLOSE_VIEWER_SEATS_FULL]) {
    it(`${code} closes the link without a reconnect and reports the code`, async () => {
      const r = await rig({ url: 'fake:?n=1' })
      await frames(r, 3)
      expect(r.host.connState).toBe('LIVE')
      r.fake.serverClose(code)
      await frames(r, 30)
      r.runTimers()
      await frames(r, 5)
      expect(r.host.connState).toBe('CLOSED')
      expect(r.sockets).toBe(1)
      expect(ctrlOps(r.ctrl, 'conn').at(-1)).toMatchObject({ state: 'CLOSED', code })
    })
  }
})

describe('presence and identity', () => {
  it('presence is sent as an op while open and dropped while closed', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 3)
    r.host.onMessage({ cmd: 'presence', m: { input_age_s: 3.5, visible: true, power: 'quiet' } })
    r.host.onMessage({ cmd: 'presence', m: { input_age_s: 1.2, visible: false, power: 'warning' } })
    await frames(r, 2)
    const sent = r.fake.received.filter((m) => m.op === 'presence')
    expect(sent).toEqual([{ op: 'presence', input_age_s: 3.5, visible: true, power: 'quiet' },
      { op: 'presence', input_age_s: 1.2, visible: false, power: 'warning' }])
    const fake = r.fake
    fake.serverClose(CLOSE_IDLE_RECLAIMED)
    await frames(r, 5)
    r.host.onMessage({ cmd: 'presence', m: { input_age_s: 0, visible: true, power: 'active' } })
    await frames(r, 2)
    expect(fake.received.filter((m) => m.op === 'presence').length).toBe(2)
  })

  it('subprotocol list with the identity token; a new identity reconnects at once', async () => {
    expect(protocolsFor('tok')).toEqual(['awr.rt.v1', 'bearer.tok'])
    expect(protocolsFor('tok', 'v1.a.b')).toEqual(['awr.rt.v1', 'bearer.tok', 'ident.v1.a.b'])
    expect(protocolsFor('', null)).toEqual(['awr.rt.v1'])
    const seen: string[][] = []
    const r = await rig({ url: 'fake:?n=1' })
    const env = (r.host as unknown as { env: { socket: (u: string, p: string[]) => unknown } }).env
    const orig = env.socket
    env.socket = (u: string, p: string[]) => {
      seen.push(p)
      return orig(u, p)
    }
    await frames(r, 3)
    r.host.onMessage({ cmd: 'identity', token: 'v1.x.y' })
    await frames(r, 5)
    expect(seen.length).toBe(1)
    expect(seen[0]).toContain('ident.v1.x.y')
    r.host.onMessage({ cmd: 'identity', token: 'v1.x.y' }) // same token: nothing
    await frames(r, 3)
    expect(seen.length).toBe(1)
  })

  it('shared-show REST carries X-AWR-Identity, sandbox paths do not', async () => {
    const hdrs: (string | null)[] = []
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init: RequestInit) => {
      hdrs.push(new Headers(init.headers).get('x-awr-identity'))
      return new Response('{}', { status: 200 })
    }))
    setIdentityProvider(() => 'v1.ident.tok')
    await apiGet('/api/sys/info', { headers: { authorization: 'Bearer t' } })
    await apiGet('/api/sandbox/v1/worlds')
    setIdentityProvider(null)
    await apiGet('/api/sys/info', { headers: { authorization: 'Bearer t' } })
    expect(hdrs).toEqual(['v1.ident.tok', null, null])
  })
})

describe('data path and serverInfo', () => {
  it('TargetLite32 rows and blobs are byte channels; msgpack stays data', () => {
    const t = new ChannelTable()
    t.advertise({ id: 7, topic: 'swarm/target/state', encoding: 'raw', schemaName: 'awr.TargetLite32.v1' })
    t.advertise({ id: 8, topic: 'uav/a/sensor/lidar/scan', encoding: 'blob', schemaName: 'awr.LidarScan8.v1' })
    t.advertise({ id: 9, topic: 'field/acoustic', encoding: 'msgpack', schemaName: 'awr.field.profile.v1' })
    expect([t.kind[7], t.kind[8], t.kind[9]]).toEqual([K_BYTES, K_BYTES, K_DATA])
  })

  it('serverInfo view carries sandbox, power and identity', () => {
    const c = new RtClientImpl({ inProcess: true, search: '' })
    ;(c as unknown as { onCtrl(l: Record<string, unknown>[]): void }).onCtrl([{ op: 'serverInfo', sessionId: 's', connId: 'c', world: { id: 'synthcity' },
      run: { id: 'sb-aaaaaaaaaa', segment: 0 }, clock: { tick_hz: 100, rate_cap: 5 }, role: 'operator', seat: 'held', capabilities: ['sandbox'],
      sandbox: { sid: 'sb-aaaaaaaaaa', state: 'ACTIVE', world_id: 'synthcity', template_id: 't1' },
      power: { viewer_idle_s: 300, presence_min_s: 15 }, identity: { tier: 'internal', exp_unix_ns: '1' } }])
    const si = c.serverInfo!
    expect(si.sandbox?.sid).toBe('sb-aaaaaaaaaa')
    expect([si.tickHz, si.rateCap]).toEqual([100, 5])
    expect(si.power?.viewer_idle_s).toBe(300)
    expect(si.identity?.tier).toBe('internal')
  })
})
