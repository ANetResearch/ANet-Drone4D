// Build information of this bundle (R-D2-29; ADR-118; AWR-04 §15.4; M15-FR-223; UX-FR-148). Owner: M15.
// The vite plugin buildInfoPlugin() (vite.config.ts, M00) defines `__AWR_BUILD__` = {version, channel, commit, dirty,
// built_at, release} and writes the same display text into the static `.boot-version` line of index.html (visible before any
// module script runs and on a boot error). This module only reads the constant and formats it the same way as the plugin's
// buildDisplay(): `V0.2.0-demo · 3f9a1c2 · 2026-10-12` (`-dirty` after the commit, `nogit` without git).
// Kept free of imports so the demo entry (app/demo/entry.tsx, the landing chunk) can use it without pulling the
// application in. `diagnostics()` is the text of "copy diagnostic information" (About, Settings, the boot version line).

export interface BuildInfo {
  version: string
  channel: 'demo' | 'local' | 'ci'
  commit: string
  dirty: boolean
  built_at: string
  release: string
}

declare const __AWR_BUILD__: BuildInfo | undefined

/** the fallback of a bundle built without the plugin (unit tests without the define, older builds) */
export const UNKNOWN_BUILD: BuildInfo = { version: '0.2.0', channel: 'local', commit: 'nogit', dirty: false, built_at: '1970-01-01T00:00:00Z', release: 'local-nogit' }

function read(): BuildInfo {
  try {
    if (typeof __AWR_BUILD__ !== 'undefined' && __AWR_BUILD__) return __AWR_BUILD__
  } catch {
    // not defined by the bundler
  }
  const g = (globalThis as { __AWR_BUILD__?: BuildInfo }).__AWR_BUILD__
  return g ?? UNKNOWN_BUILD
}

/** this bundle's build (constant for the page lifetime) */
export const BUILD: BuildInfo = read()

/** short commit for display: 7 hex digits, `-dirty` appended, `nogit` as is */
export function commitShort(b: BuildInfo = BUILD): string {
  const c = b.commit === 'nogit' || !b.commit ? 'nogit' : b.commit.slice(0, 7)
  return `${c}${b.dirty && c !== 'nogit' ? '-dirty' : ''}`
}

/** the display line of the boot mask, About and Settings (same text as vite.config.ts buildDisplay) */
export function buildDisplay(b: BuildInfo = BUILD): string {
  const c = b.commit === 'nogit' || !b.commit ? 'nogit' : b.commit.slice(0, 7)
  return `V${b.version}-${b.channel} · ${c}${b.dirty ? '-dirty' : ''} · ${(b.built_at || '').slice(0, 10)}`
}

/** `V0.2.0-demo · 3f9a1c2` (the power-save card and narrow places) */
export function buildShort(b: BuildInfo = BUILD): string {
  return `V${b.version}-${b.channel} · ${commitShort(b)}`
}

/** the multi-line diagnostic text (copy actions): every build field, the page and the browser */
export function diagnostics(extra: Record<string, string | number | null | undefined> = {}, b: BuildInfo = BUILD): string {
  const lines = [
    `build: ${buildDisplay(b)}`,
    `version: ${b.version}`,
    `channel: ${b.channel}`,
    `commit: ${b.commit}${b.dirty ? ' (dirty)' : ''}`,
    `built_at: ${b.built_at}`,
    `release: ${b.release}`,
  ]
  if (typeof location !== 'undefined') lines.push(`page: ${location.pathname}`)
  if (typeof navigator !== 'undefined') lines.push(`agent: ${navigator.userAgent}`)
  if (typeof document !== 'undefined') {
    const ff = document.documentElement.dataset.ff
    if (ff) lines.push(`form_factor: ${ff}${document.documentElement.hasAttribute('data-coarse') ? ' coarse' : ''}`)
  }
  for (const [k, v] of Object.entries(extra)) if (v !== null && v !== undefined && v !== '') lines.push(`${k}: ${v}`)
  return lines.join('\n')
}

/** copy text to the clipboard; false when the API is missing or refused (insecure context, permissions) */
export async function copyText(text: string): Promise<boolean> {
  try {
    const nav = globalThis.navigator as Navigator | undefined
    if (!nav?.clipboard?.writeText) return false
    await nav.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}
