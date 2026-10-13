// Open interface page model (AWR-04 §11.4; M15-FR-221; UX-FR-145; AWR-14 §20.14): pure helpers over an OpenAPI 3.1
// document of the sandbox REST v1 (live GET /api/sandbox/v1/openapi.json; the contracts snapshot has the same routes):
// every operation (method, path, summary, parameters, body, responses, x-awr auth / idempotency / rate class / SB id),
// grouped by the owner file tag into the page's sections, and the copyable examples: curl with the token as the
// placeholder $AWR_SANDBOX_TOKEN (never the real token) and the session id filled in when this browser holds one, and the
// Python SDK call of tools/sdk/awr_sandbox.py (the server's x-codeSamples Python source when the live document has one),
// and the reason codes of x-awr-reasons.

export const METHODS = ['get', 'post', 'put', 'patch', 'delete'] as const
export type Method = (typeof METHODS)[number]

export interface ApiParam { name: string; in: string; required: boolean; type: string; description: string }
export interface ApiOp {
  key: string
  method: Method
  path: string
  summary: string
  description: string
  group: string
  params: ApiParam[]
  body: string | null
  responses: { code: string; description: string; schema: string | null }[]
  auth: string
  idempotent: string
  rate: string
  sb: string
  realtime: string | null
  /** reason codes the operation may answer (x-awr-reasons, M17-FR-053; absent in the contracts snapshot) */
  reasons: number[]
  /** the server's Python sample (x-codeSamples lang Python), preferred over the generated SDK call */
  pySample: string | null
}

/** page sections in display order (tag -> group id) */
export const GROUPS: readonly { id: string; tags: readonly string[] }[] = [
  { id: 'sessions', tags: ['rest/sandbox.py'] },
  { id: 'catalog', tags: ['rest/catalog.py'] },
  { id: 'vehicles', tags: ['rest/fleet.py'] },
  { id: 'perception', tags: ['rest/perception.py'] },
  { id: 'targets', tags: ['rest/targets.py'] },
  { id: 'tasking', tags: ['rest/tasking.py'] },
  { id: 'env', tags: ['rest/env.py'] },
  { id: 'realtime', tags: ['sandbox-api'] },
]

type Json = Record<string, unknown>
const obj = (x: unknown): Json => (x && typeof x === 'object' && !Array.isArray(x) ? (x as Json) : {})
const str = (x: unknown): string => (typeof x === 'string' ? x : '')

