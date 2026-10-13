// D1-core panels (M15-FR-021; AWR-14 §3.5 table): registered once at start-up, with the shipped ext page mission-edit
// (D1-AC-17); the other ext panels (agents) are registered by registerExtPanels() only when their D1-ext features ship (dev
// and test builds can opt in with ?ext=1).
import { createElement } from 'react'
import { registerPanel } from './registry'
import { WorldPanel } from './world/WorldPanel'
import { LayersPanel } from './layers/LayersPanel'
import { EnvPanel } from './env/EnvPanel'
import { DronesPanel } from './drones/DronesPanel'
import { DroneDetailPanel } from './drone-detail/DroneDetailPanel'
import { MissionPanel } from './mission/MissionPanel'
import { TimelinePanel } from './timeline/TimelinePanel'
import { EventsPanel } from './events/EventsPanel'
import { ChartsPanel } from './charts/ChartsPanel'
import { PerfPanel } from './perf/PerfPanel'
import { AgentsPanel } from './agents/AgentsPanel'
import { MissionEditPanel } from './mission-edit/MissionEditPanel'
import { TargetsPanel } from './targets/TargetsPanel'
import { TargetDetailPanel } from './targets/TargetDetailPanel'
import { NestDetailPanel, NestsPanel } from './nests/NestsPanel'
import { TasksPanel } from './tasks/TasksPanel'
import { RelayPanel } from './tasks/RelayPanel'
import { TaskNewPanel } from './tasks/TaskNewPanel'

let done = false
export function registerCorePanels(): void {
  if (done) return
  done = true
  registerPanel({ id: 'world', titleKey: 'panel.world.title', icon: 'nav.world', home: 'left', allowed: ['left'], minSize: { w: 256, h: 0 }, layer: 'core', order: 0, render: () => createElement(WorldPanel) })
  registerPanel({ id: 'layers', titleKey: 'panel.layers.title', icon: 'nav.layers', home: 'left', allowed: ['left', 'right'], minSize: { w: 256, h: 0 }, layer: 'core', order: 1, render: () => createElement(LayersPanel) })
  registerPanel({ id: 'env', titleKey: 'panel.env.title', icon: 'nav.environment', home: 'left', allowed: ['left', 'right', 'bottom'], minSize: { w: 256, h: 160 }, layer: 'core', order: 2, render: () => createElement(EnvPanel) })
  registerPanel({ id: 'drones', titleKey: 'panel.drones.title', icon: 'nav.fleet', home: 'right', allowed: ['right', 'left'], minSize: { w: 280, h: 0 }, layer: 'core', order: 0, render: () => createElement(DronesPanel) })
  registerPanel({ id: 'drone-detail', titleKey: 'panel.droneDetail.title', icon: 'drone.quad', home: 'right', allowed: ['right'], minSize: { w: 280, h: 0 }, layer: 'core', order: 1, streaming: 2, render: () => createElement(DroneDetailPanel) })
  // the route and area editor ships with D1-AC-17 (P4-UI): page 3 of the right rail, opened from the detail page and the mission tab
  registerPanel({ id: 'mission-edit', titleKey: 'panel.missionEdit.title', icon: 'mission.edit', home: 'right', allowed: ['right'], minSize: { w: 320, h: 0 }, layer: 'ext', order: 2, render: () => createElement(MissionEditPanel) })
  registerPanel({ id: 'events', titleKey: 'panel.events.title', icon: 'tl.marker', home: 'bottom', allowed: ['bottom', 'right'], minSize: { w: 0, h: 160 }, layer: 'core', order: 0, render: () => createElement(EventsPanel) })
  registerPanel({ id: 'charts', titleKey: 'panel.charts.title', icon: 'perf.chart', home: 'bottom', allowed: ['bottom', 'right'], minSize: { w: 0, h: 200 }, layer: 'core', order: 1, streaming: 4, render: () => createElement(ChartsPanel) })
  registerPanel({ id: 'perf', titleKey: 'panel.perf.title', icon: 'nav.perf', home: 'bottom', allowed: ['bottom', 'right'], minSize: { w: 0, h: 200 }, layer: 'core', order: 2, streaming: 2, render: () => createElement(PerfPanel) })
  registerPanel({ id: 'mission', titleKey: 'panel.mission.title', icon: 'mission.list', home: 'bottom', allowed: ['bottom', 'right'], minSize: { w: 280, h: 160 }, layer: 'core', order: 3, render: () => createElement(MissionPanel) })
  registerPanel({ id: 'timeline', titleKey: 'panel.timeline.title', icon: 'tl.clock', home: 'bottom', allowed: ['bottom'], minSize: { w: 0, h: 160 }, layer: 'core', order: 4, render: () => createElement(TimelinePanel) })
  // D2 entity pages of the right rail (AWR-14 §20.5.2; M15-FR-211 to FR-213): targets and nests lists with their detail
  // pages, switched by the rail's entity ToggleGroup (ui/layout/DroneRail.tsx)
  registerPanel({ id: 'targets', titleKey: 'panel.targets.title', icon: 'target.person', home: 'right', allowed: ['right', 'left'], minSize: { w: 280, h: 0 }, layer: 'core', order: 3, render: () => createElement(TargetsPanel) })
  registerPanel({ id: 'target-detail', titleKey: 'panel.targetDetail.title', icon: 'target.person', home: 'right', allowed: ['right'], minSize: { w: 320, h: 0 }, layer: 'core', order: 4, render: () => createElement(TargetDetailPanel) })
  registerPanel({ id: 'nests', titleKey: 'panel.nests.title', icon: 'nest.box', home: 'right', allowed: ['right', 'left'], minSize: { w: 280, h: 0 }, layer: 'core', order: 5, render: () => createElement(NestsPanel) })
  registerPanel({ id: 'nest-detail', titleKey: 'panel.nestDetail.title', icon: 'nest.box', home: 'right', allowed: ['right'], minSize: { w: 320, h: 0 }, layer: 'core', order: 6, render: () => createElement(NestDetailPanel) })
  // D2 tasking (AWR-14 §20.5.2, §20.9; M15-FR-214): the "new task" page (right rail page 3, drawn in the viewport) and the
  // Dock task and relay tabs; the Dock shows them instead of the D1 mission tab on runs with the `tasking` capability
  // (ui/layout/BottomDock.tsx dockPanelsFor)
  registerPanel({ id: 'task-new', titleKey: 'panel.taskNew.title', icon: 'task.area', home: 'right', allowed: ['right'], minSize: { w: 360, h: 0 }, layer: 'core', order: 7, render: () => createElement(TaskNewPanel) })
  registerPanel({ id: 'tasks', titleKey: 'panel.tasks.title', icon: 'task.guard', home: 'bottom', allowed: ['bottom', 'right'], minSize: { w: 320, h: 200 }, layer: 'core', order: 3, render: () => createElement(TasksPanel) })
  registerPanel({ id: 'relay', titleKey: 'panel.relay.title', icon: 'task.relay', home: 'bottom', allowed: ['bottom', 'right'], minSize: { w: 320, h: 200 }, layer: 'core', order: 3.5, streaming: 1, render: () => createElement(RelayPanel) })
}

let extDone = false
export function registerExtPanels(): void {
  if (extDone) return
  extDone = true
  registerPanel({ id: 'agents', titleKey: 'panel.agents.title', icon: 'nav.agents', home: 'bottom', allowed: ['bottom', 'right'], minSize: { w: 0, h: 200 }, layer: 'ext', order: 5, render: () => createElement(AgentsPanel) })
}
