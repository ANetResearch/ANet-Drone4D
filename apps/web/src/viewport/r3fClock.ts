// Clock for @react-three/fiber (FX-UBO, ADR-086; M06 §6.6). Owner: M06.
// R3F 9.8.1 puts `new THREE.Clock()` into every store; three r183 deprecated Clock in favour of Timer and its
// constructor logs "THREE.Clock: This module has been deprecated. Please use THREE.Timer instead." once per canvas.
// R3F reads and writes the clock's fields directly (frameloop="never": delta = timestamp - clock.elapsedTime, then
// clock.oldTime and clock.elapsedTime are overwritten), so a THREE.Timer cannot stand in. R3fClock is the same
// performance.now() stopwatch with the same fields and methods, without the warning; vite.config.ts resolves R3F's
// imports of 'three' to r3fThree.ts, which re-exports three with this Clock. The engine clock (engine/time, the loop)
// never used THREE.Clock: its time semantics are unchanged, and R3F keeps receiving the loop's seconds through
// advance(tS) (LoopDriver).
export class R3fClock {
  autoStart: boolean
  startTime = 0
  oldTime = 0
  elapsedTime = 0
  running = false

  constructor(autoStart = true) {
    this.autoStart = autoStart
  }
  start(): void {
    this.startTime = performance.now()
    this.oldTime = this.startTime
    this.elapsedTime = 0
    this.running = true
  }
  stop(): void {
    this.getElapsedTime()
    this.running = false
    this.autoStart = false
  }
  getElapsedTime(): number {
    this.getDelta()
    return this.elapsedTime
  }
  getDelta(): number {
    if (this.autoStart && !this.running) {
      this.start()
      return 0
    }
    let diff = 0
    if (this.running) {
      const now = performance.now()
      diff = (now - this.oldTime) / 1000
      this.oldTime = now
      this.elapsedTime += diff
    }
    return diff
  }
}
