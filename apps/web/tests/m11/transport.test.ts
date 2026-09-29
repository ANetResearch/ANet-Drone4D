// Reconnect and close-code handling of rt.worker (M11-FR-093, M11 §6.6.3, M11-AC-008, AC-047; AWR-17 §6.13 item 2, §8.3;
// AWR-14 §7.6, §7.7): backoff shape, the close-code table (backoff, 4401 fresh token without backoff, 4403 viewer, 4429
// 30 s, 1013/1009 classified, 1002 x3 / 1008 / 4426 FATAL), 1006 triage with whoami (401 refresh, 403 FATAL auth), layout
// hash mismatch self-close, connect timeout, close() and reconnectNow().
import { describe, expect, it } from 'vitest'
import * as L from '@awr/contracts/layouts'
import { BACKOFF, backoffDelay, httpUrlOf, protocolsFor } from '@/net/rt/transport'
import { ctrlOps, frames, rig } from '../net/harness'
import { advertise, connOf, ctrlOf, scriptRig, serverInfo, timeFrame } from './wire'

const lastConn = (ctrl: { op: string }[]): Record<string, unknown> => ctrlOps(ctrl as never, 'conn').at(-1) as Record<string, unknown>

describe('backoff and subprotocols', () => {
  it('500 ms x 1.5^n up to 10 s with +-20 % jitter', () => {
    expect(backoffDelay(1, 0.5)).toBe(500)
    expect(backoffDelay(2, 0.5)).toBe(750)
    expect(backoffDelay(3, 0.5)).toBe(1125)
    expect(backoffDelay(30, 0.5)).toBe(BACKOFF.maxDelayMs)
    expect(backoffDelay(1, 0)).toBe(400)
    expect(backoffDelay(1, 0.999999)).toBe(600)
    expect(backoffDelay(30, 0)).toBe(8000)
    expect(BACKOFF.connectTimeoutMs).toBe(4000)
  })
  it('awr.rt.v1 first, bearer token second, no empty bearer', () => {
    expect(protocolsFor('abc')).toEqual(['awr.rt.v1', 'bearer.abc'])
    expect(protocolsFor('')).toEqual(['awr.rt.v1'])
  })
  it('whoami URL on the same host', () => {
    expect(httpUrlOf('ws://127.0.0.1:8000/api/rt', '/api/auth/whoami')).toBe('http://127.0.0.1:8000/api/auth/whoami')
    expect(httpUrlOf('wss://h.example/api/rt?x=1', '/api/auth/whoami')).toBe('https://h.example/api/auth/whoami')
  })
})

