# INT-1 集成验证报告

| 项 | 内容 |
|---|---|
| 工作包 | INT-1 集成验证（全部模块合入后的首轮集成） |
| 日期 | 2026-09-29 |
| 环境 | 8 核 CPU、无 GPU；Chromium 151（SwiftShader，Tier S）；Node 22；Python `.venv`；端口偏移 6 |
| 约束执行 | 未安装任何依赖（没有触发 19 §7.4 的评估安装）；未运行性能基准与 `perf` 标记用例；未使用 git；`make lint` 在最后一次修改后仍通过 |
| 越权修改 | 本工作包获授权修改任何路径以修复集成问题，全部越权修改见第 4 节（逐文件列出所有者与原因） |

## 1 结论摘要

- 集成链路已经打通。通过的验收：
  - walking skeleton（D1-AC-34）、交互（D1-AC-32）
  - 功能矩阵 B/S（D1-AC-14）、无运行期编译（D1-AC-25）、画布尺寸稳定（D1-AC-24）
  - 设计体系（D1-AC-20）、可用性（D1-AC-21）
  - 早期数据源（D1-AC-35）、远程访问（D1-AC-33）
  - 契约（D1-AC-13，含 env-gpu）、六城世界校验（D1-AC-01）
  - 动力学（D1-AC-12）、录制回放（D1-AC-18 功能项）、重建链路（D1-AC-22）、确定性（D1-AC-31）
- `make run` 下全部进程都进入 RUNNING。原先 job-worker 不写心跳，一直卡在 STARTING，INT-1 已修复。
- 未通过的 P0 有两项：
  1. **D1-AC-15（S1）**：功能链路修通后，剩下的是规格冲突。
     - 按规格裕度 1.3 计算，p600-01 在最后一圈转到主塔远侧时（约 671–685 s）触发 `SAF.BAT.ENERGY_RTL`（例如 552.99 < 560.69）。
     - 随后连带出现 missions_done = false、soc_min 0.178、guard_events 1 三个谓词失败。
     - 同一构建把裕度临时改为 1.0 后，9 个谓词全部为真（实验值，未提交）。这需要以 ADR 裁决，见 7.1。
     - 另外，×10 墙钟 286 s，超过 210 s 的限额（M16-NFR-008）。
  2. **D1-AC-11a（进程恢复）**：kill api 一项已通过。INT-1 把 api 的导入耗时从 1.62 s 降到 1.06 s；
     此前重连要 3.26 s，现在满足 ≤ 3 s。kill sim-core 的重启仍要 3.17 s（阈值 3.0 s），功能断言都成立，见 7.3。
- 另有 1 项核心剧本冒烟未通过：`test_ladder_smoke`（ladder n10）。最小间距 9.66 m < 14 m，guard_events 20，属剧本与编排设计问题，见 7.2。
- 其余性能类验收（03a/03b/04/05/06/07/08/09a/09b/10/23/26/27/28/29/30 等）都在性能阶段执行。本轮遵守"并行阶段不跑
  性能基准与 perf 标记用例"，标为未测。

## 2 门禁执行结果

日志都在会话临时目录的 `logs/`，下文用文件名指代。

| 门禁 | 命令 | 结果 | 证据 |
|---|---|---|---|
| lint | `make lint` | 通过。只剩 1 条非致命警告 PERF-E017（`m14.bench.*` 注册项缺 `build`、`backend.kind` 不是登记值，属 M14） | lint-final.log |
| 契约 | `make test-contracts` | 通过。pytest 331 通过、3 跳过；vitest 225 通过；golden 与生成物 `--check` 都是最新 | tc-final.log |
| pytest | `pytest -m "not perf"`（全量） | 全量（48 min）：2435 通过、6 失败、10 跳过、45 取消选择（perf）、1 xfail。INT-1 随后修掉其中 4 项：`test_chain_inproc` 靠扩展点隔离；`test_make_demo` 靠事件首见补拉；`test_interest` 两例受负载影响，复跑通过。复跑受影响的目录（tests/rt、tests/runtime、tests/recorder、tests/world/test_ingest*）270 通过，`tests/e2e/test_demo_check.py` 8 通过。剩下 2 项：`test_s1`（7.1）与 `test_ladder_smoke`（7.2） | pytest-final.log |
| vitest | `npx vitest run`（unit + browser） | 908 通过、1 失败、1 跳过（118 个文件）。失败的 `tests/m11/alloc.test.ts` 在全量并行下保留堆增长 65608 B，比 65536 B 多 72 B；单独运行两次都通过（7.11）。`hotkeys.test.ts` 放宽 hook 超时后通过 | vitest-final*.log |
| 构建 | `make build`（生产）与 `VITE_AWR_TEST_SWITCHES=1 npx vite build`（测试） | 通过（只有 chunk 体积提示） | build-final.log |
| 世界 | `make validate`（`worldpkg validate worlds/* --deep`） | 通过。六城 0 错误 0 警告，71 份文档 0 无效 | validate-final.log |
| skeleton | `npx playwright test perf/skeleton.spec.ts --project perf`（D1-AC-34） | 通过 4/4（完整链路、六城 `/world/<id>`、FakeSource、fake_gw） | pw-skeleton*.log |
| 交互 | `npx playwright test --project=e2e interaction`（D1-AC-32） | 通过 1/1 | pw-interaction*.log |
| 功能矩阵 | `perf/m06/feat-matrix.spec.ts`（Tier S、Tier B）（D1-AC-14） | 通过（Tier S、Tier B；`perf/m06` 共 8 例全部通过，含 smoke 与设备丢失） | pw-m06*.log |
| 预热 | `perf/m06/warmup.spec.ts` + `perf/warmup.spec.ts`（D1-AC-25） | 通过（m06 warmup 1/1；根 warmup 在 `make run` 实机上 1/1） | pw-m06*.log、pw-root-wl*.log |
| 布局 | `perf/m06/layout.spec.ts` + `perf/layout.spec.ts`（D1-AC-24） | 通过（m06 layout 2/2；根 layout 在 `make run` 实机上 1/1） | pw-m06*.log、pw-root-wl*.log |
| 其他 e2e | brand、sanitize、motion、honesty、a11y、timeline | a11y 1、brand 1、sanitize 1、honesty 3、motion 2、timeline 实时段 2 全部通过；timeline 回放段是用例自带的 fixme，跳过。motion 的 reduced 用例在整批运行时失败过 1 次（7 个动画在运行），单独运行两次都通过（7.11）。另外补跑 `perf/m07/env-gpu.spec.ts` 2/2、`perf/m01/recon.spec.ts` 2/2 | pw-e2e-final.log、pw-motion-*.log、pw-timeline2.log、pw-env-gpu.log、pw-recon2.log |
| S1 | `pytest tests/e2e/test_scenarios.py::test_s1`（D1-AC-15） | 未通过（FAILED，见 7.1）；单独运行 286 s，全量中结论相同 | test_s1_final.log、pytest-final.log |
| 混沌 | `make chaos-core`（D1-AC-11a） | 未通过：kill api 通过；kill sim-core 重启 3.17 s，超过 3.0 s（见 7.3） | chaos-core-final.log |
| 夹具 | `make test-fixtures`（D1-AC-35） | 通过。pytest 21、vitest 112 通过 | test-fixtures.log |
| 远程 | `tests/e2e/remote_smoke.sh`（D1-AC-33） | 通过（relay 转发 → 127.0.0.1） | remote_smoke.log |
| 运行 | `AWR_PORT_OFFSET=6 make run` + 截图 | 通过：demo profile 下 sim-core、api、recorder、agent-runtime、job-worker 全部进入 RUNNING，日志里 0 条 ERROR；截图见第 6 节；`make stop` 正常停止 | make-run-final.log、shots-final.log |

