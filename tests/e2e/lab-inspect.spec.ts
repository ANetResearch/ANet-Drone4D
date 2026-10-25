// Playwright `lab/inspect`（R07；D3-AC-25 内部工作区部分；AWR-14 §21.9；AWR-17 §18.10.2、§18.10.3；AWR-05 §6.1.6）。
// 被测栈（tests/e2e/lab_stack.py serve-internal，同源）：内部测试构建（VITE_AWR_TEST_SWITCHES=1）+ 真实 bench-api（会话 Cookie、BN-57
// 下发 d3_rr）+ 真实 rerun-proxy + 两个真实 Rerun 0.38.1 catalog 服务；Viewer 为真实 @rerun-io/web-viewer 0.38.1（Wasm）。
//   1 鉴权：未登录访问跳转 /lab/login?next=，登录后回到原页面；d3_session 与 d3_rr 均为 HttpOnly、SameSite=Strict，d3_rr 只有
//     `/rerun/` 与 `/rerun.cloud.v1alpha1.RerunCloudService` 两条路径、10 min；全部请求 URL 不含令牌；Viewer 的 gRPC-web 请求携带
//     d3_rr 并返回 200，bench-api 请求不携带 d3_rr；无 Cookie 访问 rerun-proxy 401；首帧可见时刻（catalog 按需取数）；
//   2 100 次往复：lab 时间线以真实鼠标拖动（12 步），Viewer 侧以 set_current_time 连续改时刻（8 步，等同 Viewer 时间面板拖动）；
//     每次静置后两个方向误差 ≤ 1 帧（100 ms 仿真时间）；lab 拖动期间回写 0 次，Viewer 侧变化期间推送 0 次；结束后静置 3 s 计数不变；
//     Viewer 收到的 set_current_time 次数 = 推送次数 = 回声次数，Viewer 派发的 time_update 次数 = 回声 + Viewer 侧变化 + 打开前排队；
//   3 Viewer 播放时 lab 播放头跟随并显示"由数据检视驱动"，时间线操作收回权威；
//   4 选择联动：场景中点选机体 → Viewer 选中对应实体（URL 片段 #selection=，catalog URI）；Viewer 中的选择 → 场景描边；
//   5 评测 catalog：评测员切换后场景出现真值演员，演员定位到 /truth/actors/<id>；研究者请求 catalog=eval 回到分发并提示；
//   6 外壳：设备档不满足时 UNSUPPORTED 与原生 Viewer 命令（不含令牌）；未注册运行 ERROR（305）与重试；各状态外壳文案过 copy-lint。
// Tier S（本机 SwiftShader）以 `?inspect=force` 启用面板，首帧数字只作开发参考；AWR_LAB_TIER=B 在 Tier B 参考设备上按 ≤ 5 s 断言。
import { join } from 'node:path'
import { expect, request, test, type BrowserContext, type Page } from '@playwright/test'
import {
  credentialIn, emulateHardwareRenderer, FIRST_FRAME_MAX_MS, FRAME_NS, measureFirstFrame, OUT_DIR, remoteBrowser, shellCopyFindings, startInternalStack,
  texturedPoint, TIER, traceInspectStates, writeResult, type CopyCheck, type InternalStack,
} from './labStack'

test.describe.configure({ mode: 'default' })

let st: InternalStack
let ctx: BrowserContext
const TRIPS = Number(process.env.AWR_R07_TRIPS ?? 100)
const REMOTE = !!process.env.AWR_LAB_BASE
// 测试构建以 ?inspect=force 跳过设备档判定；已部署的生产构建无该开关，本机改以渲染器字符串仿真 Tier B
const FORCE = TIER === 'S' && !REMOTE ? '?inspect=force' : ''
const RR_PATHS = ['/rerun/', '/rerun.cloud.v1alpha1.RerunCloudService']

