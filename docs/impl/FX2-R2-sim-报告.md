# FX2-R2-sim 仿真内核、安全、任务与环境：验收与加固报告（第 2 轮修复）

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R2-sim（修复区域 sim：`python/awr/sim`、`python/awr/environment`、`python/awr/swarm` 与 `scenarios`） |
| 日期 | 2026-09-30 至 2026-10-02 |
| 依据 | `docs/impl/D1-验收报告-第1轮.md`（§3、§4.1、§4.3、§4.4、§4.6、§4.7、§5）；AWR-03 §1.3、§8.4、ADR-017、ADR-019、ADR-021、ADR-033、ADR-049、ADR-057、ADR-059、ADR-060、ADR-061；AWR-10 §3.3；AWR-18 §7.4、§7.6、§8.8；AWR-19 §4.2；M07、M08、M09、M10、M11、M16 PRD |
| 分派项 | D1-AC-07（P0）、D1-AC-09b、D1-AC-11b、D1-AC-15（P0）、D1-AC-16、D1-AC-17、D1-AC-27（P0）、D1-AC-28、D1-AC-29 |
| 环境 | 8 核 Xeon E5-2603 v4（1.7 GHz、无睿频、AVX2）；`.venv`（Python 3.12、numpy 2.5、numba）。本阶段 gateway、web-engine、web-ui 修复包同时在本机运行，load 常在 4–10 |
| 约束执行 | 没有安装依赖，没有使用 git；修复阶段没有跑验收用的性能基准与 perf 标记用例。下文性能数字全部是进程内诊断测量（假墙钟全速推进、`time.thread_time_ns` 计主线程 CPU 时间，排除其他进程抢占），只作相对比较，不作判定；按任务要求在修复结束后做了一次持排他锁的短时自测（第 5.3 节，持锁 < 10 min）。颜色、图标、UI 组件不涉及；无 emoji 与禁用字形；`make lint` 通过 |
| 规格变更 | 新增 ADR-065（不改任何 D1 阈值）；修订 M08、M09、M10、M11、M16 PRD 与 AWR-10、AWR-18、AWR-19 的对应条目；修订 S2、S4、S5 剧本参数（M16，经 authoring 生成） |

## 1 结论摘要

| AC | 第 1 轮 | 根因（已核实） | 本轮处理 | 状态（自测口径） |
|---|---|---|---|---|
| D1-AC-07（P0） | N = 1000 起不来；N ≤ 200 单步 p99 2.7–7.8 ms、最大 11.8–15 ms | 布设逐架重复 51 ms 平坦格搜索；大机群固定开销集中在少数 tick 类型；checkpoint 主线程 13–24 ms 与后台不释放 GIL 的编码；负载脚本只发 4 条命令 | 布设只求一次；调度相位、融合核与逐位等价的向量化（第 2.2 节）；checkpoint 主线程开销与 GIL；GC 节拍；fleet_ladder 负载口径 | N = 1000 起得来；进程内 CPU 时间 p99 5.7 → 3.9–4.2 ms、最大 18.9 → 7.2–8.7 ms（load 4–10）；p99 ≤ 3 ms 需在无干扰负载下复测 |
| D1-AC-09b | ladder n1000 后端进不了稳态 | 导演同一 tick 逐架 `fleet/add`（O(N²)）；准入与任务下发集中在单个 tick；任务启动前能量预检一个 tick 做 1000 架（本轮自测发现） | 增删每 stage ≤ 50 架、`agent_slot` 增量维护、空槽下界；任务引擎计数节流；分片准入；预检分批与向量化 | 多进程 n1000 剧本不再被判挂死；标记时机问题见第 6 节第 10 条；前端帧节奏属 web 区域 |
| D1-AC-11b | 挂死检出 3.34 s；第二次恢复点晚于第一次；熔断用例竞态 | 1 s SIGTERM 宽限后才退出且期间报告 RUNNING；崩溃进程写出的更晚代未被跳过；用例在进程退出后 `os.kill` | 判定即 STOPPING、转储后 SIGKILL；`.stale` 标记；用例修正 | 第 5.3 节自测 |
| D1-AC-15（P0） | 冷缓存 S1 崩溃循环；wx-storm `landed_all` 假 | 运行期签名未预热（只读视图、float32、单机 C 连续视图） | 统一预热入口、签名覆盖用例、单机发布走 numpy | 签名覆盖用例：S1 160 s（含 RTL）运行期新增签名 0；wx-storm 见第 6 节（未解决） |
| D1-AC-16 | ×1 与 ×10 accepted 相差 8.48 s | 未定位（M14 锁步与 agent-runtime 属 other 区域） | — | 未处理，见第 6 节 |
| D1-AC-17 | S2 间距 5.78 m/guard 6/soc 0.117；S4 间距 5.66 m、覆盖无值；S5 soc 0.239 | 编组出生 6 m；编队成员交回点与调用目标点不一致（202 后续飞整圈）；解散返航同高交叉；多机割草航带内切分对飞；S4 覆盖任务被能量预检拒绝；S5 航速 | 第 2.6 节 | 进程内 ×10：S2 SUCCEEDED（soc 0.400，间距 10.0 m，guard 0）；S5 SUCCEEDED（soc 0.310）；S4 只剩间距 6.6 m（转场）；任务编辑用例属 web-ui |
| D1-AC-27（P0） | 后端进不了稳态；诊断运行单步最大 94 ms | 同 09b；全机 RTL 时每次迭代准入 8 架、生效 8 架、读回 64 条、重算 32 架爬升终点；gen2 9 ms | 分片 4 架、读回 16 条、重算 16 架、gen2 每 2 s | 进程内全机 RTL（1000 架）单步最大 15–19 → 10.7 ms（load 10）；前端指标属 web 区域 |
| D1-AC-28 | 工具未实现 | `--clients`、`--with-recorder`、`--with-flight60` 只解析不执行 | 实现并发口径 | 冒烟（N = 4）全链路通过；判定待验收 |
| D1-AC-29 | RSS 端点 sim-core +70%、api +91% | sim-core 未复现增长 | 进程内 30 min（仿真）soak-shenzhen 与 ladder n200 的 RSS 趋势 | sim-core 首尾 +3.8%、+4.4%（平台在第 5 min 后不再增长）；api 属 gateway；RSS 口径接线属 M16 |