/** short name of a schema reference ("session.schema.json#/$defs/create_request" -> "session.create_request") */
export function refName(ref: string): string {
  const m = /\/([a-z_]+)\.schema\.json(?:#\/\$defs\/([A-Za-z0-9_]+))?$/.exec(ref)
  if (!m) return ref.split('/').pop() ?? ref
  return m[2] ? `${m[1]}.${m[2]}` : m[1]
}

function schemaOf(content: unknown): string | null {
  const c = obj(content)
  for (const mt of Object.keys(c)) {
    const s = obj(obj(c[mt]).schema)
    if (typeof s.$ref === 'string') return refName(s.$ref)
    if (typeof s.type === 'string') return s.type
  }
  return null
}

function groupOf(tags: unknown): string {
  const list = Array.isArray(tags) ? tags.filter((x): x is string => typeof x === 'string') : []
  for (const g of GROUPS) if (list.some((t) => g.tags.includes(t))) return g.id
  return 'other'
}

/** every operation of the document, in path order */
export function listOperations(doc: unknown): ApiOp[] {
  const paths = obj(obj(doc).paths)
  const out: ApiOp[] = []
  for (const path of Object.keys(paths).sort()) {
    const item = obj(paths[path])
    const shared = Array.isArray(item.parameters) ? item.parameters : []
    for (const method of METHODS) {
      const op = obj(item[method])
      if (!item[method]) continue
      const x = obj(op['x-awr'])
      const params: ApiParam[] = [...shared, ...(Array.isArray(op.parameters) ? op.parameters : [])].map((p) => {
        const q = obj(p)
        const sch = obj(q.schema)
        return { name: str(q.name), in: str(q.in), required: q.required === true || q.in === 'path', type: str(sch.type) || (typeof sch.$ref === 'string' ? refName(sch.$ref) : ''), description: str(q.description) }
      })
      const responses = Object.entries(obj(op.responses)).map(([code, r]) => ({ code, description: str(obj(r).description), schema: schemaOf(obj(r).content) }))
      const rt = op['x-awr-realtime']
      const reasons = Array.isArray(op['x-awr-reasons']) ? op['x-awr-reasons'].filter((c): c is number => Number.isInteger(c)) : []
      const samples = Array.isArray(op['x-codeSamples']) ? op['x-codeSamples'].map(obj) : []
      const py = samples.find((x) => x.lang === 'Python')
      out.push({
        key: `${method.toUpperCase()} ${path}`, method, path, summary: str(op.summary), description: str(op.description), group: groupOf(op.tags), params,
        body: op.requestBody ? schemaOf(obj(op.requestBody).content) ?? 'object' : null, responses,
        auth: str(x.auth) || (Array.isArray(op.security) && op.security.length === 0 ? 'public' : 'sandbox'), idempotent: str(x.idempotent), rate: str(x.rate), sb: str(x.sb),
        realtime: typeof rt === 'string' ? rt : rt ? JSON.stringify(rt) : null,
        reasons, pySample: py && typeof py.source === 'string' ? py.source : null,
      })
    }
  }
  return out
}

/** operations per page section (sections without operations are left out) */
export function groupOperations(ops: readonly ApiOp[]): { id: string; ops: ApiOp[] }[] {
  const ids = [...GROUPS.map((g) => g.id), 'other']
  return ids.map((id) => ({ id, ops: ops.filter((o) => o.group === id) })).filter((g) => g.ops.length > 0)
}

/** example request bodies of the common writes (others: an empty object) */
const BODIES: Readonly<Record<string, unknown>> = {
  'POST /sessions': { world: 'synthcity', template: 't5_area_guard', principal_hint: '<32 hex>' },
  'POST /sessions/{sid}/targets': { class: 'person', pos: [150.0, -40.0] },
  'POST /sessions/{sid}/nests': { name: 'N1', pos: [-120.0, 80.0], kind: 'box' },
  'POST /sessions/{sid}/vehicles': { model_id: 'p600_mid360', nest_id: 'nest-01' },
  'POST /sessions/{sid}/vehicles/{vid}/commands': { op: 'takeoff', args: { alt_m: 60.0 } },
  'POST /sessions/{sid}/clock': { op: 'speed', rate: 5 },
  'POST /sessions/{sid}/template': { template: 't5_area_guard' },
  'POST /sessions/{sid}/world': { world: 'shanghai', template: 't5_area_guard' },
  'POST /sessions/{sid}/tasks': { type: 'area_scan_gsd', polygon: [[0, -600], [600, -600], [600, -200], [0, -200]], gsd_cm: 3.0 },
}

/** the path with the session id filled in when known (other parameters stay as {name}) */
export function fillPath(path: string, sid: string | null): string {
  return sid ? path.replace('{sid}', sid) : path
}

/** copyable curl (the token is always the placeholder $AWR_SANDBOX_TOKEN) */
export function curlOf(op: ApiOp, origin: string, sid: string | null): string {
  const url = `${origin}/api/sandbox/v1${fillPath(op.path, sid)}`
  const lines = [`curl -X ${op.method.toUpperCase()} '${url}'`]
  if (op.auth !== 'public') lines.push(`  -H "Authorization: Bearer $AWR_SANDBOX_TOKEN"`)
  if (op.body) {
    lines.push(`  -H 'content-type: application/json'`)
    lines.push(`  -d '${JSON.stringify(BODIES[`${op.method.toUpperCase()} ${op.path}`] ?? {})}'`)
  }
  return lines.join(' \\\n')
}

/** the Python SDK equivalent (tools/sdk/awr_sandbox.py; the generic request() for routes without a helper) */
export function sdkOf(op: ApiOp, origin: string): string {
  const head = `from awr_sandbox import Sandbox\n\nsb = Sandbox("${origin}")\n`
  const k = `${op.method.toUpperCase()} ${op.path}`
  const known: Readonly<Record<string, string>> = {
    'POST /sessions': 's = sb.create("synthcity", "t5_area_guard")',
    'GET /sessions/{sid}': 's = sb.create("synthcity")\nprint(s.state())',
    'POST /sessions/{sid}/keepalive': 's = sb.create("synthcity")\ns.keepalive()',
    'GET /sessions/{sid}/snapshot': 's = sb.create("synthcity")\nsnap = s.snapshot()',
    'POST /sessions/{sid}/world': 's = sb.create("synthcity")\ns.switch_world("shanghai")',
    'POST /sessions/{sid}/template': 's = sb.create("synthcity")\ns.load_template("t1_point_watch")',
    'POST /sessions/{sid}/clock': 's = sb.create("synthcity")\ns.clock("speed", rate=5)',
    'GET /sessions/{sid}/vehicles': 's = sb.create("synthcity")\nprint(s.vehicles.list())',
    'POST /sessions/{sid}/vehicles': 's = sb.create("synthcity")\nuav = s.vehicles.add("p600_mid360", nest_id="nest-01")',
    'GET /sessions/{sid}/targets': 's = sb.create("synthcity")\nprint(s.targets.list())',
    'POST /sessions/{sid}/targets': 's = sb.create("synthcity")\ns.targets.create({"class": "person", "pos": [150.0, -40.0]})',
    'GET /catalog/models': 'print(sb.catalog.models())',
  }
  const rel = op.path.replace(/^\/sessions\/\{sid\}\/?/, '')
  const body = known[k] ?? (op.path.startsWith('/sessions/{sid}')
    ? `s = sb.create("synthcity")\nprint(s.sb.http.request("${op.method.toUpperCase()}", s.path("${rel}"), token=s.token))`
    : `print(sb.http.request("${op.method.toUpperCase()}", "/api/sandbox/v1${op.path}"))`)
  return head + body
}