test.beforeAll(async ({ browser }) => {
  test.setTimeout(300_000)
  st = await startInternalStack()
  const b = (await remoteBrowser()) ?? browser
  ctx = await b.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1, ignoreHTTPSErrors: REMOTE })
  if (REMOTE && TIER === 'S') await emulateHardwareRenderer(ctx)
})
/** 已部署工作区为生产构建（无测试钩子），只运行第 1 个用例 */
const needsLocal = () => test.skip(REMOTE, 'needs the local test stack (test hooks, orphan run)')
test.afterAll(async () => {
  await ctx?.close()
  await st?.close()
})

const inspectPath = (rid: string, q = '') => `/lab/runs/${encodeURIComponent(rid)}/inspect${FORCE}${q ? (FORCE ? '&' : '?') + q : ''}`

async function login(page: Page, role: 'researcher' | 'evaluator', path: string): Promise<void> {
  await page.context().clearCookies()
  await page.goto(st.url + path)
  await page.waitForURL(/\/lab\/login\?next=/, { timeout: 30_000 })
  // next 为原页面（页面在跳转前可能已把播放头写回查询参数 t）
  expect(decodeURIComponent(new URL(page.url()).searchParams.get('next') ?? '').startsWith(path)).toBe(true)
  await page.fill('#lab-token', st.tokens[role])
  await page.click('[data-view="lab-login"] button[type="submit"]')
  await page.waitForURL((u) => u.pathname === path.split('?')[0], { timeout: 30_000 })
}

/** 测试钩子：controller（window.__awrInspect）、其 Viewer 适配与同步器；在页面内安装 Viewer 侧计数 */
async function instrument(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    const c = (window as unknown as { __awrInspect?: { rerunSync: unknown; viewer: unknown } }).__awrInspect
    return !!c && !!c.rerunSync && !!c.viewer
  }, null, { timeout: 30_000 })
  await page.evaluate(() => {
    type V = { set_current_time: (r: string, tl: string, t: number) => void; on: (e: string, cb: (x: { items?: unknown[] }) => void) => () => void }
    const w = window as unknown as { __awrInspect: { viewer: V }; __r07: { setCalls: number; tu: number; sel: string[][]; lastTuAt: number; pushAt: number[] } }
    const v = w.__awrInspect.viewer
    w.__r07 = { setCalls: 0, tu: 0, sel: [], lastTuAt: performance.now(), pushAt: [] }
    const orig = v.set_current_time.bind(v)
    v.set_current_time = (r, tl, t) => {
      w.__r07.setCalls++
      w.__r07.pushAt.push(performance.now())
      orig(r, tl, t)
    }
    v.on('time_update', () => {
      w.__r07.tu++
      w.__r07.lastTuAt = performance.now()
    })
    v.on('selection_change', (e) => {
      w.__r07.sel.push(((e.items ?? []) as { entity_path?: string }[]).map((i) => i.entity_path ?? ''))
    })
  })
}

interface Probe {
  lab: number
  viewer: number
  viewerKnown: number | null
  stats: { pushed: number; echoes: number; applied: number; coalesced: number; reclaimed: number; stale: number }
  setCalls: number
  tu: number
  source: string
}
const probe = (page: Page): Promise<Probe> => page.evaluate(() => {
  type S = { stats(): Probe['stats']; viewerTimeNs: number | null; source: string; recordingId: string; playhead: { get(): { tS: number } } }
  const w = window as unknown as { __awrInspect: { rerunSync: S; viewer: { get_current_time(r: string, tl: string): number } }; __r07: { setCalls: number; tu: number } }
  const s = w.__awrInspect.rerunSync
  return {
    lab: Math.round(s.playhead.get().tS * 1e9), viewer: w.__awrInspect.viewer.get_current_time(s.recordingId, 'sim_time'), viewerKnown: s.viewerTimeNs,
    stats: s.stats(), setCalls: w.__r07.setCalls, tu: w.__r07.tu, source: s.source,
  }
})

