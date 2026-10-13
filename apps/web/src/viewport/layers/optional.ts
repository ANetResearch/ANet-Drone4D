// Layer adapters owned by other modules, mounted by WorldCanvas when present (import.meta.glob keeps the build independent
// of their delivery). Owner: M06. M07: environment.tsx (EnvironmentLayer); D2 (ADR-110): targets.tsx (TargetsLayer, M18:
// targets and their sensitive volumes) and tasking.tsx (TaskingLayer, M20: stations, strips, re-shoot points).
import type { ComponentType } from 'react'
import type { RenderBackend } from '../renderer'

type Adapter = ComponentType<{ be: RenderBackend }>
const pick = (mods: Record<string, Record<string, Adapter | undefined>>, name: string): Adapter | null => Object.values(mods)[0]?.[name] ?? null

export const EnvironmentLayer = pick(import.meta.glob('./environment.tsx', { eager: true }), 'EnvironmentLayer')
export const TargetsLayer = pick(import.meta.glob('./targets.tsx', { eager: true }), 'TargetsLayer')
export const TaskingLayer = pick(import.meta.glob('./tasking.tsx', { eager: true }), 'TaskingLayer')
