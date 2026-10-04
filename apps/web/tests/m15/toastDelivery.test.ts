// ADR-069, ADR-081 (D1-AC-27, D1-AC-03a; M15-FR-089): notify() records the latest content of a merge key and the toast
// manager is called in the post-layout slot of the next frame (a ResizeObserver callback; here the next macrotask, Node
// has no ResizeObserver, and a fake observer for the slot itself); a live toast is rewritten at most once per
// TOAST_UPDATE_MIN_MS with the latest content, identical content is not rewritten, a level change goes out at once, a key
// whose toast was removed starts over with an add, and one delivery writes at most one toast. Without a toast surface
// (?chrome=0) notify() takes the non-DOM channel and never calls the toast manager.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.setConfig({ testTimeout: 30_000 })

describe('toast delivery (ADR-069)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('adds after the next frame, coalesces updates to one per interval, skips identical content', async () => {
    const { toast } = await import('@/ui/components/ui/toast')
    const { notify, TOAST_UPDATE_MIN_MS } = await import('@/app/providers/ToastProvider')
    const add = vi.spyOn(toast, 'add').mockImplementation(() => 'x')
    const update = vi.spyOn(toast, 'update').mockImplementation(() => {})
    notify('k1', 'warning', 'a')
    expect(add).not.toHaveBeenCalled()            // never inside the caller's task (the bridge flush)
    await vi.advanceTimersByTimeAsync(1)
    expect(add).toHaveBeenCalledTimes(1)
    expect(add.mock.calls[0][0]).toMatchObject({ id: 'k1', title: 'a', type: 'warning' })
    notify('k1', 'warning', 'b')
    notify('k1', 'warning', 'c')
    await vi.advanceTimersByTimeAsync(1)
    expect(update).not.toHaveBeenCalled()         // within the interval: held
    await vi.advanceTimersByTimeAsync(TOAST_UPDATE_MIN_MS)
    expect(update).toHaveBeenCalledTimes(1)
    expect(update.mock.calls[0]).toEqual(['k1', expect.objectContaining({ title: 'c' })])
    await vi.advanceTimersByTimeAsync(TOAST_UPDATE_MIN_MS)
    notify('k1', 'warning', 'c')                  // same content as shown: no rewrite
    await vi.advanceTimersByTimeAsync(TOAST_UPDATE_MIN_MS + 1)
    expect(update).toHaveBeenCalledTimes(1)
  })

  it('sends a level change at once and starts over after the toast was removed', async () => {
    const { toast } = await import('@/ui/components/ui/toast')
    const { notify } = await import('@/app/providers/ToastProvider')
    let onRemove: (() => void) | undefined
    const add = vi.spyOn(toast, 'add').mockImplementation((o: { onRemove?: () => void }) => {
      onRemove = o.onRemove
      return 'x'
    })
    const update = vi.spyOn(toast, 'update').mockImplementation(() => {})
    notify('k2', 'warning', 'w')
    await vi.advanceTimersByTimeAsync(1)
    notify('k2', 'critical', 'c')
    await vi.advanceTimersByTimeAsync(1)
    expect(update).toHaveBeenCalledTimes(1)
    expect(update.mock.calls[0][1]).toMatchObject({ type: 'error', timeout: 0 })
    onRemove?.()
    notify('k2', 'warning', 'again')
    await vi.advanceTimersByTimeAsync(1)
    expect(add).toHaveBeenCalledTimes(2)
  })

  it('writes one toast per delivery and takes the next key in the following one', async () => {
    const { toast } = await import('@/ui/components/ui/toast')
    const { notify } = await import('@/app/providers/ToastProvider')
    const add = vi.spyOn(toast, 'add').mockImplementation(() => 'x')
    notify('k3', 'warning', 'one')
    notify('k4', 'warning', 'two')
    await vi.advanceTimersToNextTimerAsync()     // one delivery (Node: a macrotask instead of rAF + idle)
    expect(add).toHaveBeenCalledTimes(1)
    await vi.advanceTimersToNextTimerAsync()
    expect(add).toHaveBeenCalledTimes(2)
    expect(add.mock.calls.map((c) => (c[0] as { id: string }).id)).toEqual(['k3', 'k4'])
  })

  it('without a surface (?chrome=0) records the notice and never calls the toast manager', async () => {
    const { toast } = await import('@/ui/components/ui/toast')
    const { notify, setSurface, headlessNotices, HEADLESS_KEEP } = await import('@/app/providers/ToastProvider')
    const { UX } = await import('@/ui/testing/uxProbe')
    const add = vi.spyOn(toast, 'add').mockImplementation(() => 'x')
    const before = UX.toasts.headless
    setSurface(false)
    try {
      notify('governor', 'info', 'floor released')
      await vi.advanceTimersByTimeAsync(TOAST_SETTLE_MS)
      expect(add).not.toHaveBeenCalled()
      expect(headlessNotices().at(-1)).toMatchObject({ key: 'governor', level: 'info', title: 'floor released' })
      expect(UX.toasts.headless).toBe(before + 1)
      for (let i = 0; i < HEADLESS_KEEP + 4; i++) notify(`k${i}`, 'warning', `n${i}`)
      expect(headlessNotices()).toHaveLength(HEADLESS_KEEP)
      expect(headlessNotices().at(-1)?.title).toBe(`n${HEADLESS_KEEP + 3}`)
    } finally {
      setSurface(true)
    }
    notify('governor', 'info', 'again')
    await vi.advanceTimersByTimeAsync(TOAST_SETTLE_MS)
    expect(add).toHaveBeenCalledTimes(1)
  })
})

