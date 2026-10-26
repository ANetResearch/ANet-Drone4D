// V0.3.1 (extension point 15, ADR-129): the Event Lab entries of the landing page. VITE_AWR_LAB_PUBLIC is a build-time
// constant (app/lab-public/flags.ts), so each case stubs it and imports the landing modules afresh:
// - switch on: the header link (desktop bar) to /lab/, the hero's third action, and the "event-lab" section after the
//   story and before the showcase with its five frames (lazy, intrinsic sizes, the thermal frame letterboxed), the sample
//   run link and the CC BY 4.0 attribution; the section numbers follow the reading order;
// - switch off: none of it, and no "/lab/" address anywhere in the page (the K9 D3 scan of the D2 public build).
// The texts come from the trimmed landing dictionary, whose Event Lab keys the build adds only with the switch
// (vite.config.ts landingI18nPlugin reads the switch once per build): each case supplies the dictionary the matching
// build would carry (landingI18nDict over LANDING_LAB_I18N_KEYS or LANDING_I18N_KEYS).
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { landingI18nDict } from '../../vite.config.ts'
import { LANDING_I18N_KEYS, LANDING_LAB_I18N_KEYS } from '@/app/demo/landingKeys'

const WEB = resolve(import.meta.dirname, '../..')
const zhCN = JSON.parse(readFileSync(resolve(WEB, 'src/app/i18n/zh-CN.json'), 'utf8')) as Record<string, string>
const en = JSON.parse(readFileSync(resolve(WEB, 'src/app/i18n/en.json'), 'utf8')) as Record<string, string>

async function render(lab: boolean, locale: 'zh-CN' | 'en' = 'zh-CN'): Promise<string> {
  vi.resetModules()
  vi.stubEnv('VITE_AWR_LAB_PUBLIC', lab ? '1' : '')
  vi.doMock('virtual:awr-landing-i18n', () => ({ default: landingI18nDict(zhCN, en, lab ? LANDING_LAB_I18N_KEYS : LANDING_I18N_KEYS) }))
  globalThis.localStorage?.setItem('awr.demo.lang', locale)
  const { Landing } = await import('@/app/demo/Landing')
  const { setLocale } = await import('@/app/demo/LandingI18n')
  setLocale(locale)
  return renderToStaticMarkup(createElement(Landing))
}

