// Isolated draft viewer: no target store, world clock, scene camera or write API access.
import { AmbientLight, Box3, BufferAttribute, BufferGeometry, Color, DirectionalLight, DoubleSide, Mesh, MeshLambertMaterial, PerspectiveCamera, Scene, Vector3, WebGLRenderer } from 'three'
import { SCENE } from '@/lib/tokens/scene.gen'
import { loadTargetAsset, type LoadedTargetAsset, type TargetAsset } from './assetData'
import { VehiclePlates } from './plates'
import { previewDistance } from './previewFraming'

export type PreviewView = 'reset' | 'front' | 'back' | 'left' | 'right' | 'in' | 'out' | 'plate'
export class AppearancePreview {
  private readonly renderer: WebGLRenderer
  private readonly scene = new Scene()
  private readonly camera = new PerspectiveCamera(38, 1, .01, 1000)
  private readonly abort = new AbortController()
  private readonly plates = new VehiclePlates(true)
  private readonly resize: ResizeObserver
  private data?: LoadedTargetAsset
  private mesh?: Mesh<BufferGeometry, MeshLambertMaterial>
  private center = new Vector3()
  private size = new Vector3(1, 1, 1)
  private yaw = -.65
  private pitch = .22
  private zoom = 1
  private plateView = false
  private plate = ''
  private drag?: { id: number; x: number; y: number }
  private disposed = false

  constructor(private readonly canvas: HTMLCanvasElement, private readonly asset: TargetAsset,
    private readonly state: (status: 'loading' | 'ready' | 'error') => void) {
    this.renderer = new WebGLRenderer({ canvas, antialias: true, alpha: false })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    this.scene.background = new Color(...SCENE.gridMinor.rgb)
    this.camera.up.set(0, 0, 1)
    this.scene.add(new AmbientLight(0xffffff, 2))
    const light = new DirectionalLight(0xffffff, 2.2)
    light.position.set(4, -6, 8); this.scene.add(light)
    this.resize = new ResizeObserver(() => this.render())
    this.resize.observe(canvas)
    canvas.addEventListener('pointerdown', this.down)
    canvas.addEventListener('pointermove', this.move)
    canvas.addEventListener('pointerup', this.up)
    canvas.addEventListener('pointercancel', this.up)
    canvas.addEventListener('webglcontextlost', this.contextLost)
    void this.load()
  }
  private async load() {
    this.state('loading')
    try {
      const signal = AbortSignal.any([this.abort.signal, AbortSignal.timeout(45000)])
      const data = await loadTargetAsset(this.asset, signal)
      if (this.disposed) { data.tex.dispose(); data.image.close(); return }
      this.data = data
      const geometry = new BufferGeometry()
      geometry.setAttribute('position', new BufferAttribute(data.p, 3))
      geometry.setAttribute('normal', new BufferAttribute(data.n, 3))
      geometry.setAttribute('uv', new BufferAttribute(data.uv, 2))
      geometry.setAttribute('color', new BufferAttribute(data.c, 3))
      geometry.setIndex(new BufferAttribute(data.indices, 1))
      this.mesh = new Mesh(geometry, new MeshLambertMaterial({ map: data.tex, vertexColors: true, side: DoubleSide }))
      const box = new Box3().setFromObject(this.mesh)
      box.getCenter(this.center); box.getSize(this.size)
      this.mesh.add(this.plates.root); this.scene.add(this.mesh)
      this.setPlate(this.plate)
      this.state('ready')
    } catch {
      if (!this.disposed) this.state('error')
    }
  }
  setPlate(text: string) {
    this.plate = text.trim().toUpperCase()
    if (this.data) this.plates.update(this.asset.class === 'vehicle' ? this.plate : '', this.asset.dimensions_m)
    this.render()
  }
  view(action: PreviewView) {
    if (action === 'plate') this.plateView = true
    else {
      this.plateView = false
      if (action === 'reset') { this.yaw = -.65; this.pitch = .22; this.zoom = 1 }
      if (action === 'front' || action === 'back') { this.yaw = action === 'front' ? 0 : Math.PI; this.pitch = .08; this.zoom = 1 }
      if (action === 'left') this.yaw -= Math.PI / 6
      if (action === 'right') this.yaw += Math.PI / 6
      if (action === 'in') this.zoom = Math.max(.3, this.zoom / 1.2)
      if (action === 'out') this.zoom = Math.min(3, this.zoom * 1.2)
    }
    this.render()
  }
  private render() {
    if (this.disposed) return
    const width = this.canvas.clientWidth, height = this.canvas.clientHeight
    if (!width || !height) return
    this.renderer.setSize(width, height, false)
    this.camera.aspect = width / height
    this.camera.updateProjectionMatrix()
    const target = this.center.clone()
    if (this.plateView) {
      target.set(this.asset.dimensions_m[0] / 2 + .015, 0, Math.min(.65, this.asset.dimensions_m[2] * .3))
      this.camera.position.copy(target).add(new Vector3(1.1, 0, .08))
    } else {
      const distance = previewDistance(this.size.toArray(), this.yaw, this.pitch, this.camera.aspect) * this.zoom
      this.camera.position.copy(target).add(new Vector3(Math.cos(this.yaw) * Math.cos(this.pitch), Math.sin(this.yaw) * Math.cos(this.pitch), Math.sin(this.pitch)).multiplyScalar(distance))
    }
    this.camera.lookAt(target)
    this.renderer.render(this.scene, this.camera)
  }
  private down = (event: PointerEvent) => {
    if (event.button !== 0) return
    this.drag = { id: event.pointerId, x: event.clientX, y: event.clientY }
    this.canvas.setPointerCapture(event.pointerId)
  }
  private move = (event: PointerEvent) => {
    if (!this.drag || this.drag.id !== event.pointerId) return
    this.plateView = false
    this.yaw -= (event.clientX - this.drag.x) * .012
    this.pitch = Math.max(-.3, Math.min(1.3, this.pitch + (event.clientY - this.drag.y) * .01))
    this.drag.x = event.clientX; this.drag.y = event.clientY
    this.render()
  }
  private up = () => { this.drag = undefined }
  private contextLost = (event: Event) => { event.preventDefault(); if (!this.disposed) this.state('error') }
  dispose() {
    this.disposed = true; this.abort.abort(); this.resize.disconnect()
    this.canvas.removeEventListener('pointerdown', this.down)
    this.canvas.removeEventListener('pointermove', this.move)
    this.canvas.removeEventListener('pointerup', this.up)
    this.canvas.removeEventListener('pointercancel', this.up)
    this.canvas.removeEventListener('webglcontextlost', this.contextLost)
    this.plates.clear(); this.mesh?.geometry.dispose(); this.mesh?.material.dispose()
    this.data?.tex.dispose(); this.data?.image.close()
    this.renderer.dispose(); this.renderer.forceContextLoss()
  }
}
