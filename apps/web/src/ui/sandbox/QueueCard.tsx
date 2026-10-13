// Queue card (AWR-04 §4.4.6 L1, §10.2; M15-FR-207; UX-FR-108; AWR-14 §20.4.3): a non-modal Card under the camera tools
// (it never takes the focus and never makes the rest inert); waiting: "sandbox queue · position k", the estimated wait and
// "leave the queue"; slot ready: "a sandbox is reserved for you · s seconds to confirm" with a 60 s wall-clock countdown,
// "enter the sandbox" (claim, then the sandbox route) and "give up"; a voided ticket says so and offers queuing again.
// Each state change is announced once (status for the position, alert for the reserved slot). The shared show and every
// viewer interaction stay usable while queued.
// D2.1 (ADR-120, ADR-121; AWR-14 §20.26.3; M15-FR-226; UX-FR-151): three kinds. `sandbox` is the D2 card above; `resume` is
// the same ticket of a parked session ("restoring the sandbox needs a queue · position n", the saved sim time, leave the
// queue or end the sandbox and download the snapshot; the reserved slot is claimed automatically while the page is
// visible, the card says "kept for you · s seconds"); `viewer` (ViewerQueueCard) is the live-viewer seat queue of the shared
// show ("live viewing queue · position n", static browsing meanwhile, the link connects by itself on seat_ready). When
// both a viewer and a sandbox card show they stack, the viewer card on top; on phones the stack sits above the tab bar.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { fmt } from '@/lib/format'
import { Button } from '@/ui/components/ui/button'
import { Card, CardContent } from '@/ui/components/ui/card'
import { Progress } from '@/ui/components/ui/progress'
import { Icon } from '@/ui/icons/Icon'
import { etaAbout } from '@/app/i18n/capacity'
import { usePower } from '@/app/power/PowerController'
import { viewerQueue } from '@/app/power/viewerQueue'
import { acquireCapacityPolling } from '@/stores/capacity'
import { CLAIM_WINDOW_MS, sandbox, useSandbox } from './session'

/** seconds left of the claim window (pure; tests) */
export function claimLeftS(deadlineMs: number | null, now = Date.now()): number {
  return deadlineMs === null ? 0 : Math.max(0, Math.ceil((deadlineMs - now) / 1000))
}

