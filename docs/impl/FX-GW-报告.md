# FX-GW 验收与加固报告（网关、回放、工具注册）

| 项 | 内容 |
|---|---|
| 工作包 | FX-GW（区域：网关、回放、工具注册） |
| 日期 | 2026-09-29（第 1 轮）；2026-10-01（续作，见 §1.6） |
| 依据 | INT-1 §3、§7.8、§7.9、§7.10、§7.11；M07-to-M11 第 1 条；M12-to-M11 第 1–3 条；M16-to-M11 第 2、5、7 条；M16-to-M00 第 2 条；M00-B-to-M00 第 1 条；M11-api-to-M00 第 2 条；FX-WEB2-to-M11 第 1–3 条（续作）；03 §3.3、§4.3、ADR-033；11 §3.2、§7.3、§7.4；17 §6.11、§9.5、§10.6；18 §1.3、§13.1；19 §5 |
| 约束执行 | 未安装依赖；未使用 git；未运行性能基准与 perf 标记用例（harness 只做 `--check`、`--list` 与无浏览器自检）；功能测试见第 3 节；续作联网只用于 5 个仓库的 GitHub API 只读查询（BOM 补录，10 次请求） |
| 结论 | 任务 1–5 全部完成。第 1 轮完成任务 1、2、3、5 与任务 4 的 OpenAPI 快照、`check-thresholds.mjs`；续作补齐任务 4 的 BOM（`tools/ci/bom.json` 95 条，`check-deps.mjs` 按 AWR-11 §7.3 全字段校验），并落实第 1 轮之后提交给本区域的 FX-WEB2-to-M11 三条请求（回放迟到者补发 playbackState、辅助生产者早期事件主动补拉、API 标题）。规格变化以 ADR-055（端口偏移 0–31）与 11、17、18、19、M11、M12、M16 的修订落地。`make lint` 通过；pytest 本区域 643 例、vitest 全量 934 例、Playwright 功能用例 13 例（timeline、motion、skeleton、fakesource、m12 smoke）全部通过；负载下暴露的用例时序与夹具端口问题已处理，前端时序问题已提请求（§3.2） |

## 1 修复内容

### 1.1 任务 1：EnvCache 同版本心跳（M07-to-M11 第 1 条）

- 现状核对：INT-1 已在 `python/awr/api/rt/detail.py` 让同纪元同版本的心跳照常 `ch.publish`（不计入 applied、不前进版本），
  更旧版本仍丢弃。本包没有再改该逻辑，补齐了缺失的用例（M07 报告第 4 条指出的两个症状各有断言）。
- 新增 `tests/rt/test_env_heartbeat.py`：
  - 单元：同版本心跳使 channel seq 前进、载荷换成最新锚点、applied 不变；旧版本与迟到的旧版本心跳丢弃；变化帧前进版本；
    新纪元（sim-core 重启，版本从 1 重新开始）与 `reset()`（回放 open/seek/close）之后无条件生效。
  - 端到端（FakeSim + 真实 Gateway + WS）：最后一次变化帧之后 4.5 s 内收到 ≥ 3 帧 `env/state`，锚点 `t_ns` 单调前进，
    相邻两帧间隔 < 2.5 s（客户端 STALE 门槛 3 s）；迟到客户端的首帧锚点 ≥ 最新心跳而不是变化帧时刻；`GET /api/env/state`
    返回同一份最新锚点。
- 客户端 `EnvStore` 已按任何帧刷新 STALE 计时（`envStore.test.ts` C04、C05），无需修改。

### 1.2 任务 2：回放

| 问题 | 修复 | 文件 |
|---|---|---|
| 冷启动 213：`sys/start` 返回后立即 `ctl/replay-worker/open`，冷启动 1–2 s 内没有 queryable，两次查询约 0.6 s 即失败 | `sys/start` 之后 watch `proc/replay-worker/ready`（带历史，≤ 10 s，`READY_TIMEOUT_S`）再 open；`sys/start` 回 105（进程已在运行）视为可继续；超时 `playbackState{error, 213}` | `python/awr/api/rt/playback.py` |
| 回放中 replay-worker 崩溃无提示（M12-FR-051、AC-051） | 回放打开期间 watch `proc/replay-worker/alive`；撤销时向全部连接广播 `playbackState{status: error, code: 213}`，保持回放模式；此后 close 不再调用已不存在的 worker（此前两次查询超时约 4.8 s） | `playback.py`；`gateway.py`（stop 时释放 watch） |
| 回放模式 roster 竞态：open 时在途的实时 roster 回复覆盖回放名册 | ① `_request_roster(producer)` 只接受当前模式的生产者（回放中由实时事件触发的查询不发出）；② 回复携带生产者，与当前模式不符时丢弃并按当前模式重查；③ backfill 名册改走 `_apply_roster`，不再清掉在途查询标志（这是原竞态的真正入口） | `python/awr/api/rt/gateway.py` |
| `/api/runs/{run}/bookmarks` 在 inproc 422 | `run` 等于当前运行 id 而不是 `r<date>-<time>-<hex>` 形式（`inproc-xxxxxx`）时，运行目录取当前运行的持久化目录；其他不合格式的 id 仍 422 | `python/awr/api/rest/runs.py` |
| `tests/e2e/timeline.spec.ts` 回放段 fixme | 去掉 fixme，写成完整用例并通过（见下） | `tests/e2e/timeline.spec.ts` |

timeline 回放段（M12-AC-028、047、026、051；M11-AC-042）：总是自起后端（`apps/web/perf/m12/common.ts` 的
`startReplayBackend`：ci profile，`--only sim-core,api,replay-worker`，`python -m awr.recorder.synth` 合成 8 架 480 s 录制）：

