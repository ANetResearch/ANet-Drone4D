# ACC-1 工作包报告（D1 验收测量，第 1 轮）

| 项 | 内容 |
|---|---|
| 工作包 | ACC-1：D1-AC-01 至 D1-AC-35 全部测量（第 1 轮），只测量与诊断，不修复被测功能 |
| 日期 | 2026-10-01 |
| 主报告 | [D1 验收报告（第 1 轮）](D1-验收报告-第1轮.md)：逐条状态、实测值、阈值、环境与 load、证据路径、诊断与归属区域、冻结建议 |
| 性能报告 | `runs/acceptance/round1/report.json`（`awr.perf.report.v1`，50 个用例）、`runs/acceptance/round1/report.html`（M16 生成器，lieflat 版式，`check-report` 通过） |
| 约束执行 | 性能用例全部经 M16 harness，按 ADR-033 / 18 §3 执行（排他锁、开跑前 load ≤ 4、3 次中位、运行内负载采样）；没有安装依赖；没有使用 git；被测功能代码没有修改；`make lint` 在最后一次修改后通过 |

## 1 结果概要

38 条（03、09、11 各分 a、b）中通过 13 条、不通过 23 条、未测 2 条；含 P0 的 25 条中通过 10 条，D1 退出条件不满足。四个主要根因：
前端 Tier S 帧节奏（web-engine、web-ui）；sim-core 运行期 numba 编译被 2 s 活性阈值杀掉、冷缓存下崩溃循环（sim）；N = 500/1000 布设与
ladder n1000 剧本 setup 超过启动宽限或活性阈值（sim）；测量工具口径缺口（D1-AC-08、28 未测）。详见主报告第 1、4 节。

## 2 改动文件（工具与用例层的最小修复）

| 文件 | 所有者 | 修改摘要 |
|---|---|---|
| `apps/web/perf/harness/backend.mjs` | M16 | 不再签发 admin token（避免抢占操作员席位）；可选 `AWR_PERF_RUNTIME_CONFIG`（仅诊断） |
| `apps/web/perf/harness/runner.mjs` | M16 | 运行结束检查 api、sim-core 重启，记 PERF-E008 并补跑 |
| `apps/web/perf/harness/analyze.mjs` | M16 | R60 窗口字段与 bench_cmd 中位数的提取 |
| `apps/web/perf/harness/exec/py.mjs` | M16 | 容忍 `NaN` 的 bench JSON；附带 `bench-ipc.json` |
| `apps/web/perf/report/build-report.mjs` | M16 | 报告指标键转小写（符合 schema） |
| `apps/web/perf/quality.spec.ts` | M16 | 自带参考页静态服务；位姿格式转换 |
| `apps/web/perf/m05/switch.spec.ts` | M05 | 记录切换耗时并写入快照 |
| `tools/bench/fleet_ladder/run.py` | M08 | 可选 `AWR_BENCH_RUNTIME_CONFIG`（仅诊断） |

每处修改的原因见主报告第 6 节。规格（阈值、参数、协议）没有变化，没有新增 ADR；暂定阈值的冻结建议见主报告第 5 节，由修复阶段写 ADR。

## 3 仓库外的状态

- `~/.cache/awr/numba` 已补齐 S1、ladder、soak 路径的运行期签名（主报告 4.1）；修复阶段应在清空该目录后复测。
- 测试构建与画质参考页在 `.cache/acc1/dist-test/`；驱动、预热与诊断脚本在 `.cache/acc1/`。
- `runs/perf/p20261001-093000-acc1` 是指向 `runs/acceptance/round1/p20261001-093000-acc1` 的符号链接。
