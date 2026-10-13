// Capacity texts of the boot mask, the landing page, the start Sheet and the queue cards (R-D2-31, R-D2-32; ADR-119,
// ADR-120; AWR-04 §15.5.3, §15.6.5; AWR-14 §20.26.2 rule 3, §20.26.9; M15-FR-225; UX-FR-150). Owner: M15.
// The pure texts of app/boot/capacityText.ts bound to the i18n runtime (t of the current locale), plus the DOM writer of the
// static `.boot-capacity` line and the store-free boot request. No store import: the landing page (landing chunk, which
// must not load zustand or three) and the early boot module share these functions; the application hands the early
// module's request to M17's stores/capacity (app/boot/capacity.ts). The file lives in app/i18n because the demo build puts
// that directory into the landing chunk (vite.config.ts LANDING_MODULES).
import { t } from '@/app/i18n'
import { capacityTexts } from '@/app/boot/capacityText'

export { BOOT_CAPACITY_REFRESH_MS, BOOT_CAPACITY_TIMEOUT_MS, CAPACITY_PATH, bootCapacityFetch, queueSides, writeCapacityLine } from '@/app/boot/capacityText'
export type { BootCapacityHandoff, CapacityKind, CapacityLine } from '@/app/boot/capacityText'

const TEXTS = capacityTexts(t)

/** bare wait text: `< 1 min`, `6 min`, `2 h`; null when it cannot be estimated */
export const etaShort = TEXTS.etaShort
/** "about 6 min" or "cannot estimate" (queue cards and the viewer line) */
export const etaAbout = TEXTS.etaAbout
/** "expected about 6 min" or "expected wait cannot be estimated" (the sandbox line) */
export const etaExpected = TEXTS.etaExpected
/** the text of one capacity response (or of a failed request: `null`) */
export const capacityLine = TEXTS.capacityLine
/** the "open my sandbox" button and Sheet hint: "needs queue · position y + 1 · about t" (null: no queue) */
export const sandboxQueueHint = TEXTS.sandboxQueueHint
