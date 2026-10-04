// 'three' as seen by @react-three/fiber (FX-UBO, ADR-086): three unchanged except Clock, which is the warning-free
// R3fClock (r3fClock.ts). Only R3F's own modules import this file: the awr-r3f-clock plugin of vite.config.ts resolves
// their `import * as THREE from 'three'` here; every other module, and this file's own import, gets three itself.
export * from 'three'
export { R3fClock as Clock } from './r3fClock'