## 3 D1 验收状态（D1-AC-01 至 D1-AC-35）

状态取值为 通过、未通过、未测、不适用，判定依据是 03 表中该条验收列出的命令。"未测（perf）"是指本轮遵守并行阶段约束，没有运行性能基准与 `perf` 标记用例。"通过"只覆盖本轮执行的命令，其中的性能子项（计时、帧率等）在说明里另外列出。

| 编号 | 范畴 | 优先级 | 状态 | 证据与说明 |
|---|---|---|---|---|
| D1-AC-01 | World | P0 | 通过 | `make validate`：六城 `--deep` 0 错误（validate-final.log）；`tests/world/test_autobuild.py` 在全量 pytest 中 通过。单城构建 ≤ 60 s 属计时项，未测 |
| D1-AC-02 | 首屏 | P0/P1 | 未测（perf） | 要读 `__perf.load.{ttfp,switchMs,tti}` 的性能用例。烟测里 skeleton 与交互用例的首屏都在门禁时限之内，但不能作为判定依据 |
| D1-AC-03a | 帧节奏（纯点云） | P0 | 未测（perf） | flight60 `scene=pc` 属性能基准 |
| D1-AC-03b | 帧节奏（整景） | P0 | 未测（perf） | flight60 `scene=full` 与 layers 配对属性能基准 |
| D1-AC-04 | 控制器 | P0 | 未测（perf） | 同上，读 `__perf.cas` |
| D1-AC-05 | 画质 | P1 | 未测（perf） | 全量参考渲染对比 |
| D1-AC-06 | 流式 | P0 | 未测（perf） | `__perf` + LoAF 用例 |
| D1-AC-07 | 机群（sim-core） | P0 | 未测（perf） | fleet_ladder 基准未跑；`tests/sim/test_kernel_parity.py`（numba 与 numpy 对拍）在全量 pytest 中 通过（61 例） |
| D1-AC-08 | 网关 | P0 | 未测（perf） | `bench_state.py --clients 3 --with-flight60` |
| D1-AC-09a | 前端与机群 N=200 | P0 | 未测（perf） | `perf:ladder --n 200` |
| D1-AC-09b | 前端与机群 N=1000 | P1 | 未测（perf） | `perf:ladder --n 1000` |
| D1-AC-10 | 命令 | P0 | 未测（perf） | `bench_cmd.py` |
| D1-AC-11a | 进程恢复（core） | P0 | 未通过 | `make chaos-core`（chaos-core-final.log）：`test_kill_api` 通过（INT-1 降低 api 导入耗时之后；此前重连 3.26 s）；`test_kill_sim_core` 的功能断言成立，但重启要 3.17 s，超过 3.0 s。见 7.3 |
| D1-AC-11b | 混沌（ext） | P1 | 未测 | `make chaos`（含 checkpoint、挂死）；checkpoint 扩展段见 M09 延后项 |
| D1-AC-12 | 动力学 | P0 | 通过 | `tests/sim/test_fleet_sih_parity.py`、`tests/sim/test_fleet_robust.py`（全量 pytest） |
| D1-AC-13 | 契约 | P0 | 通过 | `make test-contracts` 通过（tc-final.log）；`perf/m07/env-gpu.spec.ts` 在 Tier S 与 Tier B 都通过（GPU windAtEnu 与 windCPU 在 1024 个点上的偏差不超过界限，pw-env-gpu.log） |
| D1-AC-14 | 渲染后端 | P0(B,S)/P1(A) | 通过（B、S） | `perf/m06/feat-matrix.spec.ts`：Tier S、Tier B 两项通过，`render.calls` 与 pass plan 一致（修复 E007 后）。Tier A 属 P1，本机无 WebGPU，未测 |
| D1-AC-15 | 剧本 S1 | P0 | 未通过 | FAILED：missions_done、guard_events、battery_soc_min、energy_rtl_count 四个谓词为假，根因只有一个：主塔远侧的 ENERGY_RTL（规格冲突）。×10 墙钟 286 s，超过 210 s。见 7.1 |
| D1-AC-16 | 剧本 S3 | P1 | 未测 | `test_s3` 要 `AWR_E2E_FULL=1`。M14-to-M08 第 1 条（lease 不接受 agent 角色）仍未处理，S3 预计阻塞 |
| D1-AC-17 | S2/S4/S5/S6 与任务编辑 | P1 | 未测 | ext 剧本长跑与 `mission-edit.spec.ts` 不在本轮范围 |
| D1-AC-18 | 录制与回放 | P1 | 通过 | `tests/recorder/test_replay.py`（全量 pytest，回放帧与录制逐字节一致）通过；`tests/e2e/timeline.spec.ts` 实时段 2 例通过（INT-1 把固定等待改成按收敛条件等待），回放段是用例自带的 fixme。seek ≤ 500 ms、10 min 与 N=1000 录制属性能子项，未测 |
| D1-AC-19 | 环境 | P0/P1 | 未测（perf） | `perf/m07/env-switch.spec.ts` 属性能用例。env 两端 golden（10261 例）在 test-contracts 中通过；S1 剧本环境预设与阵风已生效（`env/preset`、`env/set` 内部命令，阵风 `code 0`） |
| D1-AC-20 | 设计体系 | P0 | 通过 | `make lint` 通过（emoji、禁用字形、token 外 hex、backdrop-filter、lucide-react、no-raw-controls、lint-lf、check-brand 等全部为 0）；`motion.spec.ts`、`brand.spec.ts`、`sanitize.spec.ts` 通过；截图页面扫描 emoji 0、`svg.lucide` 0、backdrop-filter 0 |
| D1-AC-21 | 可用性 | P1 | 通过 | `a11y.spec.ts` 通过（pw-a11y.log）：纯图标按钮都有可访问名称，焦点环可见，Shift+/ 能打开帮助，输入框中 KeyP 不误触发。此前的失败原因：Esc 关闭帮助后对话框还在退出过渡中，模态探针仍判定为打开，紧接着的 Ctrl+K 被拦截。已修复，见 4.4 的 `ui/hotkeys/registry.ts` |
| D1-AC-22 | 重建链路 | P1 | 通过 | `tests/reconstruction/test_mock_chain.py` 通过：状态机走到 SUCCEEDED，IR 与世界包 `--deep` 校验，Ajv strict。`perf/m01/recon.spec.ts` 2/2 通过：产物世界进入目录，Web 端揭开遮罩、首屏、有点绘制（pw-recon2.log）。UI 提交入口要依赖 job-worker 的 `svc/job/*` 服务，这部分尚未接线，见 7.5 |
| D1-AC-23 | UI 开销 | P1 | 未测（perf） | `ui-overhead.spec.ts` |
| D1-AC-24 | 画布尺寸稳定 | P0 | 通过 | `perf/m06/layout.spec.ts`（Ctrl+B 与 Dock 拖动、窗口缩放两例）与根 `perf/layout.spec.ts` 通过。"> 50 ms 帧占比"那一项属 perf 口径，在性能阶段复核 |
| D1-AC-25 | 无运行期编译 | P0 | 通过 | `perf/m06/warmup.spec.ts` 与根 `perf/warmup.spec.ts` 通过（揭开遮罩后 programs 不增加） |
| D1-AC-26 | 时延与插值 | P0 | 未测（perf） | `latency.spec.ts`；×10 实时 HOLD 占比依赖 ×10 可达（S1 目前只有约 ×2.8–×10，见 7.1） |
| D1-AC-27 | 事件风暴 | P0/P1 | 未测（perf） | `storm.spec.ts` + fleet_ladder |
| D1-AC-28 | sim-core 并发实时性 | P1 | 未测（perf） | fleet_ladder 并发 |
| D1-AC-29 | soak | P1 | 未测（perf） | 30 min soak |
| D1-AC-30 | GC | P1 | 未测（perf） | CDP Tracing |
| D1-AC-31 | 确定性 | P1 | 通过 | `tests/sim/test_resim.py`（全量 pytest） |
| D1-AC-32 | 交互 | P0 | 通过 | `tests/e2e/interaction.spec.ts`：`/world/shenzhen` 直达；点选 GoTo accepted → succeeded，机体停在目标点 3 m 以内；添加 P600 后出现、移除后消失，都在 1 s 内（断言口径为 1 s 加 0.5 s 轮询粒度余量）；zones 叠加与 `zones.geojson` 一致 |
| D1-AC-33 | 远程访问 | P0 | 通过 | `tests/rt/test_access.py`（全量 pytest）；`remote_smoke.sh` 经 relay 转发访问成功 |
| D1-AC-34 | 集成门禁 | P0 | 通过 | `perf/skeleton.spec.ts` 4/4：World → Range → 点云上屏 → FleetSim → StateRing → Gateway → WS → 无人机上屏 → goto 闭环，无 pageerror |
| D1-AC-35 | 早期数据源 | P0 | 通过 | `make test-fixtures`：`gen_fixtures --check` 最新，`test_fixtures.py` 与 `test_fake_gw.py` 21 例、vitest frame/net/m11 112 例通过；`tests/e2e/test_fake_source.py` 取消 xfail 后通过 |

