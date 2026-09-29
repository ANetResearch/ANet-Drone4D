// Query key factory (M15-FR-038, §6.6.3).
export const qk = {
  worlds: () => ['worlds'] as const,
  world: (id: string) => ['worlds', id] as const,
  sessionCurrent: () => ['sessions', 'current'] as const,
  scenarios: (worldId: string) => ['scenarios', worldId] as const,
  fleetVehicles: () => ['fleet', 'vehicles'] as const,
  missions: () => ['missions'] as const,
  envPresets: () => ['env', 'presets'] as const,
  sysInfo: () => ['sys', 'info'] as const,
}
