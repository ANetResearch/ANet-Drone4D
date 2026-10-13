// D2.2-W1: a `sel` deep link belongs to the world of its route (ui/shell/urlSync requestSelection): it is applied once the
// world scope is live for that world and the roster has arrived (unknown ids dropped), and it is dropped when the world is
// browsed statically, when the source simulates another world, or when the view settles on another world first.
import { beforeEach, describe, expect, it } from 'vitest'
import { createRtClient } from '@/net/rt'
import { worldScope } from '@/stores/worldScope'
import { selection, selectionStore } from '@/stores/selection'
import { checkPendingSelection, pendingSelection, requestSelection } from '@/ui/shell/urlSync'

const rt = createRtClient()
const roster = rt.roster as unknown as { set(version: number, entries: Record<string, unknown>[]): void; clear(): void }

describe('world-bound sel deep link', () => {
  beforeEach(() => {
    worldScope.clearForTests()
    selection.clear()
    roster.clear()
  })

  it('waits for the live world and the roster, then selects the known ids only', () => {
    worldScope.setView('synthcity', 'shared')
    requestSelection('synthcity', ['p600-03', 'zz-99'])
    expect(pendingSelection()).not.toBe(null)
    worldScope.setStreamWorld('synthcity')
    expect(selectionStore.getState().ids).toEqual([]) // no roster yet
    roster.set(1, [{ agent_no: 3, id: 'p600-03', kind: 'uav' }])
    checkPendingSelection()
    expect(pendingSelection()).toBe(null)
    expect(selectionStore.getState().ids).toEqual(['p600-03'])
  })

  it('is dropped on a statically browsed world and when the source simulates another world', () => {
    worldScope.setView('shenzhen', null)
    roster.set(2, [{ agent_no: 3, id: 'p600-03', kind: 'uav' }])
    requestSelection('shenzhen', ['p600-03'])
    expect(pendingSelection()).toBe(null)
    expect(selectionStore.getState().ids).toEqual([])
    // local build: the run world is synthcity, the route shows shenzhen
    worldScope.setView('shenzhen', 'local')
    worldScope.setStreamWorld('synthcity')
    requestSelection('shenzhen', ['p600-03'])
    expect(pendingSelection()).toBe(null)
    expect(selectionStore.getState().ids).toEqual([])
  })

  it('waits while the viewport still shows the previous world, then follows where the view settles', () => {
    worldScope.setView('synthcity', 'shared')
    worldScope.setStreamWorld('synthcity')
    roster.set(3, [{ agent_no: 3, id: 'p600-03', kind: 'uav' }])
    requestSelection('shenzhen', ['p600-03']) // the route changed, the canvas has not opened shenzhen yet
    expect(pendingSelection()?.world).toBe('shenzhen')
    expect(selectionStore.getState().ids).toEqual([])
    worldScope.setView('synthcity', null)
    worldScope.setView('shenzhen', null) // static browsing of shenzhen
    expect(pendingSelection()).toBe(null)
    expect(selectionStore.getState().ids).toEqual([])
  })
})