## 4 越权修改清单

以下文件都不属于 INT-1，按 03 §4.3 的所有者分组。每一处都是为了让集成门禁通过，或者落实跨模块请求。
原所有者复核时，以"原因"一列为准。

### 4.1 后端（Python）

| 文件 | 所有者 | 修改 | 原因 |
|---|---|---|---|
| `python/awr/sim/safety/geofence.py` | M09 | `admit()` 改调新增的 `_coarse_chunked()`：按 M04 `MAX_VERTICES = 1000` 分窗（相邻窗重叠 1 个顶点），非末段不判终点净空，违例的航段下标换算回整条折线 | S1 下段螺旋是 1000 航点，准入折线有 1002 个顶点，M04 抛出"1002 vertices > 1000"，被当作 102 PATH_INVALID 反复拒绝，p600-01 一直悬停 |
| `python/awr/sim/safety/params.py` | M09 | `RtlParams.h_top_tol_m = 20.0` | M04 `heightmap_top_along` 的缺省 tol 100 m 是粗校验口径，紧贴超高层的短返航线也会取到塔顶（z_rtl 389 m、t_rtl 326 s），扫描中段误触 ENERGY_RTL |
| `python/awr/sim/safety/battery.py` | M09 | `h_top()` 非 exact 时使用 `h_top_tol_m` | 同上 |
| `python/awr/sim/runtime/main.py` | M08 | 新增 `_register_metrics()`（elapsed_s、landed_all、landed_home_err_m，owner M08，只替换 M08 自己的条目）；`_ext_rows` 合并 `hooks.state_ext_fields(slots)`（battery、link、gcs_loss_policy） | M10-to-M08 第 1 条（剧本度量缺失，S1 谓词求值为空）；M09-to-M08 第 5 条（state_ext 的 M09 字段写死） |
| `python/awr/sim/runtime/slow.py` | M08 | `STARVE_NS = 50 ms`；atomic 任务连续顺延满 50 ms【墙钟】后强制执行一次 | M07-to-M08 第 5 条、M14-to-M08 第 2 条：`left_us < p99` 时 atomic 任务永远被跳过，查询与估价挂起到网关超时 |
| `python/awr/sim/core/command.py` | M08 | 插件命令被接受后补发 `cmd.succeeded`（effect OK、verify_trust 4、simulated）；resume 分支在 `_succeed_now` 之前取出调用，再传给 `apply_operator`；`LOCK_EXEMPT_OPS`，加锁机体的其余命令返回 114；`_reject_raw` 优先用 `detail["remedy"]`；新增公开的 `extend_deadline(cid, until_t_ns)`；goto 在运动提供者接管时跳过第⑧步细校验 | M07-to-M08 第 1 条（插件命令没有终态）；M09-to-M08 第 1、2、3 条；M10-to-M08 第 2 条；skeleton 的 GoTo 被 102 "fine check" 拒绝（safe_transit 提供者自己规划路线，第⑧步的细校验按直线判定不适用） |
| `python/awr/sim/core/roster.py` | M08 | 模块级 `register_sensor_rig(fn)`；`Roster.add` 可选 `sensor_names`，填 `e.sensors` | M13-to-M08 第 1 条（roster `sensors[]` 为空，视锥与 FPV 没有数据） |
| `python/awr/sim/mission/engine.py` | M10 | TrackRT 增加 `rej_n`、`retry_at_ns`：被拒后按 0.5·2^k s（上限 30 s）退避 | 被拒调用每步重试，S1 一次运行产生 6896 条 `cmd.rejected`（M16-to-M10 第 2 条） |
| `python/awr/sim/mission/runtime.py` | M10 | `extend_deadline` 优先用引擎的公开接口；新增 `_apply_scenario_env(sc)`，剧本加载后提交内部 `env/preset`、`env/set`（principal `scenario:<id>`） | 剧本 `environment` 块此前没有下发，S1 的风与预设不生效 |
| `python/awr/api/rest/fleet.py`（新建） | M08（领域路由） | R12–R16、R62 与 profiles/caps 查询的薄转发层（鉴权、幂等键 → cid、确认令牌、HTTP 映射） | M08-to-M11 第 1 条、M09-to-M11 第 4 条、M15-to-M11 第 1 条：路由缺失，UI 添加与移除 P600 返回 404，D1-AC-32 阻塞 |
| `python/awr/api/rest/scenarios.py` | M10 | `GET /api/scenario` → query `scenario/result` | M16 剧本面板与 honesty 用例需要当前剧本结论 |
| `python/awr/api/main.py` | M11 | `_default_scenarios()` 读 `scenarios/catalog.json` 的 `worlds.<id>.default` 并传给 Catalog | M16-to-M11 第 3 条（`default_scenario_id` 为空） |
| `python/awr/api/rt/detail.py` | M11 | 同 epoch、同 version 的 env 心跳照常发布，只是不重复应用 | M07-to-M11 第 1 条（高优先级）：新连接收不到环境关键帧 |
| `python/awr/api/static.py` | M11 | `SPA_RESERVED` 加 `vehicles`；新增 `GET/HEAD /vehicles/{model}/model/{file}.glb`（`?v=` 时 immutable，否则 no-cache + ETag） | M06-to-M11 第 1 条：P600 hero 模型 404 |
| `python/awr/runtime/events.py` | M11 | `_CATEGORY_ALIASES` 加 `anet → agent`；EventPublisher 在构造时为全部类别预声明发布者；EventSubscriber 新增可选参数 `backfill_first_max`（缺省 0，行为不变）：首次见到某生产者、且其首条 seq − 1 不超过该值时，从 seq 1 起经 `_replay` 补拉 | M14-to-M11 第 2 条；首批事件在发布者懒声明时丢失；M16-to-M11 第 1 条：sim-core 启动阶段的 `scenario.loaded`、`mission.created` 早于 api 订阅，事件环里看不到（`test_make_demo` 失败） |
| `python/awr/api/rt/events.py` | M11 | Gateway 的 EventIngest 以 `backfill_first_max = 1024` 构造订阅（生产者 `_replay` 环 4096 条，不会截断） | 同上：只在生产者刚启动时回补；api 在运行中途重启时首条 seq 很大，行为不变 |
| `python/awr/recorder/policy.py` | M12 | `AWR_SCENARIO_LOAD=0` 时 `load_scenario` 返回 None；profile 的 `record` 覆盖顶层值 | 默认 S1 让 recorder 在订阅建立之前自启，`tests/recorder/test_processes.py` 收不到 `rec.started` |
| `tools/fake/fake_gw.py` | M00 | `--calls {reject,ack}`，缺省 reject（返回 211 SIM_UNAVAILABLE 终态） | fake_gw 对 call 没有终态，前端调用一直挂起；skeleton 与 test_fake_gw 分别按需选用 |
| `configs/runtime.yaml` | M11 | sim-core 插件改为 `awr.sim.sensors.plugin`；agent-runtime 加 `start_after: [sim-core]` | M13-to-M11 第 1 条；M14-to-M11 第 1 条 |
| `python/awr/world/ingest/__init__.py` | M03 | `ingest` 改为按需导入（PEP 562 `__getattr__`），其余导出不变 | api 经 `package.catalog → ingest.manifest` 导入本包时，急切导入 pipeline 会连带导入 terrain 与 scipy.ndimage（约 0.65 s）。`import awr.api.main` 从 1.62 s 降到 1.06 s，缩短 kill api 后的重连（D1-AC-11a） |
| `python/awr/jobs/worker.py` | M03 | 受监管时调用 `init_child("job-worker")`，每轮写心跳 `hb.job-worker`，收到 SIGTERM 后退出主循环 | runtime.yaml 为 job-worker 配置了心跳 liveness，但 worker 从不写心跳：demo profile 下每 30 s 判定启动超时、重启 5 次后熔断，顶栏一直显示"进程 job-worker 状态 STARTING"。修复后 STARTING → RUNNING（ready），停止时 rc 0 |

