# FX-SIM1 验收与加固报告（sim-core 性能、S1 与 ladder 剧本、进程重启）

| 项 | 内容 |
|---|---|
| 工作包 | FX-SIM1（区域：sim-core 性能、S1 与 ladder 剧本、进程重启） |
| 日期 | 2026-09-29（第 1 轮，因额度中断，未完成验证与文档）；2026-10-01（续作，本报告） |
| 依据 | INT-1 §3（D1-AC-07、11a、15）、§7.1、§7.2、§7.3、§7.11；03 §8.4、ADR-026、ADR-033、ADR-052；12 §5.8、§7.2、§7.4；16 §12.6；17 §6.4、§9.7；18 运行协议；19 §4.2；M07-FR-017；M08-FR-005、FR-027、FR-057；M09-FR-011、FR-050–FR-053；M10-FR-004、FR-013、FR-062；M16 §6.4.2–§6.4.8、§7.3.3–§7.3.4、M16-NFR-008 |
| 约束执行 | 未安装依赖；未使用 git；未运行性能基准（fleet_ladder、flight60、`perf` 标记用例）。单步耗时只用进程内短时剖析确认（假墙钟全速推进，见各节"功能剖析"）；`make chaos-core` 按任务书要求运行（排他锁，机器空闲时） |
| 结论 | 任务 1–4 全部完成。S1（D1-AC-15）9 个谓词全部为真，多进程 ×10 墙钟 159.9 s（限额 210 s，INT-1 为 286 s）；sim-core kill -9 到 RUNNING 2.10–2.11 s（INT-1 为 3.17 s），`make chaos-core` 通过；`test_ladder_smoke` 通过（守卫事件 0，实测最小间距 16.7 m）。续作还发现并修复了大机群（ladder 剧本）下 cmd_watch 逐行判定的扩展性问题：n1000 的 pipeline 均值由 11.0 ms/tick 降到 3.6 ms/tick。另修正 `SimCore.stop()` 未解冻永久代造成的同进程内存累积（全量 pytest 的 11 例 RSS 失败随之消失）。全量 `pytest -m "not perf"` 2526 例全部通过，Vitest 948 例、Playwright 功能用例 5 例通过，`make lint` 通过。规格变化以 ADR-054、ADR-060、ADR-061、ADR-062 落地，同步修订 03、10、12、16、17、18、19 与 M07、M08、M09、M10、M11、M16 PRD |

## 0 续作接手时的状态

第 1 轮（2026-09-29）在中途被额度限制中断，留下的状态如下：

- **代码大部分已写**：
  - 绕行返航（M09、M08、M10 三处）；
  - S1 小机群固定开销优化与 env stage 摊销；
  - sim-core 启动路径（推迟 BLAS 探测、按需导入 `scipy.ndimage`、CSafeLoader）与 supervisor 退避首档 0.1 s；
  - ladder 布局与加载器校验 V-SC-14、V-SC-15。
- **未完成部分**：
  - 没有写任何 ADR，PRD 也未同步；
  - `make lint` 有 8 条 ruff 错误；
  - `tests/e2e/test_scenarios_static.py` 的 ladder 布局表、构造间距与 soak 圈数断言仍是旧值，共 14 例失败；
  - soak 中 ladder n200 与 S1 的水平距离为 47.99 m，低于断言的 50 m。
- **ADR 编号冲突**：代码注释里引用的 ADR-055（env 摊销）、ADR-056（退避首档）、ADR-057（ladder）已被 FX-GW、FX-WEB2、FX-SIM2 用于别的决策。本包改为：
  - ADR-054（空缺编号）：绕行返航；
  - ADR-060：S1 ×10 与大机群固定开销；
  - ADR-061：重启时限；
  - ADR-062：ladder 布局。

  代码、配置与用例中的引用逐处改号。附录 E 中已有的 055–059 与续作期间他人新增的 ADR-063 保持不变。

续作先核对上述状态，再逐项补齐。凡第 1 轮已写的代码，都经过本轮的用例与剖析复核后才保留。

## 1 修复内容

### 1.1 任务 1：S1 能量 RTL 规格冲突（D1-AC-15；ADR-054，方案①）

