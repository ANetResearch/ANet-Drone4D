// /sandbox/api, the open interface page (AWR-04 §11.4; M15-FR-221; UX-FR-145; AWR-14 §20.14): renders the sandbox
// OpenAPI 3.1 document with shadcn parts (no Swagger UI). A page-layer route in its own lazy chunk, so it loads neither
// three nor the engine; registered before /sandbox/:sid by its two static segments (app/router/match.ts ranking).
import { lazy } from 'react'
import type { RouteDef } from '@/app/router/router'

const ApiDocs = lazy(() => import('@/ui/views/ApiDocs').then((m) => ({ default: m.ApiDocsPage })))

export const route: RouteDef = { id: 'sandboxApi', pattern: '/sandbox/api', layer: 'page', d1: 'core', component: ApiDocs }
