// D1-core actions and hotkeys (M15-FR-098, FR-099; AWR-14 §6.10 full table; AWR-03 §8.5): rails and Dock (Mod+B, \, `),
// HUD (P), command palette (Mod+K), help (?), camera modes 1-5, follow lock L, focus F, north N, home Home, selection
// (Mod+A, . and ,), trail and frustum layers (T, V), commands (G GoTo tool, H hover, Shift+X safety stop, Shift+R return
// home, Shift+L land, Shift+T take-off, Delete remove), simulation (Space, [ and ], -> step 100 ms, Shift+-> 1 s,
// Shift+. one tick) and the Esc chain (overlay -> tool -> FPV -> follow lock -> selection). Menus, the palette and the
// hotkeys run the same actions; refused hotkeys flash their reason in the tool hint bar ("read-only mode").
import { navigate, updateSearch } from '@/app/router/router'
import { t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { camera, type CameraMode } from '@/viewport/facade'
import { layers, layersStore } from '@/stores/layers'
import { selection, selectionStore } from '@/stores/selection'
import { LIVE_RATES, REPLAY_RATES, timeline, timelineStore } from '@/stores/timeline'
import { layoutActions } from '@/ui/layout/layoutState'
import { perfHudActions } from '@/ui/hud/PerfHud'
import { overlays, overlaysStore } from '@/ui/shell/overlays'
import { canWriteNow } from '@/ui/shell/connView'
import { writeDeniedKey } from '@/ui/shell/guards'
import { transportReasons } from '@/ui/layout/timelineGuards'
import { bookmarkUi } from '@/ui/panels/timeline/BookmarkEditor'
import { leaveReplay } from '@/ui/views/replayFlow'
import { registerHotkey, setDeniedSink } from '@/ui/hotkeys/registry'
import { toolMode } from '@/ui/tools/toolMode'
import { railFilterActive, railIds, railStore, stepId } from '@/ui/panels/drones/railModel'
import { opText } from '@/ui/notify/severity'
import { precheck } from './admission'
import { confirmThen } from './ConfirmHost'
import { removeVehicles } from './removeVehicles'
import { registerAction, type ActionDescriptor } from './registry'
import { runForSelection } from './vehicleCommands'

const primary = () => selectionStore.getState().primary
const hasSel = () => selectionStore.getState().ids.length > 0
const needWrite = (extra: () => string | null = () => null) => () => writeDeniedKey() ?? extra()
const needSelection = () => (hasSel() ? null : 'hint.noSelection')
const needFocus = () => (primary() ? null : 'hint.noFocus')

/** pre-check of a command for the whole selection (the primary decides for single-vehicle hotkeys) */
function selectionCheck(op: string): string | null {
  const p = primary()
  if (!p) return needSelection()
  if (selectionStore.getState().ids.length >= 2) return null
  const c = precheck(op, p)
  return c.ok ? null : c.reasonKey
}

/** confirmation texts of the dangerous selection commands (AWR-14 §13.4) */
function confirmSelection(op: 'rtl' | 'land' | 'takeoff', args: Record<string, unknown> = {}): void {
  const { ids, primary: p } = selectionStore.getState()
  const n = ids.length
  const target = n >= 2 ? t('confirm.vehicles', { n }) : (p ?? ids[0] ?? '')
  const what = opText(op)
  confirmThen({
    id: `sel:${op}`,
    title: t('confirm.selection.title', { what, target }),
    body: t(`confirm.selection.${op}`, { alt: 2.5 }),
    action: n >= 2 ? t('confirm.selection.actionN', { what, n }) : what,
    run: () => void runForSelection(op, args, { all: n >= 2 && !railFilterActive(railStore.getState()) && n === railIds().length, toastSuccess: true }),
  })
}

function stepRate(dir: 1 | -1): void {
  const s = timelineStore.getState()
  const reasons = transportReasons(s.mode)
  const table = (s.mode === 'replay' ? REPLAY_RATES : LIVE_RATES).filter((r) => reasons.speed(r) === null)
  if (!table.length) return
  const cur = s.rateRequested
  let i = table.findIndex((r) => r >= cur - 1e-9)
  if (i < 0) i = table.length - 1
  const at = Math.abs(table[i] - cur) < 1e-9 ? i + dir : dir > 0 ? i : i - 1
  const next = table[Math.max(0, Math.min(table.length - 1, at))]
  if (next !== cur) timeline.setRate(next)
}

/** transport guards of the current mode (M12 guard table plus the session guards; ui/layout/TimelineBar) */
const tMode = () => timelineStore.getState().mode
const playDenied = () => transportReasons(tMode()).play
const stepDenied = () => transportReasons(tMode()).step
const rateDenied = () => {
  const r = transportReasons(tMode())
  return (tMode() === 'replay' ? REPLAY_RATES : LIVE_RATES).some((x) => r.speed(x) === null) ? null : r.speed(1)
}
/** replay-only transport keys: live mode flashes "live cannot rewind" */
const replayStepDenied = () => (tMode() === 'replay' ? stepDenied() : 'hint.liveNoRewind')
/** Timeline start: replay seeks to the segment start, live fits the view to the whole run */
function timelineStart(): void {
  const s = timelineStore.getState()
  if (s.mode === 'replay' && s.playback) timeline.seek(s.playback.dataStartS)
  else timeline.fit()
}
/** the Timeline (track, slider, bar) holds the keyboard focus (Home and End then act on the timeline, AWR-14 §6.10) */
const timelineFocused = () => typeof document !== 'undefined' && document.activeElement?.closest('[data-timeline-area],[data-slot="timeline-bar"]') != null

/** Esc chain: one level per press (AWR-14 §6.10) */
export function escapeChain(): void {
  const o = overlaysStore.getState()
  if (o.palette || o.help || o.about || o.alarms) {
    overlays.set('palette', false)
    overlays.set('help', false)
    overlays.set('about', false)
    overlays.set('alarms', false)
    return
  }
  if (toolMode.escape()) return
  if (camera.mode === 'fpv') {
    camera.setMode('orbit')
    return
  }
  if (camera.followLock) {
    camera.setFollowLock(false)
    return
  }
  selection.clear()
}

let done = false
export function registerBuiltinActions(): void {
  if (done) return
  done = true
  setDeniedSink((key) => toolMode.flash(key))
  const A: ActionDescriptor[] = [
    // panels and views
    { id: 'view.left', labelKey: 'menu.view.left', icon: 'panel.left', group: 'panel', hotkey: 'mod+KeyB', run: () => layoutActions.toggleLeft() },
    { id: 'view.right', labelKey: 'menu.view.right', icon: 'panel.right', group: 'panel', hotkey: 'Backslash', run: () => layoutActions.toggleRight() },
    { id: 'view.dock', labelKey: 'menu.view.dock', icon: 'panel.bottom', group: 'panel', hotkey: 'Backquote', run: () => layoutActions.toggleDock() },
    { id: 'view.hud', labelKey: 'menu.view.hud', icon: 'nav.perf', group: 'panel', hotkey: 'KeyP', run: () => perfHudActions.toggle() },
    { id: 'panel.perf', labelKey: 'panel.perf.title', icon: 'nav.perf', group: 'panel', run: () => layoutActions.openDockTab('perf') },
    { id: 'panel.events', labelKey: 'panel.events.title', icon: 'tl.marker', group: 'panel', run: () => layoutActions.openDockTab('events') },
    { id: 'panel.mission', labelKey: 'panel.mission.title', icon: 'mission.list', group: 'panel', run: () => layoutActions.openDockTab('mission') },
    { id: 'panel.charts', labelKey: 'panel.charts.title', icon: 'perf.chart', group: 'panel', run: () => layoutActions.openDockTab('charts') },
    { id: 'panel.timeline', labelKey: 'panel.timeline.title', icon: 'tl.clock', group: 'panel', run: () => layoutActions.openDockTab('timeline') },
    { id: 'jump.worlds', labelKey: 'menu.world.open', icon: 'nav.world', group: 'jump', keywords: ['world', 'hub'], run: () => navigate('/worlds') },
    { id: 'palette.open', labelKey: 'header.search', icon: 'cmd.search', group: 'settings', hotkey: 'mod+KeyK', allowInEditable: true, run: () => overlays.set('palette', true) },
    { id: 'help.shortcuts', labelKey: 'menu.help.shortcuts', icon: 'shortcuts', group: 'settings', hotkey: 'shift+Slash', run: () => overlays.set('help', true) },
    { id: 'settings.open', labelKey: 'header.settings', icon: 'nav.settings', group: 'settings', run: () => updateSearch({ settings: 'general' }) },
    // camera (AWR-14 §6.4, §6.5)
    { id: 'camera.follow', labelKey: 'camera.follow', icon: 'cam.follow', group: 'camera', hotkey: 'KeyL', when: () => primary() !== null && (camera.mode === 'orbit' || camera.mode === 'bird'),
      disabledReasonKey: () => (primary() ? 'hint.followMode' : 'hint.noFocus'), run: () => void camera.setFollowLock(!camera.followLock) },
    { id: 'camera.focus', labelKey: 'camera.focus', icon: 'cmd.track', group: 'camera', hotkey: 'KeyF', run: () => camera.focus(selectionStore.getState().ids) },
    { id: 'camera.north', labelKey: 'camera.north', icon: 'heading', group: 'camera', hotkey: 'KeyN', run: () => camera.northUp() },
    // Home is one binding: with the Timeline focused it jumps to the start (AWR-14 §6.10 conflict rule 4), else it resets the view
    { id: 'camera.home', labelKey: 'camera.home', icon: 'cam.reset', group: 'camera', hotkey: 'Home', run: () => (timelineFocused() ? timelineStart() : camera.home()) },
    // selection (AWR-14 §6.2)
    { id: 'select.all', labelKey: 'select.all', icon: 'check', group: 'drone', hotkey: 'mod+KeyA', run: () => selection.select(railIds()) },
    { id: 'select.next', labelKey: 'select.next', icon: 'chev.down', group: 'drone', hotkey: 'Period', run: () => {
      const id = stepId(railIds(), primary(), 1)
      if (id) selection.select([id])
    } },
    { id: 'select.prev', labelKey: 'select.prev', icon: 'chev.up', group: 'drone', hotkey: 'Comma', run: () => {
      const id = stepId(railIds(), primary(), -1)
      if (id) selection.select([id])
    } },
    // layers (AWR-14 §6.15)
    { id: 'layer.trails', labelKey: 'layers.trails', icon: 'layer.trajectory', group: 'layer', hotkey: 'KeyT', run: () => layers.setVisible('trails', !layersStore.getState().visible.trails) },
    { id: 'layer.frustums', labelKey: 'layers.frustums', icon: 'cam.fov', group: 'layer', hotkey: 'KeyV', run: () => layers.setVisible('frustums', !layersStore.getState().visible.frustums) },
    // commands (AWR-14 §6.7, §6.11, §6.13)
    { id: 'cmd.goto', labelKey: 'menu.mission.goto', icon: 'cmd.goto', group: 'drone', hotkey: 'KeyG', keywords: ['goto'],
      when: () => canWriteNow() && primary() !== null && precheck('goto', primary()).ok,
      disabledReasonKey: needWrite(() => needFocus() ?? precheck('goto', primary()).reasonKey),
      run: () => {
        const p = primary()
        if (p) toolMode.enterGoto(p)
      } },
    { id: 'cmd.hover', labelKey: 'command.cmd.hover', icon: 'cmd.hover', group: 'drone', hotkey: 'KeyH', when: () => canWriteNow() && hasSel() && selectionCheck('hover') === null,
      disabledReasonKey: needWrite(() => selectionCheck('hover')), run: () => void runForSelection('hover', {}, { toastSuccess: true }) },
    { id: 'cmd.safety_stop', labelKey: 'command.cmd.safetyStop', icon: 'mission.abort', group: 'drone', hotkey: 'shift+KeyX', when: () => canWriteNow() && hasSel(),
      disabledReasonKey: needWrite(needSelection), run: () => void runForSelection('safety_stop', {}, { toastSuccess: true }) },
    { id: 'cmd.resume', labelKey: 'command.cmd.resume', icon: 'mission.start', group: 'drone', when: () => canWriteNow() && hasSel(),
      disabledReasonKey: needWrite(needSelection), run: () => void runForSelection('resume', {}, { toastSuccess: true }) },
    { id: 'cmd.rtl', labelKey: 'command.cmd.rth', icon: 'cmd.rth', group: 'drone', hotkey: 'shift+KeyR', when: () => canWriteNow() && hasSel() && selectionCheck('rtl') === null,
      disabledReasonKey: needWrite(() => selectionCheck('rtl')), run: () => confirmSelection('rtl') },
    { id: 'cmd.land', labelKey: 'command.cmd.land', icon: 'cmd.land', group: 'drone', hotkey: 'shift+KeyL', when: () => canWriteNow() && hasSel() && selectionCheck('land') === null,
      disabledReasonKey: needWrite(() => selectionCheck('land')), run: () => confirmSelection('land') },
    { id: 'cmd.takeoff', labelKey: 'command.cmd.takeoff', icon: 'cmd.takeoff', group: 'drone', hotkey: 'shift+KeyT', when: () => canWriteNow() && hasSel() && selectionCheck('takeoff') === null,
      disabledReasonKey: needWrite(() => selectionCheck('takeoff')), run: () => confirmSelection('takeoff', { alt_m: 2.5 }) },
    { id: 'vehicle.add', labelKey: 'menu.sim.addP600', icon: 'plus', group: 'drone', keywords: ['p600', 'add'], when: () => canWriteNow(),
      disabledReasonKey: needWrite(), run: () => toolMode.enterAdd() },
    { id: 'vehicle.remove', labelKey: 'command.cmd.remove', icon: 'mission.delete', group: 'drone', hotkey: 'Delete', when: () => canWriteNow() && hasSel(),
      disabledReasonKey: needWrite(needSelection), run: () => removeVehicles(selectionStore.getState().ids) },
    // simulation clock and replay transport (AWR-14 §6.10, §6.17; M12 §8.4; stores/timeline actions: in replay the step
    // keys become seeks of +1 s, +10 s and one recording block, the back keys and PageUp/PageDown only exist there)
    { id: 'sim.toggle', labelKey: 'menu.sim.play', icon: 'tl.play', group: 'sim', hotkey: 'Space', when: () => playDenied() === null,
      disabledReasonKey: playDenied, run: () => timeline.togglePlay() },
    { id: 'sim.slower', labelKey: 'sim.slower', icon: 'tl.rewind', group: 'sim', hotkey: 'BracketLeft', when: () => rateDenied() === null, disabledReasonKey: rateDenied, run: () => stepRate(-1) },
    { id: 'sim.faster', labelKey: 'sim.faster', icon: 'tl.ff', group: 'sim', hotkey: 'BracketRight', when: () => rateDenied() === null, disabledReasonKey: rateDenied, run: () => stepRate(1) },
    { id: 'sim.step', labelKey: 'sim.step100', icon: 'tl.stepfwd', group: 'sim', hotkey: 'ArrowRight', when: () => stepDenied() === null,
      disabledReasonKey: stepDenied, run: () => timeline.step('100ms') },
    { id: 'sim.step1s', labelKey: 'sim.step1s', icon: 'tl.skipfwd', group: 'sim', hotkey: 'shift+ArrowRight', when: () => stepDenied() === null,
      disabledReasonKey: stepDenied, run: () => timeline.step('1s') },
    { id: 'sim.tick', labelKey: 'sim.stepTick', icon: 'tl.stepfwd', group: 'sim', hotkey: 'shift+Period', when: () => stepDenied() === null,
      disabledReasonKey: stepDenied, run: () => timeline.step('tick') },
    { id: 'replay.back1s', labelKey: 'replay.back1s', icon: 'tl.stepback', group: 'sim', hotkey: 'ArrowLeft', when: () => replayStepDenied() === null,
      disabledReasonKey: replayStepDenied, run: () => timeline.step('-1s') },
    { id: 'replay.back10s', labelKey: 'replay.back10s', icon: 'tl.skipback', group: 'sim', hotkey: 'shift+ArrowLeft', when: () => replayStepDenied() === null,
      disabledReasonKey: replayStepDenied, run: () => timeline.step('-10s') },
    { id: 'replay.backSample', labelKey: 'replay.backSample', icon: 'tl.stepback', group: 'sim', hotkey: 'shift+Comma', when: () => replayStepDenied() === null,
      disabledReasonKey: replayStepDenied, run: () => timeline.step('-sample') },
    { id: 'replay.prevMark', labelKey: 'timeline.prevMark', icon: 'tl.skipback', group: 'sim', hotkey: 'PageUp', when: () => tMode() === 'replay' && stepDenied() === null,
      disabledReasonKey: () => (tMode() === 'replay' ? stepDenied() : null), run: () => timeline.jumpMarker(-1) },
    { id: 'replay.nextMark', labelKey: 'timeline.nextMark', icon: 'tl.skipfwd', group: 'sim', hotkey: 'PageDown', when: () => tMode() === 'replay' && stepDenied() === null,
      disabledReasonKey: () => (tMode() === 'replay' ? stepDenied() : null), run: () => timeline.jumpMarker(1) },
    { id: 'timeline.bookmark', labelKey: 'timeline.addBookmark', icon: 'tl.bookmark', group: 'sim', hotkey: 'KeyM', when: () => timelineStore.getState().runId !== null,
      disabledReasonKey: () => 'hint.noRun', run: () => void bookmarkUi.addAndEdit() },
    { id: 'timeline.start', labelKey: 'timeline.start', icon: 'tl.skipback', group: 'sim', run: timelineStart },
    { id: 'timeline.end', labelKey: 'timeline.end', icon: 'tl.skipfwd', group: 'sim', hotkey: 'End', when: timelineFocused, disabledReasonKey: () => null,
      run: () => {
        const s = timelineStore.getState()
        if (s.mode === 'replay' && s.playback) timeline.seek(s.playback.dataEndS)
        else {
          const span = s.view.t1S - s.view.t0S
          timeline.zoom(s.rangeEndS - span, s.rangeEndS)
        }
      } },
    { id: 'replay.runs', labelKey: 'runs.title', icon: 'data.folder', group: 'jump', keywords: ['runs', 'replay', 'recording'], run: () => navigate('/runs') },
    { id: 'replay.exit', labelKey: 'replay.backToLive', icon: 'tl.live', group: 'sim', keywords: ['live', 'replay'], when: () => tMode() === 'replay',
      disabledReasonKey: () => 'hint.notReplay', run: () => void leaveReplay() },
    { id: 'jobs.open', labelKey: 'jobs.title', icon: 'nav.recon', group: 'jump', keywords: ['recon', 'jobs', 'reconstruction'], run: () => navigate('/jobs') },
  ]
  const modes: readonly CameraMode[] = ['orbit', 'free', 'third', 'fpv', 'bird']
  modes.forEach((m, i) => A.push({
    id: `camera.mode.${m}`, labelKey: `camera.${m}`, icon: `cam.${m}` as ActionDescriptor['icon'], group: 'camera', hotkey: `Digit${i + 1}`,
    when: m === 'third' || m === 'fpv' ? () => primary() !== null : undefined,
    disabledReasonKey: m === 'third' || m === 'fpv' ? needFocus : undefined,
    run: () => {
      const r = camera.setMode(m)
      if (!r.ok) notify('camera:mode', 'info', t(r.reason === 'no_camera_sensor' ? 'hint.noCamera' : 'hint.noFocus'))
    },
  }))
  for (const a of A) {
    registerAction(a)
    if (a.hotkey) {
      registerHotkey({
        id: a.id, combo: a.hotkey, labelKey: a.labelKey, group: a.group, allowInEditable: a.allowInEditable, blockedByModal: a.blockedByModal,
        when: a.when, deniedKey: a.disabledReasonKey, run: () => void a.run(),
      })
    }
  }
  registerAction({ id: 'escape', labelKey: 'hotkey.escape', group: 'settings', hotkey: 'Escape', run: escapeChain })
  // Base UI dialogs, menus and popovers close themselves on Escape; the chain only runs when none of them is open
  registerHotkey({
    id: 'escape', combo: 'Escape', labelKey: 'hotkey.escape', group: 'settings', blockedByModal: false,
    when: () => typeof document === 'undefined' || document.querySelector(OPEN_LAYER) === null,
    deniedKey: () => null, run: escapeChain,
  })
}
const OPEN_LAYER = '[data-slot="dialog-content"],[data-slot="alert-dialog-content"],[role="menu"],[data-slot="popover-content"]'