## 2 根因与修复

### 2.1 启动与 numba 签名（D1-AC-07、D1-AC-15）

- **布设**：`_spawn_plan` 的出生基点每个 SimCore 只求一次（此前每架机 51 ms，N = 1000 约 51 s > 15 s 启动宽限）；N > 64 时排成 C 列网格（C ≡ 2 mod 4），四层交错起飞后同层水平距离 ≥ 2 × 间距。
- **预热**：新增 `awr.sim.runtime.warm`（`make numba-warm`，`make run` 前置；e2e 夹具 `tests/e2e/awrproc.py` 在启动 supervisor 前执行），按名称装载插件并以运行期签名调用全部核：M09 `fleet_scan`（只读视图、float32 记分板）、`geofence_scan`（只读）、M10 `track_step`（只读 ENU 视图）、M07 湍流采样与 DTM 双线性、M08 contact（只读网格四种组合）、tap、cmd_watch、机间碰撞核，以及 plan-pool 工作进程用的规划核（按名称装载，不违反 AWR-10 §3.3 的导入边界）。
- **单机发布**：`tap_fill` 在 n = 1 时结构化字段视图同时是 C 连续，numba 会另编一个签名；本轮签名覆盖用例发现后，单行改走 numpy 路径（两者逐位相同）。
- **用例**：`tests/sim/test_numba_signatures.py` 在子进程中以全部插件与真实世界运行 S1（160 s，140 s 全机 RTL），比较 `start()` 之后与结束时各调度器的签名集合，运行期不得新增；另有不依赖世界的快速用例。

### 2.2 稳态单步（D1-AC-07）

逐 tick 的 stage 构成（进程内，N = 1000，环绕负载）显示 p99 由少数 tick 类型决定：偶数 tick（l1、tap、contact）叠加 50 Hz 的 env，或叠加 10 Hz 的 M09 stage。处理：

