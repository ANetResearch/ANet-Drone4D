// LoopDriver and RenderSubscriber (M06 §6.6, FR-016..018; R3F 9.8.1 core/loop.ts). Owner: M06.
// LoopDriver installs the R3F advance hook (seconds: frameloop="never" derives delta as timestamp - elapsedTime) and
// starts the engine loop; RenderSubscriber is the only priority-1 useFrame subscriber, so R3F never calls gl.render
// itself and the render phase (RenderBackend.renderFrame) is the single renderer.render caller (AWR-03 §3.6 rule 2).
import { useEffect, type RefObject } from 'react'
import { advance, useFrame, useThree } from '@react-three/fiber'
import { Color, type PerspectiveCamera } from 'three'
import { ctx as frameCtx, loop } from '@/engine'
import { SCENE } from '@/lib/tokens/scene.gen'
import { installHost } from './hostRuntime'
import { applyPendingViewport } from './facade'
import type { RenderBackend } from './renderer'
import { vp } from './session'

export function LoopDriver({ be, hostRef }: { be: RenderBackend; hostRef: RefObject<HTMLDivElement> }) {
  const gl = useThree((s) => s.gl)
  const camera = useThree((s) => s.camera) as PerspectiveCamera
  const scene = useThree((s) => s.scene)
  const size = useThree((s) => s.size)
  useEffect(() => {
    const [r, g, b] = SCENE.clear
    gl.setClearColor(new Color(r, g, b), 1)
    const off = installHost(be, scene, camera, hostRef.current, gl.domElement)
    applyPendingViewport()
    loop.setAdvance((tS) => advance(tS, true)) // seconds (R3F 9.8.1 frameloop="never", AWR-10 §6.3)
    loop.start()
    return () => {
      loop.setAdvance(null)
      off()
    }
  }, [gl, be, camera, scene, hostRef])
  useEffect(() => {
    scene.add(vp.worldRoot)
    return () => {
      scene.remove(vp.worldRoot)
    }
  }, [scene])
  useEffect(() => {
    vp.cssW = size.width
    vp.cssH = size.height
    vp.changed()
  }, [size])
  return null
}

/** the only priority-1 subscriber: R3F no longer calls gl.render itself */
export function RenderSubscriber() {
  useFrame(() => loop.runPhase('render', frameCtx), 1)
  return null
}
