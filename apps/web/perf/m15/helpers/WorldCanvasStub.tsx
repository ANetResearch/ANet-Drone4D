// Test-only stand-in for viewport/WorldCanvas (M06), used by perf/m15/helpers/build-ui.mjs when the M15 UI specs run in
// UI-only mode (`?viewport=off`) while the renderer is being changed in parallel. Never part of a shipped build.
export function WorldCanvas() {
  return null
}
