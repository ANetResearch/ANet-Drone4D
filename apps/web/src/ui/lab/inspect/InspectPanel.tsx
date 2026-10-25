// 数据检视面板（Rerun Web Viewer 的宿主外壳，AWR-14 §21.9.1、§21.9.2；AWR-05 §6.1.6 文案行：标题、空状态、加载与错误提示由宿主提供，
// Rerun 内部界面保持英文、不承载业务操作）。内部工作区（/lab/runs/:rid/inspect，catalog 来源）与 /lab/ 公开样例页（F07，静态切片
// 来源）共用：文案函数 t 由调用方传入（内部为应用词典，公开构建为其分片词典），本组件不引用 net/bench.ts、应用路由与应用词典。
// Viewer 画布的父节点始终挂载；各状态的外壳以不透明覆盖层盖住画布与 Viewer 自带的加载遮罩。
import * as React from 'react'
import { Alert, AlertDescription, AlertTitle } from '@/ui/components/ui/alert'
import { Button } from '@/ui/components/ui/button'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyTitle } from '@/ui/components/ui/empty'
import { Progress } from '@/ui/components/ui/progress'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { cn } from '@/lib/utils'
import { fmtMb, type InspectSource, type Support } from './inspect'
import { InspectController, type InspectDeps, type InspectSnapshot } from './inspectController'
import type { LabSelection, PlayheadPort } from './rerunSync'

export type InspectT = (key: string, params?: Record<string, string | number>) => string

export interface InspectPanelProps {
  source: InspectSource
  playhead: PlayheadPort
  support: Support
  t: InspectT
  /** UNSUPPORTED 时的替代操作：公开站"下载切片"，内部"在原生 Viewer 中打开" */
  fallback?: React.ReactNode
  onSelect?: (sel: LabSelection) => void
  onRange?: (minS: number, maxS: number) => void
  /** 控制器就绪（页面据此转发拖动与场景选择） */
  onController?: (c: InspectController | null) => void
  /** 测试注入（假 Viewer） */
  start?: InspectDeps['start']
  className?: string
}

const STATE_ATTR: Record<InspectSnapshot['state'], string> = {
  UNSUPPORTED: 'unsupported', IDLE: 'idle', LOADING: 'loading', OPENING: 'opening', READY: 'ready', ERROR: 'error',
}

export function useInspectSnapshot(c: InspectController): InspectSnapshot {
  return React.useSyncExternalStore((cb) => c.subscribe(cb), () => c.snapshot, () => c.snapshot)
}

