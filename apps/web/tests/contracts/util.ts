// Shared helpers for the contract tests (M00): file access relative to the repository root and golden comparisons.
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'

export const ROOT = new URL('../../../../', import.meta.url)
export const contractsPath = (rel: string): URL => new URL(`packages/contracts/${rel}`, ROOT)
export const readBytes = (rel: string): Uint8Array => new Uint8Array(readFileSync(contractsPath(rel)))
export const readJson = <T = unknown>(rel: string): T => JSON.parse(readFileSync(contractsPath(rel), 'utf8')) as T
export const sha256Hex = (b: Uint8Array | string): string => createHash('sha256').update(b).digest('hex')
export const hexToBytes = (h: string): Uint8Array => Uint8Array.from(h.match(/../g) ?? [], (x) => parseInt(x, 16))
export const bytesToHex = (b: Uint8Array): string => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')

/** Canonical JSON (sorted keys, no whitespace, ECMAScript number formatting), identical to contracts_lib.canonical_json. */
export function canonical(v: unknown): string {
  if (v === null || typeof v !== 'object') return JSON.stringify(v)
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']'
  const o = v as Record<string, unknown>
  return '{' + Object.keys(o).sort().map((k) => JSON.stringify(k) + ':' + canonical(o[k])).join(',') + '}'
}

/** Golden raw values: "NaN", "-0", "Infinity", "-Infinity" are spelled as strings in JSON. */
export function sameRaw(a: unknown, b: unknown): boolean {
  if (Array.isArray(b)) return Array.isArray(a) && a.length === b.length && b.every((x, i) => sameRaw(a[i], x))
  if (b === 'NaN') return typeof a === 'number' && Number.isNaN(a)
  if (b === '-0') return Object.is(a, -0)
  if (b === 'Infinity') return a === Infinity
  if (b === '-Infinity') return a === -Infinity
  return a === b
}

/** schemaName -> generated accessor stem: awr.rt.BatchHeader.v1 -> RtBatchHeader. */
export const stemOf = (schemaName: string): string =>
  schemaName.replace(/^awr\./, '').replace(/\.v\d+$/, '').split('.').map((s) => s[0].toUpperCase() + s.slice(1)).join('')
