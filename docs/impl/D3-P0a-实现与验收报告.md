# D3 P0a 实现与验收报告（RP0a）

| 项 | 内容 |
|---|---|
| 任务 | RP0a P0a 实现与验收报告（D3 收尾与封板，批次 B09a 之后；含 P-1 的 D3-AC-46、47 与完整回归门） |
| 被测提交 | `d3-dev` HEAD `c075fd716dceed448da71103151dcd156ce1eee4`（树 `653ac8e36175`，"feat(d3): M25 worker render process group, pass D3-AC-11 re-test"）；回归门与 I01 均取该提交的 `git archive` 快照，不取工作树 |
| 日期 | 2026-10-22（UTC）；测量时段 07:39–10:20 UTC |
| 依据 | AWR-05（docs/05）v1.6 §11.1、§11.2（阈值原文取自 `c075fd7`）；ADR-194（封板范围与规则）、ADR-195（独占卡口径）、§4.7；ADR-196 至 ADR-217；用户 2026-10-22 决定（ADR-194 至 217 与 C-01 至 C-52 已确认；D3-AC-18 采用"同量级、排序一致"；Tier B 设备项移 D3.x） |
| 范围 | P-1：D3-AC-46、47；P0a：D3-AC-01 至 10、17 至 19、34、51、56 至 58、60，共 21 条（阻塞级）。D3-AC-11 属 P0b，按指示采用 K08 独占卡结果，仅在 D3-AC-47 中引用 |
| 口径 | 性能类数字只采信 A 级独占卡或 CPU 池正式测量（ADR-195）；共享卡与共享主机数字逐项标注，P0a 允许共享卡原型（ADR-195 决策 (7)）；"通过"指有可重复的测试计数或文档化测量（ADR-194 决策 (3)） |
| 状态 | 判定为本任务的工程判定；"按 ADR 通过"所依 ADR 已由用户 2026-10-22 确认，D3-AC-18 的新 ADR 即 ADR-218，写入报告时已并入 `docs/05`（`82ce0ae`）。写入报告时 `d3-dev` HEAD 已前进到 `82ce0ae`（另一批次的文档并入提交），被测提交仍为 `c075fd7` |

## 0. 摘要

P-1 与 P0a 共 21 条验收（D3-AC-46、47；D3-AC-01 至 10、17 至 19、34、51、56 至 58、60）判定计数：通过 9；按 ADR 通过 8；发布就绪 1；不通过 3；移 D3.x 0。

| 编号 | 条款 | 判定 | 依据 |
|---|---|---|---|
| D3-AC-46 | D2 发布面文案复核 | 发布就绪 | 文案与 README 侧通过；`d2` 分支、`v0.2.2` 标签与上线复核未执行 |
| D3-AC-47 | P-1 实测报告 | 按 ADR 通过 | 16 项全部有实测，ADR-213 至 216 冻结；性能项取 A 级独占与 CPU 池 |
| D3-AC-01 | D2 零影响回归门 | 不通过 | 零影响（两档）、resim、imports、public-e2e 通过；pytest 步骤、perf、lint、demo-parity 四项未通过，均有已知原因 |
| D3-AC-02 | 世界包构建与标定 | 按 ADR 通过 | ADR-210（L5 调平）；两机 sha256 一致；构建 162 s |
| D3-AC-03 | NavMesh 与锚点 | 按 ADR 通过 | ADR-212 口径：可达 1000/1000，> 3 m 比例 2.68% |
| D3-AC-04 | 不完整即未知 | 通过 | 覆盖外 unknown 100%，`void` 与 `invalid_mask` 联动，EC 剔除 |
| D3-AC-05 | MeshWorldQuery | 通过 | 会话内存增量 +0.88 MB（≤ 20 MB），105 个 D2 用例重跑通过 |
| D3-AC-06 | 事件编译三道门 | 通过 | 三道门与 4 类关键事件，32 项测试通过 |
| D3-AC-07 | 事件磁带逐字节确定性（v1.6 按 ADR-197 修订） | 按 ADR 通过 | ADR-197：Python 生成加 TS 解码逐字节对拍，两机 `content_sha256` 一致 |
| D3-AC-08 | Exogenous 隔离 | 通过 | ≥ 5 种策略下脚本演员轨迹与磁带一致 |
| D3-AC-09 | 多机多视角观测 | 不通过 | HEAD 上 6 项探针用例失效（`ProbeL2._poses`），其余度量用例通过 |
| D3-AC-10 | 跨传感器时间一致（v1.6 按 ADR-198 修订） | 按 ADR 通过 | ADR-198：渲染与求交误差 ≤ 0.055 mm |
| D3-AC-17 | lockstep、确定性与决策时延（v1.3：正式成绩配置） | 通过 | B10 第 2 轮 60 对重复逐字节一致；同 ISA（C-41 (C)） |
| D3-AC-18 | 评测与四项指标分量（v1.3；v1.4 改称） | 按 ADR 通过 | 用户决定"同量级、排序一致"成立；AO_real 0.000、AO_pos 0.976；EC 3/6 行 ≤ 0.1（信息项） |
| D3-AC-19 | 录制与回放 | 通过 | 背压丢帧 0，回放差 ≤ 5 cm，同步回放最大差 0 ns |
| D3-AC-34 | Realistic 档锁步（v1.3：前移到 P0a，缺省档） | 按 ADR 通过 | ADR-216：RTF 3.60、启动 p95 3.15 s、1.02 核 / 1.265 GiB（CPU 池正式） |
| D3-AC-51 | 内部工作区部署与角色 | 通过 | 绑定层面扫描通过，Cookie 与 4 角色矩阵 8/8，部署形态复测通过 |
| D3-AC-56 | 最小基准闭环端到端（首个里程碑） | 通过 | B10 第 2 轮 120 个 Run 全部 COMPLETED，墙钟比 ≤ 2.49，样例包与 `lab/mbl` 通过 |
| D3-AC-57 | 赛道指标、置信区间与排行榜规则（v1.4 按 ADR-191 修订） | 通过 | 榜单两次重算逐字节一致，构造用例 (a) 至 (h) 通过 |
| D3-AC-58 | Python SDK 与基线策略 | 按 ADR 通过 | ADR-202：干净容器 45.0–51.2 s；`track_share` EC 高于 `coverage_scan`；动作流 60/60 一致 |
| D3-AC-60 | 卷帘快门与 1080p 多机输出 | 不通过 | 卷帘几何通过（逐顶点 p99 0.034 px）；`test_1080p` 2 项失败（探针签名、读回环 6 槽位） |
| D3-AC-11（P0b，仅引用） | 相机档位吞吐 | 通过（K08，A 级独占；不计入本表 21 条） | 16 项条款全部通过，`seal_ready = true`；C-44 已确认 |

阻塞封板的是 3 条"不通过"与 D3-AC-46 的发布动作：回归门在 HEAD 上不是全绿（D3-AC-01），两处证据测试因探针与 `ea01e1e` 之后的 L2 异步路径不同步而失败（D3-AC-09、60）。三者均已定位到具体原因，不是阈值未达，修复范围见 §3。零影响对拍（两档 `AWR_D3_ENABLED`）、resim、import 方向在 HEAD 上通过。

## 1. 回归门与全量测试

### 1.1 重载回归门（`make gate-d3-dmax`）

命令：`make gate-d3-dmax GATE_REF=c075fd716dceed448da71103151dcd156ce1eee4 GATE_D3_ROOT=<证据根>`（步骤 `pytest,zero-impact,resim,perf,imports`）。dmax CPU 池容器 `d3-test-gate-main`（`d3-agent-py:dev`，Python 3.12，D2 锁文件离线 venv，`--cpus 16 --memory 48g`，`--cgroup-parent d3.slice`，不挂 GPU）。开始 07:39:11 UTC，结束 08:52:06 UTC，墙钟 72 min 55 s（含源码与世界包同步）。`AWR_D3_ENABLED` 在零影响与 resim 步骤内分别取 0 与 1 两档。

| 步骤 | 结果 | 耗时 | 要点 |
|---|---|---|---|
| pytest（`-m "not perf"`） | 不通过 | 3,616 s | 步骤在会话收尾报 `OSError: Too many open files`（容器缺省 `nofile` 上限），退出码 1，没有失败清单；2026-10-19 的 G05 运行同样如此（`gate/out/g05-pyt`）。逐用例诊断见下 |
| zero-impact | 通过 | 185.5 s（`=0`）、168.6 s（`=1`） | 两档 `relay_batch.py --case b --hours 1 --seed 11 --determinism`：`relay_stats.json` sha256 `c7354dc2fa9b07b451ba70a50acd36140d7d9a40fb64a81c98de047578b9370f`、`events.jsonl` sha256 `88de54fa95083adbf555cd910a9bdb6e1f63ad89c072b355a9a0072042882b9e`，两档相同且与 `baseline.json`（D2 基线）相同；两档 `tests/targets/test_determinism.py` 均通过；间隙检查 `gaps.non_exempt` 与 D2 基线同样不通过且取值相同（差分口径） |
| resim | 通过 | 4.4 s × 2 | 两档帧摘要序列相同且与 D2 基线相同：3,750 帧，`frames_sha256` `f6d5a3fe6952d0d83c3a7836201c06acf8c9da606ca257d88ce7fd8f3705d4d0`，输入序列 5 条 |
| perf（D2 性能子集 vs 基线 ≤ 5%） | 不通过 | 69.5 s；复测 2 min 35 s | 见下表 |
| imports | 通过 | 8.4 s | `tools/lint/check_py_imports.py` |

perf 步骤（G04：独立子进程预热 1 次、重复 7 次，线程 CPU 时间取中位，对基线 `baseline.json` 的 `perf` 节，判据 ≤ 5%）两次测量，逐用例比值：

| 用例 · 指标 | 基线（D2） | 第 1 次 | 比值 | 第 2 次（复测） | 比值 |
|---|---|---|---|---|---|
| `tests/targets/test_budget.py::test_stage_budget_30x12` · stage_cpu_ms_per_sim_s | 8.7256 | 8.9254 | 1.023 | 9.1703 | 1.051 |
| 同上 · call_cpu_ms | 1067.292 | 1110.411 | 1.040 | 1150.67 | 1.078 |
| `tests/perception/test_budget.py::test_stage_cost_12x30` · call_cpu_ms | 261.994 | 263.941 | 1.007 | 267.913 | 1.023 |
| `tests/perception/test_field_budget.py::test_field_cost_12x30_per_sim_second` · call_cpu_ms | 4424.172 | 4812.656 | 1.088 | 4715.439 | 1.066 |

判读：4 个指标中第 1 次 1 项、第 2 次 3 项超过 5%，用例内重复离散 ≤ 0.35%（离散小而偏移大，说明是整体偏移，不是抖动）。测量时 dmax 1 分钟负载 20.6–23.2（64 逻辑核，每核归一 0.32–0.36，在前置条件上限内），B21 正式评测与其他租户同机运行；基线是在 `d3.slice` 内无其他 D3 作业的窗口、每核负载 0.30–0.45 下录制的（`docs/05` §26.3 第 29 行：`d3-dev` 对基线比值 0.96–1.012）。既有结论（D2-FX-R2 与 FX-SIM2 报告）：perf 标记的计时用例须在排他性能锁与低负载窗口下判定。本次按"与主机负载相关"标注，**不能据此判定为产品回退，也不能判定为通过**；须在 B21 结束后的无 D3 作业窗口复测（遗留 §4.1 第 2 项）。

#### pytest 步骤逐用例诊断

该步骤以 `{py} -m pytest -q -m "not perf"` 在 D2 锁文件 venv 中运行整个仓库的测试（含 `tests/d3`）。为得到失败清单，以脚本相同的镜像、venv 与挂载另起容器 `d3-rp0a-pytest`（`d3.slice`，`--cpus 16 --memory 48g`，另加 `--ulimit nofile=1048576`，`-rfE --junitxml`），08:57:11–09:51:47 UTC，54 min 31 s：**5,613 passed、32 failed、6 errors、551 skipped、62 deselected、1 xfailed**。38 个未通过用例分为两组，均不是 D3 引入的回归：

