// i18n provider (M15-FR-102): applies the persisted locale (D1: zh-CN only) and keeps <html lang> in sync.
import * as React from 'react'
import { setLocale } from '@/app/i18n'
import { usePrefs } from '@/stores/prefs'

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const locale = usePrefs((s) => s.ui.locale)
  React.useLayoutEffect(() => setLocale(locale), [locale])
  return <>{children}</>
}
