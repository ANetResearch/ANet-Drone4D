// Label icon sprite (M15-FR-068; M06-FR-067): a hidden <svg> with one <symbol id="awr-icon-<key>"> per key of the engine
// label icon table, referenced by M06 LabelLayer through <use href="#awr-icon-<key>">. Unknown keys throw in dev builds.
import { LABEL_ICON_KEYS } from '@/engine'
import { iconD, resolveIcon } from './Icon'
import type { IconKey } from './registry'

export function IconSprite({ keys = LABEL_ICON_KEYS }: { keys?: readonly string[] }) {
  return (
    <svg aria-hidden="true" width="0" height="0" style={{ position: 'absolute', width: 0, height: 0, overflow: 'hidden' }} data-icon-sprite="">
      {keys.map((k) => (
        <symbol key={k} id={`awr-icon-${k.replace(/\./g, '-')}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
          <path d={iconD(resolveIcon(k as IconKey))} />
        </symbol>
      ))}
    </svg>
  )
}