afterEach(() => {
  vi.doUnmock('virtual:awr-landing-i18n')
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('landing page Event Lab entries (VITE_AWR_LAB_PUBLIC, V0.3.1)', () => {
  it('with the switch: header link, hero action and the Event Lab section directly after the hero and before the story', async () => {
    const html = await render(true)
    expect(html).toMatch(/<a[^>]*href="\/lab\/"[^>]*data-landing-lab-entry="nav"/)
    expect(html).toMatch(/<a[^>]*href="\/lab\/"[^>]*data-landing-lab-entry="hero"/)
    expect(html).toMatch(/<a[^>]*href="\/lab\/"[^>]*data-landing-lab-entry="section"/)
    expect(html).toMatch(/<a[^>]*href="\/lab\/samples\/run-koyqeft6xe"[^>]*data-landing-lab-sample=""/)
    const order = ['id="event-lab"', 'id="story"', 'id="showcase"', 'id="closing"', 'id="open-source"'].map((s) => html.indexOf(s))
    expect(order.every((i) => i >= 0)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    // the hero links to the featured sample run
    expect(html).toMatch(/<a[^>]*href="\/lab\/samples\/run-koyqeft6xe"[^>]*data-landing-lab-featured=""/)
    // header navigation: the section in NAV (scrolls) plus the full-page link
    expect(html).toContain('href="#event-lab"')
    // section numbers follow the reading order (Event Lab 01, story 02, showcase 03, closing, open source 05)
    const idx = (id: string) => new RegExp(`<section id="${id}"[\\s\\S]*?tabular-nums">(\\d\\d)<`).exec(html)?.[1]
    expect([idx('event-lab'), idx('showcase'), idx('open-source')]).toEqual(['01', '03', '05'])
    const sec = html.slice(html.indexOf('id="event-lab"'), html.indexOf('id="story"'))
    const imgs = [...sec.matchAll(/<img [^>]*>/g)].map((m) => m[0])
    const { LAB_SAMPLES } = await import('@/app/demo/LandingContent')
    expect(imgs).toHaveLength(5 + LAB_SAMPLES.length) // the five frames and one gallery thumbnail per sample run
    for (const f of ['lab-oblique', 'lab-junction', 'lab-thermal', 'lab-residential', 'lab-blocks']) {
      const img = imgs.find((i) => i.includes(`data-src="/demo/lab/${f}.webp"`))
      expect(img, f).toBeDefined()
      // the address is set only near the viewport (useNearViewport), not with the page: no eager src in the markup
      expect(img).not.toMatch(/ src="/)
      expect(img).toContain('loading="lazy"')
      expect(img).toMatch(f === 'lab-thermal' ? /width="640" height="512"/ : /width="1600" height="900"/)
    }
    expect(sec).toContain('class="relative aspect-video bg-muted"')
    expect(sec).toContain('data-lab-attribution=""')
    for (const u of ['https://sketchfab.com/bg3d', 'https://creativecommons.org/licenses/by/4.0/']) expect(sec).toContain(`href="${u}"`)
    // real texts, no key names left (the switched dictionary carries every Event Lab key)
    expect(html).not.toMatch(/>landing\.[a-z]/)
    expect(sec).toContain('事件沙盘 · 事件回放与每机私有视角')
    expect(sec).toContain('样例运行 run-koyqeft6xe · 事件 crop-handoff-001 · 世界 oryahovo-crop1')
    expect(sec).toContain('“oryahovo - crop #1 - metashape texture problems”')
    expect(sec).toMatch(/作者 <a [^>]*>Mitko<\/a>（<a [^>]*>sketchfab\.com\/bg3d<\/a>），以 <a [^>]*>CC BY 4\.0<\/a> 许可发布/)
  })

  it('with the switch, in English: the same entries with the English texts', async () => {
    const html = await render(true, 'en')
    expect(html).not.toMatch(/>landing\.[a-z]/)
    for (const s of ['Event Lab · event replay with per-UAV private views', 'UAV view samples', 'Open Event Lab', 'View sample run run-koyqeft6xe']) expect(html).toContain(s)
    expect(html).toMatch(/by <a [^>]*>Mitko<\/a> \(<a [^>]*>sketchfab\.com\/bg3d<\/a>\), licensed under <a [^>]*>CC BY 4\.0<\/a>, modified/)
  })

  it('without the switch: no Event Lab entry and no /lab/ address in the page', async () => {
    const html = await render(false)
    expect(html).not.toContain('data-landing-lab-entry')
    expect(html).not.toContain('id="event-lab"')
    expect(html).not.toContain('/lab/')
    const idx = (id: string) => new RegExp(`<section id="${id}"[\\s\\S]*?tabular-nums">(\\d\\d)<`).exec(html)?.[1]
    expect([idx('showcase'), idx('open-source')]).toEqual(['02', '04'])
  })

  it('NAV leads with the Event Lab section; the story follows', async () => {
    vi.resetModules()
    vi.stubEnv('VITE_AWR_LAB_PUBLIC', '1')
    const on = await import('@/app/demo/LandingContent')
    expect(on.NAV).toEqual(['event-lab', 'story', 'showcase', 'open-source'])
    vi.resetModules()
    vi.stubEnv('VITE_AWR_LAB_PUBLIC', '')
    const off = await import('@/app/demo/LandingContent')
    expect(off.NAV).toEqual(['story', 'showcase', 'open-source'])
  })

  it('the sample gallery renders one card per LAB_SAMPLES entry (1 to 8) with lazy thumbnails, facts and a full-page link', async () => {
    const { LAB_SAMPLES } = await import('@/app/demo/LandingContent')
    expect(LAB_SAMPLES[0]).toMatchObject({ run: 'run-koyqeft6xe', event: 'crop-handoff-001', eventType: 'rendezvous_handoff', nUav: 3, obsLevel: 'L2' })
    for (const s of LAB_SAMPLES) expect(s.run).toMatch(/^run-[a-z2-7]{10}$/)
    const html = await render(true, 'en')
    const gallery = html.slice(html.indexOf('data-lab-gallery'), html.indexOf('data-lab-frames'))
    expect(gallery.match(/data-lab-sample-card="/g)).toHaveLength(LAB_SAMPLES.length)
    const card = /<a [^>]*data-landing-lab-card="run-koyqeft6xe"[^>]*>/.exec(gallery)?.[0]
    expect(card).toContain('href="/lab/samples/run-koyqeft6xe"')
    const img = /<img [^>]*>/.exec(gallery)?.[0] ?? ''
    expect(img).toMatch(/loading="lazy"/)
    expect(img).toMatch(/decoding="async"/)
    expect(img).toMatch(new RegExp(`width="${LAB_SAMPLES[0]!.thumbW}" height="${LAB_SAMPLES[0]!.thumbH}"`))
    expect(img).toContain(`data-src="${LAB_SAMPLES[0]!.thumb}"`)
    expect(img).not.toMatch(/ src="/)
    expect(gallery).toContain('Policy track_share')
    expect(gallery).toContain('3 UAVs')
    expect(gallery).toContain('Observation L2')
    expect(gallery).toContain('rendezvous_handoff')
    expect(gallery).toContain('grid-cols-1')
  })

  it('renders eight gallery entries without a layout change', async () => {
    vi.resetModules()
    vi.stubEnv('VITE_AWR_LAB_PUBLIC', '1')
    const real = await import('@/app/demo/LandingContent')
    const many = Array.from({ length: 8 }, (_, i) => ({ ...real.LAB_SAMPLES[0]!, run: `run-koyqeft6x${'abcdefgh'[i]}`, event: `ev-${i}-001` }))
    vi.resetModules()
    vi.doMock('@/app/demo/LandingContent', async (orig) => ({ ...(await orig<typeof import('@/app/demo/LandingContent')>()), LAB_SAMPLES: many }))
    vi.doMock('virtual:awr-landing-i18n', () => ({ default: landingI18nDict(zhCN, en, LANDING_LAB_I18N_KEYS) }))
    const { EventLab } = await import('@/app/demo/LandingEventLab')
    const html = renderToStaticMarkup(createElement(EventLab))
    expect(html.match(/data-lab-sample-card="/g)).toHaveLength(8)
    vi.doUnmock('@/app/demo/LandingContent')
  })
})