### 4.2 契约（M00，`make contracts` 重新生成）

| 文件 | 修改 | 原因 |
|---|---|---|
| `packages/contracts/bus/event.schema.json` | `known_kinds` 增加 55 个（M08、M10、M12 事件，M01 `job.log`，M14 `agent.*`，`anet.evidence.gap`、`geo.ready`、`cmd.progress` 等） | M08/M10/M14/M01-M02/SK-B-to-M00 各请求；未登记的 kind 让事件校验失败 |
| `packages/contracts/rt/payloads/uav_state_ext.schema.json` | 可选 `profile, ctrl, thrust_frac, tilt_deg, pos_err_m, wind_rel_mps, sens{gimbal,imu}`；`loc` 增加 `eph_m, epv_m, hdop, err_enu_m` | M08-to-M00 第 2 条、M13-to-M00 第 3 条；M08 的 `_ext_m08` 输出此前被 schema 拒绝 |
| `packages/contracts/scenario/scenario.schema.json` | `id_prefix` 放宽为 `^[a-z0-9]+(-[a-z0-9]+)*$` | M16-to-M00 第 1 条 |
| `packages/contracts/rt/enums.json` | SensorKind 增加 gnss/imu/baro；新增 GnssFix、GimbalMode、ReconEngine、ReconStage | M13-to-M00 第 1 条、M01-M02-to-M00 第 2 条 |
| `packages/contracts/rt/rng_streams.json` | 登记流 11–16（M01），保留号改为从 17 起 | M01-M02-to-M00 第 3 条 |
| 生成物 `python/awr/contracts/{schema_types,enums,rng_streams}.py`、`packages/contracts/gen/ts/{types,enums}.ts` | 重新生成 | 同上 |

### 4.3 Make 与工程配置

| 文件 | 所有者 | 修改 | 原因 |
|---|---|---|---|
| `mk/common.mk` | M00 | `with_lock_sh/ex` 改为 `flock … bash -c '$(1)'` | 带 `cd` 的配方报 "flock: failed to execute cd"，chaos-core、test-world 等目标全部无法执行 |
| `mk/m14.mk`（新建） | M14 | `test-agent`、`test-agent-web`（`TEST_TARGETS += test-agent-web`） | M14 PRD 列出的目标缺失 |
| `mk/m12.mk`（新建） | M12 | `test-m12`、`bench-rec`、`bench-seek`、`bench-replay`、`m12-reindex`、`m12-repair` | M12 PRD 列出的目标缺失 |
| `mk/environment.mk`（新建） | M07 | `test-env`、`bench-env`、`env-assets`（共享目录 `worlds/_shared`）、`env-fixtures` | M07-to-M00 第 2 条 |
| `apps/web/vite.config.ts` | M00 | dev 代理增加 `'^/vehicles/.+'` | P600 模型在 dev 下 404 |
| `apps/web/.oxlintrc.json` | M00 | `src/engine/pointcloud/**` 覆盖：禁止 import `@/net` | M05-to-M00 第 4 条（M05-AC-029 结构隔离） |

### 4.4 前端

路径相对 `apps/web/src/`。

| 文件 | 所有者 | 修改 | 原因 |
|---|---|---|---|
| `engine/mission/zones.ts`、`engine/mission/MissionOverlay.ts`、`engine/drones/frustums.ts` | M06 | 透明 DoubleSide 材质设 `forceSinglePass = true` | three 对透明双面材质会画两次，`render.calls` ≠ pass plan（M06-E007），feat-matrix 与 skeleton 失败 |
| `engine/pointcloud/PointCloudEngine.ts`、`engine/pointcloud/types.ts` | M05 | `drawCount()` 返回 `root.visible ? 1 : 0`；info 增加 northConfidence、syntheticGroundZ | M06-to-M05 第 1 条（three 把零范围的可见 Points 也计入 draw）；honesty 数据来源 |
| `stores/world.ts`、`viewport/layers/pointcloud.tsx` | M05 | 写入 northConfidence、syntheticGroundZ | M16-to-M15 第 1 条（诚实标识：北向、合成地面） |
| `engine/drones/vehicleModels.ts` | M06 | 缺省模型源 `p600 → /vehicles/p600/model/p600.glb` | 首次出现 P600 模型（D1-AC-25）与 hero 档 |
| `viewport/overlay/ViewCube.tsx` | M06 | 增加 focus-visible 焦点环类 | D1-AC-21 焦点可见 |
| `ui/shell/RtContext.tsx` | M15 | 绑定 `bindSafety`、`bindAgents`；导出 `setAgentsPanelOpen`；timeline `setNotifier`；labels `setFormatter`；viewCube `setLabels` | M09-to-M15 第 1 条、M14-to-M15 第 1 条、M12-to-M15 第 4 条；ViewCube 与标签原来显示英文键 |
| `ui/panels/agents/AgentsPanel.tsx` | M15 | 挂载与卸载时调用 `setAgentsPanelOpen` | M14-to-M15 第 1 条（面板打开时才订阅） |
| `ui/lf/LfTimelineTrack.tsx`、`ui/layout/TimelineBar.tsx`、`ui/panels/timeline/TimelinePanel.tsx` | M15 | 改用 `timelineTrack`；绘制区间、L4 标记（HERO、SUPERSEDED 置灰）与书签 | M12-to-M15 第 1 条、M15-to-M12 第 3 条（轨道原为空模型） |
| `ui/tools/ToolLayer.tsx` | M15 | Profile 读 `profile_id`、`display_name` | 与 R17 返回字段一致 |
| `ui/panels/world/WorldPanel.tsx` | M15 | 诚实标识行（北向未验证或近似、北向未知、合成地面） | M16-to-M15 第 1 条 |
| `ui/panels/drone-detail/DroneDetailPanel.tsx` | M15 | `useProfileStatus(model)`（查 `/api/fleet/profiles`）；占位机型显示徽标"参数未辨识" | M16-to-M15 第 1 条 |
| `ui/hud/PerfHud.tsx` | M15 | 强制档位徽标 | honesty "forced tier" |
| `ui/actions/fleetRest.ts`、`net/api.ts` | M15、M11 | 新增 `apiDelete` 并用于移除 P600 | M15-to-M11 第 1 条 |
| `app/i18n/zh-CN.json`、`en.json` | M15 | 补 timeline.*、label.*、viewcube.*、world.* 文案键（en 为空串，回退中文） | 上面各项新增文案 |
| `styles/motion/tiers.css` | M15 | reduced/off 档下 `#boot-mask` 的过渡用 `--duration-instant` | `motion.spec.ts` reduced 档断言 |
| `ui/hotkeys/registry.ts` | M15 | 模态探针不再把处于退出过渡的弹层（Base UI `data-closed`、`data-ending-style`）算作打开 | Esc 之后立刻按 Mod+K 会被拦截，命令面板打不开（D1-AC-21 `a11y.spec.ts`） |

