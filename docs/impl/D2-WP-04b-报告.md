# D2.1 WP-04b 沙盒池：容量、驻留、身份库：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | WP-04b 沙盒池：容量、驻留、身份库（所有者 M17；AWR-04 §15.15.1 第 2 行；依赖 WP-03b、M08 变更请求） |
| 日期 | 2026-10-05 |
| 依据 | 用户 D2.1 原文 `docs/inputs/D2.1-需求原文-2026-10-05.md`；AWR-04 v1.3 §15.4 至 §15.9、§15.12 至 §15.15（ADR-118 至 ADR-121、ADR-123）；M17 PRD §14（M17-FR-068 至 FR-082、NFR-016 至 018、AC-025 至 034）；17 §17.20.1 至 §17.20.5、§17.20.7；14 §20.4.7；请求 `d2-WP-12-to-M17.md`、`d2-WP-16-to-M17.md` |
| 路径 | `python/awr/sandbox/**`（新 `park.py`、`identity.py`、`capacity.py`）、`python/awr/api/rest/sandbox.py`、`apps/web/src/stores/{capacity,sandboxSession}.ts`、`apps/web/src/net/sandbox/**`、`tools/sdk/**`；测试 `tests/sandbox/**`、`apps/web/tests/m17/**`（ADR-110 登记的 M17 测试目录） |
| 约束执行 | 只写本包路径；跨所有权需求写入 `.cache/impl/requests/d2-WP-04b-to-*.md`（6 份）与契约草案 `d2-WP-04b-drafts/`；未安装依赖；未执行 git 写操作；未跑性能基准（数值类只记功能用例中的实测耗时）；无 emoji 与禁用字形；口令只在测试运行时随机生成，仓库与本文不含任何口令 |
| 结论 | P0 全部实现：公开容量接口与池统计（FR-068、069）、驻留与恢复全链路（FR-070 至 075，含服务端兜底、无连接驻留、抢占与 L3/L4 回收改为驻留、恢复票、保留与上限、池重启后恢复）、内部档（FR-076）、身份库（FR-077）、前端两个 store（FR-078、079）、SDK（FR-080）；P1 的指挥器配对（FR-081）与内存额度预留（FR-082）也已实现。checkpoint 落盘在 M08 正式接口到来前走过渡路径，已用真实 sim-core 验证（驻留 0.7 s、恢复 2.7 s、仿真时刻差 ≤ 0.1 s）。Python 新增用例 50 个、按 D2.1 口径改写 2 个，Vitest 新增 17 个，全部通过；`make lint` 中本包文件全部通过（唯一失败项为他人文件，§4.3） |

## 0. 摘要

1. **实例用户口径与容量**：槽位 = SPAWNING、READY、ACTIVE、IDLE、PARKING、DRAINING 的会话 + OFFERED 票；PARKED 不占槽位。内部档会话先占
   `internal.reserved_slots`（1）个不计入 N_max 的预留槽位，总占用至多 N_max + 1。池命名空间 `sys/session/stats` 给出池统计与完整的
   `awr.capacity.v1`（池统计 + show-api 状态文件 + 部署 `RELEASE` 构建信息 + 节能阈值 + `hint`），sandbox-api 的 SB-71 只做 1 s 缓存与
   并发合并（`awr.api` 不能依赖 `awr.sandbox`，§3 偏差 1）。
2. **驻留（PARKING → PARKED）**：触发来源为 SB-69（客户端，`idle`、`hidden`、`user`）、SB-72 信标、服务端兜底（ACTIVE 且最近交互超过
   `sandbox_idle_s + grace_s`，队列非空用 `sandbox_idle_busy_s`，内部档用 `internal_idle_s`，提前 `warn_s` 推送
   `sandbox.expiring{action: park}`，告知期间有新交互即取消，手动控制租约期间不驻留）、无连接空闲 `idle_disconnected_s`（队列非空 60 s，
   替代 D2 的空闲结束）、抢占与 L3/L4 回收。池执行：暂停时钟 → checkpoint 落盘（正式接口或过渡路径）→ 导出配置快照 → 记录环头部 →
   停止进程 → 原子写入 `var/sandbox/parked/<sid>/{checkpoint.bin, snapshot.json, session.json, meta.json}`（超过 8 MB 时丢弃 checkpoint
   退回快照）→ 删除 tmpfs、叶子组与运行目录中除日志、secret、会话配置外的内容 → 释放槽位并派发 → 驻留上限（每前缀 2、全站 64、
   512 MB，超出时最早者结束）。剩余寿命冻结，`lifetime_max` 只累计持有时间。