1. 回放前经 R70 给录制的运行写一个书签（等价于实时录制期间添加）；
2. 深链 `/world/shenzhen/replay/<run>?seg=0&t=420` 打开后状态 paused，所见时刻（`__perf.time.tRenderS`）= 420 s（±1 块）；
3. 连续 25 次 Shift+. 前进 1.00 s（±半块），→ +1 s、← −1 s；
4. 回放前写的书签出现在书签列表，Shift+→ 后按 PageDown 逐个标记前进并停在该书签；M 在所见时刻新增共享书签（REST 列表变为 2 条）；
5. 经 `GET /api/sys/procs` 取 replay-worker 的 pid 并 kill -9：store 的 `playback` 变为 `error/213`、"回放已停止"提示出现，
   用时 ≤ 1.5 s（1 s + 轮询粒度，沿用 INT-1 口径；实测远小于 1 s）；随后 close 回到实时；全程无 pageerror。

对接中发现并修复的三个问题（都在回放区域内）：

- 合成录制绑定：`synth` 命令行把 `content_version` 固定写成 `synth`，当前运行有绑定时回放打开 122。命令行缺省改为取
  `worlds/<world>/world.json` 的 `contentVersion` 与坐标哈希（`--world`、`--content-version`、`--coordinate-sha256` 可覆盖）。
  `perf/m12/seek-latency.spec.ts` 与 `replay20x.spec.ts` 用同一入口，性能阶段会同样遇到这个 122（`python/awr/recorder/synth.py`）。
- 深链进入回放时直接 117：`ui/views/replayFlow.ts` 在首个 TIME 到达前读 timeline store 的 `state4`（此时为 0，被当作
  STOPPED 而跳过暂停）。改为先等首个 TIME（`timeView.atMs`，≤ 5 s），按 `timeView.state` 判定并等待 PAUSED（≤ 5 s）；
  同时把该文件的 `@/engine/time/index` 改为门面 `@/engine`（TS-BND-01）。该文件属 M15 UI（验收阶段另一工作包正在编写回放 UI），
  本包只改了 `enterReplay` 的暂停判定与一个 import。
- 回放后端收尾：`startReplayBackend.close()` 固定等 1.5 s 后删目录，supervisor 仍在写日志时 `ENOTEMPTY`；改为等 supervisor 退出
  （30 s 后 SIGKILL）再删，删目录带重试；另外 supervisor 的 stdout/stderr 管道此前无人读取，长时间回放可能写满 64 KiB 而阻塞，
  现在排空（`apps/web/perf/m12/common.ts`）。

另外，`tests/recorder/test_processes.py` 去掉了"已知网关竞态"的 213 旁路（真实进程链路现在必须 open 成功），并按 17 §6.11 的
串行约定等 seek 的 `did_seek` 终态再 close（原用例在 seek 进行中发 close 得到 105）；`tests/recorder/test_replay.py` 去掉了
"再取一次回放 roster"的规避。

### 1.3 任务 3：工具与注册

- **`netem_proxy.py` 控制口**（M16-to-M11 第 2 条；M16 §6.11、§7.4）：`--control 127.0.0.1:<port>`（只允许回环）提供极简
  HTTP/1.1 接口：`POST /cut?ms=`（0–60000，缺省 3000；立即关闭全部连接并在窗口内拒绝新连接）、`POST /profile?name=W0..W3`
  （对已建立连接的后续数据立即生效）、`POST /stall?ms=`、`GET /stats`、`GET /health`；未知路径 404、方法不符 405、参数非法 400。
  `--upstream` 为 `--target` 的别名；`--no-auto-cut` 关闭 W3 自带的 30 s 定时断连（缺省保留，`perf/net.spec.ts` 行为不变）；
  READY 行带控制口。harness：`backend.mjs` 的 `startProxy(h, profile, seed, {autoCut})` 返回 `{base, control}`，
  `cutProxy(proxy, ms)` 改走 `/cut`（无控制口时退回 SIGUSR1 停顿），新增 `setProxyProfile`；`runner.mjs` 以
  `AWR_PERF_PROXY_CONTROL` 把控制口传给规格。
- **perf 注册表**（INT-1 §7.10；PERF-E017）：
  - `perf/m14/cases.mjs`：两个 py 用例补 `build: 'none'`、`params`；`m14.bench.s3` 的后端 `supervisor` 改为登记值 `live`
    （`only: 'sim-core,api,agent-runtime'`，世界 newyork、剧本 s3-newyork-sar），`m14.bench.stress` 改为 `tool`；两者加
    `--out {runDir}/bench-result.json`。
  - 新增 `perf/m07/cases.mjs`（env-gpu、env-switch、env-visual、stage-bench、assets-bench）、`perf/m11/cases.mjs`（backpressure、
    fakesource、handshake；IPC 与多客户端基准已在核心注册表）、`perf/m12/cases.mjs`（smoke、interp、redraw、seek-latency、
    replay20x 与 bench_write/seek/replay 三个工具）、`perf/m13/cases.mjs`（`tests/sensors/bench_stage.py -m perf`；阶梯中的
    sensors stage 份额由核心 `fleet-ladder` 读取）。
  - `node perf/harness/run.mjs --check`（严格加载，不带 `--lenient`）：`registry OK: 106 cases`（此前 PERF-E017 三条错误）。
- **fake_gw 的命令口径**（M16-AC-021）：INT-1 已改为缺省 `--calls reject`（211 `SIM_UNAVAILABLE`，未知服务 110），本包复核
  `tests/e2e/test_fake_source.py::test_fake_gateway_rejects_commands_with_211` 与 `tests/runtime/test_fake_gw.py` 均通过，未再改代码。
