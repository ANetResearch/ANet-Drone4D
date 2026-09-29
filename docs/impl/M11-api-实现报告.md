# M11-api 实时网关：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | M11-api（所有者 M11：`python/awr/api/**`（领域 rest 文件除外；含 `rest/auth.py`、`sys.py`、`worlds.py`、`sessions.py`、`commands.py`）、`python/awr/runtime/**`、`configs/runtime.yaml`、`tools/bench/ipc/**`、`tests/rt/**`、`tests/runtime/**`、`mk/m11.mk`） |
| 日期 | 2026-09-29 |
| 依据 | M11 PRD §2、§4.2–§4.12、§5、§6.2–§6.10、§7.1–§7.7、§9、§10；AWR-17 §3、§4.1–§4.4、§6.1–§6.12、§7.1–§7.5、§8、§9.2–§9.7；AWR-03 §3.3、§3.7、§5.2、§8.4（D1-AC-08、10、11a、27、33、35）；M12 §6.7.5、§6.7.6、§7.4；前置报告 SK-E2E、SK-B、SK-F、M11-R、MS1-MS2 |
| 结论 | 在 SK-B 骨架上完成 D1-core 的完整 Gateway（会话、订阅、调度、credit、令牌桶、L4 拥塞、TIME 与纪元、可靠事件、RPC 全部命令与批量、鉴权三角色与单席位、访问模式与 Origin/Host、限流与上限、审计、兴趣集与 GCS 心跳、低频详情拆分、环境关键帧缓存、perf/server 与窗口聚合、sys/procs 与 sys/restart、停止流程、api 重启恢复语义）与 SyntheticSource；D1-ext 完成 playback 与 ReplaySource、REST 命令镜像、确认令牌与席位接管、`/api/rt/{topics,inspect}`、perf-report、audit 查询、perf/clients、`rec/*` 路由、`.awrrt` 采集、checkpoint 恢复策略。`tests/rt` 73 例（新增 66 例）与 `tests/runtime` 131 例功能用例全部通过；本工作包路径 ruff、PY-CB-01、py-imports 无违规 |

## 1. 实现清单

### 1.1 Gateway（`python/awr/api/rt/`）

