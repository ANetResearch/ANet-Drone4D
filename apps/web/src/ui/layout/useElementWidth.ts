// Width of an element in CSS px (ResizeObserver), for canvas charts that size their backing store to the container.
import * as React from 'react'

export function useElementWidth<T extends HTMLElement>(fallback = 0): [React.RefObject<T | null>, number] {
  const ref = React.useRef<T>(null)
  const [w, setW] = React.useState(fallback)
  React.useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setW(Math.floor(el.clientWidth))
    if (typeof ResizeObserver !== 'function') return
    const ro = new ResizeObserver((es) => {
      const nw = Math.floor(es[0].contentRect.width)
      setW((p) => (p === nw ? p : nw))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}
