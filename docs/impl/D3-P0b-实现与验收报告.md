# D3 P0b 实现与验收报告（RP0b）

| 项 | 内容 |
|---|---|
| 任务 | RP0b P0b 实现与验收报告（D3 收尾与封板，封板合并之后；覆盖 `docs/05` §11.3 的 P0b 全部验收条款，并给出 41 条封板范围验收的汇总，见 §6） |
| 被测提交 | `main` 合并提交 `88bedb2ce9edca7d62e4cc4636ebfed780510b7e`（父提交 `origin/main` 的 `705a1be` 与 `d3-dev` 的 `1cea12c`；git tree id 前 12 位 `124c22c6bb23`，与 `d3-dev` 同树）。回归门取该提交的 `git archive` 快照，不取工作树 |
| 日期 | 2026-10-25（UTC）；本报告直接测量的时段 08:29–09:36 UTC（`/lab/` 构建、回归门、emax 上线实测与回滚演练）；各条款的逐项测量取自 2026-10-18 至 2026-10-25 各批次的正式记录（`docs/05` §24 至 §33），逐条标注批次与日期 |
| 依据 | AWR-05（`docs/05`）v1.6 §11.3（阈值原文取自被测提交）；ADR-194（封板范围与规则）、ADR-195（独占卡口径）、ADR-207、ADR-213 至 ADR-216、ADR-218、ADR-219、ADR-220；用户 2026-10-22 决定（ADR-194 至 217 与 C-01 至 C-52 已确认；D3-AC-18 采用"同量级、排序一致"；Tier B 设备项移 D3.x）；§23.5 的 C 项确认表、§33.5 的 F 项遗留登记；`docs/impl` 下的正式记录（D3-独占卡性能验收、D3-SLAM-实测记录、D3-相机实测记录、D3-批量基准记录、D3-排行榜v1-正式评测记录、D3-排行榜v1-重评记录、D3-评测运行保留接入记录、D3-正式运行稳健性与安全、D3-文案评审记录、D3-G3DGS-评审记录）；封板上线实测证据 `LIVE-EVIDENCE.md` |
| 范围 | P0b：D3-AC-11 至 16、20 至 25、33、35、43、59、61 至 64，按 `docs/05` §11.3 与 ADR-194 决策 (1) 共 20 条（阻塞 18 条，应交付 2 条：D3-AC-63、64）。P-1 与 P0a 的判定沿用 [D3-P0a-实现与验收报告.md](D3-P0a-实现与验收报告.md)（RP0a），仅在 §6 汇总中按 `docs/05` §33 的后续关闭记录更新 |
| 口径 | 性能类数字只采信 A 级独占卡或 CPU 池正式测量（ADR-195）；共享卡与共享主机数字逐项标注为开发参考；"通过"指有可重复的测试计数或文档化测量（ADR-194 决策 (3)）；被测提交上直接执行的只有回归门（§1）与 emax 上线实测（§2 D3-AC-21 及相关条款）。各条款的功能与性能证据取自其采集批次的提交，`tests/d3` 全量测试（I01）未在被测提交上重跑，最近一次全量结果为 K14 的 2,460 个用例（§1.4）。本报告对找不到证据的条款一律写"未找到证据" |
| 状态 | 判定为本任务的工程判定。D3-AC-21 不通过，为当前唯一的实测不通过项，也是封板阻塞项，处置选项在用户（§3）。ADR-220、C-53、C-54 为"先行生效，待用户确认"，相关判定已逐处标注 |

## 0. 摘要

> **封板判定修订（2026-10-25 11:00 UTC 起，§7）**：用户 2026-10-25 决定采用 ModelScope 外部分发并提供令牌（ADR-221），以及证据补测不在封板前执行、已决定顺延的子条款按 ADR 通过（ADR-222）。按 ADR-221 重新上线后公网复测通过，D3-AC-21 改判**通过**；本节下表中 8 条"部分通过"改判**按 ADR 通过（ADR-222）**，补测事项登记为 D3.x 顺延（`docs/05` §34.5）。下表与 §2、§3 保留被测提交 `88bedb2` 时的原始判定，修订后的判定以 §7 为准。

P0b 共 20 条验收（D3-AC-11 至 16、20 至 25、33、35、43、59、61 至 64）判定计数：通过 7；按 ADR 通过 4；部分通过 8；不通过 1；发布就绪 0；移 D3.x 0。

| 编号 | 条款 | 判定 | 依据 |
|---|---|---|---|
| D3-AC-11 | 相机档位吞吐（v1.3 按档位重算） | 按 ADR 通过 | K08 渲染进程组 A 级独占，16 项条款全部计入并通过；依 ADR-213 (2)、ADR-214 (4)；3 并发条款在渲染进程数上限形态下的 A 级复测未做，C-54 待确认 |
| D3-AC-12 | LiDAR 并发 | 按 ADR 通过 | 100 架 p95 14.06 ms、400 架 p95 53.06 ms（A 级）；逐射线按 ADR-207 计数，C-19 已确认 |
| D3-AC-13 | LiDAR/IMU 输出合同 | 部分通过 | 合同主体通过；`slam_cam` 编码器噪声用例仍为 xfail（F-52），C4 在 realistic 档的口径未见冻结 ADR |
| D3-AC-14 | FAST-LIVO2 RPE 区间 | 按 ADR 通过 | ADR-215 冻结表：主组 LIVO clean 0.054%、realistic 中位 0.112%、HKisland03 ATE 1.043 m |
| D3-AC-15 | 多机 LiDAR 采集与重建对比 | 部分通过 | 多机采集与地图条款 10 项判定全部通过；对比视图的 Playwright 与 Tier S、Tier B 显示未找到证据 |
| D3-AC-16 | 观测隔离无泄漏 | 通过 | A06 至 A08、K18：检索命中 0，容器对 96 个目标连接全部失败，排名运行种子明文残留 0 |
| D3-AC-20 | Rerun 派生与回放 | 通过 | 公开切片 4.97–7.80 MB，底图 5.68 MB（舞台裁剪）与 23.55 MB（全世界），跨 3 个 Run 位姿查询最大 0.395 s |
| D3-AC-21 | 公开站只读样例视图 | 不通过 | 12 个子条款中 WS 推送时延 p95 增幅（≤ 10%）实测 +66.7% 至 +338.8%；D2-AC-04 容量用例线上复测未找到证据；封板阻塞 |
| D3-AC-22 | GPU、存储、评测窗口与背压 | 部分通过 | 写速率、阻塞、并发、保留接入通过；证据帧条款开放（F-107），30 天到期精简尚无定时作业（F-125） |
| D3-AC-23 | 企业级文案 | 通过 | 严格模式 0 违规，须改正项全部关闭，评审与裁决经用户 2026-10-22 确认 |
| D3-AC-24 | 许可与署名 | 部分通过 | 仓库侧与上线版本目录通过；镜像扫描的归档为 2026-10-18 旧标签与旧镜像，被测提交上的复扫未找到证据 |
| D3-AC-25 | Rerun 数据检视与时间轴同步 | 按 ADR 通过 | ADR-219：两项首帧 ≤ 5 s 移入 D3.x；其余条款通过；封板须记录的 dmax 无头 Chromium 开发参考数字未找到证据 |
| D3-AC-33 | OSM 车道图与车辆运动 | 通过 | 走廊采样比例 1.0，车距违例 0，两机车辆生成哈希一致 |
| D3-AC-35 | Fast 与 Realistic 两档一致性与 gz 健壮性 | 部分通过 | 一致性用例 9/9、同 ISA 复测通过；GPU 相机逐字节一项未找到证据，ADR-208 的生产接线与 D3-AC-17 对拍重做未完成 |
| D3-AC-43 | 内部排行榜 v1 与内部提交 | 部分通过 | 800 次正式运行、14/14 判定、v1.1 重评通过；4 次平台原因运行待独占卡窗口重跑（F-124） |
| D3-AC-59 | 受邀成员提交与容器加固 | 部分通过 | 红队 26 项、registry 30/30、扫描门通过；ZeroTier 策略未应用（用户决定封板后办理），registry 接入门的 dmax 部署演练未找到证据 |
| D3-AC-61 | 热成像 | 通过 | 条款登记用例覆盖全部分句；dmax 两进程逐字节一致，99/99 |
| D3-AC-62 | 第一批四类事件 | 通过 | 13 个事件（dev 5、test 8）strict 编译通过，四类基线评测完成 |
| D3-AC-63 | 数据集导出（应交付） | 部分通过 | 导出器与 16 个用例通过；条款要求的 RP0b 正式核验未执行，真实运行的热成像导出无法核验（F-56） |
| D3-AC-64 | 高斯外观层评估与决策备忘（应交付） | 通过 | 结论 C0，G1 至 G8 逐项测量或引用，G5 补测通过，ADR-206 冻结 |

封板阻塞项：D3-AC-21（实测不通过，处置选项在用户，见 §3）。其余 7 条"部分通过"的阻塞级条款与应交付条款 D3-AC-63 按 ADR-194 决策 (3)（阻塞项全部通过；P0b 应交付项须 2/2）同样不满足封板条件，需要工程补证据或由用户依决策 (3) 裁定，见 §3。D2 零影响回归门在被测提交上全绿（§1）。

## 1. 回归门与上线实测

### 1.1 被测快照

合并提交 `88bedb2ce9edca7d62e4cc4636ebfed780510b7e` 的父提交为 `origin/main` 的 `705a1be` 与 `d3-dev` 的 `1cea12c`（"docs(d3) 第十批并入"），已推送 `origin/main`。合并前对 `origin/main..d3-dev` 新增行做密钥扫描：命中 11 处均为令牌前缀检查与测试用的占位令牌，无真实凭据；禁入路径（`worlds/`、`data/raw/`、`refs/`、`.cache/`、`runs/`、`var/`）0 个。门的元数据（`meta.json`）：`ref = 88bedb2`，`dirty = false`，`tree_changed = false`，`tag = tag_after = 124c22c6bb23`。

### 1.2 重载门（dmax CPU 池）

命令：`make gate-d3-check GATE_REF=88bedb2 GATE_TIER=full` 的重载部分（步骤 pytest、zero-impact、resim、perf、imports）。容器 `d3-test-gate-main`（镜像 `sha256:7cd2988c4334…`，Python 3.12，D2 锁文件离线 venv，`--cpus 16 --memory 48g`，不挂 GPU，`d3.slice`，`--ulimit nofile=1048576`，`--shm-size 2g`）。开始 2026-10-25 08:31:23 UTC，结束 09:36:14 UTC，墙钟 64 min 51 s；报告 `pass = true`，退出码 0。

| 步骤 | 结果 | 耗时 | 要点 |
|---|---|---|---|
| pytest（D2 范围，`-m "not perf"`，`--ignore=tests/d3`，4 个依赖 npm、前端生产构建或 make 的用例 `--deselect` 并由其他步骤覆盖） | 通过（按 ADR-220） | 3,374.4 s | 首轮 4 failed、4,573 passed、116 skipped、66 deselected（3,293.52 s）；4 个失败用例各经 1 次隔离重跑通过（`tests/e2e/test_identity_e2e.py::test_r79_constant_time`、`tests/e2e/test_sandbox_sdk_e2e.py::test_scenario2_example_script`、`tests/rt/test_interest.py::test_interest_push_union_and_detail_bytes`、`tests/sensors/test_pose_chain_e2e.py::test_roster_sensors_pose_topic_and_state_ext`），首轮失败数 ≤ 8 且无收集与会话级错误。ADR-220 为先行生效、待确认（C-53）；上述 4 个用例是否在 D2 标签上同样不稳定，门报告只标为"负载敏感"，未见 D2 标签上的对照记录 |
| zero-impact | 通过 | 175.3 s（`AWR_D3_ENABLED=0`）、169.0 s（`=1`） | `relay_batch.py --case b --hours 1 --seed 11 --determinism`：`relay_stats.json` sha256 `c7354dc2fa9b07b451ba70a50acd36140d7d9a40fb64a81c98de047578b9370f`、`events.jsonl` sha256 `88de54fa95083adbf555cd910a9bdb6e1f63ad89c072b355a9a0072042882b9e`，两档相同且与 D2 基线相同（`diffs = []`）；两档 `test_determinism.py` 通过；`gaps.non_exempt` 与 D2 基线同样不通过且取值相同（差分口径，`same-as-d2`） |
| resim | 通过 | 4.1 s × 2 | 两档 3,750 帧，`frames_sha256` `f6d5a3fe6952d0d83c3a7836201c06acf8c9da606ca257d88ce7fd8f3705d4d0`，输入序列 5 条，`first_diverging_chunk = null` |
| perf（D2 性能子集对基线 ≤ 5%，7 次中位，线程 CPU 时间） | 通过 | 65.4 s | 逐指标比值见下表；执行时每核归一负载 0.437（64 逻辑核，1 min 负载 27.97），未加排他锁 |
| imports | 通过 | 8.1 s | `tools/lint/check_py_imports.py`，836 个文件 |

perf 步骤逐指标比值（基线为 D2 录制值，判据 ≤ 1.05）：

| 用例 · 指标 | 基线（D2） | 本次 | 比值 |
|---|---|---|---|
| `tests/targets/test_budget.py::test_stage_budget_30x12` · stage_cpu_ms_per_sim_s | 8.7256 | 9.0376 | 1.0358 |
| 同上 · call_cpu_ms | 1067.292 | 1110.027 | 1.0400 |
| `tests/perception/test_budget.py::test_stage_cost_12x30` · call_cpu_ms | 261.994 | 260.562 | 0.9945 |
| `tests/perception/test_field_budget.py::test_field_cost_12x30_per_sim_second` · call_cpu_ms | 4424.172 | 4602.892 | 1.0404 |

说明：最大比值 1.0404，在判据内。用例自身的绝对预算断言在 dmax 上对 D2 同样失败（`test_stage_budget_30x12` 与 `test_field_cost_12x30` 的 outcome 为 failed，`pytest_exits` 全为 1），门按比值而非绝对值判定，已登记为 F-123 (b)。本次每核负载 0.437 高于 F-123 (c) 建议的 0.4 门限，登记的经验是 0.477 时曾出现比值 1.082 不通过、0.34 至 0.35 时通过；本次结果在判据内，但读数时应注明负载偏高。

### 1.3 轻量门（本机，tier full）

本机（Ink93）快照 `src-124c22c6bb23`，开始 08:31:30 UTC，结束 08:41:23 UTC（9 min 53 s）；报告 `pass = true`，退出码 0。

| 步骤 | 结果 | 耗时 | 要点 |
|---|---|---|---|
| contracts（`make test-contracts`） | 通过 | 116.2 s | 日志末行 527 passed、3 skipped（78.26 s） |
| web-unit（`vitest --project unit`） | 通过 | 52.0 s | 187 个文件通过、4 个跳过；1,681 个测试通过、25 个跳过 |
| lint（`make lint`） | 通过 | 53.0 s | REL-01（`m1geo_full.parquet`）已由 K14 的停止跟踪解决；ruff、oxlint、lint-m06、copy-lint 严格模式、no-emoji、check-brand、check-release、registry（143 个用例）通过 |
| public-build（`make demo-build` 与 K9 prod-bundle-scan） | 通过 | 7.2 s | 演示构建 66 个文件，禁用字符串 0，测试文件 0，真值文件 0 |
| k9-d3 | 通过 | | 公开构建 24 个文件，D3 入口字符串（`/lab/`、`/api/bench/v1…`）命中 0 |
| demo-parity | 通过 | 13.0 s | 对 `v0.2.2` 构建 66/66 个文件逐字节一致（`only_a` 0、`only_b` 0、`differ` 0） |
| lab-build（`make lab-build`） | 通过 | 8.2 s | 29 个文件，字符串 0，测试文件 0，真值文件 0 |
| public-e2e（本机 cgroup 模拟 emax，Tier S） | 通过 | 168.8 s | 落地页（6 个分块）、World Hub（7 个世界）、共享展示 viewer（8 架）、访客沙盒、`/lab/` 静态加载（14 个请求）、Tier S 首屏、D3 入口请求 0；synthcity 首屏 490,260 B，shenzhen 321,384 B，揭示时程序数 27，绘制点 20,000，GL 错误 0，预算违规 0，TTFP 10.3 ms 与 5.8 ms；进程内负载前 3.3（1 min） |

门的构建信息记录 `commit 1cea12c495`、`dirty = true`（与合并提交同树的 `d3-dev` 提交，快照构建环境的标注），门元数据的 `dirty = false`；本报告不据此另作判定。

### 1.4 emax 上线实测（2026-10-25，08:33–09:36 UTC）

主构建为 D2 标签 `v0.2.2` 的构建（发布 `20261025-053022-e0639eae7a`，05:30 UTC 安装；回滚目标 `20261017-031658-80e924a7c2`）。`/lab/` 构建：在与合并提交同树的干净工作树以 `AWR_BUILD_SHA=88bedb2ce9 AWR_BUILD_DIRTY=0 AWR_RELEASE=20261025-082924-88bedb2ce9` 执行 `make lab-build`，29 个文件，K9 与 lab 档扫描字符串 0，测试文件 0，真值文件 0；构建信息的产品版本号沿用 0.2.0。公开样例版本目录 `2026.10.25-1`（M27 `publish_static`）：事件 `crop-handoff-001`（dev），运行公开 id `run-koyqeft6xe`，15 个文件共 135,235,462 B，含 3 路 RGB 预览（640×360，31.4、34.0、39.2 MB）、3 路热成像离线渲染（3.7–4.9 MB）、`tracks_obs.bin`、事件磁带、4 个 Rerun 切片（含底图）、`LICENSE`（CC BY 4.0）与 `NOTICE`。`labctl.py index` 与 `check`（含 8 个 test 事件的泄漏核对）通过；`push` 49.6 s。