| 文件 | 内容 | 对应需求 |
|---|---|---|
| `gateway.py` | 60 Hz 绝对截止时间 tick（落后跳格计 overrun）；on_tick：inbox（roster、`state/{producer}/{ext,safety,sensor,mission,env}`、`state/sim-core/{detail,perf}`、`state/agent-runtime/*`、sys/procs、liveliness `ready`/`alive`、席位回复）→ 事件 pump → 源 poll → TIME 合成与纪元（先 TIME 后 SNAPSHOT；无 checkpoint 重开时在途调用 212）→ 每秒（按 tick 序号换算，跳格不漏，放在置到期位之前）窗口与令牌桶、perf/server、sys/procs、在途表清理 → 各连接 flush 事件、置到期位 → 批量进度 → 兴趣集与 GCS。roster 合并与逐机 channel（state、state_ext、safety、env、传感器位姿）增量 advertise/unadvertise；`serverInfo.clock` 由 roster 的 `caps_ref` 合成并在变化时重发；席位缓存持久化到 `gw.seat`、宽限、未连接持有者宽限、重登记、接管 4403；生产者健康与 `status proc.<name>`；回放模式切换（enter/seek/exit、backfill 装入）；M10 `path.changed` → `uav/{id}/path`；停止流程 `begin_stop()`；`inspect()` 诊断 | FR-023、024、035、036、046–049、052、054、059、074、075、085、087、104 |
| `session.py` | ClientSession：控制面有界 FIFO 1024（满时 status 318 直接写 socket 后 1013）；sender task 先排空控制面再发送时装帧；对齐网格到期位（尾帧保证）、`slot_overwrites`、`credit_skips`；帧内 roster 在前、(priority, channel_id) 排序、≤ 1 MiB、首条记录不受预算限制；credit 窗口（`ping.srttMs` 5 s 内有效，否则最近 32 帧"发出到被 ack 覆盖"最小时延，否则 5 ms）；令牌桶每秒按 maxKbps 或已确认字节速率 EWMA 调整；L4 拥塞（> 200 ms 或连续 3 次 > 50 ms 置位、连续 3 次 < 20 ms 恢复）；订阅语义（量化、swarm ≥ 10 Hz、msgpack 通配 ≤ 2 Hz、多订阅取最高 rate 与优先级、同 id 只改 rate、通配补发 `added`、≤ 256、≥ 30 Hz Full64 ≤ 64、perf/clients 需 admin、订阅限流）；事件过滤与 event/events 合批；perf 行与 inspect 行 | FR-030–033、037–044、066 |
| `scheduler.py` | SubChan、字节令牌桶（可注入时钟）、credit 窗口公式 | FR-037、040、041 |
| `channels.py` | Channel（发布、按 (seq, reset, 帧时刻) 缓存 2 份编码、全局编码计数）、ChannelRegistry（按首段分桶的通配匹配、增删回调、按生产者 RESET） | FR-030、034、038 |
| `clock.py` | GatewayClock（TIME、gw.epoch、gw.seen；未改动逻辑） | FR-048、052、053 |
| `sources/base.py`、`live.py` | RingSource：attach、`classify`（supervisor FAILED/BACKOFF/STARTING、心跳年龄、pid、liveliness DELETE）、纪元判定（含 gw.seen 补做、"此前不健康或写者 pid 变化"的重开判据）、懒切片、行号表重建、identity 检查；新订阅逐机 channel 立即取当前值 | FR-046–049 |
| `sources/replay.py` | ReplaySource（ext）：复合帧每帧重建行号；环 segment（gen）变化不自行切 epoch | FR-050 |
| `sources/synthetic.py` | SyntheticSource：sim-core 进程内替身（写 StateRing、提供 roster/cmd/lease/clock/query、批量 per_uav、事件、环境心跳），`fake_gateway_app()` 与 `python -m awr.api.rt.sources.synthetic`：真实 Gateway 协议栈 + 合成数据（N ∈ {1, 200, 1000}） | FR-099；D1-AC-35 |
| `rpc.py` | RpcRouter：入口 ①（115、admin 专属 115、停止中 213、只读 118、席位 116）→ ②（call 50/s 突发 100 + env 2/s、sim 5/s、机群增删 10/s；111 带 retry_after_ms）→ ③ 确认令牌（112）→ 结构校验（300/110；follow_path 航点以 numpy 形状检查）→ 路由（roster 未取回时交主生产者裁决）；在途表（改绑、终态缓存回放、60 s、history、长轮询事件）；Admission 映射（accepted、rejected、duplicate + call_state、warnings）；`cmd.*` → result/progress（≤ 2 Hz）；cancel；无 checkpoint 重开 212；批量（per_uav 汇总、生产者 109 时逐机展开、progress 与 `fleet.batch.progress` ≤ 2 Hz、final）；入口拒绝的 api 事件 `cmd.rejected`；ConfirmTokens（ext）；ClientPublish（CLIENT_DATA → `ctl/sim-core/setpoint` 32 B、seq/过期/60 Hz 丢弃、322 每 channel 一次） | FR-055–062、064、070 |
| `events.py` | EventIngest：EventSubscriber 交付 → `cmd.*` 转 RPC、批量子调用与 `cmd.progress`、`env.keyframe`、`path.changed` 不进环；`sim.started`/`sim.reset` 的纪元一致性校验（`epoch_mismatch`）；`proc.state`、`seat.*` 刷新进程表与席位；全局 EventRing 65,536；api 事件 `emit_api()`；缺口 GAP + `status events.gap` | FR-065–070 |
| `interest.py` | InterestAggregator（detail ≤ 64 最近订阅优先、marks ≤ 16、topics、250 ms 去抖 + 1 Hz 重发、`interest.truncated`）、GcsBeacon（5 Hz，按 principal 计龄） | FR-024、071 |
| `detail.py` | DetailDemux（`[[agent_no, item]]` 只 skip 建索引、bin 去头、最近一批供新订阅立即取值；sensor 48 B、detail 32 B 切片；mission 批；agent-runtime 状态）、EnvCache（关键帧事件与心跳，(epoch, version) 只前进，presets_sha256 校验） | FR-072–074 |
| `metrics.py` | 进程 CPU（/proc/self/stat）、事件循环延迟、tick 年龄与 on_tick 耗时、编码与字节速率、perf/server 构建、600 s 环与窗口聚合 | FR-085–087 |
| `playback.py` | PlaybackController（ext）：open（117、sys/start、122）/seek（110、TIME → playbackState → SNAPSHOT）/play/pause/speed（110、SPEED_CLAMPED）/close（sys/stop），playbackState 广播 | FR-078、079 |
| `inspect.py` | R57 `/api/rt/topics`、R58 `/api/rt/inspect`（demo 需 admin，`?dump=` 仅 dev/test） | FR-086（ext） |
| `awrrt.py` | `AWR_RT_CAPTURE` 采集（每连接 `.awrrt`，后台线程写盘，上限 20 万条 / 256 MiB） | FR-088（ext） |
| `ws.py` | 握手、hello（maxKbps、tier、deviceClass、resume 与超出环范围的 GAP）、控制面分派、二进制 CLIENT_DATA 与格式错误计数、ping 与 clientStats 限频、playback、采集钩子、`auth.denied` 审计 | FR-020、025、027、029、062 |
| `protocol.py` | 未改动 | — |

