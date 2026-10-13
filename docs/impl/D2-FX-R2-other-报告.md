# D2-FX-R2-other 报告（D2 验收第 1 轮修复，区域 other）

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-other：区域 other 的修复。第 2 次分派（2026-10-12）：D2-AC-39 S7 浸泡（P0，未测）；第 1 次分派（2026-10-07）：D2-AC-26（P0，见第 1 部分） |
| 区域 | other：按工作包授权跨模块修改了 M20（`python/awr/sim/tasking/{dayroll.py（新）, relay.py}`、`tests/tasking/test_dayroll.py`（新））、M16（`tools/bench/sandbox/{soak.py, _sbbench.py}`、`tests/e2e/test_sandbox_bench_tools.py`）、M00（`packages/contracts/bus/event.schema.json` 与生成物 `python/awr/contracts/schema_types.py`、`packages/contracts/gen/ts/types.ts`）、M11（`python/awr/runtime/supervisor.py`、`tests/runtime/test_show_calendar.py`（新）） |
| 日期 | 2026-10-12 |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（D2-AC-39 行、§2.3 第 5 条、§4.7 第 3 条、§5、§8 第 8、9 条）；`d2-ACC-1-to-M16.md` 第 8 条、`d2-ACC-1-to-文档总编.md` 第 3、10 条；AWR-04 v1.3 §4.10 第 2 条、ADR-094、§13 D2-AC-39、§14.1 D2-MS5（"24 h 浸泡与其他用例并行"）；M20 PRD M20-FR-074、M20-AC-031、§7.1–7.2；17 §17.7.5、§17.8；ADR-033（PR-1、PR-2） |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4）、无 GPU；`.venv` Python 3.12.3；同机并行的其他修复工作包使负载持续在 4–15 |
| 约束执行 | 没有安装依赖；没有执行 git 写操作；没有跑性能基准（没有取 `runs/.perf.lock`；浸泡工具只以 `--smoke` 运行；内存归因是诊断级，写在 scratchpad）；没有修改任何阈值，没有冻结"暂定"阈值，没有新 ADR；没有放宽 P0 硬口径（≥ 24 h 不缩短、不外推）；没有 emoji 与禁用字形；没有前端与渲染代码改动（只有生成的 TS 类型多一个事件名字面量）；文档措辞修订写成请求 |

## 第 2 部分（2026-10-12）：D2-AC-39 S7 浸泡

### 2.1 结论摘要

D2-AC-39 第 1 轮登记为"未测"（只跑了 6 h，软重置无法由工具确认）。逐项定位后，"未测"背后有四个根因，其中前两个即使跑满 24 h 也会使
本条无法判定：

1. **软重置在第 1 轮的窗口内根本没有发生**（M20）：日切以 `(t_cal − 4 h) // 86400` 计日序号，日界落在 UTC 04:00（北京 12:00），不是
   AWR-04 §4.10 的北京 04:00；只在接力任务 RUNNING 时逐任务求值；归档的是累计值而不是当日值；"清理交接记录与间隙明细"没有实现；也没有任何
   可观测的输出。第 1 轮浸泡窗口是 UTC 17:31–23:31，跨过北京 04:00（UTC 20:00）而不含 UTC 04:00。
2. **浸泡工具取日历的来源不存在**（M16）：工具从 show-api `/api/events` 中找 `env.keyframe`，而该事件按设计不进事件环
   （`awr.api.rt.events.NOT_IN_RING`），日历序列恒为空，`soft_reset_0400` 恒为 0（第 1 轮 `events` 计数中没有 `env.keyframe`）。
3. **工具判据与 D2-AC-39 原文不一致**（M16）：把 1 Hz 瞬时共享展示 RTF ≥ 0.99 当作判据（第 1 轮报告因此 `ok: false`），却没有运行时长判据
   （缩短运行显示为"缩短窗口内判据全部满足"）。
4. **24 h 无法排进验收**（M16 工具与流程）：工具在整个运行期间独占 ADR-033 的性能排他锁，与 D2-MS5"24 h 浸泡与其他用例并行"矛盾；验收只能在
   24 h 独占与其余性能阶段之间取舍，第 1 轮因此缩短为 6 h。

修复：M20 新增会话级日切与软重置（当地 04:00、当日值日表、清理交接记录与已结束的接力工作项、`relay.day_rollover` 事件，契约已登记）；浸泡工具
改为读 `GET /api/env/state` 的日历并与日切事件逐次对账、校验日表、增加 `duration_h` 判据、RTF 改为观测（含 10 min 窗口）、每小时写中间报告、
信号安全退出、精简内存采样，并提供 `--lock-scope start`（只在起跑与栈启动期间持锁，符合 D2-MS5）与 `--calendar-start-local`（几分钟内跨过当地
04:00 的功能自检，supervisor 新增可选的 `AWR_SHOW_CALENDAR_START`）。

验证：单元测试 8 + 2 + 3 例；S7 批处理从北京 03:58 起 240 s 恰有一条日切事件（日界 UTC 20:00:00）；真实栈（S7、show-api、2 个 viewer）
自检 `soak.py --smoke --calendar-start-local 03:57`：`soft_reset_0400` 通过（跨越 1、事件 1）、`day_table` 通过、`discovered` 0、重启 0。
功能测试与 `make lint` 通过。

**D2-AC-39 本身仍需第 2 轮按原口径运行 ≥ 24 h 才能判定**（P0 硬口径，不缩短、不外推；本包处于并行阶段，不跑性能基准）。工具已能对每一项判据
给出确定的通过或不通过；第 1 轮缩短窗口内的实测值（RSS 斜率 1.246 / 0.332 MB/h、重启 0、被发现 0、OOM 0）按 24 h 判据都有余量，内存增长的
归因见 2.4.3。

### 2.2 根因定位

