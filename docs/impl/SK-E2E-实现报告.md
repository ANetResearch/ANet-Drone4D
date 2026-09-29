# SK-E2E walking skeleton 集成门禁（D1-AC-34）：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | SK-E2E walking skeleton 集成门禁（D1-MS3 出口）；授权修改 SK-B、SK-F 触及的全部路径以打通链路 |
| 日期 | 2026-09-29 |
| 依据 | AWR-03 §3.3、§3.6、§4.3、§8.4（D1-AC-01、D1-AC-20、D1-AC-34、D1-AC-35）、§8.5、§8.6 D1-MS3；AWR-12 §4.2.2（OperatorSeat T01–T07）；AWR-14 §4.4、§6.7、§6.11（UX-FR-050）；AWR-17 §3–§7、§9；AWR-18 PR-7、§9.5；前置报告 SK-B、SK-F、M11-R、M15-S、M03-M04 |
| 结论 | 真实后端上的完整链路已打通并固化为门禁：`VITE_AWR_TEST_SWITCHES=1 make build` → `make run`（supervisor → sim-core + api，api 直接服务 `apps/web/dist`、`/worlds` 与 `/api`）→ `npx playwright test perf/skeleton.spec.ts`：深圳 World → 首屏 1 次 Range → Tier S 点云上屏 → 1 架 FleetSim → StateRing → Gateway → WS → 无人机上屏 → UI 起飞 → 点选地面（R07 ray_hit）→ GoTo 结果序列 accepted → running → succeeded，机体停在命中点正上方目标 3 m 内（实测 < 1 m），无 pageerror、无 ≥ 400 的 HTTP 响应。D1-AC-01 的"/world/<id> 可访问"（六城，含删除后 `make run` 自动重建）与 D1-AC-35（FakeSource、fake_gw、`make test-fixtures`）同时确认通过 |

本报告第 4 节是下一阶段 14 个并行 agent 的必读材料（接口约定与已知坑）。

## 1. 链路验证（D1-AC-34 逐段）

门禁用例 `apps/web/perf/skeleton.spec.ts` 第 1 组"walking skeleton full chain (D1-AC-34)"。后端：`SKELETON_API=host:port` 时用运行中的
`make run`；未设置时由 `perf/skeleton.server.ts` 的 `startBackend()` 自起 `python -m awr.runtime.supervisor --profile ci --only sim-core,api`
（空闲端口、临时 runs 目录）。浏览器直连 api，中间没有代理或替身。

| 段 | 断言 | 实测（SwiftShader，1280×720，Tier S） |
|---|---|---|
| World → Range | `/world/shenzhen` 由 api 返回 SPA；首屏 `octree.bin?v=` 的 206 恰好 1 次；`crossOriginIsolated === true`（COOP/COEP/CORP 由 api 中间件给出） | 206 × 1，`bytes=0-321383`，首屏 321,384 B |
| 点云上屏 | 后端 `{tier: 'S', deviceClass: 'software'}`；10k < drawn ≤ 25k；`gpu.calls === passPlan` | drawn 21,922，TTFP 约 0.56 s（非性能口径） |
| FleetSim → StateRing → Gateway → WS | serverInfo `role = operator`、`seat = held`、`world = shenzhen`；roster 1 架；无人机层有位姿；`__perf.net.swarmHz > 5`；DroneRail 1 行 | `p600-01` 出生于 ENU (87.1, 12.5, 0.30)，swarm 10 Hz |
| 起飞 | 详情页"起飞"→ AlertDialog（默认 2.5 m AGL）→ 结果序列 `accepted, running, succeeded`；高度增加 > 1.5 m | 约 5 s |
| 点选地面 | 围绕机体的候选点投影到画布空闲区，真实鼠标点击 → `POST /api/world/shenzhen/query {op: ray_hit}`（M04 GeoProbeServer）；命中点与瞄准点水平距离 ≤ 3 m 且不高于地面 2 m（开阔地） | 首个候选即命中 |
| GoTo 闭环 | 目标 = 命中点正上方，`z = max(当前 z, 命中 z + 10 m)`；结果序列 `accepted, running, succeeded`；渲染位姿与目标距离 ≤ 3 m | 约 50 m 航程 15–20 s，终点误差 < 1 m |
| 全程 | 无 pageerror；无 ≥ 400 的 HTTP 响应 | 通过 |

同组第 2 例覆盖 D1-AC-01 的"/world/<id> 可访问"：六城 `GET /world/<id>` 200 text/html、`/worlds/<id>/world.json` 200 且 id 一致、
页面揭开遮罩且 `__perf.pc.drawn > 0`、无 pageerror。

截图（`/data/projs/anet-drone/.cache/impl/shots/`）：

| 文件 | 内容 |
|---|---|
| `skeleton-01-world.png` | 真实后端：深圳点云、DroneRail 一行 `p600-01`、连接"在线" |
| `skeleton-02a-takeoff-dialog.png` | 起飞确认对话框（目标高度输入，焦点在"取消"） |
| `skeleton-02-takeoff.png` | 起飞完成，详情页命令组 |
| `skeleton-03-goto-running.png` | GoTo 执行中（标记铅垂线与目标点） |
| `skeleton-04-arrived.png` | 到达 |
| `skeleton-05-focus.png` | "聚焦选中"近景（60 m） |
| `skeleton-city-<id>.png` | 其余五城经 api 打开（D1-AC-01） |
| `skeleton-autobuild-suzhou.png` | 删除苏州后 `make run` 自动重建再打开 |
| `skeleton-fake-*.png`、`skeleton-fake-gw.png` | D1-AC-35 两条早期数据源 |

## 2. 修复的阻断问题

