# SK-B 后端 walking skeleton 实现报告（D1-MS3）

工作包：SK-B（M11 网关与 api 进程、M08 sim-core 最小实现与扩展点骨架）。
结论：后端链路"supervisor → sim-core（FleetSim → StateRing，CommandEngine）→ api（Gateway → awr.rt.v1 WS，REST，静态 World）"
在进程内（`--inproc`，LocalBus + LocalRing）与真实进程（supervisor、zenoh、mmap StateRing）两种模式下均已跑通：客户端签发 token →
WS 握手 → 订阅 → 收到 BATCH（Lite32、Full64，帧头 epoch）→ takeoff、goto → result accepted → running → succeeded → 位置到达。
`make run`（demo profile 全部进程）打印 READY，`/api/health/ready` 为 200，`/world/shenzhen` 返回前端页面。

## 1. 实现清单

### 1.1 api 进程（M11，`python/awr/api/**`）

| 文件 | 内容 | 对应条目 |
|---|---|---|
| `main.py` | `create_app(settings, *, bus, ring_cls, subscribe_events)`；lifespan：受监管时 `init_child("api")`（保存并恢复 uvicorn 的 SIGTERM/SIGINT 处理器，停机仍走 uvicorn 优雅关闭）→ 打开总线（zenoh，或 `AWR_API_BUS=local`）→ Catalog 读世界摘要 → 启动 Gateway → 10 Hz 写 `hb.api` → `bus.ready()`；`rest/*.py` 按文件名排序自动发现 `router`（含 M04 `world_query`）；WS `/api/rt`；静态路由与 SPA 回退最后注册；异常处理统一 problem+json（ApiProblem、422 校验 → 300、404 → 305、405 → 306）；模块级 `app` 惰性构造（导入无副作用）；`python -m awr.api.main` 开发入口 | M11-FR-080、FR-087、FR-100；M11-AC-034 |
| `settings.py` | `ApiSettings.from_env()`：supervisor 注入的 `AWR_*`、秘密文件、`admin.token`；Host 白名单与 Origin 白名单；`ring_path` | M11-FR-015、FR-022 |
| `security.py` | token 签发与校验（`awr.runtime.principal`，HKDF 派生 K_auth、K_entry）、`principal_hint` → principal_id、可信入口签名 principal | M11-FR-021、FR-056 |
| `middleware.py` | 纯 ASGI：Host 守卫（400 `323`）、`/api` Origin 守卫（403 `303`）、X-Request-Id（UUIDv7）、COOP/COEP/CORP/nosniff/Referrer-Policy、`AWR-API-Version: 1`、`/api` 缺省 `Cache-Control: no-store`、Bearer 解析 | M11-FR-022；17 §3.3、§4.1、§5.4；M11-AC-009、AC-033 |
| `problem.py`、`deps.py` | problem+json 构造（符合 `rest/problem.schema.json`，429/503 带 Retry-After）；`require_role` 与 `Viewer`/`Operator`/`Admin` 依赖别名 | 17 §4.4 |
| `static.py` | `/worlds/{id}/**`（world.json no-cache + sha256 强 ETag + 304；`?v=` 当前版本 immutable、旧版本 409 `310` + no-store；无 `?v=` no-cache + ETag；每请求 stat world.json 感知原子替换）、单区间 Range 206 / 416 `309`、`Accept-Ranges`；`/worlds/_shared/env/**`；`/assets/**` immutable；`/brand/**`、`/bench/**` no-cache + ETag；SPA 回退（`/`、`/world/:id` 等 → index.html no-cache，`/api/**` 不回退）；拒绝 `..` 与隐藏段 | M11-FR-082；17 §5.1–§5.4；M11-AC-033 |
| `rest/auth.py` | R01 `POST /api/auth/token`（admin 与 lan 模式 operator 须 admin_secret，401 `304`；operator/admin 经 `ctl/sim-core/lease{seat_claim}` 占席位，冲突 409 `116`，sim 不可达 503 `211`）；R02 `GET /api/auth/whoami` | M11-FR-021、FR-023、FR-081 |
| `rest/worlds.py` | R04 `GET /api/worlds`（`{items, next_cursor}`，snake_case，分页 limit/cursor）、R05 `GET /api/worlds/{id}`（另含 coordinate_sha256、bounds_m、camera_home、layers、qa）；Catalog 在线程池读取 | M11-FR-081、FR-083；17 §4.3.2 |
| `rest/sys.py` | R50 live、R51 ready（200 / 503 `{status: not_ready, sim, code: 211}`）、R52 sys/info（公开 / viewer 两级）、R53 sys/procs（经 supervisor `sys/procs`）、R56 `GET /api/events`（按全局 seq 补拉，410 `319`）、R59 sys/config | M11-FR-068、FR-081、FR-085 |
| `rest/sessions.py` | R08 `GET /api/sessions/current`（会话状态由环健康与 TIME.state 推导，AWR-12 §4.1.1） | M11-FR-081 |
| `rt/ws.py` | WS 握手顺序：Host → Origin（403）→ 子协议（400 + `AWR-Supported-Protocols`）均以 `send_denial_response` 在升级前拒绝；以 `awr.rt.v1` 接受（不回显 bearer）→ token（error 301/302 + 4401）→ 连接上限（32 / 每 principal 8，4429）→ serverInfo、全量 advertise、TIME、活动 status → hello 10 s（error 317 + 4408；hello 前其他 op 回 300）；contracts 主版本不同 error 311 + 4426；role 只降不升；resume 补发事件；二进制 > 4 KiB 1009；10 s 内 3 次格式错误 1002；op 分派（subscribe、unsubscribe、ack、ping、call、cancel、clientStats；advertise → 322；playback → 213；未知 → 313） | M11-FR-020、FR-025、FR-027、FR-029；M11-AC-008 |
| `rt/protocol.py`、`rt/channels.py` | 控制消息构造（error、status、result 过滤 effect 字段）；ChannelRegistry（固定 channel 1–6，动态 id 从 100 起、按 topic 稳定；`swarm/state` 别名；Channel.record 按 (seq, reset) 缓存编码） | M11-FR-030、FR-034、FR-038 |
| `rt/clock.py` | GatewayClock：全局 epoch 持久化（`gw.epoch`、`gw.seen`）、TIME 合成（10 Hz 或变化即发）、pong 的 server_ns/sim_ns | M11-FR-048、FR-052、FR-053 |
| `rt/session.py` | ClientSession：控制面 FIFO 1024（满时 318 + 1013）、sender task、对齐网格到期位、发送时装帧（尾帧保证）、帧内 roster 恒在最前、SNAPSHOT/GAP/REPLAY、credit W 每秒重算、令牌桶、订阅语义（rate 量化、swarm ≥ 10 Hz、msgpack 通配 ≤ 2 Hz、`added: true` 补发）、事件过滤与 `event`/`events` 合并 | M11-FR-031、FR-032、FR-037、FR-039 至 FR-043、FR-066；M11-AC-012、AC-013、AC-014 |
| `rt/rpc.py` | 入口 ①（viewer 115、席位 116）、参数 schema 结构校验（110/300）、路由（`uav/{id}/cmd/*` → `ctl/{producer}/cmd`，`sim/*` → clock，`seat/release`、acquire/release → lease，`env/query` → query）、签名 principal、bus 调用 1 s 超时重试 2 次（211/213）、在途表、accepted/running/progress/终态 result、终态重放 `duplicate: true`、cancel、生产者重启 212 | M11-FR-055 至 FR-060、FR-064；M11-AC-025 |
| `rt/events.py` | EventIngest：`evt/**` 订阅与 pump、cmd.* 转 RPC 结果、全局 EventRing 65,536、缺口 GAP + `status events.gap`、`sim.started`/`sim.reset` 触发 roster 重取 | M11-FR-065 至 FR-067；M11-AC-004 |
| `rt/sources/{base,live}.py` | LiveSource：LOSSY 读者 `api`、200 ms 重试 attach、健康分级（OK/STALLED/DOWN/RESTARTING/FAILED）、segment/epoch 判据、每 60 tick 检查 identity、Lite32 转发与 `uav/{id}/state` 按行懒切片、roster_version 变化触发 roster 查询 | M11-FR-046 至 FR-049 |
| `rt/gateway.py` | 60 Hz 绝对截止时间 tick（落后跳格计 overrun）；inbox（线程回调只 `call_soon_threadsafe`）；roster 合并与逐机 channel 增量 advertise/unadvertise；`state/sim-core/ext` 按字节区间切片；席位缓存与 sim-core 重启后重登记；`proc.sim-core` 健康 status；1 Hz `perf/server`、`sys/procs` | M11-FR-035、FR-036、FR-075、FR-085；M11-AC-021、AC-023 |
| `inproc.py` | `python -m awr.api.inproc`：LocalBus + LocalRing，sim-core 主循环在后台线程（以 importlib 按名加载，不静态依赖 awr.sim）；`InprocSim`、`inproc_settings()` 供测试 | M11-FR-102 |

