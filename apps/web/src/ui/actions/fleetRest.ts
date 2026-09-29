// Fleet REST writes (AWR-17 §4.3.5 R15 DELETE /api/fleet/vehicles/{id}); net/api.ts apiDelete carries the bearer token,
// the 401 retry and the problem+json error shape (INT-1: M15-to-M11 item 1 delivered).
import { apiDelete } from '@/net/api'

export async function deleteVehicle(id: string, o: { force?: boolean } = {}): Promise<number> {
  const path = `/api/fleet/vehicles/${encodeURIComponent(id)}${o.force ? '?force=true' : ''}`
  return apiDelete(path, { headers: { accept: 'application/json', 'Idempotency-Key': crypto.randomUUID() } })
}
