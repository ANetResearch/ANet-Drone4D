// YAML via the project's Python (PyYAML 6.0.3 is locked; no Node YAML dependency is added, M16 §9.4, §14 item 15).
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { ROOT } from './registry.mjs'

export const PY = process.env.AWR_PY ?? join(ROOT, '.venv', 'bin', 'python')

/** parse a YAML file to JSON (null for an empty document); throws when the file is not valid YAML */
export function readYaml(path) {
  if (!existsSync(path)) return null
  const out = execFileSync(PY, ['-c', 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))))', path],
    { encoding: 'utf8', timeout: 30_000 })
  return JSON.parse(out)
}