### 1.2 运行时库（M11，`python/awr/runtime/**`）

- `supervisor.py`：READY 横幅前等待核心层进程离开 STARTING（上限为启动宽限，≤ 20 s）；核心进程非 RUNNING 时首行追加
  `degraded=<name>=<state>(<reason>)` 并加一行 warn（回复 MS12-to-M11 第 3 条）。首行仍以 `READY` 开头，现有解析不受影响。
- `events.py`：`EventPublisher.emit()` 增加仅关键字参数 `fields: dict | None`，与 `**data` 合并（向后兼容）；用于 data 中含 `kind`
  等与形参同名的键（`sim.contact.collision.data.kind`）。

### 1.3 sim-core（M08，`python/awr/sim/{runtime,fleet,core,backends}/**`）

| 文件 | 内容 | 对应条目 |
|---|---|---|
| `runtime/main.py`、`__main__.py` | 启动序列（init_child → `StateRing.open_or_create` → epoch/segment：复用文件各 + 1、新文件保持 1/0 → ZenohBus → 组合插件 → 载入世界与 GeoProbeServer → 布设 → 自动播放 → `sim.started`、`geo.ready` → gc.freeze → `bus.ready()`）；主循环 `iterate()`：心跳 → 步顶 drain（≤ 512）→ 到期 tick → flush → 慢任务（geo.run、state_ext 2 Hz、perf 1 Hz、登记的慢任务、审计 fsync）→ 1 Hz step stats；服务 cmd、clock、lease/seat、roster、estimate（213）、query、geo、gcs、interest；`reset()`（epoch、segment + 1）；可注入墙钟（测试单步驱动） | M08-FR-001 至 FR-006、FR-008、FR-053；M04-to-M08 |
| `runtime/clock.py` | SimClock：4 ms tick、play/pause/step/speed/reset、追帧 `max(5, ⌈5·rate⌉)` 与 rtf_limited、暂停累计 | M08-FR-001、FR-002、FR-008；M08-AC-001 |
| `runtime/config.py` | SimConfig（`AWR_SIM_N`、`AWR_SIM_SPAWN`、`AWR_SIM_AUTOPLAY`、`AWR_PLUGINS`、`AWR_SIM_LOAD_WORLD` 等） | M08-FR-005 |
| `fleet/state.py` | FleetState SoA（容量 1024 预分配）、状态块、兜底状态块、`EnuViews`（ENU/FLU 只读视图，按 version 懒刷新） | M08-FR-010、FR-011、FR-087 |
| `fleet/px4lite.py`、`setpoint.py`、`params_px4.py`、`profiles.py` | 由 g08 `fleetsim_g08.py` 迁移的 PX4-lite L1 numpy oracle（位置/速度环、推力矢量、姿态、电机一阶滞后、composite/linear 气动）；refgen（GOTO 平滑、STOP_MOTION、TAKEOFF SPOOLUP+CLIMB、LAND 剖面、RTL 阶段、HOLD）；内置 x500_sih、x500、p600_mid360 参数与 3 套限速配置 | M08-FR-016 至 FR-028、FR-032、FR-034、FR-035、FR-043、FR-045、FR-046 |
| `fleet/pipeline.py`、`fleet/fleet.py`、`fleet/stages/*` | Pipeline.build（重名、order 区段、一字段一写者、Σbudget ≤ 0.40）；内置 stage：clock、ingest（apply_tick 复核）、l1（125 Hz）、contact（DSM/DTM 或水平面：着地、碰撞、触地事件）、fsm_min（兜底 FSM，M09 登记后自动停用）、cmd_watch（50 Hz 完成判据）、tap（Full64 + Lite32 零拷贝发布，快进节流 FASTFWD） | M08-FR-012 至 FR-014、FR-036、FR-050、FR-057、FR-060 |
| `fleet/stages/registry.py`、`budgets.py`、`core/metrics.py`、`core/interfaces.py` | 冻结签名的扩展点：`@register_stage(name, every, phase, order, *, owner, fidelity, budget_core, writes, reads, shards)`、`register_state_block`、`register_admission_check`、`register_slow_task`、`register_query`、`register_energy_model`、`register_safety_hooks`、度量注册表、EnergyModel/SafetyHooks/MotionProvider 协议；`isolated_registry()` 测试隔离 | M08-FR-011、FR-012、FR-055、FR-088、FR-090；M08-AC-003 |
| `core/state_model.py` | g04 状态模型转正（FS/SUB、custom_mode 双射、derive_px4、derive_prometheus、mock_emulate_px4、准入矩阵 `admission_matrix()`，打包复用 contracts `pack_fs/pack_ctrl`） | M08-FR-049、FR-052、FR-069、FR-070；M00-B-to-M08 第 1 条 |
| `core/command.py`、`admission.py`、`authority.py`、`roster.py`、`fuser.py`、`supervisor_queue.py` | CommandEngine：principal 验签、准入 ④ 生命周期 + 矩阵 + 登记检查、⑤ 租约、⑥ 参数边界（含世界 bounds）、⑦ caps 与骨架覆盖、⑧ 登记检查；accepted（apply_tick = 下一 tick）→ running → succeeded/failed/timeout，effect 与 metrics；幂等 60 s / 4096；取代 206；cancel；`register_motion_provider`；`submit_internal`、`subscribe_results`；LeaseManager 与操作席位；Roster（agent_no、roster_version、Mock 生命周期 PENDING → STARTING → BOOTED → READY）；Full64 字段融合；SupervisorQueue（SafetyActuator） | M08-FR-015、FR-054 至 FR-058、FR-062、FR-067、FR-086、FR-089、FR-090；M08-AC-025、AC-026、AC-028 |
| `backends/base.py`、`mock.py` | 冻结的 SimBackend/EntityAdapter/DroneAdapter 协议、BackendCaps（读 `rt/caps/*.json`）、MockBackend 最小实现 | M08-FR-065、FR-066 |

