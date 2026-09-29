# SK-F 前端 walking skeleton（D1-MS3）：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | SK-F 前端 walking skeleton（D1-MS3 前端部分）。授权在 M11（`net/**`、`stores/fleet.ts`）、M05（`engine/pointcloud/**`、`viewport/layers/pointcloud.tsx`）、M06（`viewport/**`、`engine/{loop,perf,drones,camera,picking,mission,anim}`、`stores/perf.ts`）路径创建最小可用实现，另按任务书写 M12 `engine/time/**` 最小版 |
| 日期 | 2026-09-29 |
| 依据 | AWR-03 §3.5–§3.8、§4.3、§8.4（D1-AC-34、D1-AC-35、D1-AC-20）、§8.6 D1-MS3、§8.7、附录 B；AWR-10 §6.3–§6.6；AWR-16 §3.2、§4；AWR-17 §4.3.1–§4.3.2、§5、§6、§7；AWR-14 §6.7；AWR-18 §9；M05 §6、§7、§9；M06 §6.2–§6.12、§6.18；M11 §4.13、§6.3.8、§6.4.14、§6.6.3、§7.5；M12 §6.3–§6.4、§7.1；研究 g01-gap（§6.1–§6.4）、g02 `lod.mjs`、g01 `common.js`/`pool.js` |
| 结论 | 前端链路 `/world/shenzhen` → world.json 与 metadata → 首屏 BFS 前缀一次 Range → Worker 内 ANET_Q16 打包 → Tier S 经典 WebGLRenderer + AnetNodesHandler 单次 draw 上屏点云 → rt.worker 建立 `awr.rt.v1` 连接并订阅 → 1 架无人机进入无人机图层与 DroneRail → 视口点选地面（`POST /api/world/{id}/query {op: "ray_hit"}`）→ GoTo 按钮下发 `uav/{id}/cmd/goto` → accepted、running、succeeded，机体停在目标点上方 3 m 内，全程无 pageerror。FakeSource 与真实 WebSocket（fake_gw）两条数据源均已跑通；与真实后端（网关 + sim-core）的全链路用例已写好（`SKELETON_API=` 时运行），留待下一步联调 |

## 1. 实现内容

### 1.1 net（M11 路径，`apps/web/src/net/**`、`stores/fleet.ts`）

