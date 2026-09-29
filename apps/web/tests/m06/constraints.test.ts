// M06-AC-005 and AC-018 (M06 §6.3 rules; g01 §0 item 5): shared InstancedMesh material or geometry -> M06-E008,
// int uniform -> M06-E009, texture.internalFormat -> M06-E010 (assertions); material.onBeforeRender, info.render.frame,
// synchronous read-back, non-white-listed drei, shared-path features and onObjectUpdate -> M06 lint failures; the
// repository passes the lint; pointer events on WorldRoot ancestors fail the dev assertion.
import { describe, expect, it } from 'vitest'
import { DataTexture, Group, InstancedMesh, Mesh, PlaneGeometry, Points, BufferGeometry, Object3D } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { texture, uniform, vec3, vec4 } from 'three/tsl'
import { assertFloatUniforms, assertInstancing, assertNoInternalFormat, assertNoPointerEventsOnWorld, checkLayerRoot, M06Error } from '@/viewport/backend/guards'
// @ts-expect-error untyped .mjs lint module
import { checkText, run } from './lint/m06-lint.mjs'

type N = any

describe('registration assertions (M06-AC-005)', () => {
  it('two InstancedMesh sharing one material -> M06-E008', () => {
    const m = new MeshBasicNodeMaterial()
    const root = new Group()
    root.add(new InstancedMesh(new PlaneGeometry(), m, 2), new InstancedMesh(new PlaneGeometry(), m, 2))
    expect(() => assertInstancing(root)).toThrowError(/M06-E008/)
  })
  it('two instanced objects sharing one geometry -> M06-E008; own geometry and material pass', () => {
    const g = new PlaneGeometry()
    const root = new Group()
    root.add(new InstancedMesh(g, new MeshBasicNodeMaterial(), 2), new InstancedMesh(g, new MeshBasicNodeMaterial(), 2))
    expect(() => assertInstancing(root)).toThrowError(/M06-E008.*geometry/)
    const ok = new Group()
    ok.add(new InstancedMesh(new PlaneGeometry(), new MeshBasicNodeMaterial(), 2), new InstancedMesh(new PlaneGeometry(), new MeshBasicNodeMaterial(), 2))
    expect(() => assertInstancing(ok)).not.toThrow()
  })
  it('int uniform -> M06-E009; float uniforms pass', () => {
    const bad = new MeshBasicNodeMaterial()
    bad.colorNode = vec4(vec3(uniform(1, 'int') as N), 1) as N
    expect(() => assertFloatUniforms(bad)).toThrowError(M06Error)
    expect(() => assertFloatUniforms(bad)).toThrowError(/M06-E009/)
    const good = new MeshBasicNodeMaterial()
    good.colorNode = vec4(vec3(uniform(1) as N), 1) as N
    expect(() => assertFloatUniforms(good)).not.toThrow()
  })
  it('texture.internalFormat -> M06-E010 (wrapper and material scan)', () => {
    const t = new DataTexture(new Uint32Array(4), 1, 1)
    ;(t as unknown as { internalFormat: string }).internalFormat = 'RGBA32UI'
    expect(() => assertNoInternalFormat(t)).toThrowError(/M06-E010/)
    const m = new MeshBasicNodeMaterial()
    m.colorNode = texture(t) as N
    const root = new Group()
    root.add(new Mesh(new PlaneGeometry(), m))
    expect(() => checkLayerRoot(root, new Map(), 'test')).toThrowError(/M06-E010/)
  })
  it('pointer events on WorldRoot or point-cloud ancestors fail in dev builds (M06-FR-021)', () => {
    const world = new Group()
    const cloud = new Points(new BufferGeometry())
    world.add(cloud)
    const other = new Object3D()
    expect(() => assertNoPointerEventsOnWorld([other], world, [cloud])).not.toThrow()
    expect(() => assertNoPointerEventsOnWorld([world], world, [cloud])).toThrowError(/M06-FR-021/)
    expect(() => assertNoPointerEventsOnWorld([cloud], world, [cloud])).toThrowError(/M06-FR-021/)
  })
})

describe('M06 lint (M06-AC-005, AC-006, AC-018)', () => {
  const f = 'apps/web/src/engine/drones/x.ts'
  const rules = (text: string, file = f): string[] => (checkText(file, text) as { rule: string }[]).map((v) => v.rule)
  it('flags each forbidden construct', () => {
    expect(rules('material.onBeforeRender = () => {}')).toEqual(['M06-L-01'])
    expect(rules('const f = r.info.render.frame')).toEqual(['M06-L-02'])
    expect(rules('r.readRenderTargetPixels(rt, 0, 0, 1, 1, b)')).toEqual(['M06-L-03'])
    expect(rules('gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, b)')).toEqual(['M06-L-03'])
    expect(rules("import { Grid, Html } from '@react-three/drei'", 'apps/web/src/viewport/x.tsx')).toEqual(['M06-L-04'])
    expect(rules("import { Html, View } from '@react-three/drei'", 'apps/web/src/viewport/x.tsx')).toEqual([])
    expect(rules('const p = new RenderPipeline(r)')).toEqual(['M06-L-05'])
    expect(rules('const s = pass(scene, cam)')).toEqual(['M06-L-05'])
    expect(rules("t.internalFormat = 'RGBA32UI'")).toEqual(['M06-L-06'])
    expect(rules('const u = uniform(1).onObjectUpdate(() => 1)')).toEqual(['M06-L-07'])
  })
  it('allows the asynchronous read-back, comments and the microbench exception', () => {
    expect(rules('await r.readRenderTargetPixelsAsync(rt, 0, 0, 1, 1, b)')).toEqual([])
    expect(rules('// material.onBeforeRender = x (documented)')).toEqual([])
    expect(rules('gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)', 'apps/web/src/viewport/backend/microbench.ts')).toEqual([])
    expect(rules('gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)', 'apps/web/src/viewport/dev/featMatrix.ts')).toEqual([])
  })
  it('the repository passes (make lint-m06)', () => {
    const v = run([]) as { f: string; rule: string }[]
    expect(v.map((x) => `${x.f} ${x.rule}`)).toEqual([])
  })
})