### 1.4 配置与入口

`configs/runtime.yaml` 已含 `sim-core`（`python -m awr.sim.runtime`，plugins 列表）与 `api`（`uvicorn awr.api.main:app --loop uvloop --ws websockets …`），
本工作包未改动；`make run` → supervisor 启动二者并打印 READY（验证见第 3 节）。

## 2. 偏差与原因

1. **WS 路径为 `/api/rt`**：任务书写作 `/rt`，而 17 §6.2、vite 代理（M00-to-M11 第 4 条）与前端均为 `/api/rt`，以契约为准。
2. **骨架命令集**：CommandEngine 只实现 takeoff、goto、hover、land（`at = here`）、rtl、cancel；其余命令在第 ⑦ 步返回 `109`（detail
   `SKELETON_UNIMPLEMENTED`），M10 经 `register_motion_provider` 登记的命令同样放行；api 侧 `confirm/issue`、`rec/*`、`seat/takeover` 返回 109。
   原因：任务要求不实现超出 skeleton 的功能。
3. **隐式 OPERATOR 租约**：席位持有者对租约为 NONE 的机体下发运动命令时隐式取得 OPERATOR 租约（发 `lease.acquired`）。原因：
   骨架前端没有显式 acquire 流程；显式 `uav/{id}/cmd/acquire`/`release` 也已接通。
