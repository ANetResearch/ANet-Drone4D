// INT-1 §7.4 (D1-AC-21): the hotkey dispatcher's default modal probe counts an open dialog, alert dialog or menu, but not
// one in its Base UI exit transition (data-closed / data-ending-style), so Esc followed at once by Mod+K opens the
// palette instead of being swallowed by the closing dialog.
import { afterEach, describe, expect, it } from 'vitest'
import { modalOpenInDom } from '@/ui/hotkeys/registry'

let host: HTMLDivElement | null = null
afterEach(() => {
  host?.remove()
  host = null
})

function mount(html: string): HTMLDivElement {
  host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  return host
}

describe('default modal probe', () => {
  it('sees an open dialog, alert dialog and menu', () => {
    expect(modalOpenInDom(mount('<div data-slot="dialog-content" data-open=""></div>'))).toBe(true)
    host!.innerHTML = '<div data-slot="alert-dialog-content"></div>'
    expect(modalOpenInDom(host!)).toBe(true)
    host!.innerHTML = '<div role="menu"></div>'
    expect(modalOpenInDom(host!)).toBe(true)
  })

  it('ignores popups in their exit transition and non-modal content', () => {
    const el = mount('<div data-slot="dialog-content" data-closed=""></div><div role="menu" data-ending-style=""></div>')
    expect(modalOpenInDom(el)).toBe(false)
    el.innerHTML = '<div data-slot="alert-dialog-content" data-ending-style=""></div><div data-slot="popover-content"></div>'
    expect(modalOpenInDom(el)).toBe(false)
  })

  it('turns false the moment the dialog starts closing', () => {
    const el = mount('<div data-slot="dialog-content"></div>')
    const d = el.firstElementChild as HTMLElement
    expect(modalOpenInDom(el)).toBe(true)
    d.setAttribute('data-ending-style', '')
    expect(modalOpenInDom(el)).toBe(false)
  })
})