| 文件 | 内容 | 需求 |
|---|---|---|
| `rt/types.ts` | 公开类型：ConnState（8 态，编号即 `CONN_STATES` 下标）、RtEvent / StatusItem / CallResult / Effect 按 AWR-17 线上字段对齐、TelemetryFrame 视图、RosterView、ServerInfoView、SwarmSnapshot、`str()` 线上值取字符串 | M11 §7.5 |
| `rt/layouts.ts` | 只再导出 `@awr/contracts` 生成物（帧头、记录头、TIME、Lite32/Full64 访问器、`decodeSwarmLite32Into`、枚举、topic 表） | M11-FR-100 |
| `rt/frame.ts` | TelemetryFrame 64 KiB 槽布局逐字节实现（头部 256 B、swarm SoA 1024 架、Full64 区 64 项、小记录区 32 项、flags 7 位）、`SlotWriter`（Worker 侧）与 `SlotReader`（主线程侧） | M11 §6.3.8；FR-090 |
| `rt/clockSync.ts` | Cristian 最小 RTT（16 样本），> 50 ms 跳变否则 0.1 平滑，srtt EWMA 0.875/0.125，5 次 100 ms 后 2 Hz，`offsetMain = off_worker + (timeOrigin_main − timeOrigin_worker)` | FR-092 |
| `rt/transport.ts` | SocketLike 接口（WebSocket 子集，供 FakeSource 替身）、退避（500 ms 起 ×1.5、上限 10 s、±20%、连接超时 4 s、4429 固定 30 s）、子协议 `awr.rt.v1` + `bearer.<token>` | AWR-17 §6.2、§6.13 |
| `rt/session.ts`（RtHost） | Worker 内协议引擎：握手（serverInfo 布局哈希与 contracts 主版本校验，不一致 1000 自关 FATAL）、hello（含 resume）、订阅重放、TIME 到达即解码、新 epoch BATCH 暂存一帧由下一 TIME 裁决、旧 epoch 丢弃；BATCH 只记每 channel 最新记录引用；pull 时把 swarm 解码进槽 SoA、Full64 与 EnvSample32/SensorPose48 原样拷贝、msgpack/json（roster、env/state、state_ext、perf/server、sys/procs）解码后走控制路径；ack 以归还槽最大 `frame_seq`（≥ 3 帧或 ≥ 50 ms）；1 s 无 TIME 转 DEGRADED；关闭码处理（1000、1002×3、1008、4426、4401 换 token、4429、其余退避）；断线期间调用立即以 213 拒绝，在途调用重连后同 id 重发，调用首个结果超时合成 `timeout 200`；页面不拉取时控制消息 50 ms 后单独投递，事件缓存上限 8192 条后丢最旧并记 GAP | FR-089、090、091、093、095、100；AWR-17 §6.2–§6.13 |
| `rt/rt.worker.ts` | 把 RtHost 绑定到 Worker 全局；`fake:` URL 时创建 FakeSource（夹具 URL 先 fetch） | FR-089 |
| `rt/client.ts`（RtClient） | 3 个可转移槽的交换（`swapFrame()` 在 telemetry 相位：取就绪槽、归还上一槽、发下一次 pull）、订阅引用计数（同 topic 取最高 rate，增删合并后每 250 ms 至多发送一次）、`call()` 返回 CallHandle（`result` 为终态 Promise、`onResult` 依次收到 accepted/running/终态、`onProgress`、`cancel`）、事件每批一次回调、status 横幅、roster 与 serverInfo 视图、`onTime`、`onData`、swarm 快照（供机群摘要）；`?source=fake&fakeN=&fakeStart=` 切换到 FakeSource；无 Worker 环境以 `LocalPort` 进程内运行；`createRtClient()` 无参返回页面单例 | FR-094、098；10 §6.5 |
| `rt/FakeSource.ts` | Socket 形态的网关替身。合成：N ≤ 1024（D1 用例 1、200、1000）按生成的 SL32/DS64 偏移写 Lite32 与 Full64，roster、state_ext、EnvKeyframe（presets 生成物）、perf/server、sys/procs 用 msgpack；握手、订阅（通配、rate class、SNAPSHOT、别名 `swarm/state`）、60 Hz 对齐 tick 与发送时装帧、credit 窗口、TIME 10 Hz、`event`/`events`、ping/pong；命令替身按准入矩阵预判并真实执行 goto（accepted → running → progress ≤ 2 Hz → succeeded 带 metrics，取代旧调用 206，cancel 6）。回放：`.awrrt` 的服务端记录按时刻逐字节投递（可循环） | FR-098；M11-AC-040；D1-AC-35 |
| `rt/index.ts` | 门面 | M11 §7.5 |
| `api.ts` | `apiGet`、`apiPost`（problem+json → ApiError{status, reason, name, remedy, detail}）、`worldQuery`（R07）、`getToken(role)`（`POST /api/auth/token`，缓存到过期前 1 分钟，409 降为 viewer，无后端返回 ''） | AWR-17 §4.3.1、§4.4 |
| `stores/fleet.ts` | overlay 相位 Tier S 4 Hz、B/A 10 Hz 从 swarm 快照与 roster 就地写行数据并按需更新摘要；新增 `fleetIdOf(i)`、`summariseFleet()` | FR-097；M11-AC-046（部分） |

### 1.2 点云（M05 路径，`engine/pointcloud/**`、`viewport/layers/pointcloud.tsx`）