| 组 | 用例数 | 原因与核对 |
|---|---|---|
| D2 范围（`tests/` 其余） | 15 | `tests/planning/test_registry.py` 7 项与 `tests/world/test_catalog.py::test_six_cities_ready` 1 项：D1 黄金摘要在 dmax 容器内不匹配，根因未定位；`d2` 的 `v0.2.1` 源码在同一容器、同一 venv 中同样失败（`NPY_DISABLE_CPU_FEATURES` 与 `OPENBLAS_CORETYPE=Haswell` 均不改变），故非 D3 引入；六城世界状态为 `stale`，推断与世界同步排除 `.status` 目录有关（未单独验证）。两者归为 dmax 容器环境差异；`tests/contracts/test_codegen.py`、`test_lint_tools.py` 2 项：容器无 `node_modules`，轻量门 `contracts` 步骤（`make test-contracts`）通过；`tests/e2e/test_demo_check.py::test_make_demo`（演示构建产物不在容器内）由 public-build 与 public-e2e 步骤覆盖；`tests/ops/test_skeleton.py`（容器无 `make`）、`tests/runtime/test_cli.py::test_doctor_quick_json_passes`（`DOC-10` `/dev/shm` 容器 64 MB）、`tests/rt/test_interest.py`、`tests/sensors/test_pose_chain_e2e.py`（异步超时，负载相关）。上述 `planning`、`world`、`rt`、`runtime`、`sensors`、`ops` 6 个文件在 HEAD 快照上以本机 D2 venv 运行：**95 passed、7 skipped** |
| `tests/d3` | 23 | D2 venv 缺 D3 依赖（PIL、protobuf、foxglove、mcap）与 Node，容器缺 `rsync`、`git`、`docker`；23 项在 I01 补跑（§1.3）中**全部通过** |

结论：D2 范围的 4,528 个用例通过，15 个失败均为容器环境差异且在适配环境中通过或由其他步骤覆盖；步骤本身的两个缺陷已登记（F-114）：范围含 `tests/d3`、缺省 `nofile` 上限使会话收尾崩溃。

### 1.2 轻量回归门与 public-e2e（本机）

命令：`make gate-d3-light GATE_REF=c075fd7…`，本机 8 核，07:40:59–07:46:29 UTC（5 min 35.8 s）；`GATE_D3_LIGHT=public-build,k9-d3,public-e2e` 另跑一次，09:00:47–09:06:38 UTC（5 min 56 s）。

| 步骤 | 结果 | 要点 |
|---|---|---|
| contracts（`make test-contracts`） | 通过 | |
| web-unit（`vitest --project unit`） | 通过 | 含 `tape.test.ts`、`replay.test.ts` 等 |
| resim | 通过 | 本机两档 |
| lint（`make lint`） | **不通过** | 唯一违规：REL-01 `research/ccf-tpci/data/m1geo_full.parquet`（12,858,479 B > 5 MB）。K02 已决定移出仓库，F-58 要求提交代理 `git rm --cached`，HEAD 尚未执行；其余规则（ruff、oxlint、lint-m06、copy-lint、no-emoji、check-brand、registry）通过 |
| imports | 通过 | |
| public-build（`make demo-build` 与 K9 prod-bundle-scan） | 通过 | 66 个文件，禁用字符串 0，测试文件 0，真值文件 0 |
| k9-d3 | 通过 | 公开构建中 D3 入口字符串（`/lab/`、`/api/bench/v1…`）0 次 |
| demo-parity | **不通过** | `d3-dev` 的演示构建与 `v0.2.1` 构建不逐字节一致（`only_a` 与 `only_b` 各 7 个资产文件、`differ index.html`），原因是 D3-AC-46 的文案复核在 `d3-dev` 而 `d2` 分支与 `v0.2.2` 尚未建立（ADR-209，F-30）；H04 演练已证明合入后对 `v0.2.2` 66/66 一致 |
| lab-build（`/lab/` 静态构建与扫描） | 通过 | |
| public-e2e（本机 cgroup 模拟 emax） | 通过 | 落地页（6 个分块）、World Hub（7 个世界）、共享展示 viewer（8 架）、访客沙盒、Tier S 首屏、D3 入口请求 0；Tier S：synthcity 首屏 490,260 B、shenzhen 321,384 B，揭示时程序数 27、绘制点 20,000、GL 错误 0、预算违规 0，TTFP 4.95 ms 与 5.52 ms；与 D2 基线比较判据通过；进程内负载前 0.66 |

emax 上线后的复测（D2 公开站 e2e、Tier S 首屏、`/api/bench/*` 404、N_max、回滚演练等）按 ADR-194 决策 (4) 在上线后执行，本次为合入前的本机门，"发布就绪"。

### 1.3 I01 全量测试（`tools/deploy/dmax/run_d3_tests.sh`）

命令：在 HEAD 的 `git archive` 快照目录（标签干净，`dirty = 0`）中运行 `GPU=2 TASK_ID=rp0a WORLDS=0 OUT_LOCAL=<证据根>/i01-tests bash tools/deploy/dmax/run_d3_tests.sh`；`AWR_D3_REQUIRE=dep,gpu`；GPU 2（A40，运行前无外部进程）；主阶段 `d3-sensorsvc:dev`（`--cpus 8 --memory 32g`），gz 阶段 `d3-gz:dev`（CPU 池，4 核）。07:45:15–08:11:38 UTC（26 min 23 s；主阶段 1,356.7 s，gz 阶段 179.0 s）。

**入口结果（`counts.json`，`ok = false`）**：共 2,333 个用例：passed 1,913、failed 25、error 279、skipped 112（optin 28、data 4、platform 46、other 34）、xfailed 4、收集错误 2。gz 阶段 61 passed、3 failed、3 skipped。

按 `AWR_D3_REQUIRE` 的约定，缺依赖计失败。304 个未通过用例（主阶段 22 失败 + 279 错误，gz 阶段 3 失败；另有 2 个收集错误）的原因经逐条核对分为六类（283 + 4 + 8 + 3 + 5 + 1 = 304）：

| 类别 | 用例数 | 原因 | 处置 |
|---|---|---|---|
| 缺 Node 22 与 navsvc `node_modules`（合成世界构建经 NavMesh 阶段，`node not found`） | 283（68 个直接报 `node not found`，其余经 `build_world` 与 `agentsdk_pkg` 的 setup 错误） | 默认镜像 `d3-sensorsvc:dev` 自 W12（Detour）后缺 Node；`d3-compile:dev` 含 Node 与 navsvc | 在 `d3-compile:dev` 补跑（B 组，GPU 2，300 个用例）：283 通过 |
| 缺 `datafusion`（`leak_redteam/test_catalog_truth`、`rerun/test_catalog`、`rerun/test_proxy`） | 4 个用例 + 2 个收集错误 | 数据面依赖只装在 `d3-lab-py` | 将 `d3-lab-py` 的 `datafusion` 54.0.0 以 `PYTHONPATH` 叠加到 `d3-compile:dev`（C 组）：28 passed、1 xfailed |
| 容器内缺 `rsync`、`git`、`docker`（`tests/d3/deploy` 7 个、`submit/test_redteam` 1 个） | 8 | 这些用例设计在主机运行 | 本机补跑（D、G 组）：`tests/d3/deploy` 与 `submit/test_redteam` 186 passed，`tests/d3/regress_gate`、`tests/d3/copy`、`test_lab_profile` 78 passed（`test_pre_push_hook_only_guards_d3_dev` 在本机 `GIT_DIR` 环境变量下失败，清除变量后 6/6 通过） |
| gz 阶段镜像 `d3-gz:dev` 陈旧（2026-10-18 构建，早于 P08/P09 插件）：`test_gz_wind` 2 项；`test_fault` gz 重放 1 项（`chaos`+`perf` 标记） | 3 | `test_gz_wind` 的风场插件不在该镜像中（在 `2e1a845`、`ea01e1e` 的源码上同样失败，与近期改动无关）；`test_fault` 为负载相关 | gz 阶段改用当前镜像 `d3-gz:b21`（H 组）：64 passed、0 failed、3 skipped |
| 测试探针与 `ea01e1e` 之后的 L2 异步路径不同步 | 5（`test_multiview` 的 3 个 `[fast]`、`test_1080p` 的 2 项；docker 套接字补跑中 3 个 `[realistic]` 同因失败） | 见 D3-AC-09、60 | **未解决**，计入 §3 |
| 负载相关 | 1（`test_official::test_variants_bitwise_equal_and_summary`，背压运行墙钟 11.25 s 未大于无背压 11.28 s） | 墙钟不等式在负载下不成立 | 隔离重跑 3/3 通过（44.7–45.8 s） |

另做一次 docker 套接字补跑（E 组，`d3-compile:dev`，GPU 2，`-k realistic` 与 `test_official`、`test_fault`、`test_container_isolation`、`test_redteam`）：`test_container_isolation` 5 项因容器内再启动 Agent 容器后监听端口不可达（`665 hello_timeout`，宿主路径映射不匹配）报错，属补跑方式与该用例的"宿主正式运行"设计不符，不作判定，归 RP0b（D3-AC-61、62）；`test_redteam` 的 2 项同因失败而在本机补跑通过。

**合并结果（按用例，任一阶段通过即记通过，共 2352 个用例）**：通过 2295、失败 14、跳过 37、预期失败 6。剩余失败 14 项：D3-AC-09 的 6 项、D3-AC-60 的 2 项（§3）；`test_container_isolation` 5 项为补跑方式不符、未判定（归 RP0b）；`test_geosvc.py::test_crop_session_memory_and_service_rss` 1 项（选择性用例，本机补跑：会话内存增量断言通过，M22-NFR-005 服务 RSS ≤ 700 MiB 断言失败，实测 907 MiB，不属 D3-AC-05 条款，见 §4.2）。

**跳过项**：合并后仍跳过的用例以选择性（`optin`）与数据层为主：正式矩阵（`AWR_A08_FORMAL` 红队矩阵、`AWR_D3_FAULT_SOAK` 浸泡）、P0b 与 P1 以后的条款（Trivy 离线库、nginx 检查、`AWR_D3_TIER_WORLD` 两档对照）、依赖 dmax 正式产物的判定用例（`AWR_R6_EC_DIR`、`AWR_GZ_LOCKSTEP_REPORT`、`AWR_D3_OFFICIAL_SUMMARY`，其数字已由 K04、K10、B10 的正式运行给出）。其中 crop 原始 OSM 数据缺失的 I01 容器内跳过项，已在本机真实检出补跑（`test_navmesh.py`、`test_vehicle.py`、`test_lanes.py`、`test_build_repro.py` 带 `AWR_D3_CROP_REBUILD=1`、`test_geosvc.py` 带 `AWR_GEOSVC_CROP_WORLD`）。

> 入口自身的缺口已作为遗留登记（F-113）：I01 在当前镜像下不能一次得到全绿的 `counts.json`；本报告以"入口结果 + 逐类补跑"的方式给出等价覆盖，补跑不改变测试代码。

### 1.4 环境与负载记录

| 项 | 记录 |
|---|---|
| dmax | Xeon Gold 6326，64 逻辑核；本任务期间 1 分钟负载 16–25（B21 正式评测与其他租户运行），`d3.slice` 内另有 `d3-gz-v1-*`、`d3-agent-v1-*` 等 B21 容器 |
| GPU | GPU 2（A40）：运行前 0 MiB、无外部进程；I01 与补跑期间仅本任务使用；GPU 1、3 及其余卡未触碰。I01 不是性能测量，不要求 A 级独占记录 |
| 本机 | E5-2603 v4，8 核，无 GPU；轻量门期间负载 0.66–4.9 |
| 容器 | 仅 `d3-*`：`d3-test-gate-main`、`d3-test-rp0a`、`d3-test-rp0a-b`、`d3-test-rp0a-f`、`d3-rp0a-*`；全部 `--rm`；未做机器级 `docker prune` |
| 时间 | 一律 UTC（dmax 宿主本地时区为 UTC+8，日志换算后使用） |


## 2. 逐条验收