3. **恢复**：SB-70 有空位（且队列中没有等待的公开票，"不插队"）时以驻留目录拉起：兼容时把 checkpoint 放回 tmpfs、预建环、以
   `AWR_RESTART_COUNT = 1`（另带 `AWR_RESUME_FROM`）拉起；不兼容（发布、契约、布局不同）直接按配置快照装配。就绪后核对环头部仿真时刻，
   落后超过 0.1 s 即报告 `resume_snapshot`；核对完成前不播放。`expires = now + max(remaining_s, 300 s)`，epoch + 1，换发 token。满员时
   发恢复票（`kind = resume`，与新建票同一 FIFO、同一认领规则，不占新建票名额），认领后拉起同一 sid。恢复失败时 checkpoint 口径改用
   快照重试一次，再失败结束（`resume_failed`，配置快照保留）。PARKED 保留 3600 s，到期结束（`parked_expired`，配置快照另存 600 s）。
4. **内部档**：池验签 `X-AWR-Identity`（sandbox-api 原样转交）后创建 `tier = internal` 会话：预留槽位、预留已用或内存准入不满足时内部票
   排在全部公开票之前（不抢占公开会话）、免工作量证明与前缀配额、寿命上限 8 h、队列非空也可续期、不被抢占、L3/L4 回收排在最后、
   每个身份同时 ≤ 2 个（会话加排队票）；沙盒 token 载荷带 `tier: "internal"`。身份撤销（撤销表 ≤ 1 Hz 重载）或身份令牌过期后降为公开档：
   寿命按公开上限截断，已超出者提前 60 s 告知后驻留。
5. **身份库 `awr.sandbox.identity`**：密钥文件规则、等长摘要常量时间比较、固定时延 300 ms ± 25 ms（抖动只在补足部分）、每前缀 0.5 次/s
   突发 3、三级锁定（前缀 5 次/10 min → 30 min、聚合前缀 10 次 → 30 min、全站 30 次/h → 15 min 且回环免全站锁定）、锁定期内不比对不计数、
   成功清零、锁定状态落盘（前缀以 HMAC 散列）且重启保留、身份令牌签发与验签（`K_ident = HKDF(密钥, "awr-identity", "awr/identity/v1")`）、
   撤销表；审计只记 jti 与前缀散列，任何路径不记录输入与令牌。由 M11 的 R79、R80、R01 调用（接口见 `d2-WP-04b-to-M11.md` 第 7 条）。
6. **前端**：`stores/capacity.ts`（启动期 1.5 s 超时与 5 s 刷新、揭开后按可见消费者租约 10 s 轮询、`updated`）；`stores/sandboxSession.ts`
   的驻留、信标、恢复与恢复票、PowerController 接口 `onPowerState`、4430 与 515、AWR-14 §20.4.7 的存储键与 WP-12 请求的字段、
   宽 < 360 的守卫（ADR-122）、配对；`net/sandbox/client.ts` 的 SB-69 至 72、76、77 与身份头。
7. **SDK 1.1.0**：`Session.park()`、`resume(wait=True)`；会话请求遇 515 或实时连接 4430 时自动恢复一次再重试（换新的幂等键）；
   续期不唤醒驻留会话；`Sandbox.capacity()`；`create(identity_token=...)`。

## 1. 实现清单

### 1.1 池（`python/awr/sandbox/**`）