/**
 * 等两侧静置：同步器的推送都已收到回声、Viewer 已回报至少 `tuAtLeast` 条 time_update，且连续 600 ms 无新事件（最长 30 s）。
 * 软件渲染下 Viewer 在下一帧（约 1 s）才应用 set_current_time 并回报，只看"无新事件"会把迟到的回报算进下一阶段。
 */
async function settle(page: Page, tuAtLeast = 0): Promise<void> {
  try {
    await page.waitForFunction((n) => {
      const w = window as unknown as { __r07: { lastTuAt: number; tu: number }; __awrInspect: { rerunSync: { stats(): { pushed: number; echoes: number } } } }
      const st = w.__awrInspect.rerunSync.stats()
      return st.echoes >= st.pushed && w.__r07.tu >= n && performance.now() - w.__r07.lastTuAt > 600
    }, tuAtLeast, { timeout: 30_000, polling: 100 })
  } catch (e) {
    throw new Error(`not settled (tu ≥ ${tuAtLeast}): ${JSON.stringify(await probe(page))}`, { cause: e })
  }
}

/**
 * 等面板 READY。Tier S（软件渲染）下 Viewer 首帧期间主线程阻塞数十秒，控制器的 10 s 打开超时（§21.9.2）可能先于 recording_open
 * 触发而进入 ERROR(timeout)；此时按用户操作点击重试（已加载的 Viewer 不重新加载），最多 2 次。Tier B 不重试。返回重试次数。
 */
async function waitReady(page: Page): Promise<number> {
  const panel = page.locator('[data-inspect-panel]')
  for (let retry = 0; ; retry++) {
    await expect(panel).toHaveAttribute('data-inspect-state', /^(ready|error)$/, { timeout: 180_000 })
    if ((await panel.getAttribute('data-inspect-state')) === 'ready') return retry
    const kind = await page.locator('[data-inspect-error]').getAttribute('data-inspect-error')
    if (TIER === 'B' || kind !== 'timeout' || retry >= 2) throw new Error(`inspect panel ERROR(${kind}) after ${retry} retries`)
    await page.click('[data-inspect-retry]')
    await expect(panel).not.toHaveAttribute('data-inspect-state', 'error', { timeout: 10_000 })
  }
}

async function openReady(page: Page, role: 'researcher' | 'evaluator', q = ''): Promise<void> {
  const path = inspectPath(st.runId, q)
  await login(page, role, path)
  await waitReady(page)
  await instrument(page)
  await settle(page)
}