### 1.2 api 进程其他文件

| 文件 | 内容 | 对应需求 |
|---|---|---|
| `main.py` | ApiContext 增加审计写线程与共享限流器；Gateway 注入 `audit`、`limiter`、`K_confirm`；SIGTERM/SIGINT 包装（先 `begin_stop()` ≤ 1.5 s 再交还 uvicorn，避免 uvicorn 先以 1012 断开 WS）；未受监管但给了 `AWR_ZENOH_CONFIG` 时按该配置开会话（基准用）；挂载 rt inspect 路由 | FR-080、104 |
| `middleware.py` | 顺序 Host → Origin → RequestId → 安全头 → Auth → 停止中 213 → 请求体上限（1 MiB，perf-report 4 MiB，413 307）→ RateLimit（principal 或来源地址，429 111 + Retry-After）→ Idempotency-Key（60 s 重放、422 321）；`auth.denied` 审计；REST 活动记入席位判定 | FR-022、026、069、080 |
| `ratelimit.py` | 令牌桶表（call、env、sim、fleet_edit、sub 20/s 突发 100、ping、stats、perf_report） | FR-026 |
| `audit.py` | `audit.jsonl` 后台写线程（O_APPEND 单行、1 s fsync、关闭时排空）、`auth.denied` 每来源每秒 ≤ 1 条 | FR-069 |
| `security.py`、`settings.py` | `K_confirm`；`procs_query`/`has_supervisor`（测试挂 supervisor 替身） | FR-021、055 |
| `rest/auth.py` | 审计 `auth.token_issued`（jti、client）、口令失败 `auth.denied`、签发后席位未连接判定、admin 席位冲突时仍签发（不占席）、R03 `POST /api/auth/confirm`（ext） | FR-021、023、069 |
| `rest/sys.py` | R54 `POST /api/sys/restart`（admin）、R60 `GET /api/sys/perf`、R47–R49 perf-report（ext）、R55 audit（ext）；原有 R50–R53、R56、R59 | FR-081、086 |
| `rest/commands.py` | R19–R22 命令镜像（ext）：与 WS 共用入口与在途表，拒绝转 problem+json，长轮询 ≤ 25 s | FR-063 |
| `rest/sessions.py` | `caps_clock` 取 Gateway 合成值 | FR-054 |

### 1.3 运行时库、配置与工具

