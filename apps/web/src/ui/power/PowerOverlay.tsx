// Power-save UI (R-D2-33, R-D2-34; ADR-121; AWR-04 §15.7.3; AWR-14 §20.26.4; M15-FR-228; UX-FR-152). Owner: M15.
//   * PowerBanner: the countdown bar at the top centre of the unobscured rect ("25 s to power save · any input cancels"),
//     a neutral Alert announced once (role status); any interaction (window capture listeners of the controller) hides it;
//   * PowerOverlay: the dim layer over the viewport (the --overlay token, pointer-events none, nothing is made inert, so
//     the first input itself still works: a drag starts rotating the camera, a tap opens a panel) and the centred card
//     "power save · live updates paused" with the role text, the last update and the version; while resuming the same card
//     shows the segmented resume track; a sandbox resume that met a full pool shows the resume queue card instead;
//   * PausedNote: "paused · last update hh:mm:ss" for the HUD, the session bar and the timeline while frozen.
// Reduced motion: no fades (the card simply appears). The card never takes the focus.
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { buildShort } from '@/lib/build'
import { RESUME_WEIGHTS, usePower } from '@/app/power/PowerController'
import { Alert, AlertDescription } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Card, CardContent } from '@/ui/components/ui/card'
import { Icon } from '@/ui/icons/Icon'
import { SegmentBar } from '@/ui/boot/SegmentBar'
import { QueueCard } from '@/ui/sandbox/QueueCard'
import { useSandbox } from '@/ui/sandbox/session'

export function PowerBanner() {
  const t = useT()
  const state = usePower((s) => s.state)
  const left = usePower((s) => s.warnLeftS)
  if (state !== 'WARNING') return null
  return (
    <div data-anchor="power-banner" className="pointer-events-none">
      <Alert role="status" className="w-[min(30rem,calc(100vw-2rem))] bg-hud py-1.5" data-power-banner={left}>
        <Icon icon="power.saving" />
        <AlertDescription className="text-foreground">{t('power.warn', { n: left })}</AlertDescription>
      </Alert>
    </div>
  )
}

/** sim time of a parked session from its t_sim_ns (string of ns) */
function simTimeOf(ns: string | null | undefined): string {
  if (!ns) return '—'
  const v = Number(ns)
  return Number.isFinite(v) ? fmt.simTime(v) : '—'
}

export function PowerOverlay() {
  const t = useT()
  const state = usePower((s) => s.state)
  const role = usePower((s) => s.role)
  const lastMs = usePower((s) => s.lastUpdateMs)
  const resume = usePower((s) => s.resume)
  const parked = useSandbox((s) => s.parked)
  const lastSimNs = usePower((s) => s.lastSimNs)
  const queueKind = useSandbox((s) => s.queue?.kind ?? null)
  const sbPhase = useSandbox((s) => s.phase)
  const saving = state === 'SAVING'
  const resuming = state === 'RESUMING' && resume !== null
  const resumeQueued = role === 'sandbox' && queueKind === 'resume' && (sbPhase === 'queued' || sbPhase === 'slot_ready')
  if (!saving && !resuming && !resumeQueued) return null
  const sandbox = role === 'sandbox'
  const segs = resume ? Object.entries(RESUME_WEIGHTS[resume.kind]).map(([id, weight]) => ({ id, weight })) : []
  return (
    <>
      <div className="pointer-events-none fixed inset-0 bg-overlay" data-power-mask={state} aria-hidden="true" />
      <div data-anchor="center" className="pointer-events-none" data-power-overlay={state}>
        {resumeQueued ? (
          <div className="pointer-events-auto"><QueueCard kinds={['resume']} /></div>
        ) : (
          <Card size="sm" className="w-[min(24rem,calc(100vw-2rem))] gap-2 rounded-xl bg-hud px-4 py-3" role="status">
            <CardContent className="flex flex-col gap-1.5 p-0">
              <span className="flex items-center gap-1.5 font-medium">
                <Icon icon={resuming ? 'power.resume' : 'power.saving'} />
                {t(resuming ? 'power.resuming' : 'power.title')}
              </span>
              {resuming ? (
                <SegmentBar segments={segs} values={resume.seg} label={t('power.resuming')} />
              ) : (
                <span className="text-hud-sub text-muted-foreground" data-power-role={role}>
                  {sandbox ? t('power.sandbox', { t: simTimeOf(parked?.tSimNs ?? (lastSimNs !== null ? String(lastSimNs) : null)) }) : t('power.viewer')}
                </span>
              )}
              <span className="text-hud-sub text-muted-foreground tabular-nums" data-power-last="">{t('power.lastUpdate', { time: fmt.wallTime(lastMs) })}</span>
              <span className="font-mono text-hud-cap text-muted-foreground" data-power-build="">{buildShort()}</span>
            </CardContent>
          </Card>
        )}
      </div>
    </>
  )
}

/** "paused · last update hh:mm:ss" while the texts are frozen (session bar, timeline, HUD) */
export function PausedNote({ className }: { className?: string }) {
  const t = useT()
  const frozen = usePower((s) => s.frozen)
  const lastMs = usePower((s) => s.lastUpdateMs)
  if (!frozen) return null
  return (
    <Badge variant="outline" className={className} data-power-paused="">
      <Icon icon="power.saving" />
      {t('power.paused', { time: fmt.wallTime(lastMs) })}
    </Badge>
  )
}
