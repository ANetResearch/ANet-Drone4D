// Brand assets are hosted locally in public/brand (M15-FR-109; ADR-032; BRAND-02, BRAND-03): only ui/brand/** and the
// AWR-15 §4.2 placements reference them; never a GitHub URL at runtime. Bytes are locked in public/brand/brand.lock.json.
export const BRAND = {
  badge: '/brand/anet-logo.svg',
  avatar96: '/brand/avatar-96.png',
  avatar460: '/brand/avatar-460.png',
} as const
/** badge viewBox 2846 x 493 (AWR-15 §4.1) */
export const BADGE_ASPECT = 2846 / 493
