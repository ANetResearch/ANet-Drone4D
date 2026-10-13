// HUD capture boxes (D2 WP-11; AWR-14 §20.10.6 "捕获框：视锥内识别物的投影包围盒（由 TargetLite32 位置与类别尺寸在客户端
// 投影）"; AWR-04 §6.4). Owner: M06. M15 draws the boxes (DOM) and reads their rectangles here: the 8 corners of the
// target's yawed box (ENU centre, l x w x h, yaw) projected with the current viewport camera (the FPV sensor camera in
// a sensor view) to CSS px of the viewport, clipped to it; the LWIR digital zoom (display only) scales about the image
// centre as the composite does. Corners behind the near plane are dropped; a box entirely behind or outside the view
// returns false. Zero allocation.
import { Vector3 } from 'three'
import { vp } from '../session'
import { sensorViewActive, sv } from './state'

const v = new Vector3()

/**
 * CSS px rectangle [x0, y0, x1, y1] (origin top-left of the viewport) of a yawed box around an ENU centre; false when no
 * part of it is in front of the camera and inside the viewport. yaw is the ENU heading of the box's length (rad, CCW
 * from east)
 */
export function projectBox(center: ArrayLike<number>, sizeLwh: ArrayLike<number>, yawRad: number, out: Float32Array): boolean {
  const cam = vp.camera
  const W = vp.cssW
  const H = vp.cssH
  if (!cam || W <= 0 || H <= 0) return false
  const hl = sizeLwh[0] / 2, hw = sizeLwh[1] / 2, hh = sizeLwh[2] / 2
  const c = Math.cos(yawRad), s = Math.sin(yawRad)
  const z = sensorViewActive() && sv.mode === 'lwir' ? sv.digitalZoom : 1
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  let n = 0
  for (let k = 0; k < 8; k++) {
    const a = (k & 1 ? 1 : -1) * hl, b = (k & 2 ? 1 : -1) * hw, h = (k & 4 ? 1 : -1) * hh
    const e = center[0] + a * c - b * s
    const nn = center[1] + a * s + b * c
    const u = center[2] + h
    v.set(e, u, -nn).applyMatrix4(cam.matrixWorldInverse)
    if (v.z > -cam.near) continue
    v.applyMatrix4(cam.projectionMatrix)
    const px = (v.x * z * 0.5 + 0.5) * W
    const py = (0.5 - v.y * z * 0.5) * H
    if (px < x0) x0 = px
    if (px > x1) x1 = px
    if (py < y0) y0 = py
    if (py > y1) y1 = py
    n++
  }
  if (n === 0 || x1 < 0 || y1 < 0 || x0 > W || y0 > H) return false
  out[0] = Math.max(0, x0)
  out[1] = Math.max(0, y0)
  out[2] = Math.min(W, x1)
  out[3] = Math.min(H, y1)
  return true
}