判定取值：通过；按 ADR 通过（阈值或口径经已确认的 ADR 或用户决定修订后通过）；发布就绪（实现与演练完成，按 ADR-194 决策 (4) 以上线实测为准，不计入封板）；不通过；移 D3.x。每条给出 `docs/05` §11 的阈值原文（取自 `c075fd7`）、实测值、证据与性能标注。

### D3-AC-46 D2 发布面文案复核

- 判定：**发布就绪**；优先级：阻塞；环境：本机；公开站（上线后复核，ADR-194）

- 阈值原文（`docs/05` §11）：

> `README.md`、`README.zh-CN.md`、公开站落地页与关于页、`zh-CN.json`、`en.json` 通过 copy-lint 中文与英文规则集（基线例外逐条登记，P0b 出口前清零）；README 不含口号式标语与未注明条件的性能数字，能力表述区分 D2 与 D3 的服务器 GPU 前提；产品名写法统一；D1 至 D4 的交付称谓与用户可见称谓按 §7.10（含 v1.6 交付称谓表）统一，不出现"ANet4D 2.0"一类称谓；GitHub About、topics 与 release notes 经评审；受影响的 D2 e2e 文本断言随同一变更更新且全部通过；复核结果已合入 `d2` 分支并随 D2 标签发布（v1.6：标签见 ADR-194 决策 (6) 与 Q-D3-21，以上线后的公开站落地页与关于页复核）

- 实测：
  - 文案侧（`d3-dev`）：`tests/d3/copy/` 共 23 项通过（含 `test_copy_review_record.py`）；K09 记录 `make lint-copy-lint lint-copy-strict` 严格模式 0 违规，基线 `exceptions = []`、`allow = []`；H11 人工评审 17 项发现中须改正 9 项已由 K09 关闭（`079e61f`），评审记录先行生效。
  - README 许可节已补 CC-BY-4.0、ODbL 与 DEM 署名（K09，W-14）；V0.3 状态翻转以 `docs/impl/v0.3-release/copy.prepared.json` 备好，封板后生效。
  - 发布线演练（H04，ADR-209，独立克隆，未动仓库引用）：`d2` 两个提交后的树 `0fa048af…`，发布面静态检查 26 passed、2 skipped，`make lint-tools` 通过，vitest 332 例通过；合入后 `demo-parity` 对 `v0.2.2` 66/66 文件一致。
  - 尚未执行：`d2` 分支在本地与 `origin` 均不存在（`git branch -a` 仅有 `d3-dev`、`main`），标签只有 `v0.2.0`、`v0.2.1`，`v0.2.2` 未打；GitHub About、topics、release 未更新；上线后的公开站落地页与关于页复核未做。
  - 本次 `make lint`（HEAD 快照）仅余一项违规：REL-01 `research/ccf-tpci/data/m1geo_full.parquet`（12,858,479 B > 5 MB）。K02 已决定该文件移出仓库（F-58 要求提交代理执行 `git rm --cached`），HEAD 尚未执行。
- 证据：
  - `tests/d3/copy/`（`3cb75d0`）；`docs/impl/D3-文案评审记录.md`（`3cb75d0`、`079e61f`）；`docs/impl/D2-v0.2.2-文案评审记录.md`、`D2-v0.2.2-发布执行单.md`；本报告 §1.2 轻量门 `lint.log`。
  - 用户 2026-10-22 决定：C-02（`v0.2.2` 与 GitHub release 按 ADR-209 执行）、C-24、C-25、C-51 已确认，满足执行条件。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：按 ADR-194 决策 (4)，本条以上线后的生产环境复测判定，"发布就绪"不计入封板；执行顺序见 §4 遗留。上方阈值引文中的 "ANet4D 2.0" 是 docs/05 原文，属于文案规范禁用的版本称呼，仅作引文保留（docs/impl 不在文案检查范围，发布面不得出现）。

### D3-AC-47 P-1 实测报告

- 判定：**按 ADR 通过**；优先级：阻塞；环境：dmax

- 阈值原文（`docs/05` §11）：

> 报告入库并以 ADR 冻结：正确尺度下 r3、r4 复测数字；CPU 与 GPU 逐射线距离差分布（p50、p99、最大）；逐点位姿与单一位姿的 ATE 比值；Fast 档 FleetSim 轨迹（`clock_profile = {d1_250, 250, 250, 250}`）上 FAST-LIVO2 LIVO 与 LIO 的 RPE100（clean、realistic，5 种子 × 3 重复；单次取段均值，跨运行取中位数）；`pose_nodes = 11` 与 `native`、`slam_cam` 两种触发相位的 RPE100 对比；720p 相机完整链路端到端吞吐（晴天档与最坏档，注明是否独占）；`/data` 持续写带宽；`slam_eval` 内存模型参数 r_map；22 B 磁带布局的存储密度；v1.3 增加：gz 锁步链路 spike 的 3 机 RTF 与同种子位姿逐位一致结果、gz `physics` 实例 CPU 与内存、`rgb1080-rs` 与 `therm512` 端到端吞吐（晴天档与最坏档）、1080p JPEG 单帧体积、标准轨 M25 会话显存与 GPU 时间；FAST-LIVO2 RPE100 基线在 gz 锁步与 Fast 档两种轨迹上各报一组

- 实测：
  - §11.1 所列 16 项全部有实测并入库 `docs/impl/D3-P-1-实测报告.md`（`c3624c2`，K08 补记于 `c075fd7`），冻结 ADR 为 ADR-213 至 ADR-216（用户 2026-10-22 已确认，C-43、C-44）。
  - 性能类以 A 级独占卡或 CPU 池为准：720p 完整链路吞吐 K03 单进程 78.81 / 61.13 帧/s（晴天 / 最坏，GPU 2，A 级），K08 渲染进程组 237.01 / 234.07 帧/s（A 级）；`rgb1080-rs` K03 52.04 / 53.46、K08 164.44 / 157.18 帧/s；`therm512` K03 114.33、K08 518.16 帧/s；`/data` O_DIRECT 持续写中位 892.6 MB/s，预算 min(300, 0.5 × 892.6) = 300 MB/s；gz `physics` 实例 CPU p95 0.49 核、最大 1.02 核，anon 最大 1.265 GiB。
  - 内容类（与独占无关）：逐射线 19.2M 条，p50 7.1e-15 m、p99 1.63e-13 m、最大 2.522 m（3 条轮廓边 float32 歧义射线，按 ADR-207 计数）；ATE 比值 crop 15.2–64.4 倍、town 51.2–88.2 倍；FAST-LIVO2 RPE100 Fast 组 LIVO clean 0.080%、realistic 中位 0.102%，gz 主组 LIVO clean 0.054%、realistic 中位 0.112%；MID-360 r_map 3.8–4.3 万点/s；22 B 磁带 14.42 B/演员/s（43 名演员）。
  - 与本次测试的关系：`tests/d3/bench/test_camera_throughput.py` 等工具测试共 26 项通过。
- 证据：
  - `docs/impl/D3-P-1-实测报告.md` §0、§2、§3、§4.1；`docs/impl/D3-独占卡性能验收.md`；`docs/05` §31.3 第 89 行、§30.3、§29.3；dmax `$D/k03/formal/out/camera-k08/camera_throughput.json`、`$D/s21`、`$D/k04`、`$D/sl3`。
  - 提交：`c3624c2`（报告）、`5f6792d`（K03 工具）、`c075fd7`（K08 与报告补记）。
- 性能标注（§4.7、ADR-195）：K03、K08 为 A 级独占（GPU 2，guard 预窗口与窗口内外部进程均为 0，3 次取中位）；gz 实例资源与 RTF 为 CPU 池正式（ADR-195 决策 (6)）；D00 的 B 级"独占（驻留豁免）"数字只作参考，正式数字由 S21 的 A 级窗口取代。
- 备注：D3-AC-14 的冻结与判定归 RP0b，不在本报告范围。

### D3-AC-01 D2 零影响回归门

- 判定：**不通过**；优先级：阻塞；环境：本机、emax（或 cgroup 模拟；v1.6：D2 公开站 e2e 与 Tier S 首屏在封板上线后的 emax 上复测，ADR-194）

- 阈值原文（`docs/05` §11）：

> D2 Python 测试全集、前端单测与契约测试通过；`AWR_D3_ENABLED = 0` 与"= 1 但不加载事件"两种条件下，`relay_batch.py --case b --hours 1 --determinism` 的 `events.jsonl`、`relay_stats.json` 与 D2 基线逐字节一致，`--resim` 帧摘要序列一致，`tests/targets/test_determinism.py` 轨迹哈希一致；D2 性能子集与基线差 ≤ 5%；`make lint-m06`、`m06.gpu-limits` 与 import 方向测试通过；公开演示构建（D2 标签构建与 `/lab/` 独立静态构建）上 ADR-111 K9 prod-bundle-scan 通过、D2 公开站 e2e 通过、Tier S 首屏指标不回退；扩展点 15、16 在开关关闭时 D2 构建产物与站点行为不变；D1 与 D2 阻塞级验收不回退

- 实测：
  - 重载门（dmax CPU 池，D2 锁文件离线 venv，两档 `AWR_D3_ENABLED`）：**零影响对拍通过**，`relay_batch.py --case b --hours 1` 的 `relay_stats.json`（`c7354dc2fa9b…`）与 `events.jsonl`（`88de54fa9508…`）两档逐字节一致且与 D2 基线一致；两档 `tests/targets/test_determinism.py` 轨迹哈希一致；**resim 通过**，3,750 帧摘要序列两档一致且与 D2 基线一致；**import 方向通过**。
  - 轻量门：contracts、web-unit、public-build（K9 prod-bundle-scan）、k9-d3、lab-build 通过；`m06-lint`、ruff、oxlint、copy-lint、no-emoji、check-brand 通过；**public-e2e（本机 cgroup 模拟 emax）通过**，Tier S 首屏与 D2 基线比较通过。
  - D2 Python 测试全集：D2 范围 4,528 个用例通过，15 个失败均为容器环境差异（本机 D2 venv 复核 95 passed；见 §1.1 诊断），`tests/d3` 范围由 I01 覆盖（回归门相关用例 48 项通过、1 项跳过）。
  - **未满足的 4 项**：(1) 性能子集比基线差 ≤ 5%：两次测量 4 个指标中 1 项与 3 项超过 5%（比值最大 1.088），测量时 B21 在同机运行，按负载相关标注，待无 D3 作业窗口复测；(2) `make lint`：REL-01 `m1geo_full.parquet`（F-58）；(3) `make demo-parity`：`d2` 分支与 `v0.2.2` 未建立（ADR-209），扩展点 15 的"构建产物逐字节一致"以 `v0.2.2` 为参照；(4) 门的 pytest 步骤在容器内不能得到干净结果（范围与 `nofile`，F-114），其实质内容已由诊断覆盖。
  - emax 上线后的复测（D2 公开站 e2e、Tier S 首屏）按 ADR-194 决策 (4) 在上线后执行，当前为"发布就绪"。
- 证据：
  - 重载门证据 `gate-evidence/653ac8e36175/heavy/{report.json,meta.json}`（07:39:11–08:52:06 UTC，tree `653ac8e36175`，`runner = dmax`）；perf 复测 `gate-evidence-perf2/`；轻量门 `gate-evidence/653ac8e36175/light/`；public-e2e `gate-evidence-e2e/653ac8e36175/light/`；pytest 诊断 `gate-pytest/junit.xml`。
  - 测试与工具：`tools/bench/d3/regress_gate.py`（`9a2353b`）、`tests/d3/regress_gate/`（`1652e0d`）、`tests/sim/test_d2_zero_impact.py`（`f24e206`，K01，10 个用例）；基线 `tests/d3/regress_gate/baseline.json`（`d2@928c57b`）。
- 性能标注（§4.7、ADR-195）：性能子集（G04）为 CPU 池 7 次中位、线程 CPU 时间；基线在无其他 D3 作业的窗口录制，本次测量时 B21 正式评测在同一主机运行，每核归一负载 0.32–0.36（前置条件上限内）。
- 备注：核心条款（零影响、resim、K9 扫描、公开站 e2e）在 HEAD 上通过；转为"通过"需要：移出 `m1geo_full.parquet`、执行 ADR-209、收窄 pytest 步骤、在无 D3 作业窗口复测 perf。上述完成后重跑 `make gate-d3-dmax` 与 `make gate-d3-light` 并以 `make gate-d3-check GATE_TIER=full` 合并两份报告。

