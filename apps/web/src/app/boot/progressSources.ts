// Sources of the boot progress segments that the shell can observe itself (AWR-04 §10.1; AWR-14 §20.3.2), used until and
// alongside the direct reports of M05 (world, points with bytes) and M06 (shader zoo counts) through lib/bootProgress:
//   * the BootController gates as floors: canvas -> bundle 1 (the main chunk runs), firstFrame -> world 1 and points 1,
//     warmup -> shader part 1, uiWarm -> ui part 1 (the reveal itself is written by the controller);
//   * the opened-world snapshot of M05 (stores/world.ts, <= 4 Hz): manifest -> world 0.5; first_screen -> world 1 and
//     points = first-frame coverage (bytes = coverage x firstScreenBytes); streaming -> points 1.
// Every source only raises a segment (the model keeps the maximum), so a direct report and a floor never fight.
import { reportBootProgress, reportWarmPart } from '@/lib/bootProgress'
import { worldStore, type WorldState } from '@/stores/world'

/** a BootController gate: 'shell', 'canvas', 'firstFrame', 'warmup', 'uiWarm' (others are ignored) */
export type GateName = string

/** the floors a resolved gate implies */
export function floorOfGate(name: GateName): void {
  switch (name) {
    case 'canvas':
      reportBootProgress('bundle', 1)
      break
    case 'firstFrame':
      reportBootProgress('bundle', 1)
      reportBootProgress('world', 1)
      reportBootProgress('points', 1)
      break
    case 'warmup':
      reportWarmPart('shaders', 1)
      break
    case 'uiWarm':
      reportWarmPart('shaders', 1)
      reportWarmPart('ui', 1)
      break
    default:
      break
  }
}

/** world and points fractions of an opened-world snapshot (pure; tests) */
export function worldFloors(s: Pick<WorldState, 'phase' | 'progress' | 'firstScreenBytes'>): { world: number; points: number; bytes: { received: number; total: number } | null } {
  const total = s.firstScreenBytes > 0 ? s.firstScreenBytes : 0
  const p = Number.isFinite(s.progress) ? Math.min(1, Math.max(0, s.progress)) : 0
  switch (s.phase) {
    case 'manifest':
      return { world: 0.5, points: 0, bytes: null }
    case 'first_screen':
      return { world: 1, points: p, bytes: total > 0 ? { received: p * total, total } : null }
    case 'streaming':
    case 'suspended':
      return { world: 1, points: 1, bytes: total > 0 ? { received: total, total } : null }
    default:
      return { world: 0, points: 0, bytes: null }
  }
}

/** follow the opened-world snapshot; returns the detach function */
export function followWorldProgress(): () => void {
  const push = (s: WorldState): void => {
    const f = worldFloors(s)
    if (f.world > 0) reportBootProgress('world', f.world)
    if (f.points > 0 || f.bytes) reportBootProgress('points', f.points, f.bytes ?? undefined)
  }
  push(worldStore.getState())
  return worldStore.subscribe((s, prev) => {
    if (s.phase !== prev.phase || s.progress !== prev.progress || s.firstScreenBytes !== prev.firstScreenBytes) push(s)
  })
}