export function InspectPanel({ source, playhead, support, t, fallback, onSelect, onRange, onController, start, className }: InspectPanelProps) {
  const host = React.useRef<HTMLDivElement>(null)
  const cbs = React.useRef({ onSelect, onRange })
  cbs.current = { onSelect, onRange }
  // 来源变化（切换 catalog、换样例）时重建控制器
  const key = source.kind === 'catalog' ? `catalog:${source.catalog}` : `static:${source.recordingId}:${source.slices.map((s) => s.url).join(',')}`
  const ctl = React.useMemo(
    () => new InspectController({
      source, playhead, support, start,
      onSelect: (s) => cbs.current.onSelect?.(s),
      onRange: (a, b) => cbs.current.onRange?.(a, b),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key, playhead, support.ok],
  )
  const snap = useInspectSnapshot(ctl)
  React.useEffect(() => {
    onController?.(ctl)
    // 测试钩子（开发与测试构建）：Playwright 读取状态与同步计数（window.__awrInspect.snapshot、.rerunSync.stats()）
    const g = globalThis as { __awrInspect?: InspectController | null }
    if (TEST_SWITCHES) g.__awrInspect = ctl
    return () => {
      onController?.(null)
      if (TEST_SWITCHES && g.__awrInspect === ctl) g.__awrInspect = null
      ctl.dispose()
    }
  }, [ctl]) // eslint-disable-line react-hooks/exhaustive-deps
  React.useEffect(() => {
    // 首次进入自动开始（§21.9.2 IDLE 行）
    if (snap.state === 'IDLE' && snap.autoStart && host.current) void ctl.start(host.current)
  }, [ctl, snap.state, snap.autoStart])

  return (
    <div data-inspect-panel="" data-inspect-state={STATE_ATTR[snap.state]} className={cn('relative flex min-h-0 flex-1 flex-col', className)}>
      <div ref={host} data-inspect-viewer="" className="relative min-h-0 flex-1 overflow-hidden" />
      {snap.state === 'READY' ? null : (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-background p-4">
          <PanelBody snap={snap} t={t} fallback={fallback}
            onLoad={() => host.current && void ctl.start(host.current)} onRetry={() => void ctl.retry()} />
        </div>
      )}
      {snap.state === 'READY' ? (
        <div className="absolute top-2 right-2 z-10">
          <Button size="sm" variant="outline" onClick={() => ctl.close()} data-inspect-close="">{t('lab.inspect.close')}</Button>
        </div>
      ) : null}
    </div>
  )
}

function PanelBody({ snap, t, fallback, onLoad, onRetry }: {
  snap: InspectSnapshot; t: InspectT; fallback?: React.ReactNode; onLoad: () => void; onRetry: () => void
}) {
  switch (snap.state) {
    case 'UNSUPPORTED':
      return (
        <Empty className="gap-3 border border-border p-6" data-inspect-unsupported={snap.unsupported && !snap.unsupported.ok ? snap.unsupported.reason : ''}>
          <EmptyHeader>
            <EmptyTitle className="text-hud-title">{t('lab.inspect.unsupported.title')}</EmptyTitle>
            <EmptyDescription className="text-hud-sub">
              {snap.unsupported && !snap.unsupported.ok ? t(`lab.inspect.unsupported.${snap.unsupported.reason}`) : ''}
            </EmptyDescription>
          </EmptyHeader>
          {fallback ? <EmptyContent>{fallback}</EmptyContent> : null}
        </Empty>
      )
    case 'IDLE':
      return (
        <Empty className="gap-3 border border-border p-6">
          <EmptyHeader>
            <EmptyTitle className="text-hud-title">{t('lab.inspect.idle.title')}</EmptyTitle>
            <EmptyDescription className="text-hud-sub">{t('lab.inspect.idle.hint')}</EmptyDescription>
          </EmptyHeader>
          <EmptyContent>
            <Button size="sm" onClick={onLoad} data-inspect-load="">{t('lab.inspect.load')}</Button>
          </EmptyContent>
        </Empty>
      )
    case 'LOADING':
      return (
        <div className="flex w-80 flex-col gap-2" data-inspect-progress={snap.receivedBytes}>
          <Progress value={Math.round((100 * snap.receivedBytes) / Math.max(snap.totalBytes, 1))} aria-label={t('lab.inspect.loadingAria')} />
          <p className="text-hud-sub tabular-nums text-muted-foreground">
            {t('lab.inspect.loading', { mb: fmtMb(snap.receivedBytes), total: fmtMb(snap.totalBytes) })}
          </p>
        </div>
      )
    case 'OPENING':
      return (
        <div className="flex size-full flex-col gap-2">
          <Skeleton className="min-h-0 flex-1" />
          <p className="text-hud-sub text-muted-foreground">{t('lab.inspect.opening')}</p>
        </div>
      )
    case 'ERROR':
      return (
        <Alert className="max-w-xl border-destructive" data-inspect-error={snap.error?.kind ?? 'unknown'}>
          <Icon icon="alert.warning" />
          <AlertTitle>{t(`lab.inspect.error.${snap.error?.kind ?? 'unknown'}`)}</AlertTitle>
          <AlertDescription>
            {snap.error?.kind === 'version' && snap.error.detail ? <span className="block font-mono">{snap.error.detail}</span> : null}
            <Button size="sm" variant="outline" className="mt-2" onClick={onRetry} data-inspect-retry="">{t('lab.inspect.retry')}</Button>
          </AlertDescription>
        </Alert>
      )
    default:
      return null
  }
}
