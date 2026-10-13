// D2 network layer (M11-FR-219, M11-AC-212 unit part; AWR-17 §17.7.1, §17.9.3): API and realtime endpoints by session
// role with a configurable API origin, sandbox tokens on sandbox session paths only (public sandbox routes go out
// without a token, a 401 refreshes once through the endpoint), the refused-upgrade triage URL, close 4410 (final, no
// reconnect) and 1012 `world_switch` (immediate reconnect, worldSwitch to the main thread), token updates without a
// reconnect, roster targets kept apart from vehicles, and FakeSource replaying the D2 sandbox fixture.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiDelete, apiGet, apiPatch, apiPost } from '@/net/api'
import {
  apiBase, apiOrigin, apiPath, apiUrl, authCheckPath, getEndpoint, isSandboxPath, onEndpointChange, rtUrl, sandboxTokenFor,
  setApiOrigin, setEndpoint, sidOfPath,
} from '@/net/endpoint'
import { createFakeRtClient, RtClientImpl } from '@/net/rt/client'
import { FakeSource } from '@/net/rt/FakeSource'
import { WS_OPEN } from '@/net/rt/transport'
import { readBytes } from '../contracts/util'
import { ctrlOps, frames, microtasks, rig } from '../net/harness'

const SID = 'sb-k3m7q2x7ab'
const loc = { protocol: 'https:', host: 'drone4d.example' }

afterEach(() => {
  setEndpoint({ kind: 'shared' })
  setApiOrigin('')
  vi.unstubAllGlobals()
})

describe('endpoint', () => {
  it('shared and sandbox prefixes, realtime URLs and the API origin', () => {
    expect(getEndpoint().kind).toBe('shared')
    expect(apiBase()).toBe('/api')
    expect(rtUrl(loc)).toBe('wss://drone4d.example/api/rt')
    const seen: string[] = []
    const off = onEndpointChange((e) => seen.push(e.kind))
    setEndpoint({ kind: 'sandbox', sid: SID, token: () => 'tok-1' })
    setEndpoint({ kind: 'sandbox', sid: SID, token: () => 'tok-2' }) // same sid: no change event
    off()
    expect(seen).toEqual(['sandbox'])
    expect(apiBase()).toBe(`/api/sandbox/v1/sessions/${SID}`)
    expect(apiPath('vehicles')).toBe(`/api/sandbox/v1/sessions/${SID}/vehicles`)
    expect(rtUrl({ protocol: 'http:', host: 'h:8002' })).toBe(`ws://h:8002/api/sandbox/v1/sessions/${SID}/rt`)
    setApiOrigin('https://gw.example:8443/')
    expect(apiOrigin()).toBe('https://gw.example:8443')
    expect(apiUrl('/api/worlds')).toBe('https://gw.example:8443/api/worlds')
    expect(rtUrl(loc)).toBe(`wss://gw.example:8443/api/sandbox/v1/sessions/${SID}/rt`)
    expect(() => setEndpoint({ kind: 'sandbox', sid: 'sb-NOPE', token: () => '' })).toThrow()
  })

  it('session paths, tokens and the triage URL', () => {
    setEndpoint({ kind: 'sandbox', sid: SID, token: () => 'tok-1' })
    expect(sidOfPath(`/api/sandbox/v1/sessions/${SID}/vehicles`)).toBe(SID)
    expect(sidOfPath(`https://x/api/sandbox/v1/sessions/${SID}`)).toBe(SID)
    expect(sidOfPath('/api/sandbox/v1/worlds')).toBeNull()
    expect(isSandboxPath('/api/sandbox/v1/worlds') && !isSandboxPath('/api/worlds')).toBe(true)
    expect(sandboxTokenFor(`/api/sandbox/v1/sessions/${SID}/targets`)).toBe('tok-1')
    expect(sandboxTokenFor('/api/sandbox/v1/sessions/sb-aaaaaaaaaa/targets')).toBe('')
    expect(sandboxTokenFor('/api/sandbox/v1/worlds')).toBe('')
    expect(authCheckPath(`wss://h/api/sandbox/v1/sessions/${SID}/rt`)).toBe(`/api/sandbox/v1/sessions/${SID}`)
    expect(authCheckPath('wss://h/api/rt')).toBe('/api/auth/whoami')
  })
})