### D3-AC-02 世界包构建与标定

- 判定：**按 ADR 通过**；优先级：阻塞；环境：dmax CPU 池

- 阈值原文（`docs/05` §11）：

> crop 世界包（P0a；town 世界包在 P0b 按同一判据复验）两次构建 `contentVersion` 相同；`scaleStatus = landmark` 且 `metric_ok()` 为真，尺度与 r2 结果差 ≤ 0.5%；调平：DEM 回归与墙面竖直度两法之差 ≤ 0.5°（超出时 qa 告警并要求控制点），且调平后 ≥ 3 处 OSM 水体或广场等已知水平面的坡度 ≤ 0.2°，或 ≥ 20 个分散控制点的 AGL 残差 RMS ≤ 2 m；V-GLB 全部通过；`dsm_2m` 与网格竖向射线抽样差 < 1 cm；D2 sim-core 与 M05 不改代码加载其点云、DSM、DTM；对未标定世界（`scaleStatus = relative` 或标定证据不完整）提交事件编译返回 600；两次构建分别在 AVX2 与 AVX-512 机器上执行时内容文件 sha256 相同；单世界构建（不含配准）≤ 10 min；`dataset.license`、`citation`、`derivedFrom[]`（含 ODbL）齐全；v1.6（ADR-210）：调平验收也可按 L5（两套公开 DEM 加墙面竖直度交叉核对）判定；L5 成立时水平面判据降为告警（G-16）

- 实测：
  - crop 世界包 `7d8c8b895e55`：两次构建 `contentVersion` 相同（本机 AVX2 两次 423 s、427 s，dmax AVX-512 一次，38/38 个内容文件 sha256 相同）；`scaleStatus = landmark`，尺度相对 r2 +0.23%（≤ 0.5%）。
  - 调平：DEM 回归与墙面竖直度两法之差 0.349°（≤ 0.5°）；按 ADR-210 采用 L5：GLO-30 剩余倾角 0.084°、墙面 0.113°、三法均值 0.168°，L3 水平面判据降为告警（G-16，多瑙河网格水面最大 3.393°）。
  - `dsm_2m` 与网格竖向射线抽样最大差 6.7e-5 m（< 1 cm）；V-GLB 15 项 pass、V-GLB-06 不适用；未标定世界提交事件编译返回 600（`tests/d3/events/test_world_gate.py`）。
  - 跨机：crop 38/38、town `b5103d23ed70` 36/36 个内容文件在本机（E5-2603 v4，AVX2）与 dmax（Gold 6326，AVX-512）上 sha256 相同；单世界构建（不含配准）dmax CPU 池 16 核 crop 162.0 s、town 148.1 s（≤ 10 min）。
  - 本机补跑 `AWR_D3_CROP_REBUILD=1`：从原始数据重建 crop 并与已发布版本逐文件对拍通过（`test_build_repro.py`，本机 12 min 内与 `test_vehicle.py`、`test_lanes.py`、`test_geosvc.py` 同批）。
  - 测试（合并后）：通过 113。
- 证据：
  - `tests/d3/world/test_build_repro.py`（`ecb759f`）、`test_dsm_vs_mesh.py`、`test_d2_load.py`、`test_town_package.py`、`test_vglb.py`；`tools/bench/d3/crossmachine.py`（`5d68e21`）；`docs/05` §24.3 第 1 行、§27.3 第 42 至 44 行、§30.3 第 80 行。
  - ADR-210（用户 2026-10-22 确认，C-26）。
- 性能标注（§4.7、ADR-195）：构建时长为 dmax CPU 池原型测量，未加 ADR-033 排他锁，余量约 3.7 倍，属共享参考。
- 备注：town 世界包在 P0b 按同一判据复验。

### D3-AC-03 NavMesh 与锚点

- 判定：**按 ADR 通过**；优先级：阻塞；环境：本机与 dmax

- 阈值原文（`docs/05` §11）：

> 行人 NavMesh 在种子洪泛裁剪后随机 1,000 对路径 100% 可达；路径落在 OSM 道路走廊的比例 ≥ 70%，经过植被 ≤ 5%；车辆 profile NavMesh 存在且示例事件的车辆段可达；点状锚点（路口、出入口、POI、人工点；建筑多边形不计）`navSnap` 中位距离 ≤ 1 m，> 3 m 的比例 ≤ 5%（允许沿 OSM 道路自动重定位；v1.6 按 ADR-212 只在保留（可寻路）分量的点状锚点上统计，被裁剪岛上的锚点置 `status = review` 并单列）；两台机器烘焙的 NavMesh sha256 相同

- 实测：
  - crop `7d8c8b895e55`（K06，两台机器数字相同）：随机 1,000 对 1000/1000 可达；路径落在 OSM 道路走廊 89.48%（≥ 70%）；经过植被 4.39%（≤ 5%，余量 0.61 个百分点）；车辆 NavMesh 存在，示例事件车辆段可达（Detour 走廊 193.4 m、148.6 m）。
  - 点状锚点 `navSnap` 中位 0.0 m（≤ 1 m）；> 3 m 的比例按 ADR-212 口径 6/224 = 2.68%（≤ 5%）；被裁剪岛上的锚点 7 个置 `review`；全体口径 13/231 = 5.63% 单列披露。
  - 两台机器烘焙的 NavMesh sha256 相同：person `be5314d1…`、vehicle `53695af9…`；navsvc 对 1,200 条请求应答逐字节相同。
  - HEAD 补跑：I01 容器内没有原始 OSM 数据（`data/raw/d3-glb/osm/oryahovo_map.osm`），`test_navmesh.py` 的 crop 统计类用例 8 项跳过；在本机真实检出（Node 22 与 navsvc，代码与 HEAD 相同）补跑 `tests/d3/world/test_navmesh.py`：12 passed、1 skipped（跨机用例，证据为 X01），约 2 min。
  - 测试（合并后）：通过 22、跳过 1（余下跳过为跨机选择性用例，证据来自 X01）。
- 证据：
  - `tests/d3/world/test_navmesh.py`（`47a4ece`，12 个用例）、`test_navmesh_layer.py`；`docs/05` §29.3 第 74、75 行、§30.3 第 81 行。
  - ADR-196、ADR-211、ADR-212（用户 2026-10-22 确认，C-27、C-33）。
- 性能标注（§4.7、ADR-195）：不涉及性能项。

### D3-AC-04 不完整即未知

- 判定：**通过**；优先级：阻塞；环境：dmax

- 阈值原文（`docs/05` §11）：

> quality 层与覆盖多边形存在；occupancy 中覆盖外体素为 unknown（抽样 100%）；打到无数据区的 LiDAR 点在真值侧车中带 `void` 标记；相机真值层输出 `invalid_mask`；构造发生在洞内的事件，EC 分母剔除该事件并在报告中单列

- 实测：
  - quality 层与覆盖多边形存在（footprint 799 顶点、2.649 km²）；覆盖外 10,000 抽样体素全部 unknown，表面 10,000 抽样全部 occupied（100%）；LiDAR `void` 标记与相机 `invalid_mask` 位 1–4 逐像素对照参考通过；带洞世界端到端：剔除后 EC 1.0（1/1），不剔除对照 0.667，复评逐字节一致。
  - 测试：通过 37。
- 证据：
  - `tests/d3/quality/`（`87c4ab1`：`test_quality_layer.py`、`test_sensor_unknown.py`、`test_ec_holes.py`）；`docs/05` §25.3 第 13 行、§27.3 第 53 行。
- 性能标注（§4.7、ADR-195）：不涉及性能项。

### D3-AC-05 MeshWorldQuery

- 判定：**通过**；优先级：阻塞；环境：本机

- 阈值原文（`docs/05` §11）：

> D2 `tests/world_query` 的 105 个用例以 DSM 柱体网格夹具（由夹具世界的 DSM 生成网格图层）在 MeshWorldQuery 下重跑通过（2.5D 方法保持 2 m 栅格语义，`contact` 半径约束 `min(2.0, cell)` 不变）；`los_batch`、`ray_hit`、`free_distance` 与 Open3D 参考逐射线距离差 p99 ≤ 1 mm、最大 ≤ 1 cm；BVH 与 0.5 m 栅格只在共享几何服务中常驻（只读 memmap），sim-core 会话匿名内存（或 PSS）增量 ≤ 20 MB，文件页不计

- 实测：
  - D2 `tests/world_query` 的 105 个用例以 DSM 柱体网格夹具在 MeshWorldQuery 下重跑通过；逐射线距离差 p99 ≤ 1 mm、最大 ≤ 1 cm 与 Open3D 参考对拍通过。
  - 会话内存增量（`smaps_rollup`，`Pss_Anon`，基线为 D2 会话，打开 MeshWorldQuery 并执行 10,000 次查询）：`geosvc` 模式 +0.88 MB（≤ 20 MB）；进程内对照 +614 MB（说明常驻只在共享几何服务中）。
  - 选择性补跑（本机，`AWR_GEOSVC_CROP_WORLD` 指向 crop）：`test_crop_session_memory_and_service_rss` 中会话 `Pss_Anon` 与 `Pss − Pss_File` 增量断言通过，同一用例的 M22-NFR-005 服务进程 RSS ≤ 700 MiB 断言失败（实测 907 MiB，不含 Detour 工作进程）；后者不是 D3-AC-05 条款（条款只约束 sim-core 会话增量），登记为遗留（F-120）。
  - 测试：通过 168。
- 证据：
  - `tests/d3/world/test_mesh_query.py`、`test_world_query_mesh.py`（`1e7431c`）；`docs/05` §24.3 第 3 行。
- 性能标注（§4.7、ADR-195）：不涉及性能项（内存增量为口径内的确定性度量）。

### D3-AC-06 事件编译三道门

- 判定：**通过**；优先级：阻塞；环境：本机

- 阈值原文（`docs/05` §11）：

> 故意写错的事件一次报出全部错误（未知锚点、时间不单调、上车窗口、类别与 profile 不符、未知参与者）且退出码 2（611）；不可行段返回 612 与建议时刻，采纳后通过；strict 模式下声明不成立返回 613；未声明的显著事件以 `declared: false` 写入真值；示例事件编译通过且 4 类关键事件（相遇、上车、进入建筑、车辆经过）全部验证成立

- 实测：
  - 故意写错的事件一次报出全部错误且退出码 2（611）；不可行段 612 与建议时刻，采纳后通过；strict 下声明不成立 613；未声明显著事件以 `declared: false` 写入真值；示例事件 4 类关键事件全部验证成立。
  - 测试：通过 32。
- 证据：
  - `tests/d3/events/test_compile.py`（`0234ee8`）、`test_compile_gate1.py`、`test_verify.py`。
- 性能标注（§4.7、ADR-195）：不涉及性能项。

### D3-AC-07 事件磁带逐字节确定性（v1.6 按 ADR-197 修订）

- 判定：**按 ADR 通过**；优先级：阻塞；环境：本机与 dmax

- 阈值原文（`docs/05` §11）：

> 同一输入在同一机器连续生成 3 次、在两台不同 CPU 型号的机器（本机 AVX2 与 dmax AVX-512）上各生成 1 次，`content_sha256` 全部相同（规范头 JCS，对规范头原始字节与解压后规范帧数据求哈希；生成器为 Python 单一实现，TS 解码器对同一 `tape.bin` 计算的 `header_sha256`、`body_sha256`、`content_sha256` 与 Python `TapeReader` 相同，逐演员逐帧定点字段逐值相等，以入库 golden 与随机生成磁带两种方式对拍；改变 zstd 压缩级别时 `content_sha256` 不变；出处信息不进哈希）；只改注释、关系或显示名时 `content_sha256` 不变而 `truth_sha256` 改变；背景 0 / 40 / 200 / 1,000 人时脚本演员列哈希相同；脚本演员到达误差 ≤ 1 mm；全部演员帧的 |z_tape − z_mesh| ≤ 2 cm，`walkable_height` 的 Python 与 JS 实现 golden 逐位一致；构造上下车与进出建筑用例，可见性切换前后不跨帧插值、无滑移，移动与等待之间的切换无位移跳变；存储 ≤ 25 B/演员/s；43 名演员 360 s 生成 ≤ 10 s