4. **兜底 FSM `fsm_min`**：M09 未交付前由 M08 以最小 FSM 写 `safety` 兜底块（无 PREFLIGHT、无 failsafe 逻辑）；M09 登记 `fsm` stage 或
   `safety` 状态块后自动停用。
5. **只有 numpy oracle、内置参数表**：numba 融合核（M08-FR-038/040）、`vehicles/*/params.yaml` 加载（FR-042）在 MS4；`AWR_KERNEL`
   暂不生效，`sim.started.kernel` 与 `perf/server.sim.kernel` 报 `numpy`。1 架飞行时约 1.2 ms/tick（未做性能测试，只作观察）。
6. **`EnuViews.omega_flu_slots(slots)`**：M08-FR-087 同时要求属性 `omega_flu` 与切片函数 `omega_flu(slots)`，Python 中同名冲突，切片函数改名。
7. **`EventPublisher.emit(fields=...)`**：在 M11 运行时库新增仅关键字参数（向后兼容），解决 data 键与形参 `kind` 同名的问题。
8. **组合根容忍缺失插件**：`AWR_PLUGINS` 中尚未交付的包（当前 `awr.environment.stage`）告警并跳过，列入 `sim.started.data.plugins_missing`；
   严格失败会使并行阶段无法运行骨架。
9. **state_ext 总线载荷**：`state/sim-core/ext` 为 msgpack `[[agent_no, state_ext], ...]`，尚未在 `bus/` 登记（已请求 M00）；state_ext 只含
   `uav_state_ext.schema.json` 的字段（M08-FR-051 中的附加字段需先登记）。