| 项 | 内容 | 文件 |
|---|---|---|
| 返航路线 | 满足两个条件时，M09 评估 18 个单绕行点候选：直飞所需越障爬升超过 10 m（`z_rtl − max(z_now, z_home + 30) > 10`），且离 home 的水平距离 ≥ 30 m。候选取 p→home 方向 1/4、1/2、3/4 处，向两侧偏移 ±1–3 个 max(30 m, L/4)。两段走廊上界一次批量求得（M04 `heightmap_top_along`，与直飞同一 tol）；t_rtl 用 12 §5.8.3 的同一公式，水平航程取两段之和。按 t_rtl 升序检查合法性：在 z_rtl 高度按 10 m 采样，不越 border、不入 nofly，border 余量 ≥ 告警余量。比直飞至少少 5 s 的第一个合法候选即为返航路线。每次 battery stage 至多评估 16 架 | `sim/safety/{battery,params,state}.py` |
| 分发 | `RtlPlan` 追加 `via_enu_m`（只追加字段）。M08 执行 RTL 时把绕行点写入 FleetState `rtl_via`；CRUISE 先飞向绕行点，参考点进入 3 m 接受半径后转向 home 上方。融合核与 numpy oracle 逐位一致 | `sim/core/{interfaces,command}.py`、`sim/fleet/{actions,state,setpoint,kernels_l1,params_px4}.py`、`sim/fleet/stages/ingest.py` |
| 一致性 | CLIMB 结束重算取两段走廊上界；`EnergyModel.rtl_route` 供 M10 能量预检按同一路线抽样返航段 | `sim/safety/battery.py`、`sim/mission/{energy,engine,runtime}.py` |
| 续作修正 | 增加"离 home ≥ 30 m"条件：n1000 剖析中，在出生点正上方爬升的机体每秒都触发无效的绕行评估，battery stage 每次调用约 4 ms | `sim/safety/{battery,params}.py` |

剧本参数不变（`scenarios/s1-shenzhen-facade.json` 未改），判据 `t_rem_usable < 1.3·t_rtl` 不变。功能剖析（进程内，每 1 s【仿真】采样）：

| 机体 | 走绕行路线的样本 | 全程最小 `t_rem/t_rtl`（运行期判据） | 按直飞口径的最小值 |
|---|---|---|---|
| p600-01 | 273/821 | 5.55 | 1.06（773 s，z 56.7 m，soc 0.41；直飞 z_rtl 389 m、t_rtl 444 s，绕行 z_rtl 57 m、t_rtl 61 s） |
| p600-02 | 163/832 | 2.67 | 1.59 |

`test_s1` 多进程（ci profile ×10）的 9 个谓词：facade_coverage 0.997，min_separation 15.55 m，guard 0，阵风 pos_err 0.67 m，
landed_all，soc_min 0.347，energy_rtl 0，home 误差 0.10 m，missions_done。`tests/safety/test_rtl_detour.py`（2 例）验证计划与实飞
一致；`tests/sim/test_kernel_parity.py::test_rtl_via_equivalence`（10 种子）验证融合核与 oracle 一致。

### 1.2 任务 2：S1 ×10 可达与 D1-AC-07 预算（ADR-060）

**S1（小机群固定开销）**：

- 第 1 轮做的优化在本轮逐项复核为等价改写，有对拍或等价用例覆盖：
  - pipeline 按 `tick % L` 预展开；
  - M09 每 tick 只建一次上下文；FastGuard 改为 numba 扫描核 `fast_guard_scan`；mission_guard 只同步变化的条件位；battery 用查表并批量查询逆风；
  - 小机群碰撞对直接枚举；`set_traj_enu` 融合写入；contact 同 tick 复用；tracker 小机群跳过筛选；网格双线性插值的标量快速路径。
- 规格变化只有一项：env stage 的 rho、env_flags 与 EnvSample32 行缓存的非风列按 10 Hz 全量求值；风与阵风仍为 50 Hz（M07-FR-017 修订）。
- 本轮新增两项等价改写：机型、传感器、剧本三个校验器共用一次 schema 扫描；覆盖戳记用广播视图代替 `meshgrid` 与 `np.c_`。
- 不降频：M09 各 stage 频率保持 ADR-026 的规定。