- **端口偏移 0–31**（INT-1 §7.9）：`python/awr/runtime/config.py` 校验改为 `le=31`；`configs/runtime.yaml` 注释；ADR-055 与
  03 §3.3、19 §3.2 表、§5 规则 1、§6 的 yaml 与参数表、环境变量表同步；`tests/runtime/test_config.py` 增加 k = 31 的端口断言
  （8310、7757、5483），非法值改为 32；`tests/runtime/test_cli.py` 找空闲偏移的范围扩到 1–8、10–31。

### 1.4 任务 4：M00 工具

- **OpenAPI 快照**（17 §2 第 10 条、§10.6 第 9 条）：新增 `tools/contracts/gen_openapi.py`（`--check`），以固定设置导出
  `create_app(...).openapi()`、键排序写出 `packages/contracts/rest/openapi.snapshot.json`（68 个路径）。FastAPI 对 GET + HEAD
  共用处理函数的路由以 `list(route.methods)[0]`（集合）生成 operationId，随 PYTHONHASHSEED 变化；生成器把每个 operationId 的
  方法后缀改为该操作自己的方法，6 个不同 hash seed 下 `--check` 一致。`mk/contracts.mk` 把它并入 `make contracts` 与
  `contracts-check`；新增 `tests/contracts/test_openapi_snapshot.py`（快照一致；api 中每个 `ApiProblem(<code>` 与 `Reason.<NAME>`
  都在 reasons.json）；`tests/contracts/test_schemas.py` 的"数据文件全覆盖"把该生成快照列为例外。
- **`tools/ci/check-thresholds.mjs`**（18 §1.3 第 4 条）：THR-01 条目字段合法；THR-02 引用编号存在（18 PERF-AC 表、03 §8.4）；
  THR-03 每个 D1 的 P0 PERF-AC 至少有阈值条目、登记用例（含方法列所列规格的登记用例）或 G1 命令；THR-04 只由断言判定、无阈值
  条目的 P0 PERF-AC 逐条列出（`--strict` 时为错误）。现状 0 违规，THR-04 列出 27 项，交 M16 补阈值条目。已并入 `make lint`
  （`lint-thresholds`）并登记到 18 §13.1；`tests/contracts/test_lint_tools.py` 增加规则自检。
- **BOM** 未做，原因见第 5 节。

### 1.5 任务 5：负载敏感用例（INT-1 §7.11）

| 用例 | 处理 |
|---|---|
| `tests/rt/test_interest.py` 两个席位宽限用例 | 固定 `sleep` 改为按条件轮询（上限只防挂死）；信标数改为"≥ 3 且不超过 5 Hz × 实际时长 + 3"；宽限内重连前把宽限临时放宽到 30 s，避免负载下宽限先到期；未连接持有者用例按 `seat_grace → seat_expire` 事件轮询（上限 15 s） |
| `tests/environment/test_env_e2e.py` | 等待上限 10–15 s 改为 30 s（env/query 是 atomic 慢任务，负载下可顺延，STARVE_NS 兜底） |
| `tests/rt/test_limits.py::test_connection_limits` | 模块栈 hello 超时 2 s 改为 30 s（先开 32 个不发 hello 的连接，负载下最早的连接会先被 4408 关闭）；令牌先取好再开连接 |
| `apps/web/tests/m11/alloc.test.ts` | 测量前 GC 两次（原有）并连续测两个 1 万帧窗口取较小者，阈值 64 KiB 不变（逐帧泄漏在两个窗口都会超限，worker 同一 isolate 的偶发分配最多影响一个）；用例超时 120 s |
| `tests/e2e/motion.spec.ts` reduced 用例 | 打开命令面板前轮询到页面空闲；"运行中的动画"只计活动时长 > 1 ms 的动画（reduced 档 JS 配方用 0.01 ms 的 `--duration-epsilon`，SwiftShader 每秒几帧时可能仍读到 running），失败时列出动画名 |

### 1.6 续作（2026-10-01）

续作开始时先复核第 1 轮的改动仍在（端口偏移 `le=31`、ADR-055、playback/gateway/runs 修复、注册表 `registry OK: 106 cases`），
再处理第 1 轮之后新增的、属于本区域的请求与任务 4 的剩余部分。

#### 1.6.1 回放迟到者补发 playbackState（FX-WEB2-to-M11 第 1 条）

- 问题：服务器处于回放模式时，新连接（页面刷新、另一客户端加入）握手只收到 `serverInfo{mode: replay}`，playbackState 只在状态变化时
  广播，客户端不知道 run、段、dataStart/End 与 speed_max；前端以"席位持有者发幂等 `playback{cmd: speed}`"过渡，viewer 只能等下次广播。
- 修复：`PlaybackController.on_hello(s)`，`ws._on_hello` 在 hello 完成后调用：回放模式下向该连接单独发送一次当前 playbackState
  （`request_id` 为空字符串，字段与广播一致；replay-worker 已丢失时为 `error, 213`），实时模式不发。
- 用例：`tests/rt/test_playback_seams.py::test_late_joiner_receives_playback_state_after_hello`（实时模式 hello 后无 playbackState；
  回放中迟到 viewer 在 hello 之前不收、之后收到一条，run/段/dataStart/dataEnd/speed/speed_max 与 open 回复一致且通过 ops schema；
  replay-worker 丢失后迟到者收到 `error, 213`）。
- 前端 `stores/timeline.ts` 的 `syncReplayState` 过渡做法可以保留（幂等，且此后多数情况下在它之前就已收到状态），去留由 M12/M15 决定。

#### 1.6.2 辅助生产者早期事件主动补拉（FX-WEB2-to-M11 第 2 条）

