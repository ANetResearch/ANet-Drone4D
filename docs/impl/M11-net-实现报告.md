# M11-net 前端实时客户端（net/rt、net/api、stores/fleet）：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | M11-net：`apps/web/src/net/**`、`apps/web/src/stores/fleet.ts`、`apps/web/tests/net/**`、`apps/web/tests/m11/**`、`apps/web/perf/m11/**` |
| 日期 | 2026-09-29 |
| 依据 | M11 PRD §4.13（FR-089 至 FR-098）、§5.1（NFR-013、015、016、022）、§6.3.8、§6.4.14、§6.4.15、§6.5、§6.6.3、§7.5、§8、§9.1、§9.5、§10.2（AC-008、012、014、015、021、024、036、040、046、047）；AWR-17 §3.2–§3.4、§6.2–§6.13、§7.2–§7.5、§8.3；AWR-14 §7.6–§7.9；AWR-10 §6.5、AD-06；AWR-03 §3.6、§4.3、§8.4（D1-AC-20、D1-AC-35）；AWR-18 §8.6（`source=fake`）；前置报告 SK-F、SK-B、SK-E2E、M11-R、M15-S |
| 结论 | D1-core（P0）的前端部分全部实现并通过功能测试：rt.worker 完成关闭码全表（含 1006 whoami 判别、4401 重签、4403 降 viewer、4429、1013、1009、1002×3、1008、4426）、纪元规则、ack 带 fps/decodeMs/lagMs、ClockSync 主线程时基、槽零分配（两侧常驻镜像，每槽 1 个视图）、1 万帧模糊输入不越界、隐藏页事件上限 8192 与本地 GAP、订阅 rate 按 `subscribed` 追踪；RtClient 全 API（含 callBatch、publishSetpoint、playback、reauthenticate、onEventGap、onError、连接信息）；FakeSource 重构为共享 FakeWorld（重连保持会话、resume、`duplicate`、批量、起降、遥操作看门狗、测试钩子）并可驱动 flight60 `scene=full&source=fake`；`stores/fleet.ts` 增补 DroneRail 所需字段。D1-ext 中本路径可做的 `playback` 客户端接口已实现（网关侧为 D1-ext）。另以 Node 全局 WebSocket 对真实网关（`python -m awr.api.inproc`）跑通 RtClient 全链路 |

## 1. 实现清单

### 1.1 `apps/web/src/net/rt/`