上线动作 08:33–08:35 UTC：`labctl activate --version 2026.10.25-1 …`（2.27 s，`bytes_total` 187,836,897）与 `enable-site`（`nginx -t` 通过，运维增加的 `include snippets/nf404.conf` 保留）。`verify.py --connect 127.0.0.1 --source-ips 127.0.0.2-127.0.0.12 --expect-nmax 2 --local` 通过（26.3 s），10 项检查结果见 D3-AC-21。上线后 D2 公开站 e2e：6 passed、1 skipped（当时未启用 `/lab/`）；带 `/lab/` 的 `D2PG_LAB=1` 公开 e2e：7 passed（3.5 min）。AC-46 上线后复核（05:50 UTC，HTML copy-lint）：zh 落地页与关于页 0 违规，en 落地页只有 C-49 关键词徽标（用户决定维持现状）与抽取伪影。

D3-AC-21 的 WS 条款实测不通过，09:05 UTC 撤下 `/lab/`（`labctl withdraw`），随后 `verify.py --expect d2` 通过（3.37 s），站点文件与上线前备份逐字节一致（sha256 前缀 `d177496e…`），`nf404` 保留；D2 线上正常。`verify.py` 的 `d2` 检查报 `POST /api/bench` 等 4 个 POST 返回 403（期望 404）：纯 D2 站点下由 D2 API 在路由前拒绝，GET 与 HEAD 均 404，不是 D3 暴露，登记为 `verify.py` 口径的后续事项。

### 1.5 与 RP0a 的对照

RP0a 记录的回归门四项未通过（pytest 步骤范围与 `nofile`、perf 复测、lint 的 REL-01、demo-parity 对 `v0.2.2`）及D3-AC-09、60 的证据回归在 `docs/05` §33 中已由 K13、K14 关闭，本报告被测提交上的完整门全绿。K14 的 I01 全量结果（2026-10-25 02:39–03:27 UTC，`adfd2d8` 叠加 K14）为 2,460 个用例：2,394 passed、1 failed、0 error、60 skipped、5 xfailed；唯一失败 `test_render_group_gpu_loss_is_647` 在容器内因退出码竞态失败、本机通过（F-123 (a)，本机通过）。该全量结果不是被测提交上的重跑。

### 1.6 环境与负载记录

| 项 | 记录 |
|---|---|
| dmax | Xeon Gold 6326，64 逻辑核；重载门执行时 1 min 负载 27.97、15 min 27.13；仅使用 `d3-*` 容器；其他项目的 `d4ale-*` 容器在使用 `d3-sensorsvc:dev`（F-123 (e)） |
| 本机 | 轻量门期间 1 min 负载 3.3 至 5.6；客户端核 4–7 |
| emax | 阿里云 ecs.e-c1m2.xlarge（cn-hangzhou，4 vCPU），公网出口约 3 Mbps；同机有 ANet 生产负载（1 min 负载 5.5–6.1，4 核） |
| 时间 | 一律 UTC |

## 2. 逐条验收

判定取值：通过；按 ADR 通过（阈值或口径经已确认的 ADR 或用户决定修订后通过）；部分通过（有未满足或未找到证据的子条款，已逐项列出）；不通过；发布就绪；移 D3.x。每条给出 `docs/05` §11.3 的阈值原文（取自被测提交）、实测值、证据与性能标注。"未找到证据"表示在 `docs/05`、`docs/impl`、回归门与上线证据中没有可引用的记录。

### D3-AC-11 相机档位吞吐（v1.3 按档位重算）

- 判定：**按 ADR 通过**（附 1 项待确认：C-54）；优先级：阻塞；环境：独占 A40

- 阈值原文（`docs/05` §11.3）：

> 单 M25 Worker、crop 世界、全输出（RGB8、平面深度、实例、语义、可见度；热成像为表观温度、实例、语义与 `objects[]`），RGB 经 nvJPEG、热成像经 u16 + zstd，存档与落盘按 §5.4.3，10 min 持续吞吐：`rgb720-gs` 晴天档 ≥ 120、最坏档 ≥ 80 帧/s；`rgb1080-rs`（逐顶点卷帘）晴天档 ≥ 60、最坏档 ≥ 40 帧/s；`therm512` ≥ 300 帧/s（v1.6 按 ADR-214 冻结为原值，持续吞吐按 10 min 平均帧率 3 次中位且每个 60 s 分箱不低于阈值判定；原为暂定）；在线闭环队列单帧时延 p99 ≤ 50 ms（720p）、≤ 80 ms（1080p）；标准轨单个正式 Run（3 × `rgb1080-rs` + 3 × `therm512` + 3 × MID-360，最坏档）在 M25 独占时传感器实时因子 ≥ 1.0，3 个并发正式 Run 时各 ≥ 0.35；会话显存 ≤ 1.2 GB（v1.6 按 ADR-213 决策 (2)：同一 Worker 进程内一个会话的增量，即会话期间进程显存峰值减去会话前基线，基线含 EGL、CUDA 主上下文、编码器与 Warp 上下文和常驻世界；Worker 进程合计另报；原文与 M25 §6.21"世界常驻每世界约 2.1 GB"冲突，§23.5 C-14）；应交付目标为 `rgb720-gs` 200 / 120、`rgb1080-rs` 90 / 60、`therm512` 500 帧/s

- 实测（K08，Worker 渲染进程组 `--render-procs 6`，dmax GPU 2，A 级，2026-10-21 20:39 至 10-22 00:07 UTC，K03 协议，完整效果链 `--fx-proxy off`，crop 世界、`crop-handoff-001`、3 机，持续项 3 次中位）：

| 条款 | 阈值（应交付） | 实测 | 60 s 分箱最低 | 判定 |
|---|---|---|---|---|
| `rgb720-gs` 晴天持续 | ≥ 120（200） | 237.01 帧/s | 216.47 | 通过，达应交付 |
| `rgb720-gs` 最坏持续 | ≥ 80（120） | 234.07 帧/s | 222.15 | 通过，达应交付 |
| `rgb1080-rs` 晴天持续 | ≥ 60（90） | 164.44 帧/s | 158.80 | 通过，达应交付 |
| `rgb1080-rs` 最坏持续 | ≥ 40（60） | 157.18 帧/s | 147.23 | 通过，达应交付 |
| `therm512` 持续 | ≥ 300（500） | 518.16 帧/s | 478.85 | 通过，达应交付 |
| 720p 在线闭环单帧时延 p99 | ≤ 50 ms | 33.63 ms | | 通过 |
| 标准轨 1080p 单帧时延 p99 | ≤ 80 ms | 68.49 ms | | 通过 |
| 标准轨单个正式 Run 实时因子 | ≥ 1.0 | 1.456 | | 通过 |
| 标准轨 3 并发正式 Run 各 Run | ≥ 0.35 | 最低 1.163 | | 通过（见保留意见） |
| 会话显存增量（ADR-213 决策 (2)） | ≤ 1.2 GB | 1.104 GB（Worker 合计 8,289 MiB） | | 通过 |

  - 判定：16 项条款全部计入并通过，`summary.seal_ready = true`。阈值为 ADR-214 冻结的原值，未改动。计入前提是 C-44（ADR-214 决策 (4)："单 M25 Worker"指每 GPU 一个 Worker 服务实例，内部可由多个渲染进程组成），用户 2026-10-22 已确认。会话显存口径取 ADR-213 决策 (2)（同一 Worker 进程内一个会话的增量）。
  - 历史：S21（2026-10-19 至 20）与 K03（2026-10-20，单进程 Worker，`therm512` 114.33 帧/s、720p 78.81 / 61.13、1080p 52.04 / 53.46、标准轨实时因子 0.911）均为 A 级不通过，已被 K08 取代；`therm512` ≥ 300 在单进程 Python Worker 下不可达（每帧 GIL 占用约 8.5 ms，预算 3.3 ms，C-38），达标路径为渲染进程组。
  - K13 复核（2026-10-22 12:03–12:18 UTC，同协议，A 级，标准轨 × 1）：1080p p99 69.55 ms，实时因子 1.342，会话显存增量 1.104 GB，与 K08 同量级，结论不变；GPU 时间改为 CUDA 事件计时后单 Run 标准轨合计 0.425 GPU·s/仿真 s（≤ 0.85 准入预算；K08 的墙钟上界为 0.960）。
  - **保留意见（C-54，先行生效，待用户确认）**：K13 为满足 §4.7 的 16 GB 内存预算，在 `sensor` 调度且并发会话 ≥ 3 时把渲染进程数限制为 R ≤ 3（cgroup 实测 R = 6 时匿名内存 16.66 GiB、Σ PSS 18.42 GiB，限制后匿名内存 8.78–9.19 GiB、Σ PSS 10.01–10.31 GiB）。"3 并发各 Run ≥ 0.35"的计入数字仍是 K08 在 R = 6 下的 1.163；限制后形态的 3 并发数字只有共享卡开发参考 0.81–0.90（最低 0.8315，外部租户占用 GPU 2，判 `shared`，不计入）。限制后形态的 A 级复测未执行（§33.5 F-122 (a)，须 GPU 2 或 3 恢复 A 级窗口）。因此该子条款对"生产部署形态"的 A 级证据尚缺，C-54 的推荐是按 C-44 同样适用，复测前以 K08 值计入。
- 证据：`tools/bench/d3/camera_throughput.py`（`--render-procs`）、`python/awr/sensorsvc/procgroup.py`、`tests/d3/bench/test_camera_throughput.py`；[D3-独占卡性能验收.md](D3-独占卡性能验收.md) §5.5、§5.6、§6；[D3-P-1-实测报告.md](D3-P-1-实测报告.md) §4.2、§4.2.1；`docs/05` §32.3 第 98、99 行、§33.3 第 113 至 115 行；dmax `$K/formal/out/camera-k08/camera_throughput.json`、guard 文件 `guard-gpu2-2039.json`。
- 性能标注（§4.7、ADR-195）：K08 为 A 级独占（guard 预窗口与窗口内外部进程均为 0；窗口内宿主机负载曾升至 130 以上，只记录不影响独占判定）；K13 × 1 为 A 级，× 3 为共享卡开发参考。
- 备注：F-122 (b) 至 (e)（同名容器冲突、录制页缓存与 `--memory 16g` 硬限、固定常量上限、dmax 目录清理）见 §4.2。

### D3-AC-12 LiDAR 并发

- 判定：**按 ADR 通过**（ADR-207 计数口径；C-19 用户 2026-10-22 确认）；优先级：阻塞；环境：独占 A40

- 阈值原文（`docs/05` §11.3）：

> Warp MeshLidar，MID-360 花样每帧 20,000 射线，10 Hz，逐点位姿 K = 11、语义反射率、32 个动态实体、天气开：每 0.1 s tick（扫描 + GPU 压缩 + 回传有效点）100 架 p95 ≤ 30 ms；应交付目标 400 架 p95 ≤ 80 ms；与 Open3D CPU 参考（均为瓦片局部原点 float32 求交）逐射线距离差（同一表面）p99 ≤ 1 mm、最大 ≤ 1 cm，命中判定不一致（含轮廓边 float32 判定歧义）≤ 1e-4（v1.6 按 ADR-207 明确计数口径，阈值不变，用户 2026-10-22 确认）

- 实测：
  - 吞吐（S21，dmax GPU 2，A 级，2026-10-19 22:11–22:23 UTC）：100 架 p95 14.06 ms（≤ 30 ms）；400 架 p95 53.06 ms（应交付 ≤ 80 ms），两项通过，取代 B 级与共享卡数字的正式地位（共享卡参考值 25.4 / 66.3 ms 不计入）。
  - 逐射线（S16，960 帧、19.2M 条射线，对拍 Open3D CPU 参考）：命中不一致 0；同一表面最大 2.83e-5 m（p99 ≤ 1 mm、最大 ≤ 1 cm 均满足，p99 1.6e-13 m）；全体最大 2.52 m 来自 3 条轮廓边 float32 判定歧义射线，按 ADR-207 计入命中不一致率后为 1.56e-7（≤ 1e-4）；产品路径 `warp_mesh.py` 的对拍（S12）p99 ≤ 1.9e-6 m、最大 ≤ 7.6e-6 m。
  - K11 将 `lidar_concurrency.py judge` 改为 ADR-207 计数口径（回退口径 `--rule original`），对 S21 `lidar2-combined` 重判 `seal_ready = true`。
- 证据：`tools/bench/d3/lidar_concurrency.py`、`tests/d3/sensorsvc/test_mesh_lidar_gpu.py`、`test_lidar_gpu_cpu_diff.py::test_judge_counting_rules`；[D3-独占卡性能验收.md](D3-独占卡性能验收.md) §2、§6；[D3-P-1-实测报告.md](D3-P-1-实测报告.md) §4.3；`docs/05` §26.3 第 31、32 行、§28.3 第 62 行、§32.3 第 101 行；ADR-207、ADR-213 决策 (9)。
- 性能标注（§4.7、ADR-195）：吞吐为 A 级独占；逐射线为内容口径，与独占无关。
- 备注：本条没有暂定阈值，P-1 报告登记吞吐与逐射线分布，不另冻结。

### D3-AC-13 LiDAR/IMU 输出合同

- 判定：**部分通过**；优先级：阻塞；环境：dmax CPU 池

- 阈值原文（`docs/05` §11.3）：

> CustomMsg md5 与 livox_ros_driver 一致；每帧槽位全部输出（MID-360 20,000 点）、无回波零点占位、`offset_time(i) = ⌊i · 10⁸ / N⌋` ns 单调、`timebase` 为帧首；IMU 200 Hz、单位 g、MID-360 内置 IMU 参数；预滚窗口位于事件零点之前（≥ 0.5 s，缺省 2 s；地面起飞为静止、空中出生为悬停保持，期间不调用 Agent），空中出生时 `gravity_align_en` 开启；IMU 读数与同算法 float64 参考实现之差 ≤ 1e-6（m/s²、rad/s；参考实现与未滤波插值之差另行报告）；clean 档（零偏置、零噪声）5 s 纯惯导回代误差 < 5 cm；\|ω\| < 5 rad/s、\|a\| < 30 m/s² 断言；`rig.json` 生成的 FAST-LIVO2 yaml 外参与 rig 一致，`exposure_time_init = 0`、`img_time_offset = 0`，内参 cx、cy 减 0.5；SLAM 相机为全局快门且内参、外参在运行中恒定；噪声参数与参数表逐项一致；删除无回波点的 bag 被合同测试拒绝（642）

- 实测：
  - IMU 与同算法 float64 参考实现之差（S19，本机）：M25 输出 gyro 5.6e-17、acc 7.4e-15；f32 存档路径 gyro 7.4e-9、acc 4.8e-7（≤ 1e-6）；clean 档 5 s 纯惯导回代误差 3.4–14.8 mm（< 5 cm）。
  - `rig.json` 与 FAST-LIVO2 yaml 外参一致、`exposure_time_init = 0`、`img_time_offset = 0`、内参 cx、cy 减 0.5，以及预滚窗口：`tests/d3/slam/test_rig.py`（S14，18 个用例）；合同用例 `test_contract.py`（C1–C11、N1、N2，S19 时 14 个用例：13 passed、1 xfailed）。
  - **未满足或未关闭**：(1) realistic 档 `slam_cam` 云台读数一项为 xfail（`capture.py` 对每个相机加编码器噪声，AWR-20 §15.6.5 只允许 cam0），该原因与阈值中"SLAM 相机……内参、外参在运行中恒定"一句相关（据 xfail 原因推断，docs/05 未单独判定），修复登记为 F-52，测试文件中该标记仍为 `strict=False` 的 xfail，未找到关闭记录；(2) C4（初始化窗口比力均值 1 ± 0.01 g）在 realistic 档读数上 200 个种子中 62.5% 不通过（C-31）；C-31 的推荐"C4 只在 clean 档或 PoseHistory 上判定，由 S14 所有者以 ADR 冻结"已随用户 2026-10-22 批量确认，但冻结用 ADR 未找到证据；(3) N1 的原因名 `livox_zero_points_missing` 未出现在 642 的 detail 中（F-52），"删除无回波点的 bag 被合同测试拒绝（642）"本身已覆盖。
  - 被测提交上的重跑：未找到证据（本条的计数取自 S14、S19 批次）。
- 证据：`tests/d3/slam/test_contract.py`、`test_rig.py`、`imu_reference.py`；`docs/05` §27.3 第 45 行、§26 的 S14 行、§27 的 S19 行、§27.5 F-52；C-31。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：转为"通过"需要：修复 `capture.py` 使 xfail 转通过，并以 ADR 冻结 C4 的判定范围。

### D3-AC-14 FAST-LIVO2 RPE 区间

- 判定：**按 ADR 通过**（ADR-215 冻结；用户 2026-10-22 确认，C-16、C-39）；优先级：阻塞；环境：dmax CPU 池

- 阈值原文（`docs/05` §11.3）：