### 4.5 测试与用例

| 文件 | 所有者 | 修改 | 原因 |
|---|---|---|---|
| `tests/sim/conftest.py`、`tests/safety/conftest.py` | M08、M09 | autouse 夹具：快照、清空并恢复进程级运动提供者与 `_FINE` 细校验器 | M10 在收集阶段自动安装全局提供者，全量运行时 `test_command_engine`、geofence 用例失败（单独跑能通过） |
| `tests/rt/test_skeleton_chain.py` | M11 | 2 Hz GCS pinger；`AWR_SCENARIO_LOAD=0` | M09-to-M11 第 1 条：M09 装配后，GCS 丢失策略让机体 RTL |
| `tests/runtime/test_fake_gw.py` | M11 | `--calls ack` | fake_gw 新缺省 |
| `tests/rt/conftest.py` | M11 | autouse 夹具：与 tests/sim 相同的运动提供者和细校验执行者隔离 | 进程内栈 InprocSim 不装配插件，但全量收集时 M10 的全局提供者会接管 goto，因为没有 stage 驱动，最后以截止 202 结束；`test_chain_inproc` 单独运行通过、全量运行失败 |
| `tests/recorder/test_processes.py` | M12 | `AWR_SCENARIO_LOAD=0` | 见 recorder/policy.py |
| `tests/mission/test_s1.py`、`tests/mission/test_director.py` | M10 | `elapsed_s` 只在未登记时登记 | M08 现在登记该度量 |
| `tests/safety/test_rtl_profile.py` | M09 | 断言用同一 tol，并要求 h_top ≥ exact | 与 `h_top_tol_m` 一致 |
| `tests/e2e/test_fake_source.py` | M16 | 去掉 xfail 与未用的 import | 已通过 |
| `tests/e2e/interaction.spec.ts` | M16 | 面板查询"添加 P600"；GoTo 改为必做（用 `__vp.project` 选候选地面点，轮询 `ground().target`，3D 距离 ≤ 3 m）；添加流程等提示与面板收起，在当前位置 80–110 m 外取出生点；新增移除步骤；zones 用 `__vp.zones()` | 原用例把 GoTo 跳过、缺移除步骤，并有 palette 排名、退出动画、出生点过近等不稳定因素 |
| `tests/e2e/motion.spec.ts` | M16 | 排除 ScrollTimeline 动画 | 滚动驱动动画不是 Base UI 部件动画 |
| `tests/e2e/timeline.spec.ts` | M16 | 画布 hover 改为 `page.mouse.move`；按 Space 后等到 `tRender` 收敛到 `simNow` 再检查冻结；viewer 用例等首个 TIME 到达（state4 > 0）后再取基线 | locator hover 被浮层挡住会一直等到超时；SwiftShader 在负载下只有约 4 Hz，固定等待 500 ms 不够 D 衰减；state4 在首个 TIME 之前为 0，被误判为"viewer 改动了时钟" |
| `tests/e2e/honesty.spec.ts` | M16 | 世界分组按钮点击加 3 s 超时；等"参数未辨识"出现 | 分组已展开时点击会一直等到用例超时 |
| `apps/web/perf/skeleton.spec.ts` | M16 | fake_gw 改用 `--calls ack`；聚焦按钮取 `.first()` | fake_gw 缺省改为返回拒绝终态，skeleton 的 call 用例需要 ack 形态；相机工具栏与详情面板各有一个"聚焦选中"按钮，严格模式下定位冲突 |
| `apps/web/perf/fixtures/ui.ts` | M16 | 快捷键前用 `page.mouse.move` 到视口中心（不用 locator hover） | 浮层遮挡时 hover 一直等到超时，根 warmup 与 layout 失败 |
| `apps/web/tests/m15/hotkeys.test.ts` | M15 | `beforeAll` 超时改为 60 s | 全量并行 vitest 下首次导入 builtin actions 超过 10 s 的缺省 hook 超时（单独运行通过） |

另外，`viewport/testHooks.ts`、`ui/actions/builtin.ts`、`mk/m16.mk`、`python/awr/sim/mission/tracker.py`、`apps/web/perf/skeleton.server.ts`
曾做过临时修改，都已恢复原样，净变更为 0。

## 5 跨模块请求处置

`.cache/impl/requests/` 共 147 份（另有 `M12-drafts/` 草稿目录）。处置分为以下几类：

- **INT-1 落实**：本工作包实现，见第 4 节。
- **已落实**：接收方已交付。INT-1 通过相关门禁的结果或抽查代码确认其生效，没有逐条复核。
- **延后**：需要接收方或基线裁决，列出责任模块。
- **知会**：说明性内容，无需动作。

没有写出的条目都是"知会"或"已落实"。

### 5.1 INT-1 落实的请求

| 请求 | 条目 | 落实 |
|---|---|---|
| M01-M02-to-M00 | 2、3、8 | enums（ReconEngine、ReconStage）、rng_streams 11–16、`job.log` 事件 |
| M05-to-M00 | 4 | `.oxlintrc.json` 覆盖规则 |
| M06-to-M05 | 1 | `drawCount()` 与实际绘制一致 |
| M06-to-M11 | 1 | P600 模型路由 |
| M07-to-M00 | 2 | `mk/environment.mk` |
| M07-to-M08 | 1、5 | 插件命令终态事件；慢任务防饿死 |
| M07-to-M11 | 1 | 同版本 env 心跳下发 |
| M08-to-M00 | 1、2 | known_kinds；state_ext 字段 |
| M08-to-M11 | 1 | R12–R16、R62、profiles、caps 路由（`rest/fleet.py`） |
| M09-to-M08 | 1、2、3、5 | resume 调用传递；114 优先级；remedy；state_ext 的 M09 字段 |
| M09-to-M11 | 1、4 | skeleton chain 用例；故障注入 REST |
| M09-to-M15 | 1 | `bindSafety` |
| M10-to-M00 | 1 | known_kinds |
| M10-to-M08 | 1、2 | 剧本度量；`extend_deadline` |
| M12-to-M15 | 1、4 | `timelineTrack`；`setNotifier` |
| M13-to-M00 | 1、3 | SensorKind；`sens` 字段 |
| M13-to-M08 | 1 | roster `sensors[]` |
| M13-to-M11 | 1 | runtime.yaml 插件名 |
| M14-to-M00 | 1 | agent 事件 known_kinds |
| M14-to-M08 | 2 | 估价饥饿（`slow.py`） |
| M14-to-M11 | 1、2 | `start_after`；`anet` 类别别名 |
| M01-to-M03 | 4（部分） | job-worker 受监管时写心跳（进程能进入 RUNNING） |
| M14-to-M15 | 1 | `bindAgents` 与面板开合 |
| M15-to-M11 | 1 | `apiDelete` |
| M15-to-M12 | 3 | 轨道模型接入 |
| M16-to-M00 | 1 | `id_prefix` |
| M16-to-M10 | 2、5 | 被拒调用退避（事件量）；早期事件可见（经 M11 的首见补拉） |
| M16-to-M11 | 1、3 | 生产者刚启动时补拉订阅之前的事件；`default_scenario_id` |
| M16-to-M15 | 1 | 诚实标识（世界面板、参数未辨识、强制档位） |
| SK-B-to-M00 | 2 | sim-core 事件 kind |
| MS12-to-M11 | 1 | fake_gw 调用终态（D1-AC-35 缺口） |

