#!/usr/bin/env node
// RAW-01 (ADR-028; AWR-18 §13.1): outside ui/components/ui/**, no native interactive elements (button, input, select,
// textarea, dialog), no ARIA widget roles on plain elements, no document.createElement of those tags, and no home-made
// overlay positioning (@floating-ui imports, react-dom createPortal). UI components come from shadcn (base-mira) only.
// Whitelist: the viewport canvas host and the DOM ViewCube (apps/web/src/viewport/**/*ViewCube*).
// Parser: oxc-parser (TSX aware). Scope: apps/web/src/**/*.{tsx,jsx,ts}.
// Usage: node tools/lint/no-raw-controls.mjs [files...]
import { Reporter, WEB_SRC, inShadcn, isMain, lineCol, readText, run, walk } from './_common.mjs'

const TAGS = new Set(['button', 'input', 'select', 'textarea', 'dialog'])
const ROLES = new Set(['button', 'dialog', 'alertdialog', 'menu', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'menubar', 'listbox', 'option',
  'combobox', 'slider', 'switch', 'checkbox', 'radio', 'radiogroup', 'tab', 'tablist', 'tabpanel', 'tooltip', 'textbox', 'spinbutton', 'searchbox'])
const WHITELIST = [/^apps\/web\/src\/viewport\/.*ViewCube[^/]*$/i]

let oxc = null
async function parser() {
  if (!oxc) oxc = await import('oxc-parser')
  return oxc
}

function walkAst(node, visit) {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) {
    for (const n of node) walkAst(n, visit)
    return
  }
  if (typeof node.type === 'string') visit(node)
  for (const k in node) {
    if (k === 'start' || k === 'end' || k === 'type') continue
    const v = node[k]
    if (v && typeof v === 'object') walkAst(v, visit)
  }
}

export async function checkFile(f, text, R) {
  if (!f.startsWith(WEB_SRC + '/') || !/\.(tsx|jsx|ts)$/.test(f) || inShadcn(f) || WHITELIST.some((re) => re.test(f))) return
  const { parseSync } = await parser()
  const res = parseSync(f, text, { lang: f.endsWith('.ts') ? 'ts' : 'tsx' })
  if (res.errors?.length) {
    const e = res.errors[0]
    const off = e.labels?.[0]?.start ?? 0
    const [l, c] = lineCol(text, off)
    R.add(f, l, c, 'RAW-01', `could not parse (${e.message}); fix the syntax so RAW-01 can run`)
    return
  }
  const at = (n) => lineCol(text, n.start ?? 0)
  walkAst(res.program, (n) => {
    if (n.type === 'JSXOpeningElement' && n.name?.type === 'JSXIdentifier') {
      const tag = n.name.name
      if (TAGS.has(tag)) {
        const [l, c] = at(n)
        R.add(f, l, c, 'RAW-01', `native <${tag}> outside ui/components/ui; use the shadcn component`)
      }
      for (const a of n.attributes ?? []) {
        if (a.type === 'JSXAttribute' && a.name?.name === 'role' && a.value?.type === 'Literal' && ROLES.has(String(a.value.value))) {
          const [l, c] = at(a)
          R.add(f, l, c, 'RAW-01', `role="${a.value.value}" on a plain element; use the shadcn component with that role`)
        }
      }
    } else if (n.type === 'CallExpression' && n.callee?.type === 'MemberExpression' && n.callee.property?.name === 'createElement' &&
      n.callee.object?.name === 'document' && n.arguments?.[0]?.type === 'Literal' && TAGS.has(String(n.arguments[0].value))) {
      const [l, c] = at(n)
      R.add(f, l, c, 'RAW-01', `document.createElement('${n.arguments[0].value}') outside ui/components/ui`)
    } else if (n.type === 'ImportDeclaration') {
      const src = String(n.source?.value ?? '')
      const portal = src === 'react-dom' && (n.specifiers ?? []).some((s) => s.imported?.name === 'createPortal')
      if (src.startsWith('@floating-ui/') || portal) {
        const [l, c] = at(n)
        R.add(f, l, c, 'RAW-01', `${portal ? 'createPortal' : src}: self-made overlay positioning; use shadcn Popover, Tooltip or Dialog`)
      }
    }
  })
}

if (isMain(import.meta.url)) {
  await run('no-raw-controls', async () => {
    const explicit = process.argv.slice(2).filter((a) => !a.startsWith('--'))
    const files = explicit.length ? explicit : walk(WEB_SRC, (r) => /\.(tsx|jsx|ts)$/.test(r))
    const R = new Reporter('no-raw-controls')
    if (!files.length) R.note(`${WEB_SRC} has no TSX sources yet`)
    for (const f of files) {
      const t = readText(f)
      if (t !== null) await checkFile(f, t, R)
    }
    R.finish({ files: files.length })
  })
}
