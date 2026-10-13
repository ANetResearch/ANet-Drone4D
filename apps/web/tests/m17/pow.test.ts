// Proof of work kernel (M17-FR-060, AC-018 unit part): in-repo SHA-256 against node:crypto, leading-zero bits, a solved
// challenge verifies with the server rule (SHA-256(prefix || decimal nonce) >= bits), and solvePow without a Worker.
import { createHash, randomBytes } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { b64decode, leadingZeroBits, solveChunk, solvePow } from '@/net/sandbox/pow'
import { sha256, toHex } from '@/net/sandbox/sha256'

describe('sha256', () => {
  it('matches node:crypto for lengths around block boundaries', () => {
    for (const n of [0, 1, 3, 31, 32, 55, 56, 57, 63, 64, 65, 119, 120, 128, 200, 1000]) {
      const m = new Uint8Array(randomBytes(n))
      expect(toHex(sha256(m))).toBe(createHash('sha256').update(m).digest('hex'))
    }
    expect(toHex(sha256(new TextEncoder().encode('abc')))).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
  })
})

describe('pow', () => {
  it('leading zero bits', () => {
    expect(leadingZeroBits(new Uint8Array([0, 0, 1]))).toBe(23)
    expect(leadingZeroBits(new Uint8Array([0x80]))).toBe(0)
    expect(leadingZeroBits(new Uint8Array([0, 0x0f]))).toBe(12)
  })

  it('solution satisfies the server check', async () => {
    const prefix = new Uint8Array(randomBytes(32))
    const ch = { challenge_id: 'c-abcdefghij', alg: 'sha256-lz' as const, prefix_b64: Buffer.from(prefix).toString('base64'),
      bits: 10, expires_unix_ns: '0' }
    const sol = await solvePow(ch, { worker: false })
    expect(sol.challenge_id).toBe('c-abcdefghij')
    const h = createHash('sha256').update(Buffer.concat([Buffer.from(prefix), Buffer.from(String(sol.nonce))])).digest()
    expect(leadingZeroBits(new Uint8Array(h))).toBeGreaterThanOrEqual(10)
    expect(solveChunk(b64decode(ch.prefix_b64), 10, sol.nonce, 1)).toBe(sol.nonce)
    expect(solveChunk(prefix, 32, 0, 50)).toBeNull()
  })

  it('aborts', async () => {
    const ac = new AbortController()
    ac.abort()
    await expect(solvePow({ challenge_id: 'c-x', alg: 'sha256-lz', prefix_b64: 'AAAA', bits: 30, expires_unix_ns: '0' },
      { worker: false, signal: ac.signal })).rejects.toThrow()
  })
})
