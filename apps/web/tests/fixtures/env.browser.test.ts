// 测试环境冒烟（M00-A，browser 项目）：本机 Chrome for Testing 151 + SwiftShader（AWR-11 TECH-FR-006）
import { describe, expect, it } from 'vitest'

describe('测试环境：浏览器', () => {
  it('使用本机 Chrome 151', () => {
    expect(navigator.userAgent).toMatch(/Chrome\/151\./)
  })

  it('WebGL2 可用（组合 C1：SwiftShader）', () => {
    const canvas = document.createElement('canvas')
    const gl = canvas.getContext('webgl2')
    expect(gl).not.toBeNull()
  })
})
