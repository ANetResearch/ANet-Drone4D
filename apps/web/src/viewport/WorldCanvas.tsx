// R3F host (ADR-008; AWR-10 §6.2; M06 §6.6, FR-016, FR-021, FR-069; g01 §5, §6.4). Owner: M06.
// Mounted once by app/App.tsx on the --z-canvas layer and never resized by the UI (ADR-028). The renderer comes from the
// async gl factory (createRenderBackend; async is required for drei Html). frameloop="never": the engine loop drives R3F
// through advance(seconds); the only priority-1 useFrame runs the render phase. `flat` keeps tokens un-tone-mapped.
// DPR starts at 0.5 (Tier S) and changes at most once, before the reveal, for Tier B/A (min(dpr, 1.5 iGPU / 2 dGPU)).
// The canvas CSS size is 100 % of the host, the drawing buffer follows the 120 ms debounced resize only. WorldRoot holds
// every registered layer root on the layer's channel. No R3F pointer events anywhere (picking is the engine Picker).
// Device loss or viewport.rebuild() remount the canvas (generation key) and re-create the backend.
import { useEffect, useRef, useState, type ComponentType } from 'react'
import { Canvas, useThree } from '@react-three/fiber'
import type { PerspectiveCamera } from 'three'
import { boot } from '@/app/boot/BootController'
import { createRenderBackend, dprFor, type RenderBackend } from './renderer'
import { channelOf, listLayers, onLayersChanged } from './layers/registry'
import { vp } from './session'
import { forcedFlags, preference } from './backend/testSwitches'
import { assertNoPointerEventsOnWorld, GUARDS_ON } from './backend/guards'
import { LoopDriver, RenderSubscriber } from './LoopDriver'
import { onRebuildRequest, warmupBackend } from './hostRuntime'
import { DronesLayer } from './layers/drones'
import { TrailsLayer } from './layers/trails'
import { GlyphsLayer } from './layers/glyphs'
import { SensorsLayer } from './layers/sensors'
import { MissionLayer } from './layers/mission'
import { ZonesLayer } from './layers/zones'
import { GroundSkyLayer } from './layers/groundSky'
import { DebugLayer } from './layers/debug'
import { PointCloudLayer } from './layers/pointcloud'
import { LabelHost } from './overlay/LabelHost'
import { ViewCube } from './overlay/ViewCube'
import { installInteraction } from './interaction'
import { installTestHooks } from './testHooks'
import { installBindings } from './bindings'

boot.addGate('warmup')
installTestHooks()

// M07 environment adapter (viewport/layers/environment.tsx, owner M07): mounted when present
const envModules = import.meta.glob<{ EnvironmentLayer?: ComponentType<{ be: RenderBackend }> }>('./layers/environment.tsx', { eager: true })
const EnvironmentLayer = Object.values(envModules)[0]?.EnvironmentLayer ?? null

function LayerMounts() {
  const internal = useThree((s) => s.internal)
  useEffect(() => {
    const sync = (): void => {
      const want = new Set(listLayers().map((l) => l.root).filter((r) => r !== null))
      for (const c of [...vp.worldRoot.children]) if (!want.has(c)) vp.worldRoot.remove(c)
      for (const s of listLayers()) {
        const r = s.root
        if (!r) continue
        const ch = channelOf(s)
        r.traverse((o) => o.layers.set(ch))
        if (r.parent !== vp.worldRoot) vp.worldRoot.add(r)
      }
      if (GUARDS_ON) assertNoPointerEventsOnWorld(internal.interaction, vp.worldRoot, listLayers().filter((l) => l.id === 'pointcloud').map((l) => l.root))
    }
    sync()
    return onLayersChanged(sync)
  }, [internal])
  return null
}

/** runs after every layer adapter mounted (last child): self test + shader zoo under the mask */
function WarmupTrigger({ be }: { be: RenderBackend }) {
  const camera = useThree((s) => s.camera) as PerspectiveCamera
  useEffect(() => {
    void warmupBackend(be, camera)
  }, [be, camera])
  return null
}

let backendReady: ((be: RenderBackend) => void) | null = null

export function WorldCanvas() {
  const host = useRef<HTMLDivElement>(null!)
  const [be, setBe] = useState<RenderBackend | null>(null)
  const [dpr, setDpr] = useState(0.5)
  const [gen, setGen] = useState(0)
  useEffect(() => {
    backendReady = (b) => {
      setBe(b)
      const d = dprFor(b.deviceClass, window.devicePixelRatio || 1)
      if (d !== 0.5) setDpr(d) // at most once, before the mask is revealed (M06 §6.6)
    }
    onRebuildRequest(() => {
      vp.generation++
      setBe(null)
      setGen((g) => g + 1)
    })
    const offUi = installInteraction(host.current)
    const offBind = installBindings()
    return () => {
      backendReady = null
      onRebuildRequest(null)
      offUi()
      offBind()
    }
  }, [])
  return (
    <div ref={host} data-viewport="" data-figure="viewport" data-backend={be ? `${be.tier}-${be.deviceClass}` : 'pending'} className="dark app-layer-canvas viewport-root [&_canvas]:h-full! [&_canvas]:w-full!">
      <Canvas key={gen} flat frameloop="never" dpr={dpr} eventSource={host} eventPrefix="client"
        resize={{ scroll: false, debounce: { scroll: 50, resize: 120 } }} camera={{ fov: 60, near: 0.5, far: 20000, position: [0, 120, 240] }}
        style={{ width: '100%', height: '100%' }}
        gl={async ({ canvas }) => {
          const b = await createRenderBackend(canvas as HTMLCanvasElement, { pref: preference(), forced: forcedFlags() })
          backendReady?.(b)
          return b.renderer as never
        }}>
        {be ? (
          <>
            <LoopDriver be={be} hostRef={host} />
            <RenderSubscriber />
            <LayerMounts />
            <PointCloudLayer be={be} />
            <GroundSkyLayer be={be} />
            <ZonesLayer be={be} />
            <DronesLayer be={be} />
            <TrailsLayer />
            <GlyphsLayer />
            <SensorsLayer />
            <MissionLayer />
            <DebugLayer />
            {EnvironmentLayer ? <EnvironmentLayer be={be} /> : null}
            <WarmupTrigger be={be} />
          </>
        ) : null}
      </Canvas>
      <LabelHost />
      <ViewCube />
    </div>
  )
}
