// UboBinder and the uniform-block budget (FX-UBO, ADR-086; viewport/backend/uboBinder.ts), plus the two console clean-ups
// of the same work package: the warning-free R3F clock (viewport/r3fClock.ts, r3fThree.ts and the awr-r3f-clock plugin
// of vite.config.ts) and the FakeSource weather cycle used by perf/m06/gpu-limits.spec.ts.
import { decode as mpDecode } from '@msgpack/msgpack'
import { describe, expect, it, vi } from 'vitest'
import * as THREE from 'three'
import { Color, Matrix3, Matrix4, Vector2, Vector3, Vector4 } from 'three'
import {
  UboBinder, WEBGL2_MIN_LIMITS, blockNames, layoutGroup, overBudget, programBlocks, readUboLimits, std140, type GroupLike, type UniformLike,
} from '@/viewport/backend/uboBinder'
import { R3fClock } from '@/viewport/r3fClock'
import * as R3F_THREE from '@/viewport/r3fThree'
import { resolveRtUrl } from '@/net/rt/client'
import { FakeWorld, parseFakeUrl } from '@/net/rt/FakeSource'

const VS = `
layout( std140 ) uniform render {
\tmat4 nodeUniform0;
\tmat4 nodeUniform1;
};
layout( std140 ) uniform object {
\tmat4 nodeUniform2;
};
uniform NodeBuffer_7 {
\tmat4 buffer7[ 64 ];
};

uniform sampler2D nodeUniform3;
uniform vec3 plain;
`
const FS = `
layout( std140 ) uniform render {
\tmat4 nodeUniform0;
};
layout( std140 ) uniform frame {
\tfloat nodeUniform4;
};
`

describe('block budget from the generated GLSL', () => {
  it('names the std140 group blocks and the buffer blocks of a stage, not plain uniforms', () => {
    expect(blockNames(VS)).toEqual(['render', 'object', 'NodeBuffer_7'])
    expect(blockNames(FS)).toEqual(['render', 'frame'])
    expect(blockNames('uniform float a;\nuniform mat4 b;')).toEqual([])
  })
  it('counts per stage, combined and distinct blocks and checks them against the limits', () => {
    const b = programBlocks(VS, FS)
    expect(b).toEqual({ vertex: 3, fragment: 2, distinct: 4 })
    expect(overBudget(b, WEBGL2_MIN_LIMITS)).toBeNull()
    expect(overBudget(b, { ...WEBGL2_MIN_LIMITS, vertex: 2 })).toBe('vertex blocks 3 > 2')
    expect(overBudget(b, { ...WEBGL2_MIN_LIMITS, fragment: 1 })).toBe('fragment blocks 2 > 1')
    expect(overBudget(b, { ...WEBGL2_MIN_LIMITS, combined: 4 })).toBe('combined blocks 5 > 4')
    expect(overBudget(b, { ...WEBGL2_MIN_LIMITS, bindings: 3 })).toBe('distinct blocks 4 > binding points 3')
  })
  it('reads the device limits (WebGL2 minimum when a value is missing)', () => {
    const vals: Record<number, unknown> = { 0x8a2f: 24, 0x8a2b: 12, 0x8a2d: 16, 0x8a2e: null }
    const gl = { getParameter: (p: number) => vals[p] } as unknown as WebGL2RenderingContext
    expect(readUboLimits(gl)).toEqual({ bindings: 24, vertex: 12, fragment: 16, combined: 24 })
  })
})

describe('std140 layout (three WebGLUniformsGroups rules)', () => {
  it('aligns float, vec2, vec3, mat3, mat4 and typed arrays and pads the block to 16 bytes', () => {
    expect(std140(1)).toEqual({ align: 4, size: 4 })
    expect(std140(new Vector2())).toEqual({ align: 8, size: 8 })
    expect(std140(new Color())).toEqual({ align: 16, size: 12 })
    expect(std140(new Matrix3())).toEqual({ align: 16, size: 48 })
    const u = (value: unknown): UniformLike => ({ value })
    const lay = layoutGroup({ uniforms: [u(1), u(new Vector3()), u(2), u(new Vector2()), u(new Matrix3()), u(new Matrix4()), u(new Float32Array(8)), u(new Vector4()), u(3)] })
    expect(lay.slots.map((s) => s.off)).toEqual([0, 16, 28, 32, 48, 96, 160, 192, 208])
    expect(lay.size).toBe(224)
    expect(lay.unsupported).toBe(0)
  })
})

