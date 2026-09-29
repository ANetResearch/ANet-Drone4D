// Re-export of the generated motion tokens (ADR-029 second round: the generator writes lib/tokens/*.gen.ts so engine/**
// can import them; this module keeps the ui/motion/tokens.ts path of AWR-15 §8.1).
export { MOTION, EASE, EASE_CSS, SPRINGS } from '@/lib/tokens/motion.gen'
export { INPUT, MOTION_BUDGET } from '@/lib/tokens/input.gen'
