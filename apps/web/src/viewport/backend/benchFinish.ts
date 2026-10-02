// finishForBench of the test builds (AWR-18 §5.2 layer pairing; M06 §6.2): waits for the rasterisation of the bound
// target with a 1-pixel synchronous read-back. Owner: M06. Only test builds install it (TEST_SWITCHES); the only other
// synchronous read-back exempt from M06-L-03 is the 300 ms device microbench.
// Chrome's WebGL finish() returns as soon as the commands are queued: it timed only the submission (0.2 ms for a
// 25k-point pass that costs about 19 ms of SwiftShader raster, FX2-R2), so the pairing measured nothing.
export function makeBenchFinish(gl: WebGL2RenderingContext): () => void {
  const px = new Uint8Array(4)
  return () => gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)
}
