// 公开只读构建的外壳（AWR-14 §21.2.4 的顶栏布局、§21.14；ADR-129；D3-AC-21）。Owner: M15.
// 顶栏 44 px：产品名"ANet Drone4D · 事件沙盘"与"公开站"徽标、导航（样例目录）、语言切换、交互沙盒与文档链接；内容区为当前路由
// 的页面；页脚为制品版本与许可署名。没有登录、会话、队列与资源指示，不建立任何 WS，只经 labGet 读取 /lab-static/（staticIndex.ts）。
// 规则：
// 1. 静态制品索引在启动时读取一次；之后每次路由切换复核一次（no-cache，浏览器带 ETag），版本变化时显示非模态横条
//    "样例数据已更新 · [刷新]"，不自动替换正在查看的数据（AWR-14 §21.14.2 规则 3）；
// 2. 未匹配的路径回到首页并显示一次"页面不存在，已打开事件沙盘首页"（AWR-14 §21.2.2 末行）；
// 3. 页面标题为 `<页面> · 事件沙盘 · ANet Drone4D 公开站`（AWR-14 §21.2.2 规则 4）。
import * as React from 'react'
import { DEMO_DOCS_URL } from '@/lib/demo'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import { getLabLocale, setLabLocale, useLabT } from './i18n'
import { BUILTIN_ROUTES, LabEmpty } from './pages'
import { composeRoutes, currentPath, dismissNotFound, LAB_PREFIX, matchLabRoute, notFoundNotice, onLabLinkClick, redirectNotFound, subscribeLocation, type LabPublicRoute } from './router'
import { loadStaticIndex, type LabFetch, type LabStaticIndex } from './staticIndex'

const EXT = import.meta.glob<{ route: LabPublicRoute }>('./samples/*.route.tsx', { eager: true })
export const LAB_ROUTES: readonly LabPublicRoute[] = composeRoutes(Object.values(EXT).map((m) => m.route), BUILTIN_ROUTES)

const BRAND_AVATAR = `${import.meta.env.BASE_URL}brand/avatar-96.png`

type IndexState = { kind: 'loading' } | { kind: 'ready'; index: LabStaticIndex } | { kind: 'error'; code: string }

function useLocationPath(): string {
  return React.useSyncExternalStore(subscribeLocation, currentPath, currentPath)
}

function Header() {
  const t = useLabT()
  const other = getLabLocale() === 'zh-CN' ? 'en' : 'zh-CN'
  return (
    <header className="sticky top-0 z-(--z-header) border-b border-border bg-background">
      <div className="mx-auto flex h-11 max-w-6xl items-center gap-3 px-4 sm:px-6">
        <a href={`${LAB_PREFIX}/`} onClick={(e) => onLabLinkClick(e, `${LAB_PREFIX}/`)} className="flex min-h-11 items-center gap-2.5 rounded-md">
          <img src={BRAND_AVATAR} width={20} height={20} alt="" aria-hidden="true" className="size-5 shrink-0" />
          <span className="text-sm font-semibold tracking-tight whitespace-nowrap">{t('lab.public.brand')}</span>
        </a>
        <Badge variant="outline" className="max-sm:hidden" data-lab-public-badge="">{t('lab.public.site')}</Badge>
        <nav className="ml-2 hidden items-center gap-0.5 md:flex" aria-label={t('lab.public.brand')}>
          <Button variant="ghost" size="sm" className="h-8 px-3 text-sm" nativeButton={false}
            render={<a href={`${LAB_PREFIX}/`} onClick={(e) => onLabLinkClick(e, `${LAB_PREFIX}/`)} />}>
            {t('lab.public.nav.samples')}
          </Button>
        </nav>
        <div className="ml-auto flex items-center gap-1">
          <Button variant="ghost" size="sm" className="h-8 px-3 text-sm" lang={other} onClick={() => setLabLocale(other, true)} data-lab-lang="">
            {t('lab.public.lang')}
          </Button>
          <Button variant="ghost" size="sm" className="h-8 px-3 text-sm max-sm:hidden" nativeButton={false} render={<a href="/" />}>
            {t('lab.public.nav.sandbox')}
          </Button>
          <Button variant="ghost" size="sm" className="h-8 px-3 text-sm max-sm:hidden" nativeButton={false}
            render={<a href={DEMO_DOCS_URL} target="_blank" rel="noreferrer noopener" />}>
            {t('lab.public.nav.docs')}
            <Icon icon="external" data-icon="inline-end" />
          </Button>
        </div>
      </div>
    </header>
  )
}

