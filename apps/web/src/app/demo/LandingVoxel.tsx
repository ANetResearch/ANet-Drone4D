// Host of the story stage (D2.2 landing story): the canvas of the voxel airplane, inside the sticky .landing-stage of
// ./LandingStory. The CSS grid of .landing-stage paints at once (and stays as the static fallback); the renderer
// (./voxelField, its own lazy chunk) starts on idle so it never competes with the first paint, and the canvas fades in
// on its first frame.
import * as React from 'react'

type IdleWindow = typeof globalThis & {
  requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number
  cancelIdleCallback?: (id: number) => void
}

export function LandingVoxel() {
  const ref = React.useRef<HTMLCanvasElement>(null)
  React.useEffect(() => {
    const el = ref.current
    if (!el) return
    let alive = true
    let stop: (() => void) | null = null
    const start = (): void => {
      void import('./voxelField').then((m) => {
        if (!alive) return
        const h = m.startVoxelField(el)
        stop = () => h.stop()
      }, () => {
        el.dataset.voxelMode = 'static'
      })
    }
    const w = globalThis as IdleWindow
    const idle = typeof w.requestIdleCallback === 'function' ? w.requestIdleCallback(start, { timeout: 1200 }) : null
    const timer = idle === null ? setTimeout(start, 200) : null
    return () => {
      alive = false
      if (idle !== null) w.cancelIdleCallback?.(idle)
      if (timer !== null) clearTimeout(timer)
      stop?.()
    }
  }, [])
  return <canvas ref={ref} className="landing-voxel" data-voxel-bg="" />
}
