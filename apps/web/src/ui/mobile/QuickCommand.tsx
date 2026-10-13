// Quick commands of the phone and the tablet commander (R-D2-36, R-D2-37; AWR-04 §15.8.3 item 2; AWR-14 §20.26.5;
// M15-FR-232; UX-FR-154). Owner: M15. A bottom sheet with three steps (ui/mobile/quickModel.ts): choose the vehicles (the
// current selection skips the step; vehicles of one nest may be chosen together), the command (goto: tap a ground point in
// the viewport; hover, fixed wing loiter on the server; return to launch; watch: choose a target, a point_watch task at its
// position, stations and keep-outs by M20) and the confirmation that shows what will be sent. Each command takes at most
// four taps; the result comes back as a Toast (accepted, or the reason code text). Only the sandbox operator and the
// internal identity with the seat see the entry (ui/sandbox/writeGate); viewers switch windows and read only.
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { rtClient, type CallHandle } from '@/net/rt'
import { useFleet } from '@/stores/fleet'
import { selection, selectionStore, useSelection } from '@/stores/selection'
import { taskingActions } from '@/stores/tasking'
import { targetsModel } from '@/app/shell/engineD2'
import { useTargetList } from '@/ui/panels/targets/TargetsPanel'
import { runBatch, runVehicleCmd } from '@/ui/actions/vehicleCommands'
import { BottomSheet } from '@/ui/components/ui/bottom-sheet'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { d2Tools } from '@/ui/tools/d2Tools'
import { UX } from '@/ui/testing/uxProbe'
import { sanitizeText } from '@/lib/sanitize'
import { fmt } from '@/lib/format'
import { QUICK_CLOSED, QUICK_OPS, gotoAltitude, quickStep, type QuickEvent, type QuickModel, type QuickOp } from './quickModel'

export const OP_ICON: Readonly<Record<QuickOp, IconKey>> = { goto: 'cmd.goto', hover: 'cmd.hover', rtl: 'cmd.rth', watch: 'cmd.watch' }

// ------------------------------------------------------------ the shared controller (FAB, task strip and the sheet)
let model: QuickModel = QUICK_CLOSED
const listeners = new Set<() => void>()
function dispatch(e: QuickEvent): void {
  const next = quickStep(model, e)
  if (next === model) return
  model = next
  UX.quick.state = next.state
  for (const cb of listeners) cb()
}
export const quick = {
  open(op?: QuickOp): void {
    dispatch({ type: 'open', selected: selectionStore.getState().ids })
    if (op) quick.op(op)
  },
  op(op: QuickOp): void {
    dispatch({ type: 'op', op })
    if (model.state === 'PICK_POINT') {
      // the next tap on the ground of the viewport is the goto point (the sheet steps aside while picking)
      d2Tools.start('TASK_POINT', null, (pts) => {
        const p = pts[0]
        if (p) dispatch({ type: 'point', p })
      })
    }
  },
  dispatch,
  get model(): QuickModel {
    return model
  },
}
function useQuick(): QuickModel {
  return React.useSyncExternalStore((cb) => {
    listeners.add(cb)
    return () => {
      listeners.delete(cb)
    }
  }, () => model, () => model)
}

/** the vehicle's current position (ENU) from the swarm columns, null when unknown */
function vehiclePos(id: string): [number, number, number] | null {
  const rt = rtClient()
  if (!rt) return null
  const no = rt.roster.agentNoOf(id)
  const sw = rt.swarm
  for (let i = 0; i < sw.n; i++) if (sw.agentNo[i] === no) return [sw.pos[3 * i], sw.pos[3 * i + 1], sw.pos[3 * i + 2]]
  return null
}
const vehicleZ = (id: string): number | null => vehiclePos(id)?.[2] ?? null

/**
 * a target's position: the store row (TargetLite32), else the live row of the target model, else the configured position
 * (a ground target's null z is the terrain, 0 here: the watch station is computed by M20 from the vehicle's position); null
 * when nothing is known yet (d2-WP-13b-to-M15 item 1: the list is the targets panel's, which falls back to the roster and
 * the seeded configurations while the rows have not arrived)
 */
export function targetPos(id: string, row?: { pos?: readonly number[] } | null): [number, number, number] | null {
  const p = row?.pos
  if (p && p.length >= 3 && p.every((v) => Number.isFinite(v))) return [p[0] as number, p[1] as number, p[2] as number]
  const i = targetsModel.rowOf(id)
  const R = targetsModel.rows
  if (i >= 0) return [R.pos[3 * i], R.pos[3 * i + 1], R.pos[3 * i + 2]]
  const c = targetsModel.cfgs.get(id)?.pos
  if (c && typeof c[0] === 'number' && typeof c[1] === 'number') return [c[0], c[1], typeof c[2] === 'number' ? c[2] : 0]
  return null
}