### 5.2 按接收方的处置

| 接收方 | 请求文件 | 处置 |
|---|---|---|
| M00 | M00-B-to-M00 | 延后：第 1 条 BOM（`tools/ci/bom.json`）仍缺，check-deps 降级运行；第 5 条文档偏差待基线裁决；第 3、4 条已落实 |
| M00 | M01-M02-to-M00 | INT-1 落实第 2、3、8 条；其余（IR schema、夹具、golden）已落实 |
| M00 | M03-to-M00、M06-to-M00、M09-to-M00、M11-net-to-M00、M15-to-M00 | 已落实或知会（lint、vitest 纳入与代理问题 MS12 已处理） |
| M00 | M05-to-M00 | INT-1 落实第 4 条；第 3 条 perf-snapshot 字段待 perf 阶段核对 |
| M00 | M07-to-M00 | INT-1 落实第 2 条；第 1、3 条已落实 |
| M00 | M08-to-M00 | INT-1 落实第 1、2 条；延后：第 5 条 NED 访问 lint（`check_py_imports.py` 规则）、第 3 条 warnings 与 110 冲突（需 12 §5.3 裁决） |
| M00 | M10-to-M00 | INT-1 落实第 1 条；延后：第 2 条 detail 字符串登记、第 4 条 R24 `coverage_grid` schema |
| M00 | M11-R-to-M00 | 第 4 条 `make test-fixtures` 已落实；延后：第 1 条 `sys_procs` state 枚举、第 2 条事件信封、第 3 条 bench-result 字段、第 5 条 AST 规则 |
| M00 | M11-api-to-M00 | 延后：第 1 条 bus_keys 构造函数补齐、第 2 条 OpenAPI 快照、第 3 条 `error.retry_after`、第 5 条 interest `record_scope`；第 4 条已随 known_kinds 合入 |
| M00 | M12-to-M00 | 延后：`rec/markers` 的 epoch、`meta.segments[]`、`workerCmd.t_ns`、`perf/rec` key（M12 回放 ext） |
| M00 | M13-to-M00 | INT-1 落实第 1、3 条；延后：第 2 条 SensorPose48 flags 位、第 4、5 条事件与 detail 登记 |
| M00 | M14-to-M00 | INT-1 落实第 1 条；延后：第 2、3 条 task schema 与 `rgb.zoom.default_accept` |
| M00 | M16-to-M00 | INT-1 落实第 1 条；第 3 条已落实；延后：第 2 条 `check-thresholds.mjs`、第 4、5 条 G1 时长与 skip 口径 |
| M00 | MS12-to-M00、SK-B-to-M00、SK-E2E-to-M00、SK-F-to-M00 | 延后：BOM、OpenAPI 快照、`leaseReply.seat.since_unix_ns`、端口偏移只有 10 档（SK-E2E 第 3 条）；其余已落实 |
| M00 | M00-to-all | 知会（mk 扩展点约定），INT-1 新建的 mk 文件遵守该约定 |
| M02 | M00-B-to-M02 | 已落实（georef 交付） |
| M03 | M00-B-to-M03、M00-to-M03、M11-R-to-M03 | 已落实（validate 通过；`awr data *` 已注册） |
| M03 | M01-to-M03 | INT-1 补了 job-worker 心跳（进程可以进入 RUNNING）；延后：第 1 条 `IngestFromArrays`、第 4 条 `svc/job/*` 服务接线（见 7.5），D1-AC-22 依赖 |
| M03 | M07-to-M03 | 已落实（`worlds/_shared` 由 `env-assets` 生成） |
| M04 | SK-F-to-M04 | 已落实（交互用例走真实 `ray_hit`） |
| M05 | M06-to-M05 | INT-1 落实第 1 条；第 2 条生产包扫描已通过；第 4 条点云拾取（P1）延后 |
| M05 | M07-to-M05、M15-to-M05、SK-F-to-M05 | 已落实或知会 |
| M06 | M05-to-M06、M07-to-M06、M12-to-M06、M13-to-M06、M14-to-M06、M15-to-M06、SK-E2E-to-M06、SK-F-to-M06、M11-net-to-M06 | 已落实（feat-matrix、warmup、layout、smoke、交互用例通过）；延后：M14-to-M06 协作叠加图层（S3，ext）、M13-to-M06 检测叠加（ext） |
| M07 | M00-B-to-M07、M03-to-M07、M05-to-M07、M06-to-M07、M08-to-M07、M12-to-M07、M15-to-M07 | 已落实（env 命令经 `register_command_handler` 登记；预设与阵风在 S1 中生效）；延后：M06-to-M07 第 1 条 `window.__env.setPreset` 钩子与 warmup 预设覆盖，需在 perf 阶段复核；M12-to-M07 回放 `env_at`（ext） |
| M07 | M16-to-M07-M12-M13 | 延后：`perf/m07/cases.mjs` 未登记；第 2 条 `length_m` 口径以 16 §12.3 为准（S1 剧本已按半波长 120 m 书写） |
| M08 | M00-B-to-M08、M04-to-M08、M11-R-to-M08、M11-api-to-M08 | 已落实 |
| M08 | M07-to-M08、M09-to-M08、M10-to-M08、M13-to-M08、M14-to-M08 | INT-1 落实第 4.1 节所列条目；延后：M09-to-M08 第 4 条 escalate、第 6 条 checkpoint 扩展段；M13-to-M08 第 3 条 `register_estimate_hook`、第 4 条剧本机体 caps 与传感器子集、第 5 条慢任务按名计时；**M14-to-M08 第 1 条 lease 接受 agent 角色（阻塞 S3）**、第 3 条外部度量接收器、第 5 条锁步钩子；M10-to-M08 第 4、5 条（提供者接管时的原生 pause、停滞判据） |
| M09 | M00-B-to-M09、M07-to-M09、M10-to-M09 | 已落实（M10 经 `safety/fault` 查询注入故障）；延后：M08-to-M09 第 1 条 `fault/inject` 命令路由（现由查询路由代替，功能可用）；M10-to-M09 关于 `path_wh(env)` 的风场形状问题待 M09 答复 |
| M09 | M08-to-M09 | 第 2–6 条知会；第 1 条见上 |
| M10 | M08-to-M10、M13-to-M10、M14-to-M10、M15-to-M10 | 已落实（S1 功能链路通过）；延后：M13-to-M10 第 5 条与 M14-to-M10 属 S3（ext） |
| M10 | M16-to-M10 | INT-1 落实第 2、5 条（第 5 条经 M11 的首见补拉）；第 1 条 S1 不能成功，根因见 7.1（ADR）；第 3 条倍速见 7.1；第 4 条 vehicle_sets 编排见 7.2 |
| M11 | M00-B-to-M11、M00-to-M11、M01-to-M11、M04-to-M11、M11-R-to-M11、M11-api-to-M11、M11-net-to-M11、SK-E2E-to-M11、SK-F-to-M11、M13-to-M11 | 已落实或 INT-1 落实（见 5.1）；延后：M13-to-M11 第 2 条前端 roster 保留 `profile_id`、第 5 条 FakeSource sensors；M01-to-M11 第 2 条 job-worker 进程条目 |
| M11 | M06-to-M11、M07-to-M11、M08-to-M11、M09-to-M11、M14-to-M11、M15-to-M11、MS12-to-M11 | INT-1 落实（见 5.1）；延后：M09-to-M11 第 3 条事件批量追加性能（perf 阶段复核）；M14-to-M11 第 4 条 agent-runtime 查询权限 |
| M11 | M12-to-M11 | 延后：第 1–3 条回放 open 与 roster 覆盖、playback 串行（ext）；第 4 条 `runtime.yaml` 的 `recorder:`/`replay:` 参数段；第 5 条 UNFINALIZED 修复 |
| M11 | M16-to-M11 | INT-1 落实第 1、3 条；延后：第 2 条 `netem_proxy.py` 控制口、第 4 条 `bench_state --with-flight60`（perf）、第 5 条 `perf/m11/cases.mjs` |
| M12 | M00-B-to-M12、M06-to-M12、M07-to-M12、M11-R-to-M12、M11-api-to-M12、M11-net-to-M12、M15-to-M12、SK-F-to-M12 | 已落实（`setFocus`、`onEpoch` 等存在，`test_replay`、`test_processes` 见全量 pytest）；延后：M15-to-M12 第 1 条传输控制动作与快捷键的完整接线、`perf/m12/cases.mjs` |
| M13 | M02-to-M13、M06-to-M13、M08-to-M13、M14-to-M13 | 已落实（传感器插件、`engine/sensors`）；延后：M14-to-M13 第 2 条 R-12、第 3 条 `render_thermal_frame`（S3） |
| M14 | M13-to-M14、M16-to-M14 | 延后：`perf/m14/cases.mjs` 注册项不合法（lint 警告 PERF-E017） |
| M15 | M00-B-to-M15、M00-to-M15、M01-to-M15、M05-to-M15、M06-to-M15、M07-to-M15、M08-to-M15、M09-to-M15、M10-to-M15、M11-api-to-M15、M11-net-to-M15、M12-to-M15、M13-to-M15、M14-to-M15、SK-B-to-M15、SK-E2E-to-M15、SK-F-to-M15、M16-to-M15 | 大部分已落实，INT-1 补齐绑定与诚实标识（见 5.1）；延后：M01-to-M15 第 1–3 条重建任务页（D1-AC-22）；M16-to-M15 第 3–5 条弱网徽标、命令面板条目、toast 计数口径（perf 用例） |
| M15 | M15-to-M03-M05-M07-M09-M10-M12-M13-M14 | 知会：各领域 store 已由所有者接管 |
| M16 | M00-to-M16、M01-to-M16、M03-to-M16、M05-to-M16、M06-to-M16、M07-to-M16、M08-to-M16、M11-R-to-M16、M11-api-to-M16、M11-net-to-M16、M12-to-M16、M13-to-M16、M14-to-M16、M15-to-M16、SK-E2E-to-M16、SK-F-to-M16 | harness 登记与 perf 用例已落实或留在 perf 阶段；延后：M03-to-M16 第 1 条 `scenarios/zones/<world>.geojson`；M08-to-M16 第 2 条 S1 前 60 s 逐字节（D1-AC-31 口径）；M14-to-M16 S3 用例（ext） |
| 联合 | M00-B-to-M10-M16、M00-B-to-M14-M01-M04-M13 | 已落实（schema 审阅完成，validate 与 test-contracts 通过） |
| 联合 | M09-to-M06-M07、M09-to-M10-M16 | 已落实（安全行字段、configure、让行优先级、剧本度量）；延后：逆风分量 `w_head`（M07） |
| 联合 | M11-api-to-M07-M09-M10-M13-M14 | 知会（线上格式），各生产者按格式发布 |
| 联合 | M16-to-M05-M06、M16-to-M08-M09 | 延后：画质参考页与 ladder `n=`（perf）；M16-to-M08-M09 第 2、4 条即 7.1 的倍速与能量问题；第 5 条 500 架 link_drop 注入接口 |
| 联合 | MS12-to-M15-M16、SK-B-to-M07-M09-M10-M13 | 已落实 |

