// Home-made router (M15-FR-003, §6.3.2; ADR-028 "no router library in D1"): native URLPattern and history, routes
// registered by app/routes/*.tsx (import.meta.glob extension point), ext routes only when delivered. Unknown paths and
// failed validations redirect to /worlds with one toast. Query writes of camera and selection use replaceState
// (1 Hz, INPUT.urlSyncHz); route changes pushState.
import type * as React from 'react'
import { useSyncExternalStore } from 'react'
import { compile, sortRoutes, type CompiledRoute } from './match'

export type RouteLayer = 'sandbox' | 'overlay' | 'page'
export interface RouteProps { params: Readonly<Record<string, string>>; search: URLSearchParams }
export interface RouteDef {
  id: string
  pattern: string
  layer: RouteLayer
  d1: 'core' | 'ext'
  component?: React.ComponentType<RouteProps>
  validate?: (p: Record<string, string>) => boolean
  /** returns the path to redirect to (replace) */
  redirect?: (p: Record<string, string>, search: URLSearchParams) => string
  /** dev/test builds only (the design sample page) */
  testOnly?: boolean
}
export interface RouteMatch { route: RouteDef; params: Readonly<Record<string, string>>; search: URLSearchParams; path: string }

let table: CompiledRoute<RouteDef>[] = []
export function setRoutes(routes: readonly RouteDef[]): void {
  table = sortRoutes(routes.map((r) => compile(r, r.pattern)))
}
export const routes = (): readonly RouteDef[] => table.map((c) => c.route)

export function matchPath(path: string, search = ''): RouteMatch | null {
  for (const c of table) {
    const params = c.test(path)
    if (params && (!c.route.validate || c.route.validate(params))) return { route: c.route, params, search: new URLSearchParams(search), path }
  }
  return null
}

const listeners = new Set<() => void>()
let current: RouteMatch | null = null
let notFoundHook: ((path: string) => void) | null = null
export const onNotFound = (cb: (path: string) => void): void => {
  notFoundHook = cb
}

function resolve(): void {
  const path = location.pathname
  const m = matchPath(path, location.search)
  if (!m) {
    notFoundHook?.(path)
    history.replaceState(null, '', '/worlds')
    current = matchPath('/worlds', '')
  } else if (m.route.redirect) {
    history.replaceState(null, '', m.route.redirect(m.params, m.search))
    current = matchPath(location.pathname, location.search)
  } else current = m
  for (const l of listeners) l()
}

export function navigate(to: string, o: { replace?: boolean } = {}): void {
  if (o.replace) history.replaceState(null, '', to)
  else history.pushState(null, '', to)
  resolve()
}

/** replace query parameters without a history entry; parameters of other modules are kept */
export function updateSearch(patch: Record<string, string | null>): void {
  const q = new URLSearchParams(location.search)
  for (const [k, v] of Object.entries(patch)) {
    if (v === null) q.delete(k)
    else q.set(k, v)
  }
  const s = q.toString()
  history.replaceState(null, '', `${location.pathname}${s ? `?${s}` : ''}`)
  if (current) current = { ...current, search: q }
  for (const l of listeners) l()
}

let started = false
export function startRouter(): void {
  if (started) return
  started = true
  addEventListener('popstate', resolve)
  resolve()
}

const subscribe = (cb: () => void) => {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}
const snap = () => current
export function useRoute(): RouteMatch | null {
  return useSyncExternalStore(subscribe, snap, snap)
}
