// QueryClient (M15-FR-038; n05 §3.9): staleTime 30 s, gcTime 5 min, one retry for idempotent GETs, no refetch on window
// focus. Commands never go through Query (RtClient.call).
import { QueryClient } from '@tanstack/react-query'

const SECOND = 1000
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30 * SECOND, gcTime: 300 * SECOND, retry: 1, refetchOnWindowFocus: false },
    mutations: { retry: 0 },
  },
})