| 项 | 改动 | 改动前 → 后（均值，CPU 时间） |
|---|---|---|
| 机间碰撞检查相位 | 按调用计数定相 → 按 tick 定相（tick ≡ 6 mod 10，落在 guard 相位）；此前 N = 1000 时恰与 env 同 tick，且计数不随 checkpoint 恢复 | env 类 tick 3.1 → 2.5 ms |
| env 只求风 tick | numba 融合路径（地面高、廓线、高度缩放、近地衰减、湍流盒或 Dryden、CALM 标志一次遍历），与 numpy 路径逐位相同（新用例 18 组对拍） | env stage 1.78 → 1.12 ms；风场查询 0.43 → 0.12–0.36 ms |
| guard（链路监视） | 全部链路正常时跳过边沿与 HOLD/RTL 候选的集合运算 | 1.17 → 0.53 ms |
| fleet_guard | 扫描核按格排序的连续副本与无开方上界预筛（访问次序不变，与 numpy oracle 对拍）；机对最小间距改为逐机最小值数组（`min_separation(slots)` 结果不变） | 每片 1.1 → 0.72 ms；第 4 片（定稿）3.3 → 1.2 ms |
| mission_guard | 无 Velocity 机体时不做集合差 | 1.86 → 1.44 ms |
| cmd_watch | 起飞完成判据并入 numba 预筛核（此前逐行求值） | 起飞期间 6.4 → 0.2 ms |
| M10 跟踪器 | 移到奇数 tick（上一轮已做） | — |
| 单步口径 | `step_us` 按"drain 开始到慢任务结束"计（AWR-18 §7.6；此前只计管线） | — |

整体（同一负载与脚本，load 4–10）：每次迭代 p99 5.7 → 3.9–4.2 ms，管线部分 p99 3.3 ms。剩余的 p99 由"偶数 tick + env 全量 tick"与"偶数 tick + battery 或 mission_guard"两类 tick 决定（各约 2.5–2.9 ms，负载下的 CPU 时间）。

### 2.3 单步最大值（D1-AC-07、D1-AC-27）

| 来源 | 改动 | 改动前 → 后 |
|---|---|---|
| checkpoint 主线程 | 数组不预先 `.copy()`（CheckpointStore 已拷贝进双缓冲）；调用表只存活动行高水位以下（容量 8192 行约 1.5 MB）；在途调用为"不变字段字典（准入时生成）+ 可变字段列表"；roster、租约（`LeaseManager.gen`）、幂等表（尾插头删的增量队列）缓存；机对最小间距存 8 KB 数组 | 13.5 ms（最大 24）→ 2.8 ms（最大 5.6） |
| checkpoint 后台编码 | 一次 5–10 ms 不释放 GIL 的 `msgpack.packb` → `pack_meta` 分批编码、批间让出 GIL（与 `packb` 逐字节相同）；sim-core `sys.setswitchinterval(0.001)` | 主循环不再被后台线程挡住 GIL |
| GC | gen2 + freeze 周期 5 s → 2 s（单次停顿与周期内新增存活对象成正比）；30 s 稳态解冻后全量回收只找到约 330 个不可达对象，冻结造成的泄漏可忽略 | 全机 RTL 期间 9.5 ms → 约 4 ms |
| 全机 RTL | 分片准入 8 → 4 架/迭代；cmd_watch 读回 64 → 16 条；FSM 重算爬升终点 32 → 16 架/tick | 单步最大 15–19 → 10.7 ms（load 10），准入 1000 架约 1 s |

### 2.4 大机群 setup 与任务下发（D1-AC-09b、D1-AC-27）

- 导演每次 stage 至多增删 50 架；`agent_slot` 增量维护；空槽查找带下界（`_free_lo`，checkpoint 恢复时复位）。
- 任务引擎（≥ 32 架的任务）每次 stage 至多下发 4 条新调用、处理 16 条终态，按轮转续做；本轮补上了"转场完成后直接起步作业项"这条此前绕过节流的路径。
- ≥ 32 架的任务启动前的能量预检按每次 stage 全部任务合计 8 架分批（算完前任务保持 IDLE）；M09 `EnergyModel.path_wh` 在无风调用时按段向量化、按段序累加（cumsum 与逐段 += 同一次序）。多进程自测中 ladder n1000 的 4 个任务曾在同一 tick 内各预检 250 架（约 2.5 s），sim-core 被判挂死并反复重启（第 5.3 节）；修复后进程内 n1000 剧本 160 s 内没有 > 500 ms 的 tick，单 tick 最大 11–18 ms（CPU 时间）。
- 确定性：所有节流都按计数，不读墙钟；分片准入以 `batch_chunk` 写入输入日志，重仿真按同一分片注入。

