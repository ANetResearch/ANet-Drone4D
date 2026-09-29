// /dev/design, the design system sample page (dev and test builds only). TEST_SWITCHES is folded at build time and the
// page is imported lazily inside that branch, so a plain production build contains neither the route component nor the
// sample page code (M15-AC-002).
import { lazy } from 'react'
import type { RouteDef } from '@/app/router/router'
import { TEST_SWITCHES } from '@/lib/testSwitches'

const DesignSample = TEST_SWITCHES ? lazy(() => import('@/app/dev/DesignSample').then((m) => ({ default: m.DesignSample }))) : undefined

export const route: RouteDef = { id: 'design', pattern: '/dev/design', layer: 'page', d1: 'core', component: DesignSample, testOnly: true }