| 根因 | 证据 | 影响 |
|---|---|---|
| ① 日界按 UTC 计算 | `relay.py` 原 `_day_rollover`：`day = int((t_cal_s − 4·3600) // 86400)`，`t_cal` 为 UNIX 时刻（UTC），日序号在 UTC 04:00 变化；S7 日历 `tz = Asia/Shanghai`，supervisor 把 `start_utc` 设为首次启动时刻（日历与北京时间同步） | 北京 12:00 才日切；第 1 轮 6 h 窗口（UTC 17:31–23:31）不含 UTC 04:00，软重置未发生 |
| ① 日切依附于运行中的接力 | 原实现在 `RelayController.step()` 的任务循环内、`T.state == RUNNING` 之后调用；接力未开始、休眠以外的非 RUNNING 状态不日切 | 软重置是否发生取决于接力状态，不是会话级 |
| ① 归档内容与清理缺失 | 原日表行写累计 `t_monitor`、累计覆盖率、累计间隙；不清空任何结构；`overlaps` 列表与接力工作项只增不减：每次接替 `_new_item` 新建一个站位工作项（`T.items`），`apply_plan` 追加分配记录（`T.assignments`），二者从不删除；`task/{tid}/status` 每秒发布全部 `items[]`，`_sync_roles` 等每秒遍历全部工作项 | M20-FR-074"当日统计归档、清理交接记录"未实现；第 1 轮 6 h 有 48 次 `relay.dispatch`，按此每日约 190 个工作项累积 |
| ① 无可观测输出 | 日切只改内存中的 `day_table`（只在批处理 `relay_stats.json` 的扩展字段中导出），不发事件、不写日志 | 任何工具都无法从实时栈确认软重置 |
| ② 工具的日历来源 | `soak.py` 原 `EventTail` 只从 `/api/events` 取 `env.keyframe`；`awr/api/rt/events.py` `NOT_IN_RING = {"cmd.progress", "env.keyframe", "path.changed"}`；第 1 轮报告 `events` 计数没有 `env.keyframe`，`soft_reset_0400.value = 0` | 跨过北京 04:00 也记为 0 |
| ③ 判据不一致 | 原 `checks` 含 `show_rtf_min ≥ 0.99`（1 Hz 瞬时最小 0.945，10 min 窗口 ≥ 0.9986），无时长判据；D2-AC-39 原文只有 RSS 斜率、非预期重启、被发现与"≥ 24 h（含 1 次软重置）" | 报告 FAIL 的原因不在 AC 内；缩短运行看起来全部通过 |
| ④ 锁范围 | `soak.py` 原在 `main()` 开头 `acquire_perf_lock` 并持有到进程结束；D2-MS5 写明"24 h 浸泡与其他用例并行"；第 1 轮验收 §2.3 第 5 条：缩短为 6 h | 24 h 浸泡与全部性能阶段互斥 |

### 2.3 修复与实现清单

