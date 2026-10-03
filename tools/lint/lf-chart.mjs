// LF-CHART-01: chart code is deterministic and updates its marks in place (AWR-15 §12; AWR-18 §4.6 "chart implementation",
// §13.1). Scope: apps/web/src/ui/lf/** (every chart component and its helpers). Written from the AWR-15 and AWR-18 rule text
// as an independent AST check on oxc-parser (P4-UI, ADR-072); it shares no code with any chart library.
//   entropy   a value that differs between renders: Math.random(), crypto.getRandomValues(), crypto.randomUUID()
//             (marks take their jitter from ui/lf/rnd.ts, a pure function of the mark index)
//   rebuild   a write that throws away a chart subtree: assignment to innerHTML or outerHTML, replaceChildren();
//             marks are created once and their attributes are updated afterwards
//   fixed-id  an element id given as a constant (JSX id="..", id={'..'}, setAttribute('id', '..'), node.id = '..'):
//             a chart is rendered in several places at once, so a constant id exists twice in the document; ids come from
//             React useId() or from a prop
// The check is pure (no file system access); lint-lf.mjs calls it for every chart file.
import { lineCol } from './_common.mjs'

const { parseSync } = await import('oxc-parser')

export const CHART_DIR = 'apps/web/src/ui/lf/'
export const isChartFile = (f) => f.startsWith(CHART_DIR) && /\.(ts|tsx|js|jsx|mjs)$/.test(f)

const ENTROPY_ON_MATH = new Set(['random'])
const ENTROPY_ON_CRYPTO = new Set(['getRandomValues', 'randomUUID'])
const SUBTREE_PROPS = new Set(['innerHTML', 'outerHTML'])

const propName = (m) => (m?.type === 'MemberExpression' && !m.computed ? m.property?.name : m?.type === 'MemberExpression' && m.property?.type === 'Literal' ? String(m.property.value) : undefined)
const baseName = (o) => (o?.type === 'Identifier' ? o.name : propName(o))
const isConstString = (n) => (n?.type === 'Literal' && typeof n.value === 'string') || (n?.type === 'TemplateLiteral' && (n.expressions?.length ?? 0) === 0)

/** visit every node of an oxc (ESTree-shaped) program once, depth first */
function visit(node, fn) {
  const stack = [node]
  while (stack.length) {
    const n = stack.pop()
    if (!n || typeof n !== 'object') continue
    if (Array.isArray(n)) {
      for (let i = n.length - 1; i >= 0; i--) stack.push(n[i])
      continue
    }
    if (typeof n.type === 'string') fn(n)
    for (const k of Object.keys(n)) {
      if (k === 'type' || k === 'start' || k === 'end' || k === 'range' || k === 'loc') continue
      const v = n[k]
      if (v && typeof v === 'object') stack.push(v)
    }
  }
}

/** findings of one chart file: [{ offset, kind, message }] in source order */
export function chartFindings(file, text) {
  const lang = /\.(tsx|jsx)$/.test(file) ? (file.endsWith('.tsx') ? 'tsx' : 'jsx') : file.endsWith('.ts') ? 'ts' : 'js'
  const res = parseSync(file, text, { lang })
  if (res.errors?.length) {
    const e = res.errors[0]
    return [{ offset: e.labels?.[0]?.start ?? 0, kind: 'parse', message: `cannot parse (${e.message}); LF-CHART-01 needs valid source` }]
  }
  const out = []
  const add = (n, kind, message) => out.push({ offset: n.start ?? 0, kind, message })
  visit(res.program, (n) => {
    if (n.type === 'CallExpression') {
      const p = propName(n.callee)
      const base = baseName(n.callee?.object)
      if (p && base === 'Math' && ENTROPY_ON_MATH.has(p)) add(n, 'entropy', `Math.${p}() gives different marks on every render; use rnd(i, k) from ui/lf/rnd.ts`)
      else if (p && base === 'crypto' && ENTROPY_ON_CRYPTO.has(p)) add(n, 'entropy', `crypto.${p}() in chart code; marks must be a pure function of their data and index`)
      else if (p === 'replaceChildren') add(n, 'rebuild', 'replaceChildren() rebuilds the chart subtree; update the existing marks in place')
      else if (p === 'setAttribute' && isConstString(n.arguments?.[0]) && String(n.arguments[0].value ?? n.arguments[0].quasis?.[0]?.value?.cooked) === 'id' && isConstString(n.arguments?.[1])) {
        add(n, 'fixed-id', 'constant element id; two instances of the chart would share it (use useId() or a prop)')
      }
    } else if (n.type === 'AssignmentExpression') {
      const p = propName(n.left)
      if (p && SUBTREE_PROPS.has(p)) add(n, 'rebuild', `${p} assignment rebuilds the chart subtree; update the existing marks in place`)
      else if (p === 'id' && isConstString(n.right)) add(n, 'fixed-id', 'constant element id; two instances of the chart would share it (use useId() or a prop)')
    } else if (n.type === 'JSXAttribute' && n.name?.type === 'JSXIdentifier' && n.name.name === 'id') {
      const v = n.value?.type === 'JSXExpressionContainer' ? n.value.expression : n.value
      if (isConstString(v)) add(n, 'fixed-id', `id="${v.value ?? v.quasis?.[0]?.value?.cooked ?? ''}" is a constant; two instances of the chart would share it (use useId() or a prop)`)
    }
  })
  return out.sort((a, b) => a.offset - b.offset)
}

/** Reporter adapter used by lint-lf.mjs */
export function checkChart(file, text, R) {
  if (!isChartFile(file)) return
  for (const f of chartFindings(file, text)) {
    const [l, c] = lineCol(text, f.offset)
    R.add(file, l, c, 'LF-CHART-01', f.message)
  }
}