| 文件 | 内容 | 需求 |
|---|---|---|
| `params.ts`、`types.ts` | 7 档阶梯与 §6.10 默认值（唯一定义处）；world.json、metadata.json 子集类型 | M05 §6.8.1、§6.10 |
| `io/meta.ts`、`io/firstScreen.ts`、`io/hierarchy.ts`、`io/q16.ts` | 廉价检查（401）；规则 R；`hierarchy.bin` 22 B 记录按 Potree BFS 顺序解析（PROXY 拒绝）与 `hierarchy_ext.bin`；`packQ16`（ANET_Q16 → 池 texel 4 字交错）、`decodePoint`、oct16 解码 | FR-001..004；AWR-16 §4.2–§4.5、§4.11 |
| `io/fetchJob.ts`、`io/worker/fetch.worker.ts`、`io/fetcher.ts` | Worker 内 Range fetch（只带 `Range` 头）、切分并打包、可转移回传；在途上限、取消、0.5/2/8 s 退避 | FR-015、016、018 |
| `io/openWorld.ts` | world.json（no-cache）→ coordinate.json?v → metadata.json?v → hierarchy.bin?v（整体 GET）+ hierarchy_ext → 规则 R → 每根一次 `octree.bin?v` Range；TTFP 起点为最后一个首屏响应体到齐 | FR-001、003、004；AWR-17 §5.5 |
| `core/NodeStore.ts`、`core/Selector.ts` | SoA 节点表（float64 立方体、子树紧包围盒）；APH 选择器（g02 `selA` 移植 + 10% 迟滞，根节点同样裁剪并取真实键，首节点规则，零分配平行数组堆，图层帧 4 个侧面裁剪，`depthCap` 用于首帧目标集） | FR-010、011、012；ADR-009 |
| `gpu/PageAllocator.ts`、`gpu/PointPool.ts` | 256 texel 页首次适配与合并；池纹理 `dataReady=false` 只分配（无 CPU 镜像），staging + `copyTextureToTexture` 至多 3 个矩形；DrawTable（RGBA32UI 1024×4，按行 `addUpdateRange`）与 NodeTable（RGBA32F，每节点 2 texel） | FR-022、028；ADR-010 |
| `render/pointMaterial.ts` | TSL 单源：`vertexIndex` → DrawTable 二分 12 步 → 池取点 → NodeTable 立方体 → 图层帧位置；Lite 点径（childDrawnMask 缩一级、1.7·pitch·projK/(−z)）；Weyl 淡入哈希（节点内下标）；classMask 浮点位运算；Height 五档色带（场景 token）+ oct16 法线光照（faceforward）；隐藏点移到相机后方；全部整型参数用 float uniform | FR-029、031、032（Height）、033；ADR-011；g01 §6.3 |
| `PointCloudEngine.ts` | 生命周期；world 相位 `update`：相机到图层帧 → 选择（固定预算：Tier S B0 = 25k，测试构建 `?fixedB=`）→ 下载 → 上传（揭开前首帧目标集全量，之后 20k 点/帧）→ 池不足时驱逐 → DrawTable（淡入 `--duration-lod-fade`、`--ease-smooth-out`）→ uniform；自适应 maxPx（300 ms）；TTFP 在提交后记录；统计与事件 | FR-005（部分）、023、024（简化）、026、030 |
| `viewport/layers/pointcloud.tsx` | 薄适配（129 行）：按路由打开世界（非世界路由保留上一个）、注册 world/governor 任务与 LayerSpec、layers store 绑定、4 Hz 写 `stores/world.ts`、`__perf.pc`/`load.ttfp`、`firstFrame` 启动门、打开后相机回 world.json 的 home 位姿 | M05 §7.3、§7.5 |

### 1.3 视口与引擎（M06 路径）与时间（M12 路径）

