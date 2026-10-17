// Loading the application chunk from the public demo entry (issue #9; M15-FR-006 boot mask). A failed dynamic import of
// src/main.tsx (a connection reset on one of its chunks) used to leave the boot mask on "loading the application" forever.
// The import is now retried with a short backoff; after the last failure the tab reloads once (a reload refetches every
// chunk), and if that does not help either, the boot mask shows an error line and a reload button (BOOT_ERROR).
// No imports: this module is bundled into the always-loaded entry chunk (the landing page chunk rule, see entry.tsx).

/** delays (ms) before the 2nd and 3rd import attempts */
export const APP_RETRY_DELAYS_MS: readonly number[] = [1000, 3000]
/** sessionStorage key marking the one automatic reload of this tab */
export const RELOAD_KEY = 'awr.boot.reload.v1'

const wait = (ms: number): Promise<void> => new Promise((ok) => setTimeout(ok, ms))

/** runs `load` until it resolves: once, then once after each delay; rejects with the last error */
export async function importWithRetry<T>(load: () => Promise<T>, delays: readonly number[] = APP_RETRY_DELAYS_MS,
  sleep: (ms: number) => Promise<void> = wait): Promise<T> {
  let last: unknown = null
  for (let i = 0; i <= delays.length; i++) {
    try {
      return await load()
    } catch (e) {
      last = e
      if (i < delays.length) await sleep(delays[i])
    }
  }
  throw last
}

export interface LoadFailTexts { message: string; reload: string }

const EN: LoadFailTexts = { message: 'Application resources failed to load. Check the network connection and reload.', reload: 'Reload' }

/** texts when the i18n chunk itself is unavailable: the boot mask carries them (index.html `data-fail-*`, both languages) */
export function fallbackTexts(lang: string, mask: { dataset: Record<string, string | undefined> } | null): LoadFailTexts {
  const k = lang.toLowerCase().startsWith('zh') ? 'Zh' : 'En'
  const d = mask?.dataset ?? {}
  return { message: d[`fail${k}`] || EN.message, reload: d[`failReload${k}`] || EN.reload }
}

/**
 * after the last failed attempt: reload this tab once (marked in sessionStorage), else show the error and a reload button
 * in the boot mask; returns what it did
 */
export function onAppLoadFailed(doc: Document, storage: Pick<Storage, 'getItem' | 'setItem'> | null, reload: () => void,
  texts: LoadFailTexts): 'reloaded' | 'shown' {
  let first = false
  try {
    first = storage !== null && storage.getItem(RELOAD_KEY) === null
    if (first) storage!.setItem(RELOAD_KEY, String(Date.now()))
  } catch {
    first = false
  }
  if (first) {
    reload()
    return 'reloaded'
  }
  const mask = doc.getElementById('boot-mask')
  if (mask) {
    mask.dataset.boot = 'BOOT_ERROR'
    mask.setAttribute('role', 'alert')
    const phase = mask.querySelector<HTMLElement>('.boot-phase')
    if (phase) phase.textContent = texts.message
    if (!mask.querySelector('.boot-reload')) {
      const b = doc.createElement('button')
      b.type = 'button'
      b.className = 'boot-reload'
      b.textContent = texts.reload
      b.addEventListener('click', () => {
        try {
          storage?.setItem(RELOAD_KEY, String(Date.now()))
        } catch {
          // storage unavailable: the reload still happens
        }
        reload()
      })
      mask.appendChild(b)
    }
  }
  return 'shown'
}

/** a successful start clears the reload mark, so a later failure in this tab may reload once again */
export function clearReloadMark(storage: Pick<Storage, 'removeItem'> | null): void {
  try {
    storage?.removeItem(RELOAD_KEY)
  } catch {
    // ignore
  }
}