**大机群（续作新发现）**：S1 达标后用 ladder 剧本在 n200 与 n1000 下做短时剖析，发现 cmd_watch 对在途调用逐行判定。ladder 每架机都有一条
M10 驱动的 follow_path 调用，所以 n200 时 cmd_watch 每次调用 8.2 ms，n1000 时 28.6 ms（50 Hz）。逐行实现是第 1 轮为 S1 小机群写的，
未考虑大机群。另有两处随机数线性增长的开销：ingest 每 tick 逐条扫描 roster 生命周期；battery 做无效绕行评估（见 1.1）。处理如下：

| 项 | 内容 | 文件 |
|---|---|---|
| cmd_watch 向量路径 | 在途调用 ≥ 32 条时，完成判据（goto、follow_path、orbit、hover 与 `_hold` 计时）和停滞、截止、暂停顺延判据改为向量预筛。产生事件的行仍按行号升序交给逐行实现处理，判据与运算次序逐项相同；小机群保持逐行路径。调用表追加 `prov`、`batched` 两列，checkpoint 恢复时由 meta 重建 | `sim/core/{command,calls}.py`、`sim/runtime/ckpt.py` |
| 进度限流 | 每个调用的 `cmd.progress` 仍 ≤ 2 Hz【墙钟】；每次 cmd_watch 至多发 16 条，取最久未发者。此前 1000 个同时开始的调用会在同一 tick 发出上千条进度 | `sim/core/command.py` |
| 生命周期 | 没有可推进条目且 roster 未变时，roster.advance 直接返回；`S.lifecycle` 只在 roster 或生命周期变更后同步。checkpoint 恢复后调用 `roster.touch()` | `sim/core/roster.py`、`sim/runtime/{main,ckpt}.py` |
| 碰撞对 | 小机群 `triu_indices` 结果缓存（只读） | `sim/fleet/collide.py` |
| 永久代泄漏 | `start()` 的 `gc.freeze()` 会把当时存活的对象（含实例自身的引用环）移入永久代；`stop()` 未解冻，同一进程内每次启停约泄漏 3.6 MB，全量 pytest 进程 RSS 因此超过 3 GiB。现 `stop()` 调用 `gc.unfreeze()`：20 次启停后 RSS 稳定在 201 MB（此前线性增长）；M01 重建用例因 RSS 守卫（3072 MiB）在全量运行中失败的 11 例随之恢复 | `sim/runtime/main.py`；M08-FR-005 加注 |

功能剖析（进程内全插件、假墙钟全速推进；不是性能基准）：

| 场景 | 修改前 | 修改后 |
|---|---|---|
| S1 ci，进程内，机器空闲 | INT-1：0.8–1.3 ms/tick | 609.6 µs/tick（pipeline 574；212 505 tick 用 129.5 s） |
| S1 ci，多进程（与 `test_s1` 同一路径） | INT-1：286 s | 159.9 s（含启动 4.5 s；限额 210 s） |
| ladder n1000 稳态（45–55 s【仿真】），pipeline 均值 | 11.0 ms/tick | 3.6 ms/tick |
| 其中 cmd_watch / ingest / battery（ms/tick） | 5.73 / 1.12 / 0.17 | 0.45 / 0.17 / 0.07 |

S1 进程内各 stage（µs/tick）：

| stage | env | M10 mission | cmd_watch | fsm | l1 | sensors | guard | contact | mission_guard | battery |
|---|---|---|---|---|---|---|---|---|---|---|
| 耗时 | 113.5 | 62.7 | 59.2 | 56.3 | 46.8 | 45.5 | 28.3 | 25.7 | 23.6 | 22.3 |

S1 多进程运行中 `sim.stage.overbudget` 共 29 条，其中 6 条在启动的 0.4 s 内。guard 不再出现；mission_guard 为 3 条（INT-1 为持续出现）。
`tests/sim/test_cmd_watch_vec.py` 覆盖等价性：36 架，脚本含 takeoff、goto、有限圈与持续 orbit、follow_path、hover、pause/resume、
rtl、land，并人为构造停滞 203 与截止 202 各一条。逐行路径与向量路径的 `cmd.*` 事件序列（种类、cid、原因码、effect、进度）、调用表
判据计时与机群状态逐位一致；进度峰值为 16 条每 tick。

