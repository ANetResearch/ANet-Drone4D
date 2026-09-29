// Epoch and RESET notifications (M12 §7.1, FR-006; AWR-17 §6.10 epoch table; AWR-03 §5.2 item 4). Owner: M12.
// onEpoch fires synchronously when the TIME of a new global epoch is ingested (seek, scenario reset, restart without a
// checkpoint, live <-> replay): the interpolation ring empties, M06 clears trails, event views snap, M07's EnvStore enters
// EPOCH_WAIT. onReset fires for a record with rflags.RESET (checkpoint restore of one producer): only that producer's
// agent range [idBase, idBase + idCount) is cleared. Listeners run in registration order; a throwing listener is isolated.
export interface ResetRange { idBase: number; idCount: number }

const epochCbs: ((epoch: number) => void)[] = []
const resetCbs: ((r: ResetRange) => void)[] = []

export function onEpoch(cb: (epoch: number) => void): () => void {
  epochCbs.push(cb)
  return () => {
    const i = epochCbs.indexOf(cb)
    if (i >= 0) epochCbs.splice(i, 1)
  }
}

export function onReset(cb: (r: ResetRange) => void): () => void {
  resetCbs.push(cb)
  return () => {
    const i = resetCbs.indexOf(cb)
    if (i >= 0) resetCbs.splice(i, 1)
  }
}

export const epochBus = {
  emitEpoch(epoch: number): void {
    for (let i = 0; i < epochCbs.length; i++) {
      try {
        epochCbs[i](epoch)
      } catch (e) {
        console.error('onEpoch listener failed', e)
      }
    }
  },
  emitReset(r: ResetRange): void {
    for (let i = 0; i < resetCbs.length; i++) {
      try {
        resetCbs[i](r)
      } catch (e) {
        console.error('onReset listener failed', e)
      }
    }
  },
  /** listener counts (tests) */
  sizes(): { epoch: number; reset: number } {
    return { epoch: epochCbs.length, reset: resetCbs.length }
  },
}