| 文件 | 内容 | 对应需求 |
|---|---|---|
| `runtime/statering.py` | `publish()` 允许 Full64 行数少于 Lite32（回放复合帧 `full_len = m × 64`）；`commit_publish(…, full_rows=)` 仅关键字新增参数 | FR-050；M12 §6.7.5 |
| `runtime/checkpoint.py` | `CheckpointStore.restore_for_restart(restart_count)`：恢复后 5 s 内再崩 → 该代 poison 改用上一代；连续 3 代中毒 → None（剧本起点重开）；`confirm_restored()` | FR-017（ext） |
| `runtime/cli.py` | `awr status --ws`（经 R58 列出 WS 会话） | M11-R-to-M11 第 7 条 |
| `configs/runtime.yaml` | api 行补 `--ws-max-queue 32` | FR-028 |
| `tools/bench/ipc/bench_gateway.py` | 真实 api 进程 + 合成生产者进程（ZenohBus + mmap StateRing）+ N 个协议客户端（Tier S 默认订阅集、30 Hz 消费、ack 合并、srttMs）；api CPU、`/api/sys/perf` 窗口、swarm 实际频率；门禁按 3 客户端与 10 客户端两档 | M11-AC-018、019、020 |
| `tools/bench/ipc/bench_encode.py` | raw Full64、Lite32、msgpack、JSON 编码与 3 客户端每 tick 装帧耗时（门禁 p99 ≤ 0.3 ms） | M11-AC-019 |
| `tools/bench/ipc/rt_client.mjs` | Node 22 内置 WebSocket 的轻量协议客户端（多客户端、Tier S 订阅集、30 Hz 消费与 ack 合并、srttMs），对运行中的 api 压测并输出 `awr.bench.rtclient.v1`（swarm 实际频率、帧率、下行字节） | M11-AC-020；PERF-FR-014 |
| `tools/bench/ipc/netem_proxy.py` | 用户态弱网代理（W0–W3：截断正态时延、每方向令牌桶带宽、按概率停顿且停顿期间不读上游、定时断连并拒绝新连接；SIGUSR1 或 `stall()` 注入停顿；可作库在线程中运行） | M11-AC-017、AC-044；AWR-18 §8.7(3)、PERF-FR-015 |
| `mk/m11.mk` | `test-rt`（pytest + vitest tests/m11、tests/net）、`test-rt-chaos`、`bench-gateway`、`bench-gateway-10`、`synthetic-gw`；`bench-ipc` 追加两项 | M11 §9.6 |

### 1.4 测试（`tests/rt/`）

| 文件 | 用例 | 覆盖（验收编号） |
|---|---|---|
| `fakesim.py`、`gwstub.py` | — | FakeSim（sim-core 的总线与环行为替身，可暂停心跳、模拟重开与 checkpoint 恢复、批量开关）、FakeSupervisor、FakeReplayWorker、GwStack（FakeSim + 真实应用 + uvicorn 线程，可重启 api）；调度单元测试替身 |
| `test_subscribe.py` | 4 | AC-012 |
| `test_scheduler.py` | 5 | AC-013（尾帧三速率、credit 挡住后源停止、sender 每 3 tick 取一次、无值不发帧、编码一次） |
| `test_credit.py` | 6 | AC-014、016、017、控制面积压 1013 |
| `test_rpc_lifecycle.py` | 6 | AC-025、027（功能部分）、REST 镜像 |
| `test_teleop.py` | 2 | AC-029（网关部分） |
| `test_events.py` | 4 | AC-004（网关部分） |
| `test_interest.py` | 7 | AC-030、031、032、`uav/{id}/path`、agent 状态 |
| `test_epoch.py` | 8 | AC-021、022（网关部分）、023、024 |
| `test_access.py` | 4 | AC-008、009 |
| `test_limits.py` | 4 | AC-010 |
| `test_rest_contract.py` | 5 | AC-034、035、确认令牌、席位接管 |
| `test_shutdown.py` | 3 | AC-049、AC-011 |
| `test_recovery.py` | 1 | AC-047（功能口径） |
| `test_synthetic.py` | 4 | AC-040（Python：N ∈ {1, 200, 1000}）、`.awrrt` 采集 |
| `test_playback.py` | 1 | AC-042（网关部分） |
| `test_netem_proxy.py` | 2 | 弱网代理：W0 透明（REST 与 WS）、W1 时延、停顿与断连 |
| `test_chaos_api_kill.py` | 1（chaos + perf，未执行） | AC-047 真实进程口径 |
| `test_skeleton_chain.py`、`test_seat_grace.py` | 7（原有） | 真实 sim-core 链路回归（本包补 `run.keep_run_dir=false`） |

## 2. 验收对照