| # | 现象 | 根因 | 修复 | 文件 |
|---|---|---|---|---|
| 1 | 第二个浏览器（或同一测试重跑、换浏览器上下文）拿到 viewer，GoTo 返回 116 `SEAT_TAKEN` | 席位只有 T01（签发即 HELD），没有 T03–T05：持有者关页后席位永不释放 | Gateway 实现 GRACE：持有者最后一个 ClientSession 关闭 → `seat_grace` + 30 s 墙钟计时；同 principal 重连 → `seat_resume`；到期 → `seat_expire`（sim-core 释放、租约成孤儿）。门禁用例另用固定 `awr.principal_hint`，重跑保持同一 principal | `python/awr/api/rt/gateway.py`；`tests/rt/test_seat_grace.py` |
| 2 | 页面启动时 `GET /api/worlds` 401（控制台错误） | `apiGet` 只在已有缓存 token 时带 Authorization；M15 的查询先于 RtProvider 拿到 token | `apiGet`/`apiPost` 无 token 时先等待 `getToken()`（与 RtProvider 共用同一请求）；401 时清缓存换 token 重试一次；token 请求 404/5xx/网络错误后 5 s 内不再请求（FakeSource 页面） | `apps/web/src/net/api.ts` |
| 3 | 真实后端上 GoTo 被拒 107（STATE） | 出生机体在地面（READY），准入矩阵只允许 FLYING 等空中态接受 goto（g04 §6.2）；FakeSource 用例把机体直接放在 60 m 空中，掩盖了这一点；UI 的起飞等按钮是禁用占位 | 详情页命令组接通：起飞（AlertDialog，`alt_m` 默认 2.5 m、0.5–120 m）、悬停（直接发送）、降落与返航（AlertDialog）；焦点在"取消"；执行中 Spinner；失败显示原因短文；只有 LIVE 且 operator/admin 持席时可用。M06 门面新增 `mission.command/commandState/gotoResults` | `ui/panels/drone-detail/DroneDetailPanel.tsx`；`viewport/{gotoRule,facade,testHooks}.ts`；i18n |
| 4 | DroneRail 显示"电量 255%" | Lite32 `battery_pct = 255` 表示 none（M09 能量模型登记前恒为 255） | 255 显示"电量未知" | `ui/panels/drones/DronesPanel.tsx`；i18n `drones.batteryNone` |
| 5 | 全链路用例经 Node 测试服务器代理 `/api`，没有真正验证 api 的静态服务、Range、COOP/COEP 与 WS 路径 | SK-F 在后端未交付时的过渡设计 | 全链路组改为浏览器直连 api，并断言 `crossOriginIsolated`、206 次数、HTTP 错误为 0；`startBackend()` 自起 supervisor，使 `npx playwright test perf/skeleton.spec.ts` 自包含 | `apps/web/perf/skeleton.{spec,server}.ts` |

逐项核对未发现问题（无需修改）：`awr.rt.v1` 握手与子协议、serverInfo 布局哈希与 contracts 主版本、BATCH 帧头与 epoch、Lite32/Full64
字节布局（渲染位姿与 sim-core 目标一致到 < 1 m）、ENU 与 three 的坐标映射（屏幕点击 → 射线 → ray_hit 命中点与瞄准 ENU 点吻合）、Range
（单区间 206、`?v=` immutable）、COOP/COEP/CORP、Host 与 Origin 守卫（回环放行）、vite dev 代理（`/api` 含 WS 升级、`^/worlds/.+`，
`make dev` 形态实测 LIVE）、`python -m awr.api.inproc` 形态实测 LIVE、`perf/server.clients[].conn_id` 与 `serverInfo.connId` 同源。

## 3. 验收对应

| 编号 | 条款 | 结果 | 证据 |
|---|---|---|---|
| D1-AC-34 | 一城 World → Range → 点云上屏 → 1 架 FleetSim → StateRing → Gateway → WS → 无人机上屏 → goto 闭环全程通过，无 pageerror | 通过 | `npx playwright test perf/skeleton.spec.ts`（4 例全过，约 1.5 min；自起后端与 `SKELETON_API=127.0.0.1:8030` 的 `make run` 两种形态各通过，后者连续重跑两次通过） |
| D1-AC-01（本包确认部分） | "/world/<id> 可访问"；"删除任一世界后 `make run` 自动重建且 `/world/<id>` 可访问" | 通过 | 六城经 api 打开（skeleton 第 2 例）；手工：临时 `AWR_WORLDS_DIR` 中去掉 suzhou 后 `make run`，worldpkg 25.6 s 重建（content_version 与原件同为 `f67d454c0050`），READY 后 `/world/suzhou` 200、页面揭开、drawn 16,115。其余条款（validate、Ajv、构建耗时）属 M03，`tests/world/test_autobuild.py` 覆盖 |
| D1-AC-35 | fake_gw 与 FakeSource 按 layouts 合成 N ∈ {1, 200, 1000}，解码与 golden 一致，回放 `.awrrt` | 通过 | `make test-fixtures`（Python 21 例、Vitest 33 例）；skeleton 第 2 组两例（FakeSource goto 闭环、fake_gw 真实 WebSocket） |
| D1-AC-35 | "前端在无后端时可跑 flight60 `scene=full`" | 未验证 | flight60 bench 驱动尚未实现（`__perf.bench.mode` 只有字段），属 M16/M06，已写入请求 |
| D1-AC-20 | lint 全套 | 通过 | `make lint` 退出码 0 |

## 4. 接口约定与已知坑（下一阶段必读）

### 4.1 进程与启动方式