D1-AC-07 的判定（N = 1000 单步 p99 ≤ 3 ms、最大 ≤ 12 ms）按 ADR-033 留给验收阶段，在排他锁下以 fleet_ladder 执行。本包只确认
功能正确与相对改进。n1000 剩余的主要固定开销是 M10 tracker（约 0.84 ms/tick）、StateRing 发布 tap（约 0.52 ms/tick）与 env 全量 tick
（约 2 ms/次），列为验收阶段的观察项（§5）。

### 1.3 任务 3：sim-core 重启 ≤ 3.0 s（D1-AC-11a；ADR-061）

| 项 | 内容 | 文件 |
|---|---|---|
| 退避首档 | supervisor 退避 0.5/1/2/4/8 s 改为 0.1/1/2/4/8 s，窗口与熔断不变 | `configs/runtime.yaml`、`runtime/config.py`、`runtime/supervisor.py`（第 1 轮）、19 §4.2 等文档 |
| 启动路径 | 第 1 轮：numba 首次编译或读缓存时的 BLAS 探测不再导入 `scipy.linalg`；`scipy.ndimage` 按需导入（M04 heightmap、derive、zones）；YAML 改用 CSafeLoader。本轮：三个 jsonschema 校验器共用一次 contracts 扫描（新增 `awr/sim/schemas.py`） | `sim/runtime/main.py`、`world/geometry/{heightmap,derive,zones}.py`、`sim/fleet/profiles.py`、`sim/sensors/spec.py`、`sim/mission/scenario_loader.py`、`sim/schemas.py` |
| 未采纳 | INT-1 建议的"先 `init_child` 再延迟导入、插件导入与世界载入并行"：实测线程并行没有收益（GIL 下 2.69–2.75 s，串行 2.35–2.59 s；demo profile 还钉在单核），延迟导入不改变总量；把 numba 预热移到 ready 之后违反 M08-FR-005；zygote 进程模型留待 V0.2 评估 | — |
| chaos 探针 | `tests/chaos/rtprobe.py` 此前只按记录的 RESET 或关键帧标志判定 SNAPSHOT。按 17 §6.4 与 §9.7 第 3 条，无 checkpoint 的重启是 segment 变化，只在 BATCH 帧头置 SNAPSHOT（flags bit0），记录不带 RESET。探针改为按帧头 flags 判定。此前重启超过 3 s，该断言从未执行到；重启提速后它才暴露 | `tests/chaos/rtprobe.py`（M16） |

实测（机器空闲；3 次各自新起后端，均为窗口内第 1 次重启）：kill -9 到 RUNNING 为 2.10–2.11 s。时间线：kill → 0.1 s 退避 → `init_child`
1.04 s → sim-core ready 2.07 s → RUNNING 2.11 s。同机有并行任务时（负载 5–9）为 2.21–2.27 s，负载 11 时为 3.1 s。`make chaos-core`
通过（`test_kill_api`、`test_kill_sim_core`，2 passed）。

### 1.4 任务 4：ladder 最小间距（INT-1 §7.2；ADR-062）

| 项 | 内容 | 文件 |
|---|---|---|
| 布局 | 20 m 交错格网：同层格距 40 m，L1、L2、L3 相对 L0 偏移 (20, 0)、(0, 20)、(20, 20)，任意两机出生点 ≥ 20 m。平地上任意两机三维距离 ≥ 20 − 3 = 17 m，与起飞错时是否生效无关（v1 依赖 5 s 错时，实测错时失效时只有 9.66 m）。中心：n10–n100 为 (−375, 20)；n200 为 (−435, 20)（续作由 −430 调整，使 soak 中与 S1 的距离为 52.9 m ≥ 50 m）；n500 为 (−510, 20)；n1000 为 (−505, −45) | `datasets/scenarios/authoring.py`、`scenarios/{ladder,soak}-shenzhen.json`（`generate --check` 0 差异） |
| 环绕速度 | 3 m、2 m/s 改为 3 m、1.2 m/s：航向朝心所需偏航角速度由 38.2°/s 降到 22.9°/s，P600 上限为 30°/s。v1 航向误差累积后会触发 TILT_ERR_KILL，这是 INT-1 中 missions_done、landed_all 失败的原因之一。soak 圈数由 200 改为 120（时长仍约 31 min） | 同上；`sim/mission/generators/{orbit,common}.py`（生成时 110） |
| 加载器校验 | `vehicle_sets` 展开后执行两条规则，失败返回 121：V-SC-14 要求同时段编组机对"竖直段 + 绕飞圆"的几何下界 ≥ max(10 m, 谓词要求)；V-SC-15 要求航向朝心 orbit 的 v/R ≤ 0.9 × 偏航上限 | `sim/mission/{scenario_loader,queries,engine}.py`、`sim/planning/worker.py` |