## 6 主界面截图与视觉核对

截图条件：生产构建，`AWR_PORT_OFFSET=6 make run`（demo profile，深圳 + S1），Chromium SwiftShader（Tier S），
页面揭开遮罩后再等 45 s（S1 进行到 T+2:19 与 T+3:13，两机处于转场或扫描中）。

- `.cache/impl/shots/int1-main-1920x1080.png`
- `.cache/impl/shots/int1-main-1280x720.png`

页面自动扫描结果（shots-final.log，两种分辨率一致）：

- 文本节点、`aria-label`、`title`、`placeholder` 中 emoji 与禁用字形为 0；
- `svg.lucide`（lucide-react 产物）为 0；`backdrop-filter` 为 0；
- `render.calls` 8 等于 pass plan 8，GL 错误 0；
- pageerror、控制台 error 与 HTTP ≥ 400 都是 0；
- 标题为"ANet Drone · World Runtime"。

对照 15（视觉设计规范与色卡）的逐项核对：

| 项 | 结论 |
|---|---|
| 色卡 ANet Graphite | 符合。暗色底、灰阶面板与文字，没有绿色与黄色；状态徽标（"飞行 · 航线"）用中性色反白 |
| 一处红 | 符合。全屏只有 HUD 的"呈现间隔 p95"数值为红色（SwiftShader 下 200 ms / 100 ms 超出 30 FPS 目标，语义正确）。告警徽标此前因 job-worker 状态横幅显示红点，修复后不再出现 |
| logo | 符合。顶栏头像标 24 px（`data-brand-avatar`，`/brand/avatar-96`）加字标"ANet Drone"，没有改色，不浮在点云画布上 |
| 图标 | 符合。菜单、图层、环境预设、相机工具栏都是线性图标（morphicons 注册表，没有 lucide-react 类名），没有 emoji |
| 布局 | 符合。浮层式左栏与右侧机群栏不改变画布尺寸（layout 用例通过）；1280×720 下左栏自动收起、HUD 精简、倍速改为下拉 |
| 诚实标识 | 符合。世界卡片显示"北向未验证（近似）"；环境面板显示 S1 下发的风（6.0 m/s，SE 135°，蒲福 4 级） |
| 待改进（M06） | ViewCube 面上的方位字在这两种分辨率下只有约 6 px，对比度低，难以辨认。建议面字号 ≥ 11 px，或只在悬停时显示 |
| 待改进（M15、M12） | 时间轴中部的 L4 空心圆标记（route 类）在暗底上像一个"°"字符，建议加大到 6 px 并配 Tooltip |
| 说明 | 点云呈块状，是因为 Tier S 在 SwiftShader 负载下处于 soft-min（预算 10K 点，HUD 标"受点预算限制"），这是规定的降级行为；无人机在该视距下以标记点显示 |


## 7 遗留问题（按模块）与修复建议

### 7.1 M09、M10、M16（基线）：S1 能量 RTL 与规格冲突（D1-AC-15，P0，阻塞）

- 现象：p600-01 在最后一圈转到主塔远侧时，t_rtl 包含翻越主塔的 z_rtl（≈ 388 m）。
  这时 `t_rem_usable < 1.3·t_rtl`（例如 552.99 < 560.69），触发 ENERGY_RTL（约 671–685 s）。
  随后下段任务 ABORTED，返航爬升耗电让 soc_min 降到 0.178，剧本兜底事件 `soc-guard-01` 计为 1 次 guard，
  四个谓词因此连锁失败。全量 pytest 与单独运行的结果一致（test_s1_final.log、pytest-final.log）。
