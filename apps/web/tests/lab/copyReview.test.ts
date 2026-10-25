// D3 文案评审须改正项的回归（docs/impl/D3-文案评审记录.md §4 W-01 至 W-07、W-09、W-10；D3-AC-23、D3-AC-46）。逐条固定改写结果，
// 防止后续改动回退：界面不出现交付称谓（W-01，另由 copy-lint COPY-10 机检）、中文不夹用 Run（W-02，见 board.test.ts）、角色名与
// 术语表一致（W-03）、英文机体称谓统一为 UAV 且按复数处理（W-04）、终止时限与超时按 §21.15.5 模板原文（W-06）、错误附原因码或
// 处理建议（W-07）、产品名 Interactive Sandbox（W-09）、英文语法（W-10）。W-05 与 W-07 的原型榜部分见 board.test.ts。
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { formatMessage } from '@/app/i18n/plural'

const APP = join(import.meta.dirname, '..', '..', 'src', 'app')
const read = (rel: string) => JSON.parse(readFileSync(join(APP, rel), 'utf8')) as Record<string, string>

/** D3 界面词典：主词典的 lab.* 键、lab 分片、/lab/ 公开构建外壳与样例页 */
function d3Dicts(): Array<[string, Record<string, string>]> {
  const out: Array<[string, Record<string, string>]> = []
  for (const f of ['i18n/zh-CN.json', 'i18n/en.json']) {
    const d = read(f)
    out.push([f, Object.fromEntries(Object.entries(d).filter(([k]) => k.startsWith('lab.')))])
  }
  for (const f of readdirSync(join(APP, 'i18n', 'lab')).filter((x) => x.endsWith('.json'))) out.push([`i18n/lab/${f}`, read(`i18n/lab/${f}`)])
  for (const f of ['lab-public/i18n/zh-CN.json', 'lab-public/i18n/en.json', 'lab-public/samples/i18n/zh-CN.json', 'lab-public/samples/i18n/en.json']) out.push([f, read(f)])
  return out
}

describe('D3 copy review must-fix findings', () => {
  it('W-01: no interface string names a delivery (D1 to D4, D3.x)', () => {
    for (const [f, d] of d3Dicts()) {
      for (const [k, v] of Object.entries(d)) expect(v, `${f} ${k}`).not.toMatch(/(?<![\w.-])D[1-4](?:\.x)?(?![\w-])/)
    }
    expect(read('i18n/zh-CN.json')['lab.login.title']).toBe('事件沙盘内部工作区登录')
    expect(read('i18n/en.json')['lab.login.title']).toBe('Event Lab internal workspace sign-in')
    expect(read('i18n/lab/lidar-compare.zh-CN.json')['lab.lidarCompare.budget.s']).toContain('（交互沙盒点预算阶梯）')
    expect(read('i18n/lab/lidar-compare.en.json')['lab.lidarCompare.budget.s']).toContain('(Interactive Sandbox point budget ladder)')
  })

  it('W-03: role names follow the AWR-14 role table and the role hints on the same pages', () => {
    const zh = read('i18n/zh-CN.json')
    const en = read('i18n/en.json')
    expect([zh['lab.role.researcher'], zh['lab.role.invited']]).toEqual(['研究者', '受邀成员'])
    expect([en['lab.role.researcher'], en['lab.role.invited']]).toEqual(['Researcher', 'Invited member'])
    for (const [f, k] of [['compare', 'lab.compare.roleDeniedHint'], ['lidar-compare', 'lab.lidarCompare.roleDeniedHint'], ['replay', 'lab.replay.roleDeniedHint']]) {
      expect(read(`i18n/lab/${f}.zh-CN.json`)[k], k).toContain(zh['lab.role.researcher'])
    }
    for (const [f, d] of d3Dicts().filter(([x]) => x.includes('zh-CN'))) for (const [k, v] of Object.entries(d)) expect(v, `${f} ${k}`).not.toContain('研究员')
  })

  it('W-04: English D3 pages call the vehicles UAVs, with plural forms', () => {
    for (const [f, d] of d3Dicts().filter(([x]) => x.endsWith('en.json'))) {
      for (const [k, v] of Object.entries(d)) expect(v, `${f} ${k}`).not.toMatch(/\bdrones?\b|\bVehicles?\b/)
    }
    expect(read('i18n/lab/compare.en.json')['lab.compare.uav']).toBe('UAV')
    const pub = read('lab-public/i18n/en.json')
    const run = read('lab-public/samples/i18n/en.json')
    expect(pub['lab.public.catalog.intro']).toContain('per-UAV private views')
    for (const n of [1, 3]) {
      const want = n === 1 ? '1 UAV' : '3 UAVs'
      expect(formatMessage(pub['lab.public.catalog.fleet']!, { n }, 'en')).toBe(want)
      const p = { event: 'crop-handoff-001', version: 1, world: 'oryahovo-crop1', policy: 'track_share', n, obs: 'L2' }
      expect(formatMessage(pub['lab.public.sample.meta']!, p, 'en')).toContain(` · ${want} · `)
      expect(formatMessage(run['lab.public.run.meta']!, p, 'en')).toContain(` · ${want} · `)
    }
  })

  it('W-06: termination and timeout copy follows the §21.15.5 template wording', () => {
    const runs = read('i18n/lab/runs.en.json')
    expect(runs['lab.runs.cancelBody']).toBe('A queued run is cancelled immediately. A started run terminates within 10 s, and recordings written so far are retained for 14 days.')
    expect(runs['lab.runs.batchBody']).toBe('Started runs terminate within 10 s. Recordings written so far are retained for 14 days.')
    expect(read('i18n/lab/inspect.en.json')['lab.inspect.error.timeout'])
      .toBe('Data inspection could not open the recording within 10 s. Check that rerun-proxy and the catalog service are available, then retry.')
    for (const [f, d] of d3Dicts()) for (const [k, v] of Object.entries(d)) expect(v, `${f} ${k}`).not.toMatch(/cancellation timeout|opening timeout/)
  })

  it('W-07: errors state the reason code or what to do next', () => {
    expect(read('i18n/lab/inspect.zh-CN.json')['lab.inspect.error.session']).toBe('工作区会话已失效（302 TOKEN_INVALID），请重新登录。')
    expect(read('i18n/lab/inspect.en.json')['lab.inspect.error.session']).toBe('The workspace session is no longer valid (302 TOKEN_INVALID). Sign in again.')
    expect(read('lab-public/samples/i18n/zh-CN.json')['lab.public.run.views.error']).toBe('视频加载失败。可在样例数据下载中获取该视频。')
    expect(read('lab-public/samples/i18n/en.json')['lab.public.run.views.error']).toBe('The video could not be loaded. It is available in the sample data downloads.')
  })

  it('W-09 and W-10: product name and English grammar', () => {
    expect(read('lab-public/i18n/en.json')['lab.public.nav.sandbox']).toBe('Interactive Sandbox')
    expect(read('i18n/lab/replay.en.json')['lab.replay.stale']).toBe('No new frame for {s} s')
  })
})