test('鉴权与 Cookie：登录跳转、d3_rr 路径限定、URL 不含令牌；catalog 首帧', async () => {
  test.setTimeout(400_000)
  const page = await ctx.newPage()
  const reqs: { url: string; method: string; cookie: string; status: number | null }[] = []
  page.on('response', (resp) => {
    const r = resp.request()
    void r.allHeaders().then((h) => reqs.push({ url: r.url(), method: r.method(), cookie: h.cookie ?? '', status: resp.status() }))
  })
  // gRPC-web 流式响应读完尾帧后由客户端取消，Chrome 另报一次 requestfailed（net::ERR_ABORTED）；只记录，不计入请求
  const failed: string[] = []
  page.on('requestfailed', (r) => failed.push(`${r.failure()?.errorText ?? ''} ${r.url()}`))
  await traceInspectStates(page)
  const path = inspectPath(st.runId)
  await login(page, 'researcher', path)
  // 首帧：已登录状态下重新导航，起点为导航开始（performance.now = 0）
  await page.goto(st.url + path)
  const retries = await waitReady(page)
  const ff = await measureFirstFrame(page, 0, { shots: join(OUT_DIR, 'lab-inspect-first-frame') })
  expect(ff.at.ready, JSON.stringify(ff)).toBeGreaterThan(0)
  const cookies = await ctx.cookies()
  const sess = cookies.filter((c) => c.name === 'd3_session')
  expect(sess).toHaveLength(1)
  expect(sess[0]).toMatchObject({ httpOnly: true, sameSite: 'Strict', path: '/' })
  const rr = cookies.filter((c) => c.name === 'd3_rr')
  expect(rr.map((c) => c.path).sort()).toEqual([...RR_PATHS].sort())
  for (const c of [...rr, ...sess]) expect(c.secure, `${c.name} Secure`).toBe(st.url.startsWith('https:'))
  for (const c of rr) {
    expect(c).toMatchObject({ httpOnly: true, sameSite: 'Strict' })
    expect(Math.abs(c.expires - Date.now() / 1000 - 600)).toBeLessThan(120)
  }
  await page.waitForTimeout(1000)
  const secrets = [st.tokens.researcher, st.tokens.evaluator, ...rr.map((c) => c.value), ...sess.map((c) => c.value)]
  const leaked = reqs.filter((r) => credentialIn(r.url, secrets))
  expect(leaked, leaked.map((r) => r.url).join('\n')).toEqual([])
  const grpc = reqs.filter((r) => new URL(r.url).pathname.startsWith('/rerun.cloud.v1alpha1.RerunCloudService/'))
  expect(grpc.length).toBeGreaterThan(0)
  for (const r of grpc) {
    expect(r.cookie, r.url).toContain('d3_rr=')
    expect(r.status, r.url).toBe(200)
  }
  const api = reqs.filter((r) => new URL(r.url).pathname.startsWith('/api/bench/'))
  expect(api.length).toBeGreaterThan(0)
  for (const r of api) expect(r.cookie, r.url).not.toContain('d3_rr=')
  const vu = api.filter((r) => /\/viewer_url\?/.test(r.url))
  expect(vu.length).toBeGreaterThan(0)
  // 无 Cookie 的请求被 rerun-proxy 拒绝（401 302）；带会话 Cookie 而无 d3_rr 同样拒绝
  const rq = await request.newContext({ ignoreHTTPSErrors: REMOTE })
  const grpcWeb = { 'content-type': 'application/grpc-web+proto', 'x-grpc-web': '1', origin: st.url }
  const bare = await rq.post(`${st.url}/rerun.cloud.v1alpha1.RerunCloudService/FindEntries`, { headers: grpcWeb, data: Buffer.alloc(5) })
  expect(bare.status()).toBe(401)
  const withSession = await rq.post(`${st.url}/rerun.cloud.v1alpha1.RerunCloudService/FindEntries`, {
    headers: { ...grpcWeb, cookie: `d3_session=${sess[0]!.value}` }, data: Buffer.alloc(5),
  })
  expect(withSession.status()).toBe(401)
  await rq.dispose()
  const out = writeResult('lab-inspect-first-frame', {
    run_id: st.runId, real_data: st.real, first_frame: ff, open_timeout_retries: retries, grpc_requests: grpc.length, api_requests: api.length, requests_total: reqs.length,
    grpc_rpcs: [...new Set(grpc.map((r) => new URL(r.url).pathname.split('/').pop()))], failed,
    offsite: reqs.filter((r) => !r.url.startsWith(st.url)).map((r) => r.url),
    note: TIER === 'S' ? 'Tier S（SwiftShader，?inspect=force）开发参考，不作正式值' : 'Tier B 参考设备',
  })
  test.info().annotations.push({ type: 'first-frame', description: `${JSON.stringify(ff.at)} visible=${ff.visibleMs} ms → ${out}` })
  expect(ff.visibleMs, 'Viewer 画面出现录制内容').not.toBeNull()
  if (TIER === 'B') expect(ff.visibleMs!).toBeLessThanOrEqual(FIRST_FRAME_MAX_MS)
  await page.close()
})

