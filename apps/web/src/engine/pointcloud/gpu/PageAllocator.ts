// PageAllocator (M05 §6.6.2): 256-texel pages over the linear pool address space, first fit over a sorted free-interval
// list, merge with neighbours on free, high water tracked. Owner: M05. Pure (no time, no DOM).
export class PageAllocator {
  private readonly starts: Int32Array
  private readonly lens: Int32Array
  private nFree = 1
  readonly pages: number
  usedPages = 0
  highWater = 0

  constructor(readonly totalTexels: number, readonly page = 256) {
    this.pages = Math.floor(totalTexels / page)
    this.starts = new Int32Array(this.pages + 1)
    this.lens = new Int32Array(this.pages + 1)
    this.reset()
  }

  reset(): void {
    this.nFree = 1
    this.starts[0] = 0
    this.lens[0] = this.pages
    this.usedPages = 0
    this.highWater = 0
  }

  get usedTexels(): number {
    return this.usedPages * this.page
  }
  get largestFree(): number {
    let m = 0
    for (let i = 0; i < this.nFree; i++) if (this.lens[i] > m) m = this.lens[i]
    return m * this.page
  }

  /** texel base address for n points, or -1 */
  alloc(n: number): number {
    const need = Math.max(1, Math.ceil(n / this.page))
    for (let i = 0; i < this.nFree; i++) {
      if (this.lens[i] < need) continue
      const s = this.starts[i]
      if (this.lens[i] === need) {
        this.starts.copyWithin(i, i + 1, this.nFree)
        this.lens.copyWithin(i, i + 1, this.nFree)
        this.nFree--
      } else {
        this.starts[i] += need
        this.lens[i] -= need
      }
      this.usedPages += need
      if (s + need > this.highWater) this.highWater = s + need
      return s * this.page
    }
    return -1
  }

  free(base: number, n: number): void {
    const s = Math.floor(base / this.page)
    const len = Math.max(1, Math.ceil(n / this.page))
    let i = 0
    while (i < this.nFree && this.starts[i] < s) i++
    const mergeLeft = i > 0 && this.starts[i - 1] + this.lens[i - 1] === s
    const mergeRight = i < this.nFree && s + len === this.starts[i]
    if (mergeLeft && mergeRight) {
      this.lens[i - 1] += len + this.lens[i]
      this.starts.copyWithin(i, i + 1, this.nFree)
      this.lens.copyWithin(i, i + 1, this.nFree)
      this.nFree--
    } else if (mergeLeft) this.lens[i - 1] += len
    else if (mergeRight) {
      this.starts[i] = s
      this.lens[i] += len
    } else {
      this.starts.copyWithin(i + 1, i, this.nFree)
      this.lens.copyWithin(i + 1, i, this.nFree)
      this.starts[i] = s
      this.lens[i] = len
      this.nFree++
    }
    this.usedPages -= len
    // lower the high water mark when the tail is free
    const last = this.nFree - 1
    if (last >= 0 && this.starts[last] + this.lens[last] === this.pages) this.highWater = Math.min(this.highWater, this.starts[last])
  }
}
