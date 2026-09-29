// M05-AC-035: DtmSampler against the Python reference (python/awr/world/geometry/grids.py Grid.bilinear, the same raster)
// on 10k random points (difference <= 1e-3 m), clamping outside the grid, ground.zM before the load, and the per-sample
// cost; the flight60 generator and the M04 query service read the same cell-centre bilinear rule.
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { DtmSampler } from '@/engine/pointcloud/io/dtm'
import type { GridSidecar } from '@/engine/pointcloud/types'
import { WORLDS, haveWorlds, rng } from './helpers'

const PY = new URL('../../../../.venv/bin/python', import.meta.url).pathname
const havePy = existsSync(PY)
const REPO = new URL('../../../../', import.meta.url).pathname

function load(city: string): { s: DtmSampler; sc: GridSidecar } {
  const scUrl = new URL(`${city}/geometry/terrain/dtm_10m.json`, WORLDS)
  const sc = JSON.parse(readFileSync(scUrl, 'utf8')) as GridSidecar
  const raw = readFileSync(new URL(sc.href, scUrl))
  const s = new DtmSampler()
  s.set(sc, new Float32Array(raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength)), 0)
  return { s, sc }
}

describe.skipIf(!haveWorlds)('DtmSampler (M05-AC-035)', { timeout: 120_000 }, () => {
  it.skipIf(!havePy).each(['shenzhen', 'sanfrancisco', 'suzhou'])('%s: 10k points agree with the Python reference within 1e-3 m', (city) => {
    const { s, sc } = load(city)
    const r = rng(city.length * 977)
    const W = sc.width * sc.cellM
    const H = sc.height * sc.cellM
    const pts: number[] = []
    for (let i = 0; i < 10_000; i++) pts.push(sc.originXY[0] - 50 + r() * (W + 100), sc.originXY[1] - 50 + r() * (H + 100))
    const code = [
      'import json, sys', 'import numpy as np', 'from pathlib import Path', 'from awr.world.geometry.grids import Grid',
      'p = np.array(json.loads(sys.stdin.read()), dtype=np.float64).reshape(-1, 2)',
      `g = Grid.open(Path(${JSON.stringify(new URL(`${city}/geometry/terrain/dtm_10m.json`, WORLDS).pathname)}), expect_kind="dtm")`,
      'print(json.dumps(g._bilinear_ref(p[:, 0], p[:, 1]).tolist()))',
    ].join('\n')
    const out = execFileSync(PY, ['-c', code], { input: JSON.stringify(pts), cwd: REPO, env: { ...process.env, PYTHONPATH: `${REPO}python` }, maxBuffer: 64 << 20 })
    const ref = JSON.parse(out.toString()) as number[]
    let worst = 0
    for (let i = 0; i < ref.length; i++) worst = Math.max(worst, Math.abs(s.sample(pts[2 * i], pts[2 * i + 1]) - ref[i]))
    expect(worst).toBeLessThanOrEqual(1e-3)
  })
  it('clamps outside the grid, returns ground.zM before the load, and samples in about a microsecond', () => {
    const { s, sc } = load('shenzhen')
    const x0 = sc.originXY[0]
    const y0 = sc.originXY[1]
    expect(s.sample(x0 - 1000, y0 - 1000)).toBe(s.sample(x0, y0))
    const empty = new DtmSampler()
    empty.reset(12.5)
    expect(empty.sample(0, 0)).toBe(12.5)
    const t0 = performance.now()
    let acc = 0
    for (let i = 0; i < 200_000; i++) acc += s.sample(x0 + (i % 1800), y0 + (i % 1900))
    const us = ((performance.now() - t0) * 1000) / 200_000
    expect(Number.isFinite(acc)).toBe(true)
    expect(us).toBeLessThan(5) // functional bound; the 1 us target is a perf-lock measurement
  })
  it('texture stores dtm - ground.zM (R32F, row 0 south)', () => {
    const { sc } = load('shenzhen')
    const scUrl = new URL('shenzhen/geometry/terrain/dtm_10m.json', WORLDS)
    const raw = readFileSync(new URL(sc.href, scUrl))
    const d = new Float32Array(raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength))
    const s = new DtmSampler()
    s.set(sc, d, 3)
    const tex = s.texture!
    expect([tex.image.width, tex.image.height]).toEqual([sc.width, sc.height])
    expect((tex.image.data as Float32Array)[5]).toBeCloseTo(d[5] - 3, 5)
  })
})
