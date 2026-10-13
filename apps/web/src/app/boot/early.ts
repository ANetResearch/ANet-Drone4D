// Early boot module (D2-FX-R2-web; R-D2-32; ADR-119; AWR-04 §15.5.3, §15.5.5; M15-FR-225; D2-AC-41). Owner: M15.
// The build inlines this module (with the capacity texts, the plural formatter and the capacity keys of zh-CN,
// vite.config.ts bootI18nPlugin) as the first classic script of index.html's <head>, right after the charset meta, so the
// request leaves while the HTML is still being parsed and long before the application chunks have arrived (D2-ACC-1: from
// the application entry the request left 0.6 s after the entry chunk and after every main chunk, and the line showed
// 1.3-4.4 s after the answer because the mask's long frames held its write back); the dev server loads it as the first
// module script.
// On every path that shows the boot mask it sends the public capacity request at once (1.5 s timeout), again every 5 s
// while the mask is up, and writes the answer into `.boot-capacity` in the same task as the answer (never in an idle or
// progress callback). The application takes the request over (window.__awrBootCapacity -> app/boot/capacity.ts, M17's
// stores/capacity), so the 5 s spacing of the mask period holds and no second request goes out. Never gates the reveal.
// The local build (channel `local`, normally without sandbox-api) writes successful answers only; the demo and CI builds
// write "capacity unavailable" as well. The demo landing page ("/", mask hidden) and the open interface page
// (/sandbox/api, mask removed) have their own capacity requests: nothing runs there. The request carries no token and no
// principal (privacy row of §15.5.3).
import { formatMessage } from '@/app/i18n/plural'
import BOOT_I18N from 'virtual:awr-boot-i18n'
import { BOOT_CAPACITY_REFRESH_MS, bootCapacityFetch, capacityTexts, writeCapacityLine, type BootCapacityHandoff } from './capacityText'

declare const __AWR_BUILD__: { channel?: string } | undefined

function channel(): string {
  try {
    return typeof __AWR_BUILD__ !== 'undefined' && __AWR_BUILD__ ? (__AWR_BUILD__.channel ?? 'local') : 'local'
  } catch {
    return 'local'
  }
}

const texts = capacityTexts((key, params) => formatMessage(BOOT_I18N[key] ?? key, params, 'zh-CN'))

/** the demo build's pages without the boot mask: the landing page and the open interface page (app/demo/entry.tsx) */
function maskless(path: string): boolean {
  return import.meta.env.VITE_AWR_DEMO === 'public' && (path === '/' || path === '/sandbox/api')
}

/**
 * start the boot capacity line; returns the handoff (null where the mask is not shown). The build inlines this module as
 * the first classic script of <head> (vite.config.ts earlyInlinePlugin), so it usually runs before the parser reached the
 * mask markup: the request goes out at once and the answer is written as soon as the line exists (at once when the body
 * is already parsed, else when parsing ends).
 */
export function startEarlyCapacity(doc: Document = document, path: string = location.pathname): BootCapacityHandoff | null {
  const g = globalThis as { __awrBootCapacity?: BootCapacityHandoff }
  if (g.__awrBootCapacity) return g.__awrBootCapacity
  if (maskless(path)) return null
  const showFailure = channel() !== 'local'
  let timer: ReturnType<typeof setTimeout> | null = null
  let parsedHook = false
  let pendingLine: Parameters<typeof writeCapacityLine>[1] | null = null
  const lineEl = (): HTMLElement | null => doc.querySelector<HTMLElement>('#boot-mask .boot-capacity')
  // the answer came before the parser reached the line: write it the moment the node is inserted (a mutation observer
  // runs at the end of the parser task that inserted it), at the latest when parsing ends
  const flush = (): boolean => {
    if (!pendingLine || !writeCapacityLine(lineEl(), pendingLine)) return false
    pendingLine = null
    return true
  }
  const write = (line: Parameters<typeof writeCapacityLine>[1]): void => {
    if (writeCapacityLine(lineEl(), line) || doc.readyState !== 'loading') return
    pendingLine = line
    if (parsedHook) return
    parsedHook = true
    let mo: MutationObserver | null = null
    const done = (): void => {
      flush()
      mo?.disconnect()
      mo = null
    }
    if (typeof MutationObserver !== 'undefined') {
      mo = new MutationObserver(() => {
        if (flush()) done()
      })
      mo.observe(doc.documentElement, { childList: true, subtree: true })
    }
    doc.addEventListener('readystatechange', done, { once: true })
  }
  const h: BootCapacityHandoff = {
    current: null, last: null, failures: 0, sentAtMs: 0,
    stop: () => {
      if (timer) clearTimeout(timer)
      timer = null
    },
  }
  const tick = (): void => {
    timer = null
    if (doc.readyState !== 'loading' && !doc.getElementById('boot-mask')) return
    const p = bootCapacityFetch()
    h.current = p
    h.sentAtMs = performance.now()
    p.then((d) => {
      h.last = d
      write(texts.capacityLine(d))
    }, () => {
      h.failures++
      if (!h.last && showFailure) write(texts.capacityLine(null))
    })
    timer = setTimeout(tick, BOOT_CAPACITY_REFRESH_MS)
  }
  g.__awrBootCapacity = h
  tick()
  return h
}

if (typeof document !== 'undefined') {
  performance.mark?.('awr.boot.early')
  startEarlyCapacity()
}