- 问题：demo 下 recorder 随剧本开录，`rec.started` 早于 api 订阅发出；recorder 只发了这一条事件时，INT-1 的"首见补拉"不会触发
  （从未见过该生产者），EventRing 中没有 `rec.started`，时间轴缺"录制中"指示。
- 修复（不改 `_replay` 线上格式）：
  - `awr.runtime.events.EventSubscriber.probe(producer, epoch)`：对尚未见过的生产者以约定纪元向 `_replay` 取 since 0。回复中最大
    seq ≤ `backfill_first_max + 1`（Gateway 为 1025）时按序交付；更长的历史只把跟踪起点移到最大 seq（与首见规则一致，不回补）；
    无回复或 truncated 且无事件（纪元不符）时撤销跟踪、恢复"未见过"。探测在途期间到达的真实事件不按猜测纪元判为旧纪元而丢弃
    （`_Tracker.unconfirmed`），纪元不同则直接按首见规则接管。
  - Gateway 在 recorder、agent-runtime、job-worker 的 `proc/<name>/ready` 出现时（watch 带历史：api 启动时已在运行的立即回调）
    发起探测；纪元 = 重启次数 + 1，受监管时等首个 `sys/procs` 回复取得 `restarts`（3 s 内仍无回复按 1），不受监管时立即按 1。
  - recorder 的事件纪元此前恒为 1：重启后 seq 从 1 重新计数而纪元不变，api 与其他消费者会把重启后的事件当作重复丢弃，直到 seq
    超过旧值。`RecorderCore(event_epoch=…)`，`main()` 传 `AWR_RESTART_COUNT + 1`，与 agent-runtime、job-worker 一致。
- 用例：`tests/runtime/test_event_probe.py` 5 例（订阅前唯一事件经探测补拉、此后正常交付无补拉；纪元猜错撤销后按首见补拉；
  猜测纪元高于真实纪元时在途的真实事件不丢、迟到探测回复被忽略；1100 条历史不回补只移起点；无 `_replay` 服务时撤销）；
  `tests/rt/test_event_probe_gw.py` 3 例（api 重启晚于 recorder 的 `rec.started` 进入 `GET /api/events` 与 WS `hello.resume`；纪元猜错
  无害；受监管时按 `sys/procs` 的 restarts + 1 探测命中）。`tests/rt/fakesim.py` 的 FakeSupervisor 增加 `proc_restarts`。

#### 1.6.3 API 标题（FX-WEB2-to-M11 第 3 条）

`create_app` 的 FastAPI 标题改为 "ANet Drone4D World Runtime API"（ADR-056），`tools/contracts/gen_openapi.py` 重新生成
`packages/contracts/rest/openapi.snapshot.json`（68 个路径；同时吸收了第 1 轮之后其他工作包对 REST 的改动），`--check` 与
`tests/contracts/test_openapi_snapshot.py` 通过。

#### 1.6.4 任务 4：BOM（`tools/ci/bom.json`）

第 1 轮认为缺 `upstream` 与逐包映射而未做。续作核对后，所需数据都有可追溯来源，不需要编造：

| 字段 | 来源 |
|---|---|
| `id`、`d1`、`new2026`、`deviation`、`stars`、`lastCommit` | AWR-11 §3.2（2026-09-28 采集）；附录 A 与 `.cache/research/t11/*`（`gh_meta.tsv`、`core_meta.txt`、`refs_lastcommit.tsv`、`review_meta.tsv`） |
| `upstream` | §3.2 star 值对应的仓库；以安装包元数据（`node_modules/*/package.json` 的 `repository`、PyPI 的 Project-URL）逐条核对 |
| `version` | `apps/web/package.json`、`pyproject.toml`（除 geo-worker、px4 外的组）、`requirements.in`，与锁文件一致 |
| `adapterBoundary` | §3.4、附录 A 的落点与 check-deps 边界规则，并以当前源码实际 import 位置核对 |
| `fallback` | §4 各层"替换预案"的对应句 |
| `status` | §9.1 有升级触发器（U-01、03、04、08、09、10、11、13）的记 WATCH，其余 LOCKED |

- 内容：95 条 = npm 46（apps/web 全部依赖）+ PyPI 31（全部直接依赖）+ vendor 1（transitions.dev，commit `e2d5551…`）+ self 17
  （T15、T18–T21、T25、T30、T32、T38、T42、T56、T57、T59、T61、T62、T64、T68）。
- §3.2 缺行的 5 个包（pytest-asyncio、httpx、pip、setuptools、py7zr）于 2026-10-01 经 GitHub API 补采（原始输出
  `.cache/research/t11/bom_supplement_meta.tsv`），在 AWR-11 §3.2 的 T71、T73 行补录并新增 T82 行，BOM 条目带 `collectedAt`。
- `tools/lint/check-deps.mjs`：读取 §7.3 的 `{schemaVersion, generatedFrom, collectedAt, items}`（旧 `{packages}` 仍可读）；
  `validateBom` 校验全部字段（缺字段、枚举越界、非精确版本、runtime 缺边界或预案、`lastCommit` 晚于采集日、未排序、重复，均为
  `deps/version`，与 §7.3 一致）；PyPI 直接依赖（`pythonDirectDeps`）必须登记（`deps/unlisted`）且进入 `requirements.lock`；
  `--bom-only` 走同一校验。BOM 生效后 npm 清单中的任何新依赖都要先登记，否则 `make lint` 失败（依赖冻结阶段适用）。
- 用例：`tests/contracts/test_lint_tools.py::test_check_deps_bom_rules`（仓库 BOM 通过 `--bom-only` 与完整检查；合成文档上 9 类违规
  各报一次；无移植来源的 self 条目 `stars`/`lastCommit` 为 null 合法；PyPI 直接依赖解析只取锁定的组）。

