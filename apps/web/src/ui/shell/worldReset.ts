// World-bound state of the shell (D2.2-W1; ADR-111; AWR-14 §20.2.3, §20.7.1): what the panels, tools and notices hold about
// the simulation of the previous world is dropped whenever the world scope changes (stores/worldScope: a world switch, a
// source switch between the shared show, static browsing and a sandbox, or a sandbox world switch). The domain stores reset
// themselves (stores/*), the viewport its poses, camera follow and sensor view (viewport/bindings/worldReset.ts); this
// module covers the M15 state:
//   * the target and nest selection of the management panels and a detail or editor page of the right rail (back to the list);
//   * the new-task draft and its drawing tool, the route and area editor, the GoTo and place tools and the ground pick;
//   * manual control (released, a pending confirmation cancelled);
//   * the alarm centre, the event log, unconfirmed discoveries with their sticky toasts and the notice de-duplication.
// Installed once by app/shell/install.ts.
import { dismiss } from '@/app/providers/ToastProvider'
import { prefs, prefsStore } from '@/stores/prefs'
import { onWorldReset } from '@/stores/worldScope'
import { alarms } from '@/ui/notify/alarms'
import { discoveryToastKey, unconfirmedDiscoveries } from '@/ui/notify/discoveries'
import { resetNoticeDedupe } from '@/ui/notify/d2Notices'
import { commitEventLog, eventLog } from '@/ui/notify/eventLog'
import { dropPendingEvents } from '@/ui/notify/eventBridge'
import { manual, manualStore } from '@/ui/manual/controller'
import { routeEditor, editStore } from '@/ui/panels/mission-edit/editStore'
import { taskDraft, taskDraftStore } from '@/ui/panels/tasks/taskDraft'
import { entityStore } from '@/ui/sandbox/entities'
import { d2Tools } from '@/ui/tools/d2Tools'
import { toolMode, toolStore } from '@/ui/tools/toolMode'
import { pick } from '@/viewport/facade'

/** one reset of the shell's world-bound state (exported for tests) */
export function resetShellWorldState(): void {
  const ent = entityStore.getState()
  if (ent.target !== null || ent.nest !== null) entityStore.setState({ target: null, nest: null, version: ent.version + 1 })
  const right = prefsStore.getState().layout.right
  if (right.page === 'detail' || right.page === 'edit') prefs.setLayout({ right: { page: 'list' } })
  const draft = taskDraftStore.getState()
  if (draft.type !== null || draft.phase !== 'type') taskDraft.reset()
  else d2Tools.end()
  if (editStore.getState().phase !== 'CLOSED') routeEditor.close()
  if (toolStore.getState().mode !== 'IDLE') toolMode.done()
  pick.clear()
  const m = manualStore.getState()
  if (m.state === 'CONFIRMING') manual.cancel()
  else if (m.state === 'ACTIVE') void manual.release()
  dropPendingEvents()
  for (const id of unconfirmedDiscoveries().keys()) dismiss(discoveryToastKey(id))
  resetNoticeDedupe()
  if (alarms.size() > 0) alarms.clear()
  if (eventLog.len > 0) {
    eventLog.clear()
    commitEventLog()
  }
}

let off: (() => void) | null = null
/** register the shell's reset with the world scope (start-up, idempotent) */
export function installWorldReset(): () => void {
  off ??= onWorldReset('shell', resetShellWorldState)
  return () => {
    off?.()
    off = null
  }
}
