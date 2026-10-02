// Viewport overlay layer (M15-FR-015, FR-027, FR-028; AWR-14 §4.3, §6.4, §7.7, §7.9): camera toolbar (ToggleGroup with the
// sliding indicator, 5 modes with hotkeys 1-5; Third and FPV need a focused vehicle), follow lock Toggle, view tools
// (focus F, north up N, home Home), the tool hint bar and its anchored Popovers (GoTo, Add P600), the status badges
// ("focus low latency" in Third and FPV; "offline, last update 12 s ago" while the link is down), the performance HUD
// and the ground readout of the last pick. Everything is anchored to the unobscured rect through --uo-* variables and
// moves by transform only; the layer itself takes no pointer events (children do).
import * as React from 'react'
import { t as tr, useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { camera, pick, viewport, type CameraMode } from '@/viewport/facade'
import { Badge } from '@/ui/components/ui/badge'
import { ButtonGroup } from '@/ui/components/ui/button-group'
import { Card } from '@/ui/components/ui/card'
import { Toggle } from '@/ui/components/ui/toggle'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Kbd } from '@/ui/components/ui/kbd'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import type { IconKey } from '@/ui/icons/registry'
import { PerfHud } from '@/ui/hud/PerfHud'
import { runAction } from '@/ui/actions/registry'
import { BoundText } from '@/ui/motion/BoundText'
import { ToolHint, ToolPopovers } from '@/ui/tools/ToolLayer'
import { connViewStore, useConnView } from '@/ui/shell/connView'
import { useSelection } from '@/stores/selection'
import { useWorld } from '@/stores/world'
import { navigate } from '@/app/router/router'
import { Button } from '@/ui/components/ui/button'
import { Progress } from '@/ui/components/ui/progress'
import { boot } from '@/app/boot/BootController'
import { IconButton } from './IconButton'

const MODES: readonly { id: CameraMode; icon: IconKey; key: string }[] = [
  { id: 'orbit', icon: 'cam.orbit', key: '1' },
  { id: 'free', icon: 'cam.free', key: '2' },
  { id: 'third', icon: 'cam.third', key: '3' },
  { id: 'fpv', icon: 'cam.fpv', key: '4' },
  { id: 'bird', icon: 'cam.bird', key: '5' },
]

export function useCameraMode(): { mode: CameraMode; followLock: boolean } {
  const [s, setS] = React.useState({ mode: camera.mode, followLock: camera.followLock })
  React.useEffect(() => camera.onMode(setS), [])
  return s
}

const fmtOffline = (s: number) => tr('badge.offline', { s: fmt.num(s, 0) })
/** `?source=fake` replaces the backend by FakeSource: every value on screen is synthetic (M16-FR-006, honesty) */
const FAKE_SOURCE = typeof location !== 'undefined' && new URLSearchParams(location.search).get('source') === 'fake'
const offlineAge = () => {
  const since = connViewStore.getState().downSinceMs
  return Number.isFinite(since) ? (Date.now() - since) / 1000 : Number.NaN
}

function GroundReadout() {
  const [, bump] = React.useReducer((x: number) => x + 1, 0)
  React.useEffect(() => viewport.onChange(bump), [])
  const g = pick.ground
  // nothing under the pointer yet: no empty "—" card in the corner (AWR-14 §4.3: shown while hovering the viewport)
  if (!g || !Number.isFinite(g.surface[0])) return null
  return (
    <div data-anchor="bottom-right">
      <Card size="sm" className="gap-0 rounded-xl bg-hud px-3 py-1.5 font-mono text-hud-sub" data-ground-readout="">
        <span data-numeric="">{g ? fmt.enu(g.surface[0], g.surface[1], g.surface[2]) : fmt.enu(Number.NaN, Number.NaN, Number.NaN)}</span>
      </Card>
    </div>
  )
}

/** compact world loading card (M15-FR-005; AWR-14 §7.3 "switching worlds"): after the reveal, while a world opens or failed */
function WorldLoadingCard() {
  const t = useT()
  const phase = useWorld((s) => s.phase)
  const progress = useWorld((s) => s.progress)
  const error = useWorld((s) => s.error)
  const worldId = useWorld((s) => s.worldId)
  const [revealed, setRevealed] = React.useState(boot.state === 'REVEALED')
  React.useEffect(() => boot.subscribe((s) => setRevealed(s === 'REVEALED')), [])
  if (!revealed || (phase !== 'manifest' && phase !== 'first_screen' && phase !== 'error')) return null
  return (
    <div data-anchor="center" data-world-loading={phase}>
      <Card size="sm" className="w-72 gap-2 rounded-xl bg-hud px-3 py-2">
        <span className="text-hud-title font-semibold">{worldId ?? ''}</span>
        {phase === 'error' ? (
          <>
            <span className="text-hud-sub text-muted-foreground">{error?.message ? `${error.code} ${error.message}` : t('world.phase.error')}</span>
            <div className="flex justify-end gap-2">
              <Button size="xs" variant="outline" onClick={() => navigate('/worlds')}>{t('world.backToList')}</Button>
              <Button size="xs" onClick={() => location.reload()}>{t('common.retry')}</Button>
            </div>
          </>
        ) : (
          <>
            <Progress value={Math.round(progress * 100)} aria-label={t('world.phase.first_screen')} />
            <span className="text-hud-sub text-muted-foreground">{t(`world.phase.${phase}`)}</span>
          </>
        )}
      </Card>
    </div>
  )
}