| 文件 | 内容 | 需求 |
|---|---|---|
| `viewport/renderer.ts` | `createRenderBackend`：经典 WebGLRenderer（reversed-Z、`info.autoReset=false`）+ AnetNodesHandler；渲染器字符串匹配软件光栅 → Tier S/software，其余按家族启发式 iGPU/dGPU；`?tier=` 测试开关；起步档与最低允许档、DPR 规则；`TextureOps`、`createPointsMaterial()`（GLPointsNodeMaterial）；`renderFrame` 为唯一 `renderer.render` | M06 §6.2；ADR-044 |
| `viewport/anetNodesHandler.ts`、`glPointsNodeMaterial.ts` | g01 §6.2、§6.3 定稿实现（RT 线性输出、`scene.fogNode`；删除模板 `gl_PointSize = 1.0` 并改缓存键） | M06 §6.3 |
| `viewport/WorldCanvas.tsx` | async gl 工厂、`flat`、Tier S DPR 0.5、`frameloop="never"` + `advance(秒)`、唯一 priority-1 渲染订阅者、LayerMounts（registry → WorldRoot）、camera 相位写 FrameCtx（camera、css/drawing buffer、dpr）、`gpu.calls/passPlan/programs`、`warmup` 启动门 | M06 §6.6；AWR-03 §3.6 |
| `viewport/session.ts`、`interaction.ts`、`gotoRule.ts`、`facade.ts`、`testHooks.ts`、`layers/{registry,drones}.tsx` | WorldRoot（rotation.x = −π/2）；单击（位移 < `input.clickTolPx`）先测无人机球体再请求 ray_hit；GoTo 目标 = 命中点正上方、z = max(当前 z, 命中 z + 10 m)；门面新增 `pick.{ground,at,clear}`、`mission.{sendGoto,gotoState}`、`viewport.{onChange,projectToScreen,backend,worldId}`，相机 `home/focus/northUp`、未遮挡区 view offset；默认订阅集（roster 10、swarm S 10/B 20、env/state 10、event all、perf/server 1、sys/procs 1）与主选 60 Hz `uav/{id}/state`；测试构建 `window.__vp` 钩子 | M06 §6.11、§6.12、§7.1；AWR-14 §6.7；AWR-03 §8.5 |
| `engine/loop.ts` | FrameCtx 增加 `be`；`RenderBackendView`、`TextureOps`、`BackendCaps`、`PointSizeMode` 类型；帧钩子（FrameSampler）、render 相位耗时、按图层 CPU 计时 `layerMs` | AWR-03 §3.6；10 §6.3 |
| `engine/perf/index.ts`、`stores/perf.ts` | `window.__perf`（awr.perf.v1 子集，预分配环，保留 M15 的 `ui`）、`perf.onReveal`；governor 相位 4/10 Hz 摘要 | M06 §6.18 |
| `engine/drones/*` | 标记点（1 draw，(6 + 2)·dpr 光栅 px，选中放大，HOLD 灰）+ 低模 InstancedMesh（≤ 32，36 三角形，4 px ±15% 迟滞，10 Hz）；telemetry 相位 `swapFrame → time.ingest → __perf.net` | M06 §6.9；AWR-03 §3.8 |
| `engine/camera/CameraRig.ts` | camera-controls 3.1.2 Orbit（camera 相位 update），ENU 与 three 帧换算 | M06 §6.11（Orbit） |
| `engine/picking/Picker.ts`、`engine/mission/gotoMarker.ts`、`engine/anim/bezier.ts` | 射线换 ENU、无人机求交（热区 ≥ 12 px）、ray_hit（1 s 超时、取消旧请求）；GoTo 铅垂线与目标点；三次贝塞尔缓动求值 | M06 §6.12、M06-FR-045（简化） |
| `engine/time/*`（M12） | SimClockView（只在 PLAYING/LIVE 推进，屏蔽 bit7，外推上限、收敛与吸附、stale）、DelayController（D_wall 60–300 ms、平滑、限速、迟滞、冻结衰减到 0、恢复回升）、InterpRing（K = 32，Hermite + slerp，外推 ≤ E_max 后 HOLD）、`initTime` 与 clock 相位任务 | M12 §6.3、§6.4、§7.1 |

### 1.4 页面（M15 路径的最小改动，已移交 M15）

- `ui/panels/drones/DronesPanel.tsx`：行 id 改为 roster id（`fleetIdOf`），点击行选中并打开详情页（原实现无入口到详情页）。
- `ui/panels/drone-detail/DroneDetailPanel.tsx`：GoTo 按钮（shadcn Button + `cmd.goto` 图标）在 LIVE、有主选、有地面拾取时可用，调用 M06 门面 `mission.sendGoto()`；显示目标 ENU。
- `/world/:id` 路由与常驻画布沿用 M15 壳；DroneRail 行数据来自 `stores/fleet.ts`。

### 1.5 测试与用例文件

