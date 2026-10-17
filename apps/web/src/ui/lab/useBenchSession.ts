// Workspace session of the lab pages (BN-02). A missing or expired session sends the page to /lab/login with `next`.
import { useQuery } from '@tanstack/react-query'
import * as React from 'react'
import { navigate } from '@/app/router/router'
import { BenchError, benchGet, type BenchSession } from '@/net/bench'

export function useBenchSession(): BenchSession | null {
  const q = useQuery({ queryKey: ['lab', 'session'], queryFn: () => benchGet<BenchSession>('/session'), retry: false, staleTime: 60_000 })
  const denied = q.error instanceof BenchError && q.error.status === 401
  React.useEffect(() => {
    if (denied) navigate(`/lab/login?next=${encodeURIComponent(location.pathname + location.search)}`, { replace: true })
  }, [denied])
  return q.data ?? null
}