| 形态 | 命令 | 说明 |
|---|---|---|
| 生产形态（门禁、演示） | `make build` → `make run` | `make run` = doctor → `worldpkg build --missing`（缺失世界自动重建）→ 前端过期时 `make build` → `python -m awr.runtime.supervisor`（profile demo）。打印 `READY run=<id> ...` 后 api 服务 `http://127.0.0.1:8000/world/shenzhen` |
| 测试构建 | `VITE_AWR_TEST_SWITCHES=1 make build` | 带 `window.__vp`、`__ux`、`?tier=`、`?fixedB=`；读这些钩子的 spec 必须用它。**`make run` 在 `apps/web/src` 比 `dist/index.html` 新时会自动做生产构建并丢掉钩子**；临时办法：`.env.local` 写 `VITE_AWR_TEST_SWITCHES=1`（mk/common.mk 导出），性能验收前删掉 |
| 开发形态 | `make dev` | supervisor profile dev（含 vite 5173 + api 8000）；vite 代理 `/api`（含 WS）与 `^/worlds/.+` 到 api；实测 LIVE |
| 只起核心进程 | `.venv/bin/python -m awr.runtime.supervisor --profile ci --only sim-core,api --set net.port_offset=0 --set net.port=<P> --set bus.rendezvous=tcp/127.0.0.1:<B> --set run.keep_run_dir=false` | 测试自起后端的推荐写法（`startBackend()`、`tests/rt/test_skeleton_chain.py::test_chain_real_processes`）；ci profile 不钉核；stdout 首行 `READY` |
| 进程内（最快） | `.venv/bin/python -m awr.api.inproc --port 8061 [--n 5] [--no-autoplay]` | LocalBus + LocalRing + 后台 sim-core 线程 + api，一个进程；浏览器可直接打开 `/world/shenzhen`；pytest 夹具 `tests/rt/conftest.py::stack` |
| 单独进程 | `python -m awr.sim.runtime`、`python -m awr.api.main` | 调试用；api 单独运行时 ready 为 503（sim down） |
| 早期数据源 | `make fake-gw FAKE_N=200`（`tools/fake/fake_gw.py --n N --port P [--replay x.awrrt --loop]`）；前端 `?source=fake&fakeN=&fakeStart=e,n,u` | 无后端开发 UI；FakeSource 会真实执行 goto |
| 停止 | Ctrl+C 或 `kill -TERM <supervisor pid>`（`make stop` 经 `awr stop`） | supervisor 按 grace 5 s 逐个停子进程；子进程设了 PDEATHSIG |

sim-core 的骨架布设由环境变量控制（supervisor 继承父进程环境）：`AWR_SIM_N`（缺省 1）、`AWR_SIM_SPAWN="x,y"`（缺省取世界原点附近平坦开阔格，
深圳为 (87.1, 12.5)）、`AWR_SIM_PROFILE`（缺省 `p600_mid360`）、`AWR_SIM_AUTOPLAY=0`、`AWR_PLUGINS`（runtime.yaml `plugins`；未交付的包告警并跳过，
见 `sim.started.data.plugins_missing`）。例：`AWR_SIM_N=5 make run`。

### 4.2 端口

| 用途 | 基础端口 | 说明 |
|---|---|---|
| api（HTTP + WS `/api/rt`） | 8000 | `+10·AWR_PORT_OFFSET`（0–9）；ci、perf profile 固定 offset 9（8090） |
| zenoh 汇合点 | 7447 | 同上平移；只允许回环 |
| vite dev / preview | 5173 / 4173 | 同上；Playwright 配置的 webServer 起 `vite preview`（`reuseExistingServer`） |
| 静态拆分回退 | 8001 | 默认不启用 |
| skeleton 测试服务器 / fake_gw（spec 内） | 4193–4195 / 8097 | 同上平移 |

**14 个并行 agent 而 offset 只有 10 档**：手工 `make run`/`make dev` 用 `AWR_PORT_OFFSET`；测试里自起后端一律用空闲端口 + `--set`（上表）。
supervisor 启动时只清理 supervisor 已退出的 `/dev/shm/awr/<run>` 目录，不会误删他人运行中的 run。

### 4.3 token、角色与席位

- 签发：`POST /api/auth/token {role: "viewer" | "operator" | "admin", principal_hint?, client?, admin_secret?}` → `{token, principal_id,
  principal_hint, role, run_id, seat: "held" | "none", exp_unix_ns}`。viewer 无需任何凭据；回环模式 operator 无需口令；admin 一律需要
  `admin_secret`（`runs/<run>/admin.token`，READY 横幅打印路径）；局域网模式 operator 也需要。
- `principal_hint` 为 16–64 位 base32（`[A-Za-z2-7]`），浏览器存 localStorage `awr.principal_hint`，决定 `principal_id = "p-" + hint.lower()`。
  **同一 principal 重复签发幂等并保持席位**；测试请用固定 hint（skeleton 用 `SKELETONEEPLAYWRIGHTGATE`，其他 spec 请用自己的 hint）。
- 席位：每个 run 只有一个写席位（operator 与 admin 共用）。他人持有时 operator 签发返回 409 `116`，前端 `getToken()` 自动改签 viewer
  （控制台会有一条 409，属预期）。**持有者全部 WS 关闭后 GRACE 30 s，期间他人仍拿不到席位**；30 s 后 FREE。签发后从未连接 WS 的
  principal 会一直占席（待 M11 决定策略）——测试脚本签发 operator token 后务必连接 WS 或改用 viewer。
- 使用：REST `Authorization: Bearer <token>`；WS 子协议 `['awr.rt.v1', 'bearer.<token>']`（token 为空时只发前者，网关回 4401）。
  写操作（call）要求 role ≠ viewer（115）且持席（116）；导航命令对 NONE 租约机体隐式取得 OPERATOR 租约。
- 前端：`net/api.ts` 的 `getToken(role = 'operator')` 缓存到过期前 1 分钟；`apiGet`/`apiPost` 自动带 token（没有时先等待签发）；
  `tokenRole()` 返回当前角色。UI 判断可写：`rtClient().serverInfo.role !== 'viewer' && serverInfo.seat === 'held'`。

### 4.4 REST 约定

