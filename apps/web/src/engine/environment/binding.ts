// Realtime inputs of the environment (M07-FR-030-FR-032, FR-008; M07 §6.6.2; 17 §6.5). Owner: M07.
// env/state keyframes (msgpack, change driven + 1 Hz heartbeat; the default subscription is registered by the drone
// layer) go to EnvStore.ingest; the global epoch of TIME goes to onEpoch (C06); a presets hash mismatch fetches
// GET /api/env/presets once and evaluates with the server copy (C08); the selected vehicle's uav/{id}/env topic is
// subscribed at 10 Hz while it is the primary selection (the gateway only produces EnvSample32 for the interest set).
import { apiGet } from '@/net/api'
import type { RtClient } from '@/net/rt/types'
import type { EnvKeyframeWire } from './state/keyframe'
import { PresetsModel } from './state/presets'
import type { EnvironmentRuntime } from './EnvRuntime'

export class EnvBinding {
  private offs: (() => void)[] = []
  private offSel: (() => void) | null = null
  private selId: string | null = null
  private fetching: string | null = null
  frames = 0
  /** env.warning{code: asset_unavailable} entered and not left (the panel shows "off (asset unavailable)") */
  assetUnavailable = false

  constructor(private readonly rt: () => RtClient | null, private readonly env: EnvironmentRuntime) {}

  install(): () => void {
    const c = this.rt()
    if (!c) return () => {}
    this.offs.push(
      c.onData((m) => {
        if (m.topic !== 'env/state' || !m.data || typeof m.data !== 'object') return
        this.frames++
        try {
          this.env.store.ingest(m.data as EnvKeyframeWire, performance.now())
        } catch (e) {
          if (import.meta.env.DEV) console.warn('env/state frame rejected', e)
        }
        this.maybeFetchPresets()
      }),
      c.onTime((t) => this.env.store.onEpoch(t.epoch)),
      c.onEvents((batch) => {
        for (const ev of batch) {
          if (ev.type === 'env.warning' && ev.data?.code === 'asset_unavailable') this.assetUnavailable = ev.data.state !== 'leave'
        }
      }),
    )
    return () => this.dispose()
  }

  private maybeFetchPresets(): void {
    const want = this.env.store.presetsWanted
    if (!want || this.fetching === want) return
    this.fetching = want
    apiGet<Record<string, unknown>>('/api/env/presets')
      .then((doc) => this.env.store.setPresets(new PresetsModel(doc as never, want)))
      .catch(() => {
        this.fetching = null
      })
  }

  /** primary selection changed: keep uav/{id}/env subscribed at 10 Hz */
  select(id: string | null): void {
    if (id === this.selId) return
    this.selId = id
    this.offSel?.()
    this.offSel = null
    const c = this.rt()
    if (id && c) this.offSel = c.subscribe(`uav/${id}/env`, { rate: 10 })
  }

  dispose(): void {
    for (const o of this.offs) o()
    this.offs = []
    this.offSel?.()
    this.offSel = null
  }
}
