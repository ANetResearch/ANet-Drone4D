// 扩展点 15（AWR-05 §12.3 第 15 处；D3-AC-01 demo-parity）：演示构建从契约原因码表剔除 D3 新增的码（vite.config.ts stripD3Reasons），
// 使 rt.worker 与 ui 两个 chunk 与 D2 标签构建逐字节一致；逐字节对拍本身由 tools/bench/d3/scan_public.py d2-parity 执行。

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { CONTRACT_REASONS_RE, D3_REASON_CODES, D3_WS_CLOSE_CODES, stripD3Reasons } from '../../vite.config'

const REPO = resolve(import.meta.dirname, '../../../..')
const GEN = resolve(REPO, 'packages/contracts/gen/ts/reasons.ts')

interface ReasonRow { code: number; name: string; d1: string }
interface ReasonsJson { reasons: ReasonRow[]; ws_close_codes: { code: number; name: string }[] }

const inRange = (n: number, [lo, hi]: readonly [number, number]): boolean => n >= lo && n <= hi

describe('D3 reason codes in the demo build (extension point 15)', () => {
  const src = readFileSync(GEN, 'utf8')
  const json = JSON.parse(readFileSync(resolve(REPO, 'packages/contracts/rt/reasons.json'), 'utf8')) as ReasonsJson
  const d3 = json.reasons.filter((r) => inRange(r.code, D3_REASON_CODES))
  const d2 = json.reasons.filter((r) => !inRange(r.code, D3_REASON_CODES))

  it('matches the generated module path only', () => {
    expect(CONTRACT_REASONS_RE.test('/x/packages/contracts/gen/ts/reasons.ts')).toBe(true)
    expect(CONTRACT_REASONS_RE.test('C:\\x\\packages\\contracts\\gen\\ts\\reasons.ts')).toBe(true)
    expect(CONTRACT_REASONS_RE.test('/x/apps/web/src/app/i18n/reasons.gen.ts')).toBe(false)
  })

  it('the D3 range holds exactly the codes reasons.json marks d1 = V0.3', () => {
    expect(d3.length).toBeGreaterThan(0)
    expect(d3.every((r) => r.d1 === 'V0.3')).toBe(true)
    expect(d2.some((r) => r.d1 === 'V0.3')).toBe(false)
  })

  it('drops every D3 reason code and D3 close code, and keeps every D2 line in order', () => {
    const out = stripD3Reasons(src)
    expect(out).not.toBeNull()
    const text = out ?? ''
    for (const r of d3) {
      expect(text).not.toContain(`  ${r.name}: ${r.code},\n`)
      expect(text).not.toContain(`  ${r.code}: { code: ${r.code},`)
    }
    for (const r of d2) {
      expect(text).toContain(`  ${r.name}: ${r.code},\n`)
      expect(text).toContain(`  ${r.code}: { code: ${r.code}, name: "${r.name}",`)
    }
    // 只删行：剩余行是原文行的有序子序列，删去的行数 = 每个 D3 码两行（Reason 与 REASONS）
    const a = src.split('\n')
    const b = text.split('\n')
    expect(a.length - b.length).toBe(2 * d3.length)
    let i = 0
    for (const line of b) {
      while (i < a.length && a[i] !== line && !line.startsWith('export const WS_CLOSE')) i++
      expect(i).toBeLessThan(a.length)
      i++
    }
    const ws = /^export const WS_CLOSE = \{ ([^}\n]*) \} as const$/m.exec(text)?.[1] ?? ''
    const kept = ws.split(', ').map((e) => Number(e.split(': ')[1]))
    expect(kept).toEqual(json.ws_close_codes.map((w) => w.code).filter((c) => !inRange(c, D3_WS_CLOSE_CODES)))
  })

  it('is idempotent and leaves other modules alone', () => {
    const once = stripD3Reasons(src) ?? src
    expect(stripD3Reasons(once)).toBeNull()
    expect(stripD3Reasons("export const X = {\n  A: 1,\n} as const\n")).toBeNull()
    expect(stripD3Reasons('export const WS_CLOSE = { NORMAL: 1000, RUN_ENDED: 4460 } as const\n')).toBe(
      'export const WS_CLOSE = { NORMAL: 1000 } as const\n')
  })
})