test(`${TRIPS} 次往复拖动：两方向误差 ≤ 1 帧、无回环、双方事件计数一致`, async () => {
  test.setTimeout(Math.max(600_000, TRIPS * 30_000))
  needsLocal()
  const page = await ctx.newPage()
  await openReady(page, 'researcher')
  const slider = page.locator('[data-inspect-timeline] [data-slot="slider"]').first()
  const box = (await slider.boundingBox())!
  const durS = await page.evaluate(() => {
    const m = /\/ (\d+):(\d+\.\d)/.exec(document.querySelector('[data-inspect-timeline]')?.textContent ?? '')
    return m ? Number(m[1]) * 60 + Number(m[2]) : 0
  })
  expect(durS).toBeGreaterThan(5)
  const xOf = (tS: number) => box.x + 8 + ((box.width - 16) * tS) / durS
  const thumbX = async () => {
    const b = await page.locator('[data-inspect-timeline] [data-slot="slider-thumb"]').first().boundingBox()
    return b ? b.x + b.width / 2 : xOf((await probe(page)).lab / 1e9)
  }
  const y = box.y + box.height / 2
  let seed = 7
  const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32)
  const p0 = await probe(page)
  const trips: { trip: number; errLabNs: number; errViewerNs: number; appliedDuringLab: number; pushedDuringViewer: number }[] = []
  let viewerCalls = 0
  let minPushGapMs = Infinity
  for (let trip = 0; trip < TRIPS; trip++) {
    // lab → Viewer：真实鼠标拖动 lab 时间线滑块（按下、12 步移动、释放）
    const a = await probe(page)
    const nPush0 = await page.evaluate(() => (window as unknown as { __r07: { pushAt: number[] } }).__r07.pushAt.length)
    const x0 = await thumbX()
    const x1 = xOf(0.5 + rnd() * (durS - 1))
    await page.mouse.move(x0, y)
    await page.mouse.down()
    for (let k = 1; k <= 12; k++) {
      await page.mouse.move(x0 + ((x1 - x0) * k) / 12, y)
      await page.waitForTimeout(25)
    }
    await page.mouse.up()
    await settle(page)
    const b = await probe(page)
    // 拖动中推送 ≤ 10 Hz：除松开时补推的精确时刻外，相邻推送间隔 ≥ 100 ms（计时器抖动容差 5 ms）
    const at = await page.evaluate((n) => (window as unknown as { __r07: { pushAt: number[] } }).__r07.pushAt.slice(n), nPush0)
    for (let i = 1; i < at.length - 1; i++) minPushGapMs = Math.min(minPushGapMs, at[i]! - at[i - 1]!)
    const errLab = Math.abs(b.lab - b.viewer)
    // Viewer → lab：Viewer 侧连续改时刻（不经同步器），等同用户在 Viewer 时间面板中拖动
    const vt = Math.round((0.5 + rnd() * (durS - 1)) * 1e9)
    const vf = b.viewer
    await page.evaluate(([from, to]) => new Promise<void>((res) => {
      const w = window as unknown as { __awrInspect: { viewer: { set_current_time(r: string, tl: string, t: number): void }; rerunSync: { recordingId: string } }; __r07: { setCalls: number; pushAt: number[] } }
      const rec = w.__awrInspect.rerunSync.recordingId
      let k = 0
      const step = () => {
        k++
        w.__r07.setCalls-- // Viewer 侧的调用不计入"同步器推送"
        w.__awrInspect.viewer.set_current_time(rec, 'sim_time', Math.round(from + ((to - from) * k) / 8))
        w.__r07.pushAt.pop()
        if (k < 8) setTimeout(step, 16)
        else res()
      }
      step()
    }), [vf, vt])
    viewerCalls += 8
    await settle(page, b.tu + 8)
    const c = await probe(page)
    const row = { trip, errLabNs: errLab, errViewerNs: Math.abs(c.lab - c.viewer), appliedDuringLab: b.stats.applied - a.stats.applied, pushedDuringViewer: c.stats.pushed - b.stats.pushed }
    trips.push(row)
    expect(row.errLabNs, `trip ${trip} lab→Viewer`).toBeLessThanOrEqual(FRAME_NS)
    expect(row.errViewerNs, `trip ${trip} Viewer→lab`).toBeLessThanOrEqual(FRAME_NS)
    expect(row.appliedDuringLab, `trip ${trip} lab 拖动期间回写`).toBe(0)
    expect(row.pushedDuringViewer, `trip ${trip} Viewer 侧变化期间推送`).toBe(0)
  }
  const before = await probe(page)
  await page.waitForTimeout(3000)
  const after = await probe(page)
  expect(after.stats).toEqual(before.stats)
  expect(after.tu).toBe(before.tu)
  const s = after.stats
  const pushed = s.pushed - p0.stats.pushed
  const echoes = s.echoes - p0.stats.echoes
  expect(after.setCalls - p0.setCalls, 'Viewer 收到的同步器推送 = pushed').toBe(pushed)
  expect(echoes, '回声 = 推送').toBe(pushed)
  expect(after.tu - p0.tu, 'time_update = 回声 + Viewer 侧变化 + 打开前排队').toBe(echoes + viewerCalls + (s.stale - p0.stats.stale))
  const sum = {
    trips: trips.length, maxErrLabNs: Math.max(...trips.map((r) => r.errLabNs)), maxErrViewerNs: Math.max(...trips.map((r) => r.errViewerNs)),
    pushed, echoes, applied: s.applied - p0.stats.applied, coalesced: s.coalesced - p0.stats.coalesced, viewerSideCalls: viewerCalls,
    timeUpdates: after.tu - p0.tu, stale: s.stale - p0.stats.stale, minPushGapMs: Number.isFinite(minPushGapMs) ? Math.round(minPushGapMs) : null,
  }
  if (Number.isFinite(minPushGapMs)) expect(minPushGapMs, '拖动中推送 ≤ 10 Hz').toBeGreaterThanOrEqual(95)
  writeResult('lab-inspect-round-trips', { run_id: st.runId, summary: sum, trips })
  test.info().annotations.push({ type: 'round-trips', description: JSON.stringify(sum) })
  await page.close()
})

