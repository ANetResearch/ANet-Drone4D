// Environment Low visuals on SwiftShader through the production backend (render-target read-back; M07-AC-016, AC-019,
// AC-020, AC-023 functional parts). The perf and fixed-time RT comparisons of the whole page live in apps/web/perf/m07.
//   - every environment material compiles and draws (rain quads, snow and dust points, arrows, 2D clouds, fog node);
//   - the draw count equals EnvironmentRuntime.drawCount() and Tier S live counts follow floor(cap rain_k);
//   - switching presets, quality levels and sub-layers after the warm-up adds no program (D1-AC-19, D1-AC-25);
//   - GPU windAtEnu (L0/L1 profile + DTM + 4 fronts + turbulence texture3D) vs windCPU: <= 0.01 vmax + 0.02 m/s (D1-AC-13);
//   - fog: a quad at a known distance shows c T + fog (1 - T) within 2/255 of the CPU value.
import { describe, expect, it } from 'vitest'
import {
  DataTexture, FloatType, Group, Mesh, NearestFilter, PerspectiveCamera, PlaneGeometry, RedFormat, RGBAFormat, Scene, Vector3, WebGLRenderTarget,
} from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, float, normalWorld, positionGeometry, positionWorld, texture, uv, vec3, vec4 } from 'three/tsl'
import { createRenderBackend, type RenderBackend } from '@/viewport/renderer'
import {
  createEnvShading, EnvironmentRuntime, PRESETS_MODEL, decodeAWRV, derive, gustCreate, makeEnvNodes, newDerived, opticalDepth, TurbBoxCPU, volumeTexture3D, windAtEnu, windCPU,
  type EnvKeyframeWire,
} from '@/engine/environment'
import { makeDtmNodes } from '@/engine/environment/terrain/dtmSampler'
import type { FrameCtx } from '@/engine'
import { gustCreate as gc } from '@/engine/environment/wind/gust'

const SEC = 1e9

function frame(preset: string, version: number, tNs: number, events: number[][] = []): EnvKeyframeWire {
  const to = Array.from(PRESETS_MODEL.presetVector(preset))
  return {
    schema: 'awr.env.keyframe.v1', world_id: 't', version, epoch: 1, seed: 7, t_ns: tNs, t_apply_ns: tNs,
    config: { wind: { level: 1, profile: { kind: 'log', z_ref_m: 10, z0_m: 0.5, d_m: 0, alpha: 0.25, adv_height_m: 40 }, library: null,
      turbulence: { model: 'box', n: 64, dx_m: 4, l_m: 30 }, gust: { model: 'cos1_front', max_active: 4 } },
    sun: { azimuth_deg: 150, elevation_deg: 50 }, weather_map: { n: 512, scale_m: 24000 }, presets_sha256: PRESETS_MODEL.sha256 },
    mode: 'step', t0_ns: tNs, t1_ns: tNs, from: to, to, via: [], to_preset: preset,
    anchors: { t_ns: tNs, s_m: 1234.5, d_enu_m: [900.25, -300.5, 0], fall_rain_m: 777.7, fall_snow_m: 55.5, wetness: 0.3, puddle: 0.1 }, events,
    vis: { streamlines: null, vmax_mps: 20 },
  }
}

function ctxOf(be: RenderBackend, cam: PerspectiveCamera, tS: number, nowMs: number, W: number, H: number): FrameCtx {
  return { frameNo: 1, nowMs, dtMs: 16, tRenderS: tS, tFocusS: tS, simRate: 1, clockState: 1, camera: cam, cssW: W, cssH: H, dbW: W, dbH: H, dpr: 1,
    cloudScale: 1, tier: be.tier, deviceClass: be.deviceClass, capsIdx: 0, moving: false, frozen: false, compiledThisFrame: false, benchLayers: false, be }
}

function camera(W: number, H: number): PerspectiveCamera {
  const c = new PerspectiveCamera(60, W / H, 0.5, 20000)
  // three frame: ENU (E, U, -N); eye 30 m up looking north-east, slightly up (sky visible)
  c.position.set(0, 30, 0)
  c.lookAt(new Vector3(200, 60, -200))
  c.updateMatrixWorld()
  return c
}

