// 公开只读构建的路由（AWR-14 §21.2.2、§21.14；扩展点 15 的 /lab/ 独立静态构建）。Owner: M15.
// 只用 History API，不加载应用路由表（app/router/table.ts 收集的 D1/D2 页面与内部工作区页面都不在本构建中）。
// - 内置路由：`/lab`（样例目录，首页）、`/lab/samples`（同一目录）、`/lab/samples/:sid`（样例运行；缺省视图列出可下载的制品）；
// - 扩展：`./samples/*.route.tsx` 各导出 `route: LabPublicRoute`（样例页的事件回放、多机私有视角与检视切片，F07），同一 pattern
//   覆盖内置路由；路由 id 一律以 `lab` 前缀登记；
// - 其他 `/lab/*` 与 /lab/ 之外的路径：回到首页并提示"页面不存在，已打开事件沙盘首页"（AWR-14 §21.2.2 末行）。
import type * as React from 'react'
import type { LabStaticIndex } from './staticIndex'

export interface LabRouteProps {
  params: Readonly<Record<string, string>>
  index: LabStaticIndex
}
export interface LabPublicRoute {
  id: string
  pattern: string
  /** 页面标题（document.title 为 `<标题> · 事件沙盘 · ANet4D 公开站`） */
  title: (p: LabRouteProps, t: (key: string, params?: Record<string, string | number>) => string) => string
  render: (p: LabRouteProps) => React.ReactNode
}
export interface LabMatch {
  route: LabPublicRoute
  params: Record<string, string>
}

export const LAB_PREFIX = '/lab'

/** pattern 段 `:name` 匹配一个非空段；末尾斜杠忽略 */
export function matchPattern(pattern: string, path: string): Record<string, string> | null {
  const norm = (p: string): string[] => p.replace(/\/+$/, '').split('/').filter((s, i) => i === 0 || s !== '')
  const a = norm(pattern)
  const b = norm(path)
  if (a.length !== b.length) return null
  const out: Record<string, string> = {}
  for (let i = 0; i < a.length; i++) {
    if (a[i].startsWith(':')) {
      let v: string
      try {
        v = decodeURIComponent(b[i])
      } catch {
        return null
      }
      out[a[i].slice(1)] = v
    } else if (a[i] !== b[i]) return null
  }
  return out
}

/** 按登记顺序匹配：扩展路由在前（同一 pattern 覆盖内置路由） */
export function matchLabRoute(routes: readonly LabPublicRoute[], path: string): LabMatch | null {
  for (const route of routes) {
    const params = matchPattern(route.pattern, path)
    if (params) return { route, params }
  }
  return null
}

/** 合并扩展路由与内置路由，校验 id 前缀与 pattern 前缀；同一 pattern 的扩展路由排在内置路由之前 */
export function composeRoutes(ext: readonly LabPublicRoute[], builtin: readonly LabPublicRoute[]): LabPublicRoute[] {
  for (const r of [...ext, ...builtin]) {
    if (!r.id.startsWith('lab')) throw new Error(`lab-public route ${r.id}: route ids carry the lab prefix (AWR-14 §21.2.2)`)
    if (r.pattern !== LAB_PREFIX && !r.pattern.startsWith(`${LAB_PREFIX}/`)) throw new Error(`lab-public route ${r.id}: pattern ${r.pattern} outside ${LAB_PREFIX}`)
  }
  const ids = new Set<string>()
  for (const r of ext) {
    if (ids.has(r.id)) throw new Error(`lab-public route ${r.id}: registered twice`)
    ids.add(r.id)
  }
  return [...[...ext].sort((a, b) => a.id.localeCompare(b.id)), ...builtin]
}

const listeners = new Set<() => void>()
let notFoundFlag = false
const emit = (): void => {
  for (const cb of listeners) cb()
}

export function currentPath(): string {
  return typeof location === 'undefined' ? LAB_PREFIX : location.pathname
}

/** 路由切换（pushState）；只接受 /lab 前缀的同源路径；清除"页面不存在"提示 */
export function labNavigate(path: string, replace = false): void {
  if (path !== LAB_PREFIX && !path.startsWith(`${LAB_PREFIX}/`)) throw new Error(`labNavigate: ${path} outside ${LAB_PREFIX}`)
  notFoundFlag = false
  if (replace) history.replaceState(null, '', path)
  else history.pushState(null, '', path)
  emit()
}

/** 未匹配的路径回到首页（replaceState），并显示"页面不存在"提示，直到关闭或下一次站内导航 */
export function redirectNotFound(): void {
  labNavigate(`${LAB_PREFIX}/`, true)
  notFoundFlag = true
  emit()
}
export const notFoundNotice = (): boolean => notFoundFlag
export function dismissNotFound(): void {
  notFoundFlag = false
  emit()
}

export function subscribeLocation(cb: () => void): () => void {
  listeners.add(cb)
  const pop = (): void => {
    notFoundFlag = false
    cb()
  }
  globalThis.addEventListener?.('popstate', pop)
  return () => {
    listeners.delete(cb)
    globalThis.removeEventListener?.('popstate', pop)
  }
}

/** 站内链接的点击：普通左键点击改为 pushState，修饰键与中键保留浏览器行为 */
export function onLabLinkClick(e: React.MouseEvent<HTMLAnchorElement>, path: string): void {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
  e.preventDefault()
  labNavigate(path)
}
