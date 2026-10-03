// engine/mission facade (M06): mission overlay, GoTo marker, zones, overlay line batches.
export { GotoMarker, GOTO_MARKER, type GotoState } from './gotoMarker'
export { MissionOverlay, MISSION, EMPTY_MISSION, type MissionData, type MissionDraft, type MissionPath, type MissionArea, type Waypoint, type WaypointState } from './MissionOverlay'
export { ZonesLayer, ZONES, parseZones, pointInRing, type ZoneFeature, type ZoneBuilt } from './zones'
export { ThinLineBatch, WideLineBatch } from './lineBatch'
