// Structural icon geometry type (lucide IconNode data, custom icons). Declared locally so that no module needs a
// type-only import from 'lucide' (M15-FR-059); lucide's exported IconNode values are assignable to it.
export type IconNode = ReadonlyArray<readonly [string, Readonly<Record<string, string | number | undefined>>]>