> 机型 `p600_mid360_inv`；轨迹由 Realistic 档（gz-sim 锁步，`physics_hz = 250`，缺省）生成，Fast 档 FleetSim（`clock_profile = {d1_250, 250, 250, 250}`，即 `l1_every = 1`）轨迹另报一组；RPE100 单次运行取段均值，跨运行取中位数；正确尺度 town 883 m 环线、30 m AGL、6 m/s；LIVO 模式为主口径，LIO 模式单列：clean 档 RPE100 ≤ 0.10%、对齐 ATE ≤ 0.20 m、地图到网格距离均值 ≤ 0.15 m（v1.6 按 ADR-215：以事件零点处的估计位姿锚定）；realistic 档 5 种子 × 3 次重复的 RPE100 中位数落在 [0.07%, 0.18%]，单次最大 ≤ 0.50%，且中位数不低于 clean 档（v1.6 按 ADR-215 冻结：gz 锁步主组 LIVO、无风与湍流轨迹、`d3-noise-1`；原暂定区间 [0.15%, 0.40%] 为 r1 样条轨迹口径）；8 m/s 航段逐点位姿相对单一位姿的 ATE 改善 ≥ 5 倍（v1.6 按 ADR-215 冻结；单一位姿发散时计为满足并注明）；HKisland03 真机回归 ATE ≤ 1.2 m（镜像健康检查，作业 ≥ 32 GB、8 核，v1.6 按 ADR-215；原为 ≥ 20 GB、4 核）；报告注明反射率模型与噪声档参数表版本

- 实测（SL3 矩阵，town `b5103d23ed70` 883 m 环线，30 m AGL，6 m/s；24 个算例 × LIVO/LIO × 3 次 = 144 次运行；dmax CPU 池，`--cpus 4`；`sem_table_v1`、`d3-noise-1`；2026-10-20 06:00–06:59 UTC，K11 于 2026-10-21 按 ADR-215 冻结表重判，不重跑）：

| 条款（ADR-215 冻结表） | gz 锁步 LIVO（主口径） | gz 锁步 LIO（单列） | Fast 档 LIVO（另报） | Fast 档 LIO（另报） |
|---|---|---|---|---|
| clean RPE100 ≤ 0.10% | 0.054% | 0.060% | 0.080% | 0.063% |
| clean 对齐 ATE ≤ 0.20 m | 0.051 m | 0.054 m | 0.098 m | 0.072 m |
| clean 地图到网格距离均值 ≤ 0.15 m（事件零点锚定） | 0.085 m | 0.062 m | 0.073 m | 0.093 m |
| realistic RPE100 中位 ∈ [0.07%, 0.18%]（5 种子 × 3 次） | 0.112% | 0.119% | 0.102% | 0.151% |
| realistic 单次最大 ≤ 0.50% | 0.237% | 0.213% | 0.275% | 0.206% |
| realistic 中位不低于 clean | 满足 | 满足 | 满足 | 满足 |
| 8 m/s 逐点位姿相对单一位姿 ATE 改善 ≥ 5 倍 | 单一位姿 3/3 发散，计为满足 | 同左 | 51.2 倍 | 88.2 倍 |

  - HKisland03 真机回归 ATE 1.043 m（≤ 1.2 m，作业 8 核、32 GB，符合 ADR-215 (4)）；反射率模型 `sem_table_v1` 与噪声档参数表 `d3-noise-1` 已注明；判定范围内 48 次运行处理帧比例均 ≥ 98%。
  - 参考口径（r1 首个估计锚定，不计入）：主组 LIVO clean 地图均值 0.316 m；按此口径不通过，即 ADR-215 决策 (1) 被否决时的回退结论。realistic 区间下界与锚定点由 ADR-215 冻结后重判通过。
  - 不在本条判定范围的一项：AC-47 对比算例 `fast-realistic-s1-r1_mid/livo-r2` 处理帧比例 0.978，使矩阵"全部运行有效"标志为假。
- 证据：`tools/bench/d3/slam_matrix.py`（`report`）、`slam_eval`（`report.AC14` 冻结表）、`slam_traj.py`；`tests/d3/slam/test_slam_eval.py`、`test_slam_matrix.py`、`test_slam_traj.py`；[D3-SLAM-实测记录.md](D3-SLAM-实测记录.md) §3、§3.2.1、§3.6；`docs/05` §29.3 第 76 至 78 行、§32.3 第 101 行；ADR-215；dmax `/data/anet-drone4d-d3/d3dev/k11/sl3/matrix.json`。
- 性能标注（§4.7、ADR-195）：RPE、ATE、地图距离为内容口径；核当量与墙钟为共享 CPU 池开发参考。
- 备注：realistic 区间 [0.07%, 0.18%] 为按实测参考中位 0.112% 的相对带宽冻结，风与湍流对照移入 D3.x（F-84）；`apps/web/tests/lab/lidarCompare.test.ts` 的夹具区间仍为 [0.15, 0.40]（F-110，不影响功能）。

### D3-AC-15 多机 LiDAR 采集与重建对比

- 判定：**部分通过**；优先级：阻塞；环境：dmax、本机 Tier S 与 Tier B

- 阈值原文（`docs/05` §11.3）：

> 3 架 `p600_mid360_inv` 在同一世界不同区域各飞 ≥ 60 s：每机产出私有 LiDAR 数据、L0 离线已知位姿地图（`eval_sim`，逐点位姿积分，体素 0.25 m，深度上限 60 m，在 `--memory 16g` 内完成）与 FAST-LIVO2 地图；两两覆盖 Jaccard ≤ 0.3（0.5 m 表面体素）；"LiDAR 与重建对比"视图在 Tier B 以裁剪网格正确显示三方叠加，在 Tier S 以点云降级显示；到网格距离着色与 `eval_sim` 数值一致，RPE、ATE、漂移分布正确

- 实测：
  - 采集与地图条款（B19，dmax；GPU 2 共享卡仅用于生成数据，不报告性能数字）：3 架 `p600_mid360_inv` 各飞 62.0 s 运动段（航程 300 m）；L0 峰值 297.5 MiB/机（在 `--memory 16g` 内完成），到网格距离中位 0.069–0.070 m；FAST-LIVO2 LIVO 与 LIO 各 3/3 成功；两两覆盖 Jaccard 最大 0.0379（≤ 0.3），合并覆盖 82.9%；着色与 `eval_sim` 数值一致（1 万点最大差 0.99993 mm）；LIVO RPE100 中位 0.077%、ATE 中位 0.057 m；10 项判定全部通过。
  - **视图条款**：`lab/lidar-compare` 视图已实现，单元测试 `apps/web/tests/lab/lidarCompare.test.ts`（17 个用例）与 `lidarCompare.browser.test.ts` 通过；Playwright `lab/lidar-compare` 用例未找到证据（`tests/e2e` 下 lab 相关用例为 `lab-inspect`、`lab-mbl`、`lab-public-samples`、`lab-roles`，没有 lidar-compare）；Tier S 点云降级显示与 Tier B 裁剪网格三方叠加的浏览器实测未找到证据。
  - 环境口径说明：RP0a §4.3 记录"D3-AC-15 的 Tier B 部分"随用户 2026-10-22 的 Tier B 决定移入 D3.x；而 ADR-219 决策 (4) 明确"不改变 D3-AC-15、51 等其他条款的环境口径"，ADR-219 只列 D3-AC-25 的两项首帧与 D3-AC-21 的 6 路并排偏差。两处表述不一致，视图条款的归属需要用户澄清（§4.1）。
- 证据：`tests/d3/slam/test_multi_uav.py`、`test_l0_map.py`；`tools/bench/d3/lidar_collect.py`；`docs/05` §27.3 第 46 行、§26 的 F06 行、§27 的 B19 行；ADR-219。
- 性能标注（§4.7、ADR-195）：内容口径，不涉及性能项。
- 备注：转为"通过"需要补 Playwright `lab/lidar-compare` 的 Tier S 用例，并由用户裁定 Tier B 显示子条款是否随 ADR-219 顺延。

### D3-AC-16 观测隔离无泄漏

- 判定：**通过**；优先级：阻塞；环境：dmax

- 阈值原文（`docs/05` §11.3）：

> 对 L1 与 L2 排名运行配置中 Agent 收到的全部消息（obs、inbox、reset 配置、错误消息）按四类方法检测（M26 PRD §8.3）：结构化检索（按 schema 字段检索演员 id、磁带演员序号、真值坐标、本机真值位姿与外参、run_seed、磁带与事件哈希与实例图）与高熵字节检索（非图像载荷的 4 与 8 字节窗口，float32、float64、毫米定点三种编码；图像载荷只检查 JPEG 段白名单）命中 0；Agent 收到的消息全部通过 `bench/obs.v1` 白名单 schema 校验（未知字段 0）；以收到的全部数值作为候选种子反演 GNSS 噪声与 L1 演员位置的红队用例，反演误差不优于噪声模型标称值；排名运行请求 T0 特征档时被拒绝（662）；行为攻击用例（在线限幅与告警探测、越权连接）不泄漏演员信息；Agent 容器对 dmax 内网、公网、catalog、沙盒 API 与 bench-api 的连接全部失败，挂载路径、主机名与环境变量不含运行标识；分发 catalog 上查询 `/truth/**` 返回 0 行；LiDAR `tag` 不含识别物位；L1 检测不含真值 id 与 `n_in_fov`，`det_id` 每帧随机且不能跨帧关联到演员，航迹按机私有

- 实测：
  - 检测规则与反演用例（A06、A07）：`tests/d3/leak_redteam/`，含结构化检索与高熵字节检索、R3 与 R4 反演、排名运行请求 T0 特征档被拒绝（662）、`det_id` 每帧随机；正式规模下带噪导航解与 GNSS 解的偶然吻合按 C-22 的孤立偶然吻合规则处理（只限导航与 GNSS 字段、每字段每帧至多一次、每运行至多 6 帧，已确认）。
  - 容器与网络（A08）：Agent 容器对 96 个目标（dmax 内网、公网、catalog、沙盒 API、bench-api）的连接全部失败，诱饵命中 0，并有主机网络阳性对照；生产配置的元数据标识（挂载路径、主机名、环境变量）命中 0；分发 catalog 上 `/truth/**` 返回 0 行；正式规模容器路径矩阵（L1、L2、L2-depth × realistic、harsh × 2 种子 × 3 机）12/12。
  - 种子保密（K18，2026-10-22 实现，2026-10-25 02:17 UTC 复查）：排名运行不在运行目录写种子明文，数据集布局下写入同一数据根隐藏区 `hidden/<事件>/truth/runs/<run_id>/run_seed.json`（0700/0600），否则以 EvalStore 为准，运行清单记 `seed_store`；全目录检索种子文本 0 命中（`test_ranked_run_writes_no_plaintext_seed`）；dmax 既有 488 个明文种子文件移入只限 root 的隔离区（清单 488 行），残留 0；2026-10-22 13:00 UTC 之后新写入隐藏区的种子文件 0。
  - 排行榜 v1 正式评测（B21）：test 真值读取审计外部访问 0，越权探测返回 691。
  - C-30 的两项审计局限（被 seccomp 终止的子进程只记录在宿主内核日志，读取需要 CAP_SYSLOG；对不存在路径的 open 不可观测）：用户 2026-10-22 决定不授予 CAP_SYSLOG，判据取主进程退出码 159 与诱饵访问，局限在本报告注明。
- 证据：`tests/d3/leak_redteam/`（`test_inversion.py`、`test_catalog_truth.py`、`test_search.py`、`test_container_isolation.py`）、`tests/d3/env/test_run_robustness.py`；`docs/05` §25.2 A06 至 A08 行、§33.3 第 125 行；[D3-正式运行稳健性与安全.md](D3-正式运行稳健性与安全.md) §1、§8；`results-B03/A08.md`；C-22、C-30。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：F-37（Agent 启动器的主机名、随机令牌目录、`DRONE4D_SOCK` 与 `DRONE4D_PROTOCOL` 规格，以 strict xfail 跟踪）在 `docs/05` 中未见关闭记录，A08 的生产配置元数据命中 0 为本条判定依据；`test_container_isolation` 在 K14 的 I01 中未列为失败，是否被跳过的逐用例记录未找到证据。登记于 §4.2。

### D3-AC-20 Rerun 派生与回放

- 判定：**通过**（证据以测试位置与实测行为准）；优先级：阻塞；环境：dmax

- 阈值原文（`docs/05` §11.3）：

> 从 MCAP 离线导出的 `.rrd` 与在线写入逐实体一致（`rerun rrd compare`）；`application_id` 无点号、实体路径校验通过；`/fleet/<uav>` 为带噪导航解，真值位姿只在 `/truth/**`；注册到评测与分发两个 catalog，分发 catalog 只含 `obs.*` 与 `agent.*`；跨 3 个 Run 的位姿查询 ≤ 1 s；`rerun_version` 不一致时给出迁移提示；`public` 档切片（3 机 10 s，含相机与 LiDAR 静态标定，不含底图）≤ 64 MB，与 `basemap.rrd`（≤ 24 MB）合并后可由原生 Viewer 打开并回放；派生物按保留期清理

- 实测：
  - 公开切片与底图（R04，真实采集：3 机，1080p/720p @ 10 Hz，2 台 MID-360）：10 s 公开切片 4.97–7.80 MB（≤ 64 MB，无需降级）；底图事件舞台裁剪区 5.68 MB（0.25 m 体素），500 万点全世界 23.55 MB（1.86 m 体素，≤ 24 MB）。
  - catalog 查询（R03）：3 个 Run × 3 机 × 360 s 的 `poses` 最大 0.395 s（≤ 1 s）；未优化冷查询最长 1.14 s，优化后 0.25–0.40 s。
  - 在线与离线 `.rrd` 逐实体一致、`application_id` 与实体路径校验、`/fleet/<uav>` 与 `/truth/**` 分离、评测与分发两个 catalog 的注册、`rerun_version` 迁移提示、派生物保留期清理：由 `tests/d3/rerun/`（`test_online_vs_offline.py`、`test_catalog.py`、`test_export.py`、`test_public_slice.py`、`test_proxy.py`）与 `tests/d3/leak_redteam/test_catalog_truth.py` 覆盖；逐条款的用例计数与 `rerun rrd compare` 对拍结果未找到证据，本条按测试位置登记。
  - 上线版本目录 `2026.10.25-1` 含 4 个 Rerun 切片（含底图），各文件体积未单列记录（总计 15 个文件 135,235,462 B）；上线后单文件均 < 64 MB（verify 的 `disk` 检查，`oversize = 0`）。
- 证据：`tests/d3/rerun/`；`tools/bench/d3/public_slice.py`；`docs/05` §25.2 R02 至 R04 行、§25.3 第 19、20 行；`LIVE-EVIDENCE.md`。
- 性能标注（§4.7、ADR-195）：切片体积为内容口径；查询时延为 dmax 共享主机数字，余量大，仅作参考。
- 备注：RP0a 记录依赖 `datafusion` 的 `rerun` 用例曾在默认镜像中因缺依赖报错（F-113），K14 重建镜像后 I01 为 0 error，F-113 关闭。

### D3-AC-21 公开站只读样例视图（v1.3）

- 判定：**不通过**（WS 推送时延条款）；其余可判定的子条款在 emax 上线实测中通过；优先级：阻塞；环境：emax（或 cgroup 模拟；v1.6：封板以上线后的 emax 实测判定，cgroup 模拟只作上线前演练，ADR-194）。**封板阻塞项**

- 阈值原文（`docs/05` §11.3）：

> 部署后公开组合根 `AWR_D3_ENABLED = 0`，进程表中无 D3 进程；`/api/bench/*`（含 `/api/bench/v1/public/**`）返回 404；公开站主构建为 D2 标签构建，`/lab/` 为独立静态构建；只读页在 Tier S 可用且只发起静态 GET 请求：样例目录、事件回放由前端磁带解码器驱动（只显示脚本演员）、多机私有视角（RGB 与热成像伪彩视频按仿真时间同步，偏差 ≤ 1 帧）、样例数据检视（Tier S 为切片下载）；公开构建产物中 `/leaderboard`、`/submissions`、`/board`、`/registry` 与 `/api/bench/v1/` 字符串为 0，页面不出现主得分与跨策略得分比较；D2-AC-04 容量用例复测通过且 N_max 不变；公开静态目录中 test 集文件数为 0；D3 在 emax 的总磁盘占用（含保留版本）不超过 Q-D3-02 的预算且无 > 64 MB 的单文件；单访客下载速率不超过 `limit_rate` 设定（2 MB/s，前 8 MB 不限）；16 路并发下载期间 D2 WS 推送时延 p95 相对无下载基线的增幅 ≤ 10%；回滚演练切回上一个 D2 标签构建 ≤ 10 min

- 实测（2026-10-25，emax 上线后 08:33–09:05 UTC；`verify.py --connect 127.0.0.1 --source-ips 127.0.0.2-127.0.0.12 --expect-nmax 2 --local`，26.3 s，10 项检查全部通过；带 `/lab/` 的公开 e2e 7 passed；WS 时延探针为 `drill.py` 的 `_ws_viewer` 与 `ws_latency` 原样，基线与下载阶段交替各 45 s × 3 轮，阶段起点去掉 2 s）。逐子条款判定：

