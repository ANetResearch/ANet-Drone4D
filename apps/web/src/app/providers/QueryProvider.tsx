// TanStack Query provider (M15-FR-038): the single QueryClient of app/query/client.ts.
import type * as React from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from '@/app/query/client'

export function QueryProvider({ children }: { children: React.ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}
