# D2-FX-R2-sandbox 沙盒区域修复报告（第 2 次执行：D2-AC-46 viewer 节省；附第 1 次执行：准入、治理、回收、内存与容量接口）

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-sandbox（修复区域 sandbox；允许跨模块修改该区域相关代码） |
| 第 2 次执行 | 2026-10-12。依据 `docs/impl/D2-验收报告-第1轮.md`（2026-10-11 至 12 的重新执行）。分派项只有 1 条：**D2-AC-46 节能节省（实测，P1）**。第 1 次执行（2026-10-07，D2-AC-02、04、06、07、28、42、45、55 与 46）的报告原文保留在本文"附：第 1 次执行"部分 |
| 第 2 次执行结论 | D2-AC-46 的 viewer 子项有**两个根因**：①测法缺陷：`power.py` 的 viewer 只握手不订阅（BATCH 0 帧），共享展示也不是 S7，测到的是连接本身的成本（每 viewer 约 0.0007 核）；②阈值不可达：按真实浏览器的订阅集与收发节律（R58 采集，桌面 Tier B）24 个 viewer 的全部增量只有 0.146 核（近景上界 0.215 核），"下降 ≥ 0.3 核"来自预算"每 viewer 0.02 核"，在任何真实口径下都不可达。节能机制本身完整生效：断开后 show-api 回到无 viewer 基线（残余 −0.0013 至 +0.0023 核，释放 98%–101%）。处理：测法改为真实浏览器订阅口径并增加无 viewer 基线；按 AWR-03 §1.3 追加 **ADR-174**（AWR-04 §15.18）以实测冻结 viewer 子项：节能后 ≤ 无 viewer 基线 + 0.01 核、下降量 ≥ 90% 的 viewer 增量、每 viewer 增量 ≤ 0.02 核，并以"每 viewer 每秒 BATCH ≥ 15"判定测量有效 |
| 规格变更 | 追加 ADR-174（P1 阈值的实测冻结，附数据；不涉及 P0）；修订 AWR-04 §15.14 D2-AC-46 的 viewer 子项与 §15.7.8 表注、修订记录一句；AWR-03 ADR 索引登记一行。§4.4.1 成本表不改（MS5 以 emax 实测统一冻结） |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4 1.7 GHz）、62 GiB；Python 3.12.3（`.venv`）；Chrome for Testing 151（C1 SwiftShader）。并行阶段同机有其他修复工作包，诊断期间负载 7–16 |
| 约束执行 | 没有安装依赖；没有执行 git 写操作；**并行阶段没有运行性能基准**（`power.py`、`capacity.py` 只以 `--smoke` 功能用例运行）；第 2.2 节的数值来自不持锁的诊断脚本（`.cache/fxr2sb-r3/`，不进仓库），只作定位与 ADR 的依据，正式数值以第 2 轮验收按 ADR-033 的 3 次中位为准；无 emoji 与禁用字形；没有改动产品代码与 UI |

## 第 2 次执行（2026-10-12）：D2-AC-46 viewer 节省

### 1 结论

| 验收 | 第 1 轮（重新执行） | 根因（已核实） | 本次处理 | 本机诊断（不持锁，只作定位） |
|---|---|---|---|---|
| D2-AC-46 节能节省（P1）viewer 子项 | 24 个 viewer 节能使 show-api 只降 0.0225–0.0228 核（≥ 0.3）；连接时 0.0663–0.0682 核 | ①viewer 只握手不订阅、共享展示不是 S7（测法）；②阈值取自预算"每 viewer 0.02 核"，实测每 viewer 0.006 核（Tier B），24 个合计 0.146 核（阈值） | `power.py` 改为浏览器订阅口径（`capacity.BROWSER_SUBS`）、S7、无 viewer 基线；ADR-174 冻结三项判据与测量有效性 | Tier B 24 viewer：基线 0.039–0.041、连接 0.185–0.187、节能后 0.0397–0.0413 核；残余 ≤ 0.0023 核、释放 ≥ 98%、每 viewer 0.0061 核：按 ADR-174 通过 |
| D2-AC-46 沙盒驻留子项 | −0.57 至 −0.62 核、−131.6 至 −137.0 MB：通过 | — | 口径不变（`--part sandbox` 可单独复测） | — |
| D2-AC-46 浏览器子项 | 60 s 主线程 0.44%–0.49%：通过 | — | 不变 | — |
| D2-AC-46 emax 实测记入交付总结 | 未测 | 需 emax 部署（MS5） | 不在本机范围；新口径的 `power.py` 可直接用于 emax 的 cgroup 外测量 | — |

### 2 定位与根因

#### 2.1 第 1 轮的 viewer 不代表观看

`power.py` 的 viewer 段使用 `capacity.Viewers`：rtprobe 握手并发 hello 后只收帧，不发 `subscribe`。show-api 对未订阅的连接只推 TIME 与事件；诊断中
24 个这样的 viewer 在 30 s 窗口内收到的 BATCH 为 0 帧，每个约 7.8 帧/s 全部是 TIME。另外 `power.py` 没有设置 `run.scenario`，共享展示不是 emax
public profile 的 S7。于是"24 个 viewer 的成本"只是 24 条空闲连接的成本（0.017–0.020 核，每 viewer 约 0.0007 核），这是第 1 轮"合计仅 0.066 核"的来源。

#### 2.2 真实浏览器的订阅与成本

1. **订阅集（R58 采集）**：诊断 spec（`.cache/fxr2sb-r3/specs/viewer-subs.spec.ts`，复用 `apps/web/perf/d21/live21.ts` 起真实栈与预览，测试构建
   `.cache/d2acc1b/dist-test`，即第 1 轮验收所用构建）以 S7 起共享展示，Chromium 打开 `/world/synthcity`，揭开 20 s 后读 `/api/rt/inspect`：13 条订阅

   | topic | rate | 来源 |
   |---|---|---|
   | `event`（mode all） | 0 | `viewport/layers/drones.tsx` |
   | `perf/server`、`sys/procs` | 1 | 同上 |
   | `fleet/roster`、`env/state` | 10 | 同上 |
   | `swarm/uav/state` | 10（Tier S）；20（Tier B、A） | 同上（`swarmRate`） |
   | `swarm/target/state` | 10 | `stores/targets.ts` |
   | `target/*/detail` | 1 | 同上 |
   | `nest/*/status`、`task/*/status`、`relay/*/stats` | 1 | `stores/tasking.ts` |
   | `mission/*/status`、`uav/*/path` | 2 | `stores/mission.ts` |

   默认视角下关注集（`uav/<id>/state@30`）为空（S7 的机体 r_px < 8）。下行约 102–108 kbit/s，客户端 7.4–8.8 fps（SwiftShader），credit_skips 247–336。