| 需求 | 实现 | 文件 | 验收（测试） |
|---|---|---|---|
| M17-FR-068 容量接口 | `pool_stats`、`read_show_status`（5 s 停更为 unknown）、`build_info`（`RELEASE` 新旧字段、构建环境、只读 git、`nogit`）、`assemble`、`hint_of`；池 `stats` 回复带 `capacity` | `capacity.py`、`pool.py` | `test_capacity.py`（6 个，含草案 schema 校验与 ≤ 1 KiB）；M17-AC-025 |
| M17-FR-069 实例口径 | `SLOT_STATES` 含 PARKING；`free_slots`、`free_reserved`、`n_internal`、按档位的 `admit` | `model.py`、`broker.py` | `test_capacity.py::test_pool_stats_instance_user_semantics`、`test_park.py::test_internal_*`；M17-AC-026 |
| M17-FR-070 驻留请求 | `park()`（202、200、409 513）；会话端点在 PARKING、PARKED 时 409 515（`resume_url`） | `park.py`、`broker.py` | `test_park.py::test_park_request_fields_and_slot_release`、`test_park_rejected_states`；`test_park_pool.py`；M17-AC-027 |
| M17-FR-071 兜底与无连接驻留 | `power_tick`：兜底阈值 + 余量、提前告知、交互取消、手动控制不驻留；IDLE 无连接 120 s（队列非空 60 s） | `park.py` | `test_server_fallback_idle_park_warning_cancel_and_manual`（阈值 + 余量 ± 2 s）、`test_idle_disconnected_park_and_busy_threshold`、`test_broker.py::test_idle_unclaimed_and_lifetime` |
| M17-FR-072 抢占与回收改为驻留 | `_maybe_preempt`、`_ladder` → `schedule_park`（`preempted`、`reclaimed_cpu`、`reclaimed_mem`），内部档免抢占、回收排最后；checkpoint 失败以快照驻留 | `broker.py`、`park.py` | `test_preempt_and_reclaim_park_instead_of_end`、`test_broker.py::test_preempt_longest_holder_with_notice`、`test_park_pool.py::test_checkpoint_failure_parks_with_snapshot`；M17-AC-028 |
| M17-FR-073 恢复 | `resume()`、`_resume_spawn`、恢复票与认领、`_prepare_resume`（兼容检查、预建环、`AWR_RESUME_FROM`）、`_finish_resume`（核对）、`on_resume_failed`；token 过期判定按 `token_exp + retention_s` | `park.py`、`pool.py`、`spawner.py` | `test_resume_*`（4 个）、`test_park_pool.py`（8 个）、`test_real_park.py`；M17-AC-029 |
| M17-FR-074 保留与上限 | `_end_parked`、`_park_limits`、驻留目录与 `state.json` 持久化、池重启接管（PARKED、PARKING、恢复中途） | `park.py`、`pool.py` | `test_retention_expiry_keeps_snapshot_and_park_limits`、`test_park_disk_limit`、`test_parked_survives_pool_crash`、`test_retention_end_keeps_snapshot` |
| M17-FR-075 活动聚合 | `activity` 接受 `last_input_unix_ns`；没有时以调用计数为交互；D2 状态文件补齐 D2.1 字段 | `broker.py` | `test_server_fallback_*`、`test_load_d2_state_without_d21_fields` |
| M17-FR-076 内部档 | 预留槽位、队首内部票、免工作量证明与前缀配额、8 h、免抢占、每身份 ≤ 2、`tier` token、撤销或过期降档 | `broker.py`、`park.py`、`tokens.py`、`queue.py` | `test_internal_tier_reserved_slot_queue_head_and_limits`、`test_internal_memory_admission_queues_without_preempting`、`test_identity_revoked_demotes_to_public`、`test_rest_park.py::test_internal_identity_create`；M17-AC-030 |
| M17-FR-077 身份库 | `IdentityService`（`decide`、`check`、`acheck`、`issue`、`verify_token`、`revoke`）、`RevocationList`、`read_secret_file`、`IdentityCfg`（含 WP-16 的 `rps`、`burst` 键名） | `identity.py` | `test_identity_lib.py`（10 个）；M17-AC-031 |
| M17-NFR-016 驻留开销 | 真实 sim-core：驻留 0.73 s、恢复到 READY 2.73 s；替身：≤ 5 s、≤ 6 s 断言 | — | `test_real_park.py`、`test_park_pool.py::test_park_and_resume_from_checkpoint` |
| M17-NFR-018 身份恒定时延 | 正确与错误的中位数差 ≤ 10 ms（各 9 次，交错） | `identity.py` | `test_fixed_delay_median_difference` |
| M17-FR-081（P1）配对 | `pair{op: code}`（8 位大写 base32、120 s、每会话 10 min ≤ 5 次、只存散列）、`pair{op: redeem}`（单次、错 5 次作废、同一 principal 的 token） | `broker.py`、`model.py` | `test_pair_code_single_use_ttl_and_rate`、`test_rest_park.py::test_commander_pairing`；M17-AC-034 |
| M17-FR-082（P1）额度预留 | `reserve{mem_mb, ttl_s}`（只接受 `svc:supervisor`）、`m_sb()` 进入准入、超额拒绝、到期与 0 撤销 | `broker.py`、`pool.py` | `test_reserve_memory_quota`、`test_reserve_only_for_supervisor_principal`；M17-AC-034 |

