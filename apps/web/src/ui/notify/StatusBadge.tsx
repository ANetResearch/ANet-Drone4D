// Shape-coded status badges (M15-FR-092; AWR-14 §11.3, §13.2; ADR-032): nominal (foreground), warning (red outline,
// hollow, TriangleAlert, text), critical-primary (solid red, OctagonAlert; only the owner of the figure's one red),
// critical-secondary (red outline, OctagonAlert), stale (dashed grey, "STALE 3.2 S"). FlightStateBadge maps the 14
// flight states to those shapes (grey, white outline, white solid with the sub mode, warning, critical).
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { FlightFlags, FlightState, subName } from '@awr/contracts/enums'
import { Badge } from '@/ui/components/ui/badge'
import { Icon } from '@/ui/icons/Icon'

export type BadgeKind = 'nominal' | 'warning' | 'critical-primary' | 'critical-secondary' | 'stale' | 'muted' | 'outline' | 'solid'

// the badge base is variant "default" (no dark: background of its own), so these backgrounds win in the dark theme
const CLS: Readonly<Record<BadgeKind, string>> = {
  nominal: 'border-transparent bg-secondary text-foreground',
  warning: 'border-brand bg-transparent text-brand-text',
  'critical-primary': 'border-transparent bg-brand-solid text-brand-foreground',
  'critical-secondary': 'border-brand bg-transparent text-brand-text',
  stale: 'border-dashed border-muted-foreground bg-transparent text-muted-foreground',
  muted: 'border-border bg-transparent text-muted-foreground',
  outline: 'border-foreground bg-transparent text-foreground',
  solid: 'border-transparent bg-foreground text-background',
}

export function StatusBadge({ kind, text, ageS, className }: { kind: BadgeKind; text: string; ageS?: number; className?: string }) {
  return (
    <Badge variant="default" data-status={kind} className={cn('gap-1 border font-medium', CLS[kind], className)}>
      {kind === 'warning' ? <Icon icon="alert.warning" /> : null}
      {kind === 'critical-primary' || kind === 'critical-secondary' ? <Icon icon="alert.critical" /> : null}
      {kind === 'stale' ? <Icon icon="state.stale" /> : null}
      <span className="truncate">{kind === 'stale' && ageS !== undefined ? `${text} ${fmt.stale(ageS)}` : text}</span>
    </Badge>
  )
}

/** badge kind of a flight state (AWR-14 §13.2; g04 §3.1) */
export function flightStateKind(fs: number, flags: number, redOwner = false): BadgeKind {
  switch (fs) {
    case FlightState.CORRECTING:
    case FlightState.HOLD:
    case FlightState.RTL:
      return 'warning'
    case FlightState.LANDING:
      return (flags & FlightFlags.FAILSAFE) !== 0 ? 'warning' : 'outline'
    case FlightState.ELAND:
    case FlightState.FAILSAFE:
    case FlightState.CRASHED:
      return redOwner ? 'critical-primary' : 'critical-secondary'
    case FlightState.FLYING:
      return 'solid'
    case FlightState.READY:
    case FlightState.TAKING_OFF:
      return 'outline'
    case FlightState.UNKNOWN:
      return 'stale'
    default:
      return 'muted'
  }
}

export function FlightStateBadge({ fs, sub, flags, redOwner, short, className }: { fs: number; sub: number; flags: number; redOwner?: boolean; short?: boolean; className?: string }) {
  const t = useT()
  const kind = flightStateKind(fs, flags, redOwner)
  const sn = fs === FlightState.FLYING ? subName(fs, sub) : null
  const name = t(short ? `fs.short.${fs}` : `fs.${fs}`)
  const text = sn ? `${name} · ${t(`fs.sub.${sn}`)}` : name
  return <StatusBadge kind={kind === 'stale' ? 'muted' : kind} text={text} className={cn(fs === FlightState.UNKNOWN && 'border-dashed', className)} />
}