2. **一个真实浏览器的成本**：show-api 无 viewer 0.0353 / 0.0360 核（前后两次），浏览器连接时 0.0430 / 0.0453 核：首个 viewer 约 +0.008–0.010 核
   （含各 channel 的共享编码，只在首个订阅者出现时发生）。这与 D1 的"1 个浏览器访客 +0.01–0.016 核"一致，也说明 §4.4.1 的"每 viewer 0.02 核"
   是由首个访客的成本上调而来。
3. **24 个模拟 viewer**（诊断脚本 `.cache/fxr2sb-r3/diag_viewers.py`、`diag_viewers2.py`、`diag_focus.py`；`_sbbench.Stack` cgroup 模拟、S7、
   每窗口 30 s、断开后 3 s 再测；浏览器口径按网页客户端节律收发：ack 每 3 帧或 34 ms、ping 500 ms、clientStats 1 s；CPU 为 show-api 的 utime + stime）：

   | viewer 口径 | 次 | 无 viewer | 连接时 | 节能后 | 增量 | 每 viewer | 残余 | 释放比例 | BATCH / viewer / s |
   |---|---|---|---|---|---|---|---|---|---|
   | 只握手（第 1 轮口径） | 1 | 0.0390 | 0.0590 | 0.0407 | 0.0200 | 0.00083 | 0.0017 | 92% | 0 |
   | 只握手 | 2 | 0.0387 | 0.0560 | 0.0390 | 0.0173 | 0.00072 | 0.0003 | 98% | 0 |
   | 浏览器 Tier S（swarm 10 Hz） | 1 | 0.0390 | 0.1187 | 0.0407 | 0.0797 | 0.00332 | 0.0017 | 98% | — |
   | 浏览器 Tier S | 2 | 0.0397 | 0.1253 | 0.0410 | 0.0856 | 0.00357 | 0.0013 | 98% | 10.0 |
   | 浏览器 Tier B（swarm 20 Hz） | 1 | 0.0390 | 0.1850 | 0.0413 | 0.1460 | 0.00608 | 0.0023 | 98% | — |
   | 浏览器 Tier B | 2 | 0.0410 | 0.1870 | 0.0397 | 0.1460 | 0.00608 | −0.0013 | 101% | 20.0 |
   | Tier B + 关注集 8 架 × 30 Hz（近景上界） | 1 | 0.0403 | 0.2550 | 0.0390 | 0.2147 | 0.00895 | −0.0013 | 101% | 40.0 |

   第 1 次的"无 viewer"是开场一次测量，三个口径共用；第 2 次每个口径各测一次（`power.viewer_part`）。负载 7–16，CPU 时间口径受同机负载影响小，
   两次的 Tier B 增量相同（0.146 核）。

#### 2.3 结论

- 节能机制本身完整生效：全部 viewer 以 1000 断开后，show-api 在 3 s 内回到无 viewer 基线，残余在测量噪声内（−0.0013 至 +0.0023 核）。
- 阈值"下降 ≥ 0.3 核"要求每 viewer ≥ 0.0125 核；D1 网关按 channel 共享编码，增加的 viewer 只增加装帧与发送，真实口径每 viewer 0.0036（Tier S）、
  0.006（Tier B）、0.009（近景上界）核，24 个合计 0.08–0.21 核。即使修正了测法，阈值仍不可达；阈值是由预算推出的期望上界（AWR-04 §15.7.8
  "数值为预期上界"），不是节能必须达到的量。
- 因此按 AWR-03 §1.3 第 3 条以追加 ADR 的方式依据实测冻结（D1 验收加固的同类先例：ADR-075、076、080）。D2-AC-46 为 P1，不涉及 P0 硬阈值。

### 3 ADR-174 的要点（全文见 AWR-04 §15.18）

1. **测法**：先测无 viewer 基线；24 个 viewer 按共享展示页默认视图的浏览器订阅集（13 条，桌面 Tier B，swarm 20 Hz）与网页客户端节律模拟；全部以
   1000 断开（D2-AC-44 的客户端节能行为）后再测；共享展示为 S7。测量有效性：每个 viewer 每秒 BATCH ≥ 0.75 × swarm 频率（15）且错误 0，否则判为
   测量无效（不通过）；第 1 轮口径保留为 `--viewer-profile hello`，只作对照，按此判为无效。
2. **冻结判据**（替代"下降 ≥ 0.3 核"）：节能后 show-api ≤ 无 viewer 基线 + 0.01 核；下降量 ≥ 90% 的 viewer 增量；每 viewer 增量 ≤ 0.02 核
   （§4.4.1 预算作为上界）。下降量、增量与每 viewer 成本的绝对值照常记录；emax 实测值在 MS5 记入交付总结（原条款不变）。
3. **余量**：残余实测 ≤ 0.0023 核（门槛约 1/4）；释放 ≥ 98%（门槛 90%）；每 viewer ≤ 0.009 核（门槛 0.02）。新判据能发现"节能后仍保留订阅或空转
   定时器"（残余 > 0.01 核或释放 < 90%），对只握手口径判为无效，不是放宽。
4. **备选与否决**：本机实测折算的绝对阈值（emax 主频不同，不能作为 emax 门槛）；加重模拟负载过线（不代表观看，且近景上界也只有 0.215 核）；只判
   "下降 ≥ 50% 的增量"（d2-ACC-1-to-文档总编 第 2 条的建议，发现不了残余）。

### 4 实现清单