### 2.5 挂死与毒性 checkpoint（D1-AC-11b）

- supervisor（`python/awr/runtime/supervisor.py`，M11，跨区域）：判定挂死即把进程转为 STOPPING（reason hung），SIGUSR1 转储栈 0.1 s 后直接 SIGKILL（附 SIGCONT，被 SIGSTOP 的进程也能回收），不再给 1 s SIGTERM 宽限。检出时延 ≤ `stale_s` + 巡检周期（2.0 + 0.2 s），阈值不变。
- CheckpointStore（`python/awr/runtime/checkpoint.py`，M11，跨区域）：恢复后 5 s 内再崩时，恢复点标记 `.poison`，其后由崩溃进程写出的各代标记 `.stale`（`load_latest` 跳过、不计入"连续 3 代中毒"），改用恢复点的上一代。新用例 `test_poison_skips_generations_written_after_restore`。
- `tests/chaos/test_chaos_ext.py`：挂死用例以 `/api/sys/procs` 的状态或 pid 变化计检出、以新进程 `step_seq` 首次前进计恢复（50 ms 采样，不再在 RUNNING 后固定等 0.3 s）；熔断用例对 `os.kill` 的 `ProcessLookupError` 竞态做容错。

### 2.6 剧本（D1-AC-17）

进程内 ×10 运行（假墙钟、plan-pool 进程模式、真实世界数据）逐项定位：

| 剧本 | 现象 | 根因 | 修复 |
|---|---|---|---|
| S2、S4 | 起飞即 `min_separation_m` < 10（5.9 m） | 编组出生间距 6 m，同时爬升时相邻机水平 6 m（与 ADR-059 对 S3 的情形相同） | 编组出生间距 12 m（authoring `_grid_vehicles`） |
| S2 | soc_min 0.117、3 次 `POS_ERR_FAILSAFE`、guard 6 | 编队成员在锚点终点交回 HOLD，交回点 = 锚点终点 + 滤波航向旋转后的槽位偏置；环形锚点上滤波航向滞后（τψ = 2 s），交回点与调用目标点相差 1.2 m（> 0.5 m 完成门限），成员停在原地直到 202 截止（约 190 s），挂起后续飞整圈并在续飞时参考跳变 57 m | 跟踪器交回时以 `CommandEngine.retarget_goal` 把成员调用的目标点改为交回点（按 slot 查当前调用：长时调用的幂等条目已过期） |
| S2、S4 | 解散返航时最小间距 5.8–9.9 m | 各成员以同一高度直线回到出生点，航线交叉 | 分层返航：rank k 的 RTL `alt_m` = 当前高度 + 12k m（不超过围栏 max_z − 10 m） |
| S2 | 两次 `SAF.SEP.AVOIDING`（CPA 1.6 m） | 多机割草在航带内部切分，相邻两块共享切点；分配时恰有一块反向飞行，两机在同一航带上同时飞向切点 | 多机切分只在航带之间切（`exact_split=False`） |
| S4 | `area_coverage{m-loop}` 无值 | B 组 5 机切分后 b-05（含 Willis 上空的高航带）预检落地 SOC 0.153 < 0.20，任务被拒（ENERGY_INFEASIBLE），从未开始 | `side_overlap` 0.7 → 0.55（预检最低 0.297 → 实测 soc_min 0.27–0.32） |
| S4 | 覆盖组转场时最小间距 6.6–6.9 m | 5 机一字排开同时起飞转场，低层机体爬到转场高度后横飞，从相邻机仍在爬升的竖直柱上方 3–6 m 处经过（b-04 越过 b-03 出生点，t ≈ 120 s）；把转场分层加大到 12 m 无效（两机到达各自层的时刻只差约 4 s） | 未修复，见第 6 节（试过的剧本 `transit.layer_dz_m = 12` 已撤回） |
| S5 | soc_min 0.239 | `terrain_follow` 的航速取 `params.speed_mps`（缺省巡航 5 m/s），编写期离线模型（0.325）低估了地形跟随的爬降 | `params.speed_mps` = 6（0.239 → 0.310，SUCCEEDED） |

S2 的 `min_separation_m` 为 10.0005 m（满足 ≥ 10，但余量很小，最小值出现在编队解散的头 3 s：rank 0 已开始巡航而 rank 1 尚在爬升到分层高度），验收阶段的多进程运行可能低于 10 m，见第 6 节。

