// Label icon keys (M06-FR-067; M15-FR-068). Owner: M06. The LabelLayer references <use href="#awr-icon-<key>"> symbols
// rendered by M15's <IconSprite>; every key must exist in ui/icons/registry.ts (check-icons, dev builds throw otherwise).
// The first four are the ones the LabelLayer uses (M06 §6.13); the rest are kept for the drone status sprite.
export const LABEL_ICON_KEYS: readonly string[] = [
  'alert.critical', 'alert.warning', 'state.hold', 'cmd.goto', 'drone.quad', 'state.stale', 'link.lost', 'bat.warn', 'lease.held',
]
