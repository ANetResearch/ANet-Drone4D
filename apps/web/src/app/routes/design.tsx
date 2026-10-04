// /dev/design, the design system sample page (dev and test builds only). The condition is written inline (not the
// TEST_SWITCHES import): only an inline import.meta.env condition is folded before chunking, so a plain production or
// public demo build emits no DesignSample chunk at all (M15-AC-002; ADR-079 item 6, applied here by DEMO-PUBLIC).
import { lazy } from 'react'
import type { RouteDef } from '@/app/router/router'

const DesignSample = import.meta.env.DEV || import.meta.env.VITE_AWR_TEST_SWITCHES === '1'
  ? lazy(() => import('@/app/dev/DesignSample').then((m) => ({ default: m.DesignSample }))) : undefined

export const route: RouteDef = { id: 'design', pattern: '/dev/design', layer: 'page', d1: 'core', component: DesignSample, testOnly: true }