export function QueueCard({ onRequeue, kinds = ['sandbox', 'resume'] }: { onRequeue?: () => void; kinds?: readonly ('sandbox' | 'resume')[] }) {
  const t = useT()
  const phase = useSandbox((s) => s.phase)
  const queue = useSandbox((s) => s.queue)
  const error = useSandbox((s) => s.error)
  const [now, setNow] = React.useState(() => Date.now())
  const [claiming, setClaiming] = React.useState(false)
  // 驻留时刻的仿真时间：服务端 t_sim_ns 到达前取进入节能时冻结的最后仿真时刻（与节能覆盖层同一口径；d2-WP-13b-to-M15 第 4 条）
  const tSimServer = useSandbox((s) => s.parked?.tSimNs ?? null)
  const lastSimNs = usePower((s) => s.lastSimNs)
  const tSim = tSimServer ?? (lastSimNs !== null ? String(lastSimNs) : null)
  const parkedSim = tSim && Number.isFinite(Number(tSim)) ? fmt.simTime(Number(tSim)) : '—'
  const ready = phase === 'slot_ready'
  React.useEffect(() => {
    if (!ready) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [ready])
  const left = ready ? claimLeftS(queue?.claimDeadlineMs ?? null, now) : 0
  React.useEffect(() => {
    if (ready && queue?.claimDeadlineMs && left === 0) sandbox.expireSlot()
  }, [ready, left, queue?.claimDeadlineMs])
  const voided = phase === 'none' && error?.code === 511
  const resume = queue?.kind === 'resume'
  const visible = !(!queue && !voided) && !(phase !== 'queued' && !ready && !voided) && kinds.includes(resume ? 'resume' : 'sandbox')
  // the capacity numbers refresh every 10 s while a queue card is visible (stores/capacity lease, AWR-04 §15.5.3)
  React.useEffect(() => (visible ? acquireCapacityPolling('queue-card') : undefined), [visible])
  if (!visible) return null
  const claim = async () => {
    setClaiming(true)
    const r = await sandbox.claim()
    setClaiming(false)
    if (r.kind === 'created' && !resume) navigate(`/sandbox/${r.sid}`)
  }
  const endAndSave = async () => {
    await sandbox.leaveQueue()
    await sandbox.end()
  }
  return (
    <div className="pointer-events-auto" data-queue-card={voided ? 'voided' : phase} data-queue-kind={resume ? 'resume' : 'sandbox'}>
      <Card size="sm" className="w-80 gap-2 rounded-xl bg-hud px-3 py-2">
        <CardContent className="flex flex-col gap-2 p-0">
          {voided ? (
            <>
              <span className="flex items-center gap-1.5 font-medium" role="status"><Icon icon="state.stale" />{t('sandbox.queue.voided')}</span>
              <div className="flex justify-end gap-2">
                <Button size="xs" variant="ghost" onClick={() => sandbox.clearError()}>{t('common.close')}</Button>
                {onRequeue ? <Button size="xs" onClick={onRequeue}>{t('sandbox.queue.again')}</Button> : null}
              </div>
            </>
          ) : ready ? (
            <>
              <span className="flex items-center gap-1.5 font-medium" role="alert" data-queue-ready="">
                <Icon icon="sandbox.mine" />
                {t(resume ? 'queue.resume.ready' : 'sandbox.queue.ready', { s: left })}
              </span>
              <span className="text-hud-sub text-muted-foreground">{queue ? `${queue.world}${queue.template ? ` · ${t(`sandbox.template.${queue.template}`)}` : ''}` : ''}</span>
              <Progress value={Math.round((left * 1000 * 100) / CLAIM_WINDOW_MS)} aria-label={t('sandbox.queue.ready', { s: left })} />
              <div className="flex justify-between gap-2">
                <Button size="xs" variant="ghost" onClick={() => void sandbox.leaveQueue()} data-queue-giveup="">{t('sandbox.queue.giveUp')}</Button>
                <Button size="xs" disabled={claiming} onClick={() => void claim()} data-queue-claim="">{t('sandbox.queue.enter')}</Button>
              </div>
            </>
          ) : (
            <>
              <span className="flex items-center gap-1.5 font-medium" role="status" data-queue-position={queue?.position ?? 0}>
                <Icon icon={resume ? 'power.resume' : 'state.hold'} />
                {t(resume ? 'queue.resume.title' : 'sandbox.queue.position', { n: queue?.position ?? 0 })}
              </span>
              <span className="text-hud-sub text-muted-foreground" data-queue-eta="">
                {resume ? t('queue.resume.sub', { t: parkedSim, eta: etaAbout(queue?.etaS) })
                  : queue?.etaS !== null && queue?.etaS !== undefined ? t('sandbox.queue.eta', { min: fmt.num(Math.max(1, Math.round(queue.etaS / 60)), 0) }) : t('sandbox.queue.etaUnknown')}
              </span>
              {resume ? null : <span className="text-hud-sub text-muted-foreground">{t('sandbox.queue.browse')}</span>}
              <div className="flex justify-end gap-2">
                {resume ? <Button size="xs" variant="ghost" onClick={() => void endAndSave()} data-queue-end="">{t('queue.resume.end')}</Button> : null}
                <Button size="xs" variant="outline" onClick={() => void sandbox.leaveQueue()} data-queue-leave="">{t('sandbox.queue.leave')}</Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/** the live-viewer seat queue of the shared show (static browsing meanwhile; connects by itself on seat_ready) */
export function ViewerQueueCard() {
  const t = useT()
  const state = usePower((s) => s.state)
  const role = usePower((s) => s.role)
  const ticket = usePower((s) => s.viewerTicket)
  const visible = state === 'QUEUED' && role === 'viewer'
  React.useEffect(() => (visible ? acquireCapacityPolling('viewer-queue') : undefined), [visible])
  if (!visible) return null
  return (
    <div className="pointer-events-auto" data-queue-card={ticket?.state ?? 'waiting'} data-queue-kind="viewer">
      <Card size="sm" className="w-80 gap-2 rounded-xl bg-hud px-3 py-2">
        <CardContent className="flex flex-col gap-2 p-0">
          <span className="flex items-center gap-1.5 font-medium" role="status" data-queue-position={ticket?.position ?? 0}>
            <Icon icon="state.hold" />
            {t('queue.viewer.title', { n: ticket?.position ?? 0 })}
          </span>
          <span className="text-hud-sub text-muted-foreground" data-queue-eta="">{t('queue.viewer.sub', { eta: etaAbout(ticket?.etaS) })}</span>
          <div className="flex justify-end">
            <Button size="xs" variant="outline" onClick={() => void viewerQueue.leave()} data-queue-leave="viewer">{t('sandbox.queue.leave')}</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

/** the queue stack under the camera tools (viewer card first; phones: above the tab bar) */
export function QueueStack({ onRequeue }: { onRequeue?: () => void }) {
  return (
    <div data-anchor="queue" className="pointer-events-none flex flex-col items-end gap-2" data-queue-stack="">
      <ViewerQueueCard />
      <QueueCard onRequeue={onRequeue} kinds={['sandbox']} />
    </div>
  )
}