function Footer({ index }: { index: LabStaticIndex | null }) {
  const t = useLabT()
  if (!index) return null
  return (
    <footer className="mx-auto flex w-full max-w-6xl flex-col gap-2 border-t border-border px-4 py-4 text-hud-sub text-muted-foreground sm:px-6" data-lab-public-footer="">
      <span className="font-mono" data-lab-static-version={index.version}>{t('lab.public.version', { version: index.version })}</span>
      <div className="flex flex-col gap-1">
        <span className="font-semibold text-foreground">{t('lab.public.licenseTitle')}</span>
        {index.license.attribution.map((a) => <span key={a}>{a}</span>)}
        {index.license.files.length > 0 ? (
          <span>
            {t('lab.public.licenseFiles')}
            {index.license.files.map((f) => (
              <a key={f} href={index.url(f)} className="ml-2 font-mono underline-offset-4 hover:underline">{f}</a>
            ))}
          </span>
        ) : null}
      </div>
    </footer>
  )
}

function Banner({ text, action, onAction, onClose }: { text: string; action?: string; onAction?: () => void; onClose: () => void }) {
  const t = useLabT()
  return (
    <div role="status" className="mx-auto flex w-full max-w-6xl items-center gap-3 border-b border-border px-4 py-2 text-sm sm:px-6" data-lab-public-banner="">
      <span className="min-w-0 flex-1">{text}</span>
      {action && onAction ? <Button size="sm" variant="outline" onClick={onAction}>{action}</Button> : null}
      <Button size="icon" variant="ghost" className="size-8" aria-label={t('lab.public.dismiss')} onClick={onClose}>
        <Icon icon="close" />
      </Button>
    </div>
  )
}

export function LabPublicApp({ fetchFn, routes = LAB_ROUTES }: { fetchFn?: LabFetch; routes?: readonly LabPublicRoute[] }) {
  const t = useLabT()
  const path = useLocationPath()
  const notFound = React.useSyncExternalStore(subscribeLocation, notFoundNotice, notFoundNotice)
  const [state, setState] = React.useState<IndexState>({ kind: 'loading' })
  const [updated, setUpdated] = React.useState(false)
  const loadedVersion = state.kind === 'ready' ? state.index.version : null
  const first = React.useRef(true)

  const fetchIndex = React.useCallback(() => loadStaticIndex(fetchFn).then((index) => setState({ kind: 'ready', index }), (e: unknown) => {
    const code = typeof e === 'object' && e !== null && 'code' in e ? String((e as { code: unknown }).code) : 'static.http'
    setState({ kind: 'error', code })
  }), [fetchFn])
  React.useEffect(() => {
    void fetchIndex()
  }, [fetchIndex])
  const reload = (): void => {
    setUpdated(false)
    setState({ kind: 'loading' })
    void fetchIndex()
  }

  // rule 1: re-check the index on every route change after the first
  React.useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    if (loadedVersion === null) return
    let alive = true
    loadStaticIndex(fetchFn).then((idx) => alive && idx.version !== loadedVersion && setUpdated(true), () => undefined)
    return () => {
      alive = false
    }
  }, [path, fetchFn, loadedVersion])

  // rule 2: unknown paths go back to the home page with a one-time notice
  const match = matchLabRoute(routes, path)
  React.useEffect(() => {
    if (!match) redirectNotFound()
  }, [match])

  const index = state.kind === 'ready' ? state.index : null
  const title = match && index ? match.route.title({ params: match.params, index }, t) : t('lab.public.catalog.title')
  React.useEffect(() => {
    document.title = `${title} · ${t('lab.public.titleSuffix')}`
  }, [title, t])

  return (
    <div className="flex min-h-dvh flex-col bg-background text-foreground" data-lab-build="public">
      <Header />
      {notFound ? <Banner text={t('lab.public.notFound')} onClose={dismissNotFound} /> : null}
      {updated ? <Banner text={t('lab.public.updated')} action={t('lab.public.refresh')} onAction={reload} onClose={() => setUpdated(false)} /> : null}
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6">
        {state.kind === 'loading' ? (
          <p className="text-sm text-muted-foreground" data-lab-public-loading="">{t('lab.public.loading')}</p>
        ) : state.kind === 'error' ? (
          <LabEmpty title={t('lab.public.errorTitle')} description={t('lab.public.errorHint', { code: state.code })}
            action={<Button size="sm" variant="outline" onClick={reload}><Icon icon="refresh" data-icon="inline-start" />{t('lab.public.reload')}</Button>} />
        ) : match ? (
          match.route.render({ params: match.params, index: state.index })
        ) : null}
      </main>
      <Footer index={index} />
    </div>
  )
}