### 2.7 fleet_ladder 工具（D1-AC-07、D1-AC-28）

- 缺省按 M08-FR-078 运行 `ladder-shenzhen` 剧本（profile `n<N>`，`AWR_SIM_N=0`），订阅剧本标记，见到 `ladder.steady` 后开始测量。
- `--scenario none` 为骨架布设：`AWR_SCENARIO_LOAD=0`（此前 supervisor 缺省剧本 S1 把机群换成 2 架，负载只发 4 条命令）；负载以 2 Hz 代发 GCS 信标（只启动 sim-core 时没有网关，operator 租约的链路源判为丢失，机体 HOLD 后 RTL）；按层批量起飞（30/42/54/66 m，层间 12 m）、≥ 95% FLYING 后 3 m 半径环绕（此前层间 8 m 与 6 m 网格使相邻机 3D 距离约 10 m，8 m 半径的相邻环绕圆相交，测到的是冲突处理）。
- 并发口径：`--clients K [--with-flight60]` 同时启动 api 并复用 `tools/bench/ipc/bench_state.py` 的客户端编排，窗口结束取 `/api/sys/perf` 的 tick 数据年龄与 api CPU；`--with-recorder` 同时启动 recorder（`AWR_REC_AUTOSTART=1`）并计 CPU 与 `rec-*.mcap` 写入速率；`--with-checkpoint` 为口径声明（监管下的 sim-core 恒挂接 checkpoint）。
- 失败处理：sim-core 未就绪、机群未就绪或窗口内重启时该规模点记为失败，明细写 `fleet-ladder.json`，不写 NaN，退出码 1。
- `make bench-ladder-smoke` 改为 `--scenario none --n 4`；诊断参数 `--keep-runs DIR` 保留运行目录（崩溃转储与日志）。
- 冒烟（N = 4，骨架布设，两次）：`--clients 1` 时 api 0.058 核、tick 数据年龄 p50 4.0 / p99 11.1 ms；`--with-recorder`（自动开始录制）时 recorder 0.055 核、写入 2.34 MB/min；记录字段齐全。

## 3 规格变更

- **ADR-065**（AWR-03 附录 E 与 §7.0 索引）：sim-core 大机群实时性与恢复：分片准入与下发节流、调度相位、GC 与 GIL、checkpoint 主线程开销、挂死即杀与毒性代判定、fleet_ladder 口径。不改任何 D1 阈值。编号说明：ADR-064 已由 web-engine 使用；上一轮代码注释中引用的 "ADR-064"（sim 调度）已统一改为 ADR-065。
- PRD 与说明书：M08-FR-005、FR-057、FR-078、FR-083；M09-FR-052、FR-080；M10 §6.3.4（跟踪器相位与下发节流）、编队状态表 CRUISE → DISBANDED 行（分层返航、交回时修正目标点）；M11-FR-016、FR-017；M16 §6.4.4、§6.4.6、§6.4.7（S2/S4 出生间距 12 m、S4 `side_overlap 0.55` 与转场分层、S5 航速 6 m/s）；AWR-10 §8 故障表两行；AWR-18 §7.4（并发口径）；AWR-19 §4.2 挂死行。
- 剧本：`scenarios/s2-shanghai-formation.json`、`s4-chicago-lakeshore.json`、`s5-sanfrancisco-terrain.json` 由 `python -m awr.datasets.scenarios generate` 从 authoring 生成；`tests/e2e/test_scenarios_static.py` 与 `tests/scenarios` 通过。

## 4 改动文件

本区域（sim）：