#### 1.6.5 加固

- **PY-CB-01**：续作初版为消除测试收尾时回调线程的 `RuntimeError: Event loop is closed`，把 Gateway 的 subscribe、watch 回调统一
  改为经 `_post`，并在 playback 的 watch 回调里加了 `contextlib.suppress`；本区域全量回归中 `test_repository_passes_py_callbacks`
  报 6 处违规（总线回调只能入队）。已改回：subscribe、watch 回调
  一律为 `loop.call_soon_threadsafe(...)` 单调用（stop() 先关闭这些句柄）；playback 的两个 watch 回调以 `if … and not loop.is_closed()`
  守卫后入队；只有 `call_cb` 的回复回调（不在 PY-CB-01 范围，查询在途时无法随 stop() 撤销）经 `Gateway._post`，事件循环已关闭时丢弃，
  消除了测试收尾时回调线程的 `RuntimeError: Event loop is closed`。`check_py_callbacks.py` 0 违规。
- **负载敏感用例**（INT-1 §7.11 同类）：`tests/runtime/test_events.py::test_570_events_per_s_no_gap_no_reorder` 在 load ≈ 19 时
  因墙钟 3 s 窗口内只发出 1443 条（断言 ≥ 1500）失败；改为按迭代计数（250 Hz × 3 s 共 1710 条，断言 ≥ 1700），缺口与乱序判定不变。

## 2 改动文件

### 2.1 本区域（网关、回放、工具注册）

| 文件 | 所有者 | 改动 |
|---|---|---|
| `python/awr/api/rt/playback.py` | M11 | 就绪等待、105 可继续、replay-worker 存活监视与 213 广播、丢失后 close 直返 |
| `python/awr/api/rt/gateway.py` | M11 | 名册按当前模式的生产者查询与丢弃过期回复、`_apply_roster`、stop 释放 playback watch |
| `python/awr/api/rest/runs.py` | M12（M11 路由面） | 当前 inproc 运行的书签目录 |
| `tools/bench/ipc/netem_proxy.py` | M11 | 控制口、剖面切换、`--no-auto-cut`、`--upstream` |
| `python/awr/runtime/config.py`、`configs/runtime.yaml` | M11-R、M00 | 端口偏移 0–31 |
| `python/awr/recorder/synth.py` | M12 | 命令行缺省绑定已安装世界 |
| `apps/web/perf/harness/backend.mjs`、`runner.mjs` | M16 | 代理控制口封装、`AWR_PERF_PROXY_CONTROL` |
| `apps/web/perf/m07/cases.mjs`、`m11/cases.mjs`、`m12/cases.mjs`、`m13/cases.mjs`（新建） | M07、M11、M12、M13 | 注册表 |
| `apps/web/perf/m14/cases.mjs` | M14 | PERF-E017 修正 |
| `apps/web/perf/m12/common.ts` | M12 | 回放后端收尾与管道排空 |
| `apps/web/perf/skeleton.spec.ts` | M16 | 测试构建检测与服务同一 dist（`AWR_PERF_DIST`/`M11_DIST`） |
| `tools/contracts/gen_openapi.py`（新建）、`packages/contracts/rest/openapi.snapshot.json`（生成）、`mk/contracts.mk` | M00 | OpenAPI 快照 |
| `tools/ci/check-thresholds.mjs`（新建）、`mk/lint.mk` | M00 | 阈值表覆盖检查 |
| `python/awr/api/rt/playback.py`（续作） | M11 | `on_hello`：回放模式迟到者补发 playbackState；watch 回调以 `loop.is_closed()` 守卫入队 |
| `python/awr/api/rt/ws.py`（续作） | M11 | hello 完成后调用 `playback.on_hello` |
| `python/awr/api/rt/gateway.py`（续作） | M11 | `PROBE_PRODUCERS` 就绪监视与 `_issue_probes`（纪元 = restarts + 1）；`call_cb` 回复经 `_post`（循环关闭时丢弃） |
| `python/awr/api/rt/events.py`（续作） | M11 | `EventIngest.probe` |
| `python/awr/runtime/events.py`（续作） | M11-R | `EventSubscriber.probe`、`on_probe_reply`、`_Tracker.probing/unconfirmed` |
| `python/awr/recorder/app.py`（续作） | M12 | 事件纪元 = 重启次数 + 1（`event_epoch`） |
| `python/awr/api/main.py`、`packages/contracts/rest/openapi.snapshot.json`（续作） | M11、M00 | API 标题 ANet Drone4D；快照重新生成 |
| `tools/ci/bom.json`（新建，续作）、`tools/lint/check-deps.mjs`（续作） | M00 | BOM 95 条；§7.3 全字段校验、PyPI 直接依赖登记 |
| `.cache/research/t11/bom_supplement_meta.tsv`（新建，续作） | M00 | 5 个补录仓库的 GitHub API 原始输出（AWR-11 附录 B 证据） |
| `apps/web/perf/skeleton.server.ts`、`skeleton.spec.ts`（续作） | M16（SK-E2E） | `startSkeletonServer(0)` 取空闲端口；D1-AC-35 的静态服务与 fake_gw 不再用固定的 4193/8097 + 10k |
| `.cache/impl/requests/FX-GW-to-M12-M15.md`（新建，续作） | — | 前端回放深链与步进时序问题的请求 |

### 2.2 区域外（为让本区域功能落地而做的最小改动）

| 文件 | 所有者 | 改动与原因 |
|---|---|---|
| `apps/web/src/ui/views/replayFlow.ts` | M15 | 深链进入回放前等首个 TIME 再判定暂停（否则 117）；引擎 import 改走门面（TS-BND-01） |

### 2.3 测试

