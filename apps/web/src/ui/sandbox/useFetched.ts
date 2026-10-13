// One sandbox REST resource read for a component (no react-query: the landing page mounts the start Sheet without the
// application providers). The result is keyed by its path, so a path change shows "loading" until the new answer instead
// of resetting state inside an effect; `reload()` asks again.
import * as React from 'react'
import { sbFetch } from './session'
import { freezableInterval } from '@/lib/powerFreeze'

export interface Fetched<T> { data: T | null; error: boolean; loading: boolean; reload: () => void }

export function useFetched<T>(path: string | null): Fetched<T> {
  const [n, setN] = React.useState(0)
  const [st, setSt] = React.useState<{ path: string; n: number; data: T | null; error: boolean } | null>(null)
  React.useEffect(() => {
    if (!path) return
    let alive = true
    void sbFetch<T>('GET', path).then(
      (r) => alive && setSt({ path, n, data: r.status === 200 ? r.body : null, error: r.status !== 200 }),
      () => alive && setSt({ path, n, data: null, error: true }),
    )
    return () => {
      alive = false
    }
  }, [path, n])
  const cur = st && st.path === path ? st : null
  return { data: cur?.data ?? null, error: cur?.error ?? false, loading: path !== null && (cur === null || cur.n !== n), reload: () => setN((x) => x + 1) }
}

/** wall clock re-rendering once a second while mounted (not while the power-save mode froze the page, D2-AC-44) */
export function useSecond(): number {
  const [now, setNow] = React.useState(() => Date.now())
  React.useEffect(() => freezableInterval(() => setNow(Date.now()), 1000), [])
  return now
}
