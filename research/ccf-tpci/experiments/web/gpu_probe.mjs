#!/usr/bin/env node
// Prints the WebGL renderer headless Chromium gets for each GPU flag set (which flags reach a real GPU on this host).
const pw = await import(process.env.PW_MODULE ?? '@playwright/test')
const chromium = pw.chromium ?? pw.default.chromium
const SETS = {
  default: [],
  'angle-vulkan': ['--use-angle=vulkan', '--enable-features=Vulkan', '--ignore-gpu-blocklist', '--enable-gpu'],
  'angle-egl': ['--use-gl=angle', '--use-angle=gl-egl', '--ignore-gpu-blocklist', '--enable-gpu'],
  'egl': ['--use-gl=egl', '--ignore-gpu-blocklist', '--enable-gpu'],
}
for (const [k, args] of Object.entries(SETS)) {
  try {
    const b = await chromium.launch({ headless: true, args })
    const p = await b.newPage()
    const r = await p.evaluate(() => {
      const c = document.createElement('canvas').getContext('webgl2')
      if (!c) return 'no webgl2'
      const x = c.getExtension('WEBGL_debug_renderer_info')
      return x ? c.getParameter(x.UNMASKED_RENDERER_WEBGL) : c.getParameter(c.RENDERER)
    })
    console.log(`${k}: ${r}`)
    await b.close()
  } catch (e) {
    console.log(`${k}: failed ${String(e).slice(0, 200)}`)
  }
}