新建 `tests/rt/test_env_heartbeat.py`、`tests/rt/test_playback_seams.py`、`tests/contracts/test_openapi_snapshot.py`；修改
`tests/rt/fakesim.py`（FakeSim roster 回复延迟；FakeReplayWorker 名册前缀与 ready token；FakeSupervisor 的 `sys/start`、`sys/stop`、
冷启动与 close 时取消未到期拉起）、`tests/rt/test_interest.py`、`tests/rt/test_limits.py`、`tests/rt/test_netem_proxy.py`、
`tests/recorder/test_replay.py`、`tests/recorder/test_processes.py`、`tests/recorder/test_runs_rest.py`、
`tests/environment/test_env_e2e.py`、`tests/runtime/test_config.py`、`tests/runtime/test_cli.py`、`tests/contracts/test_schemas.py`、
`tests/contracts/test_lint_tools.py`、`tests/e2e/timeline.spec.ts`、`tests/e2e/motion.spec.ts`、`apps/web/tests/m11/alloc.test.ts`。

新增用例要点：`test_playback_seams.py` 四例（冷启动 1.5 s 后 open 成功且确实等待、空闲期再 open 走 105；就绪超时 213；在途实时名册
回复被丢弃、回放名册保持、close 后恢复实时名册；replay-worker 丢失 1 s 内两个连接都收到 213、close < 2 s）；`test_netem_proxy.py`
两例（控制口全部路由与错误码、WS 经 `/cut` 断开后恢复、`/profile` 立即生效；命令行 READY 行、`--upstream`、非回环控制口被拒）；
`test_runs_rest.py` 一例（inproc 当前运行书签增删、其他 inproc id 仍 422）。

续作：新建 `tests/runtime/test_event_probe.py`（5 例）、`tests/rt/test_event_probe_gw.py`（3 例）；`tests/rt/test_playback_seams.py`
增加迟到者一例；`tests/contracts/test_lint_tools.py` 增加 `test_check_deps_bom_rules`；`tests/rt/fakesim.py` 的 FakeSupervisor 增加
`proc_restarts`；`tests/runtime/test_events.py` 的 570 条/s 用例改为按迭代计数；`tests/e2e/timeline.spec.ts` 每次按键前等显示
时刻与时钟一致且无在途控制（回放段 `settle`，实时段等 store 为 PAUSED），viewer 用例 reveal 上限放宽（§3.2）。

### 2.4 文档

| 文档 | 修订 |
|---|---|
| 03 | §3.3 第 6 条端口偏移 0–31；§7.0 索引；附录 E 新增 ADR-055 |
| 17 | §2 第 10 条 OpenAPI 快照生成与比对；§4.3.9 inproc 当前运行 id 的例外；§6.11 补充约定（串行与 105、按需进程就绪等待、名册来源、进程丢失 213） |
| 18 | §1.3 第 4 条过渡口径；§13.1 登记 THR-01 至 04 |
| 19 | §3.2 表、§5 规则 1、§6 yaml 注释与 `net.port_offset` 行、`AWR_PORT_OFFSET` 行改为 0–31 |
| M11 PRD | 工具目录中的控制口；M11-AC-036 的测量方法（两次 GC、两个窗口取较小者，阈值不变） |
| M12 PRD | `synth.py` 命令行缺省绑定已安装世界 |
| M16 PRD | §6.11 控制接口已交付的形态与 harness 封装 |
| 11（续作） | §3.2 T71、T73 补录 4 个包并新增 T82（py7zr），说明补采日；§7.3 表增加 `items[].collectedAt`、stars/lastCommit 的 null 例外，新增"口径补充"6 条（多包技术点、补采、无来源自研、scope 划分、status 规则、登记范围）；附录 B 登记补采原始输出 |
| 17（续作） | §6.11 补充约定第 5 条（迟到者 playbackState）；§9.5 事件纪元约定与"首见补拉与主动探测" |
| 18（续作） | §13.1 deps 规则行：BOM 已交付、全字段校验与登记范围 |
| M11 PRD（续作） | M11-FR-065（订阅前事件：首见补拉与主动探测）、M11-FR-078（迟到者 playbackState） |
| M12 PRD（续作） | §7.6：recorder 事件纪元 = 重启次数 + 1；`rec.started` 由网关探测补拉 |

## 3 测试结果

### 3.1 第 1 轮（2026-09-29）

| 命令 | 结果 |
|---|---|
| `pytest -m "not perf" tests/rt tests/recorder tests/runtime tests/contracts tests/environment tests/e2e/test_fake_source.py` | 633 通过、9 取消选择（8 min 44 s）。其后修改的 `fakesim.py`（取消未到期拉起）、`test_openapi_snapshot.py`、`test_lint_tools.py`、`gen_openapi.py` 的受影响用例单独复跑通过（`test_playback_seams.py` + `test_playback.py` 5 例；`tests/contracts/{test_openapi_snapshot,test_lint_tools,test_schemas}.py` 114 例） |
| `npx vitest run`（unit + browser，全量） | 925 通过、1 失败、1 跳过（119 个文件）。唯一失败 `tests/m15/hotkeys.test.ts`（`Home` 被登记两次）来自另一工作包 12:27 起对 `ui/actions/builtin.ts` 的回放快捷键改动（`timeline.start` 占用 Home），不在本区域；`tests/m11/alloc.test.ts` 在全量并行中通过 |
| Playwright `tests/e2e/timeline.spec.ts`（私有测试构建，`AWR_WEB_DIST`） | 3/3 通过（实时 2 例 + 回放 1 例） |
| Playwright `tests/e2e/motion.spec.ts` | 2/2 通过 |
| Playwright `perf/skeleton.spec.ts`（D1-AC-34、35，功能用例） | 4/4 通过（完整链路含 goto、六城、FakeSource、fake_gw） |
| Playwright `perf/m11/fakesource.spec.ts`、`perf/m12/smoke.spec.ts`（功能冒烟） | 4/4 通过 |
| `node perf/harness/run.mjs --check`（严格） | `registry OK: 106 cases` |
| `node perf/harness/selftest.mjs` | OK |
| `make lint` | tools/lint 与 tools/ci 全部规则（含新 `lint-thresholds`、`lint-m16-registry`）通过；ruff 与 oxlint 在本包文件上 0 违规。整体仍失败：ruff 7 条在 `python/awr/sim/{fleet,mission,safety}/*`、`tests/sim/test_kernel_parity.py`，oxlint 9 条在 `src/stores/jobs.ts`、`src/ui/views/{JobsPage,Report}.tsx`，均为其他工作包 12:26–13:17 正在编辑的文件 |