/** a WebGL2 stand-in that records the calls the binder makes */
function fakeGl() {
  const calls: string[] = []
  let nb = 0
  const gl = {
    DYNAMIC_DRAW: 0x88e8,
    getUniformBlockIndex: (_p: unknown, name: string) => ({ render: 0, object: 1, NodeBuffer_7: 2 } as Record<string, number>)[name] ?? 0xffffffff,
    uniformBlockBinding: (_p: unknown, bi: number, pt: number) => calls.push(`ubb ${bi}->${pt}`),
    getActiveUniformBlockParameter: () => 256,
    createBuffer: () => ({ id: ++nb }),
    deleteBuffer: (b: { id: number }) => calls.push(`del ${b.id}`),
    bindBuffer: () => {},
    bufferData: (_t: number, size: number) => calls.push(`data ${size}`),
    bufferSubData: (_t: number, off: number) => calls.push(`sub ${off}`),
    bindBufferBase: (_t: number, i: number, b: { id: number }) => calls.push(`base ${i}=${b.id}`),
  }
  return { gl: gl as unknown as WebGL2RenderingContext, calls }
}
function group(id: number, name: string, uniforms: UniformLike[]): GroupLike & { dispose(): void } {
  const ls: ((e: { target: GroupLike }) => void)[] = []
  const g = {
    id, name, uniforms,
    addEventListener: (_t: 'dispose', f: (e: { target: GroupLike }) => void) => ls.push(f),
    removeEventListener: (_t: 'dispose', f: (e: { target: GroupLike }) => void) => ls.splice(ls.indexOf(f), 1),
    dispose: () => [...ls].forEach((f) => f({ target: g })),
  }
  return g
}

describe('UboBinder (per-draw binding)', () => {
  it('binds block i to point i once per program, uploads changed values only, rebinds every draw, deletes on dispose', () => {
    const { gl, calls } = fakeGl()
    const b = new UboBinder(gl, WEBGL2_MIN_LIMITS)
    const m = new Matrix4()
    const f = { value: 0.5 }
    const render = group(1, 'render', [{ value: m }, f])
    const object = group(2, 'object', [{ value: new Vector3(1, 2, 3) }])
    const unused = group(3, 'frame', [{ value: 1 }])
    const prog = {} as WebGLProgram
    b.bind([render, object, unused], prog)
    expect(calls).toEqual(['ubb 0->0', 'data 256', 'sub 0', 'sub 64', 'base 0=1', 'ubb 1->1', 'data 256', 'sub 0', 'base 1=2'])
    calls.length = 0
    b.bind([render, object, unused], prog) // nothing changed: no upload, bindings again
    expect(calls).toEqual(['base 0=1', 'base 1=2'])
    calls.length = 0
    f.value = 0.75
    b.bind([render, object], prog)
    expect(calls).toEqual(['sub 64', 'base 0=1', 'base 1=2'])
    expect(b.stats).toMatchObject({ points: 2, groups: 2, created: 2, deleted: 0 })
    calls.length = 0
    render.dispose()
    expect(calls).toEqual(['del 1'])
    expect(b.stats).toMatchObject({ groups: 1, deleted: 1 })
  })
  it('records the largest block counts and the programs over budget', () => {
    const b = new UboBinder(fakeGl().gl, WEBGL2_MIN_LIMITS)
    expect(b.noteProgram({ vertex: 3, fragment: 2, distinct: 3 })).toBeNull()
    expect(b.noteProgram({ vertex: 13, fragment: 1, distinct: 13 })).toBe('vertex blocks 13 > 12')
    expect(b.stats).toMatchObject({ maxVertex: 13, maxFragment: 2, maxCombined: 14, maxDistinct: 13, violations: 1 })
  })
})

