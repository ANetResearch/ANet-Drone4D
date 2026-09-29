// Overlay pages cap the canvas at 5 fps while they cover >= 90% of the viewport (M15-FR-007; AWR-14 §2.1 rule 2).
import * as React from 'react'
import { frameCap } from '@/ui/shell/frameCap'

const OVERLAY_FPS = 5
export function useFrameCapOverlay(source: string): void {
  React.useEffect(() => {
    frameCap.set(source, OVERLAY_FPS)
    return () => frameCap.set(source, 0)
  }, [source])
}