| 编号 | 要点 | 结果 | 证据 |
|---|---|---|---|
| M11-AC-004 | 缺口补拉、乱序 0、1 s 放弃、GAP、resume、REST 410 | 通过（网关部分；570 条/s 压测见 M11-R bench_cmd） | `test_events.py` |
| M11-AC-008 | 握手顺序、400、4401、300、4408、4426 | 通过 | `test_access.py::test_handshake_order_and_hello_rules`、`test_ws_denials` |
| M11-AC-009 | 回环 Origin 任意端口、其他 403、Host 400、局域网口令、token 不入日志 | 通过（`ssh -L` 冒烟属 M16 的 `tests/e2e/remote_smoke.sh`） | `test_access.py` |
| M11-AC-010 | 33 个连接 4429、257 订阅 315、257 KiB 1009、3 次格式错误 1002、第 101 次 call 111 | 通过 | `test_limits.py`、`test_rpc_lifecycle.py::test_rate_limit_111` |
| M11-AC-011 | 提议 deflate 时无 `Sec-WebSocket-Extensions`；runtime.yaml 参数齐全 | 通过（以 runtime.yaml 原样命令行起 uvicorn 子进程） | `test_shutdown.py` |
| M11-AC-012 | 订阅语义 | 通过 | `test_subscribe.py` |
| M11-AC-013 | 尾帧、编码一次、发送时装帧（含原型失败场景） | 通过（P1 编码共享比口径由 bench 给出） | `test_scheduler.py` |
| M11-AC-014 | W 四种情形、窗口满数据 0 控制面照常、只发最新 | 通过（客户端 ack 节奏属 net 工作包） | `test_credit.py` |
| M11-AC-016 | maxKbps 2048：priority 0 不降频、priority 3 顺延后送达、首条不受限 | 通过 | `test_credit.py::test_bucket_priority_and_first_record` |
| M11-AC-017 | 拥塞 status 与恢复、控制面不丢、无 1013 | 通过（以可注入发送耗时的 WS 替身；代理口径见遗留问题第 2 条） | `test_credit.py::test_congestion_status_and_recovery` |
| M11-AC-018/019/020 | 容量与 tick 预算 | 未判定（并行阶段禁跑基准；`make bench-gateway`、`bench-gateway-10`、`bench-ipc` 已就绪，仅做过几秒冒烟） | `tools/bench/ipc/bench_gateway.py`、`bench_encode.py` |
| M11-AC-021 | 先 TIME 后 SNAPSHOT、api 重启 epoch 不变、api 停机期间重开补做 + 1、伪造 sim.started 计 mismatch | 通过（网关部分；rt.worker 暂存规则属 net 工作包） | `test_epoch.py` |
| M11-AC-022 | checkpoint 恢复 epoch 不变、受影响 channel 下一条 RESET、其他不受影响 | 通过（网关部分） | `test_epoch.py::test_scenario_reset_no_212_and_checkpoint_reset_flag` |
| M11-AC-023 | 心跳停写 STALLED、liveliness DELETE、BACKOFF → RESTARTING、FAILED、proc 状态、ready 503、布局 312 | 通过 | `test_epoch.py` |
| M11-AC-024 | t_srv_ns、10 个 TimeState 与 bit7、pong、serverInfo.clock 随 caps | 通过（ClockSync 误差属 net 工作包） | `test_epoch.py` |
| M11-AC-025 | 10 个主命令生命周期、只发发起连接、duplicate 执行计数 1、115/116/118/111/300/110/107、cancel、REST 镜像 | 通过 | `test_rpc_lifecycle.py` |
| M11-AC-027 | 批量 1 条汇总、progress ≤ 2 Hz、fleet.batch.progress、final | 通过（功能口径；1000 架风暴与 Playwright 属 M16） | `test_rpc_lifecycle.py::test_batch_*` |
| M11-AC-029 | CLIENT_DATA 转发、过期与乱序丢弃、322、viewer 拒绝 | 通过（网关部分；sim-core 锁存与看门狗属 M08） | `test_teleop.py` |
| M11-AC-030 | 兴趣集 ≤ 350 ms（本机功能口径 ≤ 500 ms 断言）、并集、topics、65 架截断、退订 ≤ 1.25 s、原样切片 | 通过 | `test_interest.py` |
| M11-AC-031 | 丢关键帧后 ≤ 1 s 心跳恢复、presets 不一致 status | 通过（网关部分；version 缺口的补拉由事件 seq 补拉覆盖） | `test_interest.py::test_env_cache_*` |
| M11-AC-032 | 持有者断开后 ping_age 增长、GRACE、宽限内重连 resume、第二个 operator 116 | 通过（网关部分；1.5 s 告警与 3 s HOLD 属 M09 联测） | `test_interest.py::test_gcs_*` |
| M11-AC-034 | 自动发现、problem+json、头部、Idempotency、审计无 token | 通过（OpenAPI 快照待 M00 生成） | `test_rest_contract.py`、`test_limits.py`、`test_access.py` |
| M11-AC-035 | perf/server 字段与 1 Hz、`/api/sys/perf` | 通过（hb.api 与 faulthandler 只在受监管时启用，未单测） | `test_rest_contract.py` |
| M11-AC-040 | SyntheticSource N ∈ {1, 200, 1000} | 通过（Python；`.awrrt` golden 回放仍由 fake_gw 覆盖） | `test_synthetic.py` |
| M11-AC-041 | walking skeleton | 通过（回归） | `test_skeleton_chain.py` |
| M11-AC-042 | 回放：TIME → playbackState → SNAPSHOT、seek 只 + 1、复合帧切片、REPLAY 与 bit7、118、SNAPSHOT 中 env 为 backfill 值 | 通过（网关部分，以 FakeReplayWorker；与 M12 真实 replay-worker 联调待其交付） | `test_playback.py` |
| M11-AC-047 | kill api：仿真不断、重连、同 cid duplicate；kill sim-core：新 epoch TIME 与 SNAPSHOT、212 | 功能口径通过（api 重启与生产者重开）；真实进程 ≤ 3 s 判据的 chaos 用例已写好待验收阶段执行 | `test_recovery.py`、`test_epoch.py`、`test_chaos_api_kill.py` |
| M11-AC-049 | SIGTERM：213、`sys.shutting_down` 与 `status proc.api`、1001、≤ 5 s 退出、审计落盘 | 通过 | `test_shutdown.py` |
| M11-AC-007（ext 部分） | 5 s 内再崩 poison 改用上一代、连续 3 代中毒重开 | 通过（存储侧策略；sim-core 接线属 M08） | `tests/runtime/test_checkpoint.py` |