### 1.2 REST（`python/awr/api/rest/sandbox.py`）

| 端点 | 要点 | 验收 |
|---|---|---|
| SB-71 `GET /capacity` | 公开、无 token；池组装的文档 1 s 缓存、并发合并；`Cache-Control: public, max-age=1`；池不可达 503 `211` | `test_capacity.py::test_capacity_endpoint_public_and_consistent`（与池真值一致、不含访客标识） |
| SB-69 `POST /sessions/{sid}/park` | 会话 token；`{reason ∈ idle, hidden, user}`；202 PARKING、200 PARKED | `test_rest_park.py::test_park_resume_with_expired_token` |
| SB-72 `POST /sessions/{sid}/park-beacon` | 体内 token（sid 作用域），204 | `test_park_beacon_and_resume_queue` |
| SB-70 `POST /sessions/{sid}/resume` | 接受驻留期间过期的 token（签名与 sid 在本进程验，过期多久由池判）；201 换发 token、200 已活动、202 恢复票、410 506 | `test_park_resume_with_expired_token`、`test_park_beacon_and_resume_queue` |
| SB-76、SB-77（P1） | `POST /sessions/{sid}/pair-code`（会话 token）、`POST /sessions/{sid}/pair`（公开） | `test_commander_pairing` |
| 会话对象 | D2.1 字段（契约 1.1.0 期间只在驻留或内部档时输出）；时钟与事件端点在驻留时 409 515（镜像显示驻留时先向池核对） | `test_park_resume_with_expired_token`（草案 schema `session_d21`） |
| 创建 | `X-AWR-Identity` 原样交给池；无效 401 `302{why: identity_*}`、未启用 403 `115` | `test_internal_identity_create` |
| 原因码 | 515 至 519 的过渡登记（`REASONS.setdefault`，契约 1.2.0 后为空操作） | 同上（409 515 的 problem+json） |

### 1.3 SDK（`tools/sdk/awr_sandbox.py` 1.1.0，1018 行）

`_SessionHttp`（会话请求总带当前 token；515 时自动恢复一次后以新幂等键重试）、`Session.park()`、`Session.resume(wait, timeout_s, on_queue)`
（202 时复用排队长轮询与认领）、`rt()` 与 `stream()` 遇 4430 恢复后重连并重新订阅、`keepalive()` 不唤醒驻留会话、`Sandbox.capacity()`、
`create(identity_token=...)`；README 增补。验收：`test_sdk.py::test_sdk_park_auto_resume_and_capacity`、`test_rest_park.py::test_sdk_create_with_identity_token`（M17-AC-033）与单文件、3.9 语法检查。

### 1.4 前端