| 文件 | 用例 |
|---|---|
| `apps/web/tests/net/harness.ts` | RtHost + FakeSource 手动时钟测试台 |
| `apps/web/tests/net/fakesource.test.ts`（13 例） | 四个 `.awrrt` 夹具经 FakeSource 回放逐字节一致；经 rt.worker 引擎解码进槽后与 payload golden 一致（Lite32 行、Full64、roster，含 time_epoch 的 epoch 暂存与丢弃计数、bit7）；N ∈ {1, 200, 1000} 合成的控制消息通过 `rt/ops.schema.json`，roster/EnvKeyframe/state_ext/perf/sys 载荷通过各自 schema，BATCH 记录按生成访问器解码、payload 8 字节对齐、优先级顺序、frame_seq 连续；credit 窗口；goto 闭环与 107 拒绝 |
| `apps/web/tests/net/codec.test.ts`（11 例） | 槽布局、TIME 编解码、握手与 LIVE、Full64 与 env/state 投递、ack 节奏、ClockSync、关闭码与 4401 换 token、断线调用拒绝、退避、`?source=fake` URL |
| `apps/web/tests/net/client.test.ts`（2 例） | RtClient（进程内）+ FakeSource 真实计时：LIVE、订阅合并、roster、goto 依次 accepted/running/succeeded、事件 |
| `apps/web/tests/pointcloud/q16.test.ts`（4 例） | AWR-16 §4.3 深圳样点 golden（`w0 0x8c9d6195` 等）、解码误差 ≤ 半步长、oct16、16 B/点与非对齐偏移 |
| `apps/web/tests/pointcloud/firstscreen.test.ts`（11 例） | 六城规则 R 与 AWR-16 §4.11 表逐项一致（Tier S 与 iGPU 起步档）；深圳层级解析（节点数、各层节点与点数、`levelsByteEnd`、BFS 连续与 4 字节对齐、紧包围盒）；openWorld 请求序列（no-cache、`?v=`、整体 GET、首屏一次 Range `bytes=0-321383`） |
| `apps/web/tests/pointcloud/selector.test.ts`（5 例） | 图层帧眼点、预算与父先子后、确定性、视锥裁剪、depthCap、PageAllocator |
| `apps/web/tests/time/clock-interp.test.ts`（5 例） | 时钟推进与冻结、bit7、D 范围；15 m/s 转弯 10 Hz Hermite 误差 < 1 mm；外推与 HOLD |
| `apps/web/tests/m06/viewport.test.ts`（5 例） | 设备能力档与起步档、pass 计划求和、GoTo 高度规则、ENU 与 three 帧、贝塞尔 |
| `apps/web/perf/skeleton.spec.ts` + `skeleton.server.ts` | D1-AC-34 前端部分（见第 3 节） |

## 2. 验收对应

| 编号 | 内容 | 结果 |
|---|---|---|
| D1-AC-34（前端部分） | 一城 World → Range → 点云上屏 → 无人机上屏 → goto 闭环，无 pageerror | FakeSource 与 fake_gw 两例通过；网关 + sim-core 全链路例（`SKELETON_API`）已就绪待联调 |
| D1-AC-35（TS 部分） | FakeSource 按 layouts 合成 N ∈ {1, 200, 1000} 的 BATCH、TIME、EnvKeyframe 与事件，解码与 golden 一致，能回放 `.awrrt` | `make test-fixtures` 通过（含 `tests/net`） |
| M11-AC-040（前端） | 同上 | 通过 |
| M11-AC-036（部分） | rt.worker 解码、槽交换、ack、epoch 规则 | 功能用例通过；Worker 解码耗时等性能指标未测（并行阶段禁跑性能） |
| M05-AC-004、M05-AC-005 | 首屏规则 R 六城一致；打包 golden | 通过 |
| AWR-03 §3.6 规则 2 | 每帧 `render.calls` 等于 pass 计划 | skeleton 用例断言通过（计划 = 各图层 `drawCount` 之和） |
| D1-AC-20 | lint 全套 | `make lint` 通过（no-emoji、no-hex、motion-lint、check-icons、no-raw-controls、oxlint 含 type-aware 等） |

## 3. 测试结果