- 全部 `/api/**` 响应 `Cache-Control: no-store`、`AWR-API-Version: 1`、`X-Request-Id`；错误体 `application/problem+json`
  `{code, reason, message, remedy, request_id}`（前端 `ApiError{status, reason, name_, remedy, detail}`，原因短文 `reasonText(code)`）。
- 守卫：Host 不在白名单 400 `323`（默认 localhost、127.0.0.1；其他主机名需 `AWR_ALLOWED_HOSTS`）；`/api` 的非回环 Origin 403 `303`
  （局域网需 `AWR_ORIGINS`）。
- 新增领域路由：在 `python/awr/api/rest/<domain>.py` 导出 `router`（FastAPI APIRouter），`main.py` 按文件名排序自动发现；可用
  `app.state.bus`、`app.state.session_world_id`、`request.state.principal`；角色依赖 `awr.api.deps` 的 `Viewer`/`Operator`/`Admin`。
- 已有：R01 token、R02 whoami、R04 `GET /api/worlds`（`{items, next_cursor}` + snake_case，需 viewer token）、R05 世界详情、
  R07 `POST /api/world/{id}/query`（op：`height_dsm`、`ground_dtm`、`agl`、`clearance`、`probe`、`ray_hit`、`segment_los`、
  `path_coarse_check`、`heightmap_top`、`terrain_profile`；**{id} 必须等于会话世界**）、R08 sessions/current、R50/R51 health、
  R52 sys/info、R53 sys/procs（仅受监管）、R56 events 补拉、R59 sys/config。例：`{op: "height_dsm", points: [[x, y]]}` →
  `{z_m: [...], source: "dsm_2m", content_version}`。
- 静态：`/worlds/<id>/world.json` no-cache + 强 ETag；其余文件带 `?v=<contentVersion>` 为 immutable，旧版本 409 `310`；单区间 Range
  206（多区间不支持）、416 `309`；`/assets/**` immutable；`/world/:id` 等 SPA 路由回退 index.html。所有资源同源（COEP require-corp，
  跨源资源会被拦）。

### 4.5 实时通道与 RtClient 用法

页面只有一个 RtClient（`createRtClient()` 无参返回单例；M15 `RtProvider` 负责 `init({url: ws(s)://<host>/api/rt, token, tier, deviceClass})`；
engine 用 `rtClient()` 读取；ui 用 `useRt()`/`useConnState()`）。

```ts
import { createRtClient, rtClient } from '@/net/rt'
const rt = createRtClient()                                       // 页面单例
const off = rt.subscribe('uav/p600-01/state', { rate: 60 })        // 引用计数；同 topic 取最高 rate；返回释放函数
const h = rt.call('uav/p600-01/cmd/goto', { pos: [e, n, u], route: 'auto' })
h.onResult((r) => { /* accepted → running → succeeded | failed | rejected | timeout | canceled */ })
const final = await h.result                                       // 终态 CallResult {status, code, reason, message, effect, ...}
rt.onEvents((batch) => {})                                         // 可靠事件，每批一次回调
rt.onStatus((items) => {}); rt.onConnState((s, info) => {}); rt.onTime((t) => {}); rt.onData((m) => {}) // msgpack/json 载荷
rt.roster.agentNoOf(id); rt.roster.idOf(no); rt.roster.entries()   // roster 视图
rt.serverInfo                                                     // role、seat、worldId、runId、connId、layouts ...
rt.swarm                                                          // 最新 swarm 列（n、agentNo、fs、battery、flags、ctrl、pos）
// telemetry 相位专用：rt.swapFrame()（engine/drones 已调用，其他模块不要再调）
```

- 连接状态：`IDLE → CONNECTING → SYNCING → LIVE`；有订阅时首个 SNAPSHOT 才 LIVE；1 s 无 TIME 转 DEGRADED；断线期间 `call` 立即 213 拒绝，
  在途调用重连后同 id 重发；首个结果超时合成 `timeout 200`。
- 默认订阅集由 `viewport/layers/drones.tsx` 统一登记（`fleet/roster` 10、`swarm/state` S 10/B,A 20、`env/state` 10、`event` all、
  `perf/server` 1、`sys/procs` 1、主选机 `uav/{id}/state` 60）；其他模块只订阅自己额外需要的 topic，并在卸载时调用释放函数。
- Topic：`fleet/roster`、`swarm/uav/state`（别名 `swarm/state`，Lite32）、`uav/{id}/state`（Full64）、`uav/{id}/state_ext`（msgpack，2 Hz
  自包含，含 lifecycle）、`env/state`、`event`、`perf/server`、`sys/procs`。rate 只能取 `0, 1, 2, 5, 10, 15, 20, 30, 60`；swarm ≥ 10 Hz；
  msgpack 通配 ≤ 2 Hz。每连接最多 256 订阅；连接上限 32（每 principal 8）。
- Service：`uav/{id}/cmd/{takeoff|goto|hover|land|rtl|cancel}`（骨架已实现；其余命令 109 `SKELETON_UNIMPLEMENTED`，M10 经
  `register_motion_provider` 放行）、`uav/{id}/cmd/{acquire|release}`、`sim/{play|pause|step|speed|reset}`、`seat/release`、`env/query`
  （M07 登记后）。参数按 `packages/contracts/rt/commands.json` 的 `args_schema` 校验（多余字段 300，越界 110）。
- 常用拒绝码：107 状态不允许（例如地面 goto：**必须先 takeoff**）、110 参数越界（含世界 bounds）、115 viewer、116 席位、109 未实现、
  206 被新命令取代、211/213 生产者不可达或服务不可用、212 生产者重启。
- 事件（`event` topic，`RtEvent{seq, t_sim_ns, type, level, uav, cid, data}`）：`cmd.*`（结果镜像）、`sim.started`、`sim.reset`、
  `sim.clock`、`sim.vehicle.state`、`lease.*`、`seat.acquired|released|expired`、`sim.contact.collision|touchdown`、`geo.ready`（后三者
  尚未在 `bus/event.schema.json` 登记，已请求 M00）。