10. **席位起始时间**：`bus/command.schema.json` 的 leaseReply.seat 不含起始时间，409 `116` 的 `holder_since_unix_ns` 以 api 首次观察时间代填（已请求 M00）。
11. **`fleet/roster` 的 lifecycle 为快照**：roster_version 只在增删时变化（M08-FR-015），逐机当前生命周期以 state_ext 与 `sim.vehicle.state` 为准；
    测试按此等待 READY。
12. **OpenAPI**：`/api/openapi.json` 在 demo profile 关闭，其余 profile 开放；17 §4.1 第 10 条"生产需 admin token"简化为关闭。
13. **api 停机退出码**：uvicorn 优雅关闭后会重新抛出捕获的 SIGTERM，api 退出码为 −15（supervisor 记 `reason: signal`）；lifespan 清理已完成，属 uvicorn 行为。
14. **世界摘要只在 api 启动时读取**：`serverInfo.world` 与 sessions/current 的 content_version 不随运行中重建刷新（会话期间世界锁定，AWR-12 §3.3.4）。

未在骨架中实现（后续里程碑）：兴趣集下推 `ctl/sim-core/interest` 与 DetailDemux（safety、sensor）、GCS 心跳 5 Hz、按 principal 限流（M11-FR-026）、
环境关键帧缓存、CLIENT_DATA（322）、回放（213）、REST 命令镜像 `/api/commands*`、R47–R49、R54、R55、R60、REST `Idempotency-Key`、CSP、CORS、
L4 拥塞的"连续 3 次 > 50 ms"判据、token 撤销 4403、断线席位 GRACE、会话切换、停机期间新 call 返回 213、ReplaySource、checkpoint、批量命令、估价。

## 3. 测试结果

| 用例 | 结果 | 耗时 |
|---|---|---|
| `tests/rt/test_skeleton_chain.py::test_chain_inproc`（token → WS 握手 → hello resume → subscribe → BATCH SNAPSHOT/epoch/roster 在前/Lite32/Full64 → state_ext 与 roster 载荷 schema 校验 → takeoff 5 m 与 goto 10 m succeeded 且位置误差 < 1 m → 重复 cid `duplicate` → 越界 110 → 退订 → 全部控制消息按 ops.schema.json 校验） | 通过 | 约 18 s |
| `test_viewer_call_rejected`（115、未知 op 313、非法 topic 314） | 通过 | — |
| `test_ws_denials`（Origin 403 `303`、子协议 400 + `AWR-Supported-Protocols`、token 302 + 4401、主版本 311 + 4426、hello 超时 317 + 4408、hello 前 300） | 通过 | — |
| `test_rest_and_static`（health、token/whoami、worlds 列表分页与详情、R07 经总线到 GeoProbeServer、sys/info 两级、sessions、Host 400 `323`、Origin 403、problem+json schema、world.json ETag/304、`?v=` immutable/409、Range 206/`bytes=-n`/416、隐藏路径 404、SPA 回退与 `/assets` immutable、COOP/COEP/CORP） | 通过 | — |
| `test_openapi_lists_routes` | 通过 | — |
| `test_chain_real_processes`（supervisor ci profile `--only sim-core,api`，随机端口与汇合点：READY → health ready → operator token 占席 → WS → BATCH → takeoff succeeded → SIGTERM 退出码 0） | 通过 | 约 14 s |
| `tests/sim/test_skeleton.py`（9 例：扩展点冻结签名、stage 登记校验与分片、单例与度量、准入矩阵、SimClock、假墙钟单步驱动 takeoff → goto → land 与拒绝码 108/115/116/110/109/NO_VEHICLE/验签、StateRing Full64 与内部状态一致、clock 服务与心跳、登记 stage 与准入检查生效、运动提供者扩展命令集） | 9 通过 | 约 14 s |
| 回归：`tests/runtime`、`tests/contracts`、`tests/world_query`（`-m "not perf and not slow"`；含 `test_state_model.py::test_m08_state_model_if_present`） | 483 通过、1 跳过 | 129 s |
| `make lint` 的 Python 部分（`ruff check python tools tests`）与 `make lint-tools`（no-emoji、no-hex、lint-lf、motion、icons、raw-controls、brand、deps、units、py-imports、py-callbacks、perf-flags） | 全部通过 | — |