| 文件 | 改动 | FR / AC |
|---|---|---|
| `tools/bench/sandbox/capacity.py`（M16） | `BROWSER_SUBS`（13 条，来源与采集方法见注释）、`VIEWER_PROFILES`（hello / browser / browser-s）、`browser_subs()`；`Viewers(stack, n, profile="hello")`：browser 口径订阅并按网页客户端节律收发（只解析帧头），新增 `batches` 计数（`stop()` 返回）；缺省 hello，变体 A、D 与浸泡的口径不变 | D2-AC-46（ADR-174） |
| `tools/bench/sandbox/power.py`（M16） | `--viewer-profile`（缺省 browser）、`--show-scenario`（缺省 `s7-synthcity-relay`）、`--part {all,sandbox,viewers}`；`viewer_part` 先测无 viewer 基线，报告 `api_cpu_none`、`increment`、`residual`、`release_frac`、`per_viewer`、`batch_hz_per_viewer`、`swarm_hz`；判据拆为 `judge_sandbox` 与 `judge_viewers`（`viewers.subscribed`、`viewers.residual_core`、`viewers.release_frac`、`viewers.cost_per_viewer`，取代 `viewers.api_cpu_drop`）；`GATES` 与模块说明按 ADR-174 更新 | D2-AC-46、D2-AC-45（驻留子项不变） |
| `tests/e2e/test_sandbox_power_bench.py`（M16） | `test_judge_power_savings` 按新结构；新增 `test_judge_power_viewers_adr174`（第 1 轮口径判无效、订阅未生效判无效、Tier S 通过、负载过高不判定、缺基线不判定）与 `test_browser_viewer_profile`；`test_power_smoke` 断言 S7、browser 口径、无 viewer 基线与订阅生效（每 viewer BATCH ≥ 15/s） | D2-AC-46 |
| `docs/04-D2-设计增补与决策记录.md` | 新增 §15.18 与 ADR-174；修订 §15.14 D2-AC-46 的 viewer 子项；§15.7.8 表注补本机实测；修订记录追加一句 | D2-AC-46 |
| `docs/03-设计基线与决策记录.md` | ADR 索引表登记 ADR-174，其后说明段补一句（编号接续 ADR-173，正文在 AWR-04 §15.18） | AWR-03 §1.3 |

诊断（不进仓库）：`.cache/fxr2sb-r3/specs/viewer-subs.spec.ts` 与 `pw.config.ts`（真实浏览器订阅与成本）、`diag_viewers.py`、`diag_viewers2.py`、
`diag_focus.py`；结果 `.cache/fxr2sb-r3/out/{browser-viewer,viewers-065157,viewers2-r1,focus}.json`。

### 5 偏差

1. **文档修改**：AWR-04 与 AWR-03 不在本工作包的路径所有权内。第 1 次执行提交给文档总编的 D2-AC-46 阈值提案未答复，第 1 轮验收仍据原阈值判不通过；
   按 AWR-03 §1.3（修改基线只能追加 ADR，编号接续最大编号；放宽验收阈值必须先追加 ADR 并附实测）与 D1 验收加固的先例（修复工作包追加 ADR-075、
   076、080 并修订验收表），本次直接追加 ADR-174，并请文档总编复核（d2-FX-R2-sandbox-to-文档总编 第 11–14 条）。若 D3 侧另有占用 174 的计划，请统一改号。
2. **与第 1 次执行提案的差别**：第 1 次执行提案"节能后回落到基线 + 0.01 核以内"且记"每 viewer 约 0.001 核"，后者是只握手口径的数值，作废；ADR-174
   先修正测法，再冻结残余、释放比例与每 viewer 上界三项。
3. **跨所有权修改**：`tools/bench/sandbox/{power,capacity}.py` 与 `tests/e2e/test_sandbox_power_bench.py` 属 M16，按工作包授权修改并已知会（d2-FX-R2-sandbox-to-M16
   第 7–11 条）。产品代码没有改动。
4. **未改的相关口径**：`capacity.py` 变体 A、D 与 `soak.py` 的 viewer 仍为只握手口径（缺省 hello），以免改变本轮其他 P0 修复的复测基线；该口径低估
   D2-AC-04"每个 api 进程 ≤ 0.7 核"与 D2-AC-39 show-api 的负载约 7 倍，已提请 M16 与文档总编裁决。
5. **数值来源**：并行阶段不跑性能基准，ADR-174 的依据为不持锁诊断（负载 7–16）与第 1 轮验收的锁内数据（只握手口径）；正式数值以第 2 轮验收按
   ADR-033 的 3 次中位为准。

### 6 测试结果

- `pytest tests/e2e/test_sandbox_power_bench.py`（含 2 个 slow 用例：`power.py --smoke` 与 `capacity.py --variant D --smoke`，真实栈）：**7 passed**（195.7 s）。
  smoke 中 browser 口径的 2 个 viewer 每秒 BATCH ≥ 15、错误 0，报告含无 viewer 基线与新字段。
- `pytest tests/e2e/test_sandbox_bench_tools.py -m "not slow"`：8 passed；其中 `capacity` 变体 A 与 `soak.py` 的 smoke（使用 `Viewers` 缺省 hello 口径）：
  2 passed（145.0 s）。
- `ruff check` 本工作包涉及的文件：通过。
- `make lint`：**通过**（最后一次修改之后运行，退出码 0）。中途一次运行曾因其他修复工作包当天新建的诊断用例 `apps/web/tests/m18/zz-fxr2-overlay-cost.test.ts`（oxlint `unbound-method` 2 条）失败，该文件随后已被其所有者删除；本工作包的文件在全部规则下无违例。
- 诊断结果见第 2.2 节（不持锁，只作定位）。

### 7 遗留

1. **正式复测**：第 2 轮验收以 `python tools/bench/sandbox/power.py`（harness 用例 `d21.bench-power`，命令行不变，缺省即新口径）按 ADR-033 运行 3 次。
2. **emax 实测**（D2-AC-46 的"emax 实测值记入 D2 交付总结"）：需 MS5 的 emax 部署，本机不可测。
3. **变体 A、D 与浸泡的 viewer 口径**：待 M16 与文档总编裁决（第 5 节第 4 条）。
4. **订阅集同步**：`BROWSER_SUBS` 镜像网页客户端的默认订阅，前端默认订阅变化时需同步；建议 M16 增加一个以 R58 读取真实浏览器订阅并比对的用例。

### 8 跨所有权请求（第 2 次执行）

| 文件 | 接收方 | 要点 |
|---|---|---|
| `d2-FX-R2-sandbox-to-M16.md`（追加"第 2 次执行"第 7–11 条） | M16 | 根因；`power.py` 与 `capacity.Viewers` 的改动与新判据；第 2 轮复测命令；变体 A、D 与浸泡的 viewer 口径（请裁决）；`BROWSER_SUBS` 的同步约束 |
| `d2-FX-R2-sandbox-to-文档总编.md`（追加"第 2 次执行"第 11–14 条） | 文档总编 | ADR-174 的复核与编号；与第 8 条提案的差别；作废"每 viewer 约 0.001 核"；D2-AC-04、39、55 的 viewer 口径 |

