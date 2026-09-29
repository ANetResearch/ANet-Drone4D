// Jobs store (AWR-03 §4.3: owner M03). Skeleton written by M15-S for the Jobs overlay page (D1-ext): job rows updated from
// job.progress and job.state events (<= 4 Hz). TODO(M03): writers and the job kinds registry.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'

export type JobState = 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELED'
export interface JobRow { jobId: string; kind: string; state: JobState; progress: number; worldId: string | null; tSubmitMs: number; tUpdateMs: number; reason: number }
export interface JobsState { jobs: readonly JobRow[]; version: number }

export const jobsStore = createAwrStore<JobsState>('jobs', () => ({ jobs: [], version: 0 }))

export function useJobs<T>(sel: (s: JobsState) => T): T {
  return useStore(jobsStore, sel)
}
