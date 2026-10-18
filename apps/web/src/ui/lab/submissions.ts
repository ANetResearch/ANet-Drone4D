// Internal submission form helpers (BN-95; bench/submission.v1): image digest, entry command and environment parsing.
// The command is a JSON string array or whitespace-separated words; the environment is KEY=VALUE per line with upper-case
// identifier keys (DRONE4D_* is reserved for the platform). Withdrawal is allowed before EVALUATING; visibility can change
// unless the submission ended as REJECTED, FAILED or WITHDRAWN.
import type { SubmissionState } from '@/net/bench'

export const DIGEST_RE = /^sha256:[0-9a-f]{64}$/
const ENV_KEY = /^[A-Z][A-Z0-9_]{0,63}$/

export function parseCmd(text: string): string[] | null {
  const s = text.trim()
  if (!s) return null
  if (s.startsWith('[')) {
    try {
      const v: unknown = JSON.parse(s)
      return Array.isArray(v) && v.length > 0 && v.every((x) => typeof x === 'string') ? (v as string[]) : null
    } catch {
      return null
    }
  }
  return s.split(/\s+/)
}

export function parseEnv(text: string): Record<string, string> | null {
  const out: Record<string, string> = {}
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    const i = line.indexOf('=')
    if (i <= 0) return null
    const k = line.slice(0, i)
    if (!ENV_KEY.test(k) || k.startsWith('DRONE4D_')) return null
    out[k] = line.slice(i + 1)
  }
  return out
}

export const canWithdraw = (s: SubmissionState): boolean => s === 'RECEIVED' || s === 'SCANNING' || s === 'VALIDATING'
export const canToggleVisibility = (s: SubmissionState): boolean => s !== 'REJECTED' && s !== 'FAILED' && s !== 'WITHDRAWN'
