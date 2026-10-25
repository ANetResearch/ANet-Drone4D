// Authored entrance surfaces attached to certified building facades. These represent
// portal locations; the engine explicitly models indoor occupancy rather than rooms.
import { BoxGeometry, CanvasTexture, DoubleSide, Group, Mesh, PlaneGeometry, SRGBColorSpace } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { texture, vec4 } from 'three/tsl'
import type { TargetsModel } from './model'

export class FacilityPortals {
  readonly root = new Group()
  private source: TargetsModel['facilities'] | null = null
  private disposeItems: (() => void)[] = []

  update(model: TargetsModel): void {
    if (this.source === model.facilities) return
    this.clear(); this.source = model.facilities
    for (const f of Object.values(model.facilities)) {
      const p = f.frontage_center, n = f.normal_xy
      if (!f.indoor || !p || p.length !== 3 || !n || n.length !== 2 || ![...p, ...n].every(Number.isFinite)) continue
      const width = f.portal_width_m ?? 1.4, height = f.portal_height_m ?? 2.4
      if (width <= 0 || width > 5 || height <= 0 || height > 5) continue
      const root = new Group()
      root.position.set(p[0] + n[0] * .08, p[1] + n[1] * .08, p[2])
      root.rotation.z = Math.atan2(n[0], -n[1])
      const canvas = document.createElement('canvas'); canvas.width = 512; canvas.height = 1024
      const c = canvas.getContext('2d'); if (!c) continue
      c.fillStyle = 'rgb(38, 53, 58)'; c.fillRect(0, 0, 512, 1024)
      c.strokeStyle = 'rgb(145, 153, 155)'; c.lineWidth = 20; c.strokeRect(12, 12, 488, 1000)
      c.beginPath(); c.moveTo(256, 0); c.lineTo(256, 1024); c.stroke()
      c.fillStyle = 'rgb(182, 188, 190)'; c.fillRect(223, 510, 12, 100); c.fillRect(277, 510, 12, 100)
      const tex = new CanvasTexture(canvas); tex.colorSpace = SRGBColorSpace
      const mat = new MeshBasicNodeMaterial(); mat.colorNode = vec4(texture(tex).rgb, 1); mat.side = DoubleSide
      const geo = new PlaneGeometry(width, height); const door = new Mesh(geo, mat)
      door.rotation.x = Math.PI / 2; door.position.z = height / 2; root.add(door)
      const sign = document.createElement('canvas'); sign.width = 1024; sign.height = 256
      const sc = sign.getContext('2d'); if (!sc) continue
      sc.fillStyle = 'rgb(42, 57, 60)'; sc.fillRect(0, 0, 1024, 256)
      sc.fillStyle = 'rgb(236, 237, 230)'; sc.textAlign = 'center'; sc.textBaseline = 'middle'; sc.font = 'bold 108px sans-serif'
      sc.fillText(f.label, 512, 128, 970)
      const st = new CanvasTexture(sign); st.colorSpace = SRGBColorSpace
      const sm = new MeshBasicNodeMaterial(); sm.colorNode = vec4(texture(st).rgb, 1); sm.side = DoubleSide
      const sg = new PlaneGeometry(width + .8, .55); const plate = new Mesh(sg, sm)
      plate.rotation.x = Math.PI / 2; plate.position.z = height + .4; root.add(plate)
      const stepGeo = new BoxGeometry(width + .2, .5, .08); const stepMat = new MeshBasicNodeMaterial({ color: 0x93918a })
      const step = new Mesh(stepGeo, stepMat); step.position.set(0, -.2, .04); root.add(step)
      this.root.add(root)
      this.disposeItems.push(() => { geo.dispose(); mat.dispose(); tex.dispose(); sg.dispose(); sm.dispose(); st.dispose(); stepGeo.dispose(); stepMat.dispose() })
    }
  }
  clear(): void { this.root.clear(); for (const fn of this.disposeItems) fn(); this.disposeItems = [] }
  dispose(): void { this.clear(); this.source = null }
}