实测：n10 以 ×5 多进程运行（`test_ladder_smoke` 同一变体），SUCCEEDED，4 个任务全部 DONE，全部落地，guard 0，FleetGuard 冲突 0，
`ladder.steady` 在 45 s 出现。进程内每 0.1 s 采样全部空中机对，最小三维间距 16.7 m（L1 机在出生点上方爬升，水平偏离 1.1 m，含风与
跟踪误差）。V-SC-14 下界（含地形）：n10–n200 为 17.0 m，n500 为 16.3 m，n1000 为 14.0 m，均 ≥ 14 m。flight60 视锥内平均可见比例：
n10–n500 为 0.53–0.55（v1 为 0.52–0.55），n1000 为 0.46（v1 为 0.50）。

### 1.5 lint 与其他

- ruff 8 条错误全部清除（未用导入、`itertools.pairwise`、docstring 中的并集符号、多余的 `int()`、Yoda 条件、导入排序）。
- 本轮新写注释中的 U+21D4 被 no-emoji 规则拦截，已改为文字。

## 2 规格变化与文档

| 变化 | 落点 |
|---|---|
| 能量 RTL 的绕行返航；`RtlPlan.via_enu_m`；`EnergyModel.rtl_route`；RTL 巡航绕行点 3 m 接受半径；S1 余量数据 | 03 附录 E **ADR-054**（§7.0 索引、D1-AC-15 行加注）；12 §5.8.3 第 1、3 条、§5.8.5 结论第 2 条、§7.2 能量 RTL 余量行；M09-FR-011、FR-052、FR-053、§6.8.3、§6.8.4、§6.8.6、电量块字段表；M08-FR-027、K10 行、参数表 `RTL_VIA_ACCEPT_M`、§7.1.5 协议；M10 §6.5.17；M16 §6.4.3 第 6 条 |
| env stage 慢变量 10 Hz 摊销；cmd_watch 大机群向量路径与等价约束；`cmd.progress` 每次 cmd_watch ≤ 16 条；battery 轮转配额；不降频 | 03 附录 E **ADR-060**；M07-FR-017 与 §7.1 装配示意；M08-FR-057、§7.4 `cmd.progress` 行；M09-FR-052；12 §4.6 调用状态机说明第 3 条 |
| supervisor 退避首档 0.1 s；启动路径；SNAPSHOT 判定口径（探针） | 03 附录 E **ADR-061**；19 §1 第 3 条、OPS-FR-014、§4.1 进程表、§4.2 BACKOFF 行与计数口径、§6.2 `runtime.yaml` 与参数表、OPS-AC-008；10 §4.7 状态表、§8.4；17 §10.7 时钟域登记表；18 §8.2 supervisor 行；M11-FR-012、§6.5 参数表、§6.6.4 状态表 |
| ladder 20 m 交错格网、1.2 m/s、各规模中心；soak 圈数；V-SC-14、V-SC-15 | 03 附录 E **ADR-062**；M16 摘要第 5 条与对照表、M16-FR-021、M16-NFR-006、SCN-E002、§6.4.8（布局表、各规模表、阶段间距表、v1 问题说明）、§7.3.3 示例、§7.3.4 soak；12 §7.4；16 §12.6 规则表、DATA-FR-045、DATA-AC-012；M10-FR-013、FR-062 |

ADR 编号：附录 E 已有 055–059（FX-GW、FX-WEB2、FX-SIM2），续作期间 FX-WEB1 新增了 063。本包使用空缺的 054 与 060–062，§7.0
索引同步登记。

## 3 改动文件

第 1 轮（2026-09-29，本轮复核后保留）：