手工验证：`python -m awr.runtime.supervisor --profile demo`（全部进程，随机端口）打印 READY，`/api/health/ready` 200，`GET /world/shenzhen` 200 text/html；
`python -m awr.api.main` 单独运行时 live 200、ready 503（sim down）；`python -m awr.sim.runtime` 单独运行可启动并响应 SIGTERM。
按并行阶段约束未运行性能基准与 perf 标记用例。

## 4. 遗留问题

1. 前端部分的 walking skeleton（M11-AC-041、M08-AC-038 的 Playwright `perf/skeleton.spec.ts`：点云上屏、无人机上屏、goto 闭环）依赖 `apps/web/src/net/**`
   的 rt.worker 与 RtClient（M15-to-M11 第 1 条、MS12-to-M11 第 1 条的 FakeSource），不在 SK-B 范围，需前端工作包交付；后端协议已按契约就绪。
2. numpy oracle 下 1 架飞行约占 0.3 核；N 较大时需要 MS4 的 numba 融合核与 `fsm`、`guard` 等 M09 stage，Σbudget 校验已就位。
3. `cmd.progress`、`geo.ready`、`sim.contact.*` 尚未登记到 `bus/event.schema.json`（已请求 M00）；在登记前 Gateway 照常转发。
4. `sys/procs` 在未受监管（`--inproc`、单独运行）时返回 503 `213`；`sys/procs` channel 只在受监管时发布。
5. `fake_gw` 仍自带最小协议实现（M11-R-to-M11 第 6 条），尚未改为复用 `awr/api/rt`；`awr status --ws`（同第 7 条）未接入。

## 5. 跨模块请求与回复

新写请求：

- `.cache/impl/requests/SK-B-to-M00.md`：OpenAPI 快照导出方式（回复 M00-B-to-M11 第 6 条）；登记 `cmd.progress`、`sim.contact.collision`、
  `sim.contact.touchdown`、`geo.ready`；登记 `state/sim-core/ext` 载荷；leaseReply.seat 增加 `since_unix_ns`；roster lifecycle 语义说明。
- `.cache/impl/requests/SK-B-to-M15.md`：`/api/worlds` 为 `{items, next_cursor}` + snake_case 且需 viewer token（回复 M15-to-M11 第 2 条），token 签发与 WS 入口。
- `.cache/impl/requests/SK-B-to-M07-M09-M10-M13.md`：扩展点签名、order 区段与预算、M09 接管兜底 FSM 的条件、M10 运动提供者放行规则、M07 风场写入、M13 视图读取。

对收到请求的处理：

| 请求 | 处理 |
|---|---|
| M04-to-M11 | 第 1 条：`world_query.router` 经自动发现挂载；第 2 条：`app.state.bus`、`app.state.session_world_id`、`request.state.principal` 已提供；第 3 条：`/api/worlds` 用 Catalog，静态服务不暴露点目录；第 5 条：READY 与 `/world/<id>` 已通 |
| M04-to-M08 | 第 1、2 条：`open_world_query` + `GeoProbeServer`，`svc_geo(height|ray_hit)` 入队、慢任务 `run()`、停止 `close()`、`geo.ready` 事件；第 3 条：contact 读 `height_dsm`/`ground_dtm`；第 4 条（active_zone_ids）待 M10 剧本 zones |
| M11-R-to-M11 | 第 1–5 条已按约定接入（init_child、ZenohBus.open(ctx)、hb.api 10 Hz、EventSubscriber、StateRing 读者、sys/procs）；第 6、7 条见遗留问题 5 |
| M11-R-to-M08 | 第 1–4 条已按约定实现（`__main__`、AWR_PLUGINS、启动序列、心跳、零拷贝发布、set_step_stats、inbox + 步顶 drain、EventPublisher）；第 5 条 checkpoint 为 ext 未做；第 6 条 SIGUSR2 由 init_child 处理 |
| M00-B-to-M08 | 第 1 条 `admission_matrix()` 已提供，契约用例通过；第 4、5 条 bus_keys 与 Reason 全部取自契约 |
| M00-B-to-M11 | 第 1–5 条按生成代码接口使用；第 6 条见 SK-B-to-M00 |
| M00-to-M11 | 第 4 条 WS 路径 `/api/rt` 一致；第 1 条 supervisor 入口未变 |
| MS12-to-M11 | 第 2 条 `/world/<id>` 可访问；第 3 条 READY 横幅降级提示已实现；第 1 条（FakeSource）属前端工作包 |
| M15-to-M11 | 第 2 条见 SK-B-to-M15；第 1、3 条（net/rt 客户端、stores/fleet）属前端工作包 |