| 需求 | 实现 | 验收 |
|---|---|---|
| M17-FR-078 | `stores/capacity.ts`：`startBootCapacity`、`stopBootCapacity`、`refreshCapacity`（共享在途请求、单一超时）、`acquireCapacityPolling`、`updated`（`__AWR_BUILD__`，本机构建不比较）、`etaBucket`、`queueNeeded` | `apps/web/tests/m17/capacity.test.ts`（7 个）；M17-AC-032 |
| M17-FR-079 | `stores/sandboxSession.ts`：`parkSandbox`、`parkSandboxBeacon`、`resumeParked`、`onPowerState`、`markSandboxParked`、`restoreQueue`、恢复票自动认领（可见时）、驻留中 token 过期仍可恢复、`expiring.action`；`net/sandbox/client.ts`：`getCapacity`、`parkSession`、`beaconPark`、`resumeSession`、`setSandboxIdentityProvider` | `apps/web/tests/m17/park.test.ts`（10 个）；M17-AC-032 |
| `d2-WP-12-to-M17.md` | 存储键 `awr.sandbox.session.v1`、`awr.sandbox.queue.v1`（旧键可读）；`anotherTab`、`verifyHint`、`retryUntilMs`、`phaseSinceMs`、`switching`、`queue.ticketToken/world/template` | `park.test.ts::storage and UI fields`、原 `sandboxSession.test.ts`（13 个，视口用例改为 ADR-122 口径） |
| ADR-122 | 创建守卫改为宽 < 360 CSS px | `sandboxSession.test.ts` 视口用例 |
| M17-FR-081（P1） | `sandboxPairCode`、`joinByPairCode`、`createPairCode`、`redeemPairCode` | `park.test.ts::commander pairing` |

## 2. 设计要点

1. **驻留不依赖 M08 新接口也可用**。sim-core 现有两处能力足以完成驻留与恢复：`clock{op: checkpoint}` 强制写一代、停止时同步写完并镜像
   最新一代；崩溃重启时若环被复用且 `AWR_RESTART_COUNT > 0` 就按最新有效代恢复。池在驻留时记下环的尺寸、布局、epoch 与 segment，恢复前把
   该代放回 tmpfs 并按记录预建环，于是 sim-core 走既有的崩溃重启恢复路径。环尺寸或布局一旦变化，sim-core 会新建环、按会话配置冷启动，
   池在就绪后核对仿真时刻即可识别并报告 `resume_snapshot`。正式接口（`ctl/sim-core/checkpoint{to: disk}`、`AWR_RESUME_FROM`）已先行调用，
   M08 实现后自动启用。
2. **恢复核对期间不播放**。`AWR_SIM_AUTOPLAY=0` 的 sim-core 在播放前仿真时刻不变，核对因此不受访客接入影响；核对完成后若已有访客接入
   立即播放。
3. **容量与身份都在池里**。`awr.api` 的依赖边界（`check_py_imports`）不允许 sandbox-api 引入 `awr.sandbox`；把容量文档组装与身份令牌验签
   放进池，sandbox-api 保持薄转发，同时池本来就需要撤销表来给内部档降档，验签放在池里也只需一份密钥派生。
4. **兜底以"最近交互"而不是"有无连接"为准**。`last_input` 来自 presence（M11 上报后）与会话内调用；开着页面但长时间不操作的访客会在
   阈值 + 余量后驻留，伪造 presence 至多推迟到寿命与抢占规则。
5. **内部档不进入公开分母**。预留槽位上的内部会话不改变公开访客看到的"x / N_max"，也不影响公开 ETA；只有超出预留的内部会话占用公开槽位。
6. **孤儿回收不再误删其他池的会话目录**（D2 遗留问题，联调中发现）：多个池（测试栈、并行的 e2e）共用 `/dev/shm/awr` 时，D2 的
   `_gc_orphans` 会删除不属于本池的 `sb-*` 目录，导致正在运行的会话 `checkpoint write failed`、心跳停止、被判挂死后冷启动并丢失新增
   机体。现在只回收没有任何进程（环境 `AWR_RUN = <sid>`）在运行的目录（`test_gc_keeps_shm_dirs_of_other_pools`）。

## 3. 偏差

