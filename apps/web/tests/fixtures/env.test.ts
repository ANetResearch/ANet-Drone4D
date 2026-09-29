// 测试环境冒烟（M00-A，unit 项目）：Node 版本与 engines 一致（AWR-11 §3.5、ADR-037）
import { describe, expect, it } from 'vitest'

describe('测试环境：Node', () => {
  it('Node 为 22.x 且不低于 22.12（Vitest 5 下限）', () => {
    const [major, minor] = process.versions.node.split('.').map(Number)
    expect(major).toBe(22)
    expect(minor).toBeGreaterThanOrEqual(12)
  })
})
