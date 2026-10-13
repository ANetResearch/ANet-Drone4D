// M18-AC-002 (TS part): the class templates of engine/targets/classes.ts equal the contract
// packages/contracts/target/class_defaults.json (drift check), and the labels and wire codes match rt/enums.json.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { TargetClass } from '@awr/contracts/enums'
import { CLASS_CODE, CLASS_DEFAULTS, CLASS_DEFAULTS_VERSION, CLASS_OF_CODE, LABEL_ZH, TARGET_CLASSES } from '@/engine/targets'

const ROOT = join(import.meta.dirname, '../../../..')

describe('class templates (M18-AC-002)', () => {
  it('equal the contract file', () => {
    const c = JSON.parse(readFileSync(join(ROOT, 'packages/contracts/target/class_defaults.json'), 'utf8'))
    expect(CLASS_DEFAULTS_VERSION).toBe(c.version)
    expect(JSON.parse(JSON.stringify(CLASS_DEFAULTS))).toEqual(c.classes)
    expect(LABEL_ZH).toEqual(c.label_zh)
    expect([...TARGET_CLASSES]).toEqual(Object.keys(c.classes))
  })
  it('wire codes', () => {
    for (const cls of TARGET_CLASSES) {
      expect(CLASS_CODE[cls]).toBe(TargetClass[cls.toUpperCase() as keyof typeof TargetClass])
      expect(CLASS_OF_CODE[CLASS_CODE[cls]]).toBe(cls)
    }
  })
})