describe('REST with sandbox tokens', () => {
  it('session routes carry the sandbox token, public routes none, 401 refreshes once', async () => {
    const calls: { url: string; auth: string | null; method: string }[] = []
    let accept = 'tok-2'
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
      const auth = new Headers(init.headers).get('authorization')
      calls.push({ url, auth, method: init.method ?? 'GET' })
      if (url.endsWith('/auth/token')) return new Response('{}', { status: 404 })
      if (url.includes('/sessions/') && auth !== `Bearer ${accept}`) {
        return new Response(JSON.stringify({ code: 302, message: 'token' }), { status: 401 })
      }
      return new Response(JSON.stringify({ ok: true, url }), { status: 200 })
    }))
    let refreshed = 0
    setEndpoint({ kind: 'sandbox', sid: SID, token: () => 'tok-1', onAuthExpired: async () => {
      refreshed++
      return 'tok-2'
    } })
    await apiGet('/api/sandbox/v1/worlds')
    expect(calls.at(-1)).toMatchObject({ url: '/api/sandbox/v1/worlds', auth: null })
    const r = await apiGet<{ ok: boolean }>(`/api/sandbox/v1/sessions/${SID}/vehicles`)
    expect(r.ok).toBe(true)
    expect(refreshed).toBe(1)
    expect(calls.slice(-2).map((c) => c.auth)).toEqual(['Bearer tok-1', 'Bearer tok-2'])
    await apiPost(`/api/sandbox/v1/sessions/${SID}/targets`, { class: 'person' }, { headers: { authorization: 'Bearer tok-2' } })
    await apiPatch(`/api/sandbox/v1/sessions/${SID}/targets/tg-1`, { label: 'x' }, { headers: { authorization: 'Bearer tok-2' } })
    expect(await apiDelete(`/api/sandbox/v1/sessions/${SID}/targets/tg-1`, { headers: { authorization: 'Bearer tok-2' } })).toBe(200)
    expect(calls.slice(-3).map((c) => c.method)).toEqual(['POST', 'PATCH', 'DELETE'])
    // a gone session: onAuthExpired returns null and the 401 surfaces as ApiError
    accept = 'nobody'
    setEndpoint({ kind: 'sandbox', sid: SID, token: () => 'tok-1', onAuthExpired: async () => null })
    await expect(apiGet(`/api/sandbox/v1/sessions/${SID}/vehicles`)).rejects.toMatchObject({ status: 401, reason: 302 })
  })

  it('the configured origin prefixes every request', async () => {
    const urls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      urls.push(url)
      return new Response('{}', { status: 200 })
    }))
    setApiOrigin('https://gw.example')
    await apiGet('/api/sandbox/v1/meta')
    expect(urls).toEqual(['https://gw.example/api/sandbox/v1/meta'])
  })
})

describe('sandbox close codes (AWR-17 §17.9.3)', () => {
  it('4410 is final: FATAL sandbox_gone and a sandboxGone control message, no reconnect', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 3)
    expect(r.host.connState).toBe('LIVE')
    r.fake.serverClose(4410)
    await frames(r, 30)
    expect(r.host.connState).toBe('FATAL')
    expect(ctrlOps(r.ctrl, 'sandboxGone')).toEqual([{ op: 'sandboxGone', code: 4410 }])
    const last = ctrlOps(r.ctrl, 'conn').at(-1) as Record<string, unknown>
    expect(last).toMatchObject({ state: 'FATAL', code: 4410, error: 'sandbox_gone' })
    expect(r.sockets).toBe(1)
  })

  it('1012 world_switch reconnects at once and tells the main thread; plain 1012 keeps the backoff', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 3)
    r.fake.serverClose(1012, 'world_switch')
    await frames(r, 1)
    expect(ctrlOps(r.ctrl, 'worldSwitch').length).toBe(1)
    const c = ctrlOps(r.ctrl, 'conn').find((m) => m.code === 1012) as Record<string, unknown>
    expect(c).toMatchObject({ state: 'RECONNECTING', nextInMs: 0 })
    await frames(r, 20)
    expect(r.host.connState).toBe('LIVE')
    expect(r.sockets).toBe(2)
    r.fake.serverClose(1012)
    await frames(r, 1)
    expect((ctrlOps(r.ctrl, 'conn').at(-1) as Record<string, unknown>).nextInMs).toBeGreaterThan(0)
    expect(ctrlOps(r.ctrl, 'worldSwitch').length).toBe(1)
  })
})