| 编号 | 实现 | 路径 |
|---|---|---|
| M20-FR-074、M20-AC-031、D2-AC-39；AWR-04 §4.10、ADR-094 | 会话级日切 `DayRoll`：每 1 s【仿真】由 `RelayController.step()` 调用；按会话日历（`ctx.env.sky.calendar`）计算下一个当地 04:00（`boundary_after`，按当地日期加一日再取墙上时刻，夏令时切换日同样只有一个日界），平时只比较一个整数；首次求值、日历被 `env/calendar` 改写（cal0 或 tz 变化）、`sim/reset` 与 checkpoint 恢复（`RelayController.reset()`）时只重定日界、不归档；没有日历时不日切；日切失败记日志、不影响接力 | `python/awr/sim/tasking/dayroll.py`（新）、`relay.py` |
| 同上 | 当日日表行（与上一次日切的累计值之差）：`day`（被归档的当地日期）、`t_cal_end_unix_ns`、`monitor_s`、`coverage`、按原因的 `gaps`、`gap_max_s`（当日）、`discovered`、`handovers`、`overlap_min_s`、`overlap_mean_s`（当日）、`config_entries`、`sorties`、`flight_h`、`energy_wh`、`energy_rtl`；当日没有任何活动的接力不写行；保留 7 天 | `dayroll.py` |
| 同上（清理累积结构） | 清空当日交接重叠明细；删除接力任务中已结束（DONE、CANCELED）且不被任何架次 `(tid, wid)` 引用的工作项及其分配记录，`T.revision + 1`（客户端重取计划几何）；在岗、接替、待分配与返航途中的架次仍引用的工作项保留；任务、站位、机巢与识别物状态不变；被发现累计不清零 | `dayroll.py` |
| 同上（累计统计不变） | `RelayRT` 新增 `day_base`、`gap_max_day`、`ov_n`、`ov_sum`、`ov_min`（非工厂缺省值，与 checkpoint 的 `vars()` 编码兼容）；`_close_gap` 更新当日最长间隙；`_promote` 更新重叠计数、和与最小值；`stats_wire` 的 `handovers.mean_overlap_s`、`min_overlap_s` 由计数给出（与此前对全部明细求均值与最小值相同）；删除 `_day_rollover` 与 `last_day` | `relay.py` |
| 同上（可观测） | 事件 `relay.day_rollover`（level 0）：`day`、`tz`、`t_cal_unix_ns`（日界，十进制字符串）、`n`、`rows[]`（含 `tid`、`target`）、`pruned_items`、`pruned_assignments`；会话有任务或机巢时发出；同时写 INFO 日志 | `dayroll.py` |
| 17 §17.7.5（新增事件） | `known_kinds` 追加 `relay.day_rollover`；重新生成 Python 与 TS 类型（其余生成器 `--check` 均为最新） | `packages/contracts/bus/event.schema.json`、`python/awr/contracts/schema_types.py`、`packages/contracts/gen/ts/types.ts` |
| D2-AC-39（工具：软重置） | 每分钟读 `GET /api/env/state` 的 `calendar.t_cal_unix_ns` 与 `tz`（`calendar.jsonl`），计算当地 04:00 跨越次数；收集 `relay.day_rollover`；`soft_reset_0400`：跨越 ≥ 1 且事件数 = 跨越数（24 h 运行没有跨越即不通过：日历未推进）；`day_table`：事件 `day` = 日界前一日的当地日期、各行 `monitor_s` ≤ 1 日、`coverage` ∈ [0, 1]；`discovered` 同时核对日表行之和 | `tools/bench/sandbox/soak.py` |
| D2-AC-39（工具：判据） | 新增 `duration_h`（实际运行 ≥ 24 h，`--smoke` 不判定）；共享展示 RTF 移到 `info`（不判定），给出 1 Hz 瞬时最小与 10 min 窗口最小（StateRing `t_sim_ns` 增量 / 墙钟，跨 epoch 的窗口跳过）与单步 p99 最大 | `soak.py` |
| D2-AC-39（工具：24 h 稳健性） | 每 `--report-every-min`（缺省 60）写 `report.partial.json`；SIGTERM/SIGINT 时提前结束并按实际时长写最终报告（`interrupted: true`）；采样器 `slim=True`（内存中每个采样约 9.3 KB → 约 3 KB，完整采样照常写 JSONL） | `soak.py`、`_sbbench.py`（`slim_sample`、`Sampler(slim=)`） |
| D2-MS5、ADR-033 | `--lock-scope start`：只在起跑条件（PR-1 锁、PR-2 负载 ≤ 4）与栈启动期间持有排他锁，就绪后释放；缺省 `run` 与旧行为相同；报告记 `lock.scope` 与释放时刻 | `soak.py` |
| 功能自检 | `--calendar-start-local HH:MM`（只能与 `--smoke` 同用）：经 `AWR_SHOW_CALENDAR_START` 让共享展示日历从当地今天的 HH:MM 开始；supervisor 首次写 `session.json` 时，该变量格式正确则代替启动时刻，否则忽略并记 WARN，未设时行为不变 | `soak.py`、`python/awr/runtime/supervisor.py` |
| 测试 | 日切：当地 04:00 日界与夏令时、每日一次且 UTC 04:00 不日切、日历改写只重定、当日行与累计统计不变、无活动不写行、工作项与分配记录清理、无日历不日切；needs_data + slow：S7 批处理从北京 03:58 起 240 s 恰有一条日切事件 | `tests/tasking/test_dayroll.py`（新，8 例） |
| 测试 | 软重置与日表对账（通过、缺事件、日期与覆盖率错误、短运行不判定、24 h 无跨越不通过）；10 min 窗口 RTF 与精简采样；`soak` 冒烟的判据集合 | `tests/e2e/test_sandbox_bench_tools.py`（+2 例，冒烟断言更新） |
| 测试 | 共享展示日历：缺省为启动时刻、覆盖生效且副本只写一次、非法值忽略 | `tests/runtime/test_show_calendar.py`（新，3 例） |

没有修改的部分：D2-AC-39 的阈值与口径（≥ 24 h、RSS 斜率 ≤ 2 MB/h、重启 0、被发现 0）；M18 的被发现计数（17 §17.8 已写明软重置不清零）；
日志轮转（supervisor `LogSink` 已按 `log.max_mb × backups` 轮转）；前端（`d2FilterOf` 已按 `relay.` 前缀归入"编排"，`describeD2` 对新事件返回
通用显示，文案请求见 2.7）。

### 2.4 测试结果

#### 2.4.1 功能测试与 lint

| 范围 | 命令 | 结果 |
|---|---|---|
| 日切单元 | `pytest tests/tasking/test_dayroll.py -m "not slow"` | 7 passed |
| 日切 S7 批处理 | `pytest tests/tasking/test_dayroll.py -k s7_batch`（needs_data + slow） | 1 passed（54 s；事件 `t_cal_unix_ns` 为 UTC 20:00:00，`t_sim` ≈ 120 s） |
| M20 与契约 | `pytest -m "not perf and not slow" tests/tasking tests/contracts` | 463 passed（含正在并行修改的 `test_ckpt.py`） |
| 浸泡与容量工具 | `pytest tests/e2e/test_sandbox_bench_tools.py -m "not slow"` | 8 passed |
| 浸泡工具真实栈冒烟（S0） | `pytest tests/e2e/test_sandbox_bench_tools.py -k soak_smoke`（needs_data + slow） | 1 passed（新判据集合与 `info` 断言） |
| M20 批处理确定性 | `pytest tests/tasking/test_batch.py`（slow） | 1 passed（`relay_stats.json` 与事件逐字节一致） |
| 批处理确定性、驻留、剧本静态 | `pytest -m "not perf and not slow" tests/sim/test_batch_determinism.py tests/sandbox/test_park.py tests/e2e/test_scenarios_static.py` | 188 passed |
| supervisor 日历 | `pytest tests/runtime/test_show_calendar.py` | 3 passed |
| 运行时与实时协议回归 | `pytest -m "not perf and not slow" tests/runtime tests/rt tests/e2e/test_sandbox_bench_tools.py tests/scenarios` | 349 passed、6 deselected |
| 契约生成物 | `tools/contracts/gen.py --check`、`gen.mjs --check`、`gen_golden.py`、`gen_frames_golden.py`、`gen_env_golden.py`、`gen_fixtures.py`、`gen_openapi.py` `--check` | 全部最新 |
| 前端契约 | `npx vitest run --project unit tests/contracts` | 271 passed |
| 类型 | `npx tsc --noEmit -p apps/web/tsconfig.json` | 只有 `tests/m18/overlayMove.test.ts` 的 14 个错误（该文件 07:21 被并行的其他工作包修改，与本包无关）；本包涉及的生成类型无错误 |
| lint | `make lint` | 通过 |

