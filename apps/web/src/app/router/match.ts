// Route matching (M15-FR-003, §6.3.2): URLPattern pathname patterns compiled once, sorted by static segment count
// (descending) then parameter count (ascending); a segment matcher is the fallback where URLPattern is missing (Node tests).
export interface CompiledRoute<R> { route: R; pattern: string; test(path: string): Record<string, string> | null }

type URLPatternLike = { exec(input: { pathname: string }): { pathname: { groups: Record<string, string | undefined> } } | null }
type URLPatternCtor = new (init: { pathname: string }) => URLPatternLike

function segmentMatcher(pattern: string) {
  const ps = pattern.split('/').filter(Boolean)
  return (path: string): Record<string, string> | null => {
    const xs = path.split('/').filter(Boolean)
    if (xs.length !== ps.length) return null
    const out: Record<string, string> = {}
    for (let i = 0; i < ps.length; i++) {
      if (ps[i].startsWith(':')) out[ps[i].slice(1)] = decodeURIComponent(xs[i])
      else if (ps[i] !== xs[i]) return null
    }
    return out
  }
}

export function compile<R>(route: R, pattern: string): CompiledRoute<R> {
  const UP = (globalThis as unknown as { URLPattern?: URLPatternCtor }).URLPattern
  if (UP) {
    const p = new UP({ pathname: pattern })
    return {
      route, pattern,
      test: (path) => {
        const m = p.exec({ pathname: path })
        if (!m) return null
        const out: Record<string, string> = {}
        for (const [k, v] of Object.entries(m.pathname.groups)) if (v !== undefined) out[k] = v
        return out
      },
    }
  }
  return { route, pattern, test: segmentMatcher(pattern) }
}

export function rank(pattern: string): [number, number] {
  const segs = pattern.split('/').filter(Boolean)
  const params = segs.filter((s) => s.startsWith(':')).length
  return [segs.length - params, params]
}

export function sortRoutes<R>(routes: CompiledRoute<R>[]): CompiledRoute<R>[] {
  return routes.sort((a, b) => {
    const [sa, pa] = rank(a.pattern)
    const [sb, pb] = rank(b.pattern)
    return sb - sa || pa - pb
  })
}