| # | 子条款 | 判定 | 实测与出处 |
|---|---|---|---|
| 1 | 公开组合根 `AWR_D3_ENABLED = 0`，进程表中无 D3 进程 | 通过 | verify `root`：env 文件中 `AWR_D3_ENABLED` 未设置，D3 进程 0，D3 容器 0（进程 669 个，其中 427 个进程的环境不可读） |
| 2 | `/api/bench/*`（含 `/api/bench/v1/public/**`）返回 404 | 通过 | verify `bench404`：9 个路径 × GET、HEAD、POST 共 27 项全部 404 |
| 3 | 公开站主构建为 D2 标签构建，`/lab/` 为独立静态构建 | 通过 | 主构建 `v0.2.2`（发布 `20261025-053022-e0639eae7a`）；`/lab/` 构建 `20261025-082924-88bedb2ce9`，29 个文件；verify `lab`、`static`（`index` 无缓存与 ETag/304，11 个 HEAD，15 条 SHA256SUMS） |
| 4 | 只读页在 Tier S 可用且只发起静态 GET；样例目录、事件回放、多机私有视角、样例数据检视可用 | 通过（页面加载与请求守卫）；播放同步见 #5 | 线上 e2e "/lab/ static build: loads with static GETs only"通过（`lab-static` 16 个请求）；"D2 站点无页面请求 `/lab/`、`/lab-static/`、`/api/bench`"通过；三条页面功能的详细用例为本机栈 `lab-public-samples`（F07、R07） |
| 5 | 视频按仿真时间同步，偏差 ≤ 1 帧 | 通过（本机 Tier S）；Tier B 6 路并排移 D3.x | R07，Tier S SwiftShader：1× 12 s 最大偏差 1 帧（0 帧 132 次、−1 帧 64 次），暂停拖动偏差 0，用例仅在主机空闲（1 min 负载 ≤ 0.5/核）时判定播放偏差。Tier B 6 路并排偏差按 ADR-219 移入 D3.x，封板须记录的 dmax 无头 Chromium 开发参考数字：未找到证据；线上 emax 上的播放同步偏差未单独测量：未找到证据 |
| 6 | 公开构建产物中 `/leaderboard`、`/submissions`、`/board`、`/registry`、`/api/bench/v1/` 字符串为 0，页面不出现主得分与跨策略得分比较 | 通过 | `/lab/` 构建扫描字符串 0（29 个文件）；演示构建 K9 扫描 0（66 个文件）；`k9-d3` 的 D3 入口字符串 0；verify `content` |
| 7 | D2-AC-04 容量用例复测通过且 N_max 不变 | N_max 通过；容量用例线上复测未找到证据 | verify `nmax`：2 = 期望 2，accepting，且每次 WS 实测前后均为 2；上线前演练记录 D2-AC-04 变体 A 在本机因主机竞争不通过，须在空闲主机重跑，正式以 emax 实测判定；emax 上的 D2-AC-04 容量用例结果：未找到证据 |
| 8 | 公开静态目录中 test 集文件数为 0 | 通过 | verify `content`：`test_files = 0`、`truth_files = 0`；`labctl check` 含 8 个 test 事件的泄漏核对通过 |
| 9 | D3 在 emax 的磁盘占用不超过 Q-D3-02 预算且无 > 64 MB 单文件 | 通过 | verify `disk`：187,938,436 B（预算 3,221,225,472 B，即 3 GiB），> 64 MB 单文件 0 个（`/lab/` 最大文件为 Rerun Viewer 的 Wasm 48.85 MB） |
| 10 | 单访客下载速率不超过 `limit_rate` 2 MB/s（前 8 MB 不限） | 通过（回环测量） | verify `rate`：首 8 MiB 突发 421.778 MiB/s，之后 1.899 MiB/s，Range 请求 1.952 MiB/s（限值 2.0，容差 0.05）；`conn_ip` 同源第 5 个连接 429；`conn_all` 32 条连接后 429。测量在 emax 本机回环上进行，不反映公网出口带宽 |
| 11 | 16 路并发下载期间 D2 WS 推送时延 p95 相对无下载基线增幅 ≤ 10% | **不通过** | 4 次实测增幅 +66.7% 至 +338.8%，见下表 |
| 12 | 回滚演练切回上一个 D2 标签构建 ≤ 10 min | 通过（口径见注） | 09:05 UTC `labctl withdraw` 加 `verify.py --expect d2`：3.37 s（门限 10 min），站点文件与上线前备份逐字节一致；上线前演练（F08/B03）的发布线与回滚亦通过。注：线上演练为撤下 `/lab/` 并恢复上线前站点文件，未切换发布到回滚目标 `20261017-031658-80e924a7c2`，该切换的线上实测：未找到证据 |

WS 推送时延实测（子条款 11）：

| 时间（UTC） | 测法 | 基线 p95 | 下载期间 p95 | 增幅 | 各轮增幅 |
|---|---|---|---|---|---|
| 08:42 | emax 本机回环，8 个 viewer，16 路整段下载，总吞吐 39.5 MiB/s | 52.5 ms | 99.3 ms | +89.0% | 82%、74%、108% |
| 08:48 | 同上，下载客户端 `nice -n 19 taskset -c 3` | 59.3 ms | 139.8 ms | +135.7% | 209%、146%、86% |
| 08:54 | 同上，每路客户端读取 ≤ 8 MiB/s（模拟访客带宽） | 58.5 ms | 97.6 ms | +66.7% | 28%、160%、48% |
| 09:03 | 公网：探针 2 个 viewer 在美国开发机，1 路整段下载自 dmax（宁波）经公网 | 164.7 ms | 722.9 ms | +338.8% | 462%、277%、350% |

- 原因分析（取自实测记录）：
  - emax 为阿里云 ecs.e-c1m2.xlarge（cn-hangzhou，4 vCPU），公网出口约 3 Mbps。自 dmax（宁波，CERNET）单路 341 KB/s、两路并发合计 355 KB/s；自美国开发机单路 330 KB/s、三路合计 254 KB/s。
  - 设计的单访客限速 2 MB/s 与 16 路并发（约 268 Mbps）远超出口带宽；一位访客下载一个样例视频（约 315 KB/s）即占满出口。emax 同机另有 ANet 生产负载（1 min 负载 5.5–6.1 / 4 核）。
  - 上线前演练（F08/B03，本机 cgroup 模拟、独占核）当时为 7.4% 与 10.8%，回环环境测不到出口带宽，因此上线前演练不能替代线上实测，这正是 ADR-194 决策 (4) 要求在 emax 上实测的原因。N_max 在每次实测前后均为 2，D2 功能未受影响。
- 处置：09:05 UTC 撤下 `/lab/`（`labctl withdraw`），D2 线上正常；版本目录 `2026.10.25-1` 与 `/lab/` 构建保留，`enable-site` 即可恢复。**决定权在用户**，可选方案：
  1. 提升 emax 公网带宽，并把 `/lab-static/` 的单访客限速与并发连接上限调到与出口匹配；须新增 ADR 记录新的带宽预算与限速值，并在公网侧（非回环）重新实测 WS 时延，判据不变（增幅 ≤ 10%）。
  2. 把样例视频与 Rerun Viewer 的 Wasm 等大文件移到外部分发（ModelScope 或 CDN），公开站只保留小文件与页面；需要前端改动（样例页、`NOTICE` 中"全分辨率视频将另行通过 ModelScope 发布"的链接）与 ModelScope 令牌，令牌当前未到位。
  3. 把 D3-AC-21 的 WS 条款豁免并移入 D3.x：与 ADR-194 决策 (3)（阈值逐条对照原文、不降低）及决策 (4)（上线实测判定）冲突，须用户明确批准并新增 ADR，同时调整 §23.4 的豁免清单。
  在三个方案中任何一个落地之前，本条保持不通过，封板不能完成。
- 证据：`LIVE-EVIDENCE.md`；`verify-2026.10.25-1.json`、`verify-withdraw-20261025.json`；`e2e-lab/metrics.json`；`emax-ws/`、`wan1/wan_ws.json`；`tools/deploy/emax/d3-public/verify.py`、`drill.py`、`nginx-check.sh`；`tests/e2e/d2-public-gate.spec.ts`（`D2PG_LAB=1`）；`apps/web/tests/lab/labPublicSamples.test.ts`；`docs/05` §28.3 第 69 行、F08、B03、R07 批次记录；ADR-194、ADR-219。
- 性能标注（§4.7、ADR-195）：速率与时延为 emax 线上实测（共享生产主机，同机有 ANet 负载）；#10 与 #11 的前三行为回环测量，第四行为公网测量。
- 备注：Rerun Viewer Wasm 的等长字符串改写（`remapVendorStrings`）已随 C-29 经用户 2026-10-22 确认，扫描规则未放宽；ModelScope 令牌未到位，全分辨率样例包（1080p 视频等）与论文数据 `m1geo_full.parquet` 未上传；`labctl pin` 按公开 id 匹配运行目录，公开 id 与内部 id 不同时匹配不到，本次直接调用 `retention.sync_references`（后续事项）。

### D3-AC-22 GPU、存储、评测窗口与背压

- 判定：**部分通过**；优先级：阻塞；环境：dmax

- 阈值原文（`docs/05` §11.3）：

> 正式评测、生产与性能验收运行只绑定独占卡（未到位时按 §4.7 条件通过；v1.6：封板不接受条件通过，ADR-194；独占卡口径按 ADR-195）；评测队列非空时正式评测优先准入，单卡并发正式 Run ≤ 3 且各自份额 ≥ 1/3 卡，`d3.slice` 内正式评测核数不超过 30、其他批作业 ≤ 8 核；`ranked_eval` Run 在评分后只保留逐帧摘要与 `truth_min`（抽样 Run 保留完整录制 30 天），重复运行摘要相同时去重；GPU 容器只绑定一张卡，CPU 池容器（含 `geosvc` 与 gz `physics` 导出档实例）不挂载 GPU；全部 D3 容器设 `--memory`，gz 与 Worker 使用独立分区与命名；Benchmark 锁步下 M25 超限时阻塞仿真时钟、不降频；交互预览队列超限返回 641 并降频，负载回落后恢复；Benchmark 运行的观测墙预览不进入 M25 队列；`/data/d3` 配额与保留清理作业生效（过期派生物被清理、pinned 不被清理、超配额时新运行排队并记录 685）；1 h 生产负载下 D3 持续写速率 p95 ≤ 写带宽预算，MCAP 写入器阻塞时间占墙钟 ≤ 1%；CPU 池作业受 `d3.slice` 配额约束

- 实测：

| 子条款 | 判定 | 实测与出处 |
|---|---|---|
| 正式评测与性能验收只绑定独占卡（封板不接受条件通过，ADR-195） | 通过 | S21、K03、K08 为 A 级（GPU 2）；B21 正式评测在 GPU 3、A 级；各自 guard 记录 |
| 评测队列优先准入，单卡并发正式 Run ≤ 3、各 ≥ 1/3 卡；`d3.slice` 内正式评测核数 ≤ 30、其他批作业 ≤ 8 核 | 通过（单卡并发）；核数上限的逐项实测未找到证据 | S21：单卡并发均值 2.998、最大 3，9/9 成功、无丢帧；`d3.slice` 内正式评测核数 ≤ 30 与其他批作业 ≤ 8 核的实测值：未找到证据，测试位置为 `tests/d3/deploy/` 与运行清单审计（`audit_manifests.py --live`，K14 将审计计数由 31 调为 32） |
| 1 h 生产负载下持续写速率 p95 ≤ 写带宽预算 | 通过 | S21（A 级，2026-10-19 20:44–21:44 UTC）：60 s 窗口 p95 12.48 MB/s（预算 300 MB/s，ADR-213 (6)）；批量规模补充（B33，B10 第 2 轮 130 次 gz 锁步运行）：按运行起止重建的 60 s 窗 p95 22.02 MB/s |
| MCAP 写入器阻塞时间占墙钟 ≤ 1% | 通过 | S21：逐 Run 最大 8.2e-5；B33 批量：最大 5.09e-4，`dropped` 全部为 0 |
| 背压（锁步阻塞不降频、预览 641 降频并恢复、观测墙预览不进 M25 队列）；GPU 容器只绑一张卡、CPU 池容器不挂 GPU；全部 D3 容器设 `--memory` | 通过 | `tests/d3/deploy/test_backpressure.py`；运行清单审计 |
| `/data/d3` 配额与保留清理（过期派生物清理、pinned 不清理、超配额排队并记录 685） | 通过 | `tests/d3/deploy/test_retention.py`；批量规模 130 个 Run 全量保留 404.38 GB，为 10 TB 配额的 4.04% |
| `ranked_eval` Run 评分后只保留逐帧摘要与 `truth_min`，抽样 Run 保留完整录制 30 天，重复运行摘要相同时去重 | 通过（生产接入与回填） | K17（2026-10-25）：`finalize_ranked_eval` 接入 subworker、`mbl_e2e` 与 `board_v1_formal.py` 发布阶段；B10 第 2 轮 60/60 对重复去重，B21 v1.1 381/397 对去重（16 对摘要不同、各自保存，另 3 对含待重跑运行）；精简后复评逐字节一致 268/268（B10 28/28、B21 240/240，评测器 0.3.0）；抽样 B10 共 7 个、B21 共 80 个；回填后 B10 运行目录 346.90 GB → 19.62 GB，B21 `dataset` 1,369.73 GB → 169.43 GB，文件系统共减少约 1.56 TB |
| 证据帧（`truth_min.evidence`） | **开放** | Env 运行不录 `camera_gt`，`truth_min.evidence` 为空；证据帧真值来源须在 ADR-163 与 M27-FR-171 中二选一（取自 `l2.jsonl` 的 `bbox_xywh`，或要求 Env 为证据帧录制 `camera_gt`），K17 后仍开放（F-107） |
| 30 天到期精简 | **开放** | 抽样运行到期（B21 为 2026-11-24 07:47 UTC）由 `retention_gc` 执行，dmax 尚无定时作业（F-125 (a)） |

  - 被测提交上的重跑：功能测试 `test_ranked_retention.py` 与 `test_retention.py` 24 passed、`test_mbl_e2e.py` 8 passed、相关 462 passed、13 skipped 取自 K17 批次（`23762fd` 起）；被测提交上未重跑。
- 证据：[D3-评测运行保留接入记录.md](D3-评测运行保留接入记录.md) §0 至 §5；[D3-独占卡性能验收.md](D3-独占卡性能验收.md) §4；[D3-批量基准记录.md](D3-批量基准记录.md)；`tools/bench/d3/write_load.py`、`tests/d3/bench/test_write_load.py`（19 个用例）；`docs/05` §32.3 第 104、105 行、§33.3 第 123、124 行、§32.5 F-106、F-107、§33.5 F-125。
- 性能标注（§4.7、ADR-195）：写速率与阻塞为 S21 的 A 级正式数字；B33 数字为共享卡原型批量规模补充，不替代；精简与回填为 CPU 池作业。
- 备注：转为"通过"需要：F-107 的规格二选一并落实，且为抽样到期精简建立定时作业；另 4 个待重跑运行重跑后须对新运行执行一次 `finalize --board v1.1`（发布阶段自动调用）。

### D3-AC-23 企业级文案

- 判定：**通过**；优先级：阻塞；环境：本机

- 阈值原文（`docs/05` §11.3）：

> `copy-lint` 中文与英文规则集对全部 D3 页面字符串、`/lab/` 公开构建、数据集发布页与 OpenAPI 描述通过；短语禁用表与白名单按 §7.10 执行（"超时"、"N 秒"、打开对话框的菜单省略号不误报）；D3-AC-46 基线例外已清零，`README.md`、`README.zh-CN.md`、`zh-CN.json` 与 `en.json` 复跑中英文规则集不回退；§7.10 评审清单人工抽查覆盖全部 D3 页面

- 实测：
  - copy-lint 范围：`tools/lint/copy-lint.scope.json` 加入 D3 lab 分片词典、`lab-public/` 全部词典与 `index.html` 模板、`reasons.zh-CN.gen.json` 与三份 OpenAPI 快照；扫描文件由 5 个增至 27 个，0 处违规；OpenAPI 原有 80 处违规已清零。`make lint` 以 `--strict` 运行 copy-lint，REL-09 以 `--copy-strict` 运行；被测提交上的 `make lint` 通过（§1.3，53.0 s）。
  - 人工评审（H11）：覆盖全部 15 条 D3 页面路由与 19 个词典文件，登记发现 17 项；须改正 9 项（W-01 至 W-07、W-09、W-10）已由 K09 改正并按评审记录第 8 节复核（记录第 9 节），回归测试 `copyReview.test.ts`、`board.test.ts`、`tests/d3/copy/test_copy_review_record.py`（6 个用例）；`make lint-copy-lint lint-copy-strict` 两次均 0 违规；`pytest tests/d3/copy tests/e2e/test_release_copy.py tests/ops/test_release_notices.py` 51 passed。COPY-05 已放行英文终止时限与超时参数语境（C-50），COPY-10 在词典与 HTML 字符串中检出交付称谓 D1 至 D4 与 D3.x（C-48）。
  - 基线例外清零：`exceptions = []`、`allow = []`（K09）。
  - 上线后 HTML 复核（05:50 UTC）：zh 落地页与关于页 0 违规，en 落地页只有 C-49 关键词徽标（用户 2026-10-22 决定维持现状）与抽取伪影。
  - 确认状态：评审记录与裁决 C-11、C-45 至 C-51 经用户 2026-10-22 确认（C-49 选维持现状）。
- 证据：`tools/lint/copy-lint.mjs`、`copy-lint.scope.json`、`selftest.mjs`；`tests/d3/copy/`；[D3-文案评审记录.md](D3-文案评审记录.md) §4、§8、§9；`docs/05` §7.10；`LIVE-EVIDENCE.md`。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：建议项 W-08、W-11 至 W-13、W-15 至 W-17 未处理，随 D3.x 或发布任务；W-14 的 V0.3 状态翻转以 `docs/impl/v0.3-release/copy.prepared.json` 备好，封板后应用（会使 `demo-parity` 按设计变红）。

