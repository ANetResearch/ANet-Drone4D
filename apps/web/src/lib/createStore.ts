// Store factory (M15-FR-030; AWR-18 §6.3 item 2): every store in stores/** and the UI-internal stores is created here, so
// writes are counted per second into __perf.ui.storeWrites (one integer increment, production builds included) and dev
// builds warn when a store writes more than 10 times per second (M15-E007). The init function receives no `set`:
// zustand passes an internal setter that bypasses the wrapped setState (zustand/esm/vanilla.mjs), so all writes must go
// through the returned store's setState; actions are plain functions next to the store (lint STORE-01).
import { createStore, type StoreApi } from 'zustand/vanilla'
import { LIMITS } from './tokens/input.gen'

const counters = new Map<string, Uint32Array>() // [writes this window, writes last window]

export function createAwrStore<T extends object>(name: string, init: () => T): StoreApi<T> {
  const store = createStore<T>()(() => init())
  const counter = new Uint32Array(2)
  counters.set(name, counter)
  const raw = store.setState
  store.setState = ((partial: never, replace?: never) => {
    counter[0]++
    raw(partial, replace)
  }) as StoreApi<T>['setState']
  return store
}

/** Called once per second (governor phase or a timer): rolls the window into perfUi.storeWrites. */
export function rollStoreWrites(perfUi: { storeWrites: Record<string, number> }): void {
  for (const [name, c] of counters) {
    perfUi.storeWrites[name] = c[0]
    c[1] = c[0]
    c[0] = 0
    if (import.meta.env.DEV && c[1] > LIMITS.storeWritesWarnPerS) console.warn(`M15-E007 store ${name} ${c[1]} writes/s`)
  }
}

/** Test helper: current window counts (not reset). */
export function storeWriteCounts(): Record<string, number> {
  const out: Record<string, number> = {}
  for (const [name, c] of counters) out[name] = c[0]
  return out
}