| # | 条款 | 偏差 | 理由与处置 |
|---|---|---|---|
| 1 | AWR-04 §15.5.4、§15.9.3 | 容量文档由池组装（`sys/session/stats` 回复的 `capacity`），sandbox-api 不读 show-api 状态文件；内部档身份令牌由池验签（sandbox-api 原样转交 `X-AWR-Identity`） | `awr.api` 只能依赖 `awr.runtime`、`awr.contracts`（AWR-03 §4.2 第 1 条，`check_py_imports`）；结果与设计相同。`d2-WP-04b-to-文档总编.md` 第 1 条 |
| 2 | M17 PRD §14.3.1 | `sandbox.eta_s` 为新到访客（位置 = 排队人数 + 1）的预计等待，不是队首 | 启动屏"预计约 t"需要的是新访客的等待。同上第 3 条 |
| 3 | 17 §17.20.2 | 池不可达时 SB-71 为 503 `211`，不是 200 + `hint = unavailable` | sandbox-api 不能组装文档（偏差 1）；前端对失败与超时同样显示 `capacity.unavailable`。同上第 2 条 |
| 4 | 17 §17.20.2 | 容量文档增加 `sandbox.internal_active` | "+1 内部"的显示需要占用数而不是配置的预留数。契约草案已含，`d2-WP-04b-to-M00.md` 第 1 条 |
| 5 | 17 §17.20.4 SB-70 | 201 的回复换发 token（与 SB-04 `created` 同形） | 驻留期间寿命冻结，原 token 可能过期；过期多久可接受由池按 `token_exp + park.retention_s` 判定 |
| 6 | M17-FR-072 | `lifetime`（寿命到期）与 `unclaimed`（未认领）仍直接结束 | FR-072 只列了 failed、user、maintenance、parked_expired，但寿命到期再驻留会无限延长；未认领的会话没有状态可保存 |
| 7 | 17 §17.20.4 `park_reason` | 身份撤销后超出公开寿命的驻留原因用 `preempted` | 枚举没有对应值 |
| 8 | AWR-04 §15.7.2 | 内部档"保持运行"的服务端 4 h 兜底未实现 | 由前端持续上报 presence 实现（寿命上限 8 h 仍有效），见 `d2-WP-04b-to-M15.md` |
| 9 | AWR-04 §15.15.4 | M08 的 `ctl/sim-core/checkpoint{to: disk}` 与 `AWR_RESUME_FROM` 未实现时使用过渡路径（§2 第 1 条） | 真实 sim-core 联调通过；`d2-WP-04b-to-M08.md` |
| 10 | 契约 1.1.0 | 会话对象的 D2.1 字段在契约 1.2.0 合入前只在驻留状态或内部档时输出；原因码 515 至 519 由 `rest/sandbox.py` 过渡登记 | 保持 1.1.0 schema 校验与 problem+json 处理器可用；合入后自动全量输出，登记变为空操作 |
| 11 | M11 hub（WP-03b） | 驻留时连接以 4430 关闭、领域路由 409 515 尚待 M11；在此之前驻留会话的连接保持打开，恢复后旧上下文按崩溃重启接上新 sim-core | 功能可用（REST 与 SDK 用例覆盖）；`d2-WP-04b-to-M11.md` 第 1、2 条 |

## 4. 测试结果

| 范围 | 命令 | 结果 |
|---|---|---|
| 本包 Python | `pytest -m "not perf" tests/sandbox` | 133 passed（新增 `test_park.py` 18、`test_park_pool.py` 8、`test_rest_park.py` 6、`test_capacity.py` 6、`test_identity_lib.py` 10、`test_real_park.py` 1，`test_sdk.py` +1，`test_broker.py` 2 个用例按 D2.1 口径改写） |
| 真实 sim-core | `pytest tests/sandbox/test_real_park.py -s` | passed：驻留 0.73 s、恢复到 READY 2.73 s、驻留时刻 t_sim 2.980 s、恢复后差 ≤ 0.1 s、机体仍在、epoch + 1 |
| 受影响的 M11 与契约用例 | `pytest -m "not perf" tests/rt/test_hub.py tests/rt/test_sandbox_api.py tests/rt/test_public_mode.py tests/rt/test_d2_topics.py tests/runtime/test_sandbox_runtime.py tests/contracts` | 403 passed（含 `test_lint_tools.py::test_repository_passes_py_imports`：REST 改为不依赖 `awr.sandbox` 后通过） |
| Vitest | `vitest run --project unit`（全部） | 156 文件、1266 passed、1 skipped；M17 4 文件 34 个 |
| TS 类型检查与 oxlint | `tsc -p apps/web/tsconfig.json --noEmit`；`oxlint --type-aware` | 通过 |
| 全量 pytest | `pytest -m "not perf" --continue-on-collection-errors` | 未在时限内完成，见 §4.2 |
| 真实栈 e2e（supervisor + 池 + 真实 sim-core） | `pytest tests/e2e/test_sandbox_sdk_e2e.py tests/e2e/test_sandbox_templates.py` | 109 passed、37 skipped、2 xfailed |
| lint | `make lint` | 见 §4.3 |

