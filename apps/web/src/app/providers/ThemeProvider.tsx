// Theme (M15-FR-002, FR-048; ADR-032; d04 §6 item 1): dark is fixed for the product; light only for report pages. No
// hotkey (the template's bare `d` toggle would fight WASD flight) and no disableTransitionOnChange style injection.
import * as React from 'react'

export type Theme = 'dark' | 'light'
const ThemeContext = React.createContext<{ theme: Theme; setTheme: (t: Theme) => void }>({ theme: 'dark', setTheme: () => {} })

export function ThemeProvider({ theme: initial = 'dark', children }: { theme?: Theme; children: React.ReactNode }) {
  const [theme, setTheme] = React.useState<Theme>(initial)
  React.useLayoutEffect(() => {
    const root = document.documentElement
    root.classList.toggle('dark', theme === 'dark')
    root.style.colorScheme = theme
  }, [theme])
  const value = React.useMemo(() => ({ theme, setTheme }), [theme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export const useTheme = () => React.useContext(ThemeContext)