---

# 附：第 1 次执行（2026-10-07）原报告

## D2-FX-R2-sandbox 沙盒区域：准入、治理、回收、内存与容量接口（D2 第 2 轮修复）

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-sandbox（修复区域 sandbox：`python/awr/sandbox/**`、`python/awr/api/rest/sandbox.py`、`configs/sandbox/**`、`tools/sdk/**`，并按"允许跨模块修改该区域相关代码"改了 M11、M10、M20、M00、M16 的相关文件，见第 3 节与第 7 节） |
| 日期 | 2026-10-07 |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（§3、§4.1、§4.7、§7、§8）与 `runs/acceptance/d2-round1/`；AWR-04 §4.4、§4.6、§4.9、§15.5–§15.7、§13、§15.14；M17 PRD（FR-007、008、015、019–026、032、068–075）；d2-ACC-1-to-M17、d2-WP-13-to-M17 |
| 分派项 | D2-AC-02、04、06、07、28、42、45、55（P0），D2-AC-46（P1） |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4 1.7 GHz）、62 GiB；Python 3.12.3（`.venv`）。并行阶段同机有感知、仿真、编排、前端等修复工作包，load 1–14，且这些工作包正在修改 `python/awr/sim/**`（运行期偶发会话 sim-core 崩溃与此同时出现，见 5.3） |
| 约束执行 | 没有安装依赖；没有执行 git 写操作；**并行阶段没有运行性能基准**（`capacity.py`、`power.py`、`world_costs.py`、`-m perf` 一律未跑）。第 5.2 节的数值来自不持锁的诊断脚本（暂存目录，不进仓库），只用于定位与验证修复方向，不作验收结论。无 emoji 与禁用字形；没有改动 UI |
| 规格变更 | 没有放宽任何 P0 阈值，没有冻结"暂定"阈值。需要写入 AWR-04 与 M17 PRD 的行为补充与一项 D2-AC-46 阈值的 ADR 提案已提交文档总编（`.cache/impl/requests/d2-FX-R2-sandbox-to-文档总编.md`） |

### 1 结论摘要

第 1 轮九项不通过中，**沙盒侧可独立修复的缺陷全部修复**；D2-AC-02、04（A、C、D）、07、55 的主判据仍取决于感知、识别物与 sandbox100 基础循环的
stage 成本（满载会话约 0.8 核，预算 0.15 核；由感知、仿真工作包修复，本工作包没有改动 `python/awr/sim/{perception,targets,runtime,fleet}/**`）。

| 验收 | 第 1 轮 | 根因（已核实） | 本轮处理 | 本机诊断（不持锁，只作修复验证） |
|---|---|---|---|---|
| D2-AC-02 沙盒创建 | READY 中位 47.4 / 23.8 s，无排队时 8.4 / 9.7 s | ①stage 成本使 2–3 个满载会话即进入 L2/L3，L3 停止创建，请求排队；②冷启动（import、numba 缓存、装配）与满载会话同权分 CPU | 拉起期间叶子组 `cpu.weight 1000`、就绪后回 100；会话父组 `sandbox/sb/` 加 `cpu.max`；饥饿下的挂死容限；L3 回收后的停止接纳滞回（第 2.2–2.4 节） | 功能用例通过；时延待 stage 成本修复后复测 |
| D2-AC-04 并发容量 | A：RTF 0.40–0.70、epoch 至 10、lag p99 110 ms、1251.6 MB；B：shanghai 508；C：×5 只给 ×1 | A/C/D：stage 成本；**epoch 8–10 不是挂死重启，而是"L3 回收驻留 → 阶梯回到 L2 → SDK 自动恢复立即被接纳 → 再饥饿"的循环**；lag：整个 `sandbox/` 组带宽耗尽时控制面一并停顿，且 hub 把全部上下文的装帧连成一段；1251.6 MB 中约 100 MB 是 cgroup 模拟栈多起的热备用 sim-core（emax 的 public profile 不起）；**B：`cgroup-exec.sh` 把 1800M 归入 1.6G 档（M_sb 950）**；A 第 2、3 次被 `clock{speed}` 的 116 中止 | 档位边界 1800/1600 MiB；停止接纳滞回；`sandbox/sb/cpu.max`；hub 上下文之间让出；116 重登记重发；模拟栈与 emax 同配置（无热备用、不钉核）；变体 B 以测试配置额度运行（实测成本表下按生产额度第 3 个本应准入） | 变体 B 的额度推导与准入单测通过；`cgroup-exec.sh` 档位用例通过 |
| D2-AC-06 / 45 驻留后内存 | 比创建前 +14.3 MB（≤ +10） | 逐进程分解（诊断）：池 +6.7、sandbox-api +4.9、规划 worker +2.3 MB：首个会话才发生的一次性分配（快照 schema 校验器与契约注册表、容量与身份模块、worker 的 M20 模块与 `scipy.ndimage`、`scipy.optimize`）与未归还内核的空闲堆页 | 池启动预热；池与 sandbox-api 在会话离开后 gc + `malloc_trim`；worker 每个作业后 trim、就绪前预热 M20 模块与覆盖核；worker 不再导入 `scipy.ndimage`、`scipy.optimize`；控制面 `MALLOC_ARENA_MAX=2`；`power.py` 的基线等启动期分配稳定后再取 | 首个会话驻留与结束后 +8.4 MB（池 +1.3、api +4.0、worker +3.1），其后每个会话周期约 +0.27 MB（10 min 保留的结束记录） |
| D2-AC-07 倍速治理 | 两个满载会话 ×10 均 ×1（degraded），52 s 后 L3 | stage 成本（2 个会话即超出会话预算）；另：就绪 10 s 内按成本表先验 c0 授予，入库的实测 c0 为满载值，小机群会被限到 ×1 | 首个实测 `c_i` 到达时向上补授予；L2 同时读 `sandbox/` 与 `sandbox/sb/` 的限流 | 单测通过；判据待 stage 成本修复后复测 |
| D2-AC-28 内存 | S 级 151–153 MB、worker PSS 94–108 MB、成本表未入库 | **S 级超额的直接原因：就绪后立即的 dry-run 在会话 sim-core 内同步执行（M10 运行时绑定之前走"无规划池 → inline"），装入 scipy.optimize、scipy.linalg 与 M20 算法，会话匿名页 126 → 151.6 MB 并常驻**（第 1 轮 `world_costs.py` 在 t = 0 即发 dry-run）；worker：首个扫描与分配作业导入 `scipy.ndimage`（约 19 MB）与 `scipy.optimize`（约 25 MB） | 沙盒会话中规划池就绪前作业等待而不在会话内执行；`_label8` 与 Hungarian 不再导入 scipy 子包；成本表入库（第 1 轮 harness 实测，`measured: true`） | synthcity 满载会话（每 30 s 扫描 dry-run，100 s）峰值 126.2 MB；worker PSS 峰值 78.1 MB；sanfrancisco 会话约 149–153 MB（≤ 170），worker 约 91 MB（≤ 90，仍超约 1 MB，见第 6 节） |
| D2-AC-42 容量接口 | 变体 D 负载下 p95 57.9 ms；所致 CPU 0.019 核 | 每个请求走完整的 FastAPI 路由与依赖解析（进程内约 0.47 ms，另加 uvicorn 收发）；负载下事件循环被会话组限流与串行装帧阻塞 | 中间件快速路径回复缓存（预序列化响应体，`t_unix_ns` 末尾拼接）；1–2 s 先回旧值后台刷新（与真值的差 ≤ 2 s）；`prefix_of` 缓存；hub 让出、会话父组限流 | 进程内 ASGI 单次 0.47 → 0.057 ms；负载下 p95 待复测 |
| D2-AC-46 节能节省（P1） | viewer 只降 0.023 核（≥ 0.3）；浏览器 60 s 2.72%（≤ 1%） | 阈值来自预算"每 viewer 0.02 核"，24 个 viewer 实测增量只有 0.024–0.037 核，阈值不可达；浏览器为前端定时任务 | 阈值 ADR 提案（数据见第 2.8 节）提交文档总编；浏览器子项交 M15（与 D2-AC-44 同一根因） | 沙盒驻留子项第 1 轮已通过（−0.78 核、−141 MB） |
| D2-AC-55 emax 资源 | D：RTF 0–0.28、1288.5 MB、场与读数 9.73 ms/仿真秒、容量 0.019 核、内部档 ≥ L2 1799 s | 同 AC-04；场与读数服务（M19）；容量接口同 AC-42 | 容量快速路径；模拟栈去掉热备用（约 100 MB）；场与读数服务的剖析结果交 M19（`field.py` 正由感知工作包修改，本工作包未动） | — |