| 文件 | 内容 | 需求 |
|---|---|---|
| `types.ts` | 公开类型：`ConnInfo` 增 `error`（version、auth、backlog、seat_revoked、conn_limit、protocol、policy、too_big，`CONN_ERROR_UI` 映射 E-07/E-08/E-09/E-16/E-17）、`sinceMs`、`lastLiveMs`；`RtClient` 增 `callBatch`、`publishSetpoint`、`playback`、`onPlaybackState`、`onEventGap`、`onError`、`connInfo`、`setClientStats`、`setHidden`、`reauthenticate`；`BatchOp`/`BatchCounts`/`BatchSummary`、`PlaybackState`、`EventGap`、`ClientStats`、`RtErrorMsg`；`SwarmSnapshot` 增 `vel`、`quat`；`ServerInfoView` 增 `principal`、`capabilities`；槽头视图增 `timeRecvMainMs`、`malformedFrames` | FR-094；M11 §7.5；14 §7.6、§7.7 |
| `frame.ts` | 槽布局按 §6.3.8 表逐项；保留区新增 `timeRecvMainMs`（216）与 `malformedFrames`（224）；`SlotWriter` 为 Worker 常驻镜像（视图只建一次），`copyTo()` 以一次 `set` 拷入空闲可转移槽；`FrameFront` 为主线程常驻镜像，`load()` 拷入后刷新头部，视图对象恒定 | FR-090、FR-095；NFR-016 |
| `decode.ts`（新） | `ChannelTable`：advertise/unadvertise、按 `subscribed{id, channels, rate, added}` 维护每订阅通道并在退订后重算通道 rate、roster 更新 channel→agent；`Decoder`：每 channel 只记最新记录引用、RESET 通道去重、截断或不一致帧 try/catch 计为 malformed 并保留此前记录；1 s 窗口统计 swarmHz、selHz（60 Hz 通道逐通道频率最大值）、focusHz（30 Hz 通道逐通道频率中位数，插入排序、零分配）、bytesPerS；`compose()` 解码进常驻镜像；`takeData()` 在无帧循环时只把 msgpack/json 通道送上控制路径 | FR-089、FR-090、FR-096；§6.3.8 |
| `ctrl.ts`（新） | 控制消息批：事件上限 8192，超出丢最旧并记录丢弃的 seq 区间与条数，下一批首条 `localGap{fromSeq, toSeq, dropped}`；隐藏页只交付非事件消息 | FR-095；§6.4.14 第 3 条 |
| `session.ts`（RtHost，重写） | 连接状态机（§6.6.3）与关闭码全表（17 §8.3、14 §7.7）；1006 以 `checkAuth`（whoami）判别 401/403；4401、4403、whoami 401 请求主线程重签 token 后立即重连（不计退避，连续 2 次失败 FATAL auth）；serverInfo 在同一连接内重发不再二次 hello；sessionId 变化清空通道表、引用、ClockSync 与事件序号，同会话带 resume；每条连接重新 5 次 100 ms 的 ping 爆发；TIME 到达即处理，新纪元 BATCH 暂存一帧、旧纪元丢弃；ack 以归还槽 FIFO 记录的 frameSeqMax 为已消费（3 帧或 50 ms，带 fps、decodeMs、lagMs）；槽头 ageMs 只在时钟推进且本槽有新数据时产生；clientStats 1 Hz；隐藏页控制消息 4 Hz、事件留在 Worker；无帧循环时 msgpack 通道经 50 ms 定时器送达；CLIENT_DATA（每机客户端 channel 1–255、预分配 32 B 帧、final 后 unadvertise、simNow 估计）；playback 透传；断线时 cancel 延到重发调用之后；`reauth` 以新 token/角色立即重连 | FR-089、091、092、093、095、100；AC-008、014、021、024、036、047 |
| `rt.worker.ts` | 绑定 RtHost；`fake:` URL 用 `openFake()`（同一 URL 的连接共享 FakeWorld）；`checkAuth` = `whoamiStatus()` | FR-089 |
| `transport.ts` | 增 `httpUrlOf()`、`whoamiStatus()` | FR-093；17 §3.3 第 4 条 |
| `client.ts`（RtClient，重写主体） | `swapFrame()` 返回常驻 `FrameFront`，上一槽随下一次 pull 归还（ack 锁定渲染节奏），每秒把 swapFrame 频率作为 fps 报给 Worker；`rt.swarm` 为常驻镜像视图（无拷贝）；订阅引用计数、250 ms 合并、未发出的订阅在窗口内释放则不发送；事件在帧循环运行时每帧至多一批（swapFrame 时交付，100 ms 兜底）；`connected` 时清空 status 横幅；`needToken` 调 `getToken(role, {force: true})`；`callBatch`、`publishSetpoint`、`playback`（request_id 匹配、10 s 兜底 213）、`onEventGap`、`onError`、`statusList()`、`reauthenticate()`、`setHidden()`（自动监听 visibilitychange）、`setClientStats()`；`resolveRtUrl` 支持 `fakeGround`、`fakeDrop`、`fakeSeed` 与 flight60 `scene=full` 缺省 2 架；`fakeUrl()`、`createFakeRtClient()` | FR-094、FR-098；10 §6.5 |
| `batch.ts`（新） | `batchSummaryOf()`、`batchCountsOf()`、`isFinalResult()` | FR-061 客户端侧；14 §6.11 |
| `FakeSource.ts`（重构） | `FakeWorld`（网关 + 仿真，按 URL 共享）：N ≤ 1024 的 Lite32/Full64/roster/state_ext/EnvKeyframe/perf/sys、60 Hz 网格、TIME 10 Hz 与全局 epoch、事件环 4096（resume 补发、超出环置 GAP 与 `status events.gap`）、调用幂等表（同 id 重发回 `duplicate: true` 并把后续结果改投新连接）、goto/takeoff/land/rtl 运动学与 accepted→running→progress→succeeded、`fleet/cmd/{op}` 批量（汇总 result、≤ 2 Hz counts、`fleet.batch.progress`、全部终态后 final，子调用不逐条发事件）、velocity + CLIENT_DATA（body/world、vmax、hold_alt、250 ms 看门狗 209）、cancel、playback 回 213、`ground`（地面出生，须先起飞）、`dropEveryS`；测试钩子 `bumpEpoch()`、`restart()`、`stalled`、`emitStorm()`、`setStatus()`/`removeStatus()`、`closeAll()`。`FakeSource` 为单连接：握手、按连接的订阅调度（发送时装帧、credit、SNAPSHOT、事件过滤 types/levelMin）、客户端 advertise 与 CLIENT_DATA、`received` 记录；`.awrrt` 回放不变 | FR-098；AC-040；D1-AC-35 |
| `index.ts` | 门面导出增补 | §7.5 |

