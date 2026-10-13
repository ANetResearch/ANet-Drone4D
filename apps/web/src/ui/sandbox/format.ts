// Small text formatters of the sandbox UI shared by the session bar and the open interface page (kept apart from
// SessionBar.tsx so the page /sandbox/api does not pull the header components; M15-FR-221).

/** mm:ss of a lifetime in seconds ("—" when unknown) */
export function lifetimeText(s: number): string {
  if (!Number.isFinite(s)) return '—'
  const v = Math.max(0, Math.floor(s))
  return `${String(Math.floor(v / 60)).padStart(2, '0')}:${String(v % 60).padStart(2, '0')}`
}