function outcomeToast(key: string, ok: boolean, code: number | null, t: (k: string, p?: Record<string, string | number>) => string): void {
  notify(`quick:${key}`, ok ? 'success' : 'warning', ok ? t('cmd.quick.accepted') : t('cmd.quick.refused'), !ok && code ? `${code} ${reasonText(code).short}` : undefined)
}

/** acknowledgement toast on the first result (accepted or running: "指令已受理"), a refusal or a later failure warns
 *  (D2-INT, d2-WP-13b-to-M15 item 2: the return-to-home command used to wait for the landing) */
function ackToast(h: CallHandle, key: string, t: (k: string, p?: Record<string, string | number>) => string): void {
  let acked = false
  h.onResult((r) => {
    const ok = r.status === 'accepted' || r.status === 'running' || r.status === 'succeeded'
    if (!acked) {
      acked = true
      outcomeToast(key, ok, ok ? null : (r.code ?? null), t)
      return
    }
    if (r.status === 'failed' || r.status === 'rejected' || r.status === 'timeout') outcomeToast(key, false, r.code ?? null, t)
  })
  h.result.then(() => {}, () => {
    if (!acked) outcomeToast(key, false, null, t)
  })
}

/** send the confirmed command; returns what was sent (tests read UX.quick.sent) */
async function send(m: QuickModel, t: (k: string, p?: Record<string, string | number>) => string): Promise<void> {
  const op = m.op
  if (!op || m.vids.length === 0) return
  UX.quick.sent.push(`${op}:${m.vids.join(',')}`)
  if (op === 'watch' && m.target) {
    // watch from where the selected vehicle is, looking at the target: the target's own position lies inside its keepout
    // (583 KEEPOUT_VIOLATION; D2-INT)
    const at = vehiclePos(m.vids[0] as string)
    const o = await taskingActions.watchPoint(at ? [at[0], at[1], 0] : m.target.pos, { label: t('cmd.quick.watchLabel', { target: m.target.label }),
      look: { mode: 'target', target: m.target.id } })
    outcomeToast('watch', o.ok, o.ok ? null : (o.code ?? null), t)
    return
  }
  const rt = rtClient()
  if (op === 'goto' && m.point && rt) {
    for (const id of m.vids) {
      const z = gotoAltitude(m.point[2], vehicleZ(id))
      const h = rt.call(`uav/${id}/cmd/goto`, { pos: [m.point[0], m.point[1], z], route: 'auto' })
      ackToast(h, 'goto', t)
    }
    return
  }
  const cmd = op === 'rtl' ? 'rtl' : 'hover'
  const h = m.vids.length > 1 ? runBatch(cmd, m.vids, false) : runVehicleCmd(cmd, m.vids[0] as string)
  if (!h) {
    outcomeToast(cmd, false, null, t)
    return
  }
  ackToast(h, cmd, t)
}