- 实测：
  - 按 ADR-197 的口径（Python 单一实现生成，TypeScript 解码器对拍）：同机连续 3 次与两机各 1 次 `content_sha256` 相同；terrain-det-43（43 名演员，360 s）`1f7e7f63…`，crop-handoff-001 `b1285649…`，本机（AVX2）与 dmax（AVX-512）一致；背景 0 / 40 / 200 / 1,000 人脚本演员列哈希相同；存储 ≤ 25 B/演员/s（14.42，背景 1,000 人 12.38）；43 名演员 360 s 生成 warm ≤ 10 s（`test_generation_43_actors_360s_within_10s` 在 `d3-compile` 镜像中通过）。
  - TS 解码器对拍 `apps/web/tests/m06/tape.test.ts` 随前端单测（轻量门 web-unit，通过）。
  - 测试：通过 18。
- 证据：
  - `tests/d3/events/test_tape_determinism.py`（`c7d242d`）、`tests/d3/world/test_walkable_height.py`；`docs/05` §30.3 第 82 行；ADR-197（用户 2026-10-22 确认）。
- 性能标注（§4.7、ADR-195）：43 名演员生成 ≤ 10 s 为 dmax 共享主机上的测试内计时（warm），属共享参考；该用例在默认 `d3-sensorsvc` 镜像中因缺 Node 而报错，见 §1.3。

### D3-AC-08 Exogenous 隔离

- 判定：**通过**；优先级：阻塞；环境：本机

- 阈值原文（`docs/05` §11）：

> 同一磁带、≥ 5 种机队策略（含悬停于演员出发点 10 m 内、盘旋、跟踪）：三名脚本演员在 sim-core 中的实现轨迹 sha256 与磁带一致；M18"被发现"事件照常计数并写入度量；`reaction_policy` 缺省值下 D2 行为逐位不变（由 D3-AC-01 覆盖）

- 实测：
  - 同一磁带上 ≥ 5 种机队策略：三名脚本演员在 sim-core 中的实现轨迹 sha256 与磁带一致；M18 "被发现"事件照常计数并写入度量；`reaction_policy` 缺省值下 D2 行为逐位不变由 D3-AC-01 覆盖。
  - 测试：通过 6。
- 证据：
  - `tests/d3/events/test_exogenous.py`（`95fcf2b`）。
- 性能标注（§4.7、ADR-195）：不涉及性能项。

### D3-AC-09 多机多视角观测

- 判定：**不通过**；优先级：阻塞；环境：dmax（单卡绑定）

- 阈值原文（`docs/05` §11）：

> 同一磁带上 1 / 3 / 5 架三种机队配置（悬停、扫描、跟踪）在 Realistic 档（缺省）下产出的 `rgb1080-rs` RGB、深度、实例、语义帧摘要集合两两交集为空（起飞前地面帧及渲染输入相同的帧除外：位姿与可见演员状态差 ≤ 1 µm / 1e-9 rad，RGB 另要求同机号、同帧号；参考策略共用约 27 s 的起飞爬升段，同一架机在此阶段产生相同帧；v1.6 口径修订，S03，待确认，§23.5 C-12）；帧时间戳等于中间行曝光中点（整数 ns，与触发网格误差 0），采集窗口结束时刻不晚于交付 tick，渲染所用位姿全部取自已推进的 PoseHistory（不外推断言）；在无遮挡、无截断的构造视角下，演员网格三维质心的投影与只渲染该演员一遍得到的掩膜质心之差 ≤ 1 px（1080p，静止机体以排除卷帘效应；720p 档同样成立），已知标记顶点在实例图中的像素位置误差 ≤ 1 px；两遍合成与联合 z-buffer 的实例 IoU 均值 ≥ 0.999；斜距 ≥ 300 m 时人与地面、人与贴墙立面的实例 IoU ≥ 0.99；`dist ≠ 0` 时 RGB 与深度、实例、语义的边缘对齐误差 ≤ 0.5 px（v1.6 口径，S03：全部边缘样本的 RMS ≤ 0.5 px 且系统偏差 ≤ 0.1 px；标签按最近邻采样（M25-FR-036），单个样本按设计量化到约 1 px）。Realistic 档部分在挂载 docker 套接字的 `d3-sensorsvc` 容器中运行，统一入口主阶段在平台层下跳过

- 实测：
  - 判定理由是证据回归，不是阈值未达：S03（`cd5b636`，2026-10-18）记录的 I01 标签 `0c6f948327b3-dirty-85b23d3a` 为 14 passed、0 failed，Realistic 运行 `s03-run7-realistic` 3 passed；本次 HEAD `c075fd7` 上 `tests/d3/sensors/test_multiview.py` 的 6 个参数化用例失败（`[fast]` 3、`[realistic]` 3：`test_multiview_frame_digests_disjoint`、`test_multiview_frame_timestamps`、`test_multiview_poses_from_advanced_history`），错误均为 `AttributeError: 'ProbeL2' object has no attribute '_poses'`（`tests/d3/sensors/test_multiview.py:733`）。
  - 原因：`ea01e1e`（B10 第 2 轮）为 `L2Sensors._cameras` 新增 EGL 异步 `submit` 路径，该路径经 `_setup` 与 `_finish_camera` 而不再调用 `_render`；测试探针 `ProbeL2` 只覆盖 `_render` 并在其中初始化 `_poses`，因此探针失效。属测试替身与产品接口不同步，未发现产品输出变化。
  - 产品行为的旁证：B10 第 2 轮正式运行 120 个 Run（含 60 个 L2）、60/60 对重复逐流一致，`tests/d3/env/test_mbl_e2e.py` 等端到端用例通过；`rgb1080-rs` 帧时间戳、白名单审计 1,405,992 条消息 0 违规。但 AC-09 的投影、IoU、边缘对齐、掩膜质心等度量需要探针重新对齐后才能在 HEAD 上重现。
  - 同文件其余 11 个用例通过（畸变模型、重映射、卷帘几何、质心与标记投影、联合 z-buffer、远距人像 IoU、边缘对齐），Realistic 档在 sibling 容器方式下仅这 3 项因同一原因失败。
- 证据：
  - I01 主阶段 `i01-tests/junit.xml`（`d3-sensorsvc:dev`，GPU 2）；挂载 docker 套接字的补跑 `i01-sock/junit1.xml`（`d3-compile:dev`，GPU 2，`-k realistic`）；失败栈见两份 junit。
  - 测试计数（合并后）：通过 11、失败 6、预期失败 2。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：修复属测试侧（`ProbeL2` 改为覆盖 `_setup`、`_finish_camera(…, job=None)` 并按 (机号, 帧号) 记录位姿与摘要），预计当日可完成；修复后在 HEAD 上重跑本文件即可转为通过。

### D3-AC-10 跨传感器时间一致（v1.6 按 ADR-198 修订）

- 判定：**按 ADR 通过**；优先级：阻塞；环境：dmax

- 阈值原文（`docs/05` §11）：

> 构造穿越路口的行人（≥ 1.3 m/s）与车辆（≥ 8 m/s）：相机曝光中点与 LiDAR 逐点时刻对应的实体位置均由同一磁带时刻函数在该时刻给出，渲染与求交所用实体位置与时刻函数之差 ≤ 1 mm（LiDAR 按逐点时刻求交，不以位姿节点段中点近似）；传感器读取实体状态一律以帧或点各自的整数 `t_ns` 查询，不跨时刻共享实体状态，实现若引入实体状态缓存则缓存键包含整数 `t_ns`（断言验证）；改变 sim-core stage 次序或时钟档不改变传感器读到的演员状态。注（v1.6，S07）：Env 控制步 100 ms 时有效的 sim-core 时钟档只有 `d1_250` 与 `sandbox100`，`d1_250_l1_125` 被拒绝（637），因此"改变时钟档"以 `d1_250` 对 `sandbox100` 验证，`sandbox100` 只比较相机（M24-FR-005 禁止 100 Hz 物理下的 LiDAR 与 IMU）

- 实测：
  - 按 ADR-198（逐点时刻求交）：相机 `t_mid` 渲染误差 0.044 mm（行人）、0.055 mm（车辆）、0.045 mm（背景行人），LiDAR 最坏 0.019 mm（≤ 1 mm）；同一时刻相机与 LiDAR 之差 1.4e-14 m；改变 stage 次序、时钟档（`d1_250` 对 `sandbox100`）与物理档时演员状态逐位相同；帧末反例 5.4 / 43.0 / 5.6 mm 说明断言有区分力。
  - 测试：通过 5。
- 证据：
  - `tests/d3/sensors/test_time_consistency.py`（`df81710`，S07，dmax GPU 1）；`docs/05` §25.3 第 12 行；ADR-198（用户 2026-10-22 确认）。
- 性能标注（§4.7、ADR-195）：不涉及性能项。

### D3-AC-17 lockstep、确定性与决策时延（v1.3：正式成绩配置）

- 判定：**通过**；优先级：阻塞；环境：dmax

- 阈值原文（`docs/05` §11）：

> 正式成绩配置（Realistic 档 gz `physics` 导出档 + M25 GPU 帧 + L2，基线策略）同一 Run 规格在同镜像、同 CPU ISA、同型号 GPU 上两次运行：gz 位姿逐位一致，Agent 收到的观测流与动作流逐字节一致；故障注入（运行中杀死 gz 实例）后按"重启实例 + 从回合起点重放"恢复，结果与无故障运行逐字节一致；注入 M25 背压（在途上限置 1、人为延迟 50 ms）时两次运行的 `(uav, sensor, seq)` 集合与帧字节逐一一致，且与无背压运行一致；跨环境容差（f32 深度 p90 ≤ 1 mm、像素偏移 ≤ 0.1 px、LiDAR 逐射线 p99 ≤ 1 mm）；`clock_profile` 或平台镜像摘要改变时运行哈希改变；Agent 超时沿用上一动作并计数；声明 2 s 决策时延的动作在 t + 2 s 生效（误差 0 tick），仿真不等待墙钟；平台复现审计作业对抽样 Run 复跑后逐帧摘要一致（Fast 档 CPU L1 的一级 b 确定性由 D3-AC-35 覆盖）

- 实测：
  - 正式配置（Realistic + gz `physics` 导出档 + M25 GPU 帧 + L2，基线策略）：B10 第 2 轮 60 对重复运行（L1 30 对、L2 30 对，M25 GPU 相机帧加 Warp 点云）的 `pose`、`obs/*`、`act/*` 流逐条一致；P05 的 360 s、3 机、5 个变体（含背压 a/b/m25/bp1/bp2）`pose_sha256`、观测与动作流、43,206 帧集合与字节、`frames.digest` 全部 0 差异。
  - 跨 ISA：用户 2026-10-22 决定 C-41 选项 (C)，逐字节可复现只在同 ISA 上成立，跨 ISA 归 D3.x；同 ISA 第二台机器复测（`NPY_DISABLE_CPU_FEATURES` 限定 AVX2）两个场景观测流、动作流、PoseHistory 逐字节一致（C-40）。
  - 测试（合并后）：通过 47、跳过 3。
  - HEAD 上曾出现两项失败，均已排除为环境因素：`test_official.py::test_variants_bitwise_equal_and_summary`（背压运行墙钟 11.25 s 未大于无背压 11.28 s，墙钟不等式在负载下不成立；dmax 同一用例隔离重跑 3/3 通过，各 44.7–45.8 s）；`test_fault.py::test_gz_kill_hang_and_midstep_kill_replay_bitwise`（`chaos` 与 `perf` 标记，I01 gz 阶段使用的 `d3-gz:dev` 镜像为 10-18 构建，早于 P08/P09 插件；改用 `d3-gz:b21` 镜像的 gz 阶段为 64 passed、0 failed）。