const TOAST_SETTLE_MS = 5

describe('frame sharing of toast writes (ADR-081)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.resetModules()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('a write waits while our periodic UI work just ran, at most TOAST_MAX_DEFER deliveries', async () => {
    const { toast } = await import('@/ui/components/ui/toast')
    const { notify, TOAST_MAX_DEFER } = await import('@/app/providers/ToastProvider')
    const { noteUiWork, UI_WORK_GAP_MS } = await import('@/ui/notify/afterLayout')
    const add = vi.spyOn(toast, 'add').mockImplementation(() => 'x')
    noteUiWork()                                  // e.g. the event bridge flush of this frame
    notify('k5', 'warning', 'after the flush')
    await vi.advanceTimersByTimeAsync(1)
    expect(add).not.toHaveBeenCalled()            // deferred to a later delivery
    await vi.advanceTimersByTimeAsync(UI_WORK_GAP_MS)
    expect(add).toHaveBeenCalledTimes(1)          // the work is older than the gap now
    // work that never stops (a flush in every frame): the write still goes out after TOAST_MAX_DEFER deliveries
    notify('k6', 'warning', 'busy')
    for (let k = 0; k < TOAST_MAX_DEFER; k++) {
      noteUiWork()
      await vi.advanceTimersToNextTimerAsync()
    }
    noteUiWork()
    await vi.advanceTimersToNextTimerAsync()
    expect(add).toHaveBeenCalledTimes(2)
  })
})

describe('post-layout slot (ADR-081)', () => {
  const g = globalThis as unknown as Record<string, unknown>
  afterEach(() => {
    delete g.ResizeObserver
    delete g.document
    vi.resetModules()
  })

  it('runs the queued callbacks in the resize observation of the next frame, once per frame, in order', async () => {
    vi.resetModules()
    let observer: (() => void) | null = null
    const style: Record<string, string> = {}
    const el = { setAttribute() {}, dataset: {} as Record<string, string>, style, isConnected: true }
    const appended: unknown[] = []
    g.document = { hidden: false, body: { appendChild: (x: unknown) => appended.push(x) }, createElement: () => el }
    g.ResizeObserver = class {
      constructor(cb: () => void) {
        observer = cb
      }
      observe() {}
      disconnect() {}
    }
    const { afterLayout } = await import('@/ui/notify/afterLayout')
    const ran: string[] = []
    afterLayout(() => ran.push('a'))
    afterLayout(() => ran.push('b'))
    expect(appended).toEqual([el])                // one sentinel, a direct child of <body>
    expect(style.width).toBe('2px')               // the request flipped its width: a frame is scheduled
    await Promise.resolve()
    expect(ran).toEqual([])                       // nothing outside the rendering steps
    observer!()                                   // the frame's resize observations (layout is clean here)
    expect(ran).toEqual(['a', 'b'])
    afterLayout(() => ran.push('c'))
    expect(style.width).toBe('1px')
    observer!()
    observer!()                                   // an observation without a request runs nothing
    expect(ran).toEqual(['a', 'b', 'c'])
  })

  it('falls back to a macrotask when the page is hidden and to a timer when no frame comes', async () => {
    vi.useFakeTimers()
    vi.resetModules()
    const el = { setAttribute() {}, dataset: {}, style: {} as Record<string, string>, isConnected: true }
    const doc = { hidden: true, body: { appendChild() {} }, createElement: () => el }
    g.document = doc
    g.ResizeObserver = class {
      observe() {}
      disconnect() {}
    }
    const { afterLayout, AFTER_LAYOUT_FALLBACK_MS } = await import('@/ui/notify/afterLayout')
    const ran: string[] = []
    afterLayout(() => ran.push('hidden'))
    await vi.advanceTimersByTimeAsync(1)
    expect(ran).toEqual(['hidden'])
    doc.hidden = false
    afterLayout(() => ran.push('no frame'))
    await vi.advanceTimersByTimeAsync(AFTER_LAYOUT_FALLBACK_MS - 10)
    expect(ran).toEqual(['hidden'])
    await vi.advanceTimersByTimeAsync(20)
    expect(ran).toEqual(['hidden', 'no frame'])
    vi.useRealTimers()
  })
})
