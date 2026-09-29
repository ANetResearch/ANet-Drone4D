// /bench, GPU self-test page (M15-FR-022, D1-ext; content and facade by M06 §8.1). Placeholder until M06 delivers.
import type { RouteDef } from '@/app/router/router'
import { BenchPage } from '@/ui/views/BenchPage'

export const route: RouteDef = { id: 'bench', pattern: '/bench', layer: 'page', d1: 'ext', component: BenchPage }