- 证据：
  - `tests/d3/env/test_tolerance.py`、`tests/d3/phys/determinism/`（`1652e0d`）、`tests/d3/deploy/test_backpressure.py`、`tests/d3/bench/test_platform_audit.py`；`docs/impl/D3-P0a-正式口径记录.md` §6.2（`ea01e1e`、`7f1b3c2`）；`docs/05` §25.3 第 22 行；补跑 `i01-rerun/official.log`、`i01-gzb21/`。
- 性能标注（§4.7、ADR-195）：确定性条款与负载无关；B10 第 2 轮为 dmax GPU 2 共享卡（P0a 原型口径，ADR-195 决策 (7)）。`perf` 标记的混沌用例按既有结论（FX-SIM2 报告：需在排他性能锁下执行）在负载下可能失败，本次隔离重跑通过。
- 备注：ADR-208（RefTracker 与两档容差，C-20）已确认，生产路径的接线与 AC-17 对拍重做归 RP0b（AC-35）。

### D3-AC-18 评测与四项指标分量（v1.3；v1.4 改称）

- 判定：**按 ADR 通过**；优先级：阻塞；环境：dmax CPU 池

- 阈值原文（`docs/05` §11）：

> 在 `crop-handoff-001` 上，6 种参考策略 × 12 种子（L1）的 EC 均值与 r6 §12.3 差 ≤ 0.1；覆盖扫描策略 AO_real v0（参考相机 oracle；v1.6 更正：为评测时计算的 `ao-oracle-v0-grid@1`，见 §5.6.5）≤ 0.05 且 AO_pos ≥ 0.85；EU v0：L2 证据经实例图关联（IoU ≥ 0.3 或点落在膨胀 3 px 的掩膜内）、L1 证据经 `det_id` 关联，各有构造用例；CE v0：构造"单机取得全部覆盖"用例 `Ĝ = 0`、"3 机均分全覆盖"用例 `Ĝ = 1`、超预算 3 倍用例 `f_res = 0`；无人机与演员最小间距计入安全指标，构造穿越演员包围体的用例 `ranked_valid = false` 且分量记 0；`run_manifest.json` 通过 schema 校验（排名运行的清单只含 `seed_commit`）；`evaluation.json` 对同一输入逐字节一致；30 机 L1 360 s 评测 ≤ 30 s，3 机 L2 360 s 评测 ≤ 120 s

- 实测：
  - r6 对照条款按用户 2026-10-22 决定（C-42 选项 (B)）改为 r6 §14 的"同量级、排序一致"：K10（`128615e`，按 ADR-217 以 r6 观测设置重跑 72 个运行，2026-10-21 19:49–20:32 UTC）EC 均值：hover1_plaza 0.750、hover3_anchors 1.000、lawn3_cover 0.250、track3_nocomm 0.979、track3_share 1.000、track5_share 1.000（r6：0.50、0.75、0.25、0.90、0.88、1.00），与 r6 之差 ≤ 0.1 的有 3/6 行；覆盖扫描行 EC 与 AO_real 在 6 行中均为最低（EC 0.250、AO_real 0.000），track5_share 的 EC 最高（1.000，与 hover3、track3_share 并列）。排序与量级条款成立。
  - 原文其余条款：覆盖扫描 AO_real 0.000（≤ 0.05）、AO_pos 0.976（≥ 0.85），通过；K07 评测器 0.2.0 重评 A10 时曾为 0.0415 与 0.8406，K10 在 r6 观测设置下复核通过。
  - 构造用例与耗时（`tests/d3/bench/`、`tests/d3/eval/`）：EU v0、CE v0（`Ĝ = 0`、`Ĝ = 1`、`f_res = 0`）、穿越演员包围体 `ranked_valid = false` 等用例通过；30 机 L1 360 s 评测中位 10.91 s（≤ 30 s），3 机 L2 360 s 评测中位 2.60 s、B10 第 2 轮最长 5.7 s（≤ 120 s）；`evaluation.json` 对同一输入 120/120 逐字节一致；运行清单 120/120 通过 schema 校验且只含 `seed_commit`。
  - 测试：通过 43、跳过 3。
- 证据：
  - `tests/d3/bench/test_r6_ec.py`（`128615e`）、`tools/bench/d3/r6_ec.py`、`configs/bench.d3/ref6-l1.yaml`；`tests/d3/eval/test_eval_timing.py`（`7eade17`）；K10 记录 `results-B09a/K10.md`，数据 dmax `$D/k10/out`、`$D/a10`；`docs/impl/D3-P-1-实测报告.md` §4.6、§4.6.1；`docs/impl/D3-P0a-正式口径记录.md` §6.2。
  - 放宽条款已由 ADR-218 写入 `docs/05`（HEAD `82ce0ae`）；ADR-218 将 r6 对照出处勘正为 r6 §14，并要求 `r6_ec.py report` 增加排序判据（docs/05 §32.5 F-109 (d)），尚未落地。补记（K15，2026-10-22）：`r6_ec.py report` 已按 ADR-218 增加排序判据（缺省 `--rule adr218`，原文逐行差值保留为参考列与 `--rule original` 回退）并补测试；以 K10 的 72 个运行目录重判（不重跑仿真）：覆盖扫描行 EC 0.250 与 AO_real 0.000 最低，track5_share EC 1.000 最高（与 hover3_anchors、track3_share 并列），观测设置 72 / 72 一致，`verdict.pass = true`；原文口径 3 / 6 行（参考）。见 [D3-P-1-实测报告.md](D3-P-1-实测报告.md) §4.6.2。
- 性能标注（§4.7、ADR-195）：评测耗时为 dmax CPU 容器（`--cpus 16`，未放入 `d3.slice`）上的共享主机数字，阈值余量 2.7–46 倍，仅作参考；EC、AO 为内容类度量，与独占无关。
- 备注：F-86：`b10-formal-r2` 的 `evaluation.json` 由评测器 0.1.0 生成，AO 分量在 0.2.0 下会变化；本条判定不依赖 AO 的 b10r2 数值，封板记录须注明评测器版本，或以 `r6_ec.py reeval` 重评。

### D3-AC-19 录制与回放

- 判定：**通过**；优先级：阻塞；环境：dmax、本机 Tier S

- 阈值原文（`docs/05` §11）：

> 注入背压时 MCAP 写入器阻塞生产者并记录，丢帧 0；帧数据只在 MCAP 中保存一份（`frames.idx` 只含偏移），Agent 收到的 RGB 字节与存档字节 sha256 一致；磁带与世界清单为附件；真值（含真值位姿）在单独文件；内部回放的演员由磁带补出且与磁带位置差 ≤ 5 cm；前端磁带解码器的脚本演员位置与内部回放差 ≤ 5 cm；两个 Run 同步对比时时间轴一致

- 实测：
  - 注入背压时 MCAP 写入器阻塞生产者并记录，丢帧 0（B33 对 130 个正式 Run 的清单统计 `writer_block_frac` 与 `dropped`）；帧数据只在 MCAP 中保存一份；磁带与世界清单为附件，真值在单独文件；内部回放演员由磁带补出，与磁带位置差 ≤ 5 cm；前端磁带解码器与内部回放差 ≤ 5 cm；两个 Run 同步对比时间轴一致（Playwright `lab/mbl`：89 次采样、267 对比较、两侧最大差 0 ns）。
  - 测试：通过 13；前端 `apps/web/tests/lab/replay.test.ts`（16 个用例）与 `tape_replay.test.ts` 随轻量门 web-unit 通过。
- 证据：
  - `tests/d3/bench/test_recorder.py`（`325cd96`）、`test_replay_actors.py`；`apps/web/tests/lab/replay.test.ts`；`docs/05` §31.3 第 95 行；`docs/impl/D3-批量基准记录.md`。
- 性能标注（§4.7、ADR-195）：1 h 生产写负载在 A 级窗口取得（12.48 MB/s，阻塞比例 8.2e-5），属 D3-AC-22（P0b）。

### D3-AC-34 Realistic 档锁步（v1.3：前移到 P0a，缺省档）

- 判定：**按 ADR 通过**；优先级：阻塞；环境：dmax（P0a 共享卡，P0b 独占卡复测）

- 阈值原文（`docs/05` §11）：

> crop `physics` 导出档 SDF：`gz sdf -k` 通过，碰撞 STL 与 raycast 网格同点偏差 ≤ 1 cm，显式重力 9.80665；3 架 x500（`p600_bench` 质量与惯量参数映射）在锁步下由 Env 驱动 360 s：`step_done` 携带每 tick 25 个物理步状态且 `pose_history_hz = 250` 与实测一致；同镜像、同 CPU ISA、同种子两次运行位姿逐位一致；有无 M25 传感器负载时物理轨迹逐位一致；机动时世界系定高误差 ≤ 2 m；`step_done`、帧时间戳与 tick 时刻之差为 0（k = 0 的相机帧除外：L2 以 `t_mid = max(…, 0)` 截断，其窗口末端不是 tick 时刻）；正式评测每 Run 重启实例，`physics` 实例启动 ≤ 5 s（v1.6 按 ADR-216：ADR-033 协议下 ≥ 40 次冷启动加各 Run 实例的 p95，同时报告最差实例）；只计物理与锁步协议时 3 机 RTF ≥ 3.0（v1.6 按 ADR-216 冻结，3 次运行取中位，按实际推进的仿真时长折算）；每实例 CPU ≤ 2 核、内存 ≤ 2 GB、不挂载 GPU；`physics = fast` 的排名请求返回 682（`why = ranked_requires_realistic`）

- 实测：
  - 按 ADR-216 冻结口径，CPU 池正式测量（K04，2026-10-20 05:17–05:34 UTC，ADR-033 排他锁与 ADR-195 决策 (6)）：`physics` 实例启动 43 个样本，p95 3.15 s、最差 3.73 s、中位 2.66 s（≤ 5 s，按 p95 与最差实例均通过）；只计物理与锁步协议的 RTF 中位 3.60（单次 3.13–3.78，≥ 3.0）；每实例 CPU p95 0.49 核、最大 1.02 核（≤ 2），anon 最大 1.265 GiB（≤ 2 GB），不挂载 GPU。
  - 锁步协议：3620 tick（3600 + 预滚 20）、坏步 0、每机 `pose_history_hz` = 250.000；同种子 3 次位姿摘要均为 `89ef00cede24…`；机动定高误差 1.426 m（≤ 2 m）；SDF 导出检查与排名拒绝（682）测试通过。
  - 测试（合并后）：通过 43、跳过 2；gz 阶段改用 `d3-gz:b21` 镜像为 64 passed、0 failed、3 skipped（跳过为浸泡、长跑数据与报告文件的可选项）。
- 证据：
  - `tools/bench/d3/gz_lockstep.py`（`1a89078`、`fab7354`）、`tests/d3/phys/test_gz_export_checks.py`（`29683a3`）、`test_gz_lockstep_long.py`、`tests/d3/api/test_spec_rules.py`；`docs/impl/D3-独占卡性能验收.md` §3.3；`docs/impl/D3-P-1-实测报告.md` §4.5；`docs/05` §29.3 第 71、72 行；ADR-216（用户 2026-10-22 确认）。
- 性能标注（§4.7、ADR-195）：CPU 池正式（ADR-195 决策 (6)，`d3.slice` 内无其他 D3 批作业的窗口）；本次 I01 与回归门运行时 dmax 另有 B21，未重测。

### D3-AC-51 内部工作区部署与角色

- 判定：**通过**；优先级：阻塞；环境：dmax、本机 Tier B

- 阈值原文（`docs/05` §11）：

> `lab-web`、`lab-gateway`、`rerun-proxy` 在 dmax `d3` profile 中运行，只经鉴权反向代理与 ZeroTier 可达（公网端口扫描不可达）；bench token 换取会话 Cookie（HttpOnly、`SameSite=Strict`、12 h）；四类角色的路由与 API 权限矩阵逐项用例通过（越权返回 403 并审计）；Playwright 覆盖 `/lab/worlds`、`/lab/worlds/:wid`、`/lab/events`、`/lab/runs`（状态机、取消、重试、原因码显示）；Rerun catalog 只能经 `rerun-proxy` 以短期令牌访问