### 4.6 StateRing 与总线 key

- StateRing：`/dev/shm/awr/<run>/state.sim-core`（`AWR_RUN_DIR`；api `ApiSettings.ring_path`）；头部 256 B、K = 32 槽、容量 1024、8 个读者游标。
  读者：`ring = StateRing.attach(path, expect_layout_id=awr.contracts.LAYOUT_ID)`、`ring.register(LOSSY, "<name>")`、
  `ring.read_latest(last_seq)`（撕裂重试 ≤ 3 次）、`ring.header()`、`ring.identity()`（inode + 写者 pid，变化即重新 attach）。
  布局哈希不一致抛 `LayoutMismatch`（312）。测试用 `LocalRing`（同接口）。**sim-core 是唯一写者**。
- 总线：全仓库只有 `awr.runtime.bus` 可以 `import zenoh`；key 字面量只允许出现在生成的 `awr.contracts.bus_keys`。命名空间
  `awr/<world>/<run>`。常用：`ctl_cmd("sim-core")`（命令，`{v, cid, op, uav, args, principal, lease, t_wall_ns, epoch_seen, batch_id}`）、
  `CTL_CLOCK`、`CTL_LEASE`（`seat_claim|seat_grace|seat_resume|seat_expire|seat_release|acquire|release`）、`ctl_roster("sim-core")`、
  `CTL_QUERY`（`register_query` 的路由）、`CTL_GCS`、`state_ext("sim-core")`（msgpack `[[agent_no, state_ext], ...]`，2 Hz）、`STATE_PERF`、
  `evt(producer, cat)`（EventPublisher 按类别合批）、`svc_geo(op)`（M04 GeoProbeServer：`height`、`ray_hit`）、`SYS_PROCS`、
  `proc_ready(name)`/`proc_alive(name)`（liveliness）。
- 所有写 sim 状态的请求都要带 api 签名的 principal（`TokenService.sign_principal(pid, role, cid, conn_id=, seat=)`，sim-core 用 K_entry 验签）；
  新增的 api → sim 调用照此构造，不要绕过。
- 进程接入运行时：`ctx = init_child("<name>")`（PDEATHSIG、日志、BLAS 断言）→ `ZenohBus.open(ctx)` → 心跳 `hb.<name>` 10 Hz →
  `bus.ready()`；进程清单在 `configs/runtime.yaml` 的 `procs:`（M11 所有，追加条目走请求）。

### 4.7 sim-core 扩展点（M07、M09、M10、M13）

插件是 `AWR_PLUGINS` 中的包（`awr.environment.stage`、`awr.sim.safety`、`awr.sim.mission`、`awr.sim.sensors`），组合根在启动时 import，
import 时完成登记；不得修改 `awr/sim/runtime/**`。签名冻结（`tests/sim/test_skeleton.py` 锁定）。

```python
import numpy as np
from awr.sim.fleet.stages.registry import (Fidelity, register_stage, register_state_block, register_admission_check,
                                           register_slow_task, register_query, register_energy_model, register_safety_hooks)
from awr.sim.core.command import register_motion_provider
from awr.sim.core.metrics import register_metric

register_state_block("battery", owner="M09", fields={"soc": (np.float32, ()), "battery_pct": (np.uint8, ()),
                                                     "p_avg_w": (np.float32, ())})

@register_stage("battery", every=25, phase=3, order=120, writes=("battery.soc", "battery.battery_pct"))
def battery_stage(S, ctx):          # S: FleetState（容量 1024 的 SoA），ctx: StageCtx（tick、t_ns、dt(stage)、events、rng、world ...）
    act = np.flatnonzero(S.active)  # 只处理活动槽；热路径不分配（预分配缓冲放模块级）
    ...
```

| 约束 | 内容 |
|---|---|
| 时序 | 主时钟 250 Hz；`every` 必须整除 250，`0 ≤ phase < every`，`(tick - phase) % every == 0` 时执行 |
| order 区段 | M07 15–24；M09 25、110–124、130–139；M10 26–29、150–169；M13 100–109（M08 0–14、30–99、128–129、140–149） |
| 预算 | `budget_core` 缺省查 `awr/sim/fleet/stages/budgets.py`（stage 名即键，不在表中必须显式给出）；Σ ≤ 0.40 核，否则拒绝构建 |
| 写者 | `writes` 声明字段；同一字段只能有一个写者 |
| 坐标 | FleetState 内部为 NED/FRD、四元数 (w, x, y, z)，只允许在 `awr/sim/fleet/**` 内出现；其他模块读 `S.enu`（EnuViews：pos、vel、acc、q_xyzw、omega_flu，`pose_enu_flu(slots)`）或 `S.pos_enu_view()`；风经 `S.set_wind_from_enu(w_enu, slots)` 写入 |
| 兜底 | M09 登记名为 `safety` 的状态块或名为 `fsm` 的 stage 后，骨架兜底 FSM `fsm_min`（order 95）自动停用；`battery` 块同理 |
| 准入 | `register_admission_check(4 或 8, name, fn)`，`fn(req: AdmitReq, ctx: AdmitCtx) -> AdmitResult | int | None` |
| 其他 | `register_slow_task(name, fn, period_wall_s=, period_sim_s=, budget_us=200)`；`register_query(name, fn)`（`ctl/sim-core/query`，M07 `env/query`）；`register_energy_model`、`register_safety_hooks`（M09 各一次）；`register_motion_provider(provider)`（M10，`MotionProvider{name, ops, start, cancel}`，覆盖的 op 即放行）；`register_metric(name, fn, owner=)` |
| 事件 | stage 内经 `ctx.events.emit(kind, t_sim_ns=ctx.t_ns, severity=0, uav=<id>, cid=None, **data)`（只追加，主循环每轮 flush）；data 键与形参同名时用 `fields={...}` |