- `python/awr/sim/runtime/main.py`、`warm.py`（新）、`ckpt.py`
- `python/awr/sim/core/command.py`、`calls.py`、`roster.py`、`authority.py`
- `python/awr/sim/fleet/state.py`、`fleet.py`、`path.py`、`collide.py`、`kernels_l1.py`、`kernels_tap.py`、`kernels_contact.py`、`kernels_watch.py`（新）、`stages/tap.py`、`stages/contact.py`
- `python/awr/sim/safety/kernels.py`、`service.py`、`flight_fsm.py`、`battery.py`、`fleet_guard.py`、`geofence.py`、`link.py`、`mission_guard.py`
- `python/awr/sim/mission/__init__.py`、`director.py`、`engine.py`、`runtime.py`、`tracker.py`、`generators/lawnmower.py`
- `python/awr/sim/planning/kernels_track.py`、`smooth.py`
- `python/awr/sim/sensors/runtime.py`、`gimbal.py`
- `python/awr/environment/field.py`、`stage.py`、`wind/turbulence.py`
- `scenarios/s2-shanghai-formation.json`、`s4-chicago-lakeshore.json`、`s5-sanfrancisco-terrain.json`
- 用例：`tests/sim/test_numba_signatures.py`（新）、`test_tap.py`、`test_contact.py`、`test_cmd_watch_vec.py`、`test_schedule_golden.py` 与 `tests/golden/m08_schedule/schedule_50_{numba,numpy}.json`；`tests/sensors/test_gimbal.py`；`tests/environment/test_wind_fused.py`（新）

跨区域（按分派结论属 sim 修复，改动最小化）：

- `python/awr/runtime/supervisor.py`、`python/awr/runtime/checkpoint.py`（M11；挂死即杀、`.stale`、`pack_meta`）
- `tests/runtime/test_checkpoint.py`、`tests/chaos/test_chaos_ext.py`、`tests/e2e/awrproc.py`
- `tools/bench/fleet_ladder/run.py`、`ladder_load.py`（M08 工具）；`mk/m08.mk`
- `python/awr/datasets/scenarios/authoring.py`（剧本的唯一来源）
- 文档：`docs/03-设计基线与决策记录.md`、`docs/10-系统架构说明书.md`、`docs/18-性能与测试方案.md`、`docs/19-部署与运维说明书.md`、`docs/modules/M08-…`、`M09-…`、`M10-…`、`M11-…`、`M16-…` PRD

## 5 测试

### 5.1 功能测试

- `pytest -m "not perf"`：`tests/sim tests/safety tests/environment tests/mission tests/sensors tests/runtime tests/planning tests/swarm tests/scenarios tests/rt tests/agent tests/contracts tests/e2e/test_scenarios_static.py` 共 1658 例，1656 通过；2 例失败为 `tests/environment/test_env_e2e.py`（进程内 uvicorn + InprocSim 的 WS 与 REST 用例，超时与 503），该次运行期间本包同时在跑两次 N = 1000 的多进程阶梯自测（机器满载）。单独运行 `tests/environment`（141 例）、`tests/safety + test_env_e2e`、`tests/sim + test_env_e2e` 均全部通过。此后的修改（能量预检分批、`path_wh` 向量化）回归：`tests/mission` 74 例、`tests/safety tests/scenarios test_scenarios_static` 265 例全部通过。
- 新增与修改的用例：`tests/sim/test_numba_signatures.py`（签名覆盖；快速用例导入 M09 核后撤销装配，避免全局登记表污染同进程的 M08 用例）、`tests/environment/test_wind_fused.py`（18 组逐位对拍）、`tests/runtime/test_checkpoint.py::test_poison_skips_generations_written_after_restore`、`tests/sim/test_cmd_watch_vec.py`（numba 与 numpy 两条路径，含起飞）、`tests/sim/test_tap.py`、`tests/sim/test_contact.py`、`tests/sensors/test_gimbal.py`、`tests/chaos/test_chaos_ext.py`。
- `make lint` 通过（ruff、导入边界、no-emoji、no-hex 等全部规则）。

### 5.2 剧本（进程内 ×10，诊断）

进程内运行 `SimCore`（全部插件、真实世界数据、plan-pool 进程模式、假墙钟全速推进），剧本导演按成功谓词判定：

| 剧本 | 第 1 轮（多进程） | 修复后（进程内） | 结果 |
|---|---|---|---|
| S5 旧金山 | soc_min 0.239 | 航速 6 m/s：soc_min 0.310，agl_min 79.7 m，覆盖 1.0，guard 0，908 s 结束 | SUCCEEDED |
| S2 上海 | 间距 5.78 m、guard 6、soc_min 0.117 | 间距 10.0005 m、guard 0、soc_min 0.400、覆盖 1.0、编队误差 0.12 m，790 s 结束 | SUCCEEDED（间距余量小） |
| S4 芝加哥 | 间距 5.66 m、覆盖无值 | 覆盖 1.0、guard 0、soc_min 0.27、两编队误差 0.08–0.09 m；间距 6.6 m | FAILED（只剩间距一项） |
| S1 wx-storm | `landed_all` 假 | 复现：两机先后 `POS_ERR_ELAND`（3.2–3.5 m，σ_u 5 m/s 湍流），p600-02 降落后翻倒、p600-01 降落途中 `TILT_KILL` | 未修复（第 6 节） |