- 实测：
  - `lab-web`、`lab-gateway`、`rerun-proxy` 在 dmax `d3` profile 中运行，监听只有 `10.253.20.5:18660` 与 `127.0.0.1:18650-18654`；宿主自身地址扫描公网网卡 lab 端口 0 个开放（22 为阳性对照）；ZeroTier 无令牌核查 12/12、有令牌核查 21/21；会话 Cookie `HttpOnly`、`SameSite=Strict`、`exp − iat` = 43,200 s。
  - Playwright：`lab-roles.spec.ts` 8/8（路由清单、Cookie、4 角色 × 12 路由矩阵、`/lab/worlds`、`/lab/events`、`/lab/runs` 状态机、取消到 CANCELLED 5.1 s、403 与 deny 审计逐键相等）；部署形态复测（`20d6755`，内部工作区按提交快照重建后）lab-inspect、lab-mbl、lab-roles 通过，冒烟 35/35。
  - 测试：通过 310（`tests/d3/lab/`、`tests/d3/deploy/test_lab_profile.py`、`test_gateway.py` 13 个用例）。
- 证据：
  - `tests/d3/lab/test_gateway.py`（`046c0cb`）、`tests/d3/deploy/test_lab_profile.py`、`tests/e2e/lab-roles.spec.ts`（`20d6755`、`77f2b54`）；`docs/05` §27.3 第 50 行、§31.3 第 96 行。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：互联网侧端口扫描须由用户在任一公网主机补做（条款证据在绑定层面给出，见 §4 遗留）；F-96(a)(b)（403 重试产生多条 deny 审计、`/lab/replay` 整页载入时启动遮罩）为体验类发现，不影响判定。

### D3-AC-56 最小基准闭环端到端（首个里程碑）

- 判定：**通过**；优先级：阻塞；环境：dmax（共享卡可）

- 阈值原文（`docs/05` §11）：

> 在 dmax 上以一条命令执行：crop 世界包 + `crop-handoff-001`（三道门 strict 编译通过）+ Realistic 档 gz 锁步 + 3 架 `p600_bench`（P0a 无热成像：`rgb1080-rs` + 倒装 MID-360 + IMU + GNSS + 导航解）+ 3 个基线策略（L1 与 L2 各一组，经 Python SDK 与隔离容器）× 10 个秘密种子 × 2 次重复 → 全部 Run 达到 COMPLETED，生成 `evaluation.json`（EC、EU、AO_real v0 与 CE v0）→ 原型榜（交汇与交接赛道）显示四项指标与置信区间及 3 个基线参考行，1 个内部成员提交的 Agent 镜像经扫描、冒烟、正式评测与评分后出现在原型榜；全部 Agent 收到的消息通过 `bench/obs.v1` 校验（未知字段 0）；样例包（每个基线 1 个代表运行的事件回放、3 机私有视角视频、MCAP 与指标）在内部工作区可浏览；单个正式 Run 墙钟 ≤ 仿真时长 × 3（共享卡，原型）

- 实测：
  - B10 第 2 轮（`b10-formal-r2`，2026-10-21 05:13–13:45 UTC，dmax GPU 2 共享卡，原型口径）：一条命令 `run_mbl_e2e.sh` 完成 world、compile、lockstep、runs、board、submit、report 7 个阶段；120 个正式 Run（L1、L2 × 3 基线 × 10 秘密种子 × 2 次重复）全部首次 COMPLETED，0 次平台重跑；机队 3 架 `p600_bench`；131 个 Run 满足 `iterations = 25 × (n_ticks + 预滚)`，0 次重启；10 项判定全部通过，`formal_passed = true`。
  - 原型榜 `p0a-formal-r2-l1`、`p0a-formal-r2-l2` 各显示四项指标、95% 置信区间与 3 个基线参考行；内部提交 `d3-example-close-track:b10` 经扫描、冒烟、10 个秘密种子正式评测与评分后以层级 1 出现在原型榜（条目 `sub-olenynk5qm7amst4`）；132 个 Run 共 1,405,992 条消息通过 `bench/obs.v1` 校验，未知字段 0。
  - 单个正式 Run 墙钟比（≤ 3，共享卡原型）：L1 0.584–0.852，L2 2.120–2.491（中位 2.310），提交 2.129–2.240。
  - 样例包 `p0a-b10-formal-r2`（B20）：3 基线 × L1、L2 共 6 个代表运行，事件回放、每个 L2 运行 3 路 1080p H.264 私有视角视频、`raw.mcap` 与指标；秘密种子检索命中 0，`--verify` 45 个文件 0 个不符；Playwright `lab/mbl` 3/3 通过，部署形态复测通过（`20d6755`）。
  - 测试：通过 22。
- 证据：
  - `tools/bench/d3/mbl_e2e.py`（`ea01e1e`）、`tools/deploy/dmax/run_mbl_e2e.sh`、`tests/d3/env/test_mbl_e2e.py`、`tests/d3/bench/test_sample_pack.py`（`ab6aa8c`）、`tests/e2e/lab-mbl.spec.ts`；报告 dmax `$D/b10r2/out/b10-formal-r2/mbl_e2e.json`；`docs/impl/D3-P0a-正式口径记录.md` §0、§6.1；`docs/05` §30.3 第 86 行、§31.3 第 94、95 行。
- 性能标注（§4.7、ADR-195）：共享卡原型（ADR-195 决策 (7) 允许 P0a 性能类测量在共享卡执行并标注原型）；墙钟比不作 A 级正式数字，榜单配置标注 `prototype = true`、`gpu_class = shared`。
- 备注：F-86：该榜单的 `evaluation.json` 为评测器 0.1.0 生成；F-95：榜单接口尚未下发"原型""共享卡"标签（页面标题为"原型排行榜"）。

### D3-AC-57 赛道指标、置信区间与排行榜规则（v1.4 按 ADR-191 修订）

- 判定：**通过**；优先级：阻塞；环境：dmax CPU 池

- 阈值原文（`docs/05` §11）：

> 各赛道的四项指标由 `evaluation.json` 存储的分量按 `score_version` 聚合，与排行榜显示值逐字节一致；评分版本或赛道集合变更产生新榜单版本，旧版本只读；95% 置信区间以两阶段分层自助法逐项计算，同一输入两次计算逐字节一致；`weights = null` 时任何接口响应与页面中不出现合成主得分；构造用例：(a) 两条目某项指标的配对差值区间含 0 时该项显示同一名次并标注"统计并列"；(b) 非支配层级按 §5.6.9 计算，构造的支配与不可比用例分层正确，层级不显示为名次；(c) 无效 Run 占比 21% 的条目在该赛道标注"无效"，不参与名次与分层；(d) 平台原因失败的 Run 自动重跑且不计入提交方；(e) 同一团队在同一"分轨 × 赛道"只显示代表条目；(f) Fast 档、SITL 或开发运行不进入排行榜；(g) 不同分轨、不同赛道、不同外观层的条目不混排、不合并；(h) 合成接口：以测试用评分版本给定权重时，`compose` 由存储的分量重算出合成值与置信区间，两次结果逐字节一致且无需重跑；单分轨 100 个条目、4 个赛道的榜单重算 ≤ 60 s

- 实测：
  - 两个榜单版本各连算两次，`board.json` 与 `details.json` 与已发布文件逐字节一致；参考条目四项指标与由 `rows.jsonl` 复算的值一致；`weights = null` 时任何响应与页面均无合成主得分；构造用例 (a) 至 (h)（统计并列、非支配层级、无效占比 21%、平台失败自动重跑、代表条目、Fast 档与开发运行不入榜、分轨与赛道不混排、合成接口 `compose` 重算逐字节一致）由 `tests/d3/board/` 覆盖；单分轨 100 条目、4 赛道重算 ≤ 60 s。
  - 测试：通过 95。
- 证据：
  - `tests/d3/board/`（`5bb4d9f`：`test_score.py`、`test_rank.py`、`test_compose.py`、`test_rank_extra.py`）、`tests/d3/api/test_bench_board_api.py`、`apps/web/tests/lab/board.test.ts`；`docs/impl/D3-P0a-正式口径记录.md` §6.2。
- 性能标注（§4.7、ADR-195）：榜单重算耗时为 dmax CPU 池共享主机数字，余量大，仅作参考。

### D3-AC-58 Python SDK 与基线策略

- 判定：**按 ADR 通过**；优先级：阻塞；环境：干净主机、dmax

- 阈值原文（`docs/05` §11）：

> `pip install drone4d`（或 Agent 基础镜像）后，按文档在 15 min 内以 `make(..., physics="realistic")` 与 `physics="fast"`（P0b 起）运行示例 Agent，同一 Agent 代码在两档与 L1、L2 上不改动；`static_guard`、`coverage_scan`、`track_share` 在 L1 与 L2 上都可运行，同一种子两次运行动作流逐字节一致；L2 参考检测器 `ref_det_v1` 只用 CPU 且在容器 `std` 资源档内满足每步墙钟预算；在 `crop-handoff-001` 上 `track_share` 的 EC 均值高于 `coverage_scan`，且在交汇与交接赛道上三个基线至少在一项指标上不全部两两统计并列（v1.4）；基线的 report 均附证据并通过 `bench/act.v1` 校验；SDK 与基线源码随文档发布，文档通过 copy-lint

- 实测：
  - 干净主机以 dmax 全新容器代替（ADR-202）：全新 `python:3.12-slim`（amd64）容器内从容器启动起算 45.0–51.2 s（≤ 900 s）完成示例 Agent；同一 Agent 代码在 `physics="realistic"` 与 `"fast"`、L1 与 L2 上不改动。
  - B10 第 2 轮：`track_share` 的 EC 均值高于 `coverage_scan`（L1 1.000 对 0.400，L2 1.000 对 0.325）；L1 四项指标、L2 的 EC、AO、CE 至少有一对基线的配对差值区间不含 0；动作流 60/60 对逐字节一致；1,426 条 report 全部附证据并通过 `bench/act.v1` 校验（L1 704、L2 722），动作协议错误 0。
  - `ref_det_v1` 在 `std` 档每步墙钟 p95 0.116 s、最差 0.181 s（预算 0.5 s），超时 0。
  - 测试（合并后）：通过 66、跳过 1（`tests/d3/agentsdk_pkg/` 的 wheel、sdist、可复现构建与干净 venv 运行，在默认 `d3-sensorsvc` 镜像中因缺 Node 与 `rsync` 而报错，改用 `d3-compile:dev` 补跑通过）。
- 证据：
  - `tests/d3/agentsdk_pkg/`（`a52c13c`）、`tests/d3/baselines/`（`128615e`）、`tests/d3/phys/test_fast.py`；`docs/impl/D3-SDK-上手演练记录.md`；`docs/impl/D3-P0a-正式口径记录.md` §6.2；`docs/05` §24.3 第 8 行、§26.3 第 35 行；ADR-202（用户 2026-10-22 确认）。
- 性能标注（§4.7、ADR-195）：`ref_det_v1` 每步墙钟为 dmax 3 个 `std` 容器并发、3 次取中位（ADR-033）的数字；干净主机时长为 CPU 池数字。均有 4 倍以上余量。

### D3-AC-60 卷帘快门与 1080p 多机输出

- 判定：**不通过**；优先级：阻塞；环境：dmax（单卡绑定）

- 阈值原文（`docs/05` §11）：

> `rgb1080-rs`（读出 20 ms）在 8 m/s 平移叠加 90°/s 偏航的构造轨迹上，逐顶点方法渲染的已知标记顶点像素位置相对 Warp 逐行射线参照的误差 p99 ≤ 0.5 px，行带方法（16 带）误差 ≤ 1.5 px；深度、实例、语义与 RGB 的边缘对齐误差 ≤ 0.5 px；`CameraObs.t_mid_ns` 为中间行曝光中点、`readout_ns = 20,000,000`；静止机体时卷帘与全局快门输出逐字节一致；3 机 × 1080p @ 10 Hz 时 RGB 经 GPU nvJPEG 编码，在线路径无 CPU 编码调用，Agent 收到的字节与存档字节 sha256 一致；1080p 单帧体积实测写入实现报告