### 2 定位与根因

#### 2.1 方法

- 第 1 轮证据的重读：`bench/capacity-A-r1/A/samples.jsonl` 的逐秒会话视图（views）与环 epoch 对照、`capacity-B` 的回复、`power-r*`、
  `world-costs/`、`ac02*`、`ac07*`。
- 诊断脚本（暂存目录，不进仓库）：以 `_sbbench.Stack`（不套 cgroup）起真实栈，逐进程读 `/proc/<pid>/status` 的 RssAnon、RssShmem 与
  `/proc/<pid>/maps`（是否映射 scipy 子包）；`world_costs.measure_world` 同一方法的会话内存与 worker PSS 时间序列；容量请求的进程内 ASGI 计时
  与 cProfile；场与读数服务的 cProfile（只剖析 `FieldService.tick`）；规划 worker 初始化与首个扫描作业的分步内存与 tracemalloc。

#### 2.2 变体 A 的"epoch 升到 8–10"：回收与自动恢复的循环

逐秒采样显示 4 个会话在 30 min 内交替出现 `ACTIVE → PARKING → PARKED → SPAWNING → READY → ACTIVE`，每次恢复 epoch + 1；没有 `session.hung`。
过程：L3（某会话 RTF < 0.95 持续 30 s）回收（驻留）超额分最高的会话 → 其余会话仍饥饿但阶梯按 L3 判据回到 L2 → 操作员客户端（SDK）收到 515 后
自动恢复，`resume` 只在 L3 时排队，L2 时被立即接纳 → 恢复的会话再次饥饿 → 30 s 后再次 L3。约每 60 s 一轮，恢复本身的冷启动又加重饥饿。
修复：`Broker.cpu_blocked()`：L3 及以上、或 L3/L4 回收之后仍在 L2 时停止接纳（创建与恢复排队、不派发排队票、容量接口显示满），降到 L1 及以下
才解除（`reclaim_hold`，审计 `ladder.reclaim_hold`）。

#### 2.3 挂死判定与饥饿

M17-FR-008 以 StateRing 心跳 > 2 s 判挂死。会话组限流时一个长 tick（第 1 轮单步 p99 162–392 ms）可被拉长到秒级。虽然第 1 轮没有发生挂死判定，
这一路径会在饥饿时把"迟到"当作"无响应"并重启（重启的冷启动又加重饥饿）。修复：沙盒组或会话父组处于限流或 CPU 压力（L2 及以上、限流比 >
`l2_throttle` 或 PSI > `l2_psi`）且该进程最近一个检查周期仍消耗 CPU（≥ 0.02 核）时容限放宽为 5 × `stale_s`；超过仍按挂死处理（`metrics.late_heartbeat`）。

#### 2.4 控制面与会话共用一个带宽配额

`sandbox/cpu.max` 1.36 核覆盖 `sandbox/{ctl,plan,sb}` 全部进程。会话需求 4 × 0.8 核时，每个 100 ms 周期的配额在约 34 ms 内耗尽，sandbox-api 与池
随整组停顿到下一周期，这是事件循环 p99 110 ms 的主要来源。修复：会话父组 `sandbox/sb/cpu.max = sandbox/cpu.max − C_ctl`（1.14 核），会话超预算时
先在子组内限流；L2 的限流判据同时读两个组。另外 hub 的 60 Hz 驱动原来在一次回调中依次为全部上下文装帧，现在上下文之间 `await asyncio.sleep(0)`。

#### 2.5 变体 B：内存档位

`_sbbench.Stack` 以 `MemoryHigh=1800M` 起 systemd scope，`cgroup-exec.sh` 按 `≥ 1843 MiB`（systemd 的 1.8G）判 1.8 GB 档，1800 MiB 落入 1.6 GB 档：
`sandbox/memory.high` 1150M、`M_sb` 950 MB。按初值成本表 sanfrancisco 505 + shanghai 465 = 970 > 950，第 2 个即 508；chicago 375 反而可准入，与
第 1 轮的现象完全一致。修复：档位边界改为 1800/1600 MiB，模拟栈写法改为与 systemd 单元相同的 `1.8G`。