describe('R3F clock without the THREE.Clock deprecation warning', () => {
  it('has the fields and semantics of THREE.Clock', () => {
    let now = 1000
    const spy = vi.spyOn(performance, 'now').mockImplementation(() => now)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const ref = new THREE.Clock()
    expect(warn).toHaveBeenCalled()
    warn.mockClear()
    const c = new R3fClock()
    expect(warn).not.toHaveBeenCalled()
    const seq: [number, number][] = []
    for (const step of [0, 16, 17, 100, 0, 33]) {
      now += step
      seq.push([c.getDelta(), ref.getDelta()])
    }
    for (const [a, b] of seq) expect(a).toBeCloseTo(b, 12)
    now += 50
    expect(c.getElapsedTime()).toBeCloseTo(ref.getElapsedTime(), 12)
    c.stop()
    ref.stop()
    now += 500
    expect([c.getDelta(), c.running, c.autoStart]).toEqual([ref.getDelta(), ref.running, ref.autoStart])
    // R3F frameloop="never" writes the fields directly (advance(timestamp))
    c.oldTime = c.elapsedTime
    c.elapsedTime = 12.5
    expect(c.elapsedTime).toBe(12.5)
    spy.mockRestore()
    warn.mockRestore()
  })
  it('r3fThree is three with that clock; the vite plugin resolves only R3F imports of three to it', async () => {
    expect(R3F_THREE.Clock).toBe(R3fClock)
    expect(R3F_THREE.Vector3).toBe(THREE.Vector3)
    expect(R3F_THREE.WebGLRenderer).toBe(THREE.WebGLRenderer)
    const cfg = (await import('../../vite.config')).default as unknown as { plugins: { name?: string; resolveId?: (s: string, i?: string) => unknown }[] }
    const p = cfg.plugins.flat().find((x) => x?.name === 'awr-r3f-clock')!
    expect(String(p.resolveId!('three', '/x/node_modules/@react-three/fiber/dist/events-9ce18a08.esm.js'))).toMatch(/src\/viewport\/r3fThree\.ts$/)
    expect(p.resolveId!('three', '/x/src/viewport/r3fThree.ts')).toBeNull()
    expect(p.resolveId!('three', '/x/node_modules/@react-three/drei/index.js')).toBeNull()
    expect(p.resolveId!('three/webgpu', '/x/node_modules/@react-three/fiber/dist/index.js')).toBeNull()
  })
})

describe('FakeSource weather cycle (gpu-limits spec)', () => {
  it('maps fakeWorld and the fakeEnv* parameters into the fake URL', () => {
    const u = resolveRtUrl('ws://h/api/rt', '?source=fake&fakeN=6&fakeWorld=synthcity&fakeEnvPeriodS=3&fakeEnvPresets=all&fakeEnvStep=1&fakeEnvCycles=2')
    const o = parseFakeUrl(u)
    expect(o).toMatchObject({ n: 6, world: 'synthcity', envPeriodS: 3, envStep: true, envCycles: 2 })
    expect(o.envPresets).toHaveLength(12)
    expect(parseFakeUrl('fake:?envPresets=fog,snow').envPresets).toEqual(['fog', 'snow'])
    expect(parseFakeUrl('fake:?n=1').envPresets).toBeUndefined()
  })
  it('cycles the listed presets with step keyframes, envCycles times, then holds the first', () => {
    let now = 0
    const w = new FakeWorld({ n: 1, autoTick: false, now: () => now, envPeriodS: 1, envPresets: ['clear', 'fog', 'snow'], envStep: true, envCycles: 2 })
    const seen: { to: string; mode: string }[] = []
    const ch = (w as unknown as { envCh: { payload: Uint8Array | null; seq: number } }).envCh
    let seq = -1
    for (let k = 0; k < 60 * 10; k++) {
      now += 1000 / 60
      w.tick()
      if (ch.seq !== seq && ch.payload) {
        seq = ch.seq
        const kf = mpDecode(ch.payload) as { to_preset: string; mode: string; version: number }
        if (!seen.length || seen[seen.length - 1].to !== kf.to_preset) seen.push({ to: kf.to_preset, mode: kf.mode })
      }
    }
    // two passes, then the first preset holds (envCycles)
    expect(seen.map((s) => s.to)).toEqual(['clear', 'fog', 'snow', 'clear', 'fog', 'snow', 'clear'])
    expect(new Set(seen.map((s) => s.mode))).toEqual(new Set(['step']))
  })
})