#### 2.4.2 真实栈自检（`--smoke`，不取锁，不判定 RSS 斜率与时长）

`python tools/bench/sandbox/soak.py --hours 0.12 --viewers 2 --smoke --no-cgroup --calendar-start-local 03:57`（S7、show-api、sandbox 栈，负载 13–15）：

| 判据 | 结果 |
|---|---|
| `soft_reset_0400` | 通过：日历跨越 1、`relay.day_rollover` 1（`day = 2026-10-11`，`t_cal_unix_ns = 1791748800000000000` 即北京 2026-10-12 04:00:00，`tz = Asia/Shanghai`） |
| `day_table` | 通过（该时刻接力尚未开始监控，按规则没有日表行） |
| `discovered` | 通过（事件 0、日表 0） |
| `unexpected_restarts`、`oom` | 通过 |
| `duration_h`、`rss_slope.*` | 不判定（`--smoke`） |
| 观测 | viewer 收帧 8612；`relay.dispatch` 2、`task.created` 3；`report.partial.json` 按 2 min 周期写出 |

#### 2.4.3 内存增长归因（诊断，非门禁）

D2-AC-39 的 RSS 斜率判据在第 1 轮缩短窗口内有余量（sim-core 1.246、show-api 0.332 MB/h，阈值 2）。为判断 24 h 是否仍有余量、以及哪些结构在增长，
做了以下诊断（不取锁、负载 8–18，只作归因，不作判定）：

| 诊断 | 结果 |
|---|---|
| 第 1 轮 6 h 数据的形状（`soak/s7-6h/samples.jsonl`，sim-core RssAnon） | 0.5–2 h 1.69、2–4 h 1.21、4–6 h 1.05 MB/h；线性拟合残差 0.17 MB，对数拟合 0.41 MB，增长接近线性且斜率缓降。仅作风险评估：按该形状 24 h 斜率不高于 6 h 的 1.25 MB/h（不作为判定，不外推） |
| S7 批处理（sandbox100）tracemalloc，修复前代码（4 帧，仿真 0.33 h 起追踪；快照 0.67、1.0、1.33 h） | 追踪起的净滞留 1.23 → 1.44 → 1.71 MB（约 0.63–0.81 MB/h【仿真】）。1.0 → 1.33 h 的 +0.258 MB 中：M20 作业结果摘要 `jobs.digests` +0.091 MB（有界，4096 条后每次删 1024 条，约 3.4 h 饱和）；M20 `wind_mps` → M07 `env.query` → `_z_ground` 的 `bilinear_cc` 结果数组两条链各 400 个（+0.083 MB，每 3 s【仿真】各 1 个，线性不饱和，见 `d2-FX-R2-other-to-M07.md`）；sim-core 事件补拉环中的 `env.keyframe`（有界，4096 条）约 +0.03 MB；批处理专有（事件捕获与等待记录）约 +0.02 MB |
| 同上，修复后代码（2 帧，仿真 1.0 h 起；快照 1.5、2.0 h） | 1.5 → 2.0 h 净增 0.35 MB：摘要 0.136 MB（有界）、M07 链 0.050 MB、批处理专有约 0.03 MB、事件补拉环约 0.04 MB；另有 M18 `targets/runtime.py` 写状态块一处 +0.058 MB（同一 M18 代码在修复前的 4 帧追踪中没有出现，可能是快照时刻的瞬时对象，未定论）。本包的改动没有引入新的增长来源 |
| 实时 sim-core 的 tracemalloc（sitecustomize 钩子） | 不可行：快照期间主循环暂停超过 StateRing 存活阈值 2 s，supervisor 判 hung 并重启 sim-core；改用批处理 |

结论：修复前后都没有发现与 D2-AC-39 阈值同量级的泄漏。本包清理的接力工作项与分配记录在 S7 中每日约 190 项（每项约 1–2 KB，另有每秒发布与遍历的成本），
是 M20-FR-074 规定要清理的累积结构；M07 链约 0.25 MB/h【仿真】，已转交 M07。第 1 轮 sim-core 的 RSS 增长（约 1.1 MB/h）中 Python 对象只占一部分，
其余来自分配器碎片与 numpy、numba 的原生分配，tracemalloc 不可见。诊断脚本与快照在 scratchpad（未入库）。

### 2.5 偏差

1. **D2-AC-39 没有运行 24 h**：并行阶段不跑性能基准；本包只修复了使本条无法判定的缺陷与工具，并以 `--smoke` 验证链路。判定以第 2 轮验收的 ≥ 24 h
   运行为准（请求 M16，见 2.7）。没有缩短、没有外推，也没有把任何子项写成通过。
2. **行为变化（规格措辞待修订）**：①日界由 UTC 04:00 改为会话日历当地 04:00；②日表行由累计值改为当日值，字段增加；③软重置删除已结束且不被架次引用
   的接力工作项与分配记录（`task/{tid}/status` 的 `items[]` 每日收缩）；④新增事件 `relay.day_rollover`。沙盒会话有日历与接力时同样在当地 04:00 日切。
   文档由文档总编修订（17 §17.7.5、M20 PRD FR-074 与 §7.1–7.2、AWR-04 §4.10），本包没有改动 `docs/`（报告除外）。
3. **跨所有权写入**：按"允许跨模块修改该区域相关代码"改了 M20、M16、M00（契约与生成物）、M11（supervisor）的文件，均已写知会请求。
   `python/awr/sim/tasking/` 同时有其他修复工作包在改（`ckpt.py`、`plugin.py`、`runtime.py` 今日被修改）：本包没有改 `runtime.py`、`plugin.py`、`ckpt.py`，
   `relay.py` 只做了局部替换，主体放在新文件 `dayroll.py`。
