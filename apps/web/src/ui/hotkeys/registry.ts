// Hotkey registry and the single keydown dispatcher (M15-FR-099, §6.11; AWR-14 §6.10; AWR-03 §8.5): bindings match
// KeyboardEvent.code (physical keys); the dispatcher listens once on window in the capture phase. Precedence (AWR-14
// §6.10 conflict rule 4): a focused component's own keys win (arrows, Home/End, Space and Enter inside sliders, tabs,
// radio groups, toolbars, menus, lists, buttons), then the tool, the editor, the focused list or viewport and finally the
// global bindings. While focus is in an editable element only Mod+K and Esc pass; while a modal dialog or a menu is
// open only Esc passes. A binding whose `when` guard fails is not handled, except that `deniedKey` may name a hint that
// is flashed once (for example "read-only mode" when a viewer presses H). Sidebar's built-in Mod+B listener is removed
// by the sidebar-overlay codemod, so this is the only listener.
export type HotkeyScope = 'global' | 'viewport' | 'list' | 'timeline' | 'tool' | 'editor'
export interface HotkeyBinding {
  id: string
  /** combo on physical keys, e.g. 'mod+KeyB', 'Backslash', 'Backquote', 'KeyP', 'mod+KeyK', 'Escape', 'shift+Slash' */
  combo: string
  scope?: HotkeyScope
  allowInEditable?: boolean
  blockedByModal?: boolean
  labelKey: string
  /** help table group (defaults to the action group) */
  group?: string
  when?: () => boolean
  /** i18n key of the hint shown when `when` fails (null: pass the key through silently) */
  deniedKey?: () => string | null
  run: () => void
}

const bindings: HotkeyBinding[] = []
const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
let denied: (key: string) => void = () => {}
/** where denied hints go (the tool hint bar); set at start-up */
export function setDeniedSink(fn: (key: string) => void): void {
  denied = fn
}

export function registerHotkey(b: HotkeyBinding): () => void {
  const i = bindings.findIndex((x) => x.id === b.id)
  if (i >= 0) bindings.splice(i, 1)
  bindings.push(b)
  return () => {
    const k = bindings.indexOf(b)
    if (k >= 0) bindings.splice(k, 1)
  }
}
export const listHotkeys = (): readonly HotkeyBinding[] => bindings

export function comboOf(ev: Pick<KeyboardEvent, 'code' | 'ctrlKey' | 'metaKey' | 'shiftKey' | 'altKey'>): string {
  const mod = isMac ? ev.metaKey : ev.ctrlKey
  return `${mod ? 'mod+' : ''}${ev.altKey ? 'alt+' : ''}${ev.shiftKey ? 'shift+' : ''}${ev.code}`
}

export function isEditableTarget(t: EventTarget | null): boolean {
  if (typeof HTMLElement === 'undefined' || !(t instanceof HTMLElement)) return false
  if (t.isContentEditable) return true
  const tag = t.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t.getAttribute('role') === 'combobox'
}

const COMPOSITE = '[role="slider"],[role="tab"],[role="radio"],[role="menuitem"],[role="menuitemcheckbox"],[role="menuitemradio"],[role="option"],[role="gridcell"],[role="row"],[data-slot="toggle-group-item"],[data-slot="toggle"],[data-rail-row]'
const COMPONENT_KEYS = new Set(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown', 'Space', 'Enter', 'NumpadEnter'])
/** the focused component consumes this key itself (AWR-14 §6.10 rule 4) */
export function isComponentKey(code: string, t: EventTarget | null): boolean {
  if (!COMPONENT_KEYS.has(code) || typeof HTMLElement === 'undefined' || !(t instanceof HTMLElement)) return false
  if (t.closest(COMPOSITE)) return true
  // Space and Enter activate a focused button or link
  if (code === 'Space' || code === 'Enter' || code === 'NumpadEnter') return t.closest('button,a[href],[role="button"],[role="switch"],[role="checkbox"]') !== null
  return false
}

// a popup in its exit transition (Base UI data-closed / data-ending-style) no longer counts as open: Esc followed at once
// by Mod+K must open the palette (INT-1, D1-AC-21)
const OPEN_MODAL = ['[data-slot="dialog-content"]', '[data-slot="alert-dialog-content"]', '[role="menu"]']
  .map((s) => `${s}:not([data-closed]):not([data-ending-style])`).join(',')
let modalOpen = () => typeof document !== 'undefined' && document.querySelector(OPEN_MODAL) !== null
export function setModalProbe(fn: () => boolean): void {
  modalOpen = fn
}

/** dispatch one key event; true when a binding handled it */
export function dispatchKey(ev: KeyboardEvent): boolean {
  if (ev.isComposing || ev.defaultPrevented) return false
  const combo = comboOf(ev)
  const editable = isEditableTarget(ev.target)
  const bare = !ev.ctrlKey && !ev.metaKey && !ev.altKey
  if (bare && isComponentKey(ev.code, ev.target)) return false
  for (let i = bindings.length - 1; i >= 0; i--) {
    const b = bindings[i]
    if (b.combo !== combo) continue
    if (editable && !b.allowInEditable && combo !== 'mod+KeyK' && combo !== 'Escape') return false
    if (modalOpen() && (b.blockedByModal ?? true) && combo !== 'Escape') return false
    if (b.when && !b.when()) {
      const k = b.deniedKey?.() ?? null
      if (k === null) continue
      ev.preventDefault()
      denied(k)
      return true
    }
    ev.preventDefault()
    b.run()
    return true
  }
  return false
}

let installed = false
export function installHotkeys(): () => void {
  if (installed || typeof window === 'undefined') return () => {}
  installed = true
  const on = (e: KeyboardEvent) => {
    dispatchKey(e)
  }
  window.addEventListener('keydown', on, { capture: true })
  return () => {
    installed = false
    window.removeEventListener('keydown', on, { capture: true })
  }
}

const KEY_NAMES: Readonly<Record<string, string>> = {
  Backslash: '\\', Backquote: '`', Slash: '/', Escape: 'Esc', Period: '.', Comma: ',', BracketLeft: '[', BracketRight: ']',
  ArrowLeft: '←', ArrowRight: '→', ArrowUp: '↑', ArrowDown: '↓', Space: 'Space', Delete: 'Delete', Home: 'Home', End: 'End',
}
/** display form of a combo for Kbd (Mod is the platform key) */
export function comboLabel(combo: string): string[] {
  return combo.split('+').map((p) => (p === 'mod' ? (isMac ? 'Cmd' : 'Ctrl') : p === 'shift' ? 'Shift' : p === 'alt' ? 'Alt'
    : p.startsWith('Key') ? p.slice(3) : p.startsWith('Digit') ? p.slice(5) : (KEY_NAMES[p] ?? p)))
}
