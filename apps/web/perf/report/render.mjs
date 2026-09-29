#!/usr/bin/env node
// render (M16-FR-071; AWR-18 §11.4 item 5): report.json -> self-contained report.html (optional report.pdf).
//   route mode (default): static server over apps/web/dist, Playwright opens /reports?src=/report.json (M15-FR-080) with the
//     local report.json routed in, waits for [data-report-ready="true"], adds the cover (brand badge 480 px, unaltered),
//     the fidelity block and the data-source footer (M16 §6.3), inlines every stylesheet and font (data URIs), drops the
//     scripts and writes report.html; --pdf prints A4 with backgrounds.
//   --plain: no browser and no route; table.log tables only (degraded mode of M16 §14 item 16), styled with the theme
//     tokens read from apps/web/src/styles/theme.css at run time (no colour literal in this file).
// Charts are static SVG; no entry animation runs (the page is rendered with reduced motion).
// Usage: node perf/report/render.mjs --run <runId|dir> [--plain] [--pdf]
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { parseArgs } from 'node:util'
import { runPath } from './build-report.mjs'

const ROOT = resolve(import.meta.dirname, '../../../..')
const WEB = resolve(ROOT, 'apps', 'web')
const LOGO = join(WEB, 'public', 'brand', 'anet-logo.svg')
const STATUS_ZH = { PASS: '通过', FAIL: '不通过', ENV_UNMET: '环境不满足', WARN: '告警', NA: '不判定', WAIVED: '已豁免' }

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c])

/** footer lines of M16 §6.3 from report.json and report.meta.json */
export function footerLines(report, meta) {
  const ds = Object.values(meta?.datasets ?? {})[0] ?? {}
  const ver = /v\d+\.\d+\.\d+/.exec(String(ds.version ?? ''))?.[0] ?? String(ds.version ?? '')
  const full = String(ds.citation ?? '')
  const author = /^[^,]+ et al\./.exec(full)?.[0]
  const venue = /\b[A-Z]{3,6} \d{4}\b/.exec(full)?.[0]
  const cite = author && venue ? `${author}, ${venue}` : full
  const city = String(report.cases.find((c) => c.world_id)?.world_id ?? 'shenzhen').toUpperCase()
  const f = meta?.fidelity ?? {}
  return [
    `数据：${ds.name ?? 'UrbanScene3D'}（${cite || 'Lin et al., ECCV 2022'}）虚拟城市采样点云 ${ver}，科研用途；许可摘要见 world.json。`,
    `坐标：示意锚点（${ds.anchor ?? 'synthetic'}），不得用于真实导航。仿真：${f.backend ?? 'Mock L1（PX4-lite）'}，机型 ${f.vehicle ?? 'p600_mid360'}`
      + `（${f.vehicle_status ?? '参数未辨识'}），结果为仿真值。${f.synthetic_source ? '含合成数据用例（不参与门禁）。' : ''}`,
    `PERF · ${city} · WEBGL2 ${String(report.env.device_class).toUpperCase()} · run ${report.run_id}`,
  ]
}

export function fidelityRows(report, meta) {
  const f = meta?.fidelity ?? {}
  return [
    ['后端', f.backend ?? 'Mock L1（PX4-lite）'], ['机型', `${f.vehicle ?? 'p600_mid360'}（${f.vehicle_status ?? '参数未辨识'}）`],
    ['仿真结果', f.simulated === false ? '实测' : '仿真值（simulated = true）'],
    ['设备能力档', `${report.env.device_class}（后端档 ${report.env.backend_tier}）`],
    ['强制档位', report.build.mode === 'test' ? '测试构建（强制开关只用于功能与配对用例）' : '无'],
  ]
}

function coverHtml(report) {
  const logo = existsSync(LOGO) ? `data:image/svg+xml;base64,${readFileSync(LOGO).toString('base64')}` : ''
  const date = /^[pb](\d{4})(\d{2})(\d{2})/.exec(report.run_id)
  return `<header data-report-cover class="rpt-cover">${logo ? `<img src="${logo}" width="480" alt="ANet" data-brand="badge">` : ''}
<h1>流畅性测试报告</h1><p class="rpt-meta">${esc(report.run_id)} · 门禁 ${esc(report.gate)} · ${date ? `${date[1]}-${date[2]}-${date[3]}` : ''} · 提交 ${esc(report.git.sha.slice(0, 12))}</p></header>`
}

