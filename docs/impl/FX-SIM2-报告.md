# FX-SIM2 验收与加固报告（sim-core 服务面、S3 与 ext 接线）

| 项 | 内容 |
|---|---|
| 工作包 | FX-SIM2（区域：sim-core 服务面、S3 与 ext 接线） |
| 日期 | 2026-09-29（实现）；2026-10-01（续作：上一次执行在写完第 1–3 节后因额度中断，本次核对改动、补跑全部功能测试并完成第 4、5 节） |
| 依据 | INT-1 §3、§7.5、§7.6、§7.7；03 §8.4（D1-AC-11b、D1-AC-16）、ADR-019、ADR-021、ADR-027、ADR-033；M08 PRD FR-004、FR-062、FR-064、FR-086–FR-090；请求 M07-to-M08 第 1、4、5 条，M14-to-M08 第 1、2、3、5 条，M13-to-M08 第 1、2、4、5 条，M13-to-M00 第 3、9 条，M09-to-M08 第 4、6 条，M08-to-M09 第 1 条，M10-to-M08 第 5 条，M14-to-M13 第 2 条，M13-to-M10 第 5 条，M01-to-M03 第 1、4、5、6 条 |
| 约束执行 | 未安装依赖；未使用 git；未运行性能基准与 `perf` 标记用例（混沌用例 `tests/chaos` 均带 perf 标记，只做收集检查，验收阶段在排他锁下执行）；Playwright 用私有测试构建（会话临时目录，`AWR_PERF_DIST`/`AWR_WEB_DIST`）与端口偏移 29，未覆盖共享的 `apps/web/dist`；功能测试结果见第 4 节 |
| 结论 | 任务 1–6 全部完成。S3 端到端 `tests/e2e/test_scenarios.py::test_s3`（`AWR_E2E_FULL=1`）通过（D1-AC-16 功能判定），续作时在含其他工作包并发改动的当前工作树上复跑仍通过。全量 `pytest -m "not perf"` 除并发中的 ladder 工作包的 13 个静态用例与 1 个负载敏感用例外全部通过；vitest 948 通过；skeleton、interaction、m01/recon 的 Playwright 功能用例通过；`make lint` 通过。规格变化以 ADR-057（慢任务公平轮转与预算借贷）、ADR-058（sim-core 服务面追加扩展点）、ADR-059（S3 布设与收尾）落地，并同步修订 M03、M08、M09、M10、M13、M14、M16 PRD 与 17 |

## 1 修复内容

### 1.1 任务 1：M08 慢任务饥饿（公平轮转 + 预算借贷，ADR-057）

INT-1 的临时做法是"atomic 任务连续顺延满 50 ms【墙钟】后强制执行一次"。本包按 ADR-057 重写 `python/awr/sim/runtime/slow.py`：

| 机制 | 内容 |
|---|---|
| 启动门槛 | `min(p99, slow_budget_us)`：p99 超过每轮上限的任务不再因门槛永远不可达而饿死 |
| 积分与队首 | 有待处理请求（`pending()`）而剩余预算不足时，把本轮剩余预算记为积分，下一轮从最早顺延的任务开始；`剩余 + 积分 ≥ 门槛` 即启动；顺延至多 10 轮，另有 20 ms【墙钟】上界（追帧时一轮很长） |
| 借贷 | 启动后超出本轮剩余预算的部分记入借贷（上限 4 ms），之后各轮扣还（保留 100 µs 下限） |
| 批处理 | 启动后同一轮内续处理排队请求（≤ 4 条）：合同网同时对 3 架报价不逐轮排队 |
| 追帧预算 | 主循环预算按本轮 tick 数折算：`max(100, min(1000, n·2800 − 已用))`（n = 1 时同原式）。S3 多进程实跑中 ×10 请求、实际 RTF 约 2.2，每轮约 50 tick、约 90 ms，原式恒为 100 µs 下限，十余个慢任务轮转一圈 > 1 s |
| 其他 | `ctl/sim-core/query` 排队 > 8 个回 111（M08 §7.2 既有规定）；暂停时有顺延请求则不阻塞等待；`state/sim-core/perf` 增加 `slow_ms_per_s{<task>}`（M13-to-M08 第 5 条）、`slow_debt_us`、`slow_deferred`、`slow_defer_max` |

