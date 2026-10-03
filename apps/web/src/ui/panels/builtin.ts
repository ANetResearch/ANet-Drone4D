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
}

let extDone = false
export function registerExtPanels(): void {
  if (extDone) return
  extDone = true
  registerPanel({ id: 'agents', titleKey: 'panel.agents.title', icon: 'nav.agents', home: 'bottom', allowed: ['bottom', 'right'], minSize: { w: 0, h: 200 }, layer: 'ext', order: 5, render: () => createElement(AgentsPanel) })
}
