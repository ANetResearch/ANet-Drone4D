// Nest panel model (M15-FR-213; UX-FR-125; AWR-14 §20.8): pure helpers of the nest status view.
import type { NestStatus } from '@awr/contracts/types'

/** idle channels as one list (1-based numbers, AWR-14 §20.8 "3 空闲 4 空闲") and the working ones (pure; tests) */
export function channelRows(chargers: NestStatus['chargers']): { busy: NestStatus['chargers']; idle: number[]; blocked: number[] } {
  return { busy: chargers.filter((c) => c.state === 'charging'), idle: chargers.filter((c) => c.state === 'idle').map((c) => c.ch + 1),
    blocked: chargers.filter((c) => c.state === 'blocked').map((c) => c.ch + 1) }
}