4. **`--lock-scope start` 是否用于验收**：工具提供了与 D2-MS5 一致的锁范围，缺省仍为 `run`；是否在第 2 轮采用由验收方与文档总编决定。

### 2.6 遗留

1. **≥ 24 h 浸泡**（P0）：第 2 轮运行 `soak.py --hours 24.5`（cgroup 模拟、24 个 viewer、S7；建议 `--lock-scope start` 并在验收开始时启动，核 0–3 留给浸泡）。
2. **S7 共享展示的单步 p99 与 RTF**：第 1 轮 6 h 中 1 Hz 瞬时 RTF 最小 0.945、单步 p99 12–15 ms，伴随 `sim.stage.overbudget` 9758 次（4.1 第 5 条，属 sim 区域与
   D2-AC-04），不是 D2-AC-39 的判据，本包未改动。
3. **日切状态不进 checkpoint**：恢复后重定日界，不补发恢复期间跨过的日切；如需补发，请 M20 在 `ckpt.py` 中保存 `DayRoll` 的 `next_ns` 与 `key`（已写请求）。
4. **日表的线上契约**：日表行目前只在 `relay.day_rollover` 事件与批处理 `relay_stats.json` 的扩展字段中；是否纳入 `tasking/relay_stats.schema.json`
   由 M20 与 M00 决定（已写请求）。
5. **前端文案**：`relay.day_rollover` 在事件表中为通用显示（P2，已请 M15）。
6. **M07 `env.query` 的小数组滞留**（诊断线索，约 0.25 MB/h【仿真】，见 2.4.3）：已转交 M07，本包未改动 `python/awr/environment/`。

### 2.7 请求

| 文件 | 对象 | 内容 |
|---|---|---|
| `.cache/impl/requests/d2-FX-R2-other-to-M16.md`（追加第 4–8 条） | M16、验收 | 工具改动复核；第 2 轮 ≥ 24 h 运行命令与自检命令；`--lock-scope start` |
| `.cache/impl/requests/d2-FX-R2-other-to-M20.md`（新） | M20（抄送并行修改 tasking 的工作包） | 日切缺陷与修复、`relay.py` 的局部修改、与 checkpoint 的关系、日表行字段 |
| `.cache/impl/requests/d2-FX-R2-other-to-M00.md`（新） | M00 | `relay.day_rollover` 的契约登记与生成物 |
| `.cache/impl/requests/d2-FX-R2-other-to-M11.md`（新） | M11 | supervisor `AWR_SHOW_CALENDAR_START` 与测试 |
| `.cache/impl/requests/d2-FX-R2-other-to-M15.md`（新） | M15（P2） | 事件表文案 |
| `.cache/impl/requests/d2-FX-R2-other-to-M07.md`（新） | M07（抄送 M20，P2） | `env.query` 只求风时 `_z_ground` 结果数组的滞留（诊断线索，约 0.25 MB/h【仿真】） |
| `.cache/impl/requests/d2-FX-R2-other-to-文档总编.md`（追加第 4–6 条） | 文档总编（抄送 M20、M16、M00、M11） | AWR-04 §4.10 与 ADR-094 的日界与清理口径、17 §17.7.5 新事件行、M20 PRD 措辞、D2-AC-39 的执行口径（不缩短不外推、软重置与日表的判定方式、RTF 非判据、与 D2-MS5 并行的锁范围） |

## 第 1 部分（2026-10-07）：D2-AC-26（D1 不回退）

以下为第 1 次分派的报告原文（节号加前缀 1.；文中"第 n 节""n.m"指本部分的 1.n、1.n.m）。

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-other：D2-AC-26（P0，不通过，D1 不回退）的根因定位与修复 |
| 区域 | other：第 1 轮诊断指向 M08（N = 1000 周期性逐机卡顿）与 M06（trails 图层回退）；按工作包授权跨模块修改了 M13（`python/awr/sim/sensors/nav_pub.py`、`tests/sensors/`）与 M06（`apps/web/src/engine/mission/MissionOverlay.ts`、`apps/web/src/viewport/layers/mission.tsx`、`apps/web/tests/m06/`）的文件 |
| 日期 | 2026-10-07 |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（D2-AC-26 行、§4.2、§8 第 3 条）；`d2-ACC-1-to-M08.md` 第 4 条、`d2-ACC-1-to-M06.md` 第 1 条；AWR-04 v1.3 §13（D2-AC-26）、§4.4（单会话机群上限 12）、§14.2；AWR-03 §3.8（Tier S 图层上限）、ADR-033（运行协议）、ADR-071（固定层阈值冻结）；17 §17.7.2（Topic 表）；M13 PRD M13-FR-209、M13-NFR-201；M06 PRD M06-FR-044；AWR-18 §5.1、§5.2；D1 第 5、6 轮验收报告；D1 代码快照 `.cache/cleanroom/`（用于对照） |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4）、无 GPU；Chrome for Testing 151（SwiftShader，C1）；Node 22.12；`.venv` Python 3.12.3；同机并行的其他修复工作包使负载持续在 8–30（诊断时记录） |
| 约束执行 | 没有安装依赖；没有执行 git 写操作；没有跑性能基准（没有取 `runs/.perf.lock`、没有走 harness 判定；所有测量都是诊断级，写在 scratchpad，负载逐次记录在第 4 节）；没有修改阈值、没有冻结"暂定"阈值、没有新 ADR；没有 emoji 与禁用字形；没有新材质、新程序或新渲染路径（只减少绘制内容），WebGL2 上限不受影响；文档措辞修订写成请求 |

### 1.1 结论摘要

D2-AC-26 的 5 个不通过子项来自两个根因，都已修复：

