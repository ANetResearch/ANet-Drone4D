// Plate geometry uses physical metres and actual metadata; no screen-space identity billboard.
import { CanvasTexture, Group, Mesh, PlaneGeometry, SRGBColorSpace } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { texture, vec4 } from 'three/tsl'

export class VehiclePlates {
  readonly root = new Group()
  private key = ''
  private tex: CanvasTexture | null = null
  private material: MeshBasicNodeMaterial | null = null
  private geometry: PlaneGeometry | null = null

  update(text: string, dimensions: readonly number[]): void {
    const key = JSON.stringify([text, dimensions])
    if (key === this.key) return
    this.clear(); this.key = key
    if (!text) return
    const canvas = document.createElement('canvas')
    canvas.width = 1024; canvas.height = 256
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.fillStyle = 'rgb(240, 240, 230)'; ctx.fillRect(0, 0, 1024, 256)
    ctx.strokeStyle = 'rgb(20, 20, 20)'; ctx.lineWidth = 12; ctx.strokeRect(8, 8, 1008, 240)
    ctx.fillStyle = 'rgb(20, 20, 20)'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
    ctx.font = 'bold 150px sans-serif'; ctx.fillText(text, 512, 138, 950)
    this.tex = new CanvasTexture(canvas); this.tex.colorSpace = SRGBColorSpace
    this.material = new MeshBasicNodeMaterial()
    this.material.colorNode = vec4(texture(this.tex).rgb, 1)
    this.geometry = new PlaneGeometry(.52, .13)
    for (const side of [-1, 1]) {
      const plate = new Mesh(this.geometry, this.material)
      // Plane local +Z faces outward along vehicle +/-X; local Y maps to world Z.
      plate.rotation.set(Math.PI / 2, 0, side > 0 ? Math.PI / 2 : -Math.PI / 2, 'ZYX')
      plate.position.set(side * (dimensions[0] / 2 + .015), 0, Math.min(.65, dimensions[2] * .3))
      this.root.add(plate)
    }
  }
  clear(): void {
    this.root.clear(); this.tex?.dispose(); this.material?.dispose(); this.geometry?.dispose()
    this.tex = null; this.material = null; this.geometry = null
  }
}
