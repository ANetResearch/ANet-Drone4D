// Capacity store (M17-FR-078, AC-032; AWR-04 §15.5.3; D2-AC-41 unit part): the boot request goes out at once with a 1.5 s
// timeout, repeats every 5 s while the mask is visible and stops at the reveal; after the reveal it polls every 10 s only
// while a visible consumer holds a lease; `updated` turns on when the server release differs from the bundle (never for
// local builds); the request carries no token; failures leave the last good value; ETA buckets and queue hints.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { BuildInfo, CapacityView } from '@/net/sandbox/client'
import { getCapacity, setSandboxFetch } from '@/net/sandbox/client'
import {
  acquireCapacityPolling, adoptBootCapacity, BOOT_REFRESH_MS, BOOT_TIMEOUT_MS, capacityPollingOwners, capacityStore, configureCapacity, etaBucket,
  POLL_MS, queueNeeded, refreshCapacity, startBootCapacity, stopBootCapacity,
} from '@/stores/capacity'

const BUILD: BuildInfo = { version: '0.2.0', channel: 'demo', commit: '3f9a1c2d4e', dirty: false, built_at: '2026-10-12T08:30:00Z',
  release: '20261012-083000-3f9a1c2d4e' }

function cap(over: Partial<CapacityView> = {}): CapacityView {
  return { schema: 'awr.capacity.v1', t_unix_ns: '1', build: BUILD, accepting: true, maintenance: false, degrade_level: 'L0',
    viewers: { live: 18, max: 24, queue: 0, eta_s: null, state: 'open' },
    sandbox: { active: 2, max: 2, reserved_internal: 1, internal_active: 0, parked: 1, queue: 3, eta_s: 360, admit_now: false,
      worlds_admit_now: [], state: 'queue' },
    power: { viewer_idle_s: 300, sandbox_idle_s: 600, hidden_s: 60, warn_s: 30 }, hint: 'queue_sandbox', ...over }
}

// manual timers
let now = 0
let timers: { at: number; fn: () => void; id: number }[] = []
let nextId = 1
function setTimer(fn: () => void, ms: number): unknown {
  const id = nextId++
  timers.push({ at: now + ms, fn, id })
  return id
}
function clearTimer(h: unknown): void {
  timers = timers.filter((t) => t.id !== h)
}
async function advance(ms: number): Promise<void> {
  const end = now + ms
  for (;;) {
    timers.sort((a, b) => a.at - b.at)
    const t = timers[0]
    if (!t || t.at > end) break
    timers.shift()
    now = t.at
    t.fn()
    await Promise.resolve()
    await Promise.resolve()
  }
  now = end
  for (let i = 0; i < 5; i++) await Promise.resolve()
}

let fetches = 0
let reply: () => Promise<CapacityView> = async () => cap()

beforeEach(() => {
  now = 1_000
  timers = []
  fetches = 0
  reply = async () => cap()
  configureCapacity({ fetch: () => { fetches++; return reply() }, now: () => now, build: () => BUILD, setTimer, clearTimer })
})

afterEach(() => {
  stopBootCapacity()
  setSandboxFetch(null)
})

