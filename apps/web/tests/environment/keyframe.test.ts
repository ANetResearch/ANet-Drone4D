// EnvKeyframe contract on the TS side (M07-AC-001): Python-encoded frames (tests/environment/fixtures/keyframes.json,
// 6 classes of M07 §6.2.5) pass Ajv strict against env_state.schema.json, decode, and re-encode byte-identically; frame
// sizes stay within the limits; asset URLs are derived the same way as the server (16 §8.4).
import { readFileSync } from 'node:fs'
import { decode, encode } from '@msgpack/msgpack'
import Ajv2020 from 'ajv/dist/2020.js'
import { describe, expect, it } from 'vitest'
import { canon, decodeKeyframe, turbUrl, weatherUrl, type EnvKeyframeWire } from '@/engine/environment/state/keyframe'

const ROOT = new URL('../../../../', import.meta.url)
const fixture = JSON.parse(readFileSync(new URL('tests/environment/fixtures/keyframes.json', ROOT), 'utf8')) as {
  cases: { name: string; limit: number; hex: string; bytes: number; version: number }[]
}
const schema = JSON.parse(readFileSync(new URL('packages/contracts/env/env_state.schema.json', ROOT), 'utf8')) as object
const ajv = new Ajv2020({ strict: true, allErrors: true, strictTuples: false, allowUnionTypes: true })
const validate = ajv.compile(schema)

const hex = (h: string): Uint8Array => Uint8Array.from(h.match(/../g)!.map((b) => parseInt(b, 16)))

describe('EnvKeyframe (M07-AC-001)', () => {
  for (const c of fixture.cases) {
    it(`${c.name}: schema, decode, byte-identical re-encode, size <= ${c.limit}`, () => {
      const b = hex(c.hex)
      expect(b.length).toBe(c.bytes)
      expect(b.length).toBeLessThanOrEqual(c.limit)
      const w = decode(b) as EnvKeyframeWire
      expect(validate(w), JSON.stringify(validate.errors)).toBe(true)
      const kf = decodeKeyframe(w)
      expect(kf.version).toBe(c.version)
      expect(kf.from.length).toBe(21)
      expect(kf.events.length).toBeLessThanOrEqual(4)
      expect(kf.route.length).toBe(kf.via.length + 2)
      const again = encode(canon(w)) // default encoder: the TS client's canonical encoding
      expect(Buffer.from(again).toString('hex')).toBe(c.hex)
    })
  }

  it('canonical numbers: -0 becomes 0', () => {
    expect(Object.is(canon(-0), 0)).toBe(true)
    expect(canon([1.5, -0])).toEqual([1.5, 0])
  })

  it('asset URLs derived from seed and parameters', () => {
    expect(turbUrl(7, 64, 4, 30)).toBe('/worlds/_shared/env/turb/vk_s7_n64_dx4_L30.awrv')
    expect(weatherUrl(123, 512)).toBe('/worlds/_shared/env/weather/weather_s123_512.awrv')
  })
})