- 根因：12 §7.2 与 ADR-052 的 S1 能量设计只在扫描结束点计算 t_rtl，没有计算扫描途中位于塔远侧的最坏点。
  M09 的规则（1.3 裕度、z_rtl = max(z_now, z_home + 30, H_top + 5)、直线返航）按规格执行，本身没有错。
- 已验证：裕度改为 1.0（仅实验，未提交）时 9 个谓词全部为真（覆盖率 0.997、最小间距 16.24 m、guard 0、
  阵风 pos_err 0.38 m、soc_min 0.349、home 误差 0.11 m）。
- 建议（ADR 选一）：
  - ① M09 的 t_rtl 改为绕塔返航路线（按 M04 走廊求绕行路径，不翻越 H_top），与 rtl 分发的实际路线一致。这是最符合物理的做法。
  - ② M16 与 12 §7.2 调整 S1 参数（下段圈数或起始 SOC），把全程最坏点计入设计检查。
  - ③ 明确 1.3 裕度只用于 H_top 没有被高估的场景。
- 倍速：S1 ci profile 实测每 tick 0.8–1.3 ms，达不到 ×10（×10 墙钟 286 s，限额 210 s）。
  - 主要开销是 safety（约 33%）、env stage（约 21%）、cmd_watch、tracker 与 contact，`guard`、`mission_guard` 持续报 `sim.stage.overbudget`。
  - 建议 M09 把 guard 与 mission_guard 向量化或降频，M07 的 env stage 按 every 5 摊销；在 perf 阶段用 fleet_ladder 复核。

### 7.2 M10、M16：ladder 冒烟失败（`test_ladder_smoke`，核心剧本）

- 现象：ladder n10 以 ×5 运行，结果 FAILED。失败的谓词是 missions_done、landed_all、guard_events 20、min_separation_m 9.66（要求 ≥ 14）。
  api 事件流中没有 FleetGuard CONFLICT、AVOIDING 事件，说明 guard 计数来自 M09 的剧本度量。
- 分析：n10 的 l0 组与 l1 组 home 相距 12 m（`origin_enu_m` −393 与 −381，`spacing_m` 24），orbit 半径 3 m，高度 60 m 与 75 m。
  爬升段两组的垂直间隔为 0，水平间距只有 6–12 m，所以最小间距 < 14 m。
  M16-to-M10 第 4 条也指出每个 set 展开为一个多机 orbit 的编排问题。
- 建议：M16 把各组 home 的水平间距拉开到 ≥ 20 m（或错开起飞时刻，按高度分层依次爬升），M10 在 vehicle_sets 展开时
  校验同一时段的最小间距；随后复跑 `tests/e2e/test_scenarios.py::test_ladder_smoke`。

### 7.3 M08、M11：sim-core 重启时限（D1-AC-11a，P0）

- `test_kill_api` 已通过。INT-1 把 api 导入耗时从 1.62 s 降到 1.06 s：之前 `package.catalog → ingest` 会连带导入 scipy.ndimage。
- `test_kill_sim_core` 的重启要 3.17 s（阈值 3.0 s），功能断言成立。按 `make run` 日志拆分：
  - supervisor 退避首档 0.5 s（19 §4.2）；
  - spawn 到 `init_child` 0.86 s（`python -m awr.sim.runtime` 先导入 main 模块，其中 numba 约 0.4 s）；
  - `init_child` 到 ready 1.5 s（世界载入、插件导入，其中 `awr.sim.safety` 约 0.7 s、numba 缓存）；
  - supervisor 巡检 ≤ 0.2 s。
- 建议：
  - M08 先调用 `init_child`，再延迟导入 numba 核；插件导入与世界载入并行；
  - M09 减少 `awr.sim.safety` 的导入期工作；
  - 或者经 19 修订，把退避首档降到 0.1 s。
  - 最终在排他性能锁下复测。

### 7.4 M15：模态探针（D1-AC-21，已修复，请复核）

- `ui/hotkeys/registry.ts` 的模态探针此前把处于退出过渡的对话框也算作打开，所以 Esc 之后 200 ms 内的 Mod+K 被吞掉。
  INT-1 已在选择器中排除 `data-closed` 与 `data-ending-style`。
- 建议 M15 为此补一条单元用例，并检查其他基于 DOM 探测的守卫是否有同样的问题。

### 7.5 M03、M01、M15：重建任务的服务与 UI 接线（D1-AC-22 的提交入口）

- 进程层已修复：job-worker 此前不写心跳，demo profile 下每 30 s 判定启动超时，重启 5 次后熔断，顶栏一直显示"进程 job-worker 状态 STARTING"。
  INT-1 在 `jobs/worker.py` 补上 `init_child` 与心跳后，进程可以进入 RUNNING，停止时 rc 0。
- 功能层仍缺：
  - M01-to-M03 第 1 条 `IngestFromArrays`；
  - 第 4 条 bus 服务 `svc/job/*` 与事件发布；
  - M01-to-M15 的任务页（进度 ≤ 4 Hz、`scale_status` 徽标）。
  - 目前 Mock 重建只能经 CLI（`python -m awr.reconstruction run`）提交。
- 长任务执行期间不会写心跳（stale_s 120 s）。建议 M03 把心跳放到 JobContext 的进度回调里。

### 7.6 M08、M14：S3 前置（D1-AC-16，P1）

- lease 对 agent 角色的 acquire/release（M14-to-M08 第 1 条）、外部度量接收器（第 3 条）与锁步钩子（第 5 条）还没有实现。
  S3 会停在 AGENT 租约。

### 7.7 M09、M08：ext 项

- escalate 第⑦步仍回 109 D1_EXT；checkpoint 扩展段（M09-to-M08 第 6 条）没有接线，影响 D1-AC-11b。
- `fault/inject` 命令路由未登记，目前由 `safety/fault` 查询代替。

### 7.8 M11、M16：工具与注册

- `netem_proxy.py` 控制口、`perf/m11/cases.mjs` 缺失。
- inproc 运行 id 下 `/api/runs/{run}/bookmarks` 返回 422，只影响 inproc。
- `tests/e2e/timeline.spec.ts` 回放段仍是 fixme，要等 M12 的录制夹具接入。

### 7.9 M00：契约与工具

- BOM（`tools/ci/bom.json`）、OpenAPI 快照、bus_keys 构造函数、NED 访问 lint、`check-thresholds.mjs` 都还没有完成。
- 端口偏移只有 10 档，不够 14 个并行 agent 使用，建议扩到 0–31。

### 7.10 M14、M07、M12、M13：perf 注册

- `perf/m14/cases.mjs` 注册项不合法（PERF-E017）。
- `perf/m07`、`m12`、`m13` 的 `cases.mjs` 缺失，perf 阶段之前需要补齐。

### 7.11 负载敏感用例（各所有者）

以下用例在全量并发下偶发失败，单独运行时通过：

- pytest：`tests/rt/test_interest.py` 的两个席位宽限用例，`tests/environment/test_env_e2e.py`，`tests/rt/test_limits.py::test_connection_limits`。
- vitest：`tests/m11/alloc.test.ts`（保留堆 65608 B，限额 65536 B）。
- Playwright：`motion.spec.ts` 的 reduced 用例（整批运行时有 7 个动画在运行）。

建议：
- 时间窗类断言改为按条件轮询；
- 堆增长阈值留出余量，或者在测量前主动 GC 两次；
- motion 用例在打开面板前等待页面空闲（没有正在运行的 Toast 或过渡）。

## 8 依赖与安装

本轮没有安装任何依赖，也没有修改 `package.json`、`package-lock.json`、`pyproject.toml`、`requirements.lock`。
所有修复都在现有依赖范围内完成。