async function renderTo(be: RenderBackend, s: Scene, cam: PerspectiveCamera, W: number, H: number): Promise<{ buf: Uint8Array; calls: number }> {
  const rt = be.createRT('selftest', { width: W, height: H }) as WebGLRenderTarget
  const r = be.renderer
  r.setRenderTarget(rt)
  r.setClearColor(0x000000, 1)
  r.clear()
  r.info.reset()
  r.render(s, cam)
  const calls = r.info.render.calls
  r.setRenderTarget(null)
  const buf = new Uint8Array(W * H * 4)
  await be.readPixels(rt, 0, 0, W, H, buf)
  rt.dispose()
  return { buf, calls }
}

describe('environment visuals (Tier S, SwiftShader)', () => {
  it('materials compile, draw counts match, no new programs after warm-up', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: { tier: 'S' } })
    const W = 96
    const H = 64
    const env = new EnvironmentRuntime({ be, reversedZ: be.caps.reversedZ, fetchBytes: () => Promise.reject(new Error('offline')) })
    const scene = new Scene() as Scene & { fogNode?: unknown }
    scene.fogNode = env.fogNode
    const worldRoot = new Group()
    worldRoot.rotation.x = -Math.PI / 2
    worldRoot.add(env.root)
    scene.add(worldRoot)
    // an opaque fogged plane (ground) so the scene fog node is compiled too
    const gm = new MeshBasicNodeMaterial()
    gm.colorNode = vec3(0.3, 0.3, 0.3) as never
    const ground = new Mesh(new PlaneGeometry(4000, 4000), gm)
    worldRoot.add(ground)
    const cam = camera(W, H)
    env.sub.arrows = true
    const ev = gc(1, 0, 6, 60, 270, 1234.5, 14, [-900, -900], [900, 900], 1.463)
    env.store.ingest(frame('thunderstorm', 1, 10 * SEC, [[1, 1, 0, ev.x0, ev.s0, 6, 120, 270, ev.sSpan]]), 0)
    env.update(ctxOf(be, cam, 10.5, 16, W, H), [100, 100, 0], false)
    // warm-up as the shader zoo does it: every object visible and drawable once
    for (const it of env.warmupObjects()) {
      it.before()
      it.object.visible = true
    }
    await renderTo(be, scene, cam, W, H)
    env.update(ctxOf(be, cam, 10.6, 32, W, H), [100, 100, 0], false)
    const p0 = be.programsCount()
    const r1 = await renderTo(be, scene, cam, W, H)
    expect(env.perf.live.rain).toBe(Math.floor(1424 * env.store.derived.rain_k))
    expect(r1.calls).toBe(env.drawCount() + 1)
    // preset, quality and sub-layer switches only change uniforms, drawRange and visibility
    env.store.ingest(frame('snow', 2, 10.7 * SEC), 40)
    env.update(ctxOf(be, cam, 10.8, 48, W, H), [100, 100, 0], false)
    await renderTo(be, scene, cam, W, H)
    env.quality.knob.apply(1)
    env.update(ctxOf(be, cam, 10.9, 64, W, H), [100, 100, 0], false)
    expect(env.drawCount()).toBe(0)
    await renderTo(be, scene, cam, W, H)
    env.quality.knob.apply(0)
    env.sub.arrows = false
    env.store.ingest(frame('sandstorm', 3, 11 * SEC), 70)
    env.update(ctxOf(be, cam, 11.1, 80, W, H), [100, 100, 0], false)
    await renderTo(be, scene, cam, W, H)
    expect(be.programsCount()).toBe(p0)
    env.dispose()
  })

  it('GPU windAtEnu matches windCPU (L0/L1 profile + DTM + 4 fronts + turbulence box)', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: { tier: 'S' } })
    const env = new EnvironmentRuntime({ be, reversedZ: be.caps.reversedZ, fetchBytes: () => Promise.reject(new Error('offline')) })
    // synthetic DTM (40 x 30, 10 m) and a small turbulence box built like the shared asset
    const dw = 40
    const dh = 30
    const dtm = new Float32Array(dw * dh)
    for (let j = 0; j < dh; j++) for (let i = 0; i < dw; i++) dtm[j * dw + i] = 5 * Math.sin(i / 5) + 3 * Math.cos(j / 4)
    const rel = new Float32Array(dtm.length)
    for (let i = 0; i < rel.length; i++) rel[i] = dtm[i] - 2
    const tex = new DataTexture(rel, dw, dh, RedFormat, FloatType)
    tex.minFilter = NearestFilter
    tex.magFilter = NearestFilter
    tex.needsUpdate = true
    const src = {
      loaded: true, groundZ: 2, texture: tex, grid: { width: dw, height: dh, cellM: 10, originX: -200, originY: -150 },
      sample(x: number, y: number): number {
        const gx = Math.min(Math.max((x + 200) / 10 - 0.5, 0), dw - 1)
        const gy = Math.min(Math.max((y + 150) / 10 - 0.5, 0), dh - 1)
        const c0 = Math.min(Math.floor(gx), dw - 2)
        const r0 = Math.min(Math.floor(gy), dh - 2)
        const tx = gx - c0
        const ty = gy - r0
        const i = r0 * dw + c0
        return (dtm[i] * (1 - tx) + dtm[i + 1] * tx) * (1 - ty) + (dtm[i + dw] * (1 - tx) + dtm[i + dw + 1] * tx) * ty
      },
    }
    env.setDtmSource(src, 2)
    const box = makeBox()
    const cpuBox = new TurbBoxCPU(box)
    const tex3 = volumeTexture3D(box)
    // thunderstorm with 4 active fronts crossing the grid
    const fa = 1.463
    const S = 5000
    const evs = [270, 300, 200, 90].map((dir, k) => gustCreate(k + 1, 0, 8 - k, 60, dir, S - 60 * k, 14, [-300, -300], [300, 300], fa))
    const w = frame('thunderstorm', 1, 10 * SEC, evs.map((e) => [1, e.id, 0, e.x0, e.s0, e.amp, e.lam, e.dirFromDeg, e.sSpan]))
    w.anchors.s_m = S + 380
    env.store.ingest(w, 0)
    const cam = camera(32, 32)
    env.update(ctxOf(be, cam, 10.0, 16, 32, 32), [0, 0, 0], false)
    env.params.turbOn = 1
    // CPU reference at 32 x 32 points
    const N = 32
    const pts = new Float32Array(N * N * 4)
    const cpu = new Float64Array(N * N * 3)
    const o = new Float64Array(4)
    for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) {
      const k = j * N + i
      const x = -150 + i * 9.7
      const y = -120 + j * 7.9
      const z = src.sample(x, y) + 3 + ((i * 7 + j * 3) % 60)
      pts.set([x, y, z, 1], 4 * k)
      windCPU(x, y, z, env.store, { turb: true, box: cpuBox, terrain: env.terrain }, o)
      cpu.set([o[0], o[1], o[2]], 3 * k)
    }
    const ptex = new DataTexture(pts, N, N, RGBAFormat, FloatType)
    ptex.minFilter = NearestFilter
    ptex.magFilter = NearestFilter
    ptex.needsUpdate = true
    env.terrain.sync()
    const n = makeEnvNodes(env.params)
    const dn = makeDtmNodes(env.terrain)
    const m = new MeshBasicNodeMaterial()
    m.vertexNode = vec4((positionGeometry as never as { xy: unknown }).xy as never, 0, 1) as never
    m.colorNode = Fn(() => {
      const p = texture(ptex, uv() as never) as never as { xyz: unknown }
      // + 64 m/s: the colour output clamps negative values
      const w = windAtEnu(n, p.xyz, { turb: true, turbTex: tex3, dtm: dn, dtmTex: dn.tex }) as { add(x: number): never }
      return vec4(w.add(64), float(1))
    })() as never
    const q = new Mesh(new PlaneGeometry(2, 2), m)
    q.frustumCulled = false
    const s = new Scene()
    s.add(q)
    const rt = new WebGLRenderTarget(N, N, { type: FloatType, depthBuffer: false })
    const r = be.renderer
    r.setRenderTarget(rt)
    r.render(s, cam)
    r.setRenderTarget(null)
    const out = new Float32Array(N * N * 4)
    await r.readRenderTargetPixelsAsync(rt, 0, 0, N, N, out)
    const vmax = derive(env.store.scalars, newDerived()).vmax_vis_mps
    let worst = 0
    let wk = 0
    for (let k = 0; k < N * N; k++) for (let c = 0; c < 3; c++) {
      const e = Math.abs(out[4 * k + c] - 64 - cpu[3 * k + c])
      if (e > worst) {
        worst = e
        wk = k
      }
    }
    if (worst > 0.01 * vmax + 0.02) console.log('worst', worst, 'k', wk, 'pt', Array.from(pts.slice(4 * wk, 4 * wk + 3)), 'gpu', Array.from(out.slice(4 * wk, 4 * wk + 3)), 'cpu', Array.from(cpu.slice(3 * wk, 3 * wk + 3)),
      'k0 gpu', Array.from(out.slice(0, 3)), 'cpu', Array.from(cpu.slice(0, 3)))
    expect(worst).toBeLessThanOrEqual(0.01 * vmax + 0.02)
    // the fronts are actually present in the sample
    let gusty = 0
    for (let k = 0; k < N * N; k++) if (Math.hypot(cpu[3 * k], cpu[3 * k + 1]) > 25) gusty++
    expect(gusty).toBeGreaterThan(0)
    rt.dispose()
    env.dispose()
  })

  it('Tier B: streamlines compile and draw from an AWSL set (D1-ext)', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: { tier: 'B' } })
    const buf = makeAwsl()
    const env = new EnvironmentRuntime({ be, reversedZ: be.caps.reversedZ, fetchBytes: () => Promise.resolve(buf) })
    expect(env.streamlines).not.toBeNull()
    const scene = new Scene()
    const root = new Group()
    root.rotation.x = -Math.PI / 2
    root.add(env.root)
    scene.add(root)
    const cam = camera(64, 64)
    const w = frame('rain', 1, 0)
    w.vis.streamlines = '/api/env/streamlines/analytic-00000000'
    env.store.ingest(w, 0)
    env.sub.streamlines = true
    env.update(ctxOf(be, cam, 0.1, 0, 64, 64), [0, 0, 0], false)
    await new Promise((r) => setTimeout(r, 50))
    env.update(ctxOf(be, cam, 0.2, 1000, 64, 64), [0, 0, 0], false)
    env.update(ctxOf(be, cam, 0.3, 1200, 64, 64), [0, 0, 0], false)
    expect(env.streamlines!.drawCount()).toBeGreaterThan(0)
    const r = await renderTo(be, scene, cam, 64, 64)
    expect(r.calls).toBe(env.drawCount())
    env.dispose()
  })

  it('shading provider: lambert (clear, no cloud shadow) equals the 15 §10.3 term with wet darkening; sky compiles', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: { tier: 'S' } })
    const env = new EnvironmentRuntime({ be, reversedZ: be.caps.reversedZ, fetchBytes: () => Promise.reject(new Error('offline')) })
    const cam = new PerspectiveCamera(30, 1, 0.5, 5000)
    cam.position.set(0, 50, 0)
    cam.lookAt(0, 0, 0)
    cam.updateMatrixWorld()
    env.store.ingest(frame('clear', 1, 0), 0)
    env.update(ctxOf(be, cam, 0.5, 16, 16, 16), [0, 0, 0], false)
    const sh = createEnvShading(env.params, env.weather)
    const m = new MeshBasicNodeMaterial()
    m.colorNode = Fn(() => vec3(sh.lambert(normalWorld, positionWorld)).mul(0.5))() as never
    const floor = new Mesh(new PlaneGeometry(200, 200), m)
    floor.rotation.x = -Math.PI / 2 // normal up (three +Y = ENU up)
    const s = new Scene()
    s.add(floor)
    const sky = new MeshBasicNodeMaterial()
    sky.colorNode = Fn(() => sh.sky(vec3(0, 1, 0)))() as never
    const q = new Mesh(new PlaneGeometry(1, 1), sky)
    q.position.set(90, 0.5, 0)
    s.add(q)
    const { buf } = await renderTo(be, s, cam, 16, 16)
    const P = env.params
    const up = 1
    // the frame carries wetness 0.3: exposed surfaces darken by (1 - 0.325 wet) (M07-FR-049)
    const lam = (0.45 + 0.15 * (0.5 + 0.5 * up) + 0.55 * Math.max(P.sunDirEnu.z, 0) * P.sunVis) * (1 - 0.325 * P.wetness)
    const c = (8 * 16 + 8) * 4
    expect(Math.abs(buf[c] - lam * 0.5 * 255)).toBeLessThanOrEqual(2)
    env.dispose()
  })

  it('fog: c T + fog (1 - T) at a known distance within 2/255', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: { tier: 'S' } })
    const env = new EnvironmentRuntime({ be, reversedZ: be.caps.reversedZ, fetchBytes: () => Promise.reject(new Error('offline')) })
    const W = 16
    const cam = new PerspectiveCamera(30, 1, 0.5, 20000)
    cam.position.set(0, 20, 0) // ENU (0, 0, 20)
    cam.lookAt(0, 20, -400) // north, level
    cam.updateMatrixWorld()
    env.store.ingest(frame('heavyRain', 1, 0), 0)
    env.update(ctxOf(be, cam, 0.5, 16, W, W), [0, 400, 0], false)
    const scene = new Scene() as Scene & { fogNode?: unknown }
    scene.fogNode = env.fogNode
    const m = new MeshBasicNodeMaterial()
    m.colorNode = vec3(0.5, 0.5, 0.5) as never
    const wall = new Mesh(new PlaneGeometry(2000, 2000), m)
    wall.position.set(0, 20, -400) // 400 m north, facing the camera
    scene.add(wall)
    const { buf } = await renderTo(be, scene, cam, W, W)
    const d = derive(env.store.scalars, newDerived())
    const T = Math.exp(-opticalDepth(20 - env.params.groundZ, 0, 400, d, env.params.fogTop, env.params.precipTop))
    const fc = env.params.fogColor
    // render targets hold linear colour (AnetNodesHandler fix 1)
    const lin = [0.5 * T + fc.x * (1 - T), 0.5 * T + fc.y * (1 - T), 0.5 * T + fc.z * (1 - T)].map((v) => v * 255)
    const c = (W / 2) * W * 4 + (W / 2) * 4
    expect(T).toBeLessThan(0.8)
    for (let k = 0; k < 3; k++) expect(Math.abs(buf[c + k] - lin[k])).toBeLessThanOrEqual(2)
    env.dispose()
  })
})

