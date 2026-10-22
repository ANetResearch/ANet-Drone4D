// Optional mesh pack. Target state, identity and heading remain authoritative server data.
import { BufferAttribute, BufferGeometry, DoubleSide, Group, Mesh } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { attribute, normalWorld, normalize, positionWorld, texture, vec4 } from 'three/tsl'
import { TARGET_ASSETS, loadTargetAsset, type TargetAsset, type LoadedTargetAsset as Loaded } from './assetData'
export { TARGET_ASSETS } from './assetData'
export type { TargetAsset } from './assetData'
import { onSceneShading, sceneShading } from '../shading'
import type { TargetsModel } from './model'
import { VehiclePlates } from './plates'
import { rollWheels } from './wheels'

export const APPEARANCE_ENABLED = import.meta.env.VITE_AWR_TARGET_ASSETS === '1'
export type AssetStatus = 'idle' | 'loading' | 'ready' | 'error'
const statuses = new Map<string, AssetStatus>()
const listeners = new Set<() => void>()
const retryListeners = new Set<(id: string) => void>()
export const subscribeAssets = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn) } }
export const assetStatus = (id: string) => statuses.get(id) ?? 'idle'
function status(id: string, value: AssetStatus) { statuses.set(id, value); for (const fn of listeners) fn() }
export function assetFor(id: unknown): TargetAsset | undefined { return TARGET_ASSETS.find((a) => a.id === id) }
/** Retry a failed download without resetting actor identity or the running world. */
export function retryAsset(id: string): void {
  if (assetStatus(id) !== 'error') return
  for (const fn of retryListeners) fn(id)
}

interface Actor { plates: VehiclePlates; key: string; mesh: Mesh<BufferGeometry, MeshBasicNodeMaterial>; data: Loaded; version: number; detailVersion: number; distance: number; lastTime: number; x: number; y: number }

function material(data: Loaded): MeshBasicNodeMaterial {
  const m = new MeshBasicNodeMaterial()
  m.side = DoubleSide
  m.colorNode = vec4(texture(data.tex).rgb.mul(attribute('color', 'vec3')).mul(sceneShading().lambert(normalize(normalWorld), positionWorld)), 1)
  m.fog = true
  return m
}

/** One mesh per persistent target id; shared immutable source buffers/textures per asset. */
export class TargetAppearances {
  readonly root = new Group()
  private readonly cache = new Map<string, Loaded>()
  private readonly pending = new Set<string>()
  private readonly failed = new Set<string>()
  private readonly actors = new Map<string, Actor>()
  private readonly abort = new AbortController()
  private readonly offShading = onSceneShading(() => {
    for (const a of this.actors.values()) { a.mesh.material.dispose(); a.mesh.material = material(a.data) }
  })
  private readonly retry = (id: string) => {
    if (this.failed.delete(id)) status(id, 'idle')
  }

  constructor() { retryListeners.add(this.retry) }

  private async load(asset: TargetAsset): Promise<void> {
    this.pending.add(asset.id)
    status(asset.id, 'loading')
    try {
      const data = await loadTargetAsset(asset, this.abort.signal)
      if (this.abort.signal.aborted) { data.tex.dispose(); data.image.close(); return }
      this.cache.set(asset.id, data)
      status(asset.id, 'ready')
    } catch (e) {
      if (!this.abort.signal.aborted) { this.failed.add(asset.id); status(asset.id, 'error'); console.warn(`Target asset ${asset.id} unavailable`, e) }
    } finally {
      this.pending.delete(asset.id)
    }
  }

  has(id: string): boolean { return this.actors.has(id) }
  drawCount(): number { return [...this.actors.values()].filter((a) => a.mesh.visible).length }

