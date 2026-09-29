// Environment quality level and PerfGovernor step 5 knob (M07-FR-043; M07 §6.6.3; ADR-041, ADR-044; M06-FR-076). Owner: M07.
// Start level by device: software and iGPU (and every Tier S) start at Low; dGPU Tier B/A would start at Med once Med is
// delivered (D1-ext). The knob has 2 levels on Tier S (Low -> Off) and 3 on B/A (Med -> Low -> Off); level 0 is the start
// level, the last one Off. Off hides precipitation, 2D clouds, cloud shadows, arrows and streamlines; fog and lighting
// stay. A manual setting (auto | off | low | med) caps the level; the governor may still go lower. Transitions only
// change uniforms, drawRange and visibility (zero program compiles).
import type { DeviceClass, Tier } from '../../loop'
import type { EnvLevel } from './envTiers'

export type EnvUserLevel = 'auto' | EnvLevel
const RANK: Record<EnvLevel, number> = { off: 0, low: 1, med: 2 }

export interface EnvKnob {
  step: 5
  id: 'env'
  levels: number
  labelKey: string
  apply(level: number): void
}

export class EnvQuality {
  readonly start: EnvLevel
  readonly levelsList: readonly EnvLevel[]
  private governorLevel = 0
  private user: EnvUserLevel = 'auto'
  /** 'governor' when the governor lowered the level, 'user' when the setting did, else null */
  reason: 'governor' | 'user' | null = null
  readonly knob: EnvKnob
  onChange: ((l: EnvLevel) => void) | null = null
  private last: EnvLevel

  constructor(tier: Tier, device: DeviceClass, medAvailable = false) {
    this.start = tier !== 'S' && device === 'dGPU' && medAvailable ? 'med' : 'low'
    this.levelsList = this.start === 'med' ? ['med', 'low', 'off'] : ['low', 'off']
    this.last = this.start
    this.knob = {
      step: 5, id: 'env', levels: this.levelsList.length, labelKey: 'env.degraded',
      apply: (level: number) => {
        this.governorLevel = Math.max(0, Math.min(this.levelsList.length - 1, level | 0))
        this.changed()
      },
    }
  }

  get level(): EnvLevel {
    const byGov = this.levelsList[this.governorLevel]
    if (this.user === 'auto') return byGov
    const cap = RANK[this.user] > RANK[this.start] ? this.start : this.user
    return RANK[cap] < RANK[byGov] ? cap : byGov
  }

  setUserLevel(l: EnvUserLevel): void {
    this.user = l
    this.changed()
  }

  /** i18n key of the degradation notice (env.degraded.low | env.degraded.off), null at the start level */
  get reasonKey(): string | null {
    const l = this.level
    return l === this.start ? null : `env.degraded.${l}`
  }

  private changed(): void {
    const l = this.level
    this.reason = l === this.start ? null : this.levelsList[this.governorLevel] === l && this.user === 'auto' ? 'governor' : this.user !== 'auto' ? 'user' : 'governor'
    if (l !== this.last) {
      this.last = l
      this.onChange?.(l)
    }
  }
}
