// M15-FR-098, FR-099 (AWR-14 §6.10; AWR-03 §8.5; D1-AC-21): every D1-core key of the shortcut table is registered once,
// the dispatcher matches physical keys, lets focused components and editable fields keep their keys (only Mod+K and Esc
// pass there), blocks keys under a modal except Esc, and flashes the refusal reason of a guarded binding once.
import { beforeAll, describe, expect, it, vi } from 'vitest'

// the first import of the viewport facade and three.js is slow on a loaded machine
vi.setConfig({ testTimeout: 30_000 })

/** AWR-14 §6.10 D1-core rows (physical key combos; Mod = Ctrl on this platform) */
const TABLE: readonly string[] = [
  'mod+KeyK', 'shift+Slash', 'Escape', 'mod+KeyB', 'Backslash', 'Backquote', 'KeyP',
  'Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5', 'KeyL', 'KeyF', 'KeyN', 'Home',
  'mod+KeyA', 'Period', 'Comma', 'KeyT', 'KeyV', 'KeyG', 'KeyH', 'shift+KeyX', 'shift+KeyR', 'shift+KeyL', 'shift+KeyT', 'Delete',
  'Space', 'BracketLeft', 'BracketRight', 'ArrowRight', 'shift+ArrowRight', 'shift+Period',
]

function key(code: string, o: { ctrl?: boolean; shift?: boolean; target?: unknown } = {}) {
  let prevented = false
  const ev = {
    code, ctrlKey: !!o.ctrl, metaKey: false, shiftKey: !!o.shift, altKey: false, isComposing: false, defaultPrevented: false,
    target: o.target ?? null, preventDefault: () => {
      prevented = true
    },
  }
  return { ev: ev as unknown as KeyboardEvent, prevented: () => prevented }
}

// the builtin actions pull in most of the UI module graph; under the full parallel vitest run the first import can take
// longer than the 10 s default hook timeout (INT-1)
beforeAll(async () => {
  const { registerBuiltinActions } = await import('@/ui/actions/builtin')
  registerBuiltinActions()
}, 60_000)

describe('shortcut table', () => {
  it('registers every D1-core key exactly once', async () => {
    const { listHotkeys } = await import('@/ui/hotkeys/registry')
    const combos = listHotkeys().map((h) => h.combo)
    for (const c of TABLE) expect(combos.filter((x) => x === c), c).toHaveLength(1)
  })

  it('labels every binding and shows key caps', async () => {
    const { listHotkeys, comboLabel } = await import('@/ui/hotkeys/registry')
    const { hasKey } = await import('@/app/i18n')
    for (const h of listHotkeys()) expect(hasKey(h.labelKey), h.id).toBe(true)
    expect(comboLabel('mod+KeyK')).toEqual(['Ctrl', 'K'])
    expect(comboLabel('shift+Slash')).toEqual(['Shift', '/'])
    expect(comboLabel('BracketLeft')).toEqual(['['])
  })
})

describe('dispatcher', () => {
  it('runs a global binding on its physical key', async () => {
    const { dispatchKey, setModalProbe } = await import('@/ui/hotkeys/registry')
    const { prefsStore } = await import('@/stores/prefs')
    setModalProbe(() => false)
    const open = prefsStore.getState().layout.dock.open
    const k = key('Backquote')
    expect(dispatchKey(k.ev)).toBe(true)
    expect(k.prevented()).toBe(true)
    expect(prefsStore.getState().layout.dock.open).toBe(!open)
  })

  it('opens the command palette with Mod+K (editable fields are covered by the browser a11y spec)', async () => {
    const { dispatchKey } = await import('@/ui/hotkeys/registry')
    const { overlays, overlaysStore } = await import('@/ui/shell/overlays')
    overlays.set('palette', false)
    expect(dispatchKey(key('KeyK', { ctrl: true }).ev)).toBe(true)
    expect(overlaysStore.getState().palette).toBe(true)
    overlays.set('palette', false)
  })

  it('blocks keys under a modal except Esc', async () => {
    const { dispatchKey, setModalProbe } = await import('@/ui/hotkeys/registry')
    const { prefsStore } = await import('@/stores/prefs')
    setModalProbe(() => true)
    try {
      const open = prefsStore.getState().layout.dock.open
      expect(dispatchKey(key('Backquote').ev)).toBe(false)
      expect(prefsStore.getState().layout.dock.open).toBe(open)
    } finally {
      setModalProbe(() => false)
    }
  })

  it('flashes the reason of a refused binding (viewer presses H)', async () => {
    const { dispatchKey, setDeniedSink } = await import('@/ui/hotkeys/registry')
    const seen: string[] = []
    setDeniedSink((k) => seen.push(k))
    const k = key('KeyH')
    expect(dispatchKey(k.ev)).toBe(true)
    expect(k.prevented()).toBe(true)
    expect(seen).toEqual(['hint.offline'])
  })
})