### 5.3 自测（排他锁，机器空闲，load 0.6–0.8）

- `tests/chaos/test_chaos_ext.py::test_main_loop_hang`（3 次）：检出 2.1–2.2 s（第 1 轮 3.34 s，阈值 2.5 s，满足）；恢复 4.49、5.48、5.54 s（阈值 4 s，不满足）。时间线（`hang/hangtl.py`）：SIGSTOP 后 2.14 s 判定并转 STOPPING，2.22 s 进程退出、BACKOFF，2.40 s 新进程 STARTING，4.43 s RUNNING，4.54 s 新进程出帧。sim-core 冷启动约 2.0 s（导入 0.83 s，其中 numba 0.39 s；插件装配与 numba 缓存加载 0.90 s；SimCore 构造与 start 0.72 s），与 ADR-061 实测的 kill -9 到 RUNNING 2.1 s 同量级。
- `test_restart_breaker`：第 1 轮的 `ProcessLookupError` 之外还有两处用例竞态：kill 之后 supervisor 处理退出之前列表仍报告旧 pid 为 RUNNING，循环重复"杀"同一进程，实际 kill 次数不足 6 次；第 5 次重启的退避为 8 s，原来每轮只等 10 s。修正后熔断可稳定观察到 FAILED；ci profile 下 supervisor 随即以 20 退出（19 §16.2），`reset_breaker` 不可达时按既有约定 skip。另发现：签发 admin 令牌要经 sim-core 申领席位，sim-core FAILED 时 `/api/auth/token` 回 503，用例改为熔断之前取令牌（gateway 区域问题，见第 6 节）。
- `test_poison_checkpoint`：通过（第二次恢复点不晚于第一次）。
- `fleet_ladder --n 1000 --runs 1 --dur 30`（缺省剧本口径，排他锁，load 1.0）：
  - 第 1 次：剧本标记出现后 sim-core 反复被判挂死（13 次重启，测量窗口内重启，按工具新口径记为失败、退出码 1）。崩溃转储（`--keep-runs` 新增的诊断参数保留）主线程栈全部停在 M10 `_try_start → energy_precheck → path_wh`：ladder n1000 的 4 个任务在同一 stage 内各预检 250 架，逐样本 Python 循环，单个 tick 约 2.5 s。这是此前进程内运行（没有活性看门狗）没有暴露的问题，已按 ADR-065 第 3a 条修复（预检分批 8 架/stage、`path_wh` 向量化）。
  - 第 2 次（修复后）：无崩溃、无重启；窗口内 RTF 0.69、sim-core CPU 0.69 核、单步 p50 8.2 ms、p99 38 ms、最大 41.7 ms、追帧饱和 419，跟踪器 `mission` 443 ms/仿真 s。原因是测量窗口（`ladder.steady` = 仿真 45 s 起）里机群还在起飞和转场：进程内同一剧本逐 10 s 统计，45 s 时 1000 架全部处于 TRANSIT，160 s 时 WORKING（环绕）686 架、TRANSIT 314 架，转场规划经 plan-pool 的吞吐约 7–10 架/s；这一阶段进程内单 tick CPU 时间均值 2.7–2.9 ms、p99 5–10 ms、最大 11–18 ms。D1-AC-07（剧本口径）、09b、27 的测量窗口都以该标记为起点，标记时机需要按机群规模调整（第 6 节）。

## 6 未解决与建议

