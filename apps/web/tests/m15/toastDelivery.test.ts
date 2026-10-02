// ADR-069 (D1-AC-27; M15-FR-089): notify() records the latest content of a merge key and the toast manager is called
// after the next presented frame (here: the next macrotask, Node has no requestAnimationFrame); a live toast is rewritten
// at most once per TOAST_UPDATE_MIN_MS with the latest content, identical content is not rewritten, a level change goes
// out at once, a key whose toast was removed starts over with an add, and one delivery writes at most one toast.
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
})
