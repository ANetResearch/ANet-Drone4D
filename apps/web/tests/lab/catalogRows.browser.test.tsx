// /lab/ sample catalog: a click anywhere on a sample row opens the sample run page (stretched row navigation), the run id stays a
// real link (keyboard, middle click, copy address), rows are grouped by event, and the application's World Hub offers the
// Event Lab entry only when VITE_AWR_LAB_PUBLIC is on. Real browser: the row click goes through history.pushState.
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CatalogPage, groupByEvent } from '@/app/lab-public/pages'
import { parseStaticIndex, sampleRuns } from '@/app/lab-public/staticIndex'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function index() {
  const run = (id: string, policy: string) => ({ run_id: id, policy, n_uav: 3, obs_level: 'L2', videos: [], slices: [] })
  return parseStaticIndex({
    schema: 'drone4d.lab_static_index.v1', version: '2026.11.03-1', built_unix_ns: '1', license: { files: [], attribution: [] },
    worlds: [], files_sha256: 'SHA256SUMS', features: { inspect: true },
    events: [
      { id: 'crop-handoff-001', version: 1, world_id: 'oryahovo-crop1', tape: 't.bin', runs: [run('run-aaaaaaaaaa', 'track_share'), run('run-bbbbbbbbbb', 'greedy')] },
      { id: 'abc-crossing-001', version: 2, world_id: 'oryahovo-crop1', tape: 't2.bin', runs: [run('run-cccccccccc', 'track_share')] },
    ],
  })
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  history.replaceState(null, '', '/lab/')
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

describe('catalog rows', () => {
  it('groups the runs by event, sorted by event id', () => {
    const g = groupByEvent(sampleRuns(index()))
    expect(g.map((x) => [x.eventId, x.runs.length])).toEqual([['abc-crossing-001', 1], ['crop-handoff-001', 2]])
  })

  it('opens the sample run page on a click on any cell of the row, and keeps the run id as a link', () => {
    act(() => root.render(<CatalogPage index={index()} params={{}} />))
    expect(host.querySelectorAll('[data-catalog-event]')).toHaveLength(2)
    const link = host.querySelector<HTMLAnchorElement>('[data-sample-link="run-bbbbbbbbbb"]')!
    expect(link.getAttribute('href')).toBe('/lab/samples/run-bbbbbbbbbb')
    const row = host.querySelector<HTMLTableRowElement>('tr[data-row-key="run-bbbbbbbbbb"]')!
    expect(row.className).toContain('cursor-pointer')
    const policyCell = row.querySelectorAll('td')[1]!
    expect(policyCell.textContent).toBe('greedy')
    act(() => policyCell.click())
    expect(location.pathname).toBe('/lab/samples/run-bbbbbbbbbb')
  })

  it('navigates once when the run id link itself is clicked', () => {
    act(() => root.render(<CatalogPage index={index()} params={{}} />))
    const before = history.length
    act(() => host.querySelector<HTMLAnchorElement>('[data-sample-link="run-cccccccccc"]')!.click())
    expect(location.pathname).toBe('/lab/samples/run-cccccccccc')
    expect(history.length - before).toBe(1)
  })
})