### D3-AC-24 许可与署名

- 判定：**部分通过**；优先级：阻塞；环境：本机、dmax

- 阈值原文（`docs/05` §11.3）：

> NOTICE、THIRD_PARTY_NOTICES、`world_manifest.json`、数据集页面、`/lab/` 页面与 `public` 切片含 CC-BY-4.0 原作者署名与 OSM ODbL 声明；仓库扫描无 FAST-LIVO2 源码与链接产物；产品镜像中无 nvdiffrast；gz 分叉代码保留原版权头

- 实测：

| 子条款 | 判定 | 证据 |
|---|---|---|
| NOTICE、THIRD_PARTY_NOTICES、`world_manifest.json` 含 CC-BY-4.0 原作者署名与 OSM ODbL 声明 | 通过 | 仓库侧由 `check-release.mjs` 的 REL-05 至 REL-09（L03，`selftest-release.mjs` 62 例）检查，被测提交上 `make lint` 通过；`dataset.license`、`citation`、`derivedFrom[]`（含 ODbL）见 D3-AC-02 |
| 数据集页面、`/lab/` 页面与 `public` 切片含署名与声明 | 通过（上线版本目录）；页面渲染未单列 | 上线版本目录 `2026.10.25-1` 含 `LICENSE`（CC BY 4.0）与 `NOTICE`（Sketchfab Mitko 署名、OSM ODbL、SRTM 与 Copernicus、热成像离线渲染声明、"全分辨率视频将另行通过 ModelScope 发布"）；README 许可节已补 CC-BY-4.0、ODbL 与 DEM 署名（K09，W-14）。`/lab/` 页面内署名文案的线上核对：未找到证据 |
| 仓库扫描无 FAST-LIVO2 源码与链接产物 | 通过 | REL-07（`images/slam` 标 `awr.image.scope=internal`，K02）；被测提交上 lint 通过 |
| 产品镜像中无 nvdiffrast | **证据过期** | 归档 `out/scan_images/20261018T233145Z-45184b7d7ec4-dirty/summary.json`（2026-10-18 23:36 UTC，标签带 `-dirty`）：6 个镜像 6 通过，产品镜像错误命中 0，对照镜像 `d3-livo`（FAST-LIVO2）与 `d3-r3-camera-render`（nvdiffrast）按预期被检出（2/2）。此后 `d3-sensorsvc`、`d3-compile`、`d3-test`、`d3-gz`（K14）与 `d3-agent-py`（K18）已重建，被测提交与现镜像上的复扫：未找到证据 |
| gz 分叉代码保留原版权头 | 通过（规则检查） | L03 为 gz 分叉文件补修改声明，由 REL 规则检查；逐文件核对记录：未找到证据 |

- 证据：`tools/lint/check-release.mjs`、`selftest-release.mjs`；`tools/deploy/dmax/scan_images.sh`；dmax `/data/anet-drone4d-d3/d3dev/out/scan_images/20261018T233145Z-45184b7d7ec4-dirty/`；`docs/05` §26 的 L03、L04 行、§25.5 F-14；`LIVE-EVIDENCE.md`。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：`docs/05` §25.5 F-14 要求 RP0a 引用入库后在 HEAD 上重采一次、得到不带 `-dirty` 的标签的镜像扫描，RP0a 与本报告均未得到该次重采。转为"通过"需要在现镜像上重跑 `scan_images.sh`（CPU 作业，不占 GPU；2026-10-18 的上次扫描用时 23:31:45–23:36:15 UTC，4 min 30 s）。

### D3-AC-25 Rerun 数据检视与时间轴同步（v1.3 前移到 P0b，覆盖公开样例）

- 判定：**按 ADR 通过**（ADR-219：两项首帧 ≤ 5 s 移入 D3.x，其余条款按原文判定）；优先级：阻塞；环境：本机 Tier B

- 阈值原文（`docs/05` §11.3）：

> 工作区按需加载 Web Viewer；在三维场景视图中拖动时间轴后 Viewer 当前时间与 lab 播放头误差 ≤ 1 帧（≤ 100 ms 仿真时间），反向亦然（内部 RT 会话中 lab 播放头与 M12 Timeline 一致）；Rerun 数据经路径限定 Cookie `d3_rr` 访问，URL 中不含令牌；100 次往复拖动后无回环震荡（双方事件计数一致）；选择联动正确；`public` 档 10 s 时间窗切片（3 机，≤ 64 MB）与 `basemap.rrd` 首帧可见 ≤ 5 s，内部工作区经 catalog 按需取数首帧可见 ≤ 5 s（均为暂定，P0b 实测后以 ADR 冻结）；`memory_limit` ≤ 1.5 GB；Viewer 未加载时 D1-AC-03b 不回退；公开样例页的面板只读取静态切片、不发起 catalog 请求；面板外壳文案通过 D3-AC-23

- 实测：

| 子条款 | 判定 | 实测与出处 |
|---|---|---|
| 工作区按需加载 Web Viewer | 通过 | `viewerLoader.ts`；`lab-inspect` 部署形态用例 1 两轮通过，catalog 运行进入 READY（K12，36.4 s） |
| 拖动时间轴后 Viewer 时间与 lab 播放头误差 ≤ 1 帧（≤ 100 ms），反向亦然；100 次往复无回环震荡 | 通过（本机 Tier S） | R07，Playwright `lab-inspect.spec.ts`：100 次往复两个方向误差 0 ns，推送数 = 回声数 = 1139；选择联动（`#selection=` 定位）生效 |
| Rerun 数据经路径限定 Cookie `d3_rr` 访问，URL 不含令牌 | 通过 | F05 dmax 部署实测两条路径、URL 不含令牌；R07 本机栈通过；rerun-proxy 透传 `x-rerun-*` 元数据后 FetchChunks 返回 200 |
| 公开样例页面板只读取静态切片、不发起 catalog 请求 | 通过 | F07 请求守卫 `samples/staticGuard.ts`；线上 e2e "/lab/ static build: loads with static GETs only"通过 |
| `memory_limit` ≤ 1.5 GB | 通过（配置） | `viewerLoader.ts` 常量 1.5GB，`inspect.test.ts` 核对；运行时占用的实测：未找到证据 |
| Viewer 未加载时 D1-AC-03b 不回退 | 间接通过 | 线上与本机 public-e2e 的 Tier S 首屏与 D2 基线比较通过；D1-AC-03b 专项复测：未找到证据 |
| 面板外壳文案通过 D3-AC-23 | 通过 | 见 D3-AC-23 |
| 两项首帧可见 ≤ 5 s（`public` 切片与 `basemap.rrd`；内部工作区经 catalog） | 移 D3.x（ADR-219） | 开发参考（Tier S SwiftShader，非正式）：catalog 58.7 s、公开真实切片 70.4 s（R07）；dmax `d3` profile 部署形态 catalog 首帧 50.6 s（K12）。封板须记录的 dmax 硬件 EGL 无头 Chromium 开发参考数字（F-112 (d)）：未找到证据 |

  - 另：Rerun 0.38.1 Viewer 启动时仍请求 GitHub releases 接口，Wasm 内含 tel.rerun.io 使用统计端点；公开页由请求守卫拦截同源 `/lab/`、`/lab-static/` 以外与非 GET 的请求；`/lab/` 构建的 Wasm 字符串等长改写（C-29，已确认）。Viewer 对 catalog 来源发出的 `recording_open` 中 `version` 为 null，面板原先判 694，R07 已修复并随 K12 部署。
- 证据：`tests/e2e/lab-inspect.spec.ts`、`lab-public-samples.spec.ts`、`lab_stack.py`；`apps/web/src/ui/lab/inspect/viewerLoader.ts`、`apps/web/tests/lab/inspect.test.ts`；`docs/05` §28.3 第 68、69 行、§32.3 第 106 行；ADR-203、ADR-219。
- 性能标注（§4.7、ADR-195）：首帧数字为开发参考，不计入。
- 备注：ADR-219 决策 (2) 要求封板记录写明"该项值得测试，以后再测"，并以 dmax 无头 Chromium 开发参考数字入记录；该数字尚未入记录，列入 §4.1 与 §3。

### D3-AC-33 OSM 车道图与车辆运动（v1.3：前移到 P0b 并升为阻塞）

- 判定：**通过**；优先级：阻塞；环境：本机

- 阈值原文（`docs/05` §11.3）：

> 车辆追踪类事件的车辆沿 OSM 车道图运动，路段 100% 落在道路走廊内（v1.6，W11：道路走廊定义为车辆道路中心线按 max(width_m/2, 5 m) 缓冲，与 `ROAD_CORRIDOR` 的 5 m 相同，见 M22 §6.6.11）；IDM 跟驰下车辆间无穿越、最小车距 ≥ 2 m；路口按让行规则通过，无冲突穿越；转弯、停车与泊车关键事件由验证器产出并与磁带一致；`crop-handoff-001` 改用车道图后语义验证仍全部成立；车辆生成确定性（两台机器 `content_sha256` 相同）

- 实测：
  - 车道图与走廊（W11、W13、E09，crop `oryahovo-crop1`）：`crop-handoff-001` 的 `vehicle_C` 走廊采样比例 1.0、超出走廊 0.0 m、行车道 1.0，经过 5 个路口单元，`safety_stops` 0，四条声明与两条关系全部成立，语义验证仍全部成立；走廊定义为车辆道路中心线按 max(`width_m`/2, 5 m) 缓冲（与 `ROAD_CORRIDOR` 的 5 m 相同）。
  - IDM 跟驰与让行：合成路网 8 车审计重叠 0、车距违例 0（最小车距 ≥ 2 m）；路口互斥单元与让行无冲突穿越；转弯、停车与泊车关键事件由磁带验证器 `audit`、`key_events` 与门 3 验证器 `verify/vehicle_track.py` 产出并与磁带一致。
  - 两机确定性（X01）：合成路网 `c2b51f85…`、crop 60 辆 `b9ddd27e…`、`vehicle_track` 合成事件 `content_sha256` `c18127bc…`、`crop-handoff-001` 的 `report.lanes.digest` `94ca4cd0…`，本机（AVX2）与 dmax（AVX-512）相同。
  - 测试：`test_vehicle.py`（14 个用例）、`test_types_vehicle.py`（10 个用例）。被测提交上的重跑：未找到证据（取自 W13、E09、X01 批次）。
- 证据：`python/awr/sim/events/vehicle.py`、`tests/d3/events/test_vehicle.py`、`test_types_vehicle.py`；`docs/05` §27.3 第 52 行、§28.3 第 59 行、§30.3 第 83 行；M22 §6.6.11、§7.3.4。
- 性能标注（§4.7、ADR-195）：不涉及性能项。

### D3-AC-35 Fast 与 Realistic 两档一致性与 gz 健壮性（v1.3：前移到 P0b）

- 判定：**部分通过**；优先级：阻塞；环境：独占 A40、dmax CPU 池

- 阈值原文（`docs/05` §11.3）：

> DroneAdapter 一致性用例集在两档上全部通过；同一磁带与 Run 规格在两档都能运行（对照运行使用 `field` 风模式或 σ_turb = 0，Bench 安全档关闭改变轨迹的 M09 自主动作）；给定相同 PoseHistory 时 M25 输出逐字节相同；同一条轨迹分别经两档的 PoseHistory 采样，LiDAR 逐点位姿差 ≤ 1 mm、IMU 输出差在重采样容差内；Fast 档 CPU L1（`raycast = cpu`）两次运行观测与动作流逐字节一致（同机，并在同 ISA 的第二台机器复测）；档间差异报告 `tier_compare.json`（位置 RMS、到达时刻差、指标差）带容差并写入数据集；gz 看门狗在步进超时 5 s 内重启实例并重放；同种子 reset 复用经验证逐位一致或明确判定为每 Run 重启

- 实测：

| 子条款 | 判定 | 实测与出处 |
|---|---|---|
| DroneAdapter 一致性用例集在两档上全部通过 | 通过（对照配置） | P09（dmax `d3-gz:p09`，2026-10-19）：9/9 用例接口判定与容差判定通过；对照配置为 `RefTracker` 加 compose `px4` 档（ADR-208）；容差冻结于 ADR-208 (2) |
| 同一磁带与 Run 规格在两档都能运行 | 通过 | `tests/d3/phys/conformance/`（两档同一磁带与 Run 规格的对照运行）、`tests/d3/phys/test_fast.py`；B10 第 2 轮为 gz 锁步档 |
| 给定相同 PoseHistory 时 M25 输出逐字节相同；LiDAR 逐点位姿差 ≤ 1 mm、IMU 差在重采样容差内 | 部分通过 | 7,501 个样本时刻逐位相同，位置最大差 2.84e-7 m；LiDAR 300 帧 4,203,660 点逐字节相同；IMU 差 0；CPU 相机替身、LiDAR、IMU 在本机与 dmax 跨机一致；`/lab/compare` 真实数据核对：LiDAR 逐点位姿差 6.7e-14 m。**GPU 相机逐字节一项**（`test_m25_gpu_camera_bytewise`，需 `AWR_D3_TIER_WORLD`）未找到补测记录（F-36） |
| Fast 档 CPU L1 两次运行观测与动作流逐字节一致（同机，并在同 ISA 的第二台机器复测） | 通过 | X01：两台机器上各自两次运行逐字节一致；同 ISA 复测以 dmax 设置 `NPY_DISABLE_CPU_FEATURES="X86_V4 AVX512_ICL AVX512_SPR"` 把 numpy 派发限定为 AVX2，两个场景的观测流与动作流逐字节一致（C-40，用户 2026-10-22 确认）。跨 ISA 对照只记录：crop 场景 900 个观测中 2 个的导航速度相差 1 ulp（numpy AVX-512 float64 `log`），C-41 选 (C)，逐字节可复现只在同 ISA 上成立，归 D3.x |
| 档间差异报告 `tier_compare.json` 带容差并写入数据集 | 通过 | 字段齐全（§26.3 第 36 行）；`tolerance_status` 在 ADR-208 确认后改为冻结 |
| gz 看门狗在步进超时 5 s 内重启实例并重放；同种子 reset 复用验证 | 测试位置登记；逐项实测未找到证据 | `tests/d3/phys/determinism/test_fault.py`；I01 gz 阶段（K14）68 passed、3 skipped；看门狗重启时长与 reset 复用的判定值：未找到证据 |

  - **ADR-208 的生产接线未完成**：ADR-208 决策 (1) 要求 Env 的 `_GzTier` 以 `RefTracker` 与 compose `px4` 档构建 `GzSimBackend`，P0a `Tracker` 只保留给 KinematicTier；ADR-208 已由用户 2026-10-22 确认（R-D3-71），并要求 Env 接线后重做 D3-AC-17 的正式配置对拍（C-20，F-112 (c)）。按本报告起草时对工作树的检索，`GzSimBackend` 的 `tracker` 缺省仍为 `Tracker`，`python/awr/agentsdk/env/` 内未见 `RefTracker` 的使用，接线与对拍重做均未找到完成记录。因此本条的一致性结论目前只适用于对照配置（按 ADR-208 回退条款的口径注明）。
  - `lab/compare` 的 Playwright 用例待补（F-70，属 P2 范围，不计入本条）。
- 证据：`tests/d3/phys/conformance/test_conformance.py`（10 项）、`tests/d3/gz/test_tier_compare.py`、`tests/d3/phys/determinism/test_fault.py`、`tests/d3/phys/test_fast.py`；`docs/05` §26.3 第 36 行、§30.3 第 84 行、§26.5 F-36、§32.5 F-112；ADR-208；C-20、C-40、C-41。
- 性能标注（§4.7、ADR-195）：一致性与逐字节项为内容口径；独占卡口径不适用。
- 备注：转为"通过"需要：Env 接线 `RefTracker` 与 `px4` 档，重做 D3-AC-17 正式配置对拍并更新位姿摘要，补测 GPU 相机逐字节项。

### D3-AC-43 内部排行榜 v1 与内部提交（v1.3：前移到 P0b，不对外）

- 判定：**部分通过**；优先级：阻塞；环境：dmax

- 阈值原文（`docs/05` §11.3）：

> 分轨 `std3-l2` 与 `std3-l1` 在榜单版本 v1 上：3 个基线参考行与 ≥ 2 个提交条目完成"8 个 test 事件 × 5 种子 × 2 重复"的正式评测，并按赛道显示四项指标及其 95% 置信区间、逐项名次与并列组、非支配层级（v1.4，不显示合成主得分）；只收 gz 锁步档的正式运行；内部成员提交的 Agent 容器在加固沙箱中运行；test 集真值访问审计日志中外部访问为 0，越权访问返回 691；排行榜接口最低角色为 `invited`，匿名与公开站访问返回 404；提交者撤回后 24 h 内条目下线