1. **N = 1000 周期卡顿（D1-AC-07 RTF、CPU、单步 p99；D1-AC-27 全机 RTL 单步最大；D1-AC-08 tick 年龄）**：根因不在 M08 的 stage，而在
   M13 慢任务 `m13.payload_pub`。D2 起 P600 为对象形式（D2-INT），D1 阶梯的 1000 架 P600 都有非 0 挂载掩码，被当作 D2 机体，
   每 500 ms【仿真】在同一轮内逐机构建 `awr.uav.payload.v1`（约 50 µs/架，N = 1000 单步约 50 ms，每秒 2 次）。同一消息（0.62 MB、1000 项）
   每秒 2 次送到 api，拆分一次 7.2 ms（首次 27 ms），抬高 tick 年龄。修复：D2 机体超过 12 架（17 §17.7.2 的"全部机体 ≤ 12"、单会话机群上限）
   时只为兴趣集生成，构建按每轮 4 架分片、每周期一次发布。诊断（负载 8–9）：`m13.payload_pub` 229 → 0.5 ms/仿真秒，RTF 0.838 → 0.996、
   0.999，CPU 0.572 → 0.500、0.482 核，追帧饱和 52 → 13、6，单步最大 103 → 10–11 ms（负载下的残余见 4.2）。
2. **trails 图层组 8.24 ms（D1-AC-03b 固定层合计 13.25 ms）**：根因不是轨迹，而是同组的任务航线。ladder n200 的 200 架都在执行 20 圈环绕
   任务，页面画满 8192 段计划路径与约 1700 段已执行路径；D1 第 6 轮测得 1.17 ms 时页面没有任何航线（快照 `__perf.layers.trails.draws`
   D1 为 0、D2 为 1）。修复：有航线的机体超过轨迹机数上限（Tier S 16、Tier B/A 64）时，任务航线只画有轨迹的机体（选中、主选与关注集
   轨迹槽位，AWR-03 §3.8 "轨迹只画选中机与关注集"），PerfGovernor 的轨迹步同时作用于航线。ladder n200 配对页面逐秒采样：航线 0 段
   （200 条全部跳过），与 D1 第 6 轮页面内容相同；按图层隔离的配对诊断中全开与隐藏 mission 已无差别。

两项修复对沙盒（≤ 12 架）与剧本 S0–S7（≤ 10 架）的行为没有影响。功能测试与 `make lint` 通过。D2-AC-26 的判定需第 2 轮验收按 ADR-033
复测（`d2-FX-R2-other-to-M16.md`）。

### 1.2 根因定位

#### 1.2.1 N = 1000 周期卡顿（M08 → M13）

| 证据 | 说明 |
|---|---|
| stage 成本不变 | 第 1 轮 N = 1000 的 `stage_ms_per_s` 合计 470（D1 第 5 轮 455）；单步 p50 2.15 ms（D1 2.16 ms）。尖峰不在 pipeline 内 |
| 尖峰形态 | 第 1 轮逐秒序列：每秒最大 48–51 ms，4 秒中有 3 秒 p99 ≈ 47 ms；p99 随 N 线性（200 / 500 / 1000：10.5 / 23.7 / 49.5 ms） |
| 慢任务归因 | 诊断运行（`fleet_ladder` 单点 N = 1000、窗口 30 s，订阅 `state/sim-core/perf` 的 `slow_ms_per_s`）：`m13.payload_pub` 229.5 ms/仿真秒，其余慢任务合计 30.3（`state_ext` 19.2、`m13.lidar_scan` 4.0、`perf` 1.7 等） |
| 代码 | `PayloadPub.slots()` = 全部挂载掩码非 0 的机体 ∪ 兴趣集；`run_slow` 在一轮内对全部 slot 调 `payload.message()`（每架按传感器逐项建字典）。P600 自 2.0.0 起为对象形式，`_spawn` 对共享展示与剧本机体按 `default_mask(sandbox=False)` 置非 0 掩码，于是 D1 阶梯全部机体都进入集合 |
| 设计前提 | 17 §17.7.2：`uav/{id}/payload` "兴趣集控制：否（全部机体 ≤ 12）"；AWR-04 §4.4 单会话机群上限 12。该前提在共享展示与阶梯的 200–1000 架下不成立 |
| 连带 | `storm.rtl` 窗口内各秒单步最大的中位已是 49.6 ms（与 RTL 无关）；api 侧 `StreamDemux.feed("payload")` 对 1000 项消息每次 7.2 ms（首次 27 ms，同时创建并通告 1000 个 `uav/{id}/payload` channel），每秒 2 次阻塞事件循环，tick 年龄 p99 10.61 → 15.78 ms |

#### 1.2.2 trails 图层组（M06）

| 证据 | 说明 |
|---|---|
| 组成 | `bench.ts` 的 trails 配对组 = trails、frustums、mission、zones、glyphs 五个图层 |
| 按图层隔离 | 私有测试构建 + 同一 live 后端（ladder n200），以 `awr.layers.v1` 依次隐藏一个图层，各跑满 150 个配对（负载 9–12）：全开 12.04 ms、隐藏 trails 10.10、隐藏 frustums 16.92、隐藏 zones 13.62、**隐藏 mission 2.51**、只留 glyphs 0.60 ms；按同次 groundSky 归一后，只有隐藏 mission 使比值从 1.7–1.8 降到 0.33 |
| 数据 | 运行目录 `paths/` 有 200 个 `uav/{id}/path` 文件（480–1184 B，约 30–70 个控制点）；页面 `mission` 图层的计划路径画满 `WideLineBatch` 上限 8192 段，已执行路径约 1700 段；世界相位 `trails` 键 CPU p95 0.045 → 5.6 ms、最大 28.7 ms（4 Hz 重建遍历全部航线） |
| 与 D1 的差别 | D1 第 6 轮快照 `layers.trails.draws = 0`、`trails` 键 CPU p95 0.045 ms，即 D1 的 1.17 ms 是在页面没有航线时测得的。航线数据链（M10 `path.changed`、M11 `on_path_changed`、`stores/mission.ts` 订阅 `uav/*/path`）在 D1 快照与当前代码中逻辑相同，D1 页面没有收到航线的具体原因未能复现（D1 后端已不可用），不影响结论：ADR-071 的 1.5 ms 是在没有任务航线的负载下冻结的（AWR-18 §5.2 第 4 条的被测负载也不含航线），而 D2 页面收到并绘制了 200 条 |
| 规格 | AWR-03 §3.8 "轨迹、视锥与叠加（含任务叠加、zones、GlyphLayer）" Tier S ≤ 1 ms（冻结 1.5），硬上限只写了"轨迹只画选中机与关注集，≤ 16 架"，任务叠加没有上限 |