1. **D1-AC-07 单步 p99 ≤ 3 ms（P0）**：进程内 CPU 时间在负载 4–10 下为 p99 3.9–4.2 ms、管线部分 3.3 ms；剩余 p99 由"偶数 tick（l1 0.69、tap 0.29、contact 0.22 ms）+ env 全量 tick（10 Hz，约 2 ms）"与"偶数 tick + battery（1.6 ms）或 mission_guard（1.4 ms）"两类 tick 决定，本机为 1.7 GHz Broadwell。无干扰负载下的数值需验收阶段在排他锁下测量。若仍超出，按收益排序：① battery 的 RTL 轮转刷新（走廊上界查询）拆成 50 Hz 小批；② env 全量 tick 的光学、热力字段同样融合；③ 把 10 Hz 的 M09 stage 迁到奇数 tick 专用相位（每个 10 Hz stage 有一半落在偶数 tick，是 25 tick 周期的奇偶性决定的）。
2. **D1-AC-11b 恢复 ≤ 4 s（P1）**：检出已满足；恢复 4.5–5.5 s，sim-core 冷启动约 2.0 s 是主体。可选：ADR-061 备选的 zygote（预导入并预热后 fork）；或把 sim-core 的 `stale_s` 降到 1.5 s（可省约 0.5 s，但主循环内任何 > 1.5 s 的停顿都会被判挂死，例如运行期加载大剧本，本包没有采用）。
3. **D1-AC-15 wx-storm `landed_all`（ext）**：14 m/s 平均风、σ_u 5 m/s 湍流下两机先后 `POS_ERR_ELAND`，p600-02 降落后翻倒、p600-01 降落途中 `TILT_KILL`。需要产品层面决定：M09 在风超过机型额定（`WIND_LIMIT`）时主动返航，或修订该 profile 的安全终止谓词（例如允许"安全动作后坠毁"计为终止），按 03 §1.3 以 ADR 记录。
4. **D1-AC-16（P1）**：未处理。accepted 时刻差 8.48 s 来自 agent-runtime（M14，other 区域）的锁步；sim 侧的 plan-pool 结果按到达时的 tick 生效（墙钟相关），×1 与 ×10 下转场与搜索任务的开始时刻会不同，建议 M14 与 M10 一起以 S3 的事件时间线逐项比对。
5. **D1-AC-17**：S4 只剩 `min_separation_m`（6.6 m，覆盖组同时转场越过相邻机的爬升柱）。建议 M10 对同一任务的入场转场做 4D 去冲突（与作业项的 `_deconflict` 同一机制，或按 rank 错开起飞），或把 B 组出生点排成与各自分区方向一致的布局；S2 间距 10.0005 m 余量很小（解散的头 3 s：rank 0 已开始巡航、rank 1 仍在爬升），多进程运行可能低于 10 m，可让 rank 0 也先垂直错开再巡航。任务编辑用例（`mission-edit.spec.ts`）属 web-ui。
6. **D1-AC-28**：工具已实现（第 2.7 节），冒烟通过；N = 1000 并发判定待验收。
7. **D1-AC-29**：sim-core 进程内 30 min（仿真）RSS 首尾 +3.8%（soak-shenzhen）、+4.4%（ladder n200），在第 5 min 后不再增长；第 1 轮的 +70% 为端点值（起点可能早于世界与插件就绪），按 18 §7.5 的首尾 5 min 中位数口径需 M16 把 RSS 接入 `server.json` 后复测；api 的 +91% 属 gateway。
8. **gateway（转交）**：签发 operator/admin 令牌要经 sim-core 申领席位，sim-core 熔断（FAILED）时 `/api/auth/token` 回 503，管理员若此前未登录则无法经 REST 执行 `sys/restart{reset_breaker}`。
9. **D1-AC-27 前端项**（我方 LoAF、Toast、渲染行数）属 web 区域；sim 侧单步最大值的判定待验收。
10. **ladder 稳态标记（影响 D1-AC-07 剧本口径、09b、27）**：`ladder-shenzhen` 的 `ladder.steady` 固定在仿真 45 s，n1000 时机群要到 200 s 以后才全部进入环绕（起飞、转场规划的 plan-pool 吞吐与下发节流）。建议 M16 按 profile 设定标记（例如 n500、n1000 用"全部航迹 WORKING"或 ≥ 240 s），同时把性能驱动的 `waitMark` 超时（`apps/web/perf/harness/backend.mjs`，现为 150 s，web-engine 区域）与 `fleet_ladder --fleet-timeout` 放宽；这是规格变更，需要 ADR，本包没有单方面修改剧本与驱动。在此之前，D1-AC-07 可按 18 §7.4 之外的骨架布设口径（`--scenario none`，起飞完成并环绕后测量）对照。

