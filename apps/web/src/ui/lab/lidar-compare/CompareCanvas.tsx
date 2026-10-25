// LiDAR 与重建对比的专用画布（AWR-14 §21.8.2；AWR-05 §7.5）：three.js 渲染器只在本组件中创建，不经 M06 图层注册表。
// 交互：环绕、平移、缩放（OrbitControls，z 向上）；单击点显示坐标与误差；按住 Shift 拖动框选区域，统计框内显示点的误差分布；
// 外部给出 `focus`（沿程曲线的段起点）时把视点移到该处。按需渲染（相机或内容变化时才绘制一帧），Tier S 以 0.5 像素比、无抗锯齿运行。
// 卸载时释放全部几何、材质与 WebGL 上下文。
import * as React from 'react'
import { PerspectiveCamera, type Points, Scene, Vector3, WebGLRenderer, type Object3D } from 'three'
import { isSoftware } from '@/viewport/backend/deviceClass'
import { pickNearest, rectErrStats, type CompareTier, type ColorBy, type DecodedPoints, type ErrStats, type PickHit, type Rect, type Vec3 } from './compare'
import { cropObject, disposeTree, pointsObject, readSceneTokens, styleTruthMesh, trajectoryObject, updatePoints, type SceneTokens } from './scene'

/** 点选半径（CSS px） */
export const PICK_RADIUS_PX = 6
/** 单击与拖动的区分阈值（CSS px） */
const CLICK_SLOP_PX = 3

/**
 * 显示档探测：WebGL2 渲染器字符串为软件光栅（SwiftShader、llvmpipe 等，ADR-044 同一判定）时为 Tier S，否则为 Tier B；无 WebGL2 时返回 null。
 * 探测用的上下文立即释放。
 */
export function probeCompareTier(): CompareTier | null {
  if (typeof document === 'undefined') return null
  const cv = document.createElement('canvas')
  const gl = cv.getContext('webgl2')
  if (!gl) return null
  const ext = gl.getExtension('WEBGL_debug_renderer_info')
  const name = String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER))
  gl.getExtension('WEBGL_lose_context')?.loseContext()
  return isSoftware(name, null) ? 'S' : 'B'
}

export interface CanvasPick { hit: PickHit; layer: DecodedPoints; xyz: Vec3 }
export interface CompareCanvasProps {
  tier: CompareTier
  points: readonly DecodedPoints[]
  mesh: Object3D | null
  trajGt: Float64Array | null
  trajEst: Float64Array | null
  crop: { min: Vec3; max: Vec3 } | null
  showCrop: boolean
  colorBy: ColorBy
  sizePx: number
  focus: Vec3 | null
  width: number
  height: number
  ariaLabel: string
  onPick: (p: CanvasPick | null) => void
  onRect: (s: (ErrStats & { points: number }) | null) => void
}

interface Ctx { renderer: WebGLRenderer; scene: Scene; camera: PerspectiveCamera; controls: { target: Vector3; update: () => boolean; enabled: boolean; dispose: () => void; addEventListener: (t: 'change', f: () => void) => void }; tokens: SceneTokens; render: () => void }

/** 相机对准内容包围盒（裁剪框优先）：自东南上方 45° 俯视 */
function frame(c: Ctx, crop: { min: Vec3; max: Vec3 } | null, objects: readonly Object3D[]): void {
  const lo = new Vector3(Infinity, Infinity, Infinity)
  const hi = new Vector3(-Infinity, -Infinity, -Infinity)
  if (crop) {
    lo.set(...crop.min)
    hi.set(...crop.max)
  } else {
    for (const o of objects) {
      const g = (o as Points).geometry
      g?.computeBoundingBox()
      if (g?.boundingBox) {
        lo.min(g.boundingBox.min)
        hi.max(g.boundingBox.max)
      }
    }
  }
  if (!Number.isFinite(lo.x)) return
  const center = lo.clone().add(hi).multiplyScalar(0.5)
  const r = Math.max(1, hi.clone().sub(lo).length() / 2)
  c.camera.near = Math.max(0.05, r / 2000)
  c.camera.far = r * 20
  c.camera.updateProjectionMatrix()
  c.controls.target.copy(center)
  c.camera.position.copy(center).add(new Vector3(0.6, -0.6, 0.55).normalize().multiplyScalar(r * 2.1))
  c.controls.update()
}