### 4.1 关键口径的实测

| 口径 | 结果 |
|---|---|
| 兜底驻留时刻（阈值 30 s + 余量 6 s，告知 5 s） | 告知在第 31 s、驻留在新交互后第 36 s（± 2 s 内） |
| 身份固定时延（300 ms ± 25 ms） | 正确与错误各 9 次的中位数在 0.27–0.36 s 内，差 ≤ 10 ms |
| 撤销对另一读者生效 | ≤ 1.1 s（撤销表按修改时间 ≤ 1 Hz 重载） |
| 容量文档大小 | N_max = 6、7 个可开世界、观看排队 100 时 ≤ 1 KiB |

### 4.2 全量 pytest

全量 `pytest -m "not perf" --continue-on-collection-errors` 在机器负载 10 左右（多个工作包并行，另有其他代理同时跑全量与 e2e 栈）下
约 40 min 只推进到 25%，按 WP-04 报告的做法中止并改跑受影响的集合。中止前的 1125 例：1070 passed、48 skipped、1 xfailed、6 failed，另有
1 个收集错误：

| 用例 | 归属 | 结论 |
|---|---|---|
| `tests/sim/test_lowpoly_fw.py`（收集错误：`_common` 模块名与 `tools/bench/ipc/_common.py` 冲突） | 他人（M21 或 bench） | 与本包无关 |
| `tests/agent/test_scoring.py::test_norms_by_profile`（数值期望 18.87，实得 19.38） | M14 | 单独运行仍失败，与本包无关 |
| `tests/e2e/test_sandbox_sdk_e2e.py` 5 例（107 `NO_VEHICLE`） | M16 的 e2e，覆盖本包 | 原因是多个池共用 `/dev/shm/awr` 时 D2 的孤儿回收误删其他池的会话目录（§2 第 6 条），已修复；修复后单独运行 5 passed、1 xfailed；`tests/e2e/test_sandbox_templates.py` 104 passed、37 skipped、1 xfailed |

### 4.3 `make lint`

本包文件全部通过（ruff 全仓 `python tools tests` 通过，`check_py_imports`、`check_py_callbacks` 通过，oxlint 通过）。最后一次运行中唯一的
失败项不属本包：`no-emoji` 报 `tests/e2e/test_sandbox_templates.py:78`（U+2600、U+27BF、U+FE0F；M16 的 e2e 目录，19:25 由并行工作包写入）。

## 5. 遗留

