// Toasts (M15-FR-089; g07 §6 item 6): Base UI Toast through shadcn toast.tsx, limit 3 (the 4th gets data-limited), timeouts
// from INPUT (info 4 s, warning 6 s, critical 0 = sticky). The merge helper updates a toast with the same merge key
// (source:type:reason) instead of adding another one.
import type * as React from 'react'
import { Toaster, toast } from '@/ui/components/ui/toast'
import { INPUT, LIMITS } from '@/lib/tokens/input.gen'
import { sanitizeText } from '@/lib/sanitize'
import { UX } from '@/ui/testing/uxProbe'

export function ToastProvider({ children }: { children: React.ReactNode }) {
  return (
    <Toaster limit={LIMITS.toastLimit} timeout={INPUT.toastInfoMs}>
      {children}
    </Toaster>
  )
}

export type ToastLevel = 'info' | 'warning' | 'critical' | 'success'
const live = new Set<string>()
/** add or update the toast of a merge key; texts are sanitised (M15-FR-108) */
export function notify(key: string, level: ToastLevel, title: string, description?: string): void {
  const timeout = level === 'critical' ? 0 : level === 'warning' ? INPUT.toastWarnMs : INPUT.toastInfoMs
  const type = level === 'critical' ? 'error' : level === 'warning' ? 'warning' : level === 'success' ? 'success' : 'info'
  const data = { title: sanitizeText(title), description: description ? sanitizeText(description) : undefined, type, timeout }
  UX.toasts.merged[key] = (UX.toasts.merged[key] ?? 0) + 1
  if (live.has(key)) {
    toast.update(key, data)
    return
  }
  live.add(key)
  toast.add({ id: key, ...data, onRemove: () => live.delete(key) })
}