效果（多进程 S3，纽约）：修改前 3 个报价中 b2、b3 迟到（475），b3 的估价 3 × 1 s 无回复；修改后 3 个报价都在 3 s【仿真】截止前返回。
`tests/environment/test_env_e2e.py` 的 `POST /api/env/query` 由"200 或 202"收紧为 200。

### 1.2 任务 2：插件命令终态（ADR-058 第 1 条）

`CommandEngine._plugin_op` 生成 `Call`（slot −1）并按机体命令同一套事件推进：准入 `cmd.accepted`（V1）→ apply_tick 的 ingest
发 `cmd.running`（V2，observed_state `applied`）→ 处理者回复无 `watch` 时同一步 `cmd.succeeded`（OK、V4、simulated、metrics
`t_exec_s`，data 带处理者 `result`）；有 `watch(ctx)` 时逐步判定 succeeded/failed，截止（`deadline_s`，缺省 10 s【仿真】）202；
剧本重置 canceled 6；`extend_deadline` 与幂等 duplicate 覆盖此类调用。INT-1 在准入时立即发 succeeded 的临时做法已去掉。
env/preset、mission/*、fault/* 均受益；WS 客户端先收 accepted 再收 running、succeeded。

### 1.3 任务 3：lease、外部度量、锁步与 S3

| 项 | 内容 | 文件 |
|---|---|---|
| agent 租约（M14-to-M08 第 1 条） | `ctl/sim-core/lease` 接受 agent principal（K_entry 验签、不要求席位）的 `acquire{owner: AGENT}` 与 `release{return_to: previous}`：抢占 MISSION/SWARM 并入栈，OPERATOR 持有时 100；agent 的席位操作与非 AGENT 类别 115；`lease.*` 事件带 by | `sim/runtime/main.py`、`sim/core/authority.py` |
| 外部度量接收器（第 3 条） | `metrics.register_external_metric`；M08 声明 `target_confidence{target_id}`、`t_conf_s{target_id, threshold}`（owner M14）；`scenario/metric{value}` 写输入日志、apply_tick 生效；无取值时读取抛 LookupError；重置清空；随 checkpoint 保存 | `sim/core/metrics.py`、`sim/core/command.py`、`sim/runtime/main.py` |
| 锁步钩子（第 5 条） | `SimCore.register_post_step_hook(fn)`：`fn(t_sim_ns, epoch, segment)` 每 tick 同步调用，登记后逐 tick 推进；M14 `LockstepDriver.step_to` 直接挂接（用例验证 SimScheduler 定时器按仿真时刻触发） | `sim/runtime/main.py` |
| M14 xfail 转绿 | `tests/agent/test_simcore_integration.py::test_agent_explicit_lease` 去掉 xfail，并补 release(previous) 断言 | 测试 |

S3 多进程联调暴露的其余问题（均属 S3 路径，本包修复）：

| 问题（实测现象） | 修复 | 文件 |
|---|---|---|
| goto（route auto）在机体运动时由 M10 提供者"先刹停再走直线"，刹停段结束后停在静止点，调用 202 截止（b1 离观测点 198 m 悬停 100 s） | 刹停段有 `next_phase` 时接续下一段 | `sim/mission/tracker.py` |
| MISSION 被 AGENT 抢占后交还续飞：转场之后没有执行"剩余部分"，而是从作业项起点重来，reference 跳回起点，pos_err 55–407 m，`POS_ERR_FAILSAFE` | 转场成功后先执行 `pending_items`（从断点 τ 起） | `sim/mission/engine.py` |
| 环绕入圆时 `center` 航向一步翻转 180°，姿态环耦合出 28.7° 倾角（设定值 7°），`TILT_ERR_KILL`，b1 坠毁 | 跟踪器输出航向按 45°/s 限幅（`YAW_RATE_MAX`），同工况最大倾角 14.2° | `sim/planning/kernels_track.py`（M10 PRD §6.5.8 表补一行） |
| 提供者接管（TRAJ）的调用在规划或刹停期间参考静止，被 M08 以"5 s 未前进 0.2 m"判 203（M10-to-M08 第 5 条）；判 202/203 后提供者仍在驱动轨迹，与随后的新调用争用 TRAJ 参考 | TRAJ 调用只在"参考前进 ≥ 0.2 m 而机体不动"时判 203；M08 自身判定的 202/203 先 `provider.cancel` 并交回 HOLD | `sim/core/command.py`、`sim/core/calls.py`、`sim/runtime/ckpt.py` |
| S3 剧本几何与时序（与实现无关）：出生点间距 6 m（同时起飞必然 < 10 m）、b1/b2 待命航线起步即交叉（2.57 m、两次 AVOIDING）、观测高度与搜索机同为 60 m（10.03 m）、复核候选在 710 s 才返航，900 s 时未落地，导演以超时判 FAILED | ADR-059：间距 12 m 并按航向排序；观测高度 75 m；确认后 `mission.abort`（`on_abort = rtl`）加 `cmd rtl` 兜底 | `datasets/scenarios/authoring.py`、`scenarios/s3-newyork-sar.json`（重生成） |

S3 最终实测（`AWR_E2E_FULL=1 pytest tests/e2e/test_scenarios.py::test_s3`，187.5 s 墙钟）：SUCCEEDED；报价 b1 0.07 中标、
b2 0.016、b3 不可行 119；b1 以 AGENT 租约抢占 MISSION，goto 到观测点、环绕驻留，热成像确认；委派 effect OK、`simulated = true`；
`target_confidence{t1}` 0.9、`t_conf_s` 158.9 s；最小间距 11.8 m；`guard_events` 0；b1、b2、c1 于 300–335 s 落地，a1 于 709 s
落地；无 incidental 确认。证据链 submitted、find、quote、awarded、effect、accepted 齐全。

续作复跑（2026-10-01，当前工作树已含 FX-SIM1 的绕行返航 ADR-054 与 ladder ADR-062 等并发改动，负载均值 6–10）：
同一命令 1 passed，用例耗时 181.6 s【墙钟】；用例内的断言（中标者 b1、b3 为 119、委派 effect OK 且 simulated、
置信度 ≥ 0.9 且确认时刻 ≤ 300 s、证据链齐全、无 incidental）全部成立。

### 1.4 任务 4：M13 传感器位姿链与检测器

| 项 | 内容 | 文件 |
|---|---|---|
| roster `sensors[]` | INT-1 已接 `register_sensor_rig`；本包补：`fleet/add` 接受剧本 `sensors` 子集（roster 只列这些），checkpoint 保存条目的 `sensors[]` | `sim/runtime/main.py`、`sim/runtime/ckpt.py` |
| 网关映射 | 网关按 roster `sensors[]` 广告 `uav/{id}/sensor/{name}/pose` 并把 SensorPose48 行路由过去（M11 既有实现）；新增进程内 sim-core + 真实 api/WS 的端到端用例 | `tests/sensors/test_pose_chain_e2e.py` |
| state_ext GNSS 与 sens | 追加扩展点 `register_state_ext_hook`，M13 登记 `SensorRuntime.state_ext_fields`：`loc.gnss_fix/sats/eph_m/epv_m/hdop`、`loc.err_enu_m`、`sens.gimbal/imu`（字段 INT-1 已在 schema 登记，用例做 schema 校验） | `sim/fleet/stages/registry.py`、`sim/runtime/main.py`、`sim/sensors/plugin.py` |
| 剧本能力集与传感器子集（M13-to-M10 第 5 条） | 剧本导演在 `fleet/add` 前调用 `configure_vehicle(vehicle_id, sensors, caps, delegated)` | `sim/mission/director.py` |
| R-12（M14-to-M13 第 2 条） | `configure_vehicle` 增加 `delegated`：所列能力只在机体持有 AGENT 租约时检测（由确定性租约状态派生）；导演按剧本 `agents.tasks[].capability` 与成员能力的交集设置（S3 的 b1–b3 为 `thermal.imaging`）。S3 实测 b1 在待命途中不再先确认 t1 | `sim/sensors/runtime.py`、`sim/sensors/detector.py` |
| M14 import 白名单 | 按 M14-NFR-018 与 10 §3（`awr.agent` 不 import `awr.sim`）：不开白名单；M14 经 `ctl/sim-core/estimate` 取报价、经 `evt/sim-core/sensor` 消费 `sensor.detect`；`render_thermal_frame` 只在 `tests/agent` 中用于核对长度。`check_py_imports` 通过 | 无代码改动 |

### 1.5 任务 5：ext 接线

| 项 | 内容 | 文件 |
|---|---|---|
| `fault/inject` 命令路由（M08-to-M09 第 1 条） | M09 登记 `fault/inject`、`fault/clear`（需席位，内部 principal 放行）；REST R16、R62 改经 `ctl/sim-core/cmd`，注入写输入日志并有调用终态；查询 `safety/fault` 保留给进程内同步调用 | `sim/safety/__init__.py`、`sim/safety/service.py`、`api/rest/fleet.py` |
| escalate 第⑦步（M09-to-M08 第 4 条） | 放行：确认令牌由网关第③步校验、生产者复核存在性（缺失 112），未装配 M09 时 109；apply 时调用 `apply_operator`（HOLD/ESCALATE → ELAND → FAILSAFE），调用随即 succeeded；apply 时复核不再对 escalate 查矩阵 | `sim/core/command.py` |
| checkpoint 扩展段（M09-to-M08 第 6 条、M07-to-M08 第 4 条） | `capture` 的 meta 增加 `ext{safety, env, metrics}`；恢复时逐段 `restore`（env 服务未构造时先 `ensure_service`），`sim.restarted` data 带 `ext` 段名 | `sim/runtime/ckpt.py` |
| make chaos（D1-AC-11b） | `tests/chaos/test_chaos_ext.py` 的 `test_checkpoint_recovery` 由 skip 改为实测（回滚 ≤ 1 s、RUNNING 后新 epoch 首帧 ≤ 1.5 s、M09/M07 扩展段已恢复、在途 takeoff 到达终态），新增 `test_poison_checkpoint`（恢复后 5 s 内再崩改用上一代）；两者带 perf 标记，本包只做收集检查（`pytest --collect-only -m chaos` 7 例），功能部分由 `tests/safety/test_ext_wiring.py` 进程内覆盖 | 测试 |

### 1.6 任务 6：M03/M01 重建任务服务

| 项 | 内容 | 文件 |
|---|---|---|
| `svc/job/*` 与事件 | 新增 `awr/jobs/service.py`：服务线程处理 `svc/job/{submit,cancel,status,engines}`（回调只入队，线程各自的 SQLite 连接）；recon 提交转 M01 `service.submit`（R01 守卫），world_build 由本模块入队；取消（排队中直接 CANCELLED、执行中置 cancel_requested、终态 348）；重试（只接受 FAILED 且 resumable，否则 348）；状态（recon 任务附 `recon{}`、`scale_status`）与日志尾部（≤ 1000 行）；`job.state`（QUEUED、阶段、终态）、`job.progress`（≤ 4 Hz）、`world.added` 经 `evt/job-worker/*` | `jobs/service.py`、`jobs/worker.py` |
| worker 主循环 | 按任务类型的首阶段认领；按 `JOB_PLUGINS` 导入 M01 `recon_job`；runner 的 `error` 原样写入 `error_json`；崩溃标记 344 JOB_WORKER_CRASHED；`emits_events = False` 的任务由 worker 代发事件；提交可唤醒主线程；工作目录保留策略（SUCCEEDED/CANCELLED 24 h、FAILED 7 天、job.log 7 天） | `jobs/worker.py`、`jobs/queue.py`、`jobs/registry.py` |
| JobContext 心跳（INT-1 §7.5） | `progress()`、`check_cancel()`、阶段切换按 ≥ 1 s 节流写 job-worker 心跳；补 `submitted_by`、`event_sink` | `jobs/context.py` |
| REST R06、R39–R42、R64 | 新增 `api/rest/jobs.py`（只做 `svc/job/*` 转发，不 import `awr.jobs`，1 s 无回复 503 213） | `api/rest/jobs.py` |
| `IngestFromArrays`（M03-FR-021） | 新增 `world/ingest/arrays.py`（`IngestFromArrays`、`ArraysAdapter`，追加 `anchor_json`、`T_ecef_world`、`session_id`、`tags`、`staging_nonce` 可选字段）与 `world/package/arrays_build.py`（`ArraysPipeline`；两段式 `PhasedBuild`：TILING 与 PACKAGING，发布在 PACKAGING 最后一个取消检查点之后）；`build_world` 支持 `ArraysAdapter`（按 `pipeline_cls`、`build_params`、`staging_nonce`，并补阶段进度与取消检查点、取消时清 staging） | `world/ingest/*`、`world/package/*` |
| M01 切换 | TILING/PACKAGING 改用 M03 `PhasedBuild`；过渡实现 `pipeline/m03_bridge.py` 删除；`register_job("recon", ..., emits_events=True)` | `reconstruction/pipeline/*`、`reconstruction/jobs/recon_job.py` |

## 2 规格变化与文档

| 变化 | 落点 |
|---|---|
| 慢任务启动规则、借贷、墙钟上界、追帧预算、query 排队上限、perf 字段 | ADR-057；M08 FR-004、§6.10.4、§7.2（perf 行） |
| 插件命令生命周期、agent 租约、外部度量、锁步钩子、state_ext 钩子、escalate、checkpoint 扩展段、故障命令路由、TRAJ 停滞判据 | ADR-058；M08 FR-062、FR-088、§7.1.1、§7.2；M09 §6.12 入口；17 §9.4 Command 行 |
| S3 出生点、观测高度、确认后返航 | ADR-059；M16 §6.4.5 表、修订说明与示例 JSON；M14 §6.10.3 示例注记 |
| `IngestFromArrays` 追加字段、`ArraysAdapter` 与 `build_world`、job-worker 服务与心跳 | M03 §6.14 主循环段、§7.2 |
| 委派能力 `delegated`（R-12） | M13-FR-041 |
| 跟踪器航向限速 45°/s | M10 §6.5.8 参数表 |

ADR 编号：本包取 ADR-057–059，写入附录 E 并在 §7.0 索引登记。续作核对（2026-10-01）时附录 E 为：ADR-054（FX-SIM1，绕行返航）、
ADR-055（端口偏移）、ADR-056（产品名）、ADR-057–059（本包）、ADR-060–062（FX-SIM1，env 摊销、重启时限、ladder；060 已入索引，
核对时附录 E 尚无其正文）。FX-SIM1 已把代码中原先与本包冲突的引用（env 摊销、退避首档、ladder 校验分别曾写作 ADR-055、056、057）
改为 060、061、062；本包代码中的 ADR-057、058 引用与附录 E 一致。仍存在的冲突见第 5 节第 6 条（前端点云代码的"ADR-054"）。

## 3 改动文件

后端（`python/awr/`）：`sim/runtime/{slow,main,ckpt}.py`；`sim/core/{command,metrics,authority,calls}.py`；
`sim/fleet/stages/registry.py`；`sim/sensors/{plugin,runtime,detector}.py`；`sim/mission/{director,engine,tracker}.py`；
`sim/planning/kernels_track.py`；`sim/safety/{__init__,service,events}.py`（`events.py` 只增加诊断字段 `last_pub_rows`：与
`last_payload` 同一次发布的行，供用例对拍）；`api/rest/fleet.py`；`api/rest/jobs.py`（新）；
`jobs/{context,registry,queue,worker}.py`、`jobs/service.py`（新）；`world/ingest/__init__.py`、`world/ingest/arrays.py`（新）；
`world/package/build.py`、`world/package/arrays_build.py`（新）；`reconstruction/jobs/recon_job.py`；
`reconstruction/pipeline/{package,stages}.py`、`reconstruction/pipeline/m03_bridge.py`（删除）；`datasets/scenarios/authoring.py`。

数据：`scenarios/s3-newyork-sar.json`（由 authoring 重生成）。

测试：新增 `tests/sim/test_slow_tasks.py`、`tests/sim/test_service_surface.py`、`tests/safety/test_ext_wiring.py`、
`tests/jobs/test_service.py`、`tests/jobs/test_api_jobs.py`、`tests/world/test_arrays_adapter.py`、`tests/mission/test_lease_resume.py`、
`tests/sensors/test_pose_chain_e2e.py`；修改 `tests/sim/test_traj_provider.py`（TRAJ 停滞用例）、`tests/sim/test_skeleton.py`（注释）、
`tests/safety/test_lock_resume.py`（注释）、`tests/agent/test_simcore_integration.py`（去 xfail）、`tests/jobs/test_queue.py`（344）、
`tests/environment/test_env_e2e.py`（query 200、插件命令终态）、`tests/chaos/test_chaos_ext.py`（checkpoint 与毒性用例）、
`tests/sim/test_fleet_rest_faults.py`（故障 REST 改经 `fault/inject|clear` 命令转发）、`tests/safety/test_channel.py`（载荷改为与同一次
发布的行对拍：量化后未变的机体本轮不发布，而 `last_rows` 已是其最新快照，原断言把二者直接比较，可能不一致）。

续作（2026-10-01）没有修改任何代码，只补写本报告第 2 节编号说明、第 3 节漏列的 3 个文件与第 4、5 节。

文档：`docs/03-设计基线与决策记录.md`（ADR-057–059 与索引）、`docs/17-接口与实时协议规范.md`、`docs/modules/` 下 M03、M08、M09、M10、
M13、M14、M16 PRD。

## 4 测试结果

续作时（2026-10-01）在当前工作树上重新执行；同机有 FX-SIM1、FX-WEB 等工作包并发跑测试，负载均值 6–33（8 核），
日志在会话临时目录 `fxsim2/`。

| 门禁 | 命令 | 结果 |
|---|---|---|
| 本区域用例 | `pytest -m "not perf"` `tests/sim/test_slow_tasks.py` `tests/sim/test_service_surface.py` `tests/safety/test_ext_wiring.py` `tests/jobs` `tests/world/test_arrays_adapter.py` `tests/mission/test_lease_resume.py` `tests/sensors/test_pose_chain_e2e.py` `tests/sim/test_traj_provider.py` `tests/agent/test_simcore_integration.py` `tests/environment/test_env_e2e.py` `tests/sim/test_fleet_rest_faults.py` | 49 通过（59.7 s） |
| 全量 pytest | `pytest -m "not perf"` | 2494 通过、14 失败、10 跳过、46 取消选择（perf），59 min 26 s。失败中 13 个是 `tests/e2e/test_scenarios_static.py` 的 ladder、soak 静态用例（`test_ladder_layout_table` 6 例、`test_ladder_constructed_separation` 6 例、`test_soak_composition_and_separation`），属并发进行中的 FX-SIM1 ladder 改造（ADR-062），与本区域无关；另 1 个 `tests/runtime/test_events.py::test_570_events_per_s_no_gap_no_reorder[zenoh]` 单独复跑 `tests/runtime/test_events.py` 12/12 通过，属负载敏感用例（M11） |
| S3（D1-AC-16） | `AWR_E2E_FULL=1 pytest tests/e2e/test_scenarios.py::test_s3` | 1 通过，181.6 s【墙钟】 |
| 剧本生成物 | `tests/e2e/test_scenarios_static.py::test_authoring_deterministic_and_committed`（在全量中） | 通过：`scenarios/s3-newyork-sar.json` 与 authoring 生成物逐字节一致 |
| 混沌（D1-AC-11b） | `pytest --collect-only -m chaos tests/chaos` | 7 例收集正常（含 `test_checkpoint_recovery`、`test_poison_checkpoint`）；均带 perf 标记，按本阶段规则不运行。功能部分由 `tests/safety/test_ext_wiring.py`（escalate、故障命令路由、checkpoint 扩展段的保存与恢复）在进程内覆盖并通过 |
| vitest | `npx vitest run --project unit --project browser` | 122 个文件通过、1 跳过；948 例通过、1 跳过 |
| Playwright skeleton（D1-AC-34） | `npx playwright test perf/skeleton.spec.ts --project perf` | 4/4 通过（第 2 次）。第 1 次在负载均值 27.7 时 `__perf.net.swarmHz > 5` 15 s 未达，失败于完整链路用例，非本区域逻辑 |
| Playwright 交互（D1-AC-32） | `npx playwright test --project=e2e interaction` | 1/1 通过（第 3 次）。前两次在负载均值 25–33 下失败于"添加 P600"：第 1 次 10 s 内 roster 未增加，第 2 次 3.3 s 出现（断言 ≤ 1.5 s）。为排除后端回归，用探针对真实 supervisor 后端（ci、free-shenzhen）连续添加 5 架：POST 66–168 ms、列表可见 160–376 ms（负载均值 22–24）；添加与 roster 查询在主循环 drain 中同步处理，不经过慢任务。两次失败均在 GoTo 的 accepted → running → succeeded 之后 |
| Playwright 重建（D1-AC-22） | `npx playwright test perf/m01/recon.spec.ts --project perf` | 2/2 通过（M01 切到 M03 `PhasedBuild` 之后的 Mock 产物世界进入目录、Web 端打开）；未在共享 `worlds/` 留下产物 |
| lint | `make lint` | 通过（退出码 0）：ruff 全部通过，oxlint `--type-aware` 0，no-emoji、no-hex、lint-lf、motion-lint、check-icons、no-raw-controls、check-brand、check-deps、check-units、check_py_imports（465 个文件）、check_py_callbacks、check-perf-flags、check-thresholds 等全部 ok。续作开始时 ruff 报 8 条，均在 FX-SIM1 正在修改的文件中，FX-SIM1 已自行修复 |

D1 验收状态（本区域）：

| 编号 | 状态 | 说明 |
|---|---|---|
| D1-AC-16（S3） | 通过（功能） | 见上表；倍速相关的墙钟与 RTF 属性能项 |
| D1-AC-11b（混沌 ext） | 功能部分通过，混沌实测待验收阶段 | checkpoint 扩展段、毒性 checkpoint、escalate、故障路由已接线；`make chaos` 带 perf 标记，需在排他性能锁下执行 |
| D1-AC-22（重建，提交入口） | 接线完成 | `svc/job/*`、事件与 REST R06、R39–R42、R64 已交付；FX-WEB2 已用真实 job-worker 跑通任务页；`recon.spec.ts` 2/2 |

## 5 遗留问题

1. **D1-AC-11b 混沌实测（验收阶段）**：`make chaos` 的 `test_checkpoint_recovery`（回滚 ≤ 1 s、RUNNING 后新 epoch 首帧 ≤ 1.5 s、
   扩展段已恢复、在途 takeoff 到达终态）、`test_poison_checkpoint`、`test_main_loop_hang`、`test_restart_breaker` 与
   `tests/chaos/test_agent_runtime.py` 带 perf 标记，本包只做收集检查，需在排他性能锁下执行。
2. **ADR-057 的性能复核（验收阶段）**：借贷与追帧预算在 n = 1 时与原式相同，但仍需按 ADR-033 用 fleet_ladder 复核
   M08-NFR-002/006 的单步 p99 与慢任务占用（`state/sim-core/perf` 的 `slow_ms_per_s`、`slow_debt_us`、`slow_defer_max`），
   以及 D1-AC-07、D1-AC-28。S3 的 ci profile 请求 ×10，多进程实跑实际 RTF 约 2.2–5（墙钟 181–188 s），属性能项。
3. **估价回复的可选字段**（M13-to-M08 第 3 条、M14-to-M08 第 4 条）：`conf_expected`、`env_target` 不在本包任务范围内，且
   `bus/command.schema.json` 的 `estimateReply` 为 `additionalProperties: false`，需先由 M00 登记契约，M08 再追加
   `register_estimate_hook(name, fn)`（M13 插件已在 install 时探测该函数，目前不存在，登记被跳过）。按 `capability_catalog.json`
   的 `physical.limits` 判 120 ENV_LIMIT、地面机体 SOC < 0.30 判 119 也未实现。现状：M14 用 §6.10.5 退化式与 `env/query` 补齐，
   S3 的 b3 已因能量不可行得到 119，判定不受影响。
4. **M13-to-M08 第 6、7 条**（StageCtx 追加 `world_seed`、`inputlog`；`fleet/add` 加载机型时校验传感器文件）：不在本包范围，属
   V0.2/P2，M13 目前有退路。
5. **负载敏感用例**：本轮在高负载下观察到 `tests/runtime/test_events.py::test_570_events_per_s_no_gap_no_reorder[zenoh]`（M11）、
   skeleton 的 `swarmHz > 5`（15 s 时限）与 interaction 的"添加后 ≤ 1.5 s 出现"各失败一次，单独或负载稍低时复跑通过；
   后端添加链路实测 ≤ 0.4 s。建议所有者（M11、M16）把这类时限断言放到性能阶段，或在功能用例中改为条件等待并放宽时限。
6. **ADR 编号冲突（其他工作包）**：前端点云代码（`apps/web/src/engine/pointcloud/{params.ts,PointCloudEngine.ts,core/Selector.ts}`，
   FX-WEB1）用"ADR-054"指 tau 限幅点径上限，而附录 E 的 ADR-054 是 FX-SIM1 的绕行返航；FX-WEB1 需另取新号（≥ 063）登记并改注释。
   另：核对时 §7.0 索引已有 ADR-060，附录 E 尚无其正文（FX-SIM1 进行中）。
7. **ladder、soak 静态用例**：全量 pytest 中 13 例失败，属 FX-SIM1 进行中的 ladder 改造（ADR-062），不在本区域。
8. **M14 import 边界**：按 M14-NFR-018 与 10 §3 未开白名单（M14 经 bus 取估价与检测事件）。若 M00 日后决定把
   `awr.sim.sensors.detector`、`thermal_mock` 迁到中立包，M13、M14 需配合调整。
