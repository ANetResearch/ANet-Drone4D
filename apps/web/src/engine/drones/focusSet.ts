// Focus set (ADR-046; M06 §6.9, FR-038, AC-029; AWR-17 §6.6). Owner: M06.
// Every 250 ms: vehicles with r_px >= 8 enter, members with r_px < 6 that stayed >= 1 s leave, vehicles that vanished
// leave at once; the set holds at most K = 32 (selected vehicles do not count: they run at 60 Hz elsewhere). Candidates
// are taken by descending r_px. Differences become RtClient.subscribe('uav/<id>/state', {rate: 30}) and its release;
// RtClient reference-counts and merges changes into one message per 250 ms. Entry and exit events are reported for the
// focusJumpM metric. Preallocated tables keyed by agentNo; no allocation in steady state.
export const FOCUS = { k: 32, enterPx: 8, exitPx: 6, dwellMs: 1000, hz: 4, rate: 30 } as const

export interface FocusDeps {
  /** roster id of an agent number (topic key); undefined before the roster */
  idOf(agentNo: number): string | undefined
  subscribe(topic: string, rate: number): () => void
}

export class FocusSet {
  readonly inSet = new Uint8Array(65536)
  private readonly since = new Float64Array(65536)
  private readonly release: ((() => void) | null)[] = new Array(65536).fill(null)
  readonly members: Int32Array
  size = 0
  private readonly candIdx: Int32Array
  private readonly candPx: Float32Array
  private readonly seen = new Uint8Array(65536)
  private readonly rpxA = new Float32Array(65536)
  /** agents that entered or left in the last update (for focusJumpM) */
  readonly changed: Int32Array
  changedN = 0
  subscribeCalls = 0

  constructor(private readonly deps: FocusDeps, readonly k: number = FOCUS.k, capacity = 1024) {
    this.members = new Int32Array(k)
    this.candIdx = new Int32Array(capacity)
    this.candPx = new Float32Array(capacity)
    this.changed = new Int32Array(2 * k + capacity)
  }

  has(agentNo: number): boolean {
    return this.inSet[agentNo & 0xffff] === 1
  }

  /** n vehicles with agent numbers and r_px; excluded[agentNo] = 1 for selected vehicles */
  update(n: number, agentNo: Uint16Array | Int32Array, rpx: Float32Array, nowMs: number, excluded: Uint8Array): void {
    this.changedN = 0
    for (let i = 0; i < n; i++) {
      this.seen[agentNo[i]] = 1
      this.rpxA[agentNo[i]] = rpx[i]
    }
    // exits: vanished, selected (now counted elsewhere), or below 6 px after the dwell
    let w = 0
    for (let j = 0; j < this.size; j++) {
      const a = this.members[j]
      let keep = this.seen[a] === 1 && excluded[a] !== 1
      if (keep && this.rpxA[a] < FOCUS.exitPx && nowMs - this.since[a] >= FOCUS.dwellMs) keep = false
      if (keep) this.members[w++] = a
      else this.leave(a)
    }
    this.size = w
    // entries: candidates >= 8 px by descending r_px
    let c = 0
    for (let i = 0; i < n; i++) {
      const a = agentNo[i]
      if (this.inSet[a] === 1 || excluded[a] === 1 || rpx[i] < FOCUS.enterPx) continue
      this.candIdx[c] = i
      this.candPx[c] = rpx[i]
      c++
    }
    while (this.size < this.k && c > 0) {
      let best = 0
      for (let j = 1; j < c; j++) if (this.candPx[j] > this.candPx[best]) best = j
      const a = agentNo[this.candIdx[best]]
      this.candIdx[best] = this.candIdx[c - 1]
      this.candPx[best] = this.candPx[c - 1]
      c--
      const id = this.deps.idOf(a)
      if (id === undefined) continue
      this.inSet[a] = 1
      this.since[a] = nowMs
      this.members[this.size++] = a
      this.release[a] = this.deps.subscribe(`uav/${id}/state`, FOCUS.rate)
      this.subscribeCalls++
      this.changed[this.changedN++] = a
    }
    for (let i = 0; i < n; i++) this.seen[agentNo[i]] = 0
  }

  private leave(a: number): void {
    this.inSet[a] = 0
    const r = this.release[a]
    this.release[a] = null
    if (r) {
      r()
      this.subscribeCalls++
    }
    this.changed[this.changedN++] = a
  }

  /** drop everything (world switch, epoch) */
  clear(): void {
    for (let j = 0; j < this.size; j++) this.leave(this.members[j])
    this.size = 0
  }
}