/**
 * the overlay follows the focused vehicle in a deferred render: a selection key press (select all on 1000 rows) answers with
 * the row highlights, and the toolbar (six tooltip triggers, the follow toggle), badges and HUD re-render in React's
 * time-sliced pass instead of inside the key handler (ADR-069; D1-AC-27)
 */
export function ViewportOverlay({ compactBp }: { compactBp: boolean }) {
  const primary = React.useDeferredValue(useSelection((s) => s.primary))
  return <ViewportOverlayBody compactBp={compactBp} primary={primary} />
}

const ViewportOverlayBody = React.memo(function ViewportOverlayBody({ compactBp, primary }: { compactBp: boolean; primary: string | null }) {
  const t = useT()
  const { mode, followLock } = useCameraMode()
  const down = useConnView((s) => Number.isFinite(s.downSinceMs))
  const degraded = useConnView((s) => s.conn === 'DEGRADED')
  return (
    <div className="app-layer-viewport-ui" data-slot="viewport-overlay">
      <div data-anchor="top-center" className="flex items-center gap-1 rounded-lg bg-hud p-1 ring-1 ring-foreground/10">
        <ToggleGroup spacing={0} size="sm" value={[mode]} aria-label={t('camera.modes')}
          onValueChange={(v: unknown[]) => typeof v[0] === 'string' && runAction(`camera.mode.${v[0]}`)}>
          {MODES.map((m) => {
            const needFocus = (m.id === 'third' || m.id === 'fpv') && !primary
            const label = needFocus ? `${t(`camera.${m.id}`)} · ${t('hint.noFocus')}` : t(`camera.${m.id}`)
            return (
              <Tooltip key={m.id}>
                <TooltipTrigger render={<ToggleGroupItem value={m.id} aria-label={label} disabled={needFocus} />}>
                  <Icon icon={m.icon} />
                </TooltipTrigger>
                <TooltipContent>{label}<Kbd>{m.key}</Kbd></TooltipContent>
              </Tooltip>
            )
          })}
        </ToggleGroup>
        <Tooltip>
          <TooltipTrigger render={<Toggle size="sm" pressed={followLock} aria-label={t('camera.follow')} disabled={!primary || (mode !== 'orbit' && mode !== 'bird')}
            onPressedChange={(on: boolean) => camera.setFollowLock(on)} />}>
            <StateIcon icon="cam.follow" alt={followLock} />
          </TooltipTrigger>
          <TooltipContent>{t('camera.follow')}<Kbd>L</Kbd></TooltipContent>
        </Tooltip>
      </div>
      <div data-anchor="top-right">
        <ButtonGroup orientation="vertical" className="rounded-lg bg-hud ring-1 ring-foreground/10">
          <IconButton icon="cmd.track" label={t('camera.focus')} hotkey="KeyF" onClick={() => runAction('camera.focus')} />
          <IconButton icon="heading" label={t('camera.north')} hotkey="KeyN" onClick={() => camera.northUp()} />
          <IconButton icon="cam.reset" label={t('camera.home')} hotkey="Home" onClick={() => camera.home()} />
        </ButtonGroup>
      </div>
      <div data-anchor="top-left" className="flex flex-col items-start gap-1">
        {FAKE_SOURCE ? (
          <Badge variant="outline" className="gap-1 bg-hud" data-badge="synthetic-source">
            <Icon icon="alert.info" />
            {t('badge.syntheticSource')}
          </Badge>
        ) : null}
        {degraded && !down ? (
          <Badge variant="outline" className="gap-1 border-dashed bg-hud" data-badge="signal-delay" data-testid="signal-delay-badge">
            <Icon icon="state.hold" />
            {t('badge.signalDelay')}
          </Badge>
        ) : null}
        {mode === 'third' || mode === 'fpv' ? <Badge variant="outline" data-badge="focus-low-latency">{t('badge.focusLowLatency', { id: primary ?? '' })}</Badge> : null}
        {down ? (
          <Badge variant="outline" className="gap-1 border-dashed" data-badge="offline">
            <Icon icon="state.stale" />
            <BoundText read={offlineAge} format={fmtOffline} />
          </Badge>
        ) : null}
      </div>
      <WorldLoadingCard />
      <ToolHint />
      <ToolPopovers />
      <PerfHud compactBp={compactBp} />
      <GroundReadout />
    </div>
  )
})