- 实测：
  - 正式评测（B21，dmax GPU 3，A 级，2026-10-21 20:13 至 10-22 09:17 UTC；记录 [D3-排行榜v1-正式评测记录.md](D3-排行榜v1-正式评测记录.md)）：2 个分轨（`std3-l2`、`std3-l1`）× 4 个赛道，每个"分轨 × 赛道"5 个条目（3 个基线参考行 `static_guard`、`coverage_scan`、`track_share` 加 2 个内部提交 `relay_scan`、`close_track`），全榜 40 个条目；每个条目在其赛道的 2 个 test 事件上各 5 种子 × 2 重复 = 20 次运行，全榜覆盖 8 个 test 事件，共 800 次 gz 锁步 realistic 运行（基线 480、提交 320）。报告 `board_v1_formal.json` 14/14 项判定通过。
  - 榜单内容：按赛道显示四项指标及 95% 置信区间、逐项名次与并列组、非支配层级；`score_version = d3-score-1`、`weights = null`，`board.json`、`details.json`、接口响应中均无合成主得分；基线不参与名次。
  - 沙箱与提交流水线：4 个提交均经扫描（Trivy）→ 冒烟（Fast 档 dev 事件）→ v1 正式评测 → 计分 → DONE；Agent 容器用户 65534、无网络、只读根、`cap-drop ALL`、`no-new-privileges`、seccomp `d3-agent-v1`、pids 256、1 CPU、2 GiB、`/tmp` 256 MiB、无 GPU。
  - 访问控制与审计：test 真值读取审计外部访问 0，越权探测返回 691；匿名访问 404；`bench/obs.v1` 白名单审计未知字段 0；隐藏区目录 0700、文件 0600、属主 10002；仓库内公开锁 `configs/bench.d3/board-v1/lock.json` 只含承诺值，不含 test 事件 id。emax 公开站 `/api/bench/*` 全部 404（D3-AC-21 子条款 2）。
  - 重评版本 v1.1（K16，2026-10-22，评测器 0.3.0，评分版本 `d3-score-2`）：B21 的 search 赛道因评测器只读 `plan.json` 的 `annotations[]`、而 `target_search` 的计分事件在事件真值 `key_events[]` 中，导致该赛道四项指标对全部条目为 0（B21 保留意见 F-102）。v1.1 由保留的运行记录与隐藏区真值离线重评全部 800 次运行（不重新仿真，墙钟 160.7 s）：search 赛道 200 次改变，其余 600 次去掉版本字段后逐字节一致，6 个非 search 榜单块与 v1 逐字节一致；v1.1 报告 13 项全部通过；`board.json` `0336e205…`、`details.json` `c9364024…` 两次发布逐字节一致；v1 只读，sha256 不变；实验室 `boards/current` 为 v1.1。
  - **未满足或开放**：(1) 4 次平台原因 L2 运行（3 个基线运行、1 个提交运行；L2 动作在首拍同拍超时，9 个 Agent 全部在 k = 0 超时一次，之后按时，判定为宿主停顿所致）的评分在 v1.1 中为 0（例如 `close_track` 的 L2 search EC 为 0.950），重跑工具 `board_v1_formal.py rerun-platform` 已覆盖基线与提交运行，但重跑须在独占卡窗口执行，**未执行**（F-124 (a)）；重跑前不得执行 `publish_board`，否则发布阶段的精简会处理这 4 个运行；(2) "提交者撤回后 24 h 内条目下线"未在正式榜上演练，由 B22 本机端到端测试覆盖，读取侧已即时隐藏，周期性 `delist_withdrawn` 作业尚未接入（F-97 (a)）；(3) B21 记录的另一发现：L2 动作超时来自宿主 CPU 争用（F-103，正式窗口内宿主负载 15–120），是否冻结宿主变更或改用 CDI 注入 GPU 仍开放；(4) vehicle 赛道 AO 按定义只对人员计算、EU 因无车辆事件上报为 0，reid 与 vehicle 类型的 `report_map` 接入会改变 EU，须新评分版本（F-124 (c)），属定义与范围而非缺陷，报告中注明。
- 证据：[D3-排行榜v1-正式评测记录.md](D3-排行榜v1-正式评测记录.md) §0；[D3-排行榜v1-重评记录.md](D3-排行榜v1-重评记录.md) §0、§5；`tests/d3/board/test_e2e_v1.py`（11 个用例）、`test_board_v1_formal.py`（15 passed）；`tools/bench/d3/board_v1_formal.py`；`docs/05` §31.3 第 94、95 行、§32.3 第 102、103 行、§33.3 第 122 行、§32.5 F-102、F-103、§33.5 F-124。
- 性能标注（§4.7、ADR-195）：B21 为 A 级独占（GPU 3，ADR-194 决策 (7)）；重评为 CPU 作业，不占 GPU。
- 备注：转为"通过"需要 4 次重跑完成并重新发布 v1.1（F-124 (a)）；F-124 (d) 的公开锁 `lock.v1.1.json` 尚未入库（仓库内 `configs/bench.d3/board-v1/` 只有 `board.json` 与 `lock.json`）。

### D3-AC-59 受邀成员提交与容器加固

- 判定：**部分通过**；优先级：阻塞；环境：dmax

- 阈值原文（`docs/05` §11.3）：

> 管理员签发 `invited` token 与 ZeroTier 授权后，受邀成员只能访问 lab-web、bench-api 与 `d3-registry`，对 catalog、实时 topic、真值接口与他人提交的访问返回 403 `115` 并审计；token 撤销后会话与推送令牌 ≤ 2 s 失效；镜像推送、扫描（Trivy HIGH 及以上未修复漏洞即拒绝，体积 > 4 GB 拒绝，692；v1.6 口径说明（B14，待用户确认，§23.5 C-13）："HIGH 及以上未修复漏洞"指镜像未修复、上游已有修复版本的漏洞（Trivy `FixedVersion` 非空，与 `--ignore-unfixed` 相同），上游无修复的只计数并记录；体积按 `docker inspect` 未压缩 `Size`，GB 为十进制）、冒烟验证与正式评测流程完整；配额：超出每周正式提交数或 GPU 时间上限返回 681；负向用例：越权系统调用、联网尝试、fork 炸弹、写满 tmpfs、读取 `/data/d3/hidden` 的尝试被加固策略阻断（691 并审计）且不影响同机其他作业；超墙钟上限被终止（693）；结果缺省只对提交者团队可见

- 实测：

| 子条款 | 判定 | 实测与出处 |
|---|---|---|
| `invited` token 与角色边界：仅 lab-web、bench-api、`d3-registry`；catalog、实时 topic、真值接口、他人提交返回 403 `115` 并审计 | 通过 | F09 `lab-roles`（本机栈 8/8）；K12 部署形态 4 × 12 角色矩阵通过，Cookie 带 Secure；`tests/d3/lab/`；发现并修复 bench-api 共用 SQLite 连接并发访问导致的间歇 500/401 |
| token 撤销后会话与推送令牌 ≤ 2 s 失效 | registry 通过；会话与推送令牌的逐项秒数未找到证据 | B25：registry 撤销生效最大 0.013 s（5 次） |
| 镜像推送、扫描（Trivy HIGH 及以上未修复漏洞即拒绝，体积 > 4 GB 拒绝，692）、冒烟、正式评测流程完整 | 通过 | B14 `test_image_scan.py`（C-13 口径经用户确认）；B25 `d3-registry`（distribution v3.0.0、`d3-registry-authz`、nginx TLS）演练 30/30 项，推送 → 接入 → `scan_image`（Trivy 0.75.0）通过；B21 的 4 个提交经扫描、冒烟、正式评测；K18：新基础镜像 `d3-agent-py`（`7cd2988c4334`，pillow 12.3.0、msgpack 1.2.2）与两个示例镜像可修复 HIGH/CRITICAL 0、242.7 MB，通过；对照旧 `:b21` 基础镜像 13 个 HIGH，拒绝 |
| 配额：超出每周正式提交数或 GPU 时间上限返回 681 | 通过 | `tests/d3/submit/test_quota_scope.py`、`tests/d3/deploy/test_registry.py`（22 个用例） |
| 负向用例（越权系统调用、联网、fork 炸弹、写满 tmpfs、读取 `/data/d3/hidden`）被加固策略阻断（691 并审计）且不影响同机其他作业；超墙钟上限被终止（693） | 通过 | B24：`tests/d3/submit/test_redteam.py` 26 个用例，2026-10-19 26 passed；RP0a 补跑中该文件在本机通过；审计局限 C-30（不授予 CAP_SYSLOG，以主进程退出码 159 与诱饵访问为判据） |
| 结果缺省只对提交者团队可见 | 通过 | `tests/d3/lab/`、`lab-roles` 的数据范围用例；"受邀成员 × `:rid`"两格与用例 4 至 8 只在本机栈验证，部署上没有受邀成员团队的受管运行（F-108 (f)） |
| ZeroTier 授权 | **未应用** | 策略工具 `ztmember.py` 已实现并在规则求值器上验证，**尚未应用**；用户 2026-10-22 决定 C-36 封板后办理，封板时不批准受邀外部成员 |
| subworker 镜像接入门 `registry_gate`（K05）的 dmax 部署与演练 | 未找到证据 | F-67 中该部署演练一项未见关闭记录 |

  - 其他：Trivy 离线库更新于 2026-10-18 18:32 UTC，至 2026-10-25 为 6.3 天（告警阈值 7 天、拒绝阈值 30 天），封板前重扫须先刷新（F-126 (f)）；镜像摘要在 overlay2 宿主上为配置摘要（C-36 (c)）。
- 证据：`tests/d3/submit/`、`tests/d3/deploy/test_registry.py`、`tests/e2e/lab-roles.spec.ts`；`tools/deploy/dmax/registry/`；[D3-正式运行稳健性与安全.md](D3-正式运行稳健性与安全.md) §0、§4、§8；[D3-排行榜v1-正式评测记录.md](D3-排行榜v1-正式评测记录.md) §0.1；`docs/05` §28.3 第 66、67 行、§33.3 第 125 行；C-13、C-30、C-36。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：转为"通过"需要：对封板后才会应用的 ZeroTier 策略做出判定归属（本条在封板范围内，用户已决定封板时不开放外部成员，是否因此把该子条款视为顺延，需用户裁定），并执行 `registry_gate` 的 dmax 部署演练。

### D3-AC-61 热成像

- 判定：**通过**；优先级：阻塞；环境：dmax（单卡绑定）

- 阈值原文（`docs/05` §11.3）：

> 辐射换算：波段积分 Planck 表的正反变换往返误差 ≤ 0.01 K（200–400 K）；构造场景：(a) 均匀 300 K、ε = 1 的平板，无噪声输出表观温度误差 ≤ 0.05 K；(b) ε = 0.9 的水平面在晴空（T_sky = T_air − 25 K）下的表观温度与解析解差 ≤ 0.1 K；(c) 雾中 1 km 外目标的表观温差按 `τ(d)` 衰减，与解析解差 ≤ 5%；噪声：均匀 300 K 场景逐像素时间标准差在标称 NETD 的 ±10% 内，固定图案噪声在运行内恒定、换种子改变；动态实体：人员与车辆（发动机运转与熄火 600 s 后）表观温度符合 `thermal_table_v1` 与 DSL 参数（误差 ≤ 0.2 K，无噪声档）；`ThermalObs` 通过 `bench/obs.v1` 校验，交付字节即存档字节；同镜像同型号 GPU 两次运行逐字节一致；热成像识别判据（`h_px_unocc ≥ 16`、可见比例 ≥ 0.5、`\|dT_app_k\| ≥ 1 K`）有构造用例

- 实测：
  - 条款登记用例 `test_ac61_clause_registry_complete` 与 `test_ac61_thresholds_match_docs05` 校验阈值与 `docs/05` 一致且每个分句均有用例；用例覆盖：Planck 表往返（`test_planck_table_round_trip_within_10_mk`）、(a) 均匀 300 K 平板（`test_case_a_uniform_300k_blackbody_plate`、`test_gpu_plate_300k_eps1_and_clear_sky_eps09`）、(b) ε = 0.9 水平面晴空（`test_case_b_eps09_horizontal_surface_clear_sky`）、(c) 雾中 1 km 衰减（`test_case_c_fog_contrast_attenuates_with_tau`、`test_gpu_fog_1km_contrast_follows_tau`）、NETD 与固定图案噪声（`test_per_pixel_temporal_std_within_10pct_of_netd`、`test_fpn_is_constant_within_run_and_changes_with_seed`）、人员与车辆表观温度（`test_person_and_dog_temperatures_and_dsl_override`、`test_vehicle_engine_running_and_600s_after_off`、`test_gpu_dynamic_entities_match_table`）、`ThermalObs` 与 `bench/obs.v1`（`test_thermal_obs.py`，含交付字节即存档字节）、识别判据边界（`test_ident_predicate_boundaries`）。
  - 同镜像同型号 GPU 两次运行逐字节一致（`test_gpu_two_processes_byte_identical`，S18）：`d3-sensorsvc:dev`（`sha256:428ff448ba20…`），A40，驱动 580.95.05，dmax GPU 3 共享卡，99/99 通过；识别用例 h 20 / 14 px、vis 0.844 / 0.156、dT 与 δ 相差 ≤ 0.021 K。
  - 热成像测试函数数：`test_thermal_core.py` 23、`test_thermal_render.py` 20、`test_thermal_obs.py` 9、`test_thermal.py` 10（`test_thermal.py` 参数化展开后为 99 个用例，S18 在 dmax 上 99/99 通过；其余三个文件的 dmax 计数未单列：未找到证据）。各分句的数值余量未在 `docs/05` 逐项记录：未找到证据（以用例通过为准）。
- 证据：`tests/d3/sensors/test_thermal.py`、`test_thermal_core.py`、`test_thermal_render.py`、`test_thermal_obs.py`；`docs/05` §26 的 S13、S17、S18 行、§26.3 第 34 行。
- 性能标注（§4.7、ADR-195）：内容口径（字节一致与物理度量）；`therm512` 吞吐见 D3-AC-11。
- 备注：F-56：Env 的 `L2Sensors` 尚未挂载热成像相机，真实 Env 运行不产生热成像；公开样例的热成像视频为离线渲染（`NOTICE` 已声明）。该点不影响本条条款，影响 D3-AC-63 的核验范围。

### D3-AC-62 第一批四类事件

- 判定：**通过**；优先级：阻塞；环境：本机、dmax

- 阈值原文（`docs/05` §11.3）：

> 四类各有 ≥ 1 个 dev 与 ≥ 2 个 test 事件 strict 编译通过且登记在对应划分；Re-ID：目标路线含 ≥ 2 处持续 ≥ 5 s 的遮挡，验证器产出的 `target_reacquire` 与磁带一致，目标与干扰者属性距离 ≥ 2（门 1 拒绝不合格配置）；交汇与交接：`meet`、`handover`、`disperse` 与关系边由验证器产出并导出 OpenLABEL，物品容器转移与磁带一致；车辆追踪：见 D3-AC-33，关键事件 `turn`、`stop`、`park` 与磁带一致；目标搜索：实际位置由先验与事件种子确定性抽样，`brief.json` 与 `bench/task.v1` 只含先验不含实际位置（泄漏扫描 0 命中），60 s 窗口关键事件计数正确；四类事件的基线评测均完成且各赛道的四项指标与置信区间可计算（v1.4）；外观库与车辆资产许可登记齐全

- 实测：
  - 事件集（E10）：`oryahovo-crop1` 上共 13 个事件。dev 5 个：`crop-reid-001`、`crop-handoff-001`、`crop-handoff-002`、`crop-vehicle-001`、`crop-search-001`（源文件在 `configs/events.d3/{reid,handoff,vehicle,search}/`，`crop-handoff-001` 保留在顶层）；test 8 个（四类各 2 个），不入 git，只存 dmax `<data_root>/hidden/<事件>/event/v1/`。登记于 `configs/bench.d3/split.yaml`（`drone4d.bench.split_set.v1`，承诺哈希），`test_split_version = v1`，摘要 `2ca09b23b4e6e244…`。全部事件 strict 与 ranked（`0.2.0-detour`）编译通过，本机与 dmax 哈希相同。
  - 类型验证器：Re-ID（`target_reacquire`、门 1 属性距离 ≥ 2 拒绝不合格配置）、交汇与交接（`meet`、`handover`、`disperse` 与关系边、OpenLABEL 导出、物品容器转移）、车辆追踪（见 D3-AC-33）、目标搜索（实际位置由先验与事件种子确定性抽样，`brief.json` 与 `bench/task.v1` 只含先验，泄漏扫描 0 命中，60 s 窗口关键事件计数）；测试 `test_types_reid.py`、`test_types_rendezvous.py`、`test_types_search.py`、`test_types_vehicle.py`、`test_event_set.py`、`leak_redteam/test_inversion.py::test_brief_and_task_contain_only_priors`、`baselines/test_event_types.py`。
  - 外观库与许可：`appearance_lib@1` 为自制程序化资产（24 个人物、16 个车辆），许可登记 `assets/actors/LICENSES.md`。
  - 四类基线评测：B21 的 800 次运行覆盖四类事件的基线与提交，各赛道四项指标与置信区间可计算；search 赛道在 v1 中四项恒为 0，已由 v1.1 修正（见 D3-AC-43）；vehicle 赛道 AO 按定义只对人员计算、EU 无车辆事件上报，两项为 0，属定义所致。
- 证据：`tests/d3/events/`、`tests/d3/leak_redteam/`、`tests/d3/baselines/`；`configs/events.d3/`、`configs/bench.d3/split.yaml`；`docs/05` §30.3 第 87 行、§32.5 F-102。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：test 事件的 id 不在本报告中列出（仅登记数量）。

### D3-AC-63 数据集导出（配套能力）

- 判定：**部分通过**；优先级：应交付；环境：dmax CPU 池

- 阈值原文（`docs/05` §11.3）：

