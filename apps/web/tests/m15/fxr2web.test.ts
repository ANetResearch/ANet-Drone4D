// D2-FX-R2-web (docs/impl/D2-D2-FX-R2-web-报告.md): the pure parts of the round-2 web fixes.
//   D2-AC-01  shared programs for identical node-material sources (vite.config.ts patchThreePrograms) and canonical buffer
//             block names (viewport/anetNodesHandler.ts canonicalBufferNames);
//   D2-AC-41  the early boot module inlined before every stylesheet (insertEarlyInline, bootCapacityDict, app/boot/early.ts)
//             and the capacity texts without the i18n runtime (app/boot/capacityText.ts);
//   D2-AC-44  the power-save freeze of the periodic UI work (lib/powerFreeze.ts);
//   D2-AC-21  the clamp state from the soft fence's events and the earlier of two predictions (ui/manual/controller.ts).
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { bootCapacityDict, insertEarlyInline, patchThreePrograms } from '../../vite.config.ts'
import { canonicalBufferNames } from '@/viewport/anetNodesHandler'
import { capacityTexts } from '@/app/boot/capacityText'
import { capacityLine } from '@/app/i18n/capacity'
import { formatMessage } from '@/app/i18n/plural'
import { freezableInterval, onPowerFrozen, powerFrozen, setPowerFrozen } from '@/lib/powerFreeze'
import { earliestApproach, isClampEvent } from '@/ui/manual/controller'
import type { CapacityView } from '@/net/sandbox/client'

const WEB = resolve(import.meta.dirname, '../..')
const ROOT = resolve(WEB, '../..')