## 3. 与 PRD 的偏差及理由

| 编号 | 偏差 | 理由与影响 |
|---|---|---|
| D-01 | 批量命令在生产者回 109（不支持批量准入）时由入口逐机展开（cid `<batch_id>:<vehicle_id>`，均带 batch_id） | 当前 sim-core 骨架对 `uav` 列表回 109；回退让 D1-AC-27 的批量路径现在可用，M08 实现批量准入后自动不再触发 |
| D-02 | `cancel` 失败以 `error{code: 105}` 返回（PRD 写作 rejected 105） | `cancel` 不是 `call`，没有自己的 call id；以目标 id 发 result 会篡改目标调用的状态（17 §6.3 error 用于非 call 请求） |
| D-03 | 订阅限流为 20 条/s、突发 100（17 只给速率） | 突发 20 时，客户端连接后一次性订阅默认集与 32–64 架关注集会被拒 |
| D-04 | REST 命令镜像不在中间件计数，由 RpcRouter 入口 ② 统一按 `call` 桶限流后转为 429 | 否则同一请求被计两次 |
| D-05 | admin 签发遇席位冲突时仍签发（`seat: none`），operator 仍返回 116 | 否则 admin 无法取得 token 执行 `seat/takeover`（ext） |
| D-06 | 签发 operator token 后 30 s 内既无 WS 连接也无 REST 请求时进入宽限（再 30 s 释放） | SK-E2E-to-M11 第 1 条待定策略；REST-only 调用方以 REST 活动保活 |
| D-07 | 席位缓存持久化到 `/dev/shm/awr/<run>/gw.seat`；api 启动时沿用持有者并进入宽限 | 否则 kill api 后重连的持有者会被入口 ① 以 116 拒绝，D1-AC-11a 的"同 cid 得 duplicate"无法成立 |
| D-08 | roster 尚未取回（api 刚启动）时机体命令交主生产者裁决（未知机体由生产者回 107） | 同上：重启后客户端立即重发时 roster 查询可能尚未完成 |
| D-09 | 每秒任务按 `k // 60` 变化触发并放在置到期位之前 | tick 跳格时 `k % 60 == 0` 会漏掉整秒；放在之前使 1 Hz 订阅在同一对齐 tick 取到本秒新值 |
| D-10 | uvicorn 的 SIGTERM 处理器被包装：先 `begin_stop()` 再交还 | uvicorn 在 lifespan 关闭前以 1012 断开 WS，照原样无法满足 FR-104 的"先事件与 status、再 1001" |
| D-11 | SyntheticSource 实现为 sim-core 的进程内替身（生产者侧），Gateway 仍以 LiveSource 读它；`.awrrt` 逐字节回放继续由 fake_gw 负责 | 让"真实协议栈"覆盖到 RPC、事件、TIME 全路径；PRD 签名中的 `awrrt` 参数保留但不实现回放模式 |
| D-12 | `playbackState` 发给全部连接 | 回放是会话级模式，所有客户端都需要进入只读与时间轴状态 |
| D-13 | 进程状态横幅不报按需进程的 STOPPED 与未交付模块（`module_missing`）的 FAILED；sim-core 的横幅由生产者健康驱动 | D1 中 ext 进程模块尚未交付，否则常驻错误横幅 |
| D-14 | perf/server 的 `api` 段附加 `tick_p99_ms`（on_tick 耗时 p99）与 `epoch_mismatch` | schema 允许附加字段；AC-019 的 tick 预算与 ARCH-AC-009 的计数需要 |
| D-15 | `/api/sys/perf` 的客户端字段取各连接最大值（`clients.*`） | PRD 未规定数组字段的聚合口径 |
| D-16 | `state/replay/{mission,env}` 的 key 由 `_with_producer()` 替换生成常量的生产者段 | bus_keys 缺少对应构造函数，已请求 M00 |
| D-17 | 兴趣集 `record_scope` 不发送 | `bus/interest.schema.json` 为 additionalProperties: false，待登记 |
| D-18 | CSP、静态拆分回退（FR-084）、会话世界切换（FR-103）、按需 `sys/start`/`sys/stop` 以外的 supervisor `sys/run` 未实现 | 均为 D1-ext；CSP 需前端配合验证，会话切换依赖 supervisor `sys/run` 编排（当前回 213） |