### 1.2 `apps/web/src/net/api.ts`

`getToken(role, {force})`（4401/4403 后强制重签；缓存按角色命中，operator 请求在已降级为 viewer 时返回 viewer 缓存）、`invalidateToken()`、`whoami()`。原有 `apiGet`/`apiPost`/`worldQuery` 与 SK-E2E 的等待 token、401 重试、无网关退避不变。

### 1.3 `apps/web/src/stores/fleet.ts`

`FleetSummary` 增 `inAir`、`batteryUnknown`、`stale`、`rosterVersion`、`tSimMs`；`fleetRows` 增 `flags`，`stale` 列按"链路非 LIVE 或 swarm 超过 `input.staleAfterMs` 未更新"填写；新增 `fleetModelOf`、`fleetRowOf`（Int16 查表）、`fleetRowOfId`、`resetFleetSummary`；`summariseFleet(src?, nowMs?)` 可注入数据源；写 store 的条件增加链路状态与陈旧度变化；overlay 相位 Tier S 4 Hz、B/A 10 Hz 不变；改为 import `@/engine/loop`（只依赖相位注册）。原字段与语义不变（M15-to-M11 第 3 条）。

### 1.4 测试与用例

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/m11/wire.ts` | — | 脚本化 socket、TIME/BATCH/serverInfo/advertise 构造、手动时钟 RtHost 台架 |
| `tests/m11/frame.test.ts` | 4 | §6.3.8 偏移逐项、区域、保留区新增字段、SlotWriter→FrameFront 往返、视图对象恒定 |
| `tests/m11/decode.test.ts` | 5 | Lite32 N = 1000 进 SoA（生成的缩放）、Full64/EnvSample32/SensorPose48 的 agent/channel/seq/样本时刻、未知通道与编码按长度跳过、每通道最新值与 frameSeqMax、RESET 去重、**1 万帧模糊输入不抛出**且之后正常解码（AC-036） |
| `tests/m11/epoch.test.ts` | 5 | 新纪元 BATCH 暂存与放行、旧纪元丢弃计数、纪元切换清空引用、u16 回绕、FakeWorld bumpEpoch 时 TIME 先于 SNAPSHOT（AC-021 客户端部分） |
| `tests/m11/transport.test.ts` | 22 | 退避形状与上限、子协议、whoami URL；1001/1011/4408/1000 退避重连、退避递增与握手后复位、1013/4429/1009 分类、4401 立即重连、连续 4401 FATAL auth、4403 viewer 与 hello.role、1008/4426 FATAL、1002×3 FATAL、1006 whoami 403/401/200、布局哈希与主版本不符自关、4 s 连接超时、close 与 reconnectNow、同连接内 serverInfo 不二次 hello、reauth（AC-008、047） |
| `tests/m11/clockSync.test.ts` | 5 | 最小 RTT、跳变/平滑、srtt EWMA、16 样本；**Worker 与主线程 timeOrigin 不同时 off_main 误差 ≤ 1 ms**；抖动链路 p95 ≤ 2 ms；5×100 ms 后 2 Hz 与 srttMs；新 sessionId 清空、同会话保留（AC-024、036；NFR-022） |
| `tests/m11/ack.test.ts` | 5 | 60 Hz 下至多每 3 帧一次 ack 并带 fps/decodeMs/lagMs、10 Hz 下 50 ms 规则、ping 带 srttMs、无 pull 时 credit 窗口停发与恢复无突发、clientStats 1 Hz 字段（AC-014） |
| `tests/m11/hidden.test.ts` | 3 | 隐藏页 60 s、200 条/s：事件 ≤ 8192、localGap 区间、GAP 槽标志、结果照常送达、ping 持续；无帧循环时事件与 roster、msgpack 通道经控制路径送达（AC-036） |
| `tests/m11/alloc.test.ts` | 1 | **`--expose-gc` 下 N = 1000 解码 1 万帧，保留堆增长 ≤ 64 KiB**（AC-036；NFR-016） |
| `tests/m11/fakeworld.test.ts` | 13 | 同会话重连：resume 后事件 seq 连续无重复、在途 goto 同 id 重发得 `duplicate` 后成功；api 重启：新 sessionId、无 resume、订阅重放；事件环溢出后 GAP 与 events.gap；批量 hover/rtl（汇总、rejected_by_code、counts、fleet.batch.progress、降落到 home）与 110；地面出生 105→起飞→goto→降落；velocity + CLIENT_DATA 与 322、看门狗 209；cancel 6；playback 213；周期断线；status 重发；flight60 URL 映射 |
| `tests/m11/client.test.ts` | 12 | RtClient（进程内 Worker、真实计时）：订阅引用计数与 250 ms 合并、降 rate 同 id 重订、全部释放退订、窗口内订阅即释放不发送；常驻帧与 swarm 视图；onTime recvMs；goto 结果序列与 progress、迟到 onResult；callBatch；未初始化与断线 213；playback；每帧至多一批事件；重连后横幅对账与连接时间戳；4403 取 viewer token；setpoint 与 onError 322；onEventGap |
| `tests/m11/fleet.test.ts` | 4 | 摘要计数、告警、电量、行查表；只在变化时写；缩小机群清除旧查表；FakeSource N = 1000 端到端（AC-046 功能部分） |
| `tests/m11/fleet.bench.ts` | — | AC-046 性能部分（Vitest 5 基准 API，p99 ≤ 1 ms；性能协议下执行，本阶段未运行） |
| `tests/m11/gateway.integration.test.ts` | 1 | 设置 `M11_GATEWAY` 时对真实网关跑握手、LIVE、roster、swarm、perf/server 含本连接、takeoff、批量、客户端断开后同会话重连（默认跳过） |
| `tests/net/harness.ts`（更新） | — | 同一台架的重连共享 FakeWorld；`checkAuth`、自定义 socket；`frames()`、`rng()` |
| `perf/m11/fakesource.spec.ts` + `staticServer.ts` | 3 | 浏览器真实 Worker：FakeSource N = 200 LIVE 与 serverInfo（视口正常时再查 roster、DroneRail、`__perf.net`）、每 3 s 断线→RECONNECTING→LIVE、flight60 `scene=full&source=fake` |
| `perf/m11/handshake.spec.ts` | 3 | 真实网关：WS 升级选 awr.rt.v1、不回显 bearer、不协商 permessage-deflate；缺子协议 400 + AWR-Supported-Protocols；外源 Origin 403；crossOriginIsolated 与 LIVE（`M11_BACKEND=1` 或 `SKELETON_API` 时运行） |
| `perf/m11/backpressure.spec.ts` | 1 | AC-015 客户端侧：主线程每帧忙 70 ms、8 s，`__perf.net.ageMs` p95 ≤ 150 ms、漂移 ≤ 20 ms（`AWR_PERF=1`，性能协议下运行） |

## 2. 需求与验收对应

| 编号 | 结果 | 证据 |
|---|---|---|
| M11-FR-089（唯一 WS、只记引用、TIME 即时、纪元暂存） | 实现 | decode、epoch 测试 |
| M11-FR-090（3 槽可转移环、归还后解码） | 实现；视图改为两侧常驻镜像 | frame、alloc、client 测试 |
| M11-FR-091（ack 3 帧或 50 ms，fps/decodeMs/lagMs） | 实现 | ack 测试 |
| M11-FR-092（ClockSync、srttMs、主线程时基） | 实现 | clockSync 测试（≤ 1 ms、p95 ≤ 2 ms） |
| M11-FR-093（重连、resume、同 id 重发、关闭码表、whoami） | 实现 | transport、fakeworld 测试；真实网关集成测试 |
| M11-FR-094（RtClient 门面） | 实现并增补批量、遥操作、回放、错误、事件缺口、重新鉴权 | client 测试 |
| M11-FR-095（零分配、隐藏页 8192 + GAP） | 实现 | alloc、hidden 测试 |
| M11-FR-096（`__perf.net` 数值经槽头） | 槽头全部字段填写（新增 timeRecvMainMs、malformedFrames）；回填由 M06 | decode、clockSync 测试 |
| M11-FR-097（stores/fleet.ts） | 实现 | fleet 测试 |
| M11-FR-098（FakeSource） | 实现（共享 FakeWorld、`createFakeRtClient` 提供与 RtClient 同接口的替身） | fakesource、fakeworld 测试；浏览器冒烟 |
| M11-FR-100（布局哈希不一致 1000 自关、E-07） | 实现 | transport 测试 |
| M11-AC-008（客户端部分） | 通过 | transport 测试 |
| M11-AC-012（客户端引用计数与合并） | 通过 | client 测试 |
| M11-AC-014（客户端 ack） | 通过 | ack 测试 |
| M11-AC-015 | 用例已写（客户端侧），待性能协议下执行 | backpressure.spec.ts |
| M11-AC-021（客户端纪元规则） | 通过 | epoch 测试 |
| M11-AC-024（客户端 ClockSync） | 通过 | clockSync 测试 |
| M11-AC-036（模糊、纪元、时基 ≤ 1 ms、1 万帧堆 ≤ 64 KiB、隐藏页） | 通过 | decode、epoch、clockSync、alloc、hidden 测试 |
| M11-AC-040（FakeSource N ∈ {1, 200, 1000}、回放 `.awrrt`、同接口、无后端 flight60 `scene=full`） | 通过（flight60 驱动本身属 M06/M16，`source=fake` 数据面已就绪） | tests/net、fakeworld、fakesource.spec |
| M11-AC-046（功能部分） | 通过；性能部分用例已写 | fleet.test、fleet.bench |
| M11-AC-047（客户端部分：≤ 3 s 重连、同 cid 重发得 duplicate） | 通过（FakeSource 与真实网关各一例） | fakeworld、gateway.integration |
| D1-AC-35（TS 部分） | 通过 | `make test-fixtures` 的 Vitest 部分 |
| D1-AC-20 | 本包路径全部通过 | 见第 3 节 |

## 3. 测试结果

| 命令 | 结果 |
|---|---|
| `npx vitest run --project unit tests/net tests/m11 tests/contracts/frame.test.ts` | 15 个文件通过、1 个跳过（集成测试需 `M11_GATEWAY`）；113 例中 112 通过、1 跳过 |
| `npx vitest run --project unit`（全量回归，最终代码） | 59 个文件 611 例：608 通过、1 跳过（本包集成测试，需 `M11_GATEWAY`）、2 失败均为 M06 并行新增的在途用例（`tests/m06/deviceClass.test.ts` 设备档启发式、`tests/m06/constraints.test.ts` 指针事件守卫），不 import net/stores，与本包无关；较早一次全量为 46 个文件 514 例全部通过 |
| `M11_GATEWAY=http://127.0.0.1:8067 npx vitest run --project unit tests/m11/gateway.integration.test.ts`（对 `python -m awr.api.inproc --port 8067 --n 2`） | 通过：握手与布局校验、LIVE、roster、swarm、perf/server 含本连接、批量汇总与 final（网关批量格式与前端解析一致）、客户端断开后同 sessionId 重连；takeoff 因机体已在空中返回 105（上一轮已起飞），属预期分支 |
| `M11_DIST=<私有测试构建> npx playwright test perf/m11 --project perf` | 3 通过、4 跳过（handshake 需后端开关，backpressure 为性能用例）；因第 5 节第 1 条，视口相关部分记为 annotation |
| `npx tsc -p tsconfig.json --noEmit` | 本包路径 0 错误（其他模块在途代码有错误） |
| `npx oxlint --type-aware src/net src/stores/fleet.ts tests/net tests/m11 perf/m11` | 0 错误 |
| `make lint` | 失败，但全部违规不在本包路径：ruff（python/awr/sim/**）、oxlint（engine/pointcloud、picking、mission、viewport/backend、tests/m15、tests/pointcloud/oracle）、lint-lf（viewport/overlay/ViewCube.tsx）、PY-CB-01 与 check_py_imports（python/awr/sim/**）；no-emoji、no-hex、motion-lint、check-icons、no-raw-controls、check-brand、check-units、check-perf-flags 全部通过 |

按并行阶段约束未运行性能基准、`fleet.bench.ts` 与 `backpressure.spec.ts`；上表时长与数值均为功能观察。浏览器冒烟使用 `VITE_AWR_TEST_SWITCHES=1 npx vite build --outDir <scratchpad>` 的私有构建，未改动共享的 `apps/web/dist`。

## 4. 与 PRD 的偏差及理由

| 偏差 | 理由与影响 |
|---|---|
| 槽视图"按槽缓存"改为两侧各一块常驻 64 KiB 镜像，槽到达时整体拷贝约 55 KB（每槽 1 个 Uint8Array 视图） | 可转移 ArrayBuffer 在对端是新对象，视图无法按槽缓存（SK-F 登记的例外）。拷贝 55 KB 约数微秒，换来：Worker 解码与主线程读取零分配（alloc 测试）、`TelemetryFrame` 与 `rt.swarm` 视图恒定可被 M06/M12/M15 长期持有、`rt.swarm` 不再逐帧拷贝。SharedArrayBuffer（V0.3）可去掉拷贝 |
| 槽头保留区新增 `timeRecvMainMs`（216）与 `malformedFrames`（224） | M12 §7.1 的 `onTime(t, recvMs)` 需要 TIME 接收时刻；模糊测试需要解析失败计数。保留区原定"写 0"，旧读者不受影响；请 M11 PRD 修订时登记 |
| ack 的已消费帧号由 Worker 按发出顺序记录（FIFO），不再读取归还槽的头部 | 读取需为每个归还槽新建视图；主线程严格按发送顺序归还（持有一槽、下一帧归还），FIFO 与读取等价且零分配 |
| `ack.fps`、`clientStats.fps` 取 RtClient 测得的 swapFrame 频率（每秒经 `stats` 报给 Worker），缺省时取归还槽频率 | Worker 只见到 pull，无法得知 rAF 频率 |
| 无帧循环（隐藏页、视口未挂载）时 msgpack/json 通道也经控制路径送达（50 ms；隐藏页 250 ms） | PRD 只在 pull 时解码；实测视口故障时 roster 与 env/state 全部停摆。raw 记录仍只在 pull 时解码，热路径不变 |
| 隐藏页由 RtClient 监听 `visibilitychange` 显式通知 Worker；隐藏时非事件控制消息仍以 4 Hz 送达 | PRD 以"不拉取"推断隐藏；显式信号区分"主线程忙"与"页面隐藏"，调用结果与状态在隐藏时仍及时 |
| 连续 2 次鉴权失败（4401 或 whoami 401，其间无成功握手）即 FATAL `auth` | 17 §8.3 只写"重签后重连"，未给上限；避免无限重签循环 |
| 同一连接内重发的 serverInfo 不再触发第二次 hello | 17 §6.3 规定模式、世界或 run 变化时重发 serverInfo，hello 必须是连接首条消息 |
| `callBatch`、`reauthenticate`、`onEventGap`、`onError`、`setClientStats`、`setHidden`、`statusList` 为 §7.5 之外的增补 | 分别服务 14 §6.11、§6.12、M11-FR-095、17 §6.3（error、clientStats）与 14 §7.7 的呈现需求；§7.5 原有签名不变（`onTime` 回调多一个参数，兼容） |
| FakeSource 的批量最终 result：全部成功为 `succeeded`，否则 `failed`，code 为 0，计数在 `data.counts` | 17 §7.5 未规定汇总终态的 status 与 code；真实网关实测同样以 `succeeded` 加 `data.counts` 收尾 |
| FakeSource 连接共享 FakeWorld（同一 Worker 内按 URL 缓存） | 重连语义（resume、`duplicate`、事件环）需要跨连接的服务端状态 |
| `stores/fleet.ts` 改为 import `@/engine/loop` | 只需要相位注册；避免把整个引擎门面（含他人在途模块）带进 store 与其单测 |

## 5. 遗留问题

1. **视口当前无法挂载**（他人在途改动）：`viewport/layers/drones.tsx` 以 `new Picker()` 无参构造，而 `Picker` 构造已读取 `deps.query`，页面加载即抛出并被 `M15-E004 viewport` 边界接住；帧循环、默认订阅集、DroneRail 与 `__perf.net` 在当前构建上无法做浏览器验证。已写入 M11-net-to-M06；`fakesource.spec.ts` 在视口恢复后自动覆盖这些断言。
2. 性能类未执行：AC-015（backpressure.spec.ts）、AC-037（Worker 解码 p95）、AC-046 性能部分（fleet.bench.ts）、AC-038/039（需 M16 编排与真实网关 N = 1000）。`bandwidth.spec.ts`、`weaknet.spec.ts`（依赖 `tools/bench/ipc/netem_proxy.py`，未交付）未编写。
3. `perf/server.clients[].fps` 在真实网关上为 0（网关未取 ack/clientStats 的 fps）；perf/server 的 SNAPSHOT 不含新连接（缓存值），前端已按"等待含本连接的样本"处理。已写入 M11-net-to-M11。
4. 签发 operator token 后从未连接 WS 的 principal 永久占席（SK-E2E-to-M11 第 1 条）属网关，未在本包处理。
5. Vitest 5 的基准 API 与 `vitest.config.ts` 的 `benchmark.include` 是否仍可用未确认（M11-net-to-M00）。
6. `resetChannelIds` 只给 channel id；M12 若需要按 agent 清环，可能需要槽中附 agentNo（M11-net-to-M12 第 2 条）。

## 6. 跨模块请求

### 6.1 新写（`.cache/impl/requests/`）

| 文件 | 要点 |
|---|---|
| `M11-net-to-M06.md` | Picker 构造导致视口崩溃（阻断）；TelemetryFrame 常驻、`rt.swarm` 视图与新列；槽头新字段；selHz/focusHz 口径与关注集订阅方式；`setClientStats`；`publishSetpoint`；无帧循环时 roster 仍送达 |
| `M11-net-to-M12.md` | `onTime(t, recvMs)`、`timeRecvMainMs`；`resetChannelIds` 零填充；`playback`/`onPlaybackState`；纪元规则与 `bumpEpoch` 测试钩子 |
| `M11-net-to-M15.md` | `ConnInfo` 新字段与 E-xx 映射、断线期间 213、事件桥与 `onEventGap` 补拉、横幅对账、`onError`、`callBatch` 与解析函数、`reauthenticate` 与 `getToken(role, {force})`、fleet store 新字段、`swarm.vel`、无后端开发开关与 `createFakeRtClient` |
| `M11-net-to-M16.md` | perf/m11 三个用例与开关（M11_DIST、M11_BACKEND、SKELETON_API、AWR_PERF、M11_GATEWAY）；`skeleton.server.ts` 增加 dist 参数；flight60 `source=fake` 映射 |
| `M11-net-to-M11.md` | 客户端新发送的 op 与字段、whoami 401/403 语义、布局校验的 9 个 schema、perf/server 首帧与 fps、SK-E2E 席位问题、`make test-rt` 纳入 tests/m11 |
| `M11-net-to-M00.md` | Vitest 5 基准约定；`make lint` 失败项的归属 |

### 6.2 来件处理

| 请求 | 处理 |
|---|---|
| M15-to-M11 | 第 1 条：ConnState 取值不变，`createRtClient`/`onConnState` 已实现并扩充连接信息；第 2 条：`apiGet`/`ApiError`/`getToken` 保持，另增 `getToken(role, {force})`；第 3 条：`FleetSummary`、`fleetRows` 原字段不变并增补；第 4 条：net/** 仍不 import react、stores、ui（oxlint 通过） |
| MS12-to-M11 | 第 1 条（FakeSource、rt.worker、RtClient）：已完成并增强，`make test-fixtures` 覆盖的 tests/net 与 tests/m11 全部通过；第 2、3 条属网关（已由 SK-B/SK-E2E 处理） |
| SK-F-to-M11 | 第 1–7 条的行为约定保留；第 2 条（槽视图例外）已由常驻镜像消除；第 8 条（conn_id 一致）实测一致 |
| SK-E2E-to-M11 | 第 2 条（api.ts 等待 token、401 重试、无网关退避）保留；第 1、4 条属网关（转 M11-net-to-M11） |
| M00-B-to-M11 | 第 2 条：只用 `@awr/contracts` TS 生成物（frame、time、layouts、reasons、commands、enums），无手写偏移；其余属 Python 侧 |
| M00-to-M11、M04-to-M11、M11-R-to-M11 | 均为 api 进程、运行时与 REST 事项，不在本包路径；本包已按第 4 条 `/api/rt` 路径与 vite 代理工作 |