### 1.3 修复与实现清单

| 编号 | 实现 | 路径 |
|---|---|---|
| D2-AC-26 → D1-AC-07、08、27；M13-FR-209、M13-NFR-201 | `PAYLOAD_ALL_MAX = 12`：D2 机体 ≤ 12 架时为全部 D2 机体并兴趣集（不变）；超过时只为兴趣集（detail 并 marks，≤ 80）生成，计数 `stats["interest_only"]` | `python/awr/sim/sensors/nav_pub.py` |
| 同上 | `PAYLOAD_SLICE = 4`：周期开始时取集合，每轮至多构建 4 条（逐机消息带构建时刻），构建完成后以周期开始时刻为头部 `t_sim_ns` 一次发布全集消息（每周期一条，M11 拆分方式与总线形状不变；2 Hz 不变） | 同上 |
| 同上（健壮性） | 仿真时间回退（`sim/reset`、恢复）时放弃在途周期，`next_due` 超前一个周期以上时归零（此前重置后要等仿真时间追上重置前的时刻才恢复发布） | 同上 |
| D2-AC-26 → D1-AC-03b；M06-FR-044；AWR-03 §3.8 | `MISSION.allPathsS = 16`、`allPathsBA = 64`；`MissionOverlay.allPathsMax`、`setPathVehicles(ids)`、`pathStats{drawn, skipped}`：有航线的机体超过上限时重建只遍历并绘制给定机体的航线；上限内与未设上限时行为不变 | `apps/web/src/engine/mission/MissionOverlay.ts` |
| 同上 | 适配器按层级设上限；世界相位（drones 相位已分配轨迹槽位之后）以 `trailVehiclesKey(L)`（选中、主选、关注集轨迹槽位、名册大小的哈希，无分配）检测变化，变化时以 `trailVehicleIds(L)` 更新；PerfGovernor 轨迹步（16 → 8 → 4 → 只选中）同时作用 | `apps/web/src/viewport/layers/mission.tsx` |
| 测试 | 载荷：大机群只为兴趣集、≤ 12 架全集与 2 Hz、每轮 ≤ 4 次构建且一次发布、重置后继续发布（4 例） | `tests/sensors/test_payload_scale.py`（新） |
| 测试 | 航线：上限内全画、超过上限只画给定机体且集合变化时重建、未设上限全画、轨迹机体的取法与键（4 例） | `apps/web/tests/m06/mission-fleet.test.ts`（新） |

没有修改的部分：M08 的 stage、主循环与慢任务框架；M11 网关（拆分逻辑对部分集合本来就成立）；`bench.ts` 与 harness 用例；阈值表。

### 1.4 测试结果

#### 1.4.1 功能测试与 lint

| 范围 | 命令 | 结果 |
|---|---|---|
| M13 全部 | `pytest -m "not perf" tests/sensors` | 199 passed（含新增 4 例；`test_nav_payload.py` 的 2 Hz 全集、D1 机体只在兴趣集、M11 拆分形状原样通过） |
| 载荷相关 | `pytest tests/rt/test_d2_topics.py tests/sim/test_fleet_rest_sandbox.py tests/sim/test_profiles_catalog.py tests/catalog` | 104 passed |
| show-api 公开模式（D2-AC-26 子项） | `pytest tests/rt/test_public_mode.py` | 11 passed |
| 前端单元 | `npx vitest run --project unit` | 1359 passed、1 skipped（167 个文件） |
| 前端浏览器 | `npx vitest run --project browser tests/m06/{visual,backend,trailBatch}.browser.test.ts` | 9 passed |
| 类型 | `npx tsc --noEmit -p apps/web/tsconfig.json` | 0 错误 |
| lint | `make lint` | 通过 |

#### 1.4.2 诊断测量（非门禁；负载高，只作根因与方向确认）

**sim-core（`fleet_ladder` 单点 N = 1000，`--smoke`，剧本 ladder-shenzhen，窗口 30 s；脚本只额外订阅 perf 的 `slow_ms_per_s`）**

| 运行 | 运行内负载均值 | RTF | CPU（核） | 单步 p50 / p99 / 最大（ms，各秒最大） | 追帧饱和 | `m13.payload_pub`（ms/仿真秒） | 慢任务合计 |
|---|---|---|---|---|---|---|---|
| 修复前 | 12.8 | 0.838 | 0.572 | 3.10 / 69.8 / 102.9 | 52 | 229.5 | 259.8 |
| 修复后 1 | 9.1 | 0.996 | 0.500 | 3.29 / 9.08 / 10.4 | 13 | 0.52 | 27.8 |
| 修复后 2（40 s） | 8.0 | 0.999 | 0.482 | 3.59 / 8.28 / 11.2 | 6 | 0.49 | 26.1 |

修复后各秒 p99 在 2.8–9 ms 之间随负载起伏，没有周期性；p50 本身比 D1 安静环境（2.16 ms）高 50–65%，说明残余来自同机负载而不是逐机路径。
另以 sitecustomize 钩子记录主循环逐迭代的线程 CPU 时间并归因（负载 29 的一次运行，只看归因不看时间）：前 2% 的迭代都是追帧批次中的 stage
时间，慢任务只出现 `perf`、`env.heartbeat`（1 Hz，D1 既有）与 `cleanup`，没有逐机尖峰。

