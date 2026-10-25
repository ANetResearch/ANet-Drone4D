// 公开样例页的请求守卫（AWR-14 §21.14.2 规则 1；D3-AC-21"只读页只发起静态 GET 请求"；D3-AC-25"公开样例页的面板只读取静态切片、
// 不发起 catalog 请求"）。Owner: M15.
// 样例运行页挂载期间包装 window.fetch 与 navigator.sendBeacon：只放行同源 /lab/ 与 /lab-static/ 的 GET、HEAD，其余请求在浏览器内
// 直接拒绝、不发出。本页自身只经 labGet 请求；守卫面向页面内的第三方组件——数据检视面板的 Rerun Web Viewer（锁定版本 0.38.1）
// 内置使用统计上报（tel.rerun.io），公开站不得向第三方发送请求。
// 嵌套挂载按引用计数；卸载时若其他代码已在守卫之上再包装 fetch（viewerLoader 在加载期间临时包装以统计 Wasm 字节），守卫改为直通，
// 不破坏对方的包装链。
import { isAllowedLabUrl } from '../staticIndex'

type Fetch = typeof globalThis.fetch

let depth = 0
let blocked = 0
let release: (() => void) | null = null

/** 地址是否可放行：同源且在 /lab/、/lab-static/ 之下 */
export function allowedRequest(url: string, method: string, base: string): boolean {
  const m = method.toUpperCase()
  if (m !== 'GET' && m !== 'HEAD') return false
  let u: URL
  try {
    u = new URL(url, base)
  } catch {
    return false
  }
  if (u.origin !== new URL(base).origin) return false
  return isAllowedLabUrl(u.pathname + u.search)
}

const urlOf = (input: RequestInfo | URL): string => (typeof input === 'string' ? input : input instanceof URL ? input.href : input.url)
const methodOf = (input: RequestInfo | URL, init?: RequestInit): string =>
  init?.method ?? (typeof input === 'object' && !(input instanceof URL) ? input.method : 'GET')

/** 守卫拒绝的请求数（页面数据属性与测试读取） */
export const blockedRequests = (): number => blocked

/** 安装守卫；返回卸载函数 */
export function installStaticGuard(): () => void {
  const g = globalThis as { fetch?: Fetch; location?: Location; navigator?: Navigator }
  if (typeof g.fetch !== 'function' || !g.location) return () => undefined
  if (depth++ === 0) {
    const prev = g.fetch
    const st = { active: true }
    const base = g.location.href
    const guarded: Fetch = (input, init) => {
      if (!st.active || allowedRequest(urlOf(input), methodOf(input, init), base)) return prev(input, init)
      blocked++
      return Promise.reject(new TypeError(`request blocked on the public read-only page: ${methodOf(input, init)} ${urlOf(input)}`))
    }
    g.fetch = guarded
    // sendBeacon 一般在 Navigator.prototype 上：守卫以实例自有属性覆盖，卸载时还原原有的自有属性描述或删除覆盖
    const nav = g.navigator
    const prevBeacon: unknown = nav ? Reflect.get(nav, 'sendBeacon') : undefined
    const prevDesc = nav ? Object.getOwnPropertyDescriptor(nav, 'sendBeacon') : undefined
    const beacon = (url: string | URL, data?: BodyInit | null): boolean => {
      if (!st.active && typeof prevBeacon === 'function') return Reflect.apply(prevBeacon, nav, [url, data]) as boolean
      blocked++
      return false
    }
    const hasBeacon = nav !== undefined && typeof prevBeacon === 'function'
    if (hasBeacon) Object.defineProperty(nav, 'sendBeacon', { value: beacon, configurable: true, writable: true })
    release = () => {
      // 守卫改为直通；其他代码在守卫之上再包装时不还原（还原会丢掉对方的包装）
      st.active = false
      if (g.fetch === guarded) g.fetch = prev
      if (hasBeacon && Reflect.get(nav, 'sendBeacon') === beacon) {
        if (prevDesc) Object.defineProperty(nav, 'sendBeacon', prevDesc)
        else Reflect.deleteProperty(nav, 'sendBeacon')
      }
    }
  }
  let done = false
  return () => {
    if (done) return
    done = true
    if (--depth === 0) {
      release?.()
      release = null
    }
  }
}
