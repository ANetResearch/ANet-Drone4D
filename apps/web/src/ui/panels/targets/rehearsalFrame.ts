import type { LifeSample } from '@/ui/sandbox/domain'

/** Interpolate presentation between recorded seconds; discrete events remain on their sampled boundary. */
export function rehearsalFrame(samples: LifeSample[], time: number): LifeSample {
  const index = Math.max(0, Math.min(samples.length - 1, Math.floor(time)))
  const a = samples[index], b = samples[Math.min(index + 1, samples.length - 1)]
  const w = Math.max(0, Math.min(1, time - a.t))
  if (a.hidden !== b.hidden) return a
  const angle = Math.atan2(Math.sin(b.yaw - a.yaw), Math.cos(b.yaw - a.yaw))
  return { ...a, pos: a.pos.map((v, i) => v + (b.pos[i] - v) * w), yaw: a.yaw + angle * w,
    distance: a.distance + (b.distance - a.distance) * w }
}
