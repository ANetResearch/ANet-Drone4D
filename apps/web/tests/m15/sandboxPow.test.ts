// M15-FR-204 (AWR-17 §17.3.5; UX-FR-107): the sandbox shell solves the proof of work with M17's solver
// (net/sandbox/pow.ts, WP-12b). Cross-check of that solver against node:crypto (d2-WP-12-to-M17 item 3): the in-repo
// SHA-256 matches the FIPS vectors, leading zero bits are counted per bit, and the nonce is the smallest that satisfies
// the server's check SHA-256(prefix || nonce_ascii) >= bits leading zero bits.
import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { leadingZeroBits, solveChunk, solvePow } from '@/net/sandbox/pow'
import { sha256 } from '@/net/sandbox/sha256'

const hex = (b: Uint8Array) => Buffer.from(b).toString('hex')
const ref = (b: Uint8Array) => createHash('sha256').update(b).digest('hex')

describe('sha256 (M17)', () => {
  it('matches the FIPS vectors and node:crypto on several lengths', () => {
    expect(hex(sha256(new TextEncoder().encode('abc')))).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
    expect(hex(sha256(new Uint8Array(0)))).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
    for (const n of [1, 31, 55, 56, 63, 64, 65, 127, 200]) {
      const b = new Uint8Array(n).map((_, i) => (i * 37 + n) & 0xff)
      expect(hex(sha256(b))).toBe(ref(b))
    }
  })

  it('counts leading zero bits', () => {
    expect(leadingZeroBits(new Uint8Array([0, 0, 0x10, 1]))).toBe(19)
    expect(leadingZeroBits(new Uint8Array([0x80]))).toBe(0)
    expect(leadingZeroBits(new Uint8Array([0, 1]))).toBe(15)
  })
})

describe('solvePow (M17)', () => {
  const prefix = new Uint8Array(32).map((_, i) => (i * 13 + 7) & 0xff)
  const bits = 10
  const lz = (n: number) => leadingZeroBits(new Uint8Array(createHash('sha256').update(Buffer.concat([Buffer.from(prefix), Buffer.from(String(n))])).digest()))

  it('finds the smallest nonce meeting the difficulty (verified with node:crypto)', async () => {
    const sol = await solvePow({ challenge_id: 'c-1', alg: 'sha256-lz', prefix_b64: Buffer.from(prefix).toString('base64'), bits, expires_unix_ns: '0' }, { worker: false })
    expect(sol.challenge_id).toBe('c-1')
    expect(lz(sol.nonce)).toBeGreaterThanOrEqual(bits)
    for (let n = 0; n < sol.nonce; n++) expect(lz(n)).toBeLessThan(bits)
  })

  it('returns null from a chunk without a solution', () => {
    expect(solveChunk(new Uint8Array(32), 40, 0, 100)).toBeNull()
  })
})