单步测试（不需要进程、zenoh、墙钟）：参照 `tests/sim/test_skeleton.py::Harness`——`LocalRing.open_or_create` + `LocalBus.open` +
`SimCore(SimConfig(n_vehicles=1, load_world=False), bus, ring, secret=..., wall_ns=lambda: W[0], reg=<isolated_registry()>)`，
`core.start()` 后推进假墙钟并调用 `core.iterate()`；命令经 `core.engine.handle({...principal 签名...})`。登记测试用
`with isolated_registry() as reg:` 隔离。

### 4.8 前端：engine/loop 相位注册

```ts
import { register, type FrameCtx } from '@/engine'
const off = register('overlay', 'mymod.task', (ctx: FrameCtx) => { /* 不分配 */ }, { order: 0, fps: 10, tiers: ['S', 'B', 'A'], layer: 'mainJs' })
// 卸载时 off()
```

- 相位顺序：`telemetry → clock → drones → camera → world →（R3F advance → render）→ overlay → governor`；同相位按 `order` 升序；同 id 重复登记会替换。
- `fps` 量化节流（0 = 每帧）；`tiers` 过滤档位；`layer` 把 CPU 时间计入 `__perf.layers` 对应键（pointcloud、drones、trails、frustums、
  environment、groundSky、labels、hudCharts、mainJs）。
- FrameCtx 字段：`frameNo, nowMs, dtMs, tRenderS, tFocusS, simRate, clockState, camera, cssW/H, dbW/H, dpr, cloudScale, tier, deviceClass,
  moving, frozen, be`（RenderBackendView）。唯一 `renderer.render` 在 WorldCanvas 的 priority-1 订阅者里，其他模块不得调用。
- engine/**、net/** 禁止 import react、@react-three、zustand、ui、viewport、stores（oxlint TS-BND-01）；engine 用 `rtClient()` 读网络。

### 4.9 前端：图层注册

图层适配文件 `apps/web/src/viewport/layers/<layer>.tsx` 归图层所属模块（pointcloud M05、environment M07，其余 M06），在 effect 中登记：

```ts
import { registerLayer } from '@/viewport/layers/registry'
const off = registerLayer({ id: 'trails', owner: 'M06', perfKey: 'trails', root: group /* Object3D，挂在 WorldRoot 下，图层帧 = 世界 ENU */,
  channel: 0, drawCount: (ctx) => n /* 本帧 draw call 数，计入 pass 计划 */, setVisible: (v) => { group.visible = v }, dispose: () => {} })