| 编号 | 事项 | 负责 | 说明 |
|---|---|---|---|
| WP04b-L1 | 驻留时 4430 关闭连接、领域路由 515、`last_input_unix_ns` 上报、`expiring.action` 与 `session.switched` 转发、R79/R80/R01 接身份库、配置段与环境 | M11（WP-03b） | `d2-WP-04b-to-M11.md` |
| WP04b-L2 | `ctl/sim-core/checkpoint{to: disk}`、`AWR_RESUME_FROM`、`RESUME_INCOMPATIBLE` | M08 | `d2-WP-04b-to-M08.md`；过渡路径已可用 |
| WP04b-L3 | 契约 1.2.0（容量、驻留草案，原因码，总线键，设计表 SB-69 至 72、76、77）、`py-imports.toml` 白名单 `awr.sandbox.identity` | M00 | `d2-WP-04b-to-M00.md`；契约合入后删除 `rest/sandbox.py` 的原因码过渡登记、`test_openapi_sandbox.py` 的 20 改为 26 |
| WP04b-L4 | 界面接入：启动屏容量行、站点已更新横条、节能覆盖层、排队卡片三种 kind、设置页身份与配对 | M15（WP-12c） | `d2-WP-04b-to-M15.md` |
| WP04b-L5 | D2-AC-45 的 cgroup 模拟（内存回落）、`tests/chaos/` 的池与 sandbox-api 重启、D2-AC-46 与 55 的节省与容量基准 | M16（WP-13b） | `d2-WP-04b-to-M16.md` |
| WP04b-L6 | `tests/fixtures/identity.py` 交付后 M17 用例改用之（`identitykit.py` 同接口） | M16、M17 | — |

## 6. 跨所有权请求

| 文件 | 要点 |
|---|---|
| `d2-WP-04b-to-M11.md` | hub 对 PARKING/PARKED 的处理（4430 + 518）、`ctx_for` 515、活动上报 `last_input_unix_ns`、`expiring.action`、`session.switched` 转发、身份库接入与 sandbox-api 不验签、`sandbox.park`/`sandbox.internal` 配置段、池的环境、show-api 状态文件、`reserve` 的 supervisor principal、前端 4430 接线 |
| `d2-WP-04b-to-M08.md` | checkpoint 落盘与 `AWR_RESUME_FROM` 正式接口；过渡路径依赖的行为请保持 |
| `d2-WP-04b-to-M00.md` | 契约 1.2.0 草案（`d2-WP-04b-drafts/sandbox/{capacity,park}.schema.json`）、原因码、总线键、枚举、设计表、白名单、部署确认 |
| `d2-WP-04b-to-M15.md` | 两个 store 的接口与接线；回复 `d2-WP-12-to-M17.md` |
| `d2-WP-04b-to-M16.md` | 替身、真实 sim-core 模板、测试配置、身份夹具、基准入口 |
| `d2-WP-04b-to-文档总编.md` | 11 条实现裁决 |

`d2-WP-16-to-M17.md` 的处置：第 3 条（`var/identity/`）与第 6 条（`RELEASE` 新字段）已按其实现（`identity.default_state_dir` 读
`AWR_IDENTITY_STATE_DIR` 与 `identity.state_dir`；`build_info` 优先读 `version`、`channel`、`commit`、`dirty`）；第 5 条容量接口的缓存头与
"不因访客而异"已满足；第 2 条（安装冒烟不计配额）暂不改（`d2-WP-04b-to-M00.md` 第 11 条）。

## 7. 文件清单

- 新增 Python：`python/awr/sandbox/{park,identity,capacity}.py`。
- 修改 Python：`python/awr/sandbox/{model,config,broker,pool,queue,spawner,sessionbus,tokens}.py`、`python/awr/api/rest/sandbox.py`。
- SDK：`tools/sdk/awr_sandbox.py`（1.1.0）、`tools/sdk/README.md`。
- 前端：`apps/web/src/stores/capacity.ts`（新）、`apps/web/src/stores/sandboxSession.ts`、`apps/web/src/net/sandbox/client.ts`。
- 测试：`tests/sandbox/{test_park,test_park_pool,test_rest_park,test_capacity,test_identity_lib,test_real_park}.py`（新）、
  `tests/sandbox/identitykit.py`（新）、`tests/sandbox/fixtures/contracts_draft/{capacity,park}.schema.json`（新）、
  `tests/sandbox/{sbharness,brokerkit,test_broker,test_sdk,test_real_simcore}.py`（改）；`apps/web/tests/m17/{capacity,park}.test.ts`（新）、
  `apps/web/tests/m17/sandboxSession.test.ts`（改）。
- 请求：`.cache/impl/requests/d2-WP-04b-to-{M11,M08,M00,M15,M16,文档总编}.md`、`.cache/impl/requests/d2-WP-04b-drafts/sandbox/`。