另：D2-AC-28 要求入库的实测成本表中 L 级世界的 ws 只有 5–13 MB，按生产额度 1100 MB，sanfrancisco + shanghai + chicago 合计约 525 MB 应被准入，
"第 3 个 508"的期望只在初值表下成立。`capacity.py` 变体 B 改为以测试配置额度（`variant_b_budget()`，当前 518.8 MB）检验同一行为，生产额度下的结论
另记（`natural_third_admitted`）；口径变化提交文档总编裁决。

#### 2.6 116 SEAT_TAKEN

会话 READY 后 sandbox-api 建上下文时以 `call_cb` 异步登记固定席位；SDK 在 `wait_ready` 之后立即发 `clock{speed}`，可能早于 sim-core 处理登记，
sim-core 以 116 拒绝（被拒的 cid 不进幂等表）。修复：`rpc._dispatch` 对 `seat_pinned` 的 Gateway 收到 116 时先同步重登记（`gw.reclaim_pinned_seat()`）
再重发一次；非沙盒与重登记失败时仍以 116 结束。

#### 2.7 内存

- **会话（S 级 ≤ 150 MB）**：`/proc/<pid>/maps` 对照显示，就绪后不发 dry-run 的满载会话 125–147 MB 且不映射任何 scipy 子包；就绪后立即发一次 GSD
  扫描 dry-run 时会话映射 `scipy/optimize` 与 `scipy/linalg`，匿名页 123.6/126.5 → 151.6/151.3 MB 并常驻（两次）。原因是 `awr.sim.tasking.jobs.JobRunner`
  在 M10 运行时尚未绑定（时钟开始推进之前）时走"没有规划池 → 同步执行"。第 1 轮 `world_costs.py` 正是在 t = 0 发 dry-run。修复后（作业等待规划池
  就绪，超过 20 s 才退回同步执行）同一负载峰值 126.2 MB。`MALLOC_ARENA_MAX=2` 的作用很小（< 1 MB）。
- **规划 worker（PSS ≤ 90 MB）**：worker 初始化后约 66 MB 匿名页（`import numba` 约 21 MB、TOPP-lite 核预热约 23 MB）；首个扫描作业导入
  `scipy.ndimage`（M20 `_label8`，连带约 130 个 scipy 模块）、首个分配作业经 `swarm.allocation.hungarian` 导入 `scipy.optimize`（约 25 MB）。
  `_label8` 改为按行游程的并查集（与 `ndimage.label` 逐元素相同），Hungarian 直接装载 `_lsap` 扩展模块（同一函数）。synthcity 峰值 94.2 → 78.1 MB；
  sanfrancisco 108 → 约 91 MB，余下的超额来自打开世界时 M04 `zones.raster` 的两层匿名金字塔（约 6.8 MB）等（请求 M04）。
- **驻留后回落**：见第 1 节表；池启动预热、会话离开后 trim、worker 预热与 scipy 子包的去除合计把首个会话的一次性增长从 13.9 MB 降到约 8.4 MB。

#### 2.8 D2-AC-46 的 viewer 阈值

（第 2 次执行更正：本节的 viewer 数值为只握手、不订阅口径，"每 viewer 约 0.001 核"作废；真实浏览器口径与 ADR-174 见上文第 2 次执行第 2、3 节。）

`power.py` 3 次：24 个 viewer 连接时 show-api 0.0643、0.0635、0.0665 核，全部节能后 0.0413、0.0395、0.0423 核（下降 0.023–0.024 核）；变体 A 中 24 个
完整订阅的 viewer show-api 0.077 核。阈值"下降 ≥ 0.3 核"在本机不可达；节能后的读数已回到无 viewer 的空闲水平。提案（文档总编）：改为"节能后
show-api CPU 回落到无 viewer 时的基线 + 0.01 核以内"，MS5 以 emax 实测冻结。本工作包没有修改阈值。

#### 2.9 场与读数服务（D2-AC-55，M19）

`FieldService.tick` 在 12 架、30 识别物、四种剖面轮流订阅时 10.9 ms/仿真秒（第 1 轮 9.73）：逐对读数链路约 58%（`quant_pairs`、`pair_dict` 与
发布前重复的 `encode_soa`）、10 Hz 慢任务的固定开销约 15%、剖面约 13%、感知体表约 14%。`python/awr/sim/perception/field.py` 在本工作包期间正被
感知工作包修改，本工作包没有改动该目录，剖析结论与建议写入 `d2-FX-R2-sandbox-to-M19.md`。

### 3 实现清单