```

- WorldRoot 已做 `rotation.x = -π/2`（ENU → three 的 (E, U, -N)），图层对象直接用 ENU 坐标；单点换算用 `enuToThree(e, n, u, v3)`（`@/engine`）。
- `drawCount` 必须与实际 draw call 一致：门禁断言 `__perf.gpu.calls === __perf.gpu.passPlan`（AWR-03 §3.6 规则 2）。
- 新材质首次出现会在 SwiftShader 上编译数百毫秒（shader zoo 尚未实现），性能用例前需在遮罩下预热。
- 参考实现：`viewport/layers/drones.tsx`（运行时对象 + 相位任务 + 订阅 + 两个图层）、`viewport/layers/pointcloud.tsx`。

### 4.10 store 约定

- 所有 store 用 `createAwrStore(name, init)`（`@/lib/createStore`，zustand vanilla，写次数计入 `__perf.ui.storeWrites`，dev 构建 > 10 次/秒告警）；
  写只经 `store.setState`（init 不接收 set）；actions 是 store 旁的普通函数；React 侧导出 `use<Domain>(selector)`。
- 领域 store 路径归属：`stores/fleet.ts` M11、`world.ts`/`layers.ts` M05、`perf.ts` M06、`timeline.ts` M12、`safety.ts` M09、`mission*.ts` M10、
  `sensors.ts` M13、`env.ts` M07、`jobs.ts` M03、`agents.ts` M14、`selection.ts`/`prefs.ts` M15。M15 只写面板 JSX。
- 高频数据不进 store：机群行是 `stores/fleet.ts` 的预分配 TypedArray（`fleetRows`，`fleetIdOf(i)`），store 只存 `{version, n, alerts, ...}`，
  overlay 相位 S 4 Hz、B/A 10 Hz 更新；面板用 `version` 作缓存键就地读行。
- 选择：`selectionStore.getState().primary`（roster id）、`selection.select([id])`；主选变化由 drones 图层绑定 60 Hz 订阅与高亮。

### 4.11 门面与测试钩子

- `@/viewport/facade`（ui 只能经此访问视口）：`viewport.{setUnobscuredRect, setFrameCap, setSuspended, projectToScreen, onChange, backend, worldId}`、
  `camera.{setMode, focus, northUp, home, onMode, setFollowLock}`、`pick.{ground, at, clear}`、
  `mission.{sendGoto, gotoState, gotoResults, command(op, args), commandState(op)}`（本包新增后三者）。
- `window.__vp`（测试构建）：`project(e,n,u)`、`pickAt`、`ground()`、`clearPick()`、`sendGoto()`、`gotoState()`、`gotoResults()`、
  `cmdState(op)`、`cmdResults(op)`、`dronePose(id)`、`flightState(id)`、`roster()`、`conn()`、`session()`（role、seat、runId、worldId）、
  `backend()`、`world()`、`layers()`、`vpSession`。`window.__ux`（M15）、`window.__perf`（awr.perf.v1）。
- DOM 钩子：`[data-rail-row]`、`[data-drone-id=<id>]`、`[data-cmd=<op>]`（`data-cmd-state`）、`[data-cmd-confirm=<op>]`、`[data-cmd-result]`、
  `[data-goto-target]`；GoTo 按钮可用名"飞到此处"，聚焦按钮"聚焦选中"。

### 4.12 测试命令

| 目的 | 命令 |
|---|---|
| lint（必须保持通过） | `make lint` |
| Python 功能测试 | `.venv/bin/python -m pytest -q tests/<模块目录> -m "not perf and not slow"`；网关链路 `tests/rt`（`stack` 夹具为进程内栈）；sim `tests/sim` |
| TS 单元 / 浏览器 | `cd apps/web && npx vitest run --project unit tests/<dir>`；`--project browser`（`*.browser.test.ts`） |
| 类型检查 | `cd apps/web && npx tsc -p tsconfig.json --noEmit` |
| 契约与早期数据源（D1-AC-35） | `make test-fixtures` |
| walking skeleton 门禁（D1-AC-34） | `VITE_AWR_TEST_SWITCHES=1 make build && cd apps/web && npx playwright test perf/skeleton.spec.ts --project perf`（自起后端，约 1.5 min）；对运行中的后端：`SKELETON_API=127.0.0.1:8000 npx playwright test perf/skeleton.spec.ts -g "full chain"` |
| 模块 Playwright 用例 | 放 `apps/web/perf/m<nn>/`，`npx playwright test perf/m<nn>/x.spec.ts --project perf`；需要真实后端时复用 `import { startBackend } from '../skeleton.server'` |
| 禁止（并行阶段） | 性能基准、`-m perf` 用例、`make perf`、`npm install`/`pip install` |

Playwright 使用本机 Chrome 151 + SwiftShader（`PW_CHROME`），视口 1280×720；配置的 webServer 会起 `vite preview`（dist 须存在）。
生产构建下读 `__vp`/`__ux` 的 spec 会 skip，skip 不算通过。

### 4.13 已知坑（汇总）

1. **地面上的机体不接受 goto**（107）：先 `uav/{id}/cmd/takeoff {alt_m}`（UI：详情页"起飞"），等 succeeded 再 goto。
2. **席位**：一个 run 一个写席位；关页后 GRACE 30 s；测试用固定 `principal_hint`；多个测试并发打同一后端会互相抢席。
3. **`make run` 自动生产构建会抹掉测试钩子**（见 4.1）。
4. **路由世界 ≠ 运行世界**：运行世界由 supervisor（`AWR_WORLD`，缺省 shenzhen）决定；打开 `/world/suzhou` 时点云是苏州，无人机仍按深圳 run 的坐标绘制，
   R07 对苏州返回会话世界不符错误。世界切换与提示未实现（已请求 M15/M06）。
5. **Tier S 固定预算 B = 25k**：近景（"聚焦选中"60 m）点云稀疏，属预算限制而非加载故障（CAS 属 M05）。
6. **电量恒为 255（未知）**直到 M09 登记能量模型；`fsm_min` 兜底 FSM 无 PREFLIGHT、无 failsafe。
7. 骨架只实现 takeoff、goto、hover、land、rtl、cancel；numpy oracle 约 1.2 ms/tick/架（N 大时等 MS4 numba 核）。
8. roster 的 `lifecycle` 是快照（只在增删时改版本），逐机生命周期看 `uav/{id}/state_ext.lifecycle` 或 `sim.vehicle.state` 事件。
9. 世界摘要只在 api 启动时读取；运行中重建世界不会刷新 `serverInfo.world`。
10. 顶栏"只读"徽标是静态占位、顶栏时钟与 Timeline 未接 TIME、详情页遥测 LfStat 未取值、World Hub 读错字段（`worlds` vs `items`）：均为 M15/M12 待办。
11. SwiftShader 下 `renderer.render` 与新材质编译会产生数百毫秒长帧，`PerfHud` 的 p95 数值在功能冒烟中没有意义；性能只在独占锁下测。
12. zenoh 只允许回环端点；api 的 Host/Origin 守卫对非回环访问返回 400/403（局域网需 `AWR_BIND`、`AWR_ORIGINS`、`AWR_ALLOWED_HOSTS`）。
13. `tools/fake/fake_gw.py` 只确认调用（goto 不会真正飞），需要闭环时用 FakeSource 或真实后端。
14. ci profile 默认 `run.keep_run_dir: true`：测试自起的 supervisor 停止后会在 `/dev/shm/awr/<run>/` 留下约 3 MB（下次任意 supervisor 启动时清理）；
    自起后端请加 `--set run.keep_run_dir=false`（`startBackend()` 已加；`tests/rt/test_skeleton_chain.py::test_chain_real_processes` 未加，
    属 M11 测试）。

## 5. 与 PRD 的偏差及理由

| 偏差 | 理由与影响 |
|---|---|
| 席位 GRACE 放在 api Gateway 计时（sim-core 只接收 `seat_grace`/`seat_resume`/`seat_expire`） | AWR-12 §4.2.2 规则 1 要求权威在 sim-core；会话与连接只有 api 知道，计时放在 api、状态变更仍经 sim-core 原子执行，sim-core 重启后按缓存重登记（M11-FR-023）。签发后从未连接的 principal 不进入 GRACE（文档未规定），已请求 M11 定策略 |
| 详情页命令组（起飞、悬停、降落、返航）由本包接通，未做客户端准入预判、Tooltip 原因、快捷键 | 门禁需要 UI 起飞；其余属 M15-FR-027/028，已写入 SK-E2E-to-M15 |
| 修改 M15 的 i18n JSON（新增 16 个键，en 为空串回退中文） | ui/** 禁止 CJK 字面量（I18N-01），新增文案只能进 JSON；已移交 M15 |
| 门禁用例在生产构建下 skip 而非失败 | 与 M15 冒烟用例一致；已请求 M00/M16 在门禁目标中把 skip 判为失败 |
| D1-AC-35 的 flight60 `scene=full` 未验证 | bench 驱动未实现（M16/M06） |
| 门禁用例选择"机体附近的开阔地面"而非 SK-F 的固定点 (-40, -170) | 真实出生点在 (87.1, 12.5)，固定远点在 home 视角下可能被建筑遮挡或落在楼顶；动态候选 + ray_hit 结果校验更稳，且仍走真实鼠标点击 |

## 6. 测试结果

| 命令 | 结果 |
|---|---|
| `npx playwright test perf/skeleton.spec.ts --project perf`（自起后端） | 4 通过（full chain 约 44 s、六城约 18 s、FakeSource 约 14 s、fake_gw 约 5 s），合计约 1.5 min；最终构建上连续多次通过 |
| 同上，`SKELETON_API=127.0.0.1:8030`（`AWR_PORT_OFFSET=3 make run`），full chain 组 | 连续两次通过（第二次机体已在空中，跳过起飞） |
| `.venv/bin/python -m pytest -q tests/rt -m "not perf"` | 7 通过（含新增 `test_seat_grace.py`） |
| `.venv/bin/python -m pytest -q tests/rt tests/sim -m "not perf and not slow"` | 16 通过 |
| `make test-fixtures` | 通过（Python 21 例；Vitest 4 个文件 33 例） |
| `npx vitest run --project unit` | 28 个文件 361 例通过 |
| `npx vitest run --project browser` | 3 个文件 10 例通过 |
| `npx playwright test perf/m15/smoke.spec.ts --project perf`（回归） | 7 通过 |
| `npx tsc -p tsconfig.json --noEmit` | 通过 |
| `make lint` | 通过（ruff、oxlint type-aware、no-emoji、no-hex、lint-lf、motion-lint、check-icons、no-raw-controls、check-brand、check-deps、units、py-imports、py-callbacks、perf-flags、lint-m15） |
| 手工：`make dev` 形态（vite 5203 → api 8030）、`python -m awr.api.inproc` 形态 | 均 REVEALED + LIVE，无 pageerror |
| 手工：删除 suzhou 后 `make run` | 25.6 s 重建、`/world/suzhou` 可访问 |

按并行阶段约束未运行性能基准与 perf 标记用例；上文时长均为功能冒烟观察值。`apps/web/dist` 当前为测试构建。

## 7. 遗留问题

1. 签发 operator token 后从未连接 WS 的 principal 永久占席（M11 定策略）；席位被占时浏览器控制台的 409 噪声。
2. 路由世界与运行世界不一致时的 UI 处理（M15、M06）。
3. `make run` 自动生产构建与测试钩子的冲突、门禁 skip 语义（M00、M16）。
4. D1-AC-35 flight60 `scene=full` 无后端运行（M16、M06）。
5. 顶栏角色徽标、TIME 时钟、详情页遥测、World Hub 字段映射（M15、M12）。
6. SK-B、SK-F 报告中列出的后续项（兴趣集下推、GCS 5 Hz 心跳、CAS、shader zoo、Tier A 等）不变。

## 8. 给其他模块的请求（`.cache/impl/requests/`）

| 文件 | 要点 |
|---|---|
| `SK-E2E-to-M15.md` | 命令组与电量显示的交接、新增 i18n 键；World Hub 读 `items`、顶栏角色徽标接 serverInfo、遥测 LfStat、路由世界与运行世界不一致的提示 |
| `SK-E2E-to-M06.md` | 门面 `mission.command/commandState/gotoResults` 与 `__vp` 新钩子；世界不一致时的无人机层与拾取；近景点云预算说明 |
| `SK-E2E-to-M11.md` | 席位 GRACE 已实现与待定策略；`net/api.ts` 的 token 等待、401 重试、无网关退避；409 噪声 |
| `SK-E2E-to-M16.md` | skeleton 用例的门禁形态、`startBackend()`、截图命名、skip 判失败、flight60 未验证 |
| `SK-E2E-to-M00.md` | `make build-test` 或透传 `VITE_AWR_TEST_SWITCHES`、`make skeleton` 门禁目标、端口偏移档位不足时的做法 |

收到的请求：无发给 SK-E2E 的请求文件。SK-B、SK-F 发出的请求中与链路相关的已核对：SK-F-to-M11 第 8 条（conn_id 一致）无需改动；
SK-B-to-M15 第 2 条（`/api/worlds` 401）已在 `net/api.ts` 解决，第 1 条（字段映射）仍待 M15。

## 9. 变更文件清单

| 文件 | 所有者 | 变更 |
|---|---|---|
| `python/awr/api/rt/gateway.py` | M11 | 席位 GRACE（T03–T05）：`seat_grace_s`、`_seat_op`、`_seat_session_closed/opened`、`_seat_grace_expired`，`add_session`/`remove_session` 调用 |
| `tests/rt/test_seat_grace.py` | M11 | 新增：GRACE、重连恢复、到期释放、他人随后取得席位 |
| `apps/web/src/net/api.ts` | M11 | REST 等待 token、401 重试、无网关退避 |
| `apps/web/src/viewport/gotoRule.ts`、`facade.ts`、`testHooks.ts` | M06 | 单机命令路径与结果记录、门面增补、测试钩子增补 |
| `apps/web/src/ui/panels/drone-detail/DroneDetailPanel.tsx` | M15 | 命令组接通、确认对话框、写权限判定 |
| `apps/web/src/ui/panels/drones/DronesPanel.tsx` | M15 | 电量未知显示 |
| `apps/web/src/app/i18n/zh-CN.json`、`en.json` | M15 | 新增 16 个键 |
| `apps/web/perf/skeleton.spec.ts`、`skeleton.server.ts` | M16（SK-F 创建） | 门禁形态重写、`startBackend()` |
| `docs/impl/SK-E2E-实现报告.md` | — | 本报告 |
