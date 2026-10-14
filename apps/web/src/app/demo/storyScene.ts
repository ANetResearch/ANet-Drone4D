// Voxel airplane and flight keyframes of the landing page story (D2.2 landing narrative). Generated from the gpt-6-sol
// design (scratch landing-design.json): the plane is a list of unit voxels [x, y, z, part] with the nose toward +x, up +y
// and the wings along z; each chapter has keyframes [t, x, y, scale, yaw, pitch, roll, action] with t in [0, 1] inside
// the chapter and x, y in normalized stage coordinates [-1, 1]. Only the lazily loaded stage renderer imports this file.

export type PlanePart = 0 | 1 | 2 | 3 | 4 // body, wing, accent, canopy, engine
export type Action = 'cruise' | 'climb' | 'bank' | 'formation' | 'scan' | 'relay' | 'land'
export type Keyframe = readonly [t: number, x: number, y: number, scale: number, yaw: number, pitch: number, roll: number, action: Action]

export const PLANE: readonly (readonly [number, number, number, PlanePart])[] = [
  [-4, 0, 0, 0], [-3, 0, 0, 0], [-2, 0, 0, 0], [-1, 0, 0, 0], [0, 0, 0, 0], [1, 0, 0, 0], [2, 0, 0, 0], [3, 0, 0, 0],
  [4, 0, 0, 0], [-2, 1, 0, 0], [-1, 1, 0, 0], [0, 1, 0, 0], [5, 0, 0, 2], [-1, 0, 1, 1], [0, 0, 1, 1], [1, 0, 1, 1],
  [-1, 0, 2, 1], [0, 0, 2, 1], [1, 0, 2, 1], [-1, 0, 3, 1], [0, 0, 3, 1], [1, 0, 3, 1], [-1, 0, 4, 1], [0, 0, 4, 1],
  [-1, 0, -1, 1], [0, 0, -1, 1], [1, 0, -1, 1], [-1, 0, -2, 1], [0, 0, -2, 1], [1, 0, -2, 1], [-1, 0, -3, 1], [0, 0, -3, 1],
  [1, 0, -3, 1], [-1, 0, -4, 1], [0, 0, -4, 1], [-4, 0, 1, 1], [-3, 0, 1, 1], [-4, 0, 2, 1], [-3, 0, 2, 1], [-4, 0, -1, 1],
  [-3, 0, -1, 1], [-4, 0, -2, 1], [-3, 0, -2, 1], [-4, 1, 0, 0], [-3, 1, 0, 0], [-4, 2, 0, 0], [-1, -1, 2, 4], [0, -1, 2, 4],
  [-1, -1, -2, 4], [0, -1, -2, 4], [-1, -1, 0, 0], [0, -1, 0, 0], [1, -1, 0, 0], [1, 1, 0, 3], [2, 1, 0, 3], [0, 0, 5, 2],
  [0, 0, -5, 2],
]

export const CHAPTER_IDS = ['hero', 'digital-twin', 'environment', 'fleet-control', 'perception', 'relay-agents', 'sandbox', 'roadmap'] as const
export type ChapterId = (typeof CHAPTER_IDS)[number]

export const KEYFRAMES: Readonly<Record<ChapterId, readonly Keyframe[]>> = {
  'hero': [[0, -0.9, -0.3, 0.7, 0, 8, 0, 'climb'], [0.55, -0.25, 0.15, 1, 0, 4, 0, 'cruise'], [1, -0.55, 0.1, 1, 0, 0, 0, 'cruise']],
  'digital-twin': [[0, -0.55, 0.1, 1, 0, 0, 0, 'cruise'], [0.45, 0, 0.05, 0.9, 8, 0, 4, 'bank'], [1, 0.5, 0.1, 0.8, 10, 0, 0, 'cruise']],
  'environment': [[0, 0.5, 0.1, 0.8, 10, 0, 0, 'cruise'], [0.5, 0.1, 0.4, 0.85, 0, 12, -8, 'climb'], [1, -0.35, 0.25, 0.85, 0, 0, 0, 'cruise']],
  'fleet-control': [[0, -0.35, 0.25, 0.85, 0, 0, 0, 'cruise'], [0.4, 0.05, 0.2, 0.85, 0, 0, 0, 'formation'], [1, 0.45, -0.05, 0.85, -8, 0, -6, 'bank']],
  'perception': [[0, 0.45, -0.05, 0.85, -8, 0, -6, 'bank'], [0.4, 0.1, 0.3, 0.9, 0, -8, 0, 'scan'], [1, -0.35, 0.3, 0.9, 0, 0, 0, 'cruise']],
  'relay-agents': [[0, -0.35, 0.3, 0.9, 0, 0, 0, 'cruise'], [0.5, 0.05, 0.1, 0.9, 8, 0, 6, 'relay'], [1, 0.45, 0.15, 0.85, 0, 0, 0, 'cruise']],
  'sandbox': [[0, 0.45, 0.15, 0.85, 0, 0, 0, 'cruise'], [0.5, 0, 0.05, 0.9, 0, -6, 0, 'land'], [1, -0.3, -0.2, 0.9, 0, 0, 0, 'land']],
  'roadmap': [[0, -0.3, -0.2, 0.9, 0, 0, 0, 'land'], [0.45, 0, 0.1, 0.9, 0, 10, 0, 'climb'], [1, 0.35, 0.25, 0.8, 0, 0, 0, 'cruise']],
}