- 实测：
  - 判定理由是证据回归：`tests/d3/sensors/test_1080p.py` 在 HEAD 上 2 项失败：`test_1080p_three_uav_online_end_to_end`（`TypeError: _probe_l2.<locals>.ProbeL2._finish_camera() got an unexpected keyword argument 'job'`）与 `test_1080p_frame_size_viewpoints`（`TimeoutError: readback ring: no free slot`）。
  - 第一项为测试探针与 `ea01e1e` 之后的 `L2Sensors._finish_camera(…, job=None)` 签名不同步，同 AC-09。第二项暴露产品边界：新异步路径在同一 tick 内先提交全部机体的绘制再按序取回，读回环缺省 6 个槽位，该用例的固定视点网格一次提交的机数多于槽位数，环耗尽后等待 120 s 超时；P0a 机队（3 架）与 L2 上限不受影响，但 L2 一个 tick 内的机数超过 6 时会死锁，需要分批提交或按机数配置槽位。
  - 条款其余部分通过：卷帘几何（S06，3 条构造轨迹、373 个标记）逐顶点 p99 0.034 px、最大 0.042 px（≤ 0.5 px），16 带最大 0.99 px（≤ 1.5 px），p99 0.89 px；边缘对齐 ≤ 0.031 px；全局快门对照 p99 9.6–21.4 px；静止机体逐字节相同；`readout_ns = 20,000,000`；`test_rolling_shutter.py` 12 个用例全部通过；在线路径 CPU 编码调用 0、Agent 收到字节与存档字节 sha256 一致（K08 的 3 × 1080p 在线与 B10 第 2 轮 L2 运行佐证）；1080p JPEG 单帧体积 realistic 均值 700 kB、最大 1,083 kB（视点网格），3 机在线均值 521 kB、最大 710 kB，已写入 `docs/impl/D3-相机实测记录.md`。
  - 测试（合并后）：通过 15、失败 2。
- 证据：
  - `tests/d3/sensors/test_rolling_shutter.py`（`c323492`）、`test_1080p.py`（`63989a5`）；`docs/impl/D3-相机实测记录.md`；`docs/05` §25.3 第 11、17 行；失败栈见 `i01-tests/junit.xml`、`i01-compile/junit.xml`。
- 性能标注（§4.7、ADR-195）：几何度量与负载无关；JPEG 编码时延（p50 2.1 ms、p99 2.7 ms）为共享卡开发参考。
- 备注：修复分两部分：探针签名（测试侧）与 L2 读回槽位的分批提交（产品侧，建议在 `_cameras` 中按 `ring.slots` 分批）；两者修复后重跑本文件。

## 3. 不通过项与封板阻塞

| 编号 | 条款 | 现状 | 阻塞原因 | 处置与预计 |
|---|---|---|---|---|
| D3-AC-01 | D2 零影响回归门 | 重载门 zero-impact、resim、imports 通过；pytest 步骤、perf 步骤不通过；轻量门 lint、demo-parity 不通过 | 见 §1.1、§1.2 与 §2 本条：4 项均有已知原因，零影响对拍本身（核心条款）通过 | lint：提交代理移出 `m1geo_full.parquet`（F-58）；demo-parity：执行 ADR-209（`d2` 分支与 `v0.2.2`）；pytest 步骤：收窄到 D2 范围或补齐依赖（F-114）；perf：B21 完成后的无 D3 作业窗口复测（约 3 min）。以上完成后重跑 `make gate-d3-dmax` 与 `make gate-d3-light` |
| D3-AC-09 | 多机多视角观测 | 6 个参数化用例因探针失效失败，其余通过 | HEAD 上测试证据不可重复（ADR-194 决策 (3)）；未发现产品输出变化 | 修复 `ProbeL2`（测试侧，预计 0.5 人日），重跑 `tests/d3/sensors/test_multiview.py`（dmax GPU 单卡；Realistic 部分需挂载 docker 套接字） |
| D3-AC-60 | 卷帘快门与 1080p 多机输出 | `test_1080p.py` 2 项失败，卷帘几何与其余条款通过 | 同上；另有 L2 读回环 6 槽位的产品边界 | 修复探针签名；`L2Sensors._cameras` 按槽位数分批提交并补用例（约 0.5 人日） |

不通过项共 3 条。三条均不是阈值未达，而是 HEAD 上回归门或证据测试为红；修复后可在同一环境重跑转为通过或按 ADR 通过。

不计入"不通过"但封板前仍须完成：D3-AC-46（发布就绪，须执行 ADR-209 并在上线后复核）；D3-AC-18（ADR-218 已并入 `docs/05`；`r6_ec.py` 的判据与测试须随之修订，F-109 (d)；K15 已完成，见 [D3-P-1-实测报告.md](D3-P-1-实测报告.md) §4.6.2）。

## 4. 遗留

### 4.1 需要用户处理

| # | 事项 | 说明 |
|---|---|---|
| 1 | 互联网侧端口扫描（D3-AC-51） | 条款证据在绑定层面给出（宿主自身扫描、ZeroTier 核查），互联网侧扫描须在任一公网主机对 dmax 公网地址执行，期望 lab 端口 0 个开放 |
| 2 | 安排 perf 复测窗口 | 回归门 perf 步骤须在 dmax 上无其他 D3 作业（B21 正式评测）时运行，约 3 min；B21 结束即可，无须独占 GPU |
| 3 | 授权并启动 ADR-209 执行 | C-02、C-24、C-25 已确认；`d2` 分支重建、`v0.2.2` 标签与 GitHub release 是对外可见动作，按执行单由提交代理执行（D3-AC-46、D3-AC-01 的 demo-parity 都依赖它） |
| 4 | 复核 ADR-218 的工具跟进 | ADR-218 与 §11.2 修订已并入 `docs/05`；`r6_ec.py report` 排序判据与 `test_r6_ec.py` 属 docs/05 F-109 (d)，封板前须落地；K15 已完成（[D3-P-1-实测报告.md](D3-P-1-实测报告.md) §4.6.2） |

### 4.2 工程遗留（提交代理与后续任务）

| # | 事项 | 归属 |
|---|---|---|
| 1 | `git rm --cached research/ccf-tpci/data/m1geo_full.parquet` 并登记 `external.json`（F-58），使 `make lint` 与 `check-release` 转绿 | 提交代理 |
| 2 | 修复 `ProbeL2`（`test_multiview.py`、`test_1080p.py`）与 L2 读回环分批提交（F-116） | M25、M26 |
| 3 | I01 入口修复（F-113）：主阶段基底改 `d3-compile`、加入 `datafusion`、主机阶段、gz 镜像更新、按阶段汇总；本次已用补跑覆盖，补跑方式见 §5 | I01、镜像 |
| 4 | 回归门 pytest 步骤范围与 `nofile` 限制（F-114） | G05 |
| 5 | `r6_ec.py` 判据与 `test_r6_ec.py` 随 ADR-218 修订；`b10-formal-r2` 的评测器 0.1.0 说明或重评（F-86） | M27 |
| 6 | B33 遗留（F-121）：`finalize_ranked_eval` 接入、LiDAR `frame_id` 去重、`truth_sha256` 与精简冲突 | RP0b、M27 |
| 7 | ADR-208（RefTracker，C-20）生产路径接线与 AC-17 对拍重做 | RP0b（D3-AC-35） |
| 8 | 默认 `d3-gz:dev` 镜像陈旧（`test_gz_wind` 2 项失败，`d3-gz:b21` 通过）：重打 `d3-gz:dev` 标签或让 I01 取当前镜像 | 镜像 |
| 9 | F-95（榜单接口下发"原型""共享卡"标签）、F-96、F-98、F-99（文案必改项已由 K09 关闭，建议项 W-08 等未处理） | M15、M27 |
| 10 | geosvc 服务进程 RSS：crop 907 MiB > M22-NFR-005 的 700 MiB（F-120；选择性用例本次首次在 HEAD 上运行，D3-AC-05 条款不受影响）。需要所有者核对是否为 K06 重建后的瓦片表增长，并决定修订预算或优化 | M22 |
| 11 | dmax 封板后统一清理（F-85）；本任务的证据副本 `$D/out/rp0a-evidence` 封板后可删 | 部署 |

### 4.3 范围外的说明

- Tier B 参考设备相关条款（D3-AC-15、21、25、51 的 Tier B 部分）：按用户 2026-10-22 决定本次不测试，记"该项值得测试，以后再测"，移入 D3.x 顺延清单；封板时以 dmax 无头 Chromium 开发参考数字记录并注明非正式。本报告判定的 D3-AC-51 不依赖 Tier B。
- D3-AC-11 在 P0b，本报告按指示采用 K08 的 A 级独占结果（渲染进程组，16 项条款全部通过，C-44 已确认）引用于 D3-AC-47；其逐条判定归 RP0b。
- 本任务期间 B21 仍在 dmax 运行，P0b 项（D3-AC-43 等）不在本报告内。

## 5. 产物与复现

本机证据根（会话临时目录）：`/tmp/claude-1001/-data-projs-anet-drone/6752f3ab-4a1a-4ad5-891b-7b68d98ce213/scratchpad/d3close/results-close/`；dmax 副本：`/data/anet-drone4d-d3/d3dev/out/rp0a-evidence/`。

| 内容 | 路径（相对证据根） |
|---|---|
| 重载门第 1 次完整运行（pytest、zero-impact、resim、perf、imports） | `gate-evidence/653ac8e36175/heavy/`（`report.json`、`meta.json`、各步骤日志）；控制台 `RP0a-gate.log` |
| 重载门 perf 复测 | `gate-evidence-perf2/653ac8e36175/heavy/`；`RP0a-gate-perf2.log` |
| 回归门 pytest 步骤逐用例诊断 | `gate-pytest/`（`junit.xml`、`pytest.log`） |
| 轻量门 | `gate-evidence/653ac8e36175/light/`；`RP0a-gate-light.log` |
| public-e2e | `gate-evidence-e2e/653ac8e36175/light/`；`RP0a-gate-e2e.log` |
| I01 主阶段与 gz 阶段 | `i01-tests/`（`counts.json`、`junit.xml`、`junit-gz.xml`、`pytest.log`）；`RP0a-i01.log` |
| I01 补跑：`d3-compile`、datafusion、主机、docker 套接字、gz `b21`、隔离重跑 | `i01-compile/`、`i01-compile2/`、`i01-labdf/`、`host-deploy-junit.xml`、`host2-junit.xml`、`host3-junit.xml`、`i01-sock/`、`i01-gzb21/`、`i01-rerun/` |
| 按用例合并与逐条计数 | `ac_counts.py`、`ac_counts.json` |

复现命令（本机仓库根目录，`GIT_DIR=/data/projs/.anet-drone4d.git GIT_WORK_TREE=/data/projs/anet-drone`）：

```
make gate-d3-dmax  GATE_REF=c075fd716dceed448da71103151dcd156ce1eee4    # 重载门，约 73 min
make gate-d3-light GATE_REF=c075fd716dceed448da71103151dcd156ce1eee4    # 轻量门，约 6 min
make gate-d3-light GATE_REF=c075fd716dceed448da71103151dcd156ce1eee4 GATE_D3_LIGHT=public-build,k9-d3,public-e2e   # public-e2e，约 6 min
GPU=2 TASK_ID=rp0a tools/deploy/dmax/run_d3_tests.sh                    # I01，须在 HEAD 的 git archive 快照目录中运行才得干净标签，约 26 min
```

dmax 使用与清理：仅使用 `d3-*` 容器（`d3-test-gate-main`、`d3-test-rp0a`、`d3-test-rp0a-b`、`d3-test-rp0a-f`、`d3-rp0a-*`），均 `--rm`，工作目录限于 `/data/anet-drone4d-d3/d3dev/`；回归门与测试容器在 `d3.slice` 或 CPU 池，I01 与补跑仅占用 GPU 2（运行前该卡无外部进程）；未做机器级 `docker prune`；临时容器与临时目录已清理。
