// Power-save freeze signal (D2-FX-R2-web; R-D2-33; ADR-121; AWR-04 §15.7.3 SAVING row "stop the 1 Hz texts", §15.7.7;
// D2-AC-44, D2-AC-46). Owner: M15.
// While the page is in the power-save mode (SAVING, and QUEUED static browsing) the last state is on screen and nothing
// changes it: the 4 Hz evaluations (red arbitration of the figures and the viewport, the event bridge flush) and the 1 Hz
// clocks have nothing to do, yet each of their timer tasks costs the renderer main thread (D2-ACC-1: 4.05 % busy over 10 s
// of saving, the threshold is 2 %). app/power/PowerController.ts sets the signal; periodic work registers through
// freezableInterval(), whose interval is cleared while frozen and started again (with one immediate run) when the page
// resumes. The module has no imports so that lib/, ui/ and viewport/ modules can use it.

let frozen = false
const subs = new Set<(on: boolean) => void>()

/** PowerController: entering the frozen states (true) and leaving them (false) */
export function setPowerFrozen(on: boolean): void {
  if (on === frozen) return
  frozen = on
  for (const cb of [...subs]) cb(on)
}

export function powerFrozen(): boolean {
  return frozen
}

/** notified on every change; returns the unsubscribe */
export function onPowerFrozen(cb: (on: boolean) => void): () => void {
  subs.add(cb)
  return () => {
    subs.delete(cb)
  }
}

/**
 * setInterval that does not run while the page is frozen by the power-save mode; when the page resumes `fn` runs once at
 * once (the state may have changed while frozen) and the interval starts again. Returns the stop function.
 */
export function freezableInterval(fn: () => void, ms: number): () => void {
  let h: ReturnType<typeof setInterval> | null = frozen ? null : setInterval(fn, ms)
  const off = onPowerFrozen((on) => {
    if (on) {
      if (h) clearInterval(h)
      h = null
    } else if (!h) {
      h = setInterval(fn, ms)
      fn()
    }
  })
  return () => {
    off()
    if (h) clearInterval(h)
    h = null
  }
}
