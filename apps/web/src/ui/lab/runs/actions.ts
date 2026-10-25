// 运行的取消与重试请求（BN-44 DELETE /runs/{rid}、BN-45 POST /runs/{rid}/retry；AWR-17 §18.6；D3-AC-51）。写请求经 net/bench 带
// X-D3-CSRF 与会话 Cookie；409 680（状态不允许或目录运行）、429 681（队列上限）、403 115（角色或项目范围）以 BenchError 抛出，由页面
// 以 Toast 显示 `code NAME（why）`。批量取消逐个串行发送（避免同一时刻向 bench-api 突发写请求），统计成功与被拒数量。
import { benchDelete, benchPost } from '@/net/bench'

export async function requestCancel(rid: string): Promise<void> {
  await benchDelete(`/runs/${encodeURIComponent(rid)}`)
}

/** → 新 attempt 序号 */
export async function requestRetry(rid: string): Promise<number> {
  const r = await benchPost<{ run_id: string; attempt: number }>(`/runs/${encodeURIComponent(rid)}/retry`, {})
  return r?.attempt ?? 0
}

export interface BatchResult { ok: string[]; rejected: { id: string; error: unknown }[] }
export async function cancelAll(ids: readonly string[], cancel: (id: string) => Promise<void> = requestCancel): Promise<BatchResult> {
  const out: BatchResult = { ok: [], rejected: [] }
  for (const id of ids) {
    try {
      await cancel(id)
      out.ok.push(id)
    } catch (error) {
      out.rejected.push({ id, error })
    }
  }
  return out
}