- 后端（`python/awr/`）：
  - `sim/safety/{battery,params,kernels,service,mission_guard,fast_guard,state}.py`
  - `sim/fleet/{actions,kernels_l1,state,setpoint,params_px4,collide,pipeline,profiles}.py`、`sim/fleet/stages/{ingest,l1}.py`
  - `sim/core/{interfaces,command}.py`
  - `sim/mission/{energy,engine,runtime,tracker,scenario_loader,queries}.py`、`sim/mission/generators/{common,orbit}.py`
  - `sim/planning/worker.py`、`sim/sensors/spec.py`、`sim/runtime/main.py`
  - `world/geometry/{grids,heightmap,derive,zones}.py`、`environment/{stage,field}.py`
  - `runtime/{config,supervisor}.py`、`datasets/scenarios/authoring.py`
- 配置与数据：`configs/runtime.yaml`；`scenarios/{ladder,soak}-shenzhen.json`。
- 测试：`tests/safety/test_rtl_detour.py`（新）、`tests/safety/test_fast_guard.py`、`tests/sim/test_kernel_parity.py`、`tests/runtime/test_config.py`。

续作（2026-10-01）：

- 后端（`python/awr/`）：
  - `sim/core/{command,calls,roster}.py`、`sim/runtime/{main,ckpt}.py`、`sim/schemas.py`（新）
  - `sim/fleet/{profiles,collide,actions}.py`、`sim/sensors/spec.py`
  - `sim/mission/{scenario_loader,coverage,energy}.py`、`sim/mission/generators/{orbit,common}.py`
  - `sim/safety/{battery,params,kernels}.py`、`environment/{stage,field}.py`
  - `runtime/{config,supervisor}.py`、`datasets/scenarios/authoring.py`
- 配置与数据：`configs/runtime.yaml`（ADR 改号）；`scenarios/{ladder,soak}-shenzhen.json`（n200 中心，重新生成）。
- 测试：
  - 新增 `tests/sim/test_cmd_watch_vec.py`；
  - 修改 `tests/e2e/test_scenarios_static.py`（ladder 布局表、构造间距、soak 圈数）、`tests/sim/test_kernel_parity.py`（导入排序）、`tests/runtime/test_config.py`（ADR 改号）；
  - 修改 `tests/chaos/rtprobe.py`（SNAPSHOT 判定，属 M16）。
- 文档：
  - `docs/03-设计基线与决策记录.md`（ADR-054、060、061、062 与 §7.0 索引，D1-AC-15 行）；
  - `docs/10-系统架构说明书.md`、`docs/12-业务逻辑设计说明书.md`、`docs/16-World数据规范.md`、`docs/17-接口与实时协议规范.md`、`docs/18-性能与测试方案.md`、`docs/19-部署与运维说明书.md`；
  - `docs/modules/` 下的 M07、M08、M09、M10、M11、M16 PRD；
  - 本报告。

越权说明：`tests/chaos/rtprobe.py`（M16）属于任务 3 的"`make chaos-core` 功能通过"。它的判定口径与 17 §6.4、§9.7 不一致，并非 sim-core
本身的问题，因此在本包内修正，并在 ADR-061 中记录。

## 4 测试结果

| 范围 | 命令 | 结果 |
|---|---|---|
| 全量 pytest（非 perf） | `pytest -m "not perf"` | **2526 通过、10 跳过、0 失败**（44 min 45 s，`gc.unfreeze` 修正之后）。修正之前的一轮为 11 失败，全部是 `tests/reconstruction`：同一进程 RSS 超过 M01 重建任务的 3072 MiB 守卫；该目录单独运行 266 例全部通过 |
| 本区域 pytest | `pytest -m "not perf" tests/{sim,safety,mission,scenarios,runtime,environment,agent,sensors}`、`tests/e2e/test_{scenarios_static,s1_energy,scenarios_energy}.py` | 1223 通过、2 失败（续作中途的一轮）。失败的是 `tests/environment/test_env_e2e.py` 的 2 例（INT-1 §7.11 所列的负载敏感用例），单独运行、按目录运行、在 794 例的同序列中运行都通过；最终全量运行通过 |
| 剧本端到端 | `pytest tests/e2e/test_scenarios.py::test_s1 tests/e2e/test_scenarios.py::test_ladder_smoke` | 2 通过（286.8 s）。S1 多进程墙钟 159.9 s，9 个谓词全真；ladder n10 SUCCEEDED |
| 混沌 core | `make chaos-core`（排他锁，机器空闲） | 2 通过（`test_kill_api`、`test_kill_sim_core`） |
| 新增与相关用例 | `tests/sim/test_cmd_watch_vec.py`（2）、`tests/safety/test_rtl_detour.py`（2）、`tests/sim/test_kernel_parity.py`、`tests/e2e/test_scenarios_static.py`（109） | 全部通过 |
| 剧本生成物 | `python -m awr.datasets.scenarios generate --check`、`pin --check` | 21 个文件 0 差异 |
| Vitest | `npx vitest run --project unit --project browser` | 122 文件通过、1 跳过；948 例通过、1 跳过 |
| Playwright 功能用例 | `perf/skeleton.spec.ts --project perf`（D1-AC-34、35）；`tests/e2e/interaction.spec.ts --project e2e`（D1-AC-32）。私有测试构建 `VITE_AWR_TEST_SWITCHES=1`，经 `AWR_PERF_DIST`、`AWR_WEB_DIST` 指向，`AWR_PORT_OFFSET=23` | skeleton 4/4 通过；interaction 1/1 通过 |
| lint | `make lint` | 通过（ruff、oxlint 与全部 LINT_TARGETS） |