/** 30 straight lines of 20 vertices (x, y, z, tau_hat, s_hat) in AWSL v1 */
function makeAwsl(): ArrayBuffer {
  const nl = 30
  const nv = 20
  const ob = 4 * (nl + 1)
  const pad = (8 - (ob % 8)) % 8
  const buf = new ArrayBuffer(48 + ob + pad + nl * nv * 20)
  const dv = new DataView(buf)
  dv.setUint32(0, 0x4c535741, true)
  dv.setUint16(4, 1, true)
  dv.setUint16(6, 3, true)
  dv.setUint32(8, nl, true)
  dv.setUint32(12, nl * nv, true)
  dv.setFloat32(16, 270, true)
  dv.setFloat32(20, 1, true)
  dv.setUint32(28, 20, true)
  for (let i = 0; i <= nl; i++) dv.setUint32(48 + 4 * i, i * nv, true)
  const v = new Float32Array(buf, 48 + ob + pad)
  for (let l = 0; l < nl; l++) for (let k = 0; k < nv; k++) v.set([-200 + 12 * k, -150 + 10 * l, 20 + l, 12 * k, 1.3], 5 * (l * nv + k))
  return buf
}

/** a deterministic 16^3 f16 box with 16 m cells (period 256 m like the shared asset) */
function makeBox() {
  const n = 16
  const u16 = new Uint16Array(n * n * n * 4)
  const f2h = (f: number): number => {
    const b = new DataView(new ArrayBuffer(4))
    b.setFloat32(0, f)
    const x = b.getUint32(0)
    const s = (x >>> 16) & 0x8000
    const e = ((x >>> 23) & 0xff) - 112
    const mm = (x >>> 13) & 0x3ff
    return e <= 0 ? s : e >= 31 ? s | 0x7c00 : s | (e << 10) | mm
  }
  for (let k = 0; k < n; k++) for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    const o = 4 * ((k * n + j) * n + i)
    u16[o] = f2h(Math.sin((2 * Math.PI * (i + 2 * j)) / n))
    u16[o + 1] = f2h(Math.cos((2 * Math.PI * (j + k)) / n))
    u16[o + 2] = f2h(0.5 * Math.sin((2 * Math.PI * (k - i)) / n))
  }
  const buf = new ArrayBuffer(64 + u16.byteLength)
  const dv = new DataView(buf)
  dv.setUint32(0, 0x56525741, true)
  dv.setUint16(4, 1, true)
  dv.setUint16(6, 2, true)
  dv.setUint16(8, n, true)
  dv.setUint16(10, n, true)
  dv.setUint16(12, n, true)
  dv.setUint16(14, 4, true)
  dv.setUint8(16, 1)
  dv.setFloat32(32, 16, true)
  dv.setFloat32(36, 16, true)
  dv.setFloat32(40, 16, true)
  dv.setUint32(56, u16.byteLength, true)
  new Uint8Array(buf, 64).set(new Uint8Array(u16.buffer))
  return decodeAWRV(buf, false)
}