## 4. 测试结果

```text
.venv/bin/python -m pytest tests/rt -m "not perf and not chaos"        73 passed
.venv/bin/python -m pytest tests/runtime -m "not perf"                 131 passed（其中 test_supervisor 的 kill -9 ≤ 100 ms
                                                                         用例在整套并发运行时偶发超时，单独重跑通过，属机器负载）
.venv/bin/ruff check python/awr/api python/awr/runtime tools/bench tests/rt tests/runtime   All checks passed
tools/lint/check_py_callbacks.py、check_py_imports.py                本工作包路径无违规（现存违规均在 awr/sim/**，M08 进行中）
node tools/lint/no-emoji.mjs 等 make lint-tools 的 JS 规则             通过
```

浏览器冒烟：以当时的测试构建运行 `npx playwright test perf/skeleton.spec.ts -g "full chain"`（自起 supervisor 的 sim-core + api），
页面 boot 到 REVEALED 且 rt 连接 LIVE，随后在用例的 `__vp.backend()` 深相等断言处失败（视口测试钩子新增 `state` 字段，与网关
无关，已写入 M11-api-to-M16 第 6 条）。

按并行阶段约束未运行 perf、chaos 用例与基准；`bench_gateway.py`、`bench_encode.py` 只以 `--secs 3 --runs 1 --no-lock` 做过脚本
冒烟（验证进程编排与输出格式，数值不作数）。

## 5. 遗留问题

1. D1-AC-08 / M11-AC-018–020 的容量数值需在验收阶段以 `make bench-gateway`、`bench-gateway-10` 与 M16 flight60 harness 实测；
   冒烟中 on_tick p99 偶见数毫秒（机器被其他 agent 占满，不作数），若验收超标，优先检查 `_publish_perf`（numpy 百分位）与
   首帧 SNAPSHOT 的编码量。
2. `netem_proxy.py` 已交付，但本机回环的内核收发缓冲可达数 MB，500 ms 停顿在默认订阅集的数据率下填不满缓冲、服务端 `send`
   不会被阻塞，因此 AC-017 的拥塞判据以可注入发送耗时的 WS 替身验证，代理用于 AC-044 的弱网剖面。