describe('boot', () => {
  it('requests at once, every 5 s while the mask shows, stops at the reveal', async () => {
    const stop = startBootCapacity()
    expect(fetches).toBe(1)
    await advance(1)
    expect(capacityStore.getState().phase).toBe('ok')
    expect(capacityStore.getState().data?.sandbox.queue).toBe(3)
    await advance(BOOT_REFRESH_MS)
    expect(fetches).toBe(2)
    await advance(BOOT_REFRESH_MS * 2)
    expect(fetches).toBe(4)
    stop()
    await advance(BOOT_REFRESH_MS * 3)
    expect(fetches).toBe(4)
  })

  it('a hanging request times out after 1.5 s and reports unavailable; the next tick retries', async () => {
    reply = () => new Promise<CapacityView>(() => {})
    startBootCapacity()
    await advance(BOOT_TIMEOUT_MS - 10)
    expect(capacityStore.getState().phase).toBe('loading')
    await advance(20)
    expect(capacityStore.getState().phase).toBe('unavailable')
    expect(capacityStore.getState().failures).toBe(1)
    reply = async () => cap()
    await advance(BOOT_REFRESH_MS)
    expect(capacityStore.getState().phase).toBe('ok')
  })

  // d2-WP-12c-to-M17 item 1 (D2-INT): the production hand-over of the early script's request in flight
  it('adopts the boot request in flight: no second request, later fetches go out', async () => {
    let resolve: (v: CapacityView) => void = () => {}
    adoptBootCapacity(new Promise<CapacityView>((r) => { resolve = r }))
    startBootCapacity()
    expect(fetches).toBe(0)
    resolve(cap({ hint: 'open' }))
    await advance(1)
    expect(capacityStore.getState().data?.hint).toBe('open')
    await advance(BOOT_REFRESH_MS)
    expect(fetches).toBe(1)
    adoptBootCapacity(null)
    await advance(BOOT_REFRESH_MS)
    expect(fetches).toBe(2)
  })

  it('keeps the last good value on a later failure', async () => {
    await refreshCapacity()
    reply = async () => { throw new Error('down') }
    await refreshCapacity()
    const s = capacityStore.getState()
    expect(s.phase).toBe('ok')
    expect(s.failures).toBe(1)
    expect(s.data?.hint).toBe('queue_sandbox')
  })
})

describe('after the reveal', () => {
  it('polls every 10 s only while a visible consumer holds a lease', async () => {
    await refreshCapacity()
    expect(fetches).toBe(1)
    await advance(POLL_MS * 3)
    expect(fetches).toBe(1)                                        // nobody visible: no polling
    const rel = acquireCapacityPolling('landing')
    const rel2 = acquireCapacityPolling('queue-card')
    expect(capacityPollingOwners().sort()).toEqual(['landing', 'queue-card'])
    expect(fetches).toBe(2)                                        // stale value refreshed at once
    await advance(POLL_MS)
    expect(fetches).toBe(3)
    rel()
    await advance(POLL_MS)
    expect(fetches).toBe(4)
    rel2()
    rel2()
    await advance(POLL_MS * 3)
    expect(fetches).toBe(4)
  })
})

describe('build comparison', () => {
  it('updated when the server release differs; never for local builds', async () => {
    reply = async () => cap({ build: { ...BUILD, release: '20261013-090000-aaaaaaaaaa' } })
    await refreshCapacity()
    expect(capacityStore.getState().updated).toBe(true)
    configureCapacity({ fetch: () => reply(), now: () => now, build: () => ({ ...BUILD, channel: 'local', release: 'local-nogit' }), setTimer, clearTimer })
    await refreshCapacity()
    expect(capacityStore.getState().updated).toBe(false)
    configureCapacity({ fetch: () => reply(), now: () => now, build: () => null, setTimer, clearTimer })
    await refreshCapacity()
    expect(capacityStore.getState().updated).toBe(false)
  })
})

describe('client and helpers', () => {
  it('GET /api/sandbox/v1/capacity without a token', async () => {
    const seen: { url: string; auth: string | null; ident: string | null }[] = []
    setSandboxFetch(async (url, init) => {
      const h = new Headers(init?.headers)
      seen.push({ url, auth: h.get('authorization'), ident: h.get('x-awr-identity') })
      return new Response(JSON.stringify(cap()), { status: 200, headers: { 'content-type': 'application/json' } })
    })
    const d = await getCapacity()
    expect(d.schema).toBe('awr.capacity.v1')
    expect(seen).toEqual([{ url: '/api/sandbox/v1/capacity', auth: null, ident: null }])
  })

  it('eta buckets and queue hints (AWR-04 §15.6.5)', () => {
    expect(etaBucket(null)).toEqual({ kind: 'unknown' })
    expect(etaBucket(30)).toEqual({ kind: 'lt1min' })
    expect(etaBucket(360)).toEqual({ kind: 'min', n: 6 })
    expect(etaBucket(7200)).toEqual({ kind: 'h', n: 2 })
    expect(queueNeeded(cap())).toEqual({ viewer: false, sandbox: true })
    expect(queueNeeded(cap({ viewers: { live: 24, max: 24, queue: 2, eta_s: 120, state: 'full' } })).viewer).toBe(true)
    expect(queueNeeded(undefined)).toEqual({ viewer: false, sandbox: false })
  })
})
