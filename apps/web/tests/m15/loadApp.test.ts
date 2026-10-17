// issue #9: the public demo entry retries the application chunk, reloads the tab once, then shows the error and a
// reload button in the boot mask (app/demo/loadApp.ts).
import { describe, expect, it } from 'vitest'
import { RELOAD_KEY, clearReloadMark, fallbackTexts, importWithRetry, onAppLoadFailed } from '@/app/demo/loadApp'

function memStore(): Storage {
  const m = new Map<string, string>()
  return { get length() { return m.size }, clear: () => m.clear(), getItem: (k) => m.get(k) ?? null, key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => void m.delete(k), setItem: (k, v) => void m.set(k, v) }
}

interface El { tag: string; className: string; textContent: string; type?: string; dataset: Record<string, string>; attrs: Record<string, string>;
  children: El[]; listeners: Record<string, () => void>; setAttribute(k: string, v: string): void; appendChild(c: El): void;
  addEventListener(k: string, f: () => void): void; querySelector(sel: string): El | null }
function el(tag: string, className = ''): El {
  const e: El = { tag, className, textContent: '', dataset: {}, attrs: {}, children: [], listeners: {},
    setAttribute(k, v) { e.attrs[k] = v }, appendChild(c) { e.children.push(c) }, addEventListener(k, f) { e.listeners[k] = f },
    querySelector(sel) { return e.children.find((c) => `.${c.className}` === sel) ?? null } }
  return e
}
const ZH = { failZh: '应用资源加载失败，请检查网络连接后重新载入', failReloadZh: '重新载入' }
function fakeDoc(): { doc: Document; mask: El } {
  const mask = el('div')
  Object.assign(mask.dataset, ZH)
  mask.children.push(el('div', 'boot-phase'))
  const doc = { getElementById: (id: string) => (id === 'boot-mask' ? mask : null), createElement: (t: string) => el(t) }
  return { doc: doc as unknown as Document, mask }
}

describe('application chunk loading (issue #9)', () => {
  it('retries after each delay and resolves on a later attempt', async () => {
    let n = 0
    const slept: number[] = []
    const v = await importWithRetry(async () => {
      n++
      if (n < 3) throw new Error('ERR_CONNECTION_RESET')
      return 'main'
    }, [10, 20], async (ms) => void slept.push(ms))
    expect(v).toBe('main')
    expect(slept).toEqual([10, 20])
  })

  it('rejects with the last error after the last attempt', async () => {
    let n = 0
    await expect(importWithRetry(async () => { throw new Error(`fail ${++n}`) }, [1, 1], async () => undefined)).rejects.toThrow('fail 3')
  })

  it('reloads once per tab, then shows the error line and a reload button', () => {
    const store = memStore()
    let reloads = 0
    const reload = (): void => void reloads++
    const t = fallbackTexts('zh-CN', { dataset: ZH })
    const a = fakeDoc()
    expect(onAppLoadFailed(a.doc, store, reload, t)).toBe('reloaded')
    expect(reloads).toBe(1)
    expect(store.getItem(RELOAD_KEY)).not.toBeNull()
    const b = fakeDoc()
    expect(onAppLoadFailed(b.doc, store, reload, t)).toBe('shown')
    expect(reloads).toBe(1)
    expect(b.mask.dataset.boot).toBe('BOOT_ERROR')
    expect(b.mask.querySelector('.boot-phase')?.textContent).toBe('应用资源加载失败，请检查网络连接后重新载入')
    const btn = b.mask.querySelector('.boot-reload')
    expect(btn?.textContent).toBe('重新载入')
    btn?.listeners.click()
    expect(reloads).toBe(2)
    clearReloadMark(store)
    expect(store.getItem(RELOAD_KEY)).toBeNull()
  })

  it('shows the error without storage and falls back to English texts outside zh', () => {
    const a = fakeDoc()
    expect(onAppLoadFailed(a.doc, null, () => undefined, fallbackTexts('en-US', null))).toBe('shown')
    expect(a.mask.querySelector('.boot-reload')?.textContent).toBe('Reload')
  })
})