测试构建：`VITE_AWR_TEST_SWITCHES=1 npx vite build --outDir <会话临时目录>/dist-test`（不写共享的 `apps/web/dist`），
Playwright 以 `AWR_PORT_OFFSET=27` 运行（使用了本包扩展后的偏移档）。

### 3.2 续作（2026-10-01，其他工作包同时运行全量 pytest 与 Playwright，load 11–28）

| 命令 | 结果 |
|---|---|
| `pytest -m "not perf" tests/rt tests/recorder tests/runtime tests/contracts tests/environment tests/e2e/test_fake_source.py` | 首轮 641 通过、1 失败（`test_repository_passes_py_callbacks`：续作初版的 PY-CB-01 违规，见 §1.6.5）；修正后受影响用例单独复跑 18 通过；全量复跑 **643 通过**、9 取消选择（9 min 40 s） |
| `npx vitest run`（unit + browser，全量） | **934 通过**、1 跳过（120 个文件；第 1 轮的 hotkeys 失败已由该工作包修复） |
| Playwright `tests/e2e/timeline.spec.ts` + `motion.spec.ts`（私有测试构建） | **5/5 通过**（load ≈ 22）。中间两次失败及处置见下 |
| Playwright `perf/skeleton.spec.ts`、`perf/m11/fakesource.spec.ts`、`perf/m12/smoke.spec.ts`（功能用例，D1-AC-34、35） | **8/8 通过** |
| `node perf/harness/run.mjs --check`（严格）、`node perf/harness/selftest.mjs` | `registry OK: 106 cases`；`selftest: OK` |
| `node tools/lint/check-deps.mjs --bom-only` 与完整检查 | ok（BOM 95 条） |
| `make lint` | **通过**（ruff、oxlint、tools/lint 全部规则、check-deps、PY-CB-01、PERF-01、THR、m06-lint、生成物一致性、lint selftest、注册表） |

续作中 Playwright 的问题与处置：

1. **共享临时目录被覆盖**：同一会话的其他工作包也用 `<scratchpad>/dist-test` 构建，运行中被重建（`index.html` ENOENT、m12 smoke
   失败）。改用 `<scratchpad>/fxgw-dist` 后消失。建议各工作包的私有构建目录带工作包前缀。
2. **固定端口冲突**：`perf/skeleton.spec.ts` 的静态服务 4193 + 10k（k = 27 时 4463）与偏移 29 的 vite preview 同端口
   （`EADDRINUSE`）。`startSkeletonServer(0)` 支持取空闲端口，规格的静态服务与 fake_gw 改用空闲端口（ADR-055 已记录的同类问题）。
3. **时间轴用例的按键时序**（load 15–28）：回放段 → 未前进（按键以 4 Hz 更新的显示时刻为基准，seek 落地后 250 ms 内按键从上一位置
   起步，实测发出 421.04 而不是 422）；实时段 Shift+. 未前进（单步守卫读 4 Hz 的 store 状态，仍为 STEPPING 时拒绝）；viewer 用例两页
   SwiftShader 同时渲染，第二页 60 s 内未 reveal。用例改为每次按键前等显示时刻与时钟一致且无在途控制（实时段等 store 为 PAUSED），
   viewer 用例 reveal 上限放宽到 120/150 s；前端问题另提请求 `.cache/impl/requests/FX-GW-to-M12-M15.md`（含 load 28 时深链因 15 s
   离线上限被丢弃的问题）。

## 4 规格变化（依据与实测）

1. **ADR-055**：`AWR_PORT_OFFSET` 0–9 扩展为 0–31（依据 INT-1 §7.9：14 个并行 agent；实测 k = 31 时 8310、7757、5483，各端口段
   不相交）。
2. **17 §6.11 补充约定**：playback 命令串行、忙时回 105（原实现行为，此前未写入协议）；open 等待 `proc/replay-worker/ready` ≤ 10 s
   （落实 M11-FR-079 原文）；回放名册只来自 replay-worker；replay-worker 丢失广播 213（落实 M12-FR-051）。实测：冷启动 1.5 s 的
   替身下 open 成功；kill -9 真实 replay-worker 后 UI 提示 < 1 s。
3. **17 §4.3.9**：当前运行 id 不合 `r…` 格式时书签落在本运行持久化目录（此前 422）。
4. **18 §13.1、§1.3**：THR-01 至 04 规则与过渡口径（THR-04 只报告，补齐阈值条目后改 `--strict`）。
5. **M11-AC-036**：测量方法补充（阈值 64 KiB 不变）；INT-1 实测全量并行时超 72 B，本包全量运行通过。
6. **M16 §6.11**：控制口形态；W3 定时断连缺省保留，`--no-auto-cut` 由控制口对齐飞行时刻。
7. **M12 synth**：命令行缺省绑定已安装世界（否则当前运行有绑定时 122）。
8. **17 §6.11 补充约定第 5 条**（续作）：回放模式下新连接 hello 之后单独收到当前 playbackState（`request_id` 为空）。实测：迟到 viewer
   在 hello 后收到一条，字段与 open 回复一致；replay-worker 丢失后为 `error, 213`。