test('Viewer 播放时 lab 播放头跟随，时间线操作收回权威', async () => {
  test.setTimeout(300_000)
  needsLocal()
  const page = await ctx.newPage()
  await openReady(page, 'researcher', 't=2')
  await page.evaluate(() => {
    const w = window as unknown as { __awrInspect: { viewer: { set_playing(r: string, v: boolean): void }; rerunSync: { recordingId: string } } }
    w.__awrInspect.viewer.set_playing(w.__awrInspect.rerunSync.recordingId, true)
  })
  await expect(page.locator('[data-inspect-driven]')).toBeVisible({ timeout: 20_000 })
  await expect.poll(async () => (await probe(page)).source, { timeout: 20_000 }).toBe('rerun')
  await expect.poll(async () => (await probe(page)).lab, { timeout: 20_000 }).toBeGreaterThan(2.3e9)
  const r0 = (await probe(page)).stats.reclaimed
  await page.getByRole('button', { name: /后退|Step back/ }).click()
  await expect(page.locator('[data-inspect-driven]')).toHaveCount(0, { timeout: 20_000 })
  await settle(page)
  const p = await probe(page)
  expect(p.source).toBe('lab')
  expect(p.stats.reclaimed).toBe(r0 + 1)
  expect(Math.abs(p.lab - p.viewer)).toBeLessThanOrEqual(FRAME_NS)
  const t1 = p.viewer
  await page.waitForTimeout(1500)
  expect((await probe(page)).viewer, 'Viewer 已暂停').toBe(t1)
  await page.close()
})

