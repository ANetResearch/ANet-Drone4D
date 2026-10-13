// The open interface page of the demo build without the application (AWR-04 §11.4; M15-FR-221: no three, no engine, no
// realtime client): app/demo/entry.tsx renders /sandbox/api here instead of loading src/main.tsx. The boot mask of
// index.html is removed (nothing boots), the locale follows the landing page's choice, "close" goes back or to the
// landing page. The chunk grouping that keeps the shared UI chunk (and through it three) away from this page is the
// build's (request to M00 in the WP-12 report); in the application the same page is the /sandbox/api route.
import { createRoot } from 'react-dom/client'
import { setLocale } from '@/app/i18n'
import { ApiDocsPage } from '@/ui/views/ApiDocs'

function locale(): 'zh-CN' | 'en' {
  try {
    const v = globalThis.localStorage?.getItem('awr.demo.lang')
    if (v === 'zh-CN' || v === 'en') return v
  } catch {
    // storage unavailable
  }
  return /^zh\b/i.test(globalThis.navigator?.language ?? 'zh-CN') ? 'zh-CN' : 'en'
}

export function renderApiDocs(el: HTMLElement): void {
  document.getElementById('boot-mask')?.remove()
  setLocale(locale())
  createRoot(el).render(<ApiDocsPage onClose={() => (history.length > 1 ? history.back() : location.assign('/'))} />)
}
