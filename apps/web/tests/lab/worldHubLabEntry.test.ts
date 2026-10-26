// The World Hub (/worlds) offers an "Event Lab" entry, a plain full-page anchor to the /lab/ static build, only in builds with
// VITE_AWR_LAB_PUBLIC=1; without the switch it is a constant-false branch and no /lab/ address is in the page.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const WEB = resolve(import.meta.dirname, '../..')
const read = (p: string): string => readFileSync(resolve(WEB, p), 'utf8')

describe('World Hub Event Lab entry', () => {
  const src = read('src/ui/views/WorldHub.tsx')
  it('is gated by the build switch read in place and links to /lab/ with a plain anchor', () => {
    // read in place, not imported from lab-public/flags: a cross-chunk import keeps /lab/ in the build without the switch (K9 D3)
    expect(src).toMatch(/const HUB_LAB_ENTRY: string \| null = import\.meta\.env\.VITE_AWR_LAB_PUBLIC === '1' \? '\/lab\/' : null/)
    expect(src).not.toMatch(/from '@\/app\/lab-public\/flags'/)
    expect(src).toMatch(/\{HUB_LAB_ENTRY \? \(\s*<Button[^>]*render=\{<a href=\{HUB_LAB_ENTRY\} data-world-hub-lab="" \/>\}/)
    expect(src).not.toMatch(/navigate\(['"`]\/lab/)
  })
  it('has the label in both locales', () => {
    expect(JSON.parse(read('src/app/i18n/en.json'))['hub.eventLab']).toBe('Event Lab')
    expect(JSON.parse(read('src/app/i18n/zh-CN.json'))['hub.eventLab']).toBe('事件沙盘')
  })
})