| 命令 | 结果 |
|---|---|
| `npx tsc -p tsconfig.json --noEmit` | 通过 |
| `npx oxlint`、`npx oxlint --type-aware` | 0 错误 |
| `make lint` | 通过 |
| `npx vitest run --project unit` | 28 个文件 361 例通过（本包新增 8 个文件 56 例） |
| `npx vitest run --project browser` | 3 个文件 10 例通过（首次运行有 Vite 依赖优化重载提示，见 M00 请求） |
| `make test-fixtures` | 通过（Python 21 例；Vitest 4 个文件 33 例） |
| `VITE_AWR_TEST_SWITCHES=1 npm run build` | 通过；产物含 `rt.worker-*.js`（129 kB）与 `fetch.worker-*.js`（1.2 kB） |
| `npx playwright test perf/skeleton.spec.ts --project perf` | 2 通过、1 跳过（全链路例需 `SKELETON_API`），约 30 s |
| `npx playwright test perf/m15/smoke.spec.ts --project perf` | 7 例通过（回归：无后端时世界打开失败不会卡住遮罩） |

功能冒烟实测（SwiftShader，1280×720，Tier S，DPR 0.5；非性能验收口径，未持性能锁）：深圳首屏 L1 一次 Range 321,384 B，TTFP 约 0.76 s，绘制约 2.2 万点，`gpu.calls = passPlan`；FakeSource goto 约 64 m 平飞 10 s 到达，终点误差 0.5 m（容差 3 m）；fake_gw 路径 swarm 约 10.9 Hz、RTT 约 4.8 ms。

截图（`/data/projs/anet-drone/.cache/impl/shots/`）：

| 文件 | 内容 |
|---|---|
| `skf-fake-01-world.png` | 深圳点云（Height 色带 + 法线光照）、DroneRail 一行 `p600-01`、连接"在线" |
| `skf-fake-02-goto-running.png` | 选中机详情页、GoTo 目标标记（铅垂线 + 目标点）与机体在途 |
| `skf-fake-03-arrived.png` | 到达目标点上方 |
| `skf-ws-fake-gw.png` | 真实 WebSocket（fake_gw）下的机体 `uav0001` 与 GoTo |

## 4. 与 PRD 的偏差及理由

| 偏差 | 理由与影响 |
|---|---|
| Tier A（WebGPURenderer）未实现，`?rb=webgpu` 打印提示后回退经典后端 | D1 中为 P1；池纹理无镜像分配与 copy 路径在 WebGPU 后端需另行验证。M06 接手 |
| 无 300 ms 微基准与点径自检；硬件设备只按家族启发式分档（未知硬件判 dGPU） | 本机只有软件光栅；Tier S 不跑微基准。M06 接手 |
| 无 shader zoo；`warmup` 门为后端就绪后 3 帧 | 新材质（GoTo 标记、低模）首次出现时编译，SwiftShader 下出现数百毫秒的帧；D1-AC-25 需要 M06 在遮罩下预编译全部图层变体 |
| Tier B 与 Tier S 同为单 pass（无 cloudRT、EDL、渲染比例） | skeleton 目标只要求 Tier S；Tier B 功能可用但无 EDL |
| 点云固定预算（无 CAS）、只有 Height 着色、CPU 缓存无上限淘汰、无取消策略、PROXY 分页不支持 | 任务书"固定预算即可"；六城均为单 chunk。M05 接手 |
| 槽视图每次换槽重建（约 20 个 TypedArray），不满足严格零分配 | 可转移 ArrayBuffer 在接收侧是新对象，视图无法复用；SharedArrayBuffer（V0.3）可消除 |
| `window.__perf` 的环容量：frame 与 render 为 65536，其余 4096 | 18 §9.2 写 65536；缩小以减少内存（约 7 MB → 2 MB），语义不变 |
| InterpRing 丢弃乱序样本；D_focus 等于 D_global；倍率过渡未做 | M12 最小版；M12 接手 |
| 关闭码 1006 的 whoami 判别与 4403 降为 viewer 未做（按退避重连） | 网关未交付，无法验证；M11 接手 |
| token 为空时不发送 `bearer.` 子协议 | 空 bearer 不是合法子协议值；网关按 4401 处理即可 |
| `?source=fake` 在生产构建也生效 | AWR-18 §9.5 规定 flight60 的 `source=` 在生产与测试构建都可用 |
| FakeSource N = 1 时机体 id 为 `p600-01`（N > 1 为 `uav%04d`，与 fake_gw 相同） | 与产品默认机型一致，便于界面冒烟 |
| RtEvent、StatusItem、CallResult 类型按 AWR-17 线上字段重写（M15 骨架为推测字段，无使用方） | 以 17 为准 |
| WorldCanvas 去掉 `linear` | M06 PRD 只要求 `flat`；输出 sRGB，点云底色与 token 一致 |
| 在 M15 路径改了 DronesPanel、DroneDetailPanel 两个文件（带 SK-F 注释） | 任务书第 4 项要求 DroneRail 与 GoTo 按钮；UI 只能在 `ui/**` 实现。已写入 SK-F-to-M15 请求移交 |
| GoTo 标记所有非失败态用前景色 g50，无描边/实心区分 | g300/g200 在点云上段灰阶上不可辨；形状区分需 GlyphLayer |
| 选择集到高亮与 60 Hz 订阅的绑定写在 `viewport/layers/drones.tsx`，未单列 `viewport/bindings/selection.ts` | 最小实现；M06 可拆分 |
| ray_hit 在 Playwright 中由测试服务器以 DSM 步进求交替身 | api 进程尚未提供 R07；全链路例改走真实路由（`localRayHit: false`） |

