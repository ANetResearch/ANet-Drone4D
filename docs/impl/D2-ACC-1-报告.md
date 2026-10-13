# D2-ACC-1 工作包报告（D2 验收测量，第 1 轮，2026-10-11 执行）

| 项 | 内容 |
|---|---|
| 工作包 | D2-ACC-1：D2-AC-01 至 D2-AC-55 全部测量（第 1 轮）；只测量与诊断，工具层最小修复 |
| 日期 | 2026-10-11 12:23 至 2026-10-12 06:35（UTC） |
| 主报告 | [D2 验收报告（第 1 轮）](D2-验收报告-第1轮.md)：逐条结论、实测值、证据路径、按根因的诊断、未测与缩短项、与上一版比较、复测清单 |
| 机器可读结果 | `runs/acceptance/d2-round1/d2-acceptance-round1.json`（55 条：`ac`、`priority`、`status`、`measured`、`area`、`diagnosis`、`evidence`） |
| harness 报告 | `runs/acceptance/d2-round1/p20261011-122944-d2acc1/report.json` 与 `report.html`（M16 生成器，`check-report` 通过） |
| 取代 | 本文与主报告取代 2026-10-07 的同名报告；上一版产物移到 `runs/acceptance/d2-round1/_prev-20261007/`（`runs/perf/p20261006-130452-d2acc1` 链接已随之更新） |
| 约束执行 | 性能、容量与带时延阈值的项目在排他锁 `runs/.perf.lock` 下运行，开跑前 load ≤ 4、运行内采样；cgroup 模拟为 systemd 用户 scope（CPUQuota 250%、MemoryHigh 1.8G、MemoryMax 2G）+ `taskset -c 0-3` + `cgroup-exec.sh`；没有修改被测功能代码；没有安装依赖；没有执行 git 写操作；没有 emoji 与禁用字形；口令只在测试运行时随机生成；`make lint` 在最后一次修改后通过 |

## 1 结果概要

55 条中**通过 37、不通过 15、未测 3**；P0 50 条中通过 34、不通过 14、未测 2；P1 5 条中通过 3、
不通过 1、未测 1。D2 退出标准不满足。不通过的 P0：01、02、04、07、08、16、19、20、26、30、34、35、45、55。主要根因（详见主报告第 1、4 节）：

1. 会话成本仍是容量模型的 2–3 倍（识别物 stage 真实组合 233 ms/仿真秒、感知 67、场与读数 10.2、基础循环 0.16 核）：D2-AC-02、04、07、08、55；
2. 接力与区域值守（霾时段与扰动后电池节律、白天视线间隙、T5 首次捕获与资源、驻留恢复丢失编排状态）：D2-AC-19、20、34、35、45；
3. 四类任务的两个执行缺陷（风下固定翼、区域巡查 + 巡边分离撞机）：D2-AC-16；
4. 前端与 D1 回归剩余项（沙盒整景帧节奏、TTFP、全机 RTL 单步最大）：D2-AC-01、26、30；以及 D2-AC-46（P1）的 viewer 节省阈值。

## 2 执行清单

| 类别 | 内容 | 验收 |
|---|---|---|
| 功能门禁 | 全量 `pytest -m "not perf"`（4262 passed、88 skipped、6 failed，全部在安静环境或单独运行中通过或查明为环境与用例隔离）、vitest unit 1374 与 browser 62、`make test-contracts`、七城 × T1–T6 180 s 模板批处理 42/42、功能 Playwright 组（FakeSource 界面、e2e、gpu-limits 三组） | 全部 |
| harness 用例（G4，`M16_PERF=1`，3 次） | `d2.boot-progress`、`d21.boot-live`、`d21.field-live`（2 轮）、`m06.sensor-views`、`m06.field-views`、`d21.power-live`、`d21.power-boot`、`d21.seats-live`、`d2.sandbox-live`、`d21.mobile-live`、`d21.identity-live`；D1 回归 `flight60` 五个、`layers`、`latency`、`storm.rtl`、`e2e.interaction`、`gw-3clients`、`ipc.cmd`、`fleet-ladder`、`e2e.scenarios`、`remote-smoke` | 01、21、26、40–44、47–53 |
| pytest 性能标记（3 次） | 感知、场、识别物、克隆校验、world_query、numba 预热 | 09、13、55 |
| 实时栈 e2e 与混沌（`M16_PERF=1`） | 驻留 8、身份 13、容量/观看/场采样 18、沙盒与驻留混沌 8 | 03、42–45、51、52 |
| 基准 | `fleet_ladder --clock sandbox100`（含与不含 D2 插件，各 3 次）、`world_costs.py --all`、`power.py` 3 次、`capacity.py` B/A/D/C、`soak.py` 6 h | 04、06、08、28、39、45、46、55 |
| 接力批处理 | AC-19 a/b/c 24 h、充电通道对照 24 h、AC-35 synthcity 24 h 与 sanfrancisco 2 h、确定性 1 h × 2、168 h 视界 dry-run、S7 3600 s、无霾对照 24 h（诊断） | 19、20、32、35、39 |
| 自建诊断（`.cache/d2acc1b/`） | 见主报告第 6 节 | 01、02、03、05、07、09、16、18、21、30、38、41、45、47、48 |