test('选择联动：Viewer → 场景（Viewer 内点选）与场景 → Viewer（#selection= 片段，catalog URI）', async () => {
  test.setTimeout(300_000)
  needsLocal()
  const page = await ctx.newPage()
  await openReady(page, 'researcher', 't=5')
  const sel = (n: number) => page.evaluate((k) => (window as unknown as { __r07: { sel: string[][] } }).__r07.sel.slice(k).flat(), n)
  const nSel = () => page.evaluate(() => (window as unknown as { __r07: { sel: string[][] } }).__r07.sel.length)
  // Viewer → 场景：在 Viewer 的相机视图（自动版面右上，首个标签为第一架机的 cam0）中点击画面，Viewer 选中该机的图像实体
  // （/fleet/<uav>/cam0/image/**，Viewer 派发 selection_change），场景映射为该机并描边
  const first = st.uavs[0]!
  let n0 = await nSel()
  const pt = await texturedPoint(page, '[data-inspect-viewer]', 0.55)
  expect(pt, 'Viewer 相机图像').not.toBeNull()
  // 先悬停（egui 按帧处理指针，软件渲染下约 1 s 一帧），再短按：按住超过点击判定时长不算点击
  await page.mouse.move(pt!.x, pt!.y)
  await page.waitForTimeout(2000)
  await page.mouse.down()
  await page.waitForTimeout(80)
  await page.mouse.up()
  await expect.poll(() => sel(n0), { timeout: 30_000 }).toContainEqual(expect.stringMatching(new RegExp(`^/fleet/${first}/`)))
  await expect(page.locator('[data-scene-selection]')).toHaveAttribute('data-scene-selection', `uav:${first}`, { timeout: 30_000 })
  await expect(page.locator(`[data-scene-uav="${first}"]`)).toHaveAttribute('data-selected', '')
  // 场景 → Viewer：场景中点选另一架机，Viewer 经 URL 片段定位并回报选中该机实体；场景描边随之切换
  const uav = st.uavs[1]!
  n0 = await nSel()
  await page.locator(`[data-scene-uav="${uav}"]`).click()
  await expect(page.locator('[data-scene-selection]')).toHaveAttribute('data-scene-selection', `uav:${uav}`)
  await expect.poll(() => sel(n0), { timeout: 30_000 }).toContain(`/fleet/${uav}`)
  await expect(page.locator(`[data-scene-uav="${uav}"]`)).toHaveAttribute('data-selected', '')
  await expect(page.locator(`[data-scene-uav="${first}"]`)).not.toHaveAttribute('data-selected', '')
  // 定位不改变时间：静置后 Viewer 与播放头仍在 1 帧内，面板保持 READY
  await settle(page)
  const p = await probe(page)
  expect(Math.abs(p.lab - p.viewer)).toBeLessThanOrEqual(FRAME_NS)
  expect(Math.abs(p.lab - 5e9)).toBeLessThanOrEqual(FRAME_NS)
  await expect(page.locator('[data-inspect-panel]')).toHaveAttribute('data-inspect-state', 'ready')
  await page.close()
})

test('评测 catalog：评测员切换后真值演员可定位；研究者请求 catalog=eval 回到分发', async () => {
  test.setTimeout(300_000)
  needsLocal()
  const page = await ctx.newPage()
  await openReady(page, 'researcher', 'catalog=eval')
  await expect(page.locator('[data-view="lab-inspect"]')).toHaveAttribute('data-inspect-catalog', 'dist')
  await expect(page.locator('[data-inspect-catalog-switch]')).toHaveCount(0)
  await expect(page.locator('[data-scene-actor]')).toHaveCount(0)
  await page.close()
  await ctx.clearCookies()
  const ev = await ctx.newPage()
  await openReady(ev, 'evaluator')
  await ev.click('[data-inspect-catalog-switch]')
  await expect(ev.locator('[data-view="lab-inspect"]')).toHaveAttribute('data-inspect-catalog', 'eval')
  await expect(ev.locator('[data-eval-tag]')).toBeVisible()
  await waitReady(ev)
  await instrument(ev)
  await expect.poll(() => ev.locator('[data-scene-actor]').count(), { timeout: 30_000 }).toBeGreaterThan(0)
  const id = await ev.locator('[data-scene-actor]').first().getAttribute('data-scene-actor')
  expect(st.actors).toContain(id)
  await ev.locator(`[data-scene-actor="${id}"]`).click()
  await expect(ev.locator('[data-scene-selection]')).toHaveAttribute('data-scene-selection', `actor:${id}`)
  await expect.poll(() => ev.evaluate(() => (window as unknown as { __r07: { sel: string[][] } }).__r07.sel.flat()), { timeout: 30_000 })
    .toContain(`/truth/actors/${id}`)
  const rr = (await ctx.cookies()).filter((c) => c.name === 'd3_rr')
  expect(rr).toHaveLength(2)
  await ev.close()
  await ctx.clearCookies()
})

