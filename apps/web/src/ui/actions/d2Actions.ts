// D2 actions (AWR-14 §20.2.4 menu and command palette increments, §20.10.2 keys B, - / = and I, §20.13.4; M15-FR-214,
// FR-218, FR-220; UX-FR-105, UX-FR-136): the sensor view cycle (B, FPV camera only; local display state, viewer safe), the
// operator's zoom steps (- / =, at most 4 per second) and night illuminator (I) in the FPV camera, the GSD and
// recognition-distance calculator, "new task" (the D2 write gate: sandbox operator or the full-function identity), the
// Dock task and relay tabs; and the `sv` URL parameter (AWR-14 §20.2.2: with cam=fpv and a focus vehicle the first FPV
// entry applies the sensor view). Registered once at start-up next to the D1 built-in actions (app/shell/install.ts).
import { notify } from '@/app/providers/ToastProvider'
import { t } from '@/app/i18n'
import { prefs } from '@/stores/prefs'
import { camera, sensorView, type SensorViewMode } from '@/viewport/facade'
import { gsdCalc } from '@/ui/views/GsdCalculator'
import { fpvToggleIllum, fpvZoomStep } from '@/ui/fpv/SensorViewBar'
import { taskDraft } from '@/ui/panels/tasks/taskDraft'
import { registerHotkey } from '@/ui/hotkeys/registry'
import { d2WriteNow } from '@/ui/sandbox/writeGate'
import { registerAction } from './registry'

const SV: readonly SensorViewMode[] = ['fpv', 'eo', 'nir', 'lwir']

let done = false
export function registerD2Actions(): () => void {
  if (done) return () => {}
  done = true
  const offs = [
    registerAction({ id: 'camera.sensorView', labelKey: 'fpv.cycle', icon: 'sensor.camera', group: 'camera', hotkey: 'KeyB', keywords: ['sensor', 'thermal', 'night'],
      when: () => camera.mode === 'fpv', disabledReasonKey: () => 'fpv.needFpv', run: () => void sensorView.cycle() }),
    registerAction({ id: 'tool.gsd', labelKey: 'gsd.open', icon: 'perf.chart', group: 'jump', keywords: ['gsd', 'calculator'], run: () => gsdCalc.open() }),
    registerAction({ id: 'task.new', labelKey: 'task.new', icon: 'task.area', group: 'jump', keywords: ['task', 'mission'],
      when: d2WriteNow, disabledReasonKey: () => 'task.readOnly', run: () => taskDraft.open() }),
    registerAction({ id: 'panel.tasks', labelKey: 'panel.tasks.title', icon: 'task.guard', group: 'panel', run: () => prefs.setLayout({ dock: { open: true, tab: 'tasks' } }) }),
    registerAction({ id: 'panel.relay', labelKey: 'panel.relay.title', icon: 'task.relay', group: 'panel', run: () => prefs.setLayout({ dock: { open: true, tab: 'relay' } }) }),
    registerHotkey({ id: 'camera.sensorView', combo: 'KeyB', scope: 'global', labelKey: 'fpv.cycle', group: 'camera', when: () => camera.mode === 'fpv',
      deniedKey: () => null, run: () => void sensorView.cycle() }),
    // FPV operator keys (AWR-14 §20.10.2): zoom out / in one step, the night view's illuminator
    registerHotkey({ id: 'fpv.zoomOut', combo: 'Minus', scope: 'global', labelKey: 'fpv.zoomOut', group: 'camera', when: () => camera.mode === 'fpv' && d2WriteNow(),
      deniedKey: () => null, run: () => fpvZoomStep(0.8) }),
    registerHotkey({ id: 'fpv.zoomIn', combo: 'Equal', scope: 'global', labelKey: 'fpv.zoomIn', group: 'camera', when: () => camera.mode === 'fpv' && d2WriteNow(),
      deniedKey: () => null, run: () => fpvZoomStep(1.25) }),
    registerHotkey({ id: 'fpv.illum', combo: 'KeyI', scope: 'global', labelKey: 'fpv.illum', group: 'camera', when: () => camera.mode === 'fpv' && sensorView.mode === 'nir' && d2WriteNow(),
      deniedKey: () => null, run: () => fpvToggleIllum() }),
  ]
  offs.push(applySvParam())
  return () => {
    for (const off of offs) off()
    done = false
  }
}

/** `?cam=fpv&sv=eo|nir|lwir`: the first FPV entry applies the sensor view (ignored without FPV or a focus vehicle) */
function applySvParam(): () => void {
  if (typeof location === 'undefined') return () => {}
  const q = new URLSearchParams(location.search)
  const sv = q.get('sv') as SensorViewMode | null
  if (!sv || !SV.includes(sv) || sv === 'fpv') return () => {}
  let off: (() => void) | null = null
  const tryApply = (mode: string): void => {
    if (mode !== 'fpv' || !off) return
    const r = sensorView.set(sv)
    if (!r.ok) notify('fpv:sv', 'info', t(`fpv.reason.${r.reason ?? 'not_fpv'}`))
    off()
    off = null
  }
  off = camera.onMode((m) => tryApply(m.mode))
  return () => off?.()
}
