// Provider stack (M15-FR-002): RootErrorBoundary > I18n > Theme (dark, no hotkey) > Query > Tooltip (delay 400 ms,
// closeDelay 0, timeout 400 ms from --duration-slow, M15-FR-058) > Toast (limit 3) > Rt.
import type * as React from 'react'
import { MOTION } from '@/lib/tokens/motion.gen'
import { TooltipProvider } from '@/ui/components/ui/tooltip'
import { RtProvider } from '@/ui/shell/RtContext'
import { RootErrorBoundary } from './ErrorBoundaries'
import { I18nProvider } from './I18nProvider'
import { QueryProvider } from './QueryProvider'
import { ThemeProvider } from './ThemeProvider'
import { ToastProvider } from './ToastProvider'

export function ErrorBoundaryShell({ children }: { children: React.ReactNode }) {
  return (
    <RootErrorBoundary>
      <I18nProvider>
        <ThemeProvider theme="dark">
          <QueryProvider>
            <TooltipProvider delay={MOTION.durationSlowMs} closeDelay={0} timeout={MOTION.durationSlowMs}>
              <ToastProvider>
                <RtProvider>{children}</RtProvider>
              </ToastProvider>
            </TooltipProvider>
          </QueryProvider>
        </ThemeProvider>
      </I18nProvider>
    </RootErrorBoundary>
  )
}
