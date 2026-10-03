# ACC-4 工作包报告（D1 验收测量，第 4 轮）

| 项 | 内容 |
|---|---|
| 工作包 | ACC-4：复测 D1-AC-03b、04、07、08、09a、16、17、19、23、25、26、27、28、29，回归 P0 的 D1-AC-02、03a、06、11a、15、32、34；其余 17 条沿用第 3 轮结果；只测量与诊断，不修复被测功能 |
| 日期 | 2026-10-03（01:00 至 06:50） |
| 主报告 | [D1 验收报告（第 4 轮）](D1-验收报告-第4轮.md)：D1-AC-01 至 35 逐条状态、实测值、阈值、环境与 load、证据路径、诊断与归属区域、冻结建议 |
| 性能报告 | `runs/acceptance/round4/report.json`（`awr.perf.report.v1`，26 个用例）、`runs/acceptance/round4/report.html`（M16 生成器，lieflat 版式，`check-report` 通过） |
| 约束执行 | 性能用例全部经 M16 harness，按 ADR-033 / 18 §3 执行（排他锁、每次运行前 load ≤ 4、3 次中位、运行内负载采样；帧节奏类用例运行内最大 load 全部 ≤ 9.44）；本轮补齐 PR-6 分区（SwiftShader 线程守护、`fleet-ladder.concurrent` 钉核）；诊断同样持排他锁、开跑前 load ≤ 4；交付状态 numba 缓存（清空后只经 `make numba-warm`）；没有安装依赖；没有执行 git 写操作；被测功能没有修改；没有新增 ADR；`make lint` 通过 |

## 1 结果概要

38 条（03、09、11 各分 a、b）中通过 31 条、不通过 7 条、没有未测项（第 3 轮通过 24、不通过 14）。含 P0 的 25 条通过 20 条，按 P0 子项计 21/25 通过（27 只因 P1 的
link_drop 单步最大不通过）；只含 P1 的 13 条通过 11 条。P0 子项不通过的是 04（整景进入目标带 2.42 s）、07（单步 p99 3.051 ms）、19（天气过渡 > 100 ms 1.10%）、
25（首次操作后最大间隔 183 ms），D1 退出条件不满足；P1 不通过的是 27 link_drop（单步最大 32.6 ms）、28（单步 p99 4.13 ms）、29（JS 堆增长 26.2%）。7 条 P0 回归项全部通过。

本轮转为通过的 7 条：03b（最大间隔 317 → 117 ms）、08（tick 年龄 p99 16.66 → 9.48 ms）、09a（467 → 200 ms）、16（开局屏障）、17（任务编辑）、23（+3.00 → +0.54 个百分点）、
26（t_sim 到像素 200 → 60 ms、credit_skips 4.44 → 0.67%）。

新发现：①性能运行协议 PR-6 的两处缺口（web harness 中 SwiftShader 工作线程落在 core0–4；`fleet-ladder.concurrent` 不钉核），前三轮都在此状态下测得，本轮修正后测量，
关守护对照显示守护对整景帧节奏的影响在噪声内；②个别运行中点云整段不绘制（pass 计划不含点云，n200 的 9 次运行中 4 次，与守护无关），是 P0 级的显示缺陷，也使
D1-AC-09a 的一次运行无效；③D1-AC-29 的堆增长主要来自 GC 锯齿幅度，GC 后的底只升 5.9%。详见主报告第 1、4 节。

## 2 改动文件

| 文件 | 改动 |
|---|---|
| `apps/web/perf/harness/exec/affinity.mjs`（新增） | PR-6 亲和性守护（Playwright 进程树中越出 2–6 的线程改回，计数写 `affinity.json`；`AWR_PERF_AFFINITY_GUARD=0` 只用于诊断对照） |
| `apps/web/perf/harness/exec/pw.mjs`、`apps/web/perf/harness/exec/common.mjs` | `runLogged` 的 `onSpawn` 回调；`pw` 执行器启动与停止守护 |
| `apps/web/perf/harness/cases/backend.mjs` | `fleet-ladder.concurrent` 去掉 `pin: 'none'`，与 `gw-3clients` 一样由 harness 以 `taskset -c 2-6` 包裹 |
| `apps/web/perf/thresholds.json` | 固定层单层预算与冻结状态对齐 ADR-071（trails 1.5、groundSky 6 ms，没有放宽任何阈值） |
| `docs/18-性能与测试方案.md`（PR-6）、`docs/modules/M16-演示数据剧本与流畅性测试PRD.md`（M16-FR-042、§6.7.4、§6.15） | 同步上述实现；PR-6 规格本身不变 |
| `docs/impl/D1-验收报告-第4轮.md`、本报告 | 新增 |

## 3 仓库外的状态

- `~/.cache/awr/numba`：本轮开始时清空（原目录改名为 `~/.cache/awr/numba.pre-acc4` 保留，可删除），只经 `make numba-warm` 预热；性能用例前后文件集合不变（118 个文件）。
- 测试构建在 `.cache/acc4/dist-test/`；驱动、清单、汇总与诊断脚本在 `.cache/acc4/`；功能测试的独立 numba 缓存在 `.cache/acc4/numba-func/`。
- `runs/perf/p20261003-010551-acc4` 是指向 `runs/acceptance/round4/p20261003-010551-acc4` 的符号链接；诊断对照在 `runs/perf/p20261003-010551-acc4-noguard/`、
  `runs/perf/p20261003-010551-acc4-diag/`（副本在 `runs/acceptance/round4/supp/`）。
- 本机遗留进程没有清理：修复阶段留下的 `tools/fake/fake_gw.py --port 42625`，以及 80 个孤儿 plan-pool 工作进程与 resource_tracker（全部早于本轮，本轮没有新增）。
