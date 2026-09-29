// net/rt facade (M11 §7.5). Owner: M11. ui/**, app/** and engine/** import the realtime client only from here.
export * from './types'
export {
  createRtClient, rtClient, createFakeRtClient, fakeUrl, resolveRtUrl, LocalPort, RtClientImpl, SUB_FLUSH_MS, EVENT_IDLE_MS,
  PLAYBACK_TIMEOUT_MS, type RtClientOptions, type WorkerLike,
} from './client'
export { FakeSource, FakeWorld, createFakeSource, isFakeUrl, openFake, parseFakeUrl, type FakeSourceOptions, type FakeEvent } from './FakeSource'
export { SLOT_BYTES, SLOT_CAP, SF, RAW_SCHEMA, SlotReader, FrameFront } from './frame'
export { batchCountsOf, batchSummaryOf, isFinalResult } from './batch'
