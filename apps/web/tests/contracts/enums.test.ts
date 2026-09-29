// Enums, reasons and the admission matrix: TS generated code equals the contract files and the Python golden
import { describe, expect, it } from 'vitest'
import * as E from '@awr/contracts/enums'
import { ADMISSION_COLUMNS, SERVICE_BY_OP, admitSymbol, matchService } from '@awr/contracts/commands'
import { REASONS, WS_CLOSE } from '@awr/contracts/reasons'
import { RATE_CLASSES, matchTopic, quantizeRate } from '@awr/contracts/topics'
import { readJson } from './util'

type Json = Record<string, unknown>
const enums = readJson<Json>('rt/enums.json')
const lib = E as unknown as Record<string, unknown>
const ident = (v: string): string => v.toUpperCase().replace(/[^A-Z0-9_]/g, '_')

describe('enums', () => {
  it('list and map enums keep their wire values', () => {
    for (const [name, def] of Object.entries(enums)) {
      if (name === 'schema' || name === 'doc' || !(name in lib)) continue
      const gen = lib[name] as Record<string, number>
      if (Array.isArray(def)) {
        def.forEach((v, i) => {
          if (typeof v === 'string') expect(gen[ident(v)] ?? gen[v], `${name}.${v}`).toBe(i)
        })
      } else if (def && typeof def === 'object' && Object.values(def).every((x) => typeof x === 'number')) {
        const vals = Object.values(def as Record<string, number>)
        if (new Set(vals).size === vals.length) for (const [k, v] of Object.entries(def as Record<string, number>)) expect(gen[k] ?? gen[ident(k)], `${name}.${k}`).toBe(v)
      }
    }
  })

  it('FlightState frozen values (AWR-17 §6.5)', () => {
    expect(E.FlightState.FLYING).toBe(5)
    expect(E.FlightState.CRASHED).toBe(13)
    expect(E.FlightFlags.FAILSAFE).toBe(8)
    expect(E.TimeState.LIVE).toBe(9)
  })
})

describe('admission', () => {
  const rows = readJson<{ rows: [string, number, number, string, string | null][] }>('golden/commands/admission.json').rows
  it('admitSymbol equals the Python golden for every op x state x failsafe', () => {
    expect(rows.length).toBeGreaterThan(400)
    for (const [op, fs, failsafe, sym, cond] of rows) {
      expect(admitSymbol(op, fs, failsafe ? E.FlightFlags.FAILSAFE : 0), `${op}/${fs}/${failsafe}`).toEqual([sym, cond])
    }
  })

  it('16 columns with RTL and LANDING split by FAILSAFE', () => {
    expect(ADMISSION_COLUMNS.length).toBe(16)
    expect(SERVICE_BY_OP.takeoff.admission?.length).toBe(16)
  })

  it('service names match with parameters and aliases', () => {
    const m = matchService('uav/uav0001/cmd/goto')
    expect(m?.service.op).toBe('goto')
    expect(m?.params).toEqual({ id: 'uav0001' })
    expect(matchService('uav/uav0001/cmd/hold')?.service.op).toBe('hover')
  })
})

describe('reasons and topics', () => {
  const doc = readJson<{ reasons: { code: number; name: string; http: number | null }[]; ws_close_codes: { code: number; name: string }[] }>('rt/reasons.json')
  it('REASONS equals reasons.json', () => {
    expect(Object.keys(REASONS).length).toBe(doc.reasons.length)
    for (const r of doc.reasons) expect([REASONS[r.code].name, REASONS[r.code].http]).toEqual([r.name, r.http])
    for (const c of doc.ws_close_codes) expect((WS_CLOSE as Readonly<Record<string, number>>)[c.name]).toBe(c.code)
  })

  it('topics match and rates quantise to rate classes', () => {
    expect(matchTopic('uav/uav0001/state')?.schemaName).toBe('awr.DroneState64.v1')
    expect(matchTopic('swarm/uav/state')?.schemaName).toBe('awr.SwarmLite32.v1')
    expect(RATE_CLASSES).toContain(quantizeRate(12))
  })
})