  update(model: TargetsModel): void {
    const live = new Set<string>()
    const r = model.rows
    for (let i = 0; i < r.n; i++) {
      const id = model.idOfRow(i)
      const cfg = model.cfgs.get(id)
      const binding = cfg?.appearance
      const spec = assetFor(binding?.asset_id)
      if (!spec || spec.class !== model.classOfRow(i) || binding?.geometry_sha256 !== spec.geometry_sha256) continue
      live.add(id)
      let actor = this.actors.get(id)
      if (actor && actor.key !== spec.id) { this.remove(id); actor = undefined }
      const data = this.cache.get(spec.id)
      if (!data) { if (this.pending.size < 2 && !this.pending.has(spec.id) && !this.failed.has(spec.id)) void this.load(spec); continue }
      if (!actor) {
        const geometry = new BufferGeometry()
        geometry.setAttribute('position', new BufferAttribute(data.p.slice(), 3))
        geometry.setAttribute('normal', new BufferAttribute(data.wheels ? data.n.slice() : data.n, 3))
        geometry.setAttribute('uv', new BufferAttribute(data.uv, 2))
        geometry.setAttribute('color', new BufferAttribute(data.c, 3))
        geometry.setIndex(new BufferAttribute(data.indices, 1))
        const mesh = new Mesh(geometry, material(data))
        mesh.name = `target-appearance:${id}`
        mesh.frustumCulled = false
        this.root.add(mesh)
        const plates = new VehiclePlates()
        mesh.add(plates.root)
        actor = { plates, key: spec.id, mesh, data, version: -1, detailVersion: -1, distance: 0, lastTime: model.tSimMs, x: r.pos[i * 3], y: r.pos[i * 3 + 1] }
        this.actors.set(id, actor)
      }
      if (actor.version === model.version && actor.detailVersion === model.detailVersion) continue
      const x = r.pos[3 * i], y = r.pos[3 * i + 1], z = r.pos[3 * i + 2]
      const dt = (model.tSimMs - actor.lastTime) / 1000
      const moved = Math.hypot(x - actor.x, y - actor.y)
      // Placement/seek must not count as walking; only contiguous simulation displacement drives gait.
      if (dt > 0 && dt < 2 && moved < Math.max(0.5, r.speed[i] * dt * 3)) actor.distance += moved
      if (dt < 0) actor.distance = 0
      // Life gait resumes from server progress after a refresh/seek. Client-only
      // accumulation would give the same actor a different pose in each viewer.
      const detail = model.details.get(id)
      if (detail?.activity && detail.t_sim_ns !== undefined) {
        const elapsed = Math.max(0, Math.min(1.5, (model.tSimMs - detail.t_sim_ns / 1e6) / 1000))
        actor.distance = detail.activity.walked_m + r.speed[i] * elapsed
      }
      actor.lastTime = model.tSimMs; actor.x = x; actor.y = y
      actor.version = model.version; actor.detailVersion = model.detailVersion
      actor.mesh.visible = r.enabled[i] === 1 && r.hidden[i] === 0
      actor.mesh.position.set(x, y, z)
      actor.mesh.rotation.z = r.heading[i]
      actor.plates.update(spec.class === 'vehicle' ? cfg?.metadata?.plate_text ?? '' : '', spec.dimensions_m)
      const size = model.sizeOf(id, model.classOfRow(i))
      actor.mesh.scale.set(size[0] / spec.dimensions_m[0], size[1] / spec.dimensions_m[1], size[2] / spec.dimensions_m[2])
      if (spec.poses > 0) {
        const dst = actor.mesh.geometry.getAttribute('position') as BufferAttribute
        const p = dst.array as Float32Array
        if (r.moving[i] && r.speed[i] > 0.05) {
          const phase = ((actor.distance / spec.stride_m) % 1) * spec.poses
          const a = Math.floor(phase), b = (a + 1) % spec.poses, w = phase - a
          for (let j = 0; j < p.length; j++) p[j] = data.poses[a * p.length + j] * (1 - w) + data.poses[b * p.length + j] * w
        } else p.set(data.p)
        dst.needsUpdate = true
      } else if (data.wheels) {
        const position = actor.mesh.geometry.getAttribute('position') as BufferAttribute
        const normal = actor.mesh.geometry.getAttribute('normal') as BufferAttribute
        rollWheels(data.p, data.n, position.array as Float32Array, normal.array as Float32Array, data.wheels, actor.distance)
        position.needsUpdate = true; normal.needsUpdate = true
      }
    }
    for (const id of this.actors.keys()) if (!live.has(id)) this.remove(id)
  }

  private remove(id: string): void {
    const a = this.actors.get(id)
    if (!a) return
    a.plates.clear(); this.root.remove(a.mesh); a.mesh.geometry.dispose(); a.mesh.material.dispose(); this.actors.delete(id)
  }
  dispose(): void {
    this.abort.abort(); this.offShading(); retryListeners.delete(this.retry)
    for (const id of this.actors.keys()) this.remove(id)
    for (const data of this.cache.values()) { data.tex.dispose(); data.image.close() }
    for (const id of new Set([...this.pending, ...this.cache.keys(), ...this.failed])) status(id, 'idle')
    this.cache.clear()
  }
}