## 3 改动文件

| 文件 | 所有者 | 修改 | 原因 |
|---|---|---|---|
| `apps/web/perf/d21/field-live.spec.ts` | M16 | 第 3 例等待循环同时等机体锚点 ≥ 2（断言与超时不变） | 原条件只等识别物锚点即退出、随后断言机体锚点，首帧顺序不同即失败（工具竞争，非产品缺陷）。差异 `runs/acceptance/d2-round1/diag/field-live.spec.ts.toolfix.diff` |

其余为本工作包的测量脚本与驱动（`.cache/d2acc1b/`）、结果目录 `runs/acceptance/d2-round1/`、本报告与主报告、跨所有权请求，均不属被测代码。没有修改规格与阈值，
没有新增 ADR。

## 4 偏差

| # | 要求 | 实际 | 原因 |
|---|---|---|---|
| 1 | 容量变体 A、C、D 各 3 次取中位 | 各 1 次有效运行 | 首次运行即以数量级违反阈值，3 次不会改变判定；节省约 3 h 独占时间；会话成本修复后须补足 |
| 2 | D2-AC-39 ≥ 24 h 浸泡 | 6 h（跨北京时间 04:00 一次），按未测登记 | 本机独占时间；文档无缩短与外推口径，不外推 |
| 3 | D2-AC-20 7×24 h 批处理 | 未运行，判不通过 | 同配置 24 h 已不通过（确定性批处理），7 天结果不可能通过 |
| 4 | 功能 Playwright 组在安静环境运行 | 无计时阈值的 FakeSource、e2e、gpu-limits 组在功能阶段（负载 6–13）运行；带计时的实时栈用例全部在性能阶段 | 节省独占时间；负载下超时的 skeleton 与需测试构建的 e2e 用例在安静环境复跑 |
| 5 | 浸泡期间不跑其他项目 | 浸泡期间另有 2 个诊断批处理在核 6–7 以 `nice 10` 运行 | AWR-04 MS5 允许浸泡与其他用例并行；浸泡判据项全部满足，运行内负载均值 1.99 |
| 6 | D2-AC-42 nginx 微缓存 | 未测 | 本机无 nginx，不装新依赖 |
| 7 | D2-AC-27 emax | 未测 | 无 emax 访问 |
| 8 | D2-AC-30 用 flight60 变体 | 自建沙盒整景诊断（默认相机） | 没有登记的沙盒 flight60 变体 |

## 5 遗留

见主报告第 8 节的复测清单。本轮新发现或定位更清楚的问题：风下固定翼的弹射爬升与航线跟踪；区域巡查 + 巡边并行时的分离死锁与能量 RTL 撞机；霾时段接力与扰动后
电池节律（无霾对照通过）；收紧治理器下限后沙盒整景帧节奏；浸泡工具无法判定软重置；三个用例的隔离问题。

## 6 请求

`.cache/impl/requests/` 下：在 `d2-ACC-1-to-M20.md`、`-M21.md`、`-M18.md`、`-M19.md`、`-M08.md`、`-M17.md`、`-M11.md`、`-M06.md`、`-M16.md`、`-文档总编.md` 中追加
"第 2 次执行（2026-10-11）"小节，新建 `d2-ACC-1-to-M09.md`。各份要点见主报告第 9 节。
