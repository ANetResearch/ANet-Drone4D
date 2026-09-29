// Brand components (AWR-15 §4.2; ADR-032). The brand assets (public/brand/*) are referenced only inside ui/brand/**,
// the AppHeader, the boot mask (index.html), About and the empty states; other modules import these components from
// '@/ui/brand' and never name an asset path.
export { BrandLockup } from './BrandLockup'
export { BrandBadge } from './BrandBadge'
export { PanelEmpty } from './PanelEmpty'
export { FullEmptyState } from './FullEmptyState'
