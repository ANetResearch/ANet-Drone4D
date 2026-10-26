// The World Hub (/worlds) offers an "Event Lab" entry, a plain full-page anchor to the /lab/ static build, only in builds with
// VITE_AWR_LAB_PUBLIC=1; without the switch it is a constant-false branch and no /lab/ address is in the page.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const WEB = resolve(import.meta.dirname, '../..')
const read = (p: string): string => readFileSync(resolve(WEB, p), 'utf8')

describe('World Hub Event Lab entry', () => {
  const src = read('src/ui/views/WorldHub.tsx')
  it('is gated by LAB_PUBLIC and links to LAB_PUBLIC_ENTRY with a plain anchor', () => {
    expect(src).toMatch(/\{LAB_PUBLIC \? \(\s*<Button[^>]*render=\{<a href=\{LAB_PUBLIC_ENTRY\} data-world-hub-lab="" \/>\}/)
    expect(src).not.toMatch(/navigate\(['"`]\/lab/)
  })
  it('has the label in both locales', () => {
    expect(JSON.parse(read('src/app/i18n/en.json'))['hub.eventLab']).toBe('Event Lab')
    expect(JSON.parse(read('src/app/i18n/zh-CN.json'))['hub.eventLab']).toBe('事件沙盘')
  })
})
