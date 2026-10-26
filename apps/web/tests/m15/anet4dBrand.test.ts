// v0.3.2 rename (ANet Drone4D -> ANet4D): every user-visible string of the web app names the product ANet4D; the old
// names survive only as legacy identifiers (schema ids such as drone4d.bench.*, the reserved DRONE4D_* environment prefix,
// the retired repository name ANet-Drone4D, which GitHub redirects). The landing hero carries the positioning, with the NS-3
// analogy phrased as an aim and used exactly once; the public addresses come from lib/site.ts only.
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { DOCS_URL, PRODUCT_NAME, REPO_LABEL, REPO_URL, SITE_ORIGIN } from '@/lib/site'
import { DEMO_DOCS_URL, DEMO_REPO_URL, DEMO_SITE_ORIGIN } from '@/lib/demo'
import { ANET4D_BIBTEX, CITATIONS } from '@/app/demo/LandingContent'

const WEB = resolve(import.meta.dirname, '../..')
const read = (p: string): string => readFileSync(resolve(WEB, p), 'utf8')
const dicts: [string, Record<string, string>][] = [
  'src/app/i18n/zh-CN.json', 'src/app/i18n/en.json', 'src/app/lab-public/i18n/zh-CN.json', 'src/app/lab-public/i18n/en.json',
  ...readdirSync(resolve(WEB, 'src/app/i18n/lab')).filter((f) => f.endsWith('.json')).map((f) => `src/app/i18n/lab/${f}`),
].map((p) => [p, JSON.parse(read(p)) as Record<string, string>])
/** legacy identifiers that may stay: schema / package ids (drone4d.xxx), the reserved env prefix, the repository name */
const legacy = (v: string): string => v.replace(/\bdrone4d\.[a-z_][\w.]*/g, '').replace(/\bDRONE4D_\*?/g, '').replace(/ANet-Drone4D/g, '')

describe('ANet4D product name (v0.3.2)', () => {
  it('no dictionary value names the retired product', () => {
    for (const [p, d] of dicts) {
      for (const [k, v] of Object.entries(d)) expect(legacy(v), `${p} ${k}`).not.toMatch(/drone\s?4d/i)
    }
  })

  it('brand keys, page titles and the Event Lab header', () => {
    const [zh, en] = [dicts[0][1], dicts[1][1]]
    expect(zh['brand.product']).toBe('ANet4D')
    expect(en['brand.product']).toBe('ANet4D')
    expect(zh['demo.product']).toBe('ANet4D 公开演示')
    expect(en['demo.product']).toBe('ANet4D public demo')
    expect(zh['landing.docTitle']).toBe('ANet4D · 新一代具身智能体协同模拟基础设施')
    expect(en['landing.docTitle']).toBe('ANet4D · Simulation infrastructure for collaborative embodied agents')
    const lab = Object.fromEntries(dicts.filter(([p]) => p.includes('lab-public')).map(([p, d]) => [p.endsWith('en.json') ? 'en' : 'zh', d]))
    expect(lab.en['lab.public.brand']).toBe('ANet4D · Event Lab')
    expect(lab.zh['lab.public.brand']).toBe('ANet4D · 事件沙盘')
  })

  it('the landing hero states the positioning; the NS-3 analogy is an aim and appears once per locale', () => {
    const [zh, en] = [dicts[0][1], dicts[1][1]]
    expect(zh['landing.slogan'].replace(/​/g, '')).toBe('新一代具身智能体协同模拟基础设施')
    expect(en['landing.slogan']).toBe('Simulation infrastructure for collaborative embodied agents')
    expect(zh['landing.tagline']).toContain('ANet4D 的目标是成为具身智能体协同研究的标准实验基础设施，如同 NS-3 之于网络研究')
    expect(en['landing.tagline']).toContain('ANet4D aims to be for collaborative embodied-agent research what NS-3 is for network research')
    for (const d of [zh, en]) {
      const hits = Object.entries(d).filter(([k, v]) => k.startsWith('landing.') && v.includes('NS-3'))
      expect(hits.map(([k]) => k)).toEqual(['landing.tagline'])
    }
  })

  it('public addresses live in lib/site.ts and the demo constants re-export them', () => {
    expect(PRODUCT_NAME).toBe('ANet4D')
    expect(SITE_ORIGIN).toBe('https://anet4d.agentnetwork.org.cn')
    expect(DOCS_URL).toBe('https://docs.agentnetwork.org.cn/anet4d/zh-cn/latest/')
    expect(REPO_URL).toBe('https://github.com/ANetResearch/ANet4D')
    expect(REPO_LABEL).toBe('github.com/ANetResearch/ANet4D')
    expect([DEMO_SITE_ORIGIN, DEMO_DOCS_URL, DEMO_REPO_URL]).toEqual([SITE_ORIGIN, DOCS_URL, REPO_URL])
    expect(ANET4D_BIBTEX).toContain('title   = {ANet4D: Simulation Infrastructure for Collaborative Embodied Agents}')
    expect(ANET4D_BIBTEX).toContain(`url     = {${REPO_URL}}`)
    expect(CITATIONS[0]).toMatchObject({ id: 'anet4d', label: 'ANet4D' })
  })

  it('HTML entries, the manifest and the landing hero image use the new name', () => {
    const app = read('index.html')
    expect(app).toContain('<title>ANet4D</title>')
    expect(app).toContain('<div class="boot-phase">ANet4D · World Runtime</div>')
    const lab = read('src/app/lab-public/index.html')
    expect(lab).toContain('<title>事件沙盘 · ANet4D 公开站</title>')
    expect(lab).toContain('<div class="boot-phase">ANet4D · Event Lab</div>')
    const manifest = JSON.parse(read('public/manifest.webmanifest')) as { name: string; short_name: string }
    expect([manifest.name, manifest.short_name]).toEqual(['ANet4D', 'ANet4D'])
    for (const s of [app, lab, read('public/manifest.webmanifest')]) expect(s).not.toMatch(/drone\s?4d/i)
    expect(read('src/app/demo/Landing.tsx')).toMatch(/src="\/demo\/anet4d-banner\.jpg"[^>]*width=\{2400\} height=\{900\}/)
  })
})