describe('client facade', () => {
  it('updateToken changes the next connection token without reconnecting; listeners fire', async () => {
    const c = createFakeRtClient({ n: 2 })
    const host = c.localHost!
    let gone = 0
    let switched = 0
    c.onSandboxGone(() => gone++)
    c.onWorldSwitch(() => switched++)
    for (let i = 0; i < 20 && c.status !== 'LIVE'; i++) {
      c.swapFrame()
      await new Promise((r) => setTimeout(r, 20))
    }
    const before = host.counters.reconnects
    c.updateToken('tok-renewed')
    await microtasks()
    expect(host.counters.reconnects).toBe(before)
    expect((host as unknown as { token: string }).token).toBe('tok-renewed')
    ;(c as unknown as { onCtrl(l: { op: string; code?: number }[]): void }).onCtrl([{ op: 'worldSwitch' }, { op: 'sandboxGone', code: 4410 }])
    expect([switched, gone]).toEqual([1, 1])
    c.close()
  })

  it('roster targets are kept apart from vehicles (agent_no numbered within each kind)', () => {
    const c = new RtClientImpl({ inProcess: true, search: '' })
    ;(c as unknown as { onCtrl(l: Record<string, unknown>[]): void }).onCtrl([{ op: 'roster', version: 3, entries: [
      { agent_no: 0, id: 'u01', kind: 'uav', model: 'p600' },
      { agent_no: 1, id: 'u02', kind: 'uav', model: 'p600' },
      { agent_no: 0, id: 'tg-1', kind: 'target', model: 'person', label: 'walker' },
    ] }])
    expect(c.roster.entries().map((e) => e.id)).toEqual(['u01', 'u02'])
    expect(c.roster.get(0)?.id).toBe('u01')
    expect(c.roster.targets?.()).toEqual([{ targetNo: 0, id: 'tg-1', cls: 'person', label: 'walker' }])
    expect(c.roster.targetIdOf?.(0)).toBe('tg-1')
    expect(c.roster.size).toBe(2)
  })
})

describe('FakeSource drives the D2 sandbox fixture', () => {
  it('replays sandbox_t5.awrrt: sandbox serverInfo and every D2 topic advertised', async () => {
    let t = 0
    const fake = new FakeSource({ fixture: readBytes('fixtures/d2/sandbox_t5.awrrt'), autoTick: false, now: () => t })
    const texts: Record<string, unknown>[] = []
    fake.onmessage = (e) => {
      if (typeof e.data === 'string') texts.push(JSON.parse(e.data) as Record<string, unknown>)
    }
    await microtasks()
    expect(fake.readyState).toBe(WS_OPEN)
    for (let guard = 0; !fake.replayDone && guard < 2000; guard++) {
      t += 5
      fake.tick()
      await microtasks()
    }
    const info = texts.find((m) => m.op === 'serverInfo') as Record<string, unknown>
    expect(info.capabilities).toContain('sandbox')
    expect((info.sandbox as Record<string, unknown>).sid).toBe(SID)
    const topics = new Set(texts.filter((m) => m.op === 'advertise')
      .flatMap((m) => (m.channels as { topic: string }[]).map((ch) => ch.topic.replace(/^(uav|target|nest|task|relay)\/[^/]+\//, '$1/*/'))))
    for (const t of ['swarm/target/state', 'target/*/detail', 'uav/*/perception', 'uav/*/payload', 'nest/*/status',
      'task/*/status', 'relay/*/stats', 'sandbox/session']) expect(topics.has(t), t).toBe(true)
  })
})