未运行：fleet_ladder、flight60、perf 标记用例与 `perf/` 性能用例（按本阶段规则留给验收阶段）。正文中的单步与墙钟数据都来自进程内功能剖析或功能用例本身的计时。

## 5 遗留问题

1. **D1-AC-07、D1-AC-28 的正式判定**（验收阶段，排他锁，fleet_ladder）：
   - n1000 剩余的主要固定开销是 M10 tracker（约 0.84 ms/tick）、StateRing 发布 tap（约 0.52 ms/tick）与 env 全量 tick（约 2 ms/次，每 5 个 env tick 一次）；
   - M07-NFR-001（env stage N = 1000 单次 p99 ≤ 1.6 ms）有风险；
   - 均为功能剖析值，机器有负载时偏高。
2. **S1 的实际倍速**：多进程约 ×5.3，没有达到名义 ×10。159.9 s 满足 M16-NFR-008 与 `test_s1` 的 210 s 限额，但 D1-AC-26 的"×10 实时运行 HOLD 占比"需要 ×10 真正可达；进程内每 tick 610 µs，×10 需要 ≤ 400 µs。剩余开销分散在 env、M10 tracker、cmd_watch 与 M09 的每次调用固定成本上。
3. **重启余量**：
   - 机器空闲时 2.10–2.11 s（余量约 0.9 s）；负载 11 时 3.1 s，所以 D1-AC-11a 必须在排他锁下判定；
   - M08-FR-005 的"exec 到 ready ≤ 2.0 s（p95）"实测约 1.97 s，接近上限；
   - 进一步缩短需要 zygote 进程模型（ADR-061 备选），涉及 M11 supervisor，留待 V0.2。
4. **ladder n1000**：
   - V-SC-14 含地形的下界为 14.0 m，恰好等于谓词 14 m，跟踪误差可能使实测略低于 14 m（仍远高于 FleetGuard 的 10 m）；
   - 占地增至 620 m × 620 m，flight60 视锥内可见比例由 0.50 降到 0.46；
   - 前端 ladder 性能用例（D1-AC-09a/b）的基线需要按新布局重新建立；
   - n1000 东缘（x ≈ −195）靠近 S1 主塔，这些机体的返航走廊上界被塔顶抬到约 200 m（M04 金字塔 tol 20 m 的保守口径），属于既有行为。
5. **`cmd.progress` 总量限流**：并发调用多于 16 条时，单个调用的实际进度频率低于 2 Hz（规格为上限，允许）。前端若按 2 Hz 期望进度，应改为按"最近一次进度"展示（M15）。
6. **请求**：
   - M16：`tests/chaos/rtprobe.py` 的 SNAPSHOT 判定已按帧头 flags 修正，请复核；`tests/e2e/test_scenarios_static.py` 的 ladder 断言已按 ADR-062 更新；
   - M07、M11：`test_env_e2e.py` 在长序列中偶发失败（INT-1 §7.11）。本包修正 `gc.freeze` 泄漏后，最终全量运行通过，建议继续观察；
   - M00：`cmd.progress` 等事件仍待在 17 §6.12 登记（M08 §14 X-10，既有遗留）。
