// M05-AC-030 (TS part, PERF-AC-061): sampleFlight60 reproduces the Python generator's table frame by frame (row k at
// t = k / 60 exactly, linear interpolation between rows, false at t >= 60 s); the companion json validates against
// awr.flight60.v1 with Ajv and its bin_sha256 matches the .bin.
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import Ajv2020 from 'ajv/dist/2020.js'
import { FLIGHT60_BYTES, checkFlight60, sampleFlight60 } from '@/engine/pointcloud/bench/flight60'

const DIR = new URL('../../public/bench/flight60/', import.meta.url)
const CONTRACTS = new URL('../../../../packages/contracts/', import.meta.url)
const cities = existsSync(DIR) ? readdirSync(DIR).filter((f) => f.endsWith('.bin')).map((f) => f.slice(0, -4)) : []

function schemaValidator() {
  const ajv = new Ajv2020({ strict: false, allErrors: true })
  const walk = (u: URL): void => {
    for (const e of readdirSync(u, { withFileTypes: true })) {
      if (e.isDirectory()) walk(new URL(`${e.name}/`, u))
      else if (e.name.endsWith('.schema.json')) {
        const s = JSON.parse(readFileSync(new URL(e.name, u), 'utf8'))
        if (s.$id && !ajv.getSchema(s.$id)) ajv.addSchema(s)
      }
    }
  }
  walk(CONTRACTS)
  return ajv.getSchema('https://schemas.anet-drone.dev/perf/1/flight60.schema.json')!
}

describe.skipIf(!cities.length)('sampleFlight60 against the generator (M05-AC-030)', () => {
  const validate = schemaValidator()
  it.each(cities)('%s', (city) => {
    const raw = readFileSync(new URL(`${city}.bin`, DIR))
    const doc = JSON.parse(readFileSync(new URL(`${city}.json`, DIR), 'utf8'))
    expect(raw.byteLength).toBe(FLIGHT60_BYTES)
    expect(validate(doc), JSON.stringify(validate.errors)).toBe(true)
    expect(createHash('sha256').update(raw).digest('hex')).toBe(doc.bin_sha256)
    const ab = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength) as ArrayBuffer
    expect(checkFlight60(ab, doc)).toBeNull()
    const fl = new Float32Array(ab)
    const eye = new Float64Array(3)
    const tgt = new Float64Array(3)
    for (let k = 0; k < 3600; k++) {
      expect(sampleFlight60(fl, k / 60, eye, tgt)).toBe(true)
      for (let a = 0; a < 3; a++) {
        if (Math.abs(eye[a] - fl[6 * k + a]) > 1e-9 || Math.abs(tgt[a] - fl[6 * k + 3 + a]) > 1e-9) throw new Error(`${city} row ${k} differs`)
      }
    }
    sampleFlight60(fl, (10 + 0.5) / 60, eye, tgt)
    expect(eye[0]).toBeCloseTo((fl[60] + fl[66]) / 2, 9)
    expect(sampleFlight60(fl, 60, eye, tgt)).toBe(false)
    expect(eye[0]).toBe(fl[6 * 3600])
  })
})