describe('shared programs (D2-AC-01)', () => {
  it('patches the acquireProgram of the installed three exactly once and keys raw shaders on their inputs', () => {
    const src = readFileSync(resolve(ROOT, 'node_modules/three/build/three.module.js'), 'utf8')
    const out = patchThreePrograms(src)
    expect(out.match(/function awrSourceKey\(/g)?.length).toBe(1)
    expect(out).toContain('cacheKey = awrSourceKey( parameters, cacheKey );')
    expect(() => patchThreePrograms('function nothing() {}')).toThrow(/acquireProgram/)
    expect(() => patchThreePrograms(out.replace('function generateDefines( defines )', 'function x( defines )'))).toThrow()
  })

  it('renames buffer blocks by order of appearance so identical instanced programs share their source', () => {
    const vs = (id: number) => `layout( std140 ) uniform render {\n\tmat4 nodeUniform0;\n};\nuniform NodeBuffer_${id} {\n\tmat4 buffer${id}[32];\n};\n`
      + `void main() { gl_Position = buffer${id}[ uint( gl_InstanceID ) ] * vec4( 1.0 ); float bufferish = 0.0; }\n`
    const fs = 'void main() {}\n'
    const a = canonicalBufferNames(vs(6825), fs)
    const b = canonicalBufferNames(vs(7263), fs)
    expect(a).not.toBeNull()
    expect(a!.vs).toBe(b!.vs)
    expect(a!.vs).toContain('uniform NodeBuffer_awr0 {')
    expect(a!.vs).toContain('mat4 awrBuf0[32];')
    expect(a!.vs).toContain('awrBuf0[ uint( gl_InstanceID ) ]')
    expect(a!.vs).toContain('float bufferish') // only the identifiers of the buffer nodes are renamed
    expect([...a!.groups]).toEqual([['NodeBuffer_6825', 'NodeBuffer_awr0']])
    // a canonical name is not taken for a node id again (a rebuild of the renamed source keeps it)
    expect(canonicalBufferNames(a!.vs, fs)).toBeNull()
    expect(a!.sig).toBe('6825')
    expect(canonicalBufferNames('void main() {}', fs)).toBeNull()
  })
})

describe('early boot capacity line (D2-AC-41)', () => {
  it('inlines the early script right after the charset meta, before every stylesheet and module script', () => {
    const html = readFileSync(resolve(WEB, 'index.html'), 'utf8')
    const out = insertEarlyInline(html, 'var x = "</script>";')
    const at = out.indexOf('<script data-awr-early="">')
    expect(at).toBeGreaterThan(out.indexOf('<meta charset="UTF-8" />'))
    expect(at).toBeLessThan(out.indexOf('<style>'))
    expect(at).toBeLessThan(out.indexOf('<script type="module"'))
    expect(out).toContain('var x = "<\\/script>";')
    expect(() => insertEarlyInline('<html></html>', '')).toThrow(/charset/)
  })

  it('carries exactly the capacity keys of zh-CN and formats like the i18n runtime', () => {
    const zh = JSON.parse(readFileSync(resolve(WEB, 'src/app/i18n/zh-CN.json'), 'utf8')) as Record<string, string>
    const dict = bootCapacityDict(zh)
    expect(Object.keys(dict).length).toBeGreaterThan(10)
    expect(Object.keys(dict).every((k) => k.startsWith('capacity.'))).toBe(true)
    const early = capacityTexts((k, p) => formatMessage(dict[k] ?? k, p, 'zh-CN'))
    const B = { version: '0.2.0', channel: 'ci', commit: 'abcdef0123', dirty: false, built_at: '2026-10-06T00:00:00Z', release: 'r' } as const
    const cap = (o: Partial<CapacityView> = {}): CapacityView => ({
      schema: 'awr.capacity.v1', t_unix_ns: '1', build: B, accepting: true, maintenance: false, degrade_level: 'L0',
      viewers: { live: 3, max: 24, queue: 0, eta_s: null, state: 'open' },
      sandbox: { active: 1, max: 1, reserved_internal: 1, parked: 0, queue: 2, eta_s: 1800, admit_now: false, worlds_admit_now: [], state: 'full' },
      power: { viewer_idle_s: 300, sandbox_idle_s: 600, hidden_s: 60, warn_s: 30 }, hint: 'queue_sandbox', ...o,
    })
    for (const d of [cap(), cap({ viewers: { live: 24, max: 24, queue: 1, eta_s: 90, state: 'full' } }), cap({ maintenance: true }), null]) {
      expect(early.capacityLine(d)).toEqual(capacityLine(d))
    }
  })

  it('the early module sends the request at once and writes the answer when the line exists (head of a parsing page)', async () => {
    const reqs: string[] = []
    vi.stubGlobal('fetch', async (u: string) => {
      reqs.push(u)
      return { ok: true, status: 200, json: async () => ({ schema: 'awr.capacity.v1', t_unix_ns: '1', build: {}, accepting: true, maintenance: false, degrade_level: 'L0',
        viewers: { live: 1, max: 24, queue: 0, eta_s: null, state: 'open' }, sandbox: { active: 0, max: 2, reserved_internal: 1, parked: 0, queue: 0, eta_s: null, admit_now: true,
          worlds_admit_now: [], state: 'open' }, power: { viewer_idle_s: 300, sandbox_idle_s: 600, hidden_s: 60, warn_s: 30 }, hint: 'open' }) }
    })
    const written: Record<string, string> = {}
    const line = { dataset: written, hidden: true, replaceChildren: () => {}, append: () => {} }
    const handlers: (() => void)[] = []
    let parsed = false
    const doc = {
      readyState: 'loading',
      documentElement: {},
      getElementById: (id: string) => (parsed && id === 'boot-mask' ? {} : null),
      querySelector: (s: string) => (parsed && s === '#boot-mask .boot-capacity' ? line : null),
      addEventListener: (t: string, cb: () => void) => { if (t === 'readystatechange') handlers.push(cb) },
      createElement: () => ({ className: '', textContent: '', dataset: {} }),
    }
    vi.stubGlobal('document', undefined) // no start at import (node has no page)
    const { startEarlyCapacity } = await import('@/app/boot/early')
    vi.stubGlobal('document', doc)
    vi.stubGlobal('MutationObserver', undefined)
    delete (globalThis as { __awrBootCapacity?: unknown }).__awrBootCapacity
    const h = startEarlyCapacity(doc as unknown as Document, '/world/synthcity')
    expect(h).not.toBeNull()
    expect(reqs).toEqual(['/api/sandbox/v1/capacity'])
    await h!.current
    await new Promise((r) => setTimeout(r, 0))
    expect(written.capacityHint).toBeUndefined() // the line is not parsed yet
    parsed = true
    doc.readyState = 'interactive'
    for (const cb of handlers) cb()
    expect(written.capacityHint).toBe('open')
    expect(line.hidden).toBe(false)
    h!.stop()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    delete (globalThis as { __awrBootCapacity?: unknown }).__awrBootCapacity
  })
})

describe('power-save freeze (D2-AC-44)', () => {
  afterEach(() => {
    setPowerFrozen(false)
    vi.useRealTimers()
  })
  it('a freezable interval stops while frozen and runs once at once on resume', () => {
    vi.useFakeTimers()
    let n = 0
    const seen: boolean[] = []
    const off = onPowerFrozen((f) => seen.push(f))
    const stop = freezableInterval(() => n++, 250)
    vi.advanceTimersByTime(1000)
    expect(n).toBe(4)
    setPowerFrozen(true)
    expect(powerFrozen()).toBe(true)
    vi.advanceTimersByTime(10_000)
    expect(n).toBe(4)
    setPowerFrozen(false)
    expect(n).toBe(5)
    vi.advanceTimersByTime(500)
    expect(n).toBe(7)
    stop()
    vi.advanceTimersByTime(1000)
    expect(n).toBe(7)
    off()
    expect(seen).toEqual([true, false])
  })
})

describe('manual control clamp and approach (D2-AC-21)', () => {
  it('only the soft-fence events of the controlled vehicle that clamped count', () => {
    expect(isClampEvent({ type: 'uav.keepout_guard', data: { uav: 'p600-01', clamped: true } }, 'p600-01')).toBe(true)
    expect(isClampEvent({ type: 'uav.keepout_guard', uav: 'p600-01', data: {} }, 'p600-01')).toBe(true)
    expect(isClampEvent({ type: 'uav.keepout_guard', data: { uav: 'p600-01', clamped: false } }, 'p600-01')).toBe(false)
    expect(isClampEvent({ type: 'uav.keepout_guard', data: { uav: 'p600-02', clamped: true } }, 'p600-01')).toBe(false)
    expect(isClampEvent({ type: 'target.discovered', data: { uav: 'p600-01' } }, 'p600-01')).toBe(false)
  })
  it('the earlier of the current-velocity and the commanded-velocity predictions wins', () => {
    const a = { target: 't1', kind: 'visual' as const, tS: 2.9, d: 10 }
    const b = { target: 't2', kind: 'acoustic' as const, tS: 1.4, d: 6 }
    expect(earliestApproach(a, b)).toBe(b)
    expect(earliestApproach(b, a)).toBe(b)
    expect(earliestApproach(null, a)).toBe(a)
    expect(earliestApproach(a, null)).toBe(a)
    expect(earliestApproach(null, null)).toBeNull()
  })
})