## 5. 遗留问题

1. 与真实后端联调：`SKELETON_API=127.0.0.1:8000 npx playwright test perf/skeleton.spec.ts` 需要网关（`awr.rt.v1`、R07 ray_hit、静态 `/worlds`）与 sim-core（goto 执行）交付；本机其他工作包正在编写 `python/awr/api/**`。
2. SwiftShader 下首帧与新材质编译造成的长帧（p95 数百毫秒）需 shader zoo 解决；性能指标（TTFP、帧节奏、Worker 解码耗时）未在独占锁下测量。
3. 顶栏时钟与 Timeline 条尚未接 TIME（M12 `stores/timeline.ts`）；详情页遥测 LfStat 未取值（M15）。
4. 构建分组：engine、net 代码并入 `ui` 块（M00 的 codeSplitting 规则未产出 engine/net 组）。
5. 相机只有 Orbit；Third/FPV/Bird、飞行缓动、离地钳制、悬停拾取、LabelLayer、ViewCube 等为 M06 后续工作。

## 6. 给其他模块的请求（`.cache/impl/requests/`）

| 文件 | 要点 |
|---|---|
| `SK-F-to-M15.md` | 两处 UI 最小改动的移交、工具态与命令状态机待实现、M06 门面新增接口、启动门 `firstFrame`/`warmup`、`__vp` 测试钩子 |
| `SK-F-to-M06.md` | viewport 与 engine 各文件的状态与后续、shader zoo 的必要性、render.calls 断言 |
| `SK-F-to-M05.md` | 点云最小版已实现与未实现清单、深圳实测 |
| `SK-F-to-M11.md` | rt.worker 行为约定（空 token、LIVE 判定、ack、重连）、槽视图例外、页面单例、FakeSource、api.ts、fleet；请网关 `perf/server.clients[].conn_id` 与 `serverInfo.connId` 一致 |
| `SK-F-to-M12.md` | engine/time 最小版的范围与缺口、`stores/timeline.ts` 接 TIME |
| `SK-F-to-M00.md` | `optimizeDeps.include` 增补、codeSplitting 分组、skeleton 测试服务器 |
| `SK-F-to-M16.md` | skeleton 用例的三种数据源、运行方式与 harness 登记 |
| `SK-F-to-M04.md` | 前端 ray_hit 请求与读取字段、GoTo 高度规则 |

已处理的来件：`M15-to-M06.md`（保留 `__perf.ui`；`warmup` 门已加；接口未改签名，门面只做增补）、`M15-to-M11.md`（ConnState 取值不变；`createRtClient`/`onConnState` 已实现；`fleetRows` 字段不变并新增 `fleetIdOf`）、`MS12-to-M11.md` 第 1 条（FakeSource 与测试已交付，`make test-fixtures` 覆盖）、`M00-B-to-M11.md` 第 2 条（前端使用 TS 生成物，不手写偏移）、`M15-to-M03-M05-...md`（`stores/world.ts` 由点云适配层写入 phase、progress、B、limitedBy、inflight、failed、error）。