> dev 运行的数据集导出三层目录（事件、真值、运行）与 `run_manifest.json` 通过 schema 校验；逐文件导出（RGB、热成像 u16、`depth_log16`、LiDAR、bag）与 `raw.mcap` 内容一致；test 划分与评测运行不可导出（691）；派生物按保留期清理（v1.6 口径，B11："评测运行"指保留级别为 `ranked_eval` 或 `kind = submission` 的运行，导出返回 691，`detail.why = eval_run`）

- 实测：
  - 导出器 `python/awr/bench/export/dataset.py`（B16，版本 1.1.0 含 B26）与 `retention.py`；`tests/d3/bench/test_export.py` 16 个用例（B16 的 13 个加 B26 的 3 个），覆盖三层目录与 `run_manifest.json` 的 schema 校验、逐文件导出（RGB、热成像 u16 为 `<uav>/thermal_u16/<seq>.png`（PNG16，0.01 K/LSB）、`depth_log16`、LiDAR、bag）与 `raw.mcap` 内容一致、`test` 划分与评测运行返回 691（`detail.why = eval_run`）、派生物保留期清理（`python -m awr.bench.export.retention gc`）。
  - **未满足**：(1) 条款方法要求"正式核验由 RP0b 在 dmax CPU 池执行"，该核验在被测提交上**未执行**，也没有此前批次的等价记录（未找到证据）；(2) 热成像导出在真实运行上无法核验：Env 的 `L2Sensors` 未挂载热成像相机（F-56），热成像的逐文件一致性只有用例级证据；(3) OpenLABEL 只有事件级真值导出，数据集导出返回 689，LeRobot 未实现（属 D3-AC-42，P3 顺延，不在本条范围）。
- 证据：`tests/d3/bench/test_export.py`；`docs/05` §26 的 B16、B26 行、§27.5 F-56；[D3-批量基准记录.md](D3-批量基准记录.md) §0。
- 性能标注（§4.7、ADR-195）：不涉及性能项。
- 备注：本条为应交付条款，P0b 的应交付项须 2/2 通过（ADR-194 决策 (3)）。转为"通过"需要在 dmax CPU 池对一个真实 dev 运行执行导出并核验 `verify_export`，以及一次 test 划分与评测运行的 691 拒绝。

### D3-AC-64 高斯外观层评估与决策备忘

- 判定：**通过**；优先级：应交付；环境：dmax

- 阈值原文（`docs/05` §11.3）：

> 按 §6.4 协议完成 G1–G8 八项指标的测量或引用（并行 Skyfall-GS 实验的结论入库并注明版本），形成决策备忘与 ADR 草案（A、B、C 三种结论之一）；如结论为 A，给出 P1 生成式高斯外观层的范围与工作量重估。v1.4：首轮证据为 r11，裁决见 ADR-187（结论 C）；G4 的 KID 在无真实航拍参照集时登记为"未测"并注明原因，G5 由 WP-D3-37 补测（§6.4.2）；补测结果不改变结论 C 时本条按已形成的备忘与 ADR 验收。v1.4：裁决经用户确认为结论 C0（ADR-188）；G4 的 KID 移至 P3 入口门以公开参照集测量（D3-AC-68），本条在 P0b 按已形成的备忘、ADR 与 G5 补测结果验收。G5 补测已完成（2026-10-19，r11 §4.6）：AP 下降 ≤ 2 个百分点通过；"有真实验证集时差距不扩大"因无真实航拍验证集登记未测；评审记录见 [impl/D3-G3DGS-评审记录.md](impl/D3-G3DGS-评审记录.md)（H06，先行生效，待封板前用户确认）；G5 幻觉审计按 ADR-206 冻结

- 实测：
  - 决策备忘 `docs/research/d3/r11-skyfall-gs.md`（含 2026-10-19 补测的 §4.6）与评审记录 [D3-G3DGS-评审记录.md](D3-G3DGS-评审记录.md)（H06）：结论 C0，维持 ADR-187（结论 C）与 ADR-188（C0）；研读对象 `jayin92/skyfall-gs@7319484`。
  - G1 至 G8 逐项测量或引用：G1 与 G2 在 v1.3 门限下不通过，G3 不可判定，G4 不通过（KID 因无真实航拍参照集登记为未测，v1.4 起移至 D3-AC-68），G5 通过，G6 通过，G7 算力通过，G8 有条件通过。
  - G5 补测（`tools/bench/d3/g5_eval.py`，dmax GPU 2，2026-10-19）：b4 的 mAP 变化 −1.70 / −0.49 个百分点（IoU 0.5 / 0.3；区间上界 −0.30 / +0.28），满足下降 ≤ 2 个百分点；产物 A −1.52 / −0.17；"有真实验证集时差距不扩大"因无真实航拍验证集登记为未测（条件不成立）；幻觉审计 b4 每帧新增 0.098（≤ 0.1，暂定门限），冻结方案（ADR-206）下净增 −0.12 ≤ 0.1、新增 0.098 ≤ 0.157，44 个新增检测全量目视复核，未见生成目标；两种门限下均通过。
  - 确认状态：评审记录原文的"用户确认"栏为待确认；ADR-206（C-15）、C-11、C-18 落在用户 2026-10-22 批量确认范围内（ADR-194 至 217、C-01 至 C-52），评审记录自身的确认栏未回填。
- 证据：[D3-G3DGS-评审记录.md](D3-G3DGS-评审记录.md) §0、§2、§6；`docs/research/d3/r11-skyfall-gs.md`；`tests/d3/bench/test_g5_eval.py`；`docs/05` §25.3 第 26 行、§26 的 G64、H06 行；ADR-187、ADR-188、ADR-206。
- 性能标注（§4.7、ADR-195）：G6 吞吐为 gsplat 单视角开发数字，随 D3-AC-69（P3）以全链路口径测量；不计入。
- 备注：`gs_eval` 产品化（M25-FR-193、200）移入 WP-D3-42（D3.x 顺延，不作为封板门）。

## 3. 不通过项与封板阻塞

按 ADR-194 决策 (3)，阻塞项须全部通过、P0b 应交付项须 2/2 通过、D3-AC-01 回归门持续全绿。回归门在被测提交上全绿（§1）。下表列出 P0b 中所有未判定为"通过"或"按 ADR 通过"的条款，以及虽判定通过但附开放项的条款。"阻塞类型"一列：**用户决定**表示需要用户在选项中选择；**工程补证据**表示需要执行测试或实现后重判；**待确认**表示先行生效的 ADR 或 C 项待用户确认。

| 编号 | 条款 | 现状 | 阻塞原因 | 阻塞类型 | 处置与预计 |
|---|---|---|---|---|---|
| D3-AC-21 | 公开站只读样例视图 | **不通过**：WS 推送时延 p95 增幅 +66.7% 至 +338.8%（判据 ≤ 10%）；D2-AC-04 容量用例线上复测未找到证据 | 实测不通过；emax 公网出口约 3 Mbps，设计限速与并发远超出口；`/lab/` 已撤下 | 用户决定 | 三个选项见 §2 D3-AC-21：(1) 提升带宽并下调 `/lab-static/` 单访客限速、新 ADR、公网侧重测；(2) 视频与 Rerun Wasm 外部分发（ModelScope 或 CDN），需前端改动与 ModelScope 令牌；(3) 豁免移入 D3.x，与 ADR-194 决策 (3) 冲突，须用户明确批准。无论选哪个，D2-AC-04 容量用例须在 emax 空闲窗口补测 |
| D3-AC-43 | 内部排行榜 v1 与内部提交 | 部分通过：4 次平台原因 L2 运行（3 个基线、1 个提交）待重跑，v1.1 中为 0 分 | 需独占卡窗口（GPU 2 或 3）重跑并重新发布 v1.1；同时影响 D3-AC-57 (d) | 工程补证据（需运维协调独占窗口） | 按 F-124 (a) 执行 `rerun-platform`，之后 `report` 与 `publish-lab --versions v1.1`；重跑前不得执行 `publish_board`；预计窗口长度未找到估计 |
| D3-AC-22 | GPU、存储、评测窗口与背压 | 部分通过：证据帧条款开放（F-107）；30 天到期精简无定时作业（F-125 (a)） | 规格二选一尚未做出；定时作业未建 | 用户决定（规格）与工程补证据 | 在 ADR-163 与 M27-FR-171 中二选一证据帧真值来源并落实；以 uid 10002 建立每日 `retention_gc` |
| D3-AC-11 | 相机档位吞吐 | 按 ADR 通过，附 C-54：渲染进程数上限形态下 3 并发的 A 级复测未做 | 计入口径待确认（C-54 先行生效）；复测需 A 级窗口 | 待确认 | 用户确认 C-54，或在 GPU 2/3 恢复 A 级后执行 `cam.sh std-x3 --runs 3 --online-sim-s 360 --repeats 3 --render-procs 6`（F-122 (a)） |
| D3-AC-13 | LiDAR/IMU 输出合同 | 部分通过：`slam_cam` 云台读数 xfail（F-52）；C4 口径无冻结 ADR | 子条款未关闭 | 工程补证据 | 修复 `capture.py` 使 xfail 转通过；以 ADR 冻结 C4 在 clean 档与 PoseHistory 上判定（C-31） |
| D3-AC-15 | 多机 LiDAR 采集与重建对比 | 部分通过：对比视图的 Playwright 与 Tier S/Tier B 显示未找到证据 | 缺视图实测；Tier B 部分归属表述不一致（RP0a §4.3 对 ADR-219 (4)） | 用户决定（归属）与工程补证据 | 用户裁定 Tier B 显示是否顺延；补 `lab/lidar-compare` 的 Playwright Tier S 用例 |
| D3-AC-24 | 许可与署名 | 部分通过：镜像扫描归档为 2026-10-18 旧标签与旧镜像 | 现镜像无扫描证据 | 工程补证据 | 在 dmax 对现镜像重跑 `scan_images.sh`（F-14 要求的不带 `-dirty` 标签重采） |
| D3-AC-35 | 两档一致性与 gz 健壮性 | 部分通过：GPU 相机逐字节未补测（F-36）；ADR-208 生产接线与 D3-AC-17 对拍重做未完成（F-112 (c)） | 生产路径仍用 P0a `Tracker`；对拍重做会改变 D3-AC-17 的位姿摘要 | 工程补证据（并影响 D3-AC-17） | Env 接线 `RefTracker` 与 `px4` 档，重做 P05 正式配置对拍，补测 GPU 相机逐字节项 |
| D3-AC-59 | 受邀成员提交与容器加固 | 部分通过：ZeroTier 策略未应用；`registry_gate` 的 dmax 部署演练未找到证据 | C-36 封板后办理（用户决定）；演练未执行 | 用户决定（子条款归属）与工程补证据 | 用户裁定 ZeroTier 子条款在封板时的判定方式；执行 `regctl.sh materialize-drill` 与 subworker 部署核对（F-67）；封板前刷新 Trivy 离线库 |
| D3-AC-63 | 数据集导出（应交付） | 部分通过：RP0b 正式核验未执行；真实运行无热成像 | 应交付项须 2/2（ADR-194 决策 (3)） | 工程补证据 | 在 dmax CPU 池对一个真实 dev 运行执行导出与 `verify_export`，并验证一次 test 划分与评测运行的 691 |
| D3-AC-25 | Rerun 数据检视与时间轴同步 | 按 ADR 通过；ADR-219 要求的 dmax 无头 Chromium 开发参考数字未入记录 | 封板记录缺项 | 工程补证据 | 按 F-112 (d) 在 dmax 硬件 EGL 无头 Chromium 上测首帧与 6 路并排偏差，注明"开发参考，非正式" |
| D3-AC-01 | D2 零影响回归门（P0a，汇总用） | 通过，依赖 ADR-220 | ADR-220 与 C-53 先行生效待确认；4 个首轮失败用例在 D2 标签上的对照未见记录 | 待确认 | 用户确认 C-53；否决时先在 `d2` 修正不稳定用例（`v0.2.3` 候选，F-123 (b)） |

不通过项共 1 条（D3-AC-21），部分通过 8 条。除 D3-AC-21 外，其余各条均不是阈值未达，而是证据缺口或口径待确认；补齐后可在同一环境重跑转为通过或按 ADR 通过。

## 4. 遗留

### 4.1 需要用户处理

| # | 事项 | 说明 |
|---|---|---|
| 1 | **D3-AC-21 处置选项** | 见 §2 与 §3：提升带宽并下调限速、外部分发、或豁免移入 D3.x（与 ADR-194 冲突）。封板阻塞，决定后才能确定公开样例上线方式 |
| 2 | 确认 ADR-220 与 C-53、C-54 | ADR-220（回归门失败用例隔离重跑）、渲染进程数上限与 C-44 的适用范围，均已先行生效 |
| 3 | 独占卡窗口 | D3-AC-43 的 4 次重跑（F-124 (a)）与 D3-AC-11 的 3 并发复测（F-122 (a)）需要 GPU 2 或 3 的 A 级窗口；GPU 2 存在外部租户的间歇占用 |
| 4 | D3-AC-15 的 Tier B 显示子条款归属 | RP0a §4.3 与 ADR-219 (4) 的表述不一致，请裁定 |
| 5 | D3-AC-59 的 ZeroTier 子条款 | C-36 封板后办理；请裁定封板时该子条款的判定方式 |
| 6 | F-107 证据帧规格 | 在 ADR-163 与 M27-FR-171 中二选一（`l2.jsonl` 的 `bbox_xywh`，或录制 `camera_gt`） |
| 7 | ModelScope 令牌 | 全分辨率样例包与 `m1geo_full.parquet` 的上传依赖令牌（若选 D3-AC-21 的方案 (2)，也是前提） |
| 8 | 封板日发布动作（F-127） | 应用并删除 `copy.prepared.json`（W-14 状态翻转，会使 `demo-parity` 按设计变红）→ RP0b 与 Z01 报告入库 → 合入 `main` 与 `v0.3.0` 标签 → GitHub Release → Z03 `plan` 与 `apply --confirm v0.3.0`；CITATION 与 BibTeX 的 0.3.0 指发布标签，产品版本号沿用 0.2.0（C-24 先例），请确认 |
| 9 | 互联网侧端口扫描（D3-AC-51，F-119） | 在任一公网主机对 dmax 公网地址执行，期望 lab 端口 0 个开放 |
| 10 | C-52 离机加密备份与 `hidden-seeds/` 备份 | 封板后办理；封板前在 dmax 本机另做一份加密副本（R-D3-74） |

### 4.2 工程遗留

| # | 事项 | 归属 |
|---|---|---|
| 1 | F-52：`capture.py` 对无云台相机不加编码器噪声；N1 原因名未进 642 detail；C4 冻结 ADR | M25、S14 |
| 2 | ADR-208 生产接线、D3-AC-17 对拍重做、GPU 相机逐字节补测（F-112 (c)、F-36） | M24、M25、M26 |
| 3 | F-37：Agent 启动器的主机名、随机令牌目录、`DRONE4D_SOCK`、`DRONE4D_PROTOCOL` 规格（strict xfail），无关闭记录 | M26 |
| 4 | F-56：Env 的 `L2Sensors` 挂载热成像相机，之后核验真实运行的热成像导出 | M25、M27 |
| 5 | F-125：到期精简定时作业；SCORING 孤立提交恢复；`GzInstance` 退出自删 `ctl.sock`；`platform_audit` 摘要同名；`superseded-<n>` 保留期限写入 M27-FR-171 | M27、运维 |
| 6 | F-97 (a)：周期性 `delist_withdrawn` 接入；F-95：榜单"原型""共享卡"标签 | M27、M15 |
| 7 | F-124 (b) 至 (e)：search 赛道 EC 接近上限（60 s 窗口定义）；reid 与 vehicle 的 `report_map` 须新评分版本；公开锁 `lock.v1.1.json` 入库；dmax `$D/k16` 清理 | M27、提交代理 |
| 8 | F-122 (b) 至 (e)：同名 gz 容器冲突；`--memory 16g` 硬限与页缓存；`RENDER_PROCS_MAX`、`SESSIONS_CAP_*` 动态估算（D3.x）；dmax `$D/k13` 清理 | M25、运维 |
| 9 | F-123：(a) `test_render_group_gpu_loss_is_647` 在容器内竞态（`procgroup.py` 读到 EOF 后先 `join(timeout)`）；(b) `v0.2.3` 候选修正 D2 不稳定测试；(c) perf 前置条件 `load_max_per_cpu` 由 0.5 收紧到 0.4，本次门执行时为 0.437；(e) `d4ale-*` 容器共用 `d3-sensorsvc:dev` 镜像 | K18/M25、H04、G04、运维 |
| 10 | F-126：subworker 对 647 暂停领取；CDI 方式注入 GPU；`requirements-d3.lock` 与 `pylib-gz.lock` 中 pillow 仍为 11.3.0；`write_load.py` 与 `determinism_official.py` 自行推导缓存路径；坠毁机体静态化归 D3.x；Trivy 离线库刷新；封板清理 `d3dev/k18/`、`:k18`、`:pre-k18` | M27、M25、镜像 |
| 11 | F-120：geosvc 服务进程 RSS 907 MiB > M22-NFR-005 的 700 MiB（不属 D3-AC-05 条款） | M22 |
| 12 | F-103 (b)：正式窗口内冻结宿主变更（systemd 重新加载）或 CDI 注入 | M27、运维 |
| 13 | `verify.py` 的 `d2` 检查对 POST 期望 404 而 D2 API 在路由前返回 403；`labctl pin` 按公开 id 匹配运行目录，公开 id 与内部 id 不同时匹配不到 | 部署 |
| 14 | F-84：SLAM 风与湍流对照（D3.x）；F-110：`lidarCompare.test.ts` 夹具区间同步为 [0.07, 0.18]；F-85：dmax 证据目录封板后统一清理 | M27、M15、部署 |

