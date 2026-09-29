// ClockSync (M11-FR-092; AWR-17 §6.10; r27 §3.10): Cristian's algorithm with minimum-RTT filtering, run inside rt.worker.
// Owner: M11. Offsets are in ms: serverMonotonicMs = localNowMs + offset. The worker converts its offset to the main
// thread time base with off_main = off_worker + (timeOrigin_main - timeOrigin_worker) (see offsetMain()).
export const CLOCK = {
  samples: 16,
  jumpMs: 50,
  smooth: 0.1,
  srttKeep: 0.875,
  burstCount: 5,
  burstGapMs: 100,
  steadyGapMs: 500,
} as const

export class ClockSync {
  private readonly rtt = new Float64Array(CLOCK.samples)
  private readonly off = new Float64Array(CLOCK.samples)
  private n = 0
  private head = 0
  offsetMs = Number.NaN
  srttMs = Number.NaN
  sent = 0

  reset(): void {
    this.n = 0
    this.head = 0
    this.offsetMs = Number.NaN
    this.srttMs = Number.NaN
    this.sent = 0
  }

  /** interval before the next ping: 5 pings 100 ms apart after connecting, then 2 Hz */
  nextGapMs(): number {
    return this.sent < CLOCK.burstCount ? CLOCK.burstGapMs : CLOCK.steadyGapMs
  }

  /** pong: t0 = client send time echoed back, t1 = receive time (local ms), serverMs = server_ns / 1e6 */
  onPong(t0: number, t1: number, serverMs: number): void {
    const rtt = Math.max(0, t1 - t0)
    const sample = serverMs - (t0 + t1) / 2
    this.rtt[this.head] = rtt
    this.off[this.head] = sample
    this.head = (this.head + 1) % CLOCK.samples
    if (this.n < CLOCK.samples) this.n++
    let best = 0
    for (let i = 1; i < this.n; i++) if (this.rtt[i] < this.rtt[best]) best = i
    const target = this.off[best]
    if (Number.isNaN(this.offsetMs) || Math.abs(target - this.offsetMs) > CLOCK.jumpMs) this.offsetMs = target
    else this.offsetMs += (target - this.offsetMs) * CLOCK.smooth
    this.srttMs = Number.isNaN(this.srttMs) ? rtt : CLOCK.srttKeep * this.srttMs + (1 - CLOCK.srttKeep) * rtt
  }

  /**
   * Offset for the main thread: server = mainNow + offMain. With worker time w = abs - originW and main time
   * m = abs - originM, server = w + offW = m + (originM - originW) + offW, so offMain = offW + originM - originW.
   */
  offsetMain(timeOriginMain: number, timeOriginWorker: number): number {
    return this.offsetMs + (timeOriginMain - timeOriginWorker)
  }
}
