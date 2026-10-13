// Open state of the fleet header's model catalogue Sheet and "add vehicle" Dialog (AWR-14 §20.6.2, §20.6.4), shared so
// that other pages can open them: the task preview's "fleet short" remedy offers "add vehicle" (AWR-14 §20.9.3), the GSD
// calculator and the clone form link the catalogue. Written on user actions only.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'

export interface FleetDialogsState { catalog: boolean; add: boolean }
export const fleetDialogsStore = createAwrStore<FleetDialogsState>('ui.fleetDialogs', () => ({ catalog: false, add: false }))
export const useFleetDialogs = <T,>(sel: (s: FleetDialogsState) => T): T => useStore(fleetDialogsStore, sel)

export const fleetDialogs = {
  setCatalog(catalog: boolean): void {
    fleetDialogsStore.setState({ catalog })
  },
  setAdd(add: boolean): void {
    fleetDialogsStore.setState({ add })
  },
  openCatalog(): void {
    fleetDialogsStore.setState({ catalog: true })
  },
  openAdd(): void {
    fleetDialogsStore.setState({ add: true })
  },
}
