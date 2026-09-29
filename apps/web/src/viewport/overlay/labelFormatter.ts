// Label text formatter hook (M06-FR-067; M15 injects the localised FlightState short texts). Owner: M06.
// The viewport never imports ui/**: M15 calls labels.setFormatter(fn) through the facade. The default returns the
// contract enum names (no CJK literals in viewport code); M15 replaces it with the i18n texts ("signal delayed", zone
// kinds, FlightState short names).
import { FLIGHT_STATE_NAMES } from '@awr/contracts/enums'

export type LabelSub = 'state' | 'hold' | 'zone.nofly' | 'zone.restricted'
export type LabelFormatter = (flightState: number, sub: LabelSub, lang: string) => string

const defaultFormatter: LabelFormatter = (fs, sub) => {
  if (sub === 'hold') return 'HOLD'
  if (sub === 'zone.nofly') return 'NOFLY'
  if (sub === 'zone.restricted') return 'RESTRICTED'
  return FLIGHT_STATE_NAMES[fs] ?? ''
}

let current: LabelFormatter = defaultFormatter
export function labelFormatter(): LabelFormatter {
  return current
}
export function setLabelFormatter(fn: LabelFormatter | null): void {
  current = fn ?? defaultFormatter
}
