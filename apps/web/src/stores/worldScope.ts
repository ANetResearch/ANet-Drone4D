// World scope of the client state (D2.2-W1; ADR-111; AWR-14 §20.2.3). Owner: M15. Every piece of client state that
// describes the simulation of a world (fleet rows and poses, targets, perception frames and field samples, tasking, nests,
// sensor views and detections, mission paths, safety and agent rows, the selection, follow, hover, readouts, drafts,
// placement and drawing previews) belongs to one pair (realtime source, world):
//   * it is ingested only while the source simulates the world the view shows: `worldLive()` is the ingestion gate of the
//     data layer (stores and engine models check it on every message; nothing is merely hidden at render time);
//   * it is cleared whenever the pair changes: `onWorldReset` handlers run on a world switch, a source switch (shared show,
//     static browsing, a sandbox) and when a known stream changes world (sandbox world switch).
// Fields:
//   world        the world the view shows: the world the viewport opened (stores/world), before that the route's or the
//                sandbox session's world
//   source       the realtime source of the surface: 'shared' | 'local' | 'sandbox:<sid>'; null for static browsing and an
//                unavailable sandbox (no realtime link belongs to the view)
//   streamWorld  serverInfo.world.id of the source's connection; kept while the link is closed on purpose (power saving,
//                viewer queue, parked sandbox: the last data of the same world stays on screen), forgotten on a source change
//   live         source, world and streamWorld are set and streamWorld === world
//   foreign      no source, or the source simulates another world: the simulation channels stay unsubscribed
//   epoch        + 1 on every reset
//   bound        the shell feeds the scope; before that (unit tests of a single store, tools without the shell) the gate is
//                open and nothing is reset
// This module imports neither app/, ui/ nor viewport/: app/shell/worldScope.ts feeds the view and the source from the
// run-time surface and the opened world, ui/shell/RtContext.tsx the stream world from the page client's serverInfo.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'

export interface WorldScopeState {
  world: string | null
  source: string | null
  streamWorld: string | null
  live: boolean
  foreign: boolean
  epoch: number
  bound: boolean
}

export const worldScopeStore = createAwrStore<WorldScopeState>('worldScope', () => ({
  world: null, source: null, streamWorld: null, live: false, foreign: false, epoch: 0, bound: false,
}))

/** module copy of the gate (read on every realtime message and frame: no store access on the hot path); open while unbound */
let liveNow = true
/** realtime data may be ingested: the source's stream simulates the world the view shows */
export const worldLive = (): boolean => liveNow
export const worldScopeNow = (): WorldScopeState => worldScopeStore.getState()

type ResetFn = (s: WorldScopeState) => void
const resets: { name: string; fn: ResetFn }[] = []

/**
 * register a reset of world-bound state (store modules at load time, the viewport and the shell at start-up); handlers run
 * in registration order after the scope changed, a throwing handler is isolated; returns the unregister function
 */
export function onWorldReset(name: string, fn: ResetFn): () => void {
  const e = { name, fn }
  resets.push(e)
  return () => {
    const i = resets.indexOf(e)
    if (i >= 0) resets.splice(i, 1)
  }
}

function commit(p: Pick<WorldScopeState, 'world' | 'source' | 'streamWorld'>, change: boolean): void {
  const cur = worldScopeStore.getState()
  // the first binding at start-up has nothing of another world to clear
  const reset = change && cur.bound
  const live = p.source !== null && p.world !== null && p.streamWorld === p.world
  const foreign = p.source === null || (p.streamWorld !== null && p.world !== null && p.streamWorld !== p.world)
  liveNow = live
  const next: WorldScopeState = { ...p, live, foreign, epoch: cur.epoch + (reset ? 1 : 0), bound: true }
  worldScopeStore.setState(next)
  if (!reset) return
  for (const r of [...resets]) {
    try {
      r.fn(next)
    } catch (e) {
      console.error(`awr world scope: reset ${r.name} failed`, e)
    }
  }
}

export const worldScope = {
  /** the world the view shows and the realtime source of the surface; any change resets the world-bound state */
  setView(world: string | null, source: string | null): void {
    const s = worldScopeStore.getState()
    if (s.world === world && s.source === source) return
    commit({ world, source, streamWorld: source === s.source ? s.streamWorld : null }, true)
  },
  /** serverInfo.world.id of the source's connection; a known stream that changes world resets the world-bound state */
  setStreamWorld(world: string | null): void {
    const s = worldScopeStore.getState()
    const w = world || null
    if (s.source === null || w === s.streamWorld) return
    commit({ world: s.world, source: s.source, streamWorld: w }, s.streamWorld !== null)
  },
  /** run every reset handler without a scope change (tests, a replaced realtime client) */
  resetNow(): void {
    const s = worldScopeStore.getState()
    commit({ world: s.world, source: s.source, streamWorld: s.streamWorld }, true)
  },
  /** tests: back to the unbound initial state (gate open, no handler runs) */
  clearForTests(): void {
    liveNow = true
    worldScopeStore.setState({ world: null, source: null, streamWorld: null, live: false, foreign: false, epoch: 0, bound: false })
  },
}

export function useWorldScope<T>(sel: (s: WorldScopeState) => T): T {
  return useStore(worldScopeStore, sel)
}
