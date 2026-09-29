#!/usr/bin/env node
// M15 shadcn installer (M15-FR-081, FR-082, FR-085; M15 §9.5; ADR-028, ADR-037).
//
//   node tools/shadcn/install.mjs            init (only when components.json is missing) + add the 44 D1 components + postadd
//   node tools/shadcn/install.mjs --online   use https://ui.shadcn.com instead of the local registry mirror (L2)
//   node tools/shadcn/install.mjs add x y    add extra items (must not be on the forbidden list) + postadd
//
// Rules enforced here:
//   * the registry is served from tools/shadcn/registry-mirror (L2 offline mirror, d04 §3.9) unless --online;
//   * the CLI never runs an npm install: every dependency the items ask for is already declared in apps/web/package.json;
//     lucide-react is shimmed into package.json for the duration of the CLI run only (ADR-030 forbids it), so the
//     CLI's "skip installed" rule skips it; apps/web/package.json and package-lock.json are restored byte for byte;
//   * files owned by other modules (tsconfig.json, vite.config.ts) and the M15 sources the CLI would overwrite
//     (styles/index.css, lib/utils.ts, components.json) are restored after the run;
//   * forbidden components (chart, sonner, drawer, carousel, calendar, navigation-menu, pagination, input-otp) are refused;
//   * postadd.mjs (codemods, dependency check, tsc) runs after every add.
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, normalize, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..', '..')
const WEB = join(ROOT, 'apps', 'web')
const MIRROR = join(HERE, 'registry-mirror')

export const D1_COMPONENTS = [
  'sidebar', 'sheet', 'tooltip', 'skeleton', 'input', 'separator', 'resizable', 'card', 'tabs', 'scroll-area', 'collapsible',
  'accordion', 'dialog', 'alert-dialog', 'popover', 'hover-card', 'menubar', 'dropdown-menu', 'context-menu', 'command', 'breadcrumb',
  'button', 'button-group', 'toggle', 'toggle-group', 'slider', 'input-group', 'field', 'label', 'checkbox', 'radio-group', 'switch',
  'select', 'native-select', 'combobox', 'table', 'badge', 'kbd', 'item', 'empty', 'progress', 'spinner', 'alert', 'toast',
]
export const FORBIDDEN = ['chart', 'sonner', 'drawer', 'carousel', 'calendar', 'navigation-menu', 'pagination', 'input-otp']

const PROTECTED = ['package.json', 'tsconfig.json', 'vite.config.ts', 'src/styles/index.css', 'src/lib/utils.ts', 'components.json']

function serveMirror() {
  return new Promise((resolve) => {
    const srv = createServer((req, res) => {
      const url = new URL(req.url ?? '/', 'http://x')
      let rel = url.pathname === '/init' ? 'init' : normalize(url.pathname).replace(/^\/+/, '')
      if (rel.includes('..')) rel = ''
      const file = join(MIRROR, rel)
      if (!rel || !existsSync(file)) {
        res.writeHead(404).end()
        return
      }
      res.writeHead(200, { 'content-type': 'application/json' }).end(readFileSync(file))
    })
    srv.listen(0, '127.0.0.1', () => resolve(srv))
  })
}

function backup() {
  const saved = new Map()
  for (const f of PROTECTED) {
    const p = join(WEB, f)
    saved.set(f, existsSync(p) ? readFileSync(p) : null)
  }
  const lock = join(ROOT, 'package-lock.json')
  saved.set('../../package-lock.json', readFileSync(lock))
  return saved
}

function restore(saved, keep = new Set()) {
  for (const [f, buf] of saved) {
    if (keep.has(f) || buf === null) continue
    writeFileSync(join(WEB, f), buf)
  }
}

function shimLucideReact() {
  // ADR-030: lucide-react must never be installed. The CLI skips dependencies that are already declared, so declare it
  // for the duration of the run only; the original package.json is restored afterwards.
  const p = join(WEB, 'package.json')
  const pj = JSON.parse(readFileSync(p, 'utf8'))
  pj.devDependencies = { ...pj.devDependencies, 'lucide-react': '1.48.0' }
  writeFileSync(p, JSON.stringify(pj, null, 2) + '\n')
}

function cli(args, env) {
  // async spawn: the registry mirror is served from this process, so the event loop must stay free (spawnSync deadlocks);
  // stdin is closed: the CLI would otherwise wait on prompts that `-y` does not cover
  const bin = join(ROOT, 'node_modules', '.bin', 'shadcn')
  return new Promise((ok, fail) => {
    const p = spawn(bin, [...args, '-c', WEB], { cwd: WEB, stdio: ['ignore', 'inherit', 'inherit'], env: { ...process.env, ...env } })
    p.on('error', fail)
    p.on('exit', (code) => (code === 0 ? ok() : fail(new Error(`shadcn ${args.slice(0, 2).join(' ')} failed with ${code}`))))
  })
}

async function main() {
  const argv = process.argv.slice(2)
  const online = argv.includes('--online')
  const extra = argv[0] === 'add' ? argv.slice(1).filter((a) => !a.startsWith('--')) : []
  const items = extra.length ? extra : D1_COMPONENTS
  const bad = items.filter((i) => FORBIDDEN.includes(i))
  if (bad.length) throw new Error(`forbidden shadcn components (M15-FR-081): ${bad.join(', ')}`)

  const srv = online ? null : await serveMirror()
  const env = srv ? { REGISTRY_URL: `http://127.0.0.1:${srv.address().port}/r` } : {}
  const saved = backup()
  const hadComponentsJson = saved.get('components.json') !== null
  const onSignal = (sig) => {
    restore(saved, new Set(['components.json']))
    console.error(`install: ${sig}, protected files restored`)
    process.exit(130)
  }
  process.once('SIGINT', onSignal)
  process.once('SIGTERM', onSignal)
  try {
    shimLucideReact()
    if (!hadComponentsJson) {
      // first run: init with the project registry:base, then migrate the aliases to src/ui (ADR-037)
      await cli(['init', join('..', '..', 'tools', 'shadcn', 'anet-base.json'), '-y', '--no-monorepo', '--no-reinstall'], env)
    }
    writeFileSync(join(WEB, 'components.json'), readFileSync(join(HERE, 'components.json')))
    shimLucideReact()
    await cli(['add', ...items, '-y', '-o'], env)
  } finally {
    restore(saved, new Set(['components.json']))
    if (!existsSync(join(WEB, 'components.json'))) writeFileSync(join(WEB, 'components.json'), readFileSync(join(HERE, 'components.json')))
    srv?.close()
  }
  const r = spawnSync(process.execPath, [join(HERE, 'postadd.mjs'), ...argv.filter((a) => a === '--no-tsc')], { cwd: ROOT, stdio: 'inherit' })
  process.exit(r.status ?? 1)
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch((e) => {
    console.error(`install: ${e.message}`)
    process.exit(1)
  })
}
