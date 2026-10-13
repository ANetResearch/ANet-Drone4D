// Sensor view inputs from the domain stores (d2-WP-11-to-M15 items 8 and 10; AWR-14 §20.10.5): the environment summary's
// sky (M07: illuminance, surface temperature and emissivity, sun direction; the calendar of the session) feeds the
// sensor view composite at most 4 times per second (night view brightness, LWIR background), and the catalogue's model
// details (M21: frame type, size, motor temperature rise) register the vehicle shapes of the drone layer. Local display
// state only: viewers use it freely. Installed once at start-up (app/shell/install.ts).
import { catalogStore } from '@/stores/catalog'
import { envStore } from '@/stores/env'
import { sensorView, vehicleModels } from '@/viewport/facade'
import { shapeOf, sunEnuOf } from './hudModel'

const ENV_MS = 250

interface SkyView { illumLx: number; sunAzDeg: number; sunElDeg: number; surface: { tC: number; emissivity: number } | null }

let installed = false
export function installSensorBindings(): () => void {
  if (installed) return () => {}
  installed = true
  let lastMs = 0
  let pending: ReturnType<typeof setTimeout> | null = null
  const pushEnv = () => {
    pending = null
    lastMs = Date.now()
    const sky = (envStore.getState() as unknown as { sky?: SkyView | null }).sky
    if (!sky || !Number.isFinite(sky.illumLx)) return
    sensorView.setEnv({
      illumLx: sky.illumLx, sunEnu: sunEnuOf(sky.sunAzDeg, sky.sunElDeg),
      ...(sky.surface ? { tSurfaceC: sky.surface.tC, emissivityBg: sky.surface.emissivity } : {}),
    })
  }
  const offEnv = envStore.subscribe(() => {
    if (pending) return
    pending = setTimeout(pushEnv, Math.max(0, ENV_MS - (Date.now() - lastMs)))
  })
  const registered = new Set<string>()
  const offCat = catalogStore.subscribe((s) => {
    for (const [id, d] of s.details) {
      if (registered.has(id)) continue
      const sh = shapeOf(d)
      if (!sh) continue
      registered.add(id)
      vehicleModels.register(id, sh)
    }
  })
  return () => {
    offEnv()
    offCat()
    if (pending) clearTimeout(pending)
    installed = false
  }
}