export function CompareCanvas(p: CompareCanvasProps) {
  const host = React.useRef<HTMLDivElement>(null)
  const ctx = React.useRef<Ctx | null>(null)
  const content = React.useRef<Object3D[]>([])
  const framed = React.useRef(false)
  const [rect, setRect] = React.useState<Rect | null>(null)
  const latest = React.useRef(p)
  React.useEffect(() => {
    latest.current = p
  })
  const pixelRatio = p.tier === 'S' ? 0.5 : Math.min(typeof devicePixelRatio === 'number' ? devicePixelRatio : 1, 2)

  // 渲染器、相机与控制器（显示档变化时重建）
  React.useEffect(() => {
    const el = host.current
    if (!el) return
    let disposed = false
    const renderer = new WebGLRenderer({ antialias: p.tier !== 'S', alpha: true, powerPreference: p.tier === 'S' ? 'low-power' : 'high-performance' })
    renderer.setPixelRatio(pixelRatio)
    renderer.setSize(latest.current.width, latest.current.height)
    renderer.domElement.setAttribute('aria-label', latest.current.ariaLabel)
    renderer.domElement.setAttribute('role', 'img')
    renderer.domElement.dataset.compareGl = p.tier
    el.appendChild(renderer.domElement)
    const scene = new Scene()
    const camera = new PerspectiveCamera(50, latest.current.width / Math.max(1, latest.current.height), 0.1, 10_000)
    camera.up.set(0, 0, 1)
    let raf = 0
    const c: Ctx = {
      renderer, scene, camera, tokens: readSceneTokens(),
      controls: { target: new Vector3(), update: () => false, enabled: true, dispose: () => {}, addEventListener: () => {} },
      render: () => {
        if (raf || disposed) return
        raf = requestAnimationFrame(() => {
          raf = 0
          if (!disposed) renderer.render(scene, camera)
        })
      },
    }
    ctx.current = c
    void import('three/addons/controls/OrbitControls.js').then(({ OrbitControls }) => {
      if (disposed) return
      const oc = new OrbitControls(camera, renderer.domElement)
      oc.screenSpacePanning = false
      oc.target.copy(c.controls.target)
      c.controls = oc
      oc.addEventListener('change', c.render)
      framed.current = false
      frame(c, latest.current.crop, content.current)
      framed.current = content.current.length > 0
      c.render()
    })
    return () => {
      disposed = true
      cancelAnimationFrame(raf)
      c.controls.dispose()
      for (const o of content.current) scene.remove(o)
      renderer.dispose()
      renderer.forceContextLoss()
      renderer.domElement.remove()
      ctx.current = null
    }
  }, [p.tier]) // eslint-disable-line react-hooks/exhaustive-deps

  React.useEffect(() => {
    const c = ctx.current
    if (!c) return
    c.renderer.setSize(p.width, p.height)
    c.camera.aspect = p.width / Math.max(1, p.height)
    c.camera.updateProjectionMatrix()
    c.render()
  }, [p.width, p.height])

  // 内容（点云、网格、轨迹、裁剪框）；网格对象归页面所有，这里只挂接
  React.useEffect(() => {
    const c = ctx.current
    if (!c) return
    c.tokens = readSceneTokens()
    const opts = { colorBy: latest.current.colorBy, sizePx: latest.current.sizePx, pixelRatio }
    const built: Object3D[] = p.points.map((L) => pointsObject(L, c.tokens, opts))
    if (p.mesh) built.push(styleTruthMesh(p.mesh, c.tokens))
    if (p.trajGt) built.push(trajectoryObject(p.trajGt, 'gt', c.tokens))
    if (p.trajEst) built.push(trajectoryObject(p.trajEst, 'est', c.tokens))
    if (p.crop && p.showCrop) built.push(cropObject(p.crop, c.tokens))
    for (const o of built) c.scene.add(o)
    content.current = built
    if (!framed.current && built.length) {
      frame(c, p.crop, built)
      framed.current = true
    }
    c.render()
    return () => {
      for (const o of built) {
        c.scene.remove(o)
        if (o !== p.mesh) disposeTree(o)
      }
      content.current = []
    }
  }, [p.points, p.mesh, p.trajGt, p.trajEst, p.crop, p.showCrop, p.tier]) // eslint-disable-line react-hooks/exhaustive-deps

  React.useEffect(() => {
    const c = ctx.current
    if (!c) return
    for (const o of content.current) if ((o as Points).isPoints) updatePoints(o as Points, c.tokens, { colorBy: p.colorBy, sizePx: p.sizePx, pixelRatio })
    c.render()
  }, [p.colorBy, p.sizePx, pixelRatio])

  React.useEffect(() => {
    const c = ctx.current
    if (!c || !p.focus) return
    const offset = c.camera.position.clone().sub(c.controls.target)
    c.controls.target.set(...p.focus)
    c.camera.position.copy(c.controls.target).add(offset)
    c.controls.update()
    c.render()
  }, [p.focus])

  const viewProj = (): Float64Array | null => {
    const c = ctx.current
    if (!c) return null
    c.camera.updateMatrixWorld()
    const m = c.camera.projectionMatrix.clone().multiply(c.camera.matrixWorldInverse)
    return Float64Array.from(m.elements)
  }
  const down = React.useRef<{ x: number; y: number; shift: boolean } | null>(null)
  const local = (e: React.PointerEvent) => {
    const r = e.currentTarget.getBoundingClientRect()
    return { x: e.clientX - r.left, y: e.clientY - r.top }
  }
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const q = local(e)
    down.current = { ...q, shift: e.shiftKey }
    if (e.shiftKey && ctx.current) {
      ctx.current.controls.enabled = false
      setRect({ x0: q.x, y0: q.y, x1: q.x, y1: q.y })
      e.currentTarget.setPointerCapture(e.pointerId)
    }
  }
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!down.current?.shift) return
    const q = local(e)
    setRect((r) => (r ? { ...r, x1: q.x, y1: q.y } : r))
  }
  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = down.current
    down.current = null
    if (!d) return
    const q = local(e)
    const m = viewProj()
    if (ctx.current) ctx.current.controls.enabled = true
    if (d.shift) {
      setRect(null)
      if (m && Math.abs(q.x - d.x) > CLICK_SLOP_PX && Math.abs(q.y - d.y) > CLICK_SLOP_PX) p.onRect(rectErrStats(p.points, m, p.width, p.height, { x0: d.x, y0: d.y, x1: q.x, y1: q.y }))
      return
    }
    if (!m || Math.hypot(q.x - d.x, q.y - d.y) > CLICK_SLOP_PX) return
    const hit = pickNearest(p.points, m, p.width, p.height, q.x, q.y, PICK_RADIUS_PX)
    const L = hit ? p.points[hit.layer] : undefined
    p.onPick(hit && L ? { hit, layer: L, xyz: [L.pos[3 * hit.i]!, L.pos[3 * hit.i + 1]!, L.pos[3 * hit.i + 2]!] } : null)
  }

  return (
    <div ref={host} data-compare-canvas={p.tier} className="relative touch-none select-none overflow-hidden" style={{ width: p.width, height: p.height }}
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}>
      {rect ? (
        <div data-compare-rect="" className="pointer-events-none absolute border border-foreground"
          style={{ left: Math.min(rect.x0, rect.x1), top: Math.min(rect.y0, rect.y1), width: Math.abs(rect.x1 - rect.x0), height: Math.abs(rect.y1 - rect.y0) }} />
      ) : null}
    </div>
  )
}
