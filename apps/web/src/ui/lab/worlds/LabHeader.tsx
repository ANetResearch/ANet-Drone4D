// 内部工作区页面的标题行（AWR-14 §21.2；/lab/worlds、/lab/events、/lab/runs 共用）：图标、页面标题、会话（主体 · 角色）、
// 工作区导航（世界、事件、运行记录、原型排行榜；当前页不重复显示）与刷新。页面标题按 §21.2.2 规则 4 写入 document.title。
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import type { BenchSession } from '@/net/bench'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'

type LabPage = 'worlds' | 'events' | 'runs'
const NAV: readonly { page: LabPage | 'board'; path: string; icon: IconKey; label: string }[] = [
  { page: 'worlds', path: '/lab/worlds', icon: 'nav.world', label: 'lab.worlds.title' },
  { page: 'events', path: '/lab/events', icon: 'mission.list', label: 'lab.events.title' },
  { page: 'runs', path: '/lab/runs', icon: 'data.folder', label: 'lab.runs.title' },
  { page: 'board', path: '/lab/board', icon: 'perf.bench', label: 'lab.board.title' },
]

export function useDocTitle(title: string): void {
  React.useEffect(() => {
    document.title = title
  }, [title])
}

export function LabHeader({ page, icon, title, session, onRefresh, children }: {
  page: LabPage; icon: IconKey; title: React.ReactNode; session: BenchSession | null; onRefresh: () => void; children?: React.ReactNode
}) {
  const t = useT()
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2" data-lab-header={page}>
      <Icon icon={icon} />
      <h1 className="text-ed-title font-bold">{title}</h1>
      <span className="text-hud-sub text-muted-foreground">{session ? t('lab.session', { name: session.principal, role: t(`lab.role.${session.role}`) }) : ''}</span>
      {children}
      <nav className="ml-auto flex items-center gap-1" aria-label={t('lab.worlds.nav')}>
        {NAV.filter((n) => n.page !== page).map((n) => (
          <Button key={n.page} size="sm" variant="ghost" onClick={() => navigate(n.path)} data-lab-nav={n.page}>
            <Icon icon={n.icon} data-icon="inline-start" />
            {t(n.label)}
          </Button>
        ))}
        <Button size="sm" variant="ghost" onClick={onRefresh} data-lab-refresh="">
          <Icon icon="refresh" data-icon="inline-start" />
          {t('lab.refresh')}
        </Button>
      </nav>
    </div>
  )
}
