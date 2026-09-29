// Class merging for the whole app and the shadcn sources (M15-FR-046; g07 §3.4).
// The default cn table does not know the @theme names added by this project: without registration `text-hud-sub` is
// taken for a text colour and swallows `text-muted-foreground`, and `duration-fast` / `ease-smooth-out` / `blur-small`
// do not take part in conflict resolution. Every namespace key added in an @theme block of styles/** must be listed
// here; `node tools/shadcn/check-cn-keys.mjs` (lint CN-01) compares both sides. Colour keys need no registration
// (the colour scale accepts any name).
import { createCn } from 'cn/config'

export const CN_THEME = {
  text: [
    'hud-kpi', 'hud-title', 'hud-sub', 'hud-cap',
    'ed-title', 'ed-hero', 'ed-sub', 'ed-axis', 'ed-value', 'ed-kpi',
    'rep-h1', 'rep-sect',
  ],
  ease: ['smooth-out', 'in-out', 'out', 'bounce', 'bounce-strong', 'spring-snappy'],
  blur: ['small', 'medium', 'large'],
  shadow: ['pop', 'sm', 'md', 'lg', 'xl'],
  font: ['sans', 'mono', 'heading'],
} as const

export const CN_DURATIONS = ['stagger', 'micro', 'quick', 'fast', 'medium', 'slow', 'very-slow', 'lod-fade'] as const

export const cn = createCn({
  extend: {
    theme: {
      text: [...CN_THEME.text],
      ease: [...CN_THEME.ease],
      blur: [...CN_THEME.blur],
      shadow: [...CN_THEME.shadow],
      font: [...CN_THEME.font],
    },
    classGroups: {
      duration: [{ duration: [...CN_DURATIONS] }],
    },
  },
})