export function QuickCommandSheet() {
  const t = useT()
  const m = useQuick()
  const rosterVersion = useFleet((s) => s.rosterVersion)
  const selected = useSelection((s) => s.ids)
  const targets = useTargetList()
  // the roster is a mutable view of the page client; rosterVersion is the cache key of the id list
  // oxlint-disable-next-line react-hooks/exhaustive-deps -- see above
  const vehicles = React.useMemo(() => (rtClient()?.roster.entries() ?? []).map((e) => e.id), [rosterVersion])
  const picking = m.state === 'PICK_POINT'
  const open = m.state !== 'CLOSED' && !picking
  const close = (): void => {
    if (picking) d2Tools.end()
    dispatch({ type: 'close' })
  }
  const title = m.state === 'SELECT' ? t('cmd.quick.select') : m.state === 'ACTION' ? t('cmd.quick.title') : m.state === 'PICK_TARGET' ? t('cmd.quick.pickTarget')
    : m.state === 'CONFIRM' ? t('cmd.quick.confirmTitle') : t('cmd.quick.title')
  return (
    <>
      {picking ? (
        <div data-anchor="top-center" className="pointer-events-auto mt-12 flex items-center gap-2 rounded-lg bg-hud px-3 py-1.5 ring-1 ring-foreground/10" data-quick-picking="">
          <Icon icon="cmd.goto" />
          <span className="text-hud-sub">{t('cmd.quick.pickPoint')}</span>
          <Button size="sm" variant="ghost" onClick={close}>{t('common.cancel')}</Button>
        </div>
      ) : null}
      <BottomSheet open={open} onOpenChange={(o) => (o ? null : close())} title={title} handleLabel={t('cmd.quick.title')} snaps={[0.5, 0.9]}
        description={m.vids.length ? t('cmd.quick.vehicles', { ids: m.vids.join(', ') }) : undefined}>
        <div className="flex flex-col gap-3" data-quick-sheet={m.state}>
          {m.state === 'SELECT' ? <SelectStep vehicles={vehicles} initial={selected} /> : null}
          {m.state === 'ACTION' ? (
            <div className="grid grid-cols-2 gap-2" data-quick-ops="">
              {QUICK_OPS.map((op) => (
                <Button key={op} variant="outline" className="h-14 flex-col gap-1" onClick={() => quick.op(op)} data-quick-op={op}>
                  <Icon icon={OP_ICON[op]} />
                  {t(`cmd.quick.${op}`)}
                </Button>
              ))}
            </div>
          ) : null}
          {m.state === 'PICK_TARGET' ? (
            <div className="flex flex-col gap-2" data-quick-targets="">
              {targets.length === 0 ? <p className="text-hud-sub text-muted-foreground">{t('targets.none')}</p> : null}
              {targets.map((tg) => {
                const pos = targetPos(tg.id, tg as { pos?: readonly number[] })
                // without a known position the watch station still follows the selected vehicle (send: vehiclePos first)
                const at = pos ?? vehiclePos(m.vids[0] ?? '') ?? null
                return (
                  <Button key={tg.id} variant="outline" className="h-11 justify-between" data-quick-target={tg.id} disabled={!at}
                    onClick={() => at && dispatch({ type: 'target', id: tg.id, label: tg.label || tg.id, pos: at })}>
                    <span className="truncate">{sanitizeText(tg.label || tg.id, 32)}</span>
                    <span className="font-mono text-muted-foreground">{pos ? fmt.enu(pos[0], pos[1], pos[2]) : '—'}</span>
                  </Button>
                )
              })}
            </div>
          ) : null}
          {m.state === 'CONFIRM' ? (
            <div className="flex flex-col gap-2" data-quick-confirm={m.op ?? ''}>
              <p className="text-hud-sub">
                {m.op === 'goto' && m.point ? t('cmd.quick.sendGoto', { ids: m.vids.join(', '), at: fmt.enu(m.point[0], m.point[1], m.point[2]) })
                  : m.op === 'watch' && m.target ? t('cmd.quick.sendWatch', { target: m.target.label })
                    : t('cmd.quick.sendCmd', { op: t(`cmd.quick.${m.op ?? 'hover'}`), ids: m.vids.join(', ') })}
              </p>
              <div className="grid grid-cols-2 gap-2">
                <Button variant="outline" className="h-11" onClick={() => dispatch({ type: 'back' })}>{t('cmd.quick.back')}</Button>
                <Button className="h-11" data-quick-send="" onClick={() => {
                  const sent = model
                  dispatch({ type: 'confirm' })
                  void send(sent, t)
                }}>{t('cmd.quick.confirm')}</Button>
              </div>
            </div>
          ) : null}
        </div>
      </BottomSheet>
    </>
  )
}

/** step 1: choose vehicles (several of one nest may go together); starts from the current selection */
function SelectStep({ vehicles, initial }: { vehicles: readonly string[]; initial: readonly string[] }) {
  const t = useT()
  const [chosen, setChosen] = React.useState<string[]>(() => [...initial])
  return (
    <>
      <div className="grid grid-cols-2 gap-2" data-quick-vehicles="">
        {vehicles.map((id) => {
          const on = chosen.includes(id)
          return (
            <Button key={id} variant={on ? 'secondary' : 'outline'} className="h-11 justify-start font-mono" aria-pressed={on} data-quick-vehicle={id}
              onClick={() => setChosen((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]))}>
              <Icon icon="drone.quad" data-icon="inline-start" />
              {sanitizeText(id, 24)}
            </Button>
          )
        })}
      </div>
      {vehicles.length === 0 ? <p className="text-hud-sub text-muted-foreground">{t('quant.empty.noVehicles')}</p> : null}
      <Button className="h-11" disabled={chosen.length === 0} onClick={() => {
        selection.select(chosen)
        dispatch({ type: 'vehicles', vids: chosen })
      }} data-quick-next="">{t('cmd.quick.next')}</Button>
    </>
  )
}