### 4.3 范围外的说明

- Tier B 参考设备相关的三项正式测量（D3-AC-25 的两项首帧 ≤ 5 s、D3-AC-21 的 6 路并排偏差 ≤ 1 帧）按 ADR-219 移入 D3.x 顺延清单，记"该项值得测试，以后再测"；封板记录以 dmax 无头 Chromium 开发参考数字注明非正式。D3-AC-15、51 的环境口径不受 ADR-219 改变，见 §4.1 第 4 项。
- D3-AC-42（批量运行与标准化数据集导出）属 P3，按 ADR-194 决策 (2) 移出封板，去向为 D4 候选；LeRobot 与 OpenLABEL 数据集导出随之顺延。
- C-41（跨 ISA 逐位可复现）选 (C)：逐字节可复现只在同 ISA 上成立，归 D3.x；正式成绩只在 dmax 上运行，排行榜不受影响。
- 建议项 W-08、W-11 至 W-13、W-15 至 W-17（文案）随 D3.x 或发布任务。

## 5. 产物与复现

| 内容 | 位置 |
|---|---|
| 回归门（本机，不入库） | `runs/perf/d3-gate/124c22c6bb23/{heavy,full}/`（`report.json`、`meta.json`、各步骤日志；重载门含 `perf/run-0..7.json`、`zero-0/1`、`resim-0/1`） |
| 重载门（dmax） | `/data/anet-drone4d-d3/d3dev/gate/out/124c22c6bb23/heavy`；源码快照 `dmax:/data/anet-drone4d-d3/d3dev/gate/src-main` |
| emax 上线与回滚证据 | `LIVE-EVIDENCE.md`、`verify-2026.10.25-1.json`、`verify-withdraw-20261025.json`、`e2e-lab/{log.txt,metrics.json}`、`emax-ws/`、`wan1/wan_ws.json`、`lab-build.log`（位于会话临时目录 `/tmp/claude-1001/-data-projs-anet-drone/6752f3ab-4a1a-4ad5-891b-7b68d98ce213/scratchpad/d3close/final/`） |
| 镜像扫描归档（2026-10-18） | `dmax:/data/anet-drone4d-d3/d3dev/out/scan_images/20261018T233145Z-45184b7d7ec4-dirty/` |
| 各条款的批次记录 | `docs/impl/` 下的 D3-独占卡性能验收、D3-SLAM-实测记录、D3-相机实测记录、D3-批量基准记录、D3-排行榜v1-正式评测记录、D3-排行榜v1-重评记录、D3-评测运行保留接入记录、D3-正式运行稳健性与安全、D3-文案评审记录、D3-G3DGS-评审记录；`docs/05` §24 至 §33 |

复现命令（本机仓库根目录，`GIT_DIR=/data/projs/.anet-drone4d.git GIT_WORK_TREE=/data/projs/anet-drone`）：

```
make gate-d3-check GATE_REF=88bedb2 GATE_TIER=full      # 完整门 13 步：重载 5 步在 dmax，轻量 8 步在本机；约 65 min
AWR_BUILD_SHA=88bedb2ce9 AWR_BUILD_DIRTY=0 AWR_RELEASE=20261025-082924-88bedb2ce9 make lab-build   # /lab/ 构建
```

emax 上线与实测使用 `labctl.py`（`index`、`check`、`push`、`activate`、`enable-site`、`withdraw`）、`tools/deploy/emax/d3-public/verify.py` 与 `drill.py`，参数见 `LIVE-EVIDENCE.md`。

资源使用与清理：重载门仅使用 dmax 的 `d3-*` 容器（CPU 池，不挂 GPU）；上线实测期间 emax 的 `/tmp` 内 test 事件 id 清单已 `shred`，探针源码已删除；dmax 的 `d3-test-gate-main`、`d3-pub-pin` 容器已清理。起草本报告时对 dmax 仅做只读访问（读取镜像扫描归档）；起草中曾在本机尝试运行一组 `tests/d3` 用例，因 580 s 超时被终止，未得出结果，未用作证据。本报告未启动任何 GPU 作业、未做任何 git 写操作。

## 6. 封板范围 41 条验收汇总

`docs/05` §11.1 至 §11.3 与 ADR-194 决策 (1) 的封板范围为 P-1 的 2 条、P0a 的 19 条、P0b 的 20 条，共 41 条。P-1 与 P0a 的判定取自 RP0a，并按 `docs/05` §33 的后续关闭记录更新；"开放"列只列影响判定的未关闭事项。

| 编号 | 阶段 | 条款 | 判定 | 开放 |
|---|---|---|---|---|
| D3-AC-46 | P-1 | D2 发布面文案复核 | 发布就绪 | 上线后复核已做（05:50 UTC：zh 落地页与关于页 0 违规，en 落地页仅 C-49 徽标）；`v0.2.2`（`e0639ea`）已存在、`demo-parity` 66/66（F-118 关闭）；GitHub About、topics、release、W-14 翻转与 `v0.3.0` 标签待封板日 |
| D3-AC-47 | P-1 | P-1 实测报告 | 按 ADR 通过 | 数字由 K08（渲染进程组）与 K13（CUDA 事件计时，合计 0.425 GPU·s/仿真 s）更新 |
| D3-AC-01 | P0a | D2 零影响回归门 | 通过（依赖 ADR-220） | 被测提交上完整门全绿（§1）；C-53 待确认；F-118 关闭；F-123 (d) 由本次提交上的门满足 |
| D3-AC-02 | P0a | 世界包构建与标定 | 按 ADR 通过 | 无 |
| D3-AC-03 | P0a | NavMesh 与锚点 | 按 ADR 通过 | 无 |
| D3-AC-04 | P0a | 不完整即未知 | 通过 | 无 |
| D3-AC-05 | P0a | MeshWorldQuery | 通过 | F-120（服务 RSS 907 MiB，非本条款） |
| D3-AC-06 | P0a | 事件编译三道门 | 通过 | 无 |
| D3-AC-07 | P0a | 事件磁带逐字节确定性 | 按 ADR 通过 | 无 |
| D3-AC-08 | P0a | Exogenous 隔离 | 通过 | 无 |
| D3-AC-09 | P0a | 多机多视角观测 | 通过（RP0a 不通过，K13 恢复） | `test_multiview` 17 passed、2 xfailed、0 failed（K13） |
| D3-AC-10 | P0a | 跨传感器时间一致 | 按 ADR 通过 | 无 |
| D3-AC-17 | P0a | lockstep、确定性与决策时延 | 通过 | ADR-208 接线后须重做正式配置对拍（F-112 (c)），见 D3-AC-35 |
| D3-AC-18 | P0a | 评测与四项指标分量 | 按 ADR 通过（ADR-218） | K15 在 `r6_ec.py` 落地排序判据并重判通过，F-109 (d) 关闭；`b10-formal-r2` 的 `evaluation.json` 为评测器 0.1.0（F-86） |
| D3-AC-19 | P0a | 录制与回放 | 通过 | 无 |
| D3-AC-34 | P0a | Realistic 档锁步 | 按 ADR 通过 | 无 |
| D3-AC-51 | P0a | 内部工作区部署与角色 | 通过 | 互联网侧端口扫描由用户补做（F-119） |
| D3-AC-56 | P0a | 最小基准闭环端到端 | 通过 | 无 |
| D3-AC-57 | P0a | 赛道指标、置信区间与排行榜规则 | 部分通过（RP0a 通过，K16 后 (d) 部分达标） | 4 次平台原因运行待重跑（F-124 (a)）；其余分句通过 |
| D3-AC-58 | P0a | Python SDK 与基线策略 | 按 ADR 通过 | 无 |
| D3-AC-60 | P0a | 卷帘快门与 1080p 多机输出 | 通过（RP0a 不通过，K13 恢复） | `test_1080p` 6 passed、`test_rolling_shutter` 12 passed，903/903 帧字节一致 |
| D3-AC-11 | P0b | 相机档位吞吐 | 按 ADR 通过 | C-54 |
| D3-AC-12 | P0b | LiDAR 并发 | 按 ADR 通过 | 无 |
| D3-AC-13 | P0b | LiDAR/IMU 输出合同 | 部分通过 | F-52、C4 冻结 ADR |
| D3-AC-14 | P0b | FAST-LIVO2 RPE 区间 | 按 ADR 通过 | 无 |
| D3-AC-15 | P0b | 多机 LiDAR 采集与重建对比 | 部分通过 | 视图 Playwright 与 Tier S/B 显示 |
| D3-AC-16 | P0b | 观测隔离无泄漏 | 通过 | F-37 |
| D3-AC-20 | P0b | Rerun 派生与回放 | 通过 | 逐条款用例计数未找到证据 |
| D3-AC-21 | P0b | 公开站只读样例视图 | **不通过** | WS 条款；D2-AC-04 线上容量复测；封板阻塞 |
| D3-AC-22 | P0b | GPU、存储、评测窗口与背压 | 部分通过 | F-107、F-125 (a) |
| D3-AC-23 | P0b | 企业级文案 | 通过 | 无 |
| D3-AC-24 | P0b | 许可与署名 | 部分通过 | 现镜像扫描 |
| D3-AC-25 | P0b | Rerun 数据检视与时间轴同步 | 按 ADR 通过 | dmax 无头 Chromium 开发参考数字 |
| D3-AC-33 | P0b | OSM 车道图与车辆运动 | 通过 | 无 |
| D3-AC-35 | P0b | 两档一致性与 gz 健壮性 | 部分通过 | ADR-208 接线、GPU 相机逐字节 |
| D3-AC-43 | P0b | 内部排行榜 v1 与内部提交 | 部分通过 | F-124 (a) |
| D3-AC-59 | P0b | 受邀成员提交与容器加固 | 部分通过 | ZeroTier、`registry_gate` 演练 |
| D3-AC-61 | P0b | 热成像 | 通过 | 无 |
| D3-AC-62 | P0b | 第一批四类事件 | 通过 | 无 |
| D3-AC-63 | P0b | 数据集导出（应交付） | 部分通过 | 正式核验未执行 |
| D3-AC-64 | P0b | 高斯外观层评估与决策备忘（应交付） | 通过 | 无 |

汇总计数（41 条）：通过 18；按 ADR 通过 12；部分通过 9（P0a 1 条：D3-AC-57；P0b 8 条）；不通过 1（D3-AC-21）；发布就绪 1（D3-AC-46）。其中 P-1 为发布就绪 1、按 ADR 通过 1；P0a 19 条为通过 11、按 ADR 通过 7、部分通过 1；P0b 20 条为通过 7、按 ADR 通过 4、部分通过 8、不通过 1。

仍开放的条款（共 11 条）：D3-AC-21（不通过，封板阻塞）；部分通过 9 条：D3-AC-57、13、15、22、24、35、43、59、63；发布就绪 1 条：D3-AC-46（上线后复核已做，对外发布动作待封板日）。此外 D3-AC-01、D3-AC-11 判定为通过但分别依赖 ADR-220（C-53）与 C-54 的用户确认，D3-AC-17 附 ADR-208 接线后的对拍重做。

## 7. 封板判定修订（2026-10-25）

### 7.1 用户决定

| 编号 | 决定 | 落实 |
|---|---|---|
| R-D3-75 | D3-AC-21 采用外部分发（ModelScope），提供 ModelScope 令牌 | ADR-221：视频、切片、底图与 Rerun Viewer Wasm 只经 ModelScope 公开数据集 `inksong/ANet-Drone4D-samples` 分发，emax 只托管索引、磁带、轨迹与许可文件 |
| R-D3-76 | "当前 emax 的版本能用了吗？能用的话这些测试都不做了" | 经核实公开站可用后，ADR-222：§3 中除 D3-AC-21 外的证据缺口不在封板前补测，登记为 D3.x 顺延，相关条款按 ADR 通过 |
| R-D3-77 | D3-AC-22 证据帧条款按 K17 现状；ZeroTier（C-36）与 Tier B 显示（ADR-219）登记顺延、按 ADR 通过 | ADR-222 决策 (2)(3) |

### 7.2 ADR-221 的实现与重新上线

- 实现提交 `7a70fea`（d3-dev），合入 `main` 为 `8df227b`（树 `bae665501c39`）：契约 `bench/lab_static_index.v1` 增加 `viewer_wasm` 与 `runs[].basemap_external`（layout_id 不变）；`labctl.py` 允许任意大小的外链制品；页面以外链优先播放与打开（CORS 模式，`/lab/` 为 COEP require-corp），Viewer 的 Wasm 请求改写到 ModelScope 副本；`verify.py` 的 rate 检查在没有足够大的托管制品时记为跳过并注明 ADR-221。单元测试：apps/web `tests/lab` 224 passed、1 skipped；`tests/d3/deploy` 157 passed、10 skipped；契约 `--check` 一致。
- ModelScope：版本目录 `2026.10.25-1` 的 15 个文件上传至 `samples/2026.10.25-1/`（提交 `dffbb13`），Viewer Wasm 0.38.1（51,225,405 B，sha256 `29561df0…`）与数据集说明、`SHA256SUMS.2026.10.25-1` 上传（提交 `9d74a7f`）。直链 `www.modelscope.cn/.../resolve/master/...` 302 至 `cdn-lfs-cn-1.modelscope.cn`，Range 206、`Access-Control-Allow-Origin: *`；自 dmax（宁波）4.24 MB/s、自美国开发机 1.71 MB/s，下载文件 sha256 与源文件一致。
- 版本目录 `2026.10.25-2`：5 个文件 831,345 B（LICENSE、NOTICE、事件磁带两个文件、tracks_obs.bin），6 路视频、3 个切片与底图带 ModelScope 外链与 bytes、sha256；`/lab/` 构建 `20261025-102133-8df227b61c`（与 `8df227b` 同树的干净工作树，K9 与 lab 档扫描通过），emax 上不部署自托管 Wasm（28 个文件）。`labctl check` 通过；`push` 29.8 s；`activate --keep 1` 清除 `2026.10.25-1` 与旧构建，emax 上 D3 占用 2,321,775 B；`enable-site` 以上线前站点文件为 base，nf404 保留。
- `verify.py`（10:24 UTC）：bench404、lab、static、nmax（2）、root（D3 进程 0）、disk（2.3 MB，单文件 > 64 MB 0 个）、content（test 文件 0、真值文件 0）通过；rate 按 ADR-221 跳过；conn_ip 与 conn_all 因没有 ≥ 12 MiB 的托管制品无法占住连接而报不通过——站点文件与首次上线（08:35 UTC，conn_ip 第 5 条 429、conn_all 第 33 条 429 实测通过）相同，以首次上线的实测为证据，`verify.py` 的连接检查在无大文件时的处理登记为后续事项。

### 7.3 公网复测（D3-AC-21 WS 条款）

探针与分析同 §2 D3-AC-21（`drill.py` 的 `_ws_viewer` 与 `ws_latency`），探针 2 个 viewer 在美国开发机经公网，负载自 dmax（宁波）经公网，基线与负载交替各 45 s × 3 轮。

| 时间（UTC） | 负载 | 基线 p95 | 负载期间 p95 | 增幅 | 各轮 | 判定 |
|---|---|---|---|---|---|---|
| 10:29 | 16 路并发样例视频下载（按索引外链，经 ModelScope），循环整段下载 | 143.7 ms | 148.9 ms | +3.6% | +7.3%、+0.3%、+4.9% | 通过（≤ 10%） |
| 10:35 | 同上，另每 10 s 一位新访客自 emax 加载 `/lab/` 页面、JS、索引、磁带与轨迹（每位约 1.0 MB） | 155.6 ms | 246.7 ms | +58.6% | +126.8%、+38.3%、+26.2% | 参考（容量风险） |

D3-AC-21 的条款为"16 路并发下载期间"，按第一行判定**通过**。第二行不是条款的测量条件：它表明 emax 约 3 Mbps 的出口在每分钟 6 位新访客的页面加载下同样吃紧，D2 页面的访问也受同一出口约束；登记为容量风险（`docs/05` §34.5），处置为 D3.x 中把事件磁带与轨迹也改为外部分发并评估 emax 带宽。

### 7.4 最终判定

| 编号 | 原判定（§0） | 最终判定 | 依据 |
|---|---|---|---|
| D3-AC-21 | 不通过 | 通过 | §7.2、§7.3；D2-AC-04 容量用例的 emax 空闲窗口复测按 ADR-222 顺延（N_max 在全部实测前后均为 2） |
| D3-AC-13、15、22、24、35、43、59、63 | 部分通过 | 按 ADR 通过（ADR-222） | 证据补测登记 D3.x（`docs/05` §34.5） |
| D3-AC-11、12、14、25 | 按 ADR 通过 | 按 ADR 通过 | 不变；D3-AC-11 的 C-54、D3-AC-25 的开发参考数字按 ADR-222 顺延 |
| D3-AC-16、20、23、33、61、62、64 | 通过 | 通过 | 不变 |

P0b 20 条最终：通过 8、按 ADR 通过 12。封板范围 41 条（§6 表按本节与 P0a 的 D3-AC-57 同样按 ADR-222 改判、D3-AC-46 随发布动作完成）：通过 20、按 ADR 通过 21。回归门在 `8df227b`（树 `bae665501c39`）上完整门 13 步通过（`make gate-d3-check GATE_TIER=full`，`docs/05` §34.4）。

