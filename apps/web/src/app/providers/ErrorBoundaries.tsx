// Error boundaries at three levels (M15-FR-008; AWR-14 §7.6): root (whole shell), viewport (canvas host) and one per panel.
// A panel failure (M15-E004, Base UI error #31 included) replaces only that panel body with an Empty state and a
// "reload panel" action; the rest of the shell keeps working.
import * as React from 'react'
import { t } from '@/app/i18n'
import { Button } from '@/ui/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/ui/components/ui/empty'
import { uxDefect } from '@/ui/testing/uxProbe'

interface State { error: Error | null; attempt: number }
class Boundary extends React.Component<{ children: React.ReactNode; fallback: (e: Error, retry: () => void) => React.ReactNode; tag: string }, State> {
  state: State = { error: null, attempt: 0 }
  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }
  componentDidCatch(error: Error): void {
    uxDefect(`boundary.${this.props.tag}`)
    console.error(`M15-E004 ${this.props.tag}`, error)
  }
  retry = () => this.setState((s) => ({ error: null, attempt: s.attempt + 1 }))
  render() {
    if (this.state.error) return this.props.fallback(this.state.error, this.retry)
    return <React.Fragment key={this.state.attempt}>{this.props.children}</React.Fragment>
  }
}

export function RootErrorBoundary({ children }: { children: React.ReactNode }) {
  return (
    <Boundary tag="root" fallback={(_e, retry) => (
      <div className="fixed inset-0 flex items-center justify-center bg-background">
        <Empty>
          <EmptyHeader>
            <EmptyTitle>{t('error.root.title')}</EmptyTitle>
            <EmptyDescription>{t('error.root.body')}</EmptyDescription>
          </EmptyHeader>
          <Button variant="outline" onClick={retry}>{t('common.retry')}</Button>
        </Empty>
      </div>
    )}>
      {children}
    </Boundary>
  )
}

export function ViewportErrorBoundary({ children }: { children: React.ReactNode }) {
  return (
    <Boundary tag="viewport" fallback={(_e, retry) => (
      <div className="app-layer-canvas flex items-center justify-center">
        <Empty>
          <EmptyHeader>
            <EmptyTitle>{t('error.viewport.title')}</EmptyTitle>
            <EmptyDescription>{t('error.viewport.body')}</EmptyDescription>
          </EmptyHeader>
          <Button variant="outline" onClick={retry}>{t('common.retry')}</Button>
        </Empty>
      </div>
    )}>
      {children}
    </Boundary>
  )
}

export function PanelErrorBoundary({ children, title }: { children: React.ReactNode; title: string }) {
  return (
    <Boundary tag="panel" fallback={(_e, retry) => (
      <Empty className="p-4">
        <EmptyHeader>
          <EmptyTitle>{t('error.panel.title', { panel: title })}</EmptyTitle>
        </EmptyHeader>
        <Button size="sm" variant="outline" onClick={retry}>{t('error.panel.reload')}</Button>
      </Empty>
    )}>
      {children}
    </Boundary>
  )
}