3. 会话世界切换（FR-103、AC-043）：需 supervisor `sys/run` 编排 run 切换与 api 以新命名空间重开总线，未实现。
4. CSP（FR-082 P1）与静态拆分回退（FR-084）未实现。
5. 回放链路只以 FakeReplayWorker 验证；与 M12 真实 replay-worker、recorder 的联调待其交付。
6. faulthandler 看门狗与 `hb.api` 只在受监管时启用，未写单测（M11-R 的 supervisor 挂死检测用例覆盖心跳路径）。
7. `awr sys loglevel` 仍未交付（17 未登记对应端点）。

## 6. 请求文件（`.cache/impl/requests/`）

| 文件 | 要点 |
|---|---|
| `M11-api-to-M00.md` | bus_keys 构造函数补齐、OpenAPI 快照导出方式与新增路由、ops.schema error 的 retry_after_ms、event kind 登记、record_scope、fake_gw 复用 SyntheticSource、m11.mk 目标、bench-result 字段 |
| `M11-api-to-M08.md` | 批量 Command 与回退、GCS、兴趣集与详情格式、CLIENT_DATA setpoint、sim.started 字段、212 判据、seat_takeover 与 seat.* 事件、mission args.mid、warnings、checkpoint 恢复策略、StateRing 复合帧、path.changed |
| `M11-api-to-M12.md` | playback 的 bus 消息与 backfill 字段、回放环复合帧与 gen 语义、state/replay/* 与 ctl/replay/roster、rec/* 路由、FakeReplayWorker |
| `M11-api-to-M07-M09-M10-M13-M14.md` | detail、env、safety、mission、path、sensor、agent 状态的网关消费格式 |
| `M11-api-to-M15.md` | status 横幅 id、serverInfo 重发时机、席位与接管、cancel 的 error 105、批量结果形状、R60/R54 |
| `M11-api-to-M11.md` | 给 net 工作包：订阅突发 100、Full64 上限、maxKbps、srttMs、playbackState 广播、cancel、perf/server 新字段 |
| `M11-api-to-M16.md` | 验收阶段需执行的 chaos 用例与 bench-gateway、synthetic-gw |

收到的请求处理：

| 请求 | 处理 |
|---|---|
| SK-E2E-to-M11 第 1 条（未连接持有者永久占席） | 已实现（D-06） |
| SK-F-to-M11 第 8 条、SK-E2E-to-M11 第 3 条（conn_id 同源） | 保持；perf 行新增 kbps |
| M11-R-to-M11 第 1–5 条 | 按约定使用 init_child、ZenohBus、EventSubscriber、StateRing 读者与 sys/* 回复格式 |
| M11-R-to-M11 第 6 条（fake_gw 复用） | 交付 SyntheticSource 与 `fake_gateway_app()`，已写入 M11-api-to-M00 |
| M11-R-to-M11 第 7 条（awr status --ws、sys loglevel） | `--ws` 已实现；loglevel 待端点登记 |
| M11-net-to-M11 第 1 条（perf/server fps 为 0） | fps 取 ack.fps，缺省取 clientStats.fps |
| M11-net-to-M11 第 2 条（whoami 401/403） | 失效 token 401（302）、非白名单 Origin 403（303），已有用例覆盖 |
| M11-net-to-M11 第 3 条（layouts 9 个 schema） | serverInfo.layouts 含 ClientDataHeader 与 VelSetpoint16 |
| M11-net-to-M11 第 4 条（perf/server 首帧不含新连接） | 新订阅 perf/server 时以上一秒的值加当前全部连接的行重发一次 |
| M11-net-to-M11 第 5 条（keep_run_dir） | `test_chain_real_processes` 已加 `run.keep_run_dir=false` |
| M11-net-to-M11 第 6 条（test-rt 含 vitest） | `make test-rt` 追加 vitest tests/m11 与 tests/net |
| M15-to-M11 第 3 条（席位变化后 serverInfo 回调） | 持有者变化时对全部连接重发 serverInfo；第 1、2 条（apiDelete、按角色重签）属 net 工作包 |
| M04-to-M11、M00-to-M11、M00-B-to-M11、MS12-to-M11 | 已由前序工作包处理，本包未改变其约定（R07 挂载、问题体、生成物接口） |
