// M15-FR-031, FR-032 (AWR-14 §3.6, §6.2): selection semantics and fault-tolerant prefs persistence.
import { beforeAll, describe, expect, it, vi } from 'vitest'

class MemoryStorage {
  m = new Map<string, string>()
  getItem(k: string) { return this.m.get(k) ?? null }
  setItem(k: string, v: string) { this.m.set(k, v) }
  removeItem(k: string) { this.m.delete(k) }
  clear() { this.m.clear() }
}
const storage = new MemoryStorage()

beforeAll(() => {
  vi.stubGlobal('localStorage', storage)
})

describe('selection', () => {
  it('replaces, adds, toggles and keeps ids unique and ordered', async () => {
    const { selection, selectionStore } = await import('@/stores/selection')
    selection.select(['a', 'b', 'a'])
    expect(selectionStore.getState().ids).toEqual(['a', 'b'])
    expect(selectionStore.getState().primary).toBe('a')
    selection.select(['c'], 'add')
    expect(selectionStore.getState().ids).toEqual(['a', 'b', 'c'])
    expect(selectionStore.getState().primary).toBe('c')
    selection.select(['a'], 'toggle')
    expect(selectionStore.getState().ids).toEqual(['b', 'c'])
    expect(selectionStore.getState().primary).toBe('c')
    const v = selectionStore.getState().version
    selection.clear()
    expect(selectionStore.getState()).toMatchObject({ ids: [], primary: null, version: v + 1 })
    selection.clear()
    expect(selectionStore.getState().version).toBe(v + 1)
  })

  it('selects a contiguous range in list order', async () => {
    const { selection, selectionStore } = await import('@/stores/selection')
    selection.selectRange('d', 'b', ['a', 'b', 'c', 'd', 'e'])
    expect(selectionStore.getState().ids).toEqual(['b', 'c', 'd'])
    expect(selectionStore.getState().primary).toBe('b')
  })
})

describe('prefs', () => {
  it('clamps widths, pages and world ids on read', async () => {
    const { clampLayout, clampUi, DEFAULT_LAYOUT, DEFAULT_UI } = await import('@/stores/prefs')
    const l = clampLayout({ ...DEFAULT_LAYOUT, left: { open: true, width: 9999 }, right: { open: false, width: -5, page: 'bogus' as never } })
    expect(l.left.width).toBe(400)
    expect(l.right).toEqual({ open: false, width: 280, page: 'list' })
    const u = clampUi({ ...DEFAULT_UI, lastWorld: '../etc', motion: 'wild' as never })
    expect(u.lastWorld).toBeNull()
    expect(u.motion).toBe('system')
  })

  it('falls back to defaults on corrupt or old-version storage', async () => {
    const { loadSlice } = await import('@/lib/persist')
    storage.setItem('k1', '{not json')
    expect(loadSlice('k1', 1, { v: 1, a: 2 })).toEqual({ v: 1, a: 2 })
    storage.setItem('k2', JSON.stringify({ v: 0, a: 5 }))
    expect(loadSlice('k2', 1, { v: 1, a: 2 })).toEqual({ v: 1, a: 2 })
    storage.setItem('k3', JSON.stringify({ v: 1, a: 5 }))
    expect(loadSlice('k3', 1, { v: 1, a: 2 })).toEqual({ v: 1, a: 5 })
  })

  it('persists layout changes after flush', async () => {
    const { prefs, prefsStore, LAYOUT_KEY } = await import('@/stores/prefs')
    prefs.setLayout({ left: { width: 300 } })
    expect(prefsStore.getState().layout.left.width).toBe(300)
    prefs.flush()
    expect(JSON.parse(storage.getItem(LAYOUT_KEY) ?? '{}').left.width).toBe(300)
  })
})
