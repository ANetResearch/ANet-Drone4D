// D2.2-FX-landing (confirms an adversarial-review finding, D2.2-R-landing §2.2): "open my sandbox" (the landing
// page's second main action) showed the server's Chinese template name/summary/first-feedback even under the
// English UI, and even the i18n fallback was empty for most of sandbox.template.*, sandbox.limitedBy.*,
// sandbox.endReason.* and sandbox.band.* in en.json, so an English visitor saw Chinese text mixed into the Sheet.
// templateLabel()/TemplateLines() now read the server's zh_ fields only under the zh-CN UI (StartSheet.tsx), and the
// four prefixes above are filled in en.json.
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it } from 'vitest'
import { getLocale, setLocale } from '@/app/i18n'
import en from '@/app/i18n/en.json'
import { templateLabel, TemplateLines } from '@/ui/sandbox/StartSheet'

const tpl = { template_id: 't5_area_guard', name_zh: '区域值守与接力（服务器自定义名）', summary_zh: '服务器自定义摘要', first_feedback_zh: '服务器自定义首次反馈' }

afterEach(() => setLocale('zh-CN'))

describe('templateLabel (D2.2-R-landing §2.2)', () => {
  it('uses the server\'s zh_ name under the zh-CN UI', () => {
    setLocale('zh-CN')
    expect(templateLabel((k) => k, tpl)).toBe(tpl.name_zh)
  })

  it('ignores the server\'s zh_ name under the en UI and falls back to the i18n dictionary (no server name_en exists yet)', () => {
    setLocale('en')
    expect(getLocale()).toBe('en')
    expect(templateLabel((k) => k, tpl)).not.toBe(tpl.name_zh)
    expect(templateLabel((k) => k, tpl)).toBe(`sandbox.template.${tpl.template_id}`)
  })
})

describe('TemplateLines (D2.2-R-landing §2.2)', () => {
  it('shows the server\'s zh_ summary and first-feedback under the zh-CN UI', () => {
    setLocale('zh-CN')
    const html = renderToStaticMarkup(h(TemplateLines, { tpl }))
    expect(html).toContain(tpl.summary_zh)
    expect(html).toContain(tpl.first_feedback_zh)
  })

  it('never renders the server\'s zh_ text under the en UI', () => {
    setLocale('en')
    const html = renderToStaticMarkup(h(TemplateLines, { tpl }))
    expect(html).not.toContain(tpl.summary_zh)
    expect(html).not.toContain(tpl.first_feedback_zh)
  })
})

describe('en.json completeness of the Sheet\'s fallback keys (D2.2-R-landing §2.2)', () => {
  const dict = en as Record<string, string>
  const prefixes = ['sandbox.template', 'sandbox.limitedBy', 'sandbox.endReason', 'sandbox.band']
  it('has no empty string under sandbox.template.*, sandbox.limitedBy.*, sandbox.endReason.* or sandbox.band.*', () => {
    const empty = Object.keys(dict).filter((k) => prefixes.some((p) => k === p || k.startsWith(`${p}.`)) && dict[k] === '')
    expect(empty).toEqual([])
  })
  it('covers every known template id (t1..t6) with a name, a short label and a first-feedback line', () => {
    for (const id of ['t1_point_watch', 't2_polyline', 't3_area_patrol', 't4_scan_gsd', 't5_area_guard', 't6_manual']) {
      expect(dict[`sandbox.template.${id}`], id).toBeTruthy()
      expect(dict[`sandbox.template.${id}.short`], `${id}.short`).toBeTruthy()
      expect(dict[`sandbox.template.${id}.first`], `${id}.first`).toBeTruthy()
    }
  })
})