function tableLog(headers, rows, numCols = new Set()) {
  return `<table class="table-log" data-lf-table><thead><tr>${headers.map((h, i) => `<th${numCols.has(i) ? ' data-num' : ''}>${esc(h)}</th>`).join('')}</tr></thead>
<tbody>${rows.map((r) => `<tr>${r.map((v, i) => `<td${numCols.has(i) ? ' data-num' : ''}>${esc(v)}</td>`).join('')}</tr>`).join('\n')}</tbody></table>`
}

const fmt = (v, d = 2) => (typeof v === 'number' && Number.isFinite(v) ? (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(d)) : '—')

/** plain degraded report: table.log only (M16 §14 item 16) */
export function plainHtml(report, meta, themeCss) {
  const worstFail = report.cases.find((c) => c.status === 'FAIL' && c.priority === 'P0') ?? report.cases.find((c) => c.status === 'FAIL')
  const gate = report.cases.map((c) => {
    const k = c.metrics.find((m) => m.threshold) ?? c.metrics[0]
    return [c.id, c.priority, STATUS_ZH[c.status] ?? c.status, k?.key ?? '—', k ? `${fmt(k.median)} ${k.unit}` : '—',
      k?.threshold ? `${k.threshold.op} ${fmt(k.threshold.value)}` : '—']
  })
  const all = report.cases.flatMap((c) => c.metrics.map((m) => [c.id, m.key, m.unit, fmt(m.median), m.runs.map((x) => fmt(x, 1)).join(' '),
    STATUS_ZH[m.status] ?? m.status]))
  const s = report.summary
  const body = `${coverHtml(report)}
<section data-figure="report-glance" class="rpt-glance"><h2>速览</h2>${tableLog(['P0 通过', 'P1 通过', 'P1 通过率', '回归', '豁免'],
    [[`${s.p0_pass} / ${s.p0_total}`, `${s.p1_pass} / ${s.p1_total}`, `${fmt(s.p1_pass_rate_pct, 1)} %`, String(s.regressions?.length ?? 0),
      String(s.waivers?.length ?? 0)]], new Set([0, 1, 2, 3, 4]))}</section>
<section data-figure="report-gate"><h2>门禁结论</h2>${tableLog(['用例', '优先级', '状态', '关键指标', '中位数', '阈值'], gate, new Set([4, 5]))}
${worstFail ? `<p class="rpt-hot" data-hot>最严重的不通过项：${esc(worstFail.id)}</p>` : ''}</section>
<section data-figure="report-env"><h2>环境</h2>${tableLog(['CPU', '核数', 'Chrome', '标志', '设备能力档'],
    [[report.env.cpu_model, String(report.env.cores), report.env.chrome ?? '', (report.env.flags ?? []).join(' '), report.env.device_class]], new Set([1]))}</section>
<section data-report-fidelity data-figure="report-fidelity"><h2>保真度声明</h2>${tableLog(['项', '内容'], fidelityRows(report, meta))}</section>
<section data-figure="report-appendix"><h2>附录：全部指标</h2>${tableLog(['用例', '指标', '单位', '中位数', '各次', '状态'], all, new Set([3, 4]))}</section>
<footer data-report-footer class="rpt-footer">${footerLines(report, meta).map((l) => `<p>${esc(l)}</p>`).join('')}</footer>`
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>流畅性测试报告 ${esc(report.run_id)}</title>
<style>${themeCss}
body{margin:0;padding:32px 40px;background:var(--background);color:var(--foreground);font:14px/1.5 Inter,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;font-variant-numeric:tabular-nums lining-nums}
h1{font-size:28px;margin:12px 0 4px} h2{font-size:16px;margin:28px 0 8px}
.rpt-meta,.rpt-footer{color:var(--muted-foreground);font-size:12px}
.table-log{border-collapse:collapse;width:100%;font-size:12px}
.table-log th{text-align:left;font-weight:600;border-bottom:1px solid var(--border);padding:4px 8px}
.table-log td{padding:4px 8px;border-bottom:1px dotted var(--border);background:transparent}
.table-log [data-num]{text-align:right}
.rpt-hot{color:var(--brand-text);font-weight:600}
.rpt-footer{margin-top:40px;border-top:1px solid var(--border);padding-top:12px}
</style></head><body data-view="report" data-report-ready="true">${body}</body></html>`
}

function themeCss() {
  const p = join(WEB, 'src', 'styles', 'theme.css')
  if (!existsSync(p)) return ''
  // keep only the custom-property blocks (:root and the light theme); Tailwind directives are not CSS
  return readFileSync(p, 'utf8').split('\n').filter((l) => !/^\s*@(import|theme|plugin|custom-variant|source|utility)\b/.test(l)).join('\n')
}

async function routeHtml(dir, report, meta, pdf) {
  const { chromium } = await import('@playwright/test')
  const { startStatic } = await import('../harness/backend.mjs')
  const { FLAGS } = await import('../harness/browser.mjs')
  const { CHROME } = await import('../harness/protocol.mjs')
  const json = readFileSync(join(dir, 'report.json'))
  const srv = await startStatic({ routes: { '/report.json': (_req, res) => res.writeHead(200, { 'Content-Type': 'application/json',
    'Cross-Origin-Resource-Policy': 'same-origin' }).end(json) } })
  const browser = await chromium.launch({ executablePath: CHROME, args: FLAGS.C1 })
  try {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1, reducedMotion: 'reduce' })
    const page = await ctx.newPage()
    await page.goto(`${srv.base}/reports?src=/report.json`)
    await page.waitForSelector('[data-report-ready="true"]', { timeout: 60_000 })
    const cover = coverHtml(report)
    const fid = `<section data-report-fidelity data-figure="report-fidelity"><h2>保真度声明</h2>${tableLog(['项', '内容'], fidelityRows(report, meta))}</section>`
    const foot = `<footer data-report-footer class="rpt-footer">${footerLines(report, meta).map((l) => `<p>${esc(l)}</p>`).join('')}</footer>`
    const slots = { cover, fid, foot, rows: fidelityRows(report, meta), lines: footerLines(report, meta) }
    const html = await page.evaluate(async ({ cover, fid, foot, rows, lines }) => {
      const root = document.querySelector('[data-view="report"]') ?? document.body
      // static document: only the report root stays in <body> (no boot mask, 3D viewport, portals, toasts or app shell);
      // the root flows in the page so that the whole report scrolls and prints
      if (root !== document.body) document.body.replaceChildren(root)
      for (const el of [...document.querySelectorAll('#boot-mask, [data-viewport], canvas')]) el.remove()
      for (let el = root; el instanceof HTMLElement; el = el.parentElement) {
        el.style.position = 'static'
        el.style.overflow = 'visible'
        el.style.height = 'auto'
        el.style.inset = 'auto'
      }
      // the report route (M15 ReportCover) may already render the cover, fidelity block or footer: add only what is missing,
      // styled with the classes of the report's own card and table (no new class names that the built CSS lacks)
      if (!root.querySelector('[data-report-cover]')) root.insertAdjacentHTML('afterbegin', cover)
      const cards = [...root.querySelectorAll('[data-slot="card"]')]
      // the last card of the report (appendix table) is the template: same card, header, table classes
      const last = cards.findLast((c) => c.querySelector('[data-slot="table"]')) ?? cards.at(-1)
      const cls = (sel) => (last?.querySelector(sel) ?? root.querySelector(sel))?.className ?? ''
      const mk = (tag, className, text) => {
        const el = document.createElement(tag)
        if (className) el.className = className
        if (text !== undefined) el.textContent = text
        return el
      }
      if (!root.querySelector('[data-report-fidelity]')) {
        if (last) {
          const sec = mk('section', last.className)
          sec.dataset.slot = 'card'
          sec.setAttribute('data-report-fidelity', '')
          sec.dataset.figure = 'report-fidelity'
          const hd = mk('div', cls('[data-slot="card-header"]'))
          hd.append(mk('div', cls('[data-slot="card-title"]'), '保真度声明'))
          const ct = mk('div', cls('[data-slot="card-content"]'))
          const tb = mk('table', cls('[data-slot="table"]'))
          const thead = mk('thead', cls('[data-slot="table-header"]'))
          const hr = mk('tr', cls('[data-slot="table-row"]'))
          for (const h of ['项', '内容']) hr.append(mk('th', cls('[data-slot="table-head"]'), h))
          thead.append(hr)
          const tbody = mk('tbody', cls('[data-slot="table-body"]'))
          for (const r of rows) {
            const tr = mk('tr', cls('[data-slot="table-row"]'))
            for (const v of r) tr.append(mk('td', cls('[data-slot="table-cell"]'), v))
            tbody.append(tr)
          }
          tb.append(thead, tbody)
          ct.append(tb)
          sec.append(hd, ct)
          cards.at(-1).after(sec)
        } else root.insertAdjacentHTML('beforeend', fid)
      }
      if (!root.querySelector('[data-report-footer]')) {
        if (last) {
          const ft = mk('footer', cls('[data-slot="card-description"]'))
          ft.setAttribute('data-report-footer', '')
          ft.style.padding = '8px 0 24px'
          for (const l of lines) ft.append(mk('p', '', l))
          const anchor = root.querySelector('[data-report-fidelity]') ?? cards.at(-1)
          anchor.after(ft)
        } else root.insertAdjacentHTML('beforeend', foot)
      }
      let css = ''
      for (const sh of [...document.styleSheets]) {
        try {
          css += [...sh.cssRules].map((r) => r.cssText).join('\n')
        } catch {
          // cross-origin sheet (none expected)
        }
      }
      const urls = [...new Set([...css.matchAll(/url\((['"]?)([^'")]+\.woff2)\1\)/g)].map((m) => m[2]))]
      for (const u of urls) {
        const b = new Uint8Array(await (await fetch(u)).arrayBuffer())
        let s = ''
        for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000))
        css = css.split(u).join(`data:font/woff2;base64,${btoa(s)}`)
      }
      for (const el of [...document.querySelectorAll('script, link[rel="stylesheet"], link[rel="modulepreload"], style')]) el.remove()
      const st = document.createElement('style')
      st.textContent = css
      document.head.appendChild(st)
      for (const img of [...document.querySelectorAll('img')]) {
        if (img.src.startsWith('data:')) continue
        const b = new Uint8Array(await (await fetch(img.src)).arrayBuffer())
        let s = ''
        for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000))
        img.src = `data:${img.src.endsWith('.svg') ? 'image/svg+xml' : 'image/png'};base64,${btoa(s)}`
      }
      return '<!doctype html>\n' + document.documentElement.outerHTML
    }, slots)
    writeFileSync(join(dir, 'report.html'), html)
    if (pdf) await page.pdf({ path: join(dir, 'report.pdf'), format: 'A4', printBackground: true })
  } finally {
    await browser.close()
    await srv.static.close()
  }
}

export async function render(dir, { plain = false, pdf = false } = {}) {
  const report = JSON.parse(readFileSync(join(dir, 'report.json'), 'utf8'))
  const meta = existsSync(join(dir, 'report.meta.json')) ? JSON.parse(readFileSync(join(dir, 'report.meta.json'), 'utf8')) : {}
  const routeOk = existsSync(join(WEB, 'dist', 'index.html'))
  if (plain || !routeOk) writeFileSync(join(dir, 'report.html'), plainHtml(report, meta, themeCss()))
  else await routeHtml(dir, report, meta, pdf)
  return join(dir, 'report.html')
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const { values } = parseArgs({ options: { run: { type: 'string' }, plain: { type: 'boolean' }, pdf: { type: 'boolean' } } })
  if (!values.run) {
    console.error('usage: render.mjs --run <runId|dir> [--plain] [--pdf]')
    process.exit(2)
  }
  const t0 = Date.now()
  render(runPath(values.run), { plain: values.plain, pdf: values.pdf }).then((p) => console.log(`${p} (${((Date.now() - t0) / 1000).toFixed(1)} s)`),
    (e) => {
      console.error(e?.stack ?? e)
      process.exit(1)
    })
}