**api 拆分（in-process 微基准，1000 架 p600_mid360 的真实载荷消息）**：消息 624 027 B；sim-core 构建 89 ms（高负载下）；
`StreamDemux.feed("payload")` 首次 27.0 ms、此后中位 7.2 ms。修复后 N = 1000 无兴趣集时不再发布该消息。

**trails 图层组（私有测试构建 + live ladder n200，`?bench=layers&fixedB=25000&n=200`，每个变体 150 个配对）**

| 构建 | 变体 | 负载 | 基底（ms） | trails 组 | groundSky | trails / groundSky |
|---|---|---|---|---|---|---|
| 修复前 | 全开 | 9–12 | 27.4 | 12.04 | 6.65 | 1.81 |
| 修复前 | 隐藏 mission | 9–12 | 40.2 | 2.51 | 7.59 | 0.33 |
| 修复后 | 全开 | 19–21 | 43.9 | 5.12 | 9.03 | 0.57 |
| 修复后 | 隐藏 mission | 19–21 | 43.4 | 4.78 | 9.13 | 0.52 |

修复后全开与隐藏 mission 一致（mission 已不贡献）；修复后两行绝对值偏高是负载 19–21 下的整体放大（同次 drones、environment 配对为 −1.9、
−4.2 ms，噪声达数毫秒），不能与 D1 的 1.17 ms 直接比较。确定性的工作量证据：修复后页面逐秒采样 150 s（96 个样本），关注集 0、轨迹绘制 0、
航线计划段 0 与已执行段 0（`pathStats` 200 条全部跳过）、zones 2、符号 200，与 D1 第 6 轮页面的内容一致（D1 快照 trails 键 draws 0）。

### 1.5 偏差

1. **没有跑门禁基准**：按"并行阶段不跑性能基准"，第 4.2 节全部是诊断测量（不持锁、单次、负载 8–30），只用于确认根因与修复方向；
   D2-AC-26 各子项的判定以第 2 轮验收为准。
2. **行为变化（规格措辞待修订）**：①D2 机体超过 12 架时 `uav/{id}/payload` 改为受兴趣集控制（17 §17.7.2 原文以"全部机体 ≤ 12"为前提，
   M13-FR-209 原文写"订阅时生成"，两者本就不一致）；②有航线的机体超过 16（Tier S）/ 64（Tier B/A）架时只画有轨迹机体的航线（AWR-03 §3.8
   未给任务叠加上限）。两项对沙盒与 S0–S7 无影响。文档由文档总编修订，并建议以一条 ADR 记录（草案见 `d2-FX-R2-other-to-文档总编.md`），
   本包没有改动 `docs/`（报告除外）。
3. **D1 页面为何没有航线未能复现**：D1 后端不可用，代码对照未发现差别；结论只依赖"D1 的 1.17 ms 测于无航线页面、D2 页面有 200 条航线"
   这一快照事实。

### 1.6 遗留

1. **N = 1000 单步 p99 余量**：第 1 轮未被尖峰污染的秒（每次运行 15 个）中 p99 中位 2.68–2.81 ms、最大 2.96–3.27 ms（门禁取各秒最大
   ≤ 3 ms，D1 第 5 轮 2.69 ms）；稳态成本相对 D1 上升的 stage 为 `sensors`（36.2 → 44.0 ms/仿真秒；微基准 1000 架每次 101 → 123 µs）与
   `contact`（15.7 → 19.1）。这两处属于 sim 区域同期修复正在改动的文件，本包没有改动，已请 M08、M13 关注（`d2-FX-R2-other-to-M08.md`）。
2. **`NavPub` 与 D1 的 `SensorPosePacker` 在 `sim/reset` 后停发**：`next_due` 不随仿真时间回退，循环剧本（S0 `on_complete = reset`）每轮
   重置后视锥位姿与 nav 要等仿真时间追上重置前的时刻才恢复；不在 D2-AC-26 范围，未改动，已请 M13 评估（`d2-FX-R2-other-to-M13.md` 第 5 条）。
3. **`d2-ACC-1-to-M06.md` 第 2 条**（整景靠治理器隐藏 D2 图层，D2-AC-30）不在本包范围。
4. **工具**：`fleet_ladder` 记录不含 `slow_ms_per_s`，`layers` 用例不记录被测负载（航线段数、关注集），第 1 轮因此只能推断；已向 M16、M08
   提出（`d2-FX-R2-other-to-M16.md` 第 2、3 条）。

### 1.7 请求

| 文件 | 对象 | 内容 |
|---|---|---|
| `.cache/impl/requests/d2-FX-R2-other-to-M13.md` | M13 | 知会 `m13.payload_pub` 的改动与测试；M13-FR-209 措辞；`NavPub` 与 `SensorPosePacker` 的重置停发 |
| `.cache/impl/requests/d2-FX-R2-other-to-M06.md` | M06 | 知会任务航线大机群上限的改动、证据与测试 |
| `.cache/impl/requests/d2-FX-R2-other-to-M08.md` | M08（抄送 M13） | 第 4 条已定位与修复；N = 1000 p99 余量与 `sensors`、`contact` 稳态上升 |
| `.cache/impl/requests/d2-FX-R2-other-to-M16.md` | M16 | 第 2 轮按 ADR-033 复测五个用例；`fleet_ladder` 记录 `slow_ms_per_s`；`layers` 记录被测负载 |
| `.cache/impl/requests/d2-FX-R2-other-to-文档总编.md` | 文档总编（抄送 M13、M06、M11、M16） | 17 §17.7.2 与总线表、M13-FR-209、AWR-03 §3.8、M06-FR-044、AWR-18 §5.1/§5.2 的措辞修订与 ADR 草案 |
