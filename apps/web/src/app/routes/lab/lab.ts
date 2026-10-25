// lab 分片路由的公共定义（AWR-14 §21.2.2 规则 1；D3-AC-51）。每个 lab 功能在本目录新增 `<feature>.route.ts` 并导出 `route`，
// 由 app/router/table.ts 的第二个 glob 收集；本文件不是路由分片（文件名不以 .route.ts 结尾），不被收集。
// 交付：与 app/routes/lab*.tsx 相同，VITE_AWR_EXT_ROUTES 列出的路由 id 才注册；列出组名 `lab` 时一次交付本目录全部分片
// （内部 D3 构建只需写一项，新增分片不必改部署配置）。演示构建与 /lab/ 公开构建都不加载本目录（vite.config.ts LAB_INTERNAL_RE）。
import type { RouteDef } from '@/app/router/router'

/** 交付全部 lab 分片路由的组名 */
export const LAB_ROUTE_GROUP = 'lab'
const EXT = new Set(String(import.meta.env.VITE_AWR_EXT_ROUTES ?? '').split(',').map((s) => s.trim()).filter(Boolean))

/** 分片路由的 `d1`：组名已交付时视同核心路由（总是注册），否则为扩展路由（按 id 交付） */
export function labDelivery(ext: ReadonlySet<string> = EXT): RouteDef['d1'] {
  return ext.has(LAB_ROUTE_GROUP) ? 'core' : 'ext'
}

/** 路由 id 一律以 `lab-` 为前缀（AWR-14 §21.2.2 规则 1） */
export function labRoute(def: Omit<RouteDef, 'd1' | 'layer'> & { layer?: RouteDef['layer'] }): RouteDef {
  if (!def.id.startsWith('lab-')) throw new Error(`lab route id ${def.id} must start with "lab-" (AWR-14 §21.2.2 rule 1)`)
  return { layer: 'overlay', ...def, d1: labDelivery() }
}