9. **17 §9.5 事件纪元与主动探测**（续作）：辅助生产者事件纪元 = 重启次数 + 1（recorder 由恒为 1 改正）；Gateway 在
   `proc/{recorder,agent-runtime,job-worker}/ready` 出现时探测 `_replay{since: 0}`，回补上限与首见补拉相同（≤ 1025 条），不改线上格式。
   实测：api 重启晚于 recorder 时 `rec.started` 进入 `GET /api/events` 与 `hello.resume`；纪元猜错与无服务时无副作用。
10. **AWR-11 §7.3 口径补充**（续作）：多包技术点共用 §3.2 的 star 与最后提交（`upstream` 取其仓库）；条目级 `collectedAt`；无移植
    来源的 self 条目 `stars`/`lastCommit` 为 null；scope 划分；status 按 §9.1 触发器；PyPI 直接依赖必须登记。依据：首次编写 BOM 时
    §7.3 的"所有字段无缺省值"与 §3.2 的数据形态不一一对应，需要可机械检查的规则；§3.2 补录 T71、T73 的 4 个包并新增 T82。

## 5 遗留问题与建议

1. **BOM 已交付（续作）**，维护要点：BOM 生效后 apps/web 清单与 PyPI 直接依赖的任何新增都要先登记（`deps/unlisted`）；版本升级
   时 BOM 的 `version` 与锁文件须同步（`deps/version`）；star 与最后提交在发布前 30 天内重采（TECH-FR-015），`collectedAt` 随之更新。
   `adapterBoundary` 目前只作文档（`check-deps` 的 imports/boundary 仍按内置规则），如需按 BOM 边界自动检查，由 M00 扩展
   `check_py_imports.py` 与 `checkImports`。
2. **thresholds.json 的断言型 P0**：`check-thresholds.mjs` 列出 27 个只由断言或 G1 命令判定的 P0 PERF-AC（THR-04），其中
   PERF-AC-022（图表调度）只被 flight60 用例间接覆盖、没有指标；建议 M16 在 flight60 `scene=full` 增加 `ui.charts` 指标与阈值，
   其余补"断言判定"条目后启用 `--strict`。
3. **弱网 W3 与飞行时刻对齐**：控制口已交付，`perf/net.spec.ts` 仍用代理启动后 30 s 的定时断连（本阶段不能跑 perf 用例验证改动）。
   建议 M16 在性能阶段改为 `startProxy(..., {autoCut: false})`，规格在 `__perf.bench.flightT ≥ 30` 时 `POST $AWR_PERF_PROXY_CONTROL/cut?ms=3000`。
4. **回放 UI 的另一工作包**：第 1 轮记录的 hotkeys 失败与 oxlint 违规已由该工作包修复（续作中 vitest、make lint 通过）。续作发现的
   三处前端时序问题（深链 15 s 离线上限、步进基准为 4 Hz 显示时刻、实时单步守卫读 4 Hz 状态）已提请求
   `.cache/impl/requests/FX-GW-to-M12-M15.md`；`replayFlow.ts`、`stores/timeline.ts` 在续作期间仍被该工作包修改，本包未改动。
5. **测试夹具端口**：`perf/m15/smoke.spec.ts` 的 4183 + 10k 与相邻偏移的 vite preview（4173 + 10(k + 1)）相同（0–9 时已存在，ADR-055
   已记录），建议 M15 改用空闲端口；`perf/skeleton.spec.ts` 的同类问题已在续作中改为空闲端口。
6. **perf/m11 缺失规格**：M11 PRD 列出的 `weaknet.spec.ts`（M11-AC-044）、`bandwidth.spec.ts`（AC-039）、`coi.spec.ts`（AC-033）
   尚未实现，因此未登记；M13 PRD 的 `sensors-ws.spec.ts`、`frustum.spec.ts`、`sensors-tab.spec.ts` 同样缺失。
7. **注册表中他模块的小问题**：`perf/m01/cases.mjs` 的 `m01.recon.budget` 是 pytest 类用例，却把 `.venv/bin/python -m pytest` 写进
   `cmd`（pytest 执行器会再加一次 `-m pytest`），建议 M01 改为只写测试路径与 `-m perf`。
8. **env 纪元比较**：EnvCache 把任何不同纪元都视为更新（包括迟到的旧纪元帧），sim-core 重启后纪元单调增加，实际无害；若将来引入
   不经 ring 头部的纪元来源，再改为按整数比较并在生产者重启时 `reset()`。
9. **M12-to-M11 第 4、5 条仍延后**：`configs/runtime.yaml` 的 `recorder:`、`replay:` 参数段（M12 §9.5，pydantic 严格模型目前拒绝，
   recorder 以代码默认值与 `AWR_REC_*`、`AWR_REPLAY_*` 环境变量运行）与 supervisor 启动时修复 UNFINALIZED 段（M12-FR-040）不在本包
   任务书内，INT-1 已登记为延后。建议下一轮由 M11-R 与 M12 一并处理：`RuntimeConfig` 增加两段严格模型，子进程经 `AWR_CONFIG` 读取。
10. **辅助生产者纪元的假设**：网关按"重启次数 + 1"探测；`sys/restart`（人工重启）若不计入 `restarts`，纪元猜测会落空，此时退回首见
    规则（无害，只是不回补订阅前的事件）。若要彻底消除猜测，可在 `replayRep` 增加可选 `epoch` 字段（需 M00 改 schema 并重新生成）。