test('外壳：UNSUPPORTED、ERROR 与重试；外壳文案过 copy-lint', async () => {
  test.setTimeout(300_000)
  needsLocal()
  const page = await ctx.newPage()
  const texts = new Set<string>()
  const findings: CopyCheck['findings'] = []
  const seen: string[] = []
  const lint = async (state: string, sel = '[data-view="lab-inspect"]') => {
    const r = await shellCopyFindings(page, sel)
    r.texts.forEach((t) => texts.add(t))
    findings.push(...r.findings)
    seen.push(state)
  }
  // 未注册到 catalog 的运行：BN-57 404 305 → ERROR(notFound)，重试仍为 ERROR
  await login(page, 'researcher', inspectPath(st.orphanRun))
  await expect(page.locator('[data-inspect-panel]')).toHaveAttribute('data-inspect-state', 'error', { timeout: 180_000 })
  await expect(page.locator('[data-inspect-error]')).toHaveAttribute('data-inspect-error', 'notFound')
  await lint('error:notFound')
  await page.click('[data-inspect-retry]')
  await expect(page.locator('[data-inspect-panel]')).toHaveAttribute('data-inspect-state', 'error', { timeout: 60_000 })
  // 加载中、打开中与就绪的外壳（状态迁移中逐个取文案）
  await page.goto(st.url + inspectPath(st.runId))
  for (const s of ['loading', 'opening', 'ready']) {
    const hit = await page.waitForSelector(`[data-inspect-panel][data-inspect-state="${s}"]`, { timeout: 180_000 }).then(() => true, () => false)
    if (hit) await lint(s)
  }
  await waitReady(page)
  await page.click('[data-inspect-close]')
  await expect(page.locator('[data-inspect-panel]')).toHaveAttribute('data-inspect-state', 'idle')
  await lint('idle')
  await page.waitForTimeout(1500)
  await expect(page.locator('[data-inspect-panel]'), '关闭后不再自动加载').toHaveAttribute('data-inspect-state', 'idle')
  // 设备档：本机为软件渲染（Tier S），不带 inspect=force 时 UNSUPPORTED，替代操作为原生 Viewer 命令（不含令牌）
  if (TIER === 'S') {
    await page.goto(`${st.url}/lab/runs/${encodeURIComponent(st.runId)}/inspect`)
    await expect(page.locator('[data-inspect-panel]')).toHaveAttribute('data-inspect-state', 'unsupported', { timeout: 60_000 })
    await expect(page.locator('[data-inspect-unsupported]')).toHaveAttribute('data-inspect-unsupported', 'software')
    await lint('unsupported:software')
    await page.locator('[data-inspect-panel] [data-inspect-native]').click()
    const cmd = (await page.locator('[data-inspect-native-cmd]').textContent()) ?? ''
    expect(cmd).toContain(`python -m awr.bench.rerun.open --run ${st.runId}`)
    expect(credentialIn(cmd, [st.tokens.researcher])).toBe(false)
    await lint('native-popover', '[data-slot="popover-content"]')
  }
  writeResult('lab-inspect-shell', { states: seen, texts: [...texts], findings })
  expect(seen).toEqual(expect.arrayContaining(['error:notFound', 'ready', 'idle']))
  expect(texts.size).toBeGreaterThan(10)
  expect(findings, JSON.stringify(findings, null, 1)).toEqual([])
  await page.close()
})
