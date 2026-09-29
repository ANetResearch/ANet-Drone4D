// List enter/exit states (M15 §7.1.4; transitions.dev 18 rewritten): for each key reports 'enter' while it is new
// relative to the previous key list, 'idle' for keys that were already present and 'exit' for keys that disappeared
// (kept until the next key list so the caller can play a short exit). Stagger is bounded by --stagger-cap at the call
// site. The previous list is tracked with the "adjust state while rendering" pattern, so no ref is read in render.
import * as React from 'react'

export function listPresence<K>(keys: readonly K[], prev: ReadonlySet<K>): ReadonlyMap<K, 'enter' | 'idle' | 'exit'> {
  const m = new Map<K, 'enter' | 'idle' | 'exit'>()
  const now = new Set(keys)
  for (const k of keys) m.set(k, prev.has(k) ? 'idle' : 'enter')
  for (const k of prev) if (!now.has(k)) m.set(k, 'exit')
  return m
}

export function useListPresence<K>(keys: readonly K[]): ReadonlyMap<K, 'enter' | 'idle' | 'exit'> {
  const [hist, setHist] = React.useState<{ keys: readonly K[]; prev: ReadonlySet<K> }>(() => ({ keys, prev: new Set() }))
  if (hist.keys !== keys) setHist({ keys, prev: new Set(hist.keys) })
  const prev = hist.prev
  return React.useMemo(() => listPresence(keys, prev), [keys, prev])
}