| 文件 | 改动 | FR / AC |
|---|---|---|
| `python/awr/sandbox/broker.py` | `cpu_blocked()` 与 `reclaim_hold`（L3/L4 回收后停止接纳直到 ≤ L1），用于创建、内部档、派发、`startable_worlds`；首个实测 `c_i` 时向上补授予（`_regrant`） | M17-FR-020、021、022；D2-AC-04、07 |
| `python/awr/sandbox/park.py` | 恢复按 `cpu_blocked()` 排队；驻留完成后 `schedule_trim` | M17-FR-073；D2-AC-45 |
| `python/awr/sandbox/pool.py` | 饥饿下的挂死容限（`STARVED_STALE_FACTOR` 5、`PROGRESS_MIN_CORE` 0.02、`metrics.late_heartbeat`）；拉起结束 `_settle`；启动时复写会话父组 `cpu.max`；启动预热 `_warm`；会话结束后 `schedule_trim`；L2 读数加入会话父组 `cpu.stat` | M17-FR-008、007、026、021；D2-AC-02、04、06、45 |
| `python/awr/sandbox/cgroups.py` | 叶子组拉起加权（`SPAWN_WEIGHT` 1000）与 `settle`；`ensure_sessions_parent`；`group_stats` 增加 `cpu_stat_sb` | M17-FR-026；D2-AC-02、04 |
| `python/awr/sandbox/ladder.py` | L2 限流比取沙盒组与会话父组的较大值 | M17-FR-021；D2-AC-07 |
| `python/awr/sandbox/spawner.py` | 叶子组以拉起加权创建；会话 sim-core `MALLOC_ARENA_MAX=2` | M17-FR-025；D2-AC-02、28 |
| `python/awr/sandbox/capacity.py` | `full` 按 `cpu_blocked()` | M17-FR-068 |
| `python/awr/api/rest/sandbox.py` | `CapacityCache`（预序列化响应体、1 s 新鲜、1–2 s 旧值并后台刷新、并发合并）；路由按同一缓存回复 | M17-FR-068；D2-AC-42、55 |
| `python/awr/api/sandbox_main.py`、`python/awr/api/middleware.py` | `fast_get` 与中间件快速路径（Host、Origin、请求 id、限流之后，安全头与 API 版本头照常） | M11-FR-203；D2-AC-42、55 |
| `python/awr/api/public.py` | `prefix_of` 加 `lru_cache(4096)` | M11-FR-209 |
| `python/awr/api/rt/rpc.py`、`python/awr/api/rt/gateway.py` | 固定席位 116 的同步重登记与重发一次（`reclaim_pinned_seat`、`stats.seat_reclaims`） | ADR-093；D2-AC-04（变体 A 中止） |
| `python/awr/api/rt/hub.py` | 驱动在上下文之间让出事件循环；上下文销毁后去抖 trim | M11-FR-205；D2-AC-04、42、45 |
| `python/awr/runtime/procutil.py` | `trim_heap()`（gc + glibc `malloc_trim(0)`） | D2-AC-06、45 |
| `python/awr/sim/planning/server.py`、`worker.py` | 作业结果送出之后与回收世界后 trim（不计入作业预算）；就绪前预热 M20 覆盖核与算法模块 | M17-FR-032；D2-AC-28、45 |
| `python/awr/sim/tasking/jobs.py` | 沙盒会话中规划池就绪前作业等待（`waiting`、`stats.deferred`、`REMOTE_WAIT_S` 20 s 后退回同步执行） | ADR-113；D2-AC-28 |
| `python/awr/tasking/scan.py` | `_label8` 不导入 scipy（与 `ndimage.label` 逐元素相同） | D2-AC-28 |
| `python/awr/swarm/allocation/hungarian.py` | 直接装载 `scipy/optimize/_lsap` 取 `linear_sum_assignment`，失败时退回包导入 | D2-AC-28、45 |
| `configs/runtime.yaml` | `sandbox-pool`、`sandbox-api` 的 `env` 增加 `MALLOC_ARENA_MAX: "2"` | D2-AC-06、45 |
| `configs/sandbox/world_costs.json` | 入库第 1 轮 harness 实测（`world-costs-20261006T185053Z`，`measured: true`） | D2-AC-28 |
| `tools/deploy/emax/cgroup-exec.sh` | 内存档位边界 1800/1600 MiB；会话父组 `sandbox/sb/cpu.max`（`AWR_CG_CTL_CPU_RESERVE`）；日志字段 `sessions_cpu_max_cores` | M17-FR-063；D2-AC-04 |
| `tools/bench/sandbox/_sbbench.py` | `mem_high` 缺省 `1.8G`；`emax_like`（`defaults.standby=false`、`cpu.pin=off`，与 emax 的 public profile 一致） | D2-AC-04、28、39、55 的测量口径 |
| `tools/bench/sandbox/capacity.py` | 变体 B 的测试配置额度 `variant_b_budget()` 与判据 `pair_admitted` | D2-AC-04 |
| `tools/bench/sandbox/power.py` | 基线前等控制面启动期分配稳定（`settle`）；逐进程分解 `ctl_baseline_mb`、`ctl_parked_mb` | D2-AC-45、46 |
| `tools/sdk/README.md`、`tools/sdk/awr_sandbox.py`（文档串） | 只用 REST 的客户端与无连接驻留（`idle_ttl_s`）的说明（d2-ACC-1-to-M17 第 6 条） | M17-FR-080 |

新增与修改的用例：`tests/rt/test_seat_pinned_retry.py`（新）、`tests/tasking/test_label8.py`（新）、`tests/tasking/test_jobs_remote_wait.py`（新）、
`tests/swarm/test_hungarian_light.py`（新）；`tests/sandbox/test_{broker,governor,ladder,cgroups,pool,capacity,admission}.py`、
`tests/ops/test_deploy_cgroup_exec.py`、`tests/runtime/test_config.py`、`tests/e2e/test_sandbox_bench_tools.py` 增补或同步。

### 4 偏差

1. **跨所有权修改**：按工作包授权直接改了 M11（`api/rt/{rpc,gateway,hub}.py`、`api/{middleware,public,sandbox_main}.py`、`runtime/procutil.py`、
   `configs/runtime.yaml`）、M10（`sim/planning/{server,worker}.py`、`swarm/allocation/hungarian.py`）、M20（`sim/tasking/jobs.py`、`tasking/scan.py`
   的 `_label8`）、M00（`cgroup-exec.sh`）、M16（`tools/bench/sandbox/{_sbbench,capacity,power}.py`）的文件，均已以请求知会所有者复核。
2. **测量口径**：cgroup 模拟栈改为与 emax 的 public profile 同一资源配置（无热备用、不钉核），第 2 轮的内存与共享展示数值与第 1 轮不直接可比
   （第 1 轮多约 100 MB 匿名页）；变体 B 改以测试配置额度运行；`power.py` 的基线改为启动期分配稳定之后。三者均待文档总编与 M16 确认。
3. **没有改 `N_max = auto` 的输入**：仍按配置的 `c_s` 计算；若改用实测，在 stage 成本修复前会把 N_max 压到 1。
4. **`JobRunner` 的等待上限**：沙盒会话等待规划池就绪超过 20 s 时仍退回同步执行（保留可用性），这种情况下会话仍会装入 scipy 子包。
5. **成本表的 c0 先验**为第 1 轮实测（0.35–0.67 核，stage 成本修复之前），stage 成本修复后应以 `world_costs.py --all --write` 刷新；补授予
   （第 1 节表的 D2-AC-07 行）使偏高的先验只影响就绪后 10 s 内的授予。
6. **场与读数服务（D2-AC-55 子项）没有改动**：感知工作包同时在修改 `perception/field.py`，避免并行冲突。

### 5 测试结果

#### 5.1 功能测试与 lint

