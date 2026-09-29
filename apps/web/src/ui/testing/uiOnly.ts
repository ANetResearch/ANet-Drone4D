// UI-only harness of test builds (M15 NFR-018: UI single cases driven by FakeSource): `?viewport=off` mounts no
// WorldCanvas and replaces the few viewport duties the shell depends on, so M15 specs can run while the renderer or the
// point cloud change in parallel: the engine loop is started directly (no R3F advance), one telemetry-phase task swaps
// the realtime slots (M06 does this in engine/drones when the viewport is mounted) and the default subscription set
// (roster, swarm, events, env, perf/server) is registered. Never active in production builds (TEST_SWITCHES folds).
import { loop, register } from '@/engine'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { createRtClient } from '@/net/rt'

export const UI_ONLY: boolean = TEST_SWITCHES && typeof location !== 'undefined' && new URLSearchParams(location.search).get('viewport') === 'off'

export function installUiOnly(): void {
  if (!UI_ONLY) return
  const rt = createRtClient()
  register('telemetry', 'ui.test.swap', () => {
    rt.swapFrame()
  })
  rt.subscribe('fleet/roster', { rate: 10 })
  rt.subscribe('swarm/state', { rate: 10 })
  rt.subscribe('event', { rate: 0, mode: 'all' })
  rt.subscribe('env/state', { rate: 10 })
  rt.subscribe('perf/server', { rate: 1 })
  loop.start()
}