describe('close codes (AWR-17 §8.3)', () => {
  it.each([1001, 1011, 4408, 1000])('%i: backoff reconnect and LIVE again', async (code) => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 3)
    expect(r.host.connState).toBe('LIVE')
    r.fake.serverClose(code)
    await frames(r, 1)
    expect(r.host.connState).toBe('RECONNECTING')
    const c = lastConn(r.ctrl)
    expect([c.code, c.nextInMs, c.error]).toEqual([code, backoffDelay(1, 0.5), undefined])
    await frames(r, 40)
    expect(r.host.connState).toBe('LIVE')
    expect(r.sockets).toBe(2)
  })

  it('backoff grows with consecutive failures and resets after a handshake', async () => {
    const r = scriptRig()
    r.sock.serverClose(1006)
    expect(connOf(r).nextInMs).toBe(backoffDelay(1, 0.5))
    r.advance(backoffDelay(1, 0.5))
    r.sock.serverClose(1006)
    expect(connOf(r).nextInMs).toBe(backoffDelay(2, 0.5))
    r.advance(backoffDelay(2, 0.5))
    r.sock.open()
    r.sock.serverSend(serverInfo())
    expect(r.host.connState).toBe('SYNCING')
    r.sock.serverClose(1011)
    expect(connOf(r).nextInMs).toBe(backoffDelay(1, 0.5))
  })

  it('1013 backlog (E-09), 4429 30 s (E-17), 1009 defect', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 2)
    r.fake.serverClose(1013)
    await frames(r, 1)
    expect(lastConn(r.ctrl)).toMatchObject({ state: 'RECONNECTING', code: 1013, error: 'backlog' })
    await frames(r, 40)
    r.fake.serverClose(4429)
    await frames(r, 1)
    expect(lastConn(r.ctrl)).toMatchObject({ state: 'RECONNECTING', code: 4429, error: 'conn_limit', nextInMs: BACKOFF.connLimitMs })
    await frames(r, 60)
    expect(r.host.connState).toBe('RECONNECTING') // still waiting the 30 s
    r.clock.t += BACKOFF.connLimitMs
    r.runTimers()
    await frames(r, 3)
    expect(r.host.connState).toBe('LIVE')
    r.fake.serverClose(1009)
    await frames(r, 1)
    expect(lastConn(r.ctrl)).toMatchObject({ code: 1009, error: 'too_big' })
    expect(ctrlOps(r.ctrl, 'defect')).toHaveLength(1)
  })

  it('4401: fresh token then immediate reconnect without backoff', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 2)
    r.fake.serverClose(4401)
    await frames(r, 1)
    expect(lastConn(r.ctrl)).toMatchObject({ state: 'RECONNECTING', code: 4401, nextInMs: 0 })
    expect(ctrlOps(r.ctrl, 'needToken').at(-1)).toMatchObject({ role: 'operator' })
    const before = r.sockets
    r.host.onMessage({ cmd: 'token', token: 'fresh' })
    await frames(r, 3)
    expect(r.sockets).toBe(before + 1)
    expect(r.host.connState).toBe('LIVE')
    // a successful handshake resets the auth failure count: one more 4401 is again a plain refresh
    r.fake.serverClose(4401)
    await frames(r, 1)
    expect(r.host.connState).toBe('RECONNECTING')
  })

  it('4401 twice before any handshake gives FATAL auth', () => {
    const r = scriptRig()
    r.sock.serverClose(4401)
    r.host.onMessage({ cmd: 'token', token: 'a' })
    r.sock.serverClose(4401)
    r.host.onMessage({ cmd: 'token', token: 'b' })
    r.sock.serverClose(4401)
    expect(r.host.connState).toBe('FATAL')
    expect(connOf(r)).toMatchObject({ state: 'FATAL', error: 'auth' })
  })

  it('4403: viewer token (E-16), hello asks for the viewer role', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 2)
    r.fake.serverClose(4403)
    await frames(r, 1)
    expect(lastConn(r.ctrl)).toMatchObject({ state: 'RECONNECTING', code: 4403, error: 'seat_revoked' })
    expect(ctrlOps(r.ctrl, 'needToken').at(-1)).toMatchObject({ role: 'viewer' })
    r.host.onMessage({ cmd: 'token', token: 'viewer-token' })
    await frames(r, 3)
    expect(r.host.connState).toBe('LIVE')
    const hello = r.fake.received.find((m) => m.op === 'hello')!
    expect(hello.role).toBe('viewer')
  })

  it.each([[1008, 'policy'], [4426, 'version']] as const)('%i is FATAL (%s)', async (code, error) => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 2)
    r.fake.serverClose(code)
    await frames(r, 30)
    expect(r.host.connState).toBe('FATAL')
    expect(lastConn(r.ctrl)).toMatchObject({ error })
    expect(r.sockets).toBe(1)
  })

  it('1002 three times in a row is FATAL protocol', () => {
    const r = scriptRig()
    for (let i = 0; i < 3; i++) {
      r.sock.serverClose(1002)
      if (i < 2) {
        expect(r.host.connState).toBe('RECONNECTING')
        r.advance(2000) // past the backoff, before the 4 s connect timeout of the next attempt
      }
    }
    expect(r.host.connState).toBe('FATAL')
    expect(connOf(r)).toMatchObject({ error: 'protocol' })
  })

  it('1006 triage: whoami 403 is FATAL auth, 401 asks for a token, 200 keeps reconnecting', async () => {
    for (const [status, expectState] of [[403, 'FATAL'], [401, 'RECONNECTING'], [200, 'RECONNECTING']] as const) {
      let asked = 0
      const r = scriptRig({ checkAuth: () => {
        asked++
        return Promise.resolve(status)
      } })
      r.sock.serverClose(1006)
      await Promise.resolve()
      await Promise.resolve()
      expect(asked).toBe(1)
      expect(r.host.connState).toBe(expectState)
      if (status === 403) expect(connOf(r)).toMatchObject({ error: 'auth' })
      if (status === 401) expect(ctrlOf(r, 'needToken')).toHaveLength(1)
      if (status === 200) expect(ctrlOf(r, 'needToken')).toHaveLength(0)
    }
  })

  it('layout hash mismatch: 1000 self-close and FATAL version (E-07)', () => {
    const r = scriptRig()
    r.sock.open()
    r.sock.serverSend(serverInfo({ layouts: { 'awr.SwarmLite32.v1': 'deadbeef' } }))
    expect(r.host.connState).toBe('FATAL')
    expect(connOf(r)).toMatchObject({ error: 'version' })
    expect(ctrlOf(r, 'error').at(-1)).toMatchObject({ code: 312, name: 'LAYOUT_MISMATCH' })
    expect(r.sock.ops('hello')).toHaveLength(0)
    r.advance(30_000)
    expect(r.socks).toHaveLength(1)
  })

  it('contracts major mismatch is FATAL version', () => {
    const r = scriptRig()
    r.sock.open()
    r.sock.serverSend(serverInfo({ contracts: '9.0.0' }))
    expect(r.host.connState).toBe('FATAL')
    expect(ctrlOf(r, 'error').at(-1)).toMatchObject({ code: 311 })
  })

  it('connect timeout 4 s counts as an abnormal close', () => {
    const r = scriptRig()
    r.advance(BACKOFF.connectTimeoutMs + 1)
    expect(r.host.connState).toBe('RECONNECTING')
    expect(connOf(r).code).toBe(1006)
  })

  it('close() is CLOSED without reconnect; reconnectNow() leaves FATAL', () => {
    const r = scriptRig()
    r.sock.open()
    r.sock.serverSend(serverInfo({ contracts: '9.0.0' }))
    expect(r.host.connState).toBe('FATAL')
    r.host.onMessage({ cmd: 'reconnect' })
    expect(r.host.connState).toBe('CONNECTING')
    expect(r.socks).toHaveLength(2)
    r.host.onMessage({ cmd: 'close' })
    expect(r.host.connState).toBe('CLOSED')
    r.advance(60_000)
    expect(r.socks).toHaveLength(2)
  })

  it('serverInfo re-sent inside a connection does not send a second hello', () => {
    const r = scriptRig()
    r.sock.open()
    r.sock.serverSend(serverInfo())
    r.sock.serverSend(advertise())
    r.sock.serverSend(timeFrame({ epoch: 1, tSimNs: 0, tSrvNs: 0 }))
    r.sock.serverSend(serverInfo())
    expect(r.sock.ops('hello')).toHaveLength(1)
    expect(ctrlOf(r, 'serverInfo').length + r.posts).toBeGreaterThan(0)
    const hello = r.sock.ops('hello')[0]
    expect(hello).toMatchObject({ client: 'awr-web/0.1.0', contracts: L.CONTRACTS_VERSION, tier: 'S', deviceClass: 'software' })
    expect(r.sock.protocols).toEqual(['awr.rt.v1', 'bearer.tok'])
  })

  it('reauth: a new token and role reconnect at once; hello carries the viewer role', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 3)
    expect(r.host.connState).toBe('LIVE')
    r.host.onMessage({ cmd: 'reauth', token: 'viewer-token', role: 'viewer' })
    await frames(r, 3)
    expect(r.sockets).toBe(2)
    expect(r.host.connState).toBe('LIVE')
    expect(r.fake.received.find((m) => m.op === 'hello')!.role).toBe('viewer')
    r.host.onMessage({ cmd: 'reauth', token: 'operator-token', role: 'operator' })
    await frames(r, 3)
    expect(r.sockets).toBe(3)
    expect(r.fake.received.find((m) => m.op === 'hello')!.role).toBeUndefined()
  })
})