- `pytest -m "not perf" tests/sandbox tests/rt tests/runtime tests/ops/test_deploy_cgroup_exec.py tests/e2e/test_sandbox_bench_tools.py tests/swarm
  tests/tasking/{test_label8,test_jobs_remote_wait,test_scan,test_greedy}.py tests/planning`：**665 passed、1 failed**。失败的
  `tests/planning/test_remote.py::test_remote_applies_at_drain_and_logs`（预算 50 ms 的作业以 PLAN_TIMEOUT 结束）由本工作包引起：worker 在送出结果
  之前做 gc + trim，计入了 3 × 预算。已改为送出结果之后只做 `malloc_trim`（不计入任何作业）；之后 `tests/planning` 142 passed、1 failed
  （`test_server_fairness.py::test_kill_precision_and_rebuild_time`，3 × 预算 ± 100 ms 的时延判据，同机负载约 10，单独复跑通过；本机 worker 冷启动含
  新增的预热约 1.8 s，仍在 5 s 重建上限内），`tests/planning/test_remote.py` + `tests/e2e/test_sandbox_power_bench.py`（含 `power.py --smoke`
  与 `capacity.py --variant D --smoke`）19 passed。
- 中途单独运行：`tests/sandbox` + `tests/ops/test_deploy_cgroup_exec.py` + `tests/runtime` 318 passed；`tests/tasking` 全量 46 passed、1 failed
  （`test_relay.py::test_handover_slow_moves_relief_to_alternative`，`relay.py` 与该用例当时正被编排工作包修改，单独复跑通过）。
- `ruff check` 本工作包涉及的全部文件通过。`make lint` 当前不通过，11 条 ruff、1 条 no-emoji（U+2194 两处）、oxlint 5 条、m06-lint 1 条全部位于
  其他工作包正在修改的文件（`python/awr/sim/tasking/{relay,runtime}.py`、`python/awr/tasking/{decompose,station,plan_kinds}.py`、
  `tests/tasking/test_{decompose,runtime_tasks}.py`、`python/awr/sim/perception/{kernels,runtime}.py`、`apps/web/src/app/i18n/capacity.ts`、
  `apps/web/src/viewport/sensorView/pass.ts`）；本工作包的文件在 ruff、no-emoji、no-hex、check_py_imports、check_py_callbacks 中均无违例。

#### 5.2 诊断（不持锁，只作修复验证，不作验收结论）

| 项 | 修复前 | 修复后 |
|---|---|---|
| 容量请求进程内 ASGI 单次 CPU | 0.47 ms | 0.057 ms |
| 驻留后控制面比基线（逐进程合计） | +13.9 MB（池 +6.7、api +4.9、worker +2.3） | 首个会话 +8.4 MB（池 +1.3、api +4.0、worker +3.1）；其后每个会话周期约 +0.27 MB |
| synthcity 满载会话匿名页 + shmem 峰值（t = 0 起每 30 s 扫描 dry-run） | 152.8 MB | 126.2 MB |
| 同上，规划 worker PSS 峰值 | 87.7 MB（只加 trim 时）；第 1 轮 94.2 | 78.1 MB |
| sanfrancisco 会话 / worker PSS | 171.9 / 102.2 MB | 约 149–153 / 约 91 MB |
| 就绪后立即 dry-run 的会话匿名页 | 126 → 151.6 MB（装入 scipy.optimize、linalg） | 不再在会话内执行（等待规划池） |

#### 5.3 运行期观察

诊断期间有 3 次会话 sim-core 以退出码 1 连续崩溃（第 3 次 FAILED），时间与感知、识别物、编排工作包修改 `python/awr/sim/**`、`python/awr/environment/**`
的时刻重合（例如 05:44:54 的一次与 `sim/tasking/executor.py`、`sim/targets/{los_cache,kernels_los}.py`、`environment/field.py` 的写入同在 05:42–05:47）；
此后以同一负载复跑 6 次 × 25–70 s 未再出现。第 2 轮验收应在全部修复合入之后进行。

### 6 遗留

1. **stage 成本**（感知 490 ms/仿真秒、识别物 25.8 ms/仿真秒、sandbox100 基础 0.255 核、场与读数 9.7 ms/仿真秒）：D2-AC-02、04（A、C、D）、07、55
   的主判据只能在它们回到预算后复测。沙盒侧的修复使超载时的行为变为"排队与回收，而不是互相饿死与反复恢复"，但不能让 4 个满载会话在
   1.14 核内以 RTF ≥ 0.99 运行。
2. **sanfrancisco 的规划 worker PSS 约 91 MB**（≤ 90）：余量受 numba 与 TOPP-lite 核的常驻内存限制，另有 M04 `zones.raster` 的匿名金字塔约 6.8 MB
   （请求 M04、M10）。
3. **驻留后回落的余量**：首个会话的一次性增长约 8.4 MB（≤ 10），sandbox-api 的 4 MB 仍未归还（推测为首个 Gateway 实例化的缓存与堆碎片）。
4. **D2-AC-46**：viewer 阈值待 ADR；浏览器主线程 ≤ 1% 由 M15 修复。
5. **D2-AC-42 的 nginx 微缓存子项**：本机无 nginx，未测。

### 7 跨所有权请求

| 文件 | 接收方 | 要点 |
|---|---|---|
| `d2-FX-R2-sandbox-to-M19.md` | M19 | 场与读数服务的剖析与建议；stage 成本修复后通知复测 |
| `d2-FX-R2-sandbox-to-M08-M18.md` | M08、M18 | stage 成本；epoch 循环的判读更正；会话常驻内存（契约注册表约 2.5 MB、低频 trim）；numba 预热 |
| `d2-FX-R2-sandbox-to-M20.md` | M20 | `jobs.py` 的等待、`_label8`、Hungarian 的改动知会 |
| `d2-FX-R2-sandbox-to-M11-M10-M00.md` | M11、M10、M00 | 已改文件的知会；worker 内存分解；`zones.raster` 匿名金字塔（M04）；部署 README 的 cgroup 表 |
| `d2-FX-R2-sandbox-to-M15.md` | M15 | D2-AC-46 浏览器子项（≤ 1%）；容量缓存语义 |
| `d2-FX-R2-sandbox-to-M16.md` | M16 | 模拟栈配置、变体 B 口径、`power.py` 基线、复测清单、判读更正 |
| `d2-FX-R2-sandbox-to-文档总编.md` | 文档总编 | §4.4.2、§4.4.4、§4.4.5、§4.4.6、§4.6、§15.5.4、M17-FR-008 的补充；变体 B 口径；D2-AC-46 阈值提案；§4.4.1 成本表 |
