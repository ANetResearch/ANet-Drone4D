# G5 深挖：后端进程模型与进程间通信——sim-core ↔ Gateway 的 1000 架状态共享、命令/事件可靠通道、进程监管与重启语义

> 单元：g05（补充深挖，对应 `00-index.md` §9 G5） ｜ 日期：2026-09-28
> 相关单元：r27（Gateway 与 zenoh）、r06（Open3D 持有 GIL）、r07（small_gicp 持有 GIL）、r25（规划必须单线程 BLAS）、r21（MAVSDK asyncio 线程池陷阱）、r22（SIH 生命周期）、n03（Crazyflow step pipeline）
> 读过的源码：`refs/backend/zenoh`（@`9fcd9cb`，1.10.1；`DEFAULT_CONFIG.json5` L611–622、L738–768）、zenoh-python 1.10.1 的类型桩（`zenoh/__init__.pyi` 中的 `Query.drop`、`Session.declare_querier`；`zenoh/handlers.pyi` 中的 `Callback(indirect=True)`；`zenoh/ext.pyi` 中标为 `_unstable` 的 `AdvancedPublisher`）、`refs/discovery/crazyflow/crazyflow/sim/{sim.py,data.py,pipeline.py}`（`staged_cmd → cmd` 语义、`step(n_steps)`）、`.cache/research/r27/{gw_proto.py,z_bench.py}`、`.cache/research/r20/mock_px4lite.py`
>
> 实测环境：Xeon E5-2603 v4 @1.7 GHz，8 核、无超线程、无睿频；Python 3.12.3；numpy 2.5.3；eclipse-zenoh 1.10.1；uvloop 0.22.1；open3d 0.20.0。压测时机器平均负载 0.4–1.6，结果偏保守。
>
> 产物目录 `.cache/research/g05/`：
>
> | 脚本 | 内容 | 输出 |
> |---|---|---|
> | `statering.py` | **StateRing 原型**：mmap(tmpfs) 单写多读 seqlock 环。代码按产品质量写，可直接迁到 `packages/runtime/awr_runtime/statering.py` | — |
> | `bench_state.py` | 状态平面：1000 架 Full64+Lite32（96 KB/帧）从 sim-core 进程到 Gateway 进程。对比 6 种传输，发送端带真实 PX4-lite 负载与不带负载两组 | `bench_state.out` |
> | `bench_cmd.py` | 命令与事件平面：Gateway(asyncio) → sim-core（1000 架，100 Hz），命令在步边界锁存；事件带 seq 做丢失与乱序检测；zenoh 与 UDS 对比 | `bench_cmd.out` |
> | `bench_gil.py` | 把 Open3D RaycastingScene（100 万三角形）放进 Gateway 线程与放进独立进程，对比事件循环的延迟 | `bench_gil.out` |
> | `supervisor_proto.py` | 监管器原型与混沌测试：kill -9 和挂死注入 → 检出 → 退避 → 从 checkpoint 恢复 → epoch+1 | `supervisor_proto.out` |
> | `probe_misc.py`、`probe_zidle.py`、`probe_mesh.py`、`probe_zshm.py`、`probe_pool.py` | liveliness 能否检出挂死；Python 3.12 `shared_memory` 的 unlink 缺陷；writer 被 kill 后能否读到最后一帧；zenoh 空闲 CPU 与事件 put 开销；peer 网状在汇合点死亡后是否存活；zenoh SHM 池在 /dev/shm 的占用；spawn 进程池的 BLAS 环境变量与崩溃行为；冷启动耗时 | `probe_misc.out` |

---

## 0. 结论（先读）

1. **从 V0.1 起，sim-core 与 api-gateway 就是两个独立进程。** r27 §3.13 提出的"MVP 仿真与 Gateway 同进程"只适用于 r21 的简化模型（1000 架每步 0.36 ms）。改用已定的 PX4-lite 级联后，本机实测 1000 架**每步 3.56 ms**，加上遥测打包后 p50 为 4.2 ms、p99 为 7.8 ms。以 100 Hz 运行要占 45% 的核，250 Hz 要占 89%。把它塞进 Gateway 的事件循环，每个 tick 都会被卡住一次，Gateway 能服务的客户端数也会腰斩。
   - 所有持有 GIL 的重计算各自放在专用进程里：
     - `geo-worker`：Open3D RaycastingScene；
     - `job-worker`：small_gicp、切片、ingest；
     - `plan-pool`：spawn 进程池，`OMP_NUM_THREADS=1`，归 sim-core 所有；
     - `px4-bridge-k`：MAVSDK，每个进程最多 16 架。
   - 所有进程统一由 `awr-supervisor` 监管。
2. **IPC 分为三个平面，按数据性质选传输**：
   - **状态平面**：高频、有损、取最新值，走 **StateRing**，即 tmpfs 上 mmap 的单写多读 seqlock 环，每个生产者一个文件。
   - **控制与事件平面**：低频、必须可靠，**从 V0.1 起就用 zenoh**（peer 模式，本机 key 空间），包括 query/reply 命令、带 seq 的事件、liveliness。
   - **大块数据平面**：走文件或 HTTP 加版本通知；V0.2 起 LiDAR 走 zenoh SHM。
   - 热路径（1000 架状态）**不上 zenoh**；zenoh 只承载每秒几十条的消息。
3. **状态平面实测**（1000 架，每帧 96 KB，发送端跑真实 PX4-lite @100 Hz）：
   - StateRing：Gateway 侧 CPU **1.5%**；Gateway 每个 tick 取到的数据年龄 p50 4.7 ms、p99 11.4 ms，已经等于"100 Hz 源被 60 Hz tick 采样"的理论下限；发送端 publish 耗时 p50 77 µs。
   - zenoh（同样数据量）：Gateway CPU 3.2–4.5%，发送端 publish p50 117–148 µs；年龄与 StateRing 相当。
   - UDS 流：Gateway CPU 2.7%。
   - **50 Hz 与 100 Hz 都满足"≥50 Hz"的要求。** 各方案的决定性差异不在速度，而在语义（见第 5 条和 §3.7）。
4. **命令平面实测**：命令准入的 RTT p50 约 9.4–10.2 ms，p99 13–22 ms，两种传输一样。时延主要来自"命令在步边界锁存"加上 GIL：接收线程要等 numpy 步进让出 GIL 才能入队，所以约等于 1 个步长。传输本身只贡献约 0.5 ms。
   - 在 50 条命令/s 加 570 条事件/s 的压测下，事件**零丢失、零乱序**。
   - zenoh 在这个压测下让 Gateway 多占约 5–8% 的核。按真实负载折算（≤5 条命令/s，事件按步合批后 ≤50 条消息/s）不到 1%；空闲会话只占 **0.13%**。
   - 选 zenoh 而不是自研 UDS 的理由：
     - 多进程之间按 key 路由；
     - query/reply 自带超时和 finalize；
     - liveliness 提供在线状态；
     - peer 网状在汇合点死亡后仍能直连（已实测）；
     - V0.5 跨主机和 rmw_zenoh 可以直接复用同一套代码。
5. **铁律：sim-core 永不阻塞在任何消费者上。** UDS 压测第一次运行就复现了死锁：Gateway 事件循环被一个阻塞调用卡住，sim-core 的 `sendall` 随之阻塞，整个仿真停摆。落实为五条规则：
   - 状态走 StateRing，有损读者永远不会反压写者；
   - sim-core 发出的所有 zenoh 流量都用 `CongestionControl.DROP`（最多等 1 ms）；
   - 事件的可靠性靠 seq 加 replay queryable，而不是靠 BLOCK；
   - 命令的可靠性靠 cid 幂等加超时重试；
   - 回调函数只负责入队。
6. **存活检测 = waitpid + 主循环心跳，liveliness 不能代替。** 实测：子进程主线程挂死 5 s（lease 设为 2 s），zenoh liveliness 始终**没有**报告 DELETE，因为 lease 由 Rust 线程维持。
   - 因此每个进程的主循环都要写心跳：sim-core 写在 StateRing 头部，其他进程写各自的 `hb.<name>` 文件。
   - supervisor 按阈值（sim-core 为 2 s）判定挂死，先发 SIGTERM，等 1 s 后发 SIGKILL，然后重启。
7. **重启语义：checkpoint（每 1 s 仿真时间一次）加 epoch。** sim-core 重启后复用原来的 StateRing 文件（读者不需要重新 mmap），加载最新 checkpoint，epoch 加 1。Gateway 看到 epoch 变化后下发 TIME 和 backfill，客户端丢弃旧 epoch 的帧，这正好复用了 r27 R15 为 seek 设计的机制。
   - 实测 kill -9 后，新 epoch 的首帧在 **789 ms** 后到达（退避 0.5 s 加冷启动 0.29 s），仿真时间最多回退 1 s。
   - 实测挂死从注入到检出为 2.13 s，恢复出帧共 **3.4 s**。
   - 5 次重启发生在 60 s 内时熔断为 FAILED。
8. **命令语义**：
   - 准入回复最迟 1 个步长给出（accepted/rejected）；完成情况走事件（succeeded/failed/…）。
   - **cid 幂等表放在 sim-core**（保留 60 s、最多 4096 条，写入 checkpoint）。这样 Gateway 重启、客户端用同一个 cid 重发时不会重复执行。
   - sim-core 回滚后，t_accept 晚于 checkpoint 的命令一律报 `SIM_ROLLBACK`。
   - 命令队列满或超时时报 `SIM_UNAVAILABLE`，可以用同一个 cid 重试。
9. **两个平台坑已实测确认**：
   - Python 3.12 的 `multiprocessing.shared_memory`：一个只做 attach 的进程退出时，resource_tracker 会把段 unlink 掉（`track=False` 要到 3.13 才有）。**所以不用它，改用 `/dev/shm/awr/<run>/` 下的普通文件加 mmap。**
   - zenoh 的 SHM transport optimization 会给每个发布会话在 /dev/shm 分配一个 **16 MiB** 的池，而 Docker 默认 /dev/shm 只有 64 MiB。**所以 V0.1 所有进程关闭 zenoh SHM**（控制消息都很小），容器设置 `shm_size: 1g`。
10. **对既有文档的修订**（详见 §11）：
    - 00-index §3.10"Planning Service 放在仿真内核进程内"改为"归 sim-core 调度，在 spawn 进程池内执行"；
    - §3.11"MVP 在进程内"改为"V0.1 起双进程加 StateRing 加 zenoh"；
    - r27 §3.15 的 `uav/{id}/cmd/**` 每机一个 queryable，改为按生产者路由 `ctl/{producer}/cmd`，由 roster 映射机体到生产者；WS 对外命名不变；
    - r06 §6 提到的 `multiprocessing.Queue` 与 ZeroMQ 方案作废。

---

## 1. 约束与证据

| # | 约束 | 数值与出处 | 设计含义 |
|---|---|---|---|
| C1 | Gateway 是单核 asyncio，是客户端扩展性的瓶颈 | 10 个客户端时 CPU 34%，50 个客户端时饱和（r27 §3.1.5） | 不能在 Gateway 里做任何 >1 ms 的同步计算 |
| C2 | FleetSim 的 CPU 成本 | 本机 PX4-lite：1000 架每步 **3.56 ms**，200 架 1.69 ms；加遥测打包后 p50 4.2 ms、p99 7.8 ms（`bench_state.out`） | 1000 架 @100 Hz 占 45% 的核；@250 Hz 占 89%，不可行（交 G8 决定步长） |
| C3 | Open3D `RaycastingScene` 在整个调用期间持有 GIL | r06 §2.1。本单元实测：2 万条射线调用 7.1 ms，Gateway 循环延迟 p99 从 5.1 升到 7.9 ms、max 15.5 ms；**10 万条射线调用 18.4 ms，p99 20 ms、max 31 ms，10% 的 tick 超过 8 ms**。放进独立进程后 p99 5.9 ms，与基线持平（`bench_gil.out`） | 放进 geo-worker 进程；**禁止 `asyncio.to_thread` 包 Open3D** |
| C4 | small_gicp 不释放 GIL，Python 默认 `num_threads=1` | r07 §6：两个线程并行和串行一样慢（1.81 s 对 1.89 s） | 放进 job-worker 进程；心跳阈值要大于单次 C 调用的最长时间 |
| C5 | 规划器的 BLAS 必须单线程 | r25：多线程 BLAS 下慢 40 倍（1024 ms 对 23 ms）；单次 L-BFGS-B 25–78 ms | 放进 spawn 进程池，spawn 之前设置 `OMP_NUM_THREADS=1`（本单元已验证子进程能继承） |
| C6 | MAVSDK asyncio 的线程池会饥饿；订阅内部是无界队列 | r21 R3/R4：16 架失联时，健康机的调用被卡 2001 ms | 放进 px4-bridge 进程，每个进程 ≤16 架，`set_default_executor(ThreadPoolExecutor(8·N+16))` |
| C7 | 状态与命令的频率 | 状态 ≥50 Hz（WS 最高 60 Hz tick）；UI 命令 ≤5 条/s；遥操作 setpoint 20–50 Hz；事件突发可达数百条/s | 高频有损数据与低频可靠数据分成两个平面 |
| C8 | 进程崩溃不能拖垮其他进程；挂死要能检出 | 本单元实测：liveliness 检不出挂死（`probe_misc.out` [1]） | 所有进程统一心跳；由 supervisor 裁决 |
| C9 | 时间权威 | r27：服务端仿真时钟是权威；seek 时 epoch 加 1 | 崩溃恢复复用 epoch 机制 |
| C10 | 部署 | 单机 Docker（MVP），V0.5 起跨主机（GPU 节点、P600 机载端） | 本机用 StateRing；跨主机用同一个接口的 zenoh 实现 |

---

## 2. 进程拓扑

### 2.1 拓扑图（V0.2 形态；V0.1 只有 supervisor、api、sim-core 三个进程）

```text
                              ┌─────────────────────────── awr-supervisor (asyncio, PID1 via tini) ───────────────────────────┐
                              │ spawn/exec · waitpid · heartbeat 巡检(5 Hz) · 退避重启 · 熔断 · 日志收集 · zenoh 汇合点 :7447 │
                              └──────┬──────────────┬──────────────┬───────────────┬──────────────┬──────────────┬───────────┘
                                     │              │              │               │              │              │
   Browser ◄══ WS awr.rt.v1 ══► ┌────▼─────┐   ┌────▼─────┐   ┌────▼─────┐   ┌─────▼────┐   ┌─────▼────┐   ┌─────▼─────┐
   Browser ◄── HTTP Range ────► │   api    │   │ sim-core │   │geo-worker│   │job-worker│   │px4-bridge│   │ recorder  │
                                │ FastAPI  │   │ 固定步长  │   │ Open3D   │   │small_gicp│   │  -k      │   │ MCAP      │
                                │ Gateway  │   │ FleetSim │   │ Embree   │   │ tiler    │   │ MAVSDK   │   │ (V0.2)    │
                                │ 1 核     │   │ 1 核     │   │ 2–4 线程 │   │ nice 10  │   │ ≤16 架   │   │           │
                                └─┬──▲──▲──┘   └┬──┬──┬──┬┘   └────▲─────┘   └────▲─────┘   └──┬───▲───┘   └─▲───▲─────┘
                                  │  │  │       │  │  │  └ spawn ─► plan-pool ×W (OMP=1, 进程池归 sim-core 所有)
          StateRing(mmap) 读 ─────┘  │  │       │  │  └─ StateRing 写：/dev/shm/awr/<run>/state.sim-core ──┬──────────┘
          (sim-core / px4-bridge-k)  │  │       │  │                                                     └─ recorder 读
                                     │  │       │  └── zenoh: evt/sim-core/*（DROP + seq + replay）
          zenoh: ctl/*/cmd（query）──┘  │       └───── zenoh: svc/geo/lidar（query，10 Hz 批量）
          zenoh: evt/**、proc/**/alive ─┘
          PX4 SIH 容器 ×N ◄─ MAVLink UDP ─► px4-bridge-k ─► StateRing state.px4-bridge-k
```

### 2.2 进程清单

| 进程 | 入口 | 运行模型 | 线程与 CPU | 频率 | 持有的状态 | 持 GIL 的库 | 崩溃影响 | 版本 |
|---|---|---|---|---|---|---|---|---|
| `awr-supervisor` | `python -m awr_runtime.supervisor -c configs/runtime.yaml` | asyncio | 很少 | 巡检 5 Hz | 进程表、重启计数、stderr 尾部 | 无 | 全部子进程收到 PDEATHSIG 退出，由容器重启 | V0.1 |
| `api` | `uvicorn apps.api.main:app --loop uvloop --ws websockets --workers 1` | asyncio 单线程 + zenoh 回调线程 | 1 核（钉 core0） | tick 60 Hz | WS 会话、订阅、在途命令表、全局 EventRing、全局 epoch | **禁止** | 客户端断线重连；仿真不受影响 | V0.1 |
| `sim-core` | `python -m sim.runtime` | **同步固定步长循环**（不用 asyncio）+ zenoh 回调线程（只入队） | 1 核（钉 core1），BLAS=1 | 物理 100–250 Hz，发布 50–100 Hz | FleetSim SoA、任务、Control Lease、Safety FSM、SimClock、cid 幂等表、事件环 | numpy（小数组，可以接受） | 状态冻结后从 checkpoint 恢复，epoch+1 | V0.1 |
| `plan-pool` ×W | sim-core 内部的 `ProcessPoolExecutor(spawn)` | 同步 | 每个 worker 1 核，`OMP_NUM_THREADS=1` | 按需 | 只读 ESDF（mmap） | scipy/numba | `BrokenProcessPool`（7 ms 感知），重建进程池 | 规划上线时 |
| `geo-worker` | `python -m world.geometry.worker --threads 3` | 同步主循环 + zenoh queryable（只入队） | Embree 用 2–4 线程 | LiDAR 10 Hz 批量；查询按需 | Embree 场景（按 NY 2 m 估约 0.5 GB） | **Open3D** | 查询返回 `SERVICE_UNAVAILABLE`；LiDAR 降级；场景重建约 5 s | V0.2 |
| `job-worker` | `python -m awr_jobs.worker` | 同步，一次一个任务 | `num_threads` 显式传入，nice 10 | 按需 | SQLite 任务队列 | **small_gicp**、Open3D、PDAL | 任务标记为 failed(resumable) | V0.1（ingest/切片）/ V0.5（fusion） |
| `px4-bridge-k` | `python -m sim.backends.px4_bridge --slot k` | asyncio（MAVSDK v4） | executor 线程数 8·N+16 | 融合 30–50 Hz | 每机 mailbox、生命周期 | libmavsdk（释放 GIL） | 名下机体全部 LOST，PX4 按自身 failsafe 处置 | V0.2 |
| `recorder` | `python -m apps.recorder` | 同步 drain 循环 | 1 线程 | 20–50 Hz 排空 | MCAP 写入器 | 无（zstd 释放 GIL） | 录制出现缺口，记 `recorder.gap` | V0.2 |
| `static`（可选） | Caddy，或 `uvicorn --workers 2` 只挂 StaticFiles | — | — | — | — | — | 点云瓦片暂停加载 | V0.2（客户端 >5 个时） |

说明：
- `api` 里的 Starlette StaticFiles（HTTP Range）在 MVP 阶段可以与 Gateway 同进程，因为只有 1–5 个客户端。静态文件读取走 anyio 线程池，I/O 期间释放 GIL，但每个请求仍有 Python 开销。客户端超过 5 个，或 loop-lag p99 超过 10 ms 时，把 `/worlds/**` 拆给 `static` 进程。
- PX4 SIH 容器由 r22 的 orchestrator 管理。它持有 docker.sock，也是一个独立的受限进程，受 supervisor 监管。

### 2.3 版本演进

| 版本 | 进程集合 | 状态平面 | 控制与事件平面 |
|---|---|---|---|
| V0.1 | supervisor、api、sim-core（另有离线 ingest/切片 CLI，或 job-worker） | StateRing（sim-core 一个生产者） | zenoh（peer，本机，SHM 关闭） |
| V0.2 | 加 geo-worker、px4-bridge-k、recorder、orchestrator | StateRing，每个生产者一个文件；Gateway 负责合并 | 同上；geo-worker 与 sim-core 之间开启 SHM（池 8 MiB） |
| V0.3–V0.4 | 加 plan-pool（规划服务）；HIL 桥（lockstep 必须放在 sim-core 的步进里，单独设计） | 同上 | 同上 |
| V0.5 | GPU 节点（重建与传感器）、P600 机载 rosbridge 适配器 | 远端生产者改用 `ZenohStateBus`（同一套 reader 接口） | zenoh 跨主机（lease 3 s），rmw_zenoh |
| V1.0 | sim-core 分片（K 个进程按空间划分）、Gateway 多 worker（SO_REUSEPORT，各自 attach StateRing）、Rust 数据面网关（可选） | 每个分片一个 StateRing | 同上 |

### 2.4 CPU 分配（`configs/runtime.yaml` 的 `cpus` 字段；默认关闭，压测和演示时打开）

| 核 | 8 核本机 | 4 核笔记本 |
|---|---|---|
| 0 | api | api |
| 1 | sim-core | sim-core |
| 2–3 | plan-pool ×2 | plan-pool ×1、geo-worker、job-worker 共用 core2–3 |
| 4–6 | geo-worker（Embree 3 线程） | — |
| 7 | job-worker、recorder、supervisor | — |
| PX4 SIH | 每机约 0.22 核（r22），不钉核；超过 8 架时降低 SIH 数量或另开节点 | 最多 4 架 |

---

## 3. IPC 设计

### 3.1 三平面总览

| 平面 | 数据 | 语义 | 传输 | 可靠性机制 | 反压 |
|---|---|---|---|---|---|
| 状态 | 整群 Full64/Lite32、TIME 源数据、心跳 | 取最新值，允许丢帧 | **StateRing**（本机）/ `ZenohStateBus`（跨主机，V0.5） | 帧序号单调递增；读到撕裂帧就重读；读者落后 K 帧时计入 overrun | **无**（有损读者不反压）；只有批处理模式下的无损读者可以反压 |
| 控制 | 命令、时钟控制、Lease、查询（raycast/los/plan） | 请求/应答，at-most-once 执行 | zenoh query/queryable | cid 幂等、1 s 准入超时、同一 cid 重试 2 次 | DROP（最多等 1 ms），失败靠重试 |
| 控制（流式） | 遥操作 setpoint | 取最新值，200 ms 截止 | zenoh pub/sub（REAL_TIME、express、DROP） | 500 ms 看门狗切 Hold（r27 §3.12） | DROP |
| 事件 | cmd.*、safety.*、mission.*、sim.*、proc.*、job.* | 有序、可恢复 | zenoh pub/sub（DROP）+ `_replay` queryable | 每个 (producer, epoch) 有独立的 seq；检测到缺口就用 replay 补拉；环长 4096 | DROP；生产者永不阻塞 |
| 低频状态 | state_ext、safety 摘要（2 Hz，全机合批） | 取最新值 | zenoh pub/sub（DATA_LOW、DROP） | 下一帧覆盖 | DROP |
| 大块数据 | LiDAR 扫描、风场网格、瓦片、MCAP | 按版本 | 文件或 HTTP 加事件通知；V0.2 起 LiDAR 走 zenoh SHM reply | 版本号 | — |

### 3.2 状态平面：StateRing

**为什么环里放线格式（Full64/Lite32，AoS），而不放 sim-core 内部的 SoA**
- Gateway 的核是客户端扩展性的瓶颈。遥测打包（NED→ENU、四元数重排、Lite32 量化）在 1000 架时每次约 0.6 ms，只在发布步执行一次。
- 这样 Gateway、recorder 以及 V1.0 的多个 Gateway worker 都能直接复用同一份字节，逐架切片后原样装进 WS 记录（r27 §3.13 的"编码一次"）。

**文件与布局**：`/dev/shm/awr/<run>/state.<producer>`（macOS 或没有 /dev/shm 时，回退到 `$AWR_RUN_DIR`）。所有字段小端、自然对齐。

```text
[0,256)    RingHeader
           0 magic u32 "AWR1" | 4 version u16 | 6 flags u16 | 8 slot_count u32 | 12 slot_bytes u32 | 16 capacity u32
          20 layout_id u32 (layouts.json 哈希) | 24 writer_pid u32 | 28 epoch u32 | 32 head u64 (最后提交的帧号)
          40 heartbeat_ns i64 (CLOCK_MONOTONIC，主循环每次迭代都写，暂停时也写) | 48 t_sim_ns i64 | 56 rate_milli i32
          60 clock_state u8 (0 STOPPED/1 PLAYING/2 PAUSED/3 STEPPING) | 64 roster_version u32 | 72 created_ns i64
          80 step_seq u64 | 88 step_budget_us u32 | 92 step_p99_us u32 (sim-core 自报，供 HUD 与 supervisor 使用) | 96–255 保留
[256,512)  ReaderCursor ×8 (32 B)：pid u32 | mode u32 (0 FREE/1 LOSSY/2 LOSSLESS) | cursor u64 | heartbeat_ns i64 | pad
[512, …)   Slot ×K，每个 slot_bytes = ceil64(64 + cap×96)
           SlotHeader 64 B：lock u64 (seqlock) | frame_seq u64 | t_sim_ns i64 | t_pub_ns i64 | n_rows u32 | epoch u32
                           | roster_version u32 | flags u32 | full_off u32 | full_len u32 | lite_off u32 | lite_len u32
           Full64[cap] | Lite32[cap]
默认 cap=1024、K=8：每个 slot 98,368 B，文件约 0.75 MiB
```

原型中读者游标是 4 个；产品版改为 8 个（header 区扩到 512 B）。

**协议（seqlock，x86-64 TSO 下正确）**：

```python
# 写者（单一进程；prototype: .cache/research/g05/statering.py::publish）
fs = hdr.head + 1; i = fs % K
wait_lossless(fs)                      # 只在批处理模式下有意义，≤250 ms 后把卡住的读者降级为 LOSSY
lock[i] = 2*fs - 1                     # 奇数：正在写（numpy 8 字节对齐单次存储）
full[i][:n] = full_src; lite[i][:n] = lite_src
slot_hdr[i] = (fs, t_sim_ns, now_mono_ns, n, epoch, roster_version, …)
lock[i] = 2*fs                         # 偶数：已提交
hdr.head = fs

# 读者（Gateway tick / recorder）
h = hdr.head;  if h == last: return None
i = h % K; s1 = lock[i];  if s1 != 2*h: retry(≤3)           # 被写者追上
copy slot_hdr + full + lite (bytes)                          # 96 KB 拷贝约 10–20 µs
if lock[i] != s1: retry                                      # 撕裂
```

- 正确性依据：x86-64 上的 store 按程序顺序对其他核可见。写者先完整写完奇数锁字，再写 payload。只要 payload 有任何字节已被改写，读者第二次读锁字必然看到一个不等于 2h 的值。锁字本身即使撕裂也不会恰好等于 2h。
- **aarch64**（Jetson 或 Apple 上开发）不保证这一点：要么在 publish/read 里加一个很小的 cffi `__atomic_thread_fence`，要么改用 `ZenohStateBus`。
- **不用 `multiprocessing.shared_memory`**：Python 3.12.3 实测，只做 attach 的进程退出后，段被 resource_tracker unlink 了（`probe_misc.out` [2]）。普通文件加 mmap 还有三个好处：名字可读；`ls /dev/shm/awr` 就能排查；supervisor 统一清理 run 目录。

**API（产品版签名，落在 `packages/runtime/awr_runtime/statering.py`）**：

```python
class Frame(NamedTuple):
    frame_seq: int; t_sim_ns: int; t_pub_ns: int; epoch: int; roster_version: int; n_rows: int
    full: bytes; lite: bytes                     # 已拷贝，可以长期持有

class StateRing:
    @classmethod
    def create(cls, path: Path, *, capacity=1024, slots=8, layout_id: int, epoch=1) -> "StateRing"   # tmp 文件 + os.replace 原子发布
    @classmethod
    def open_or_create(cls, path: Path, *, capacity, slots, layout_id) -> tuple["StateRing", bool]    # 写者重启路径：兼容就复用，否则替换
    @classmethod
    def attach(cls, path: Path) -> "StateRing"                                                          # 读者
    # 写者
    def heartbeat(self, t_sim_ns: int, clock_state: int, rate_milli: int = 1000) -> None
    def publish(self, full: np.ndarray, lite: np.ndarray, t_sim_ns: int, roster_version: int, flags: int = 0) -> int
    def set_epoch(self, epoch: int) -> None
    # 读者
    def register(self, mode: Literal[1, 2] = LOSSY) -> int
    def read_latest(self, last_seq: int = 0) -> Frame | None
    def drain(self) -> tuple[list[Frame], int]                          # (帧列表, overrun 数)，recorder 使用
    def writer_age_ms(self) -> float
    def identity(self) -> tuple[int, int, int]                          # (st_ino, writer_pid, epoch)，读者每 1 s 检查，变化就重新 attach 或切换 epoch
```

**多生产者合并**：
- 每个生产者（`sim-core`、`px4-bridge-0..k`，V1.0 还有 `sim-core-0..k`）各写一个文件。
- 机体数字 id（Full64 的 `id` 字段是 u16）由 supervisor 按区段分配，通过环境变量 `AWR_ID_BASE`、`AWR_ID_COUNT` 传入，例如 sim-core 用 0–1023，px4-bridge-k 用 1024+16k 起的 16 个。
- 名字、生产者和 id 的映射由 roster 提供：`ctl/{producer}/roster` queryable，加上 `roster.changed` 事件；slot 头带 `roster_version`，不一致时 Gateway 异步重拉。
- Gateway 的 `swarm/state` payload = 各生产者的 Lite32 字节直接拼接（ids 互不相交）。

**参数**：

| 参数 | 默认 | 依据 |
|---|---|---|
| `slots` K | 8 | 发布 100 Hz 时覆盖 80 ms 历史；recorder 以 ≥20 Hz 轮询即可无损排空 |
| `capacity` | 1024（每个生产者） | 1000 架目标；px4-bridge 用 16 |
| `publish_hz` | `min(100, physics_hz)`，1000 架时 100 | 实测 100 Hz：Gateway 每个 tick 都有新帧；50 Hz 时只有 83% 的 tick 有新帧，年龄 p50 9.8 ms、p99 21 ms |
| 读者轮询 | Gateway 60 Hz tick；recorder 20–50 Hz；supervisor 5 Hz 只读头部 | — |
| 心跳阈值 | Gateway：>250 ms 标记 `stalled`，>2 s 或 pid 已死标记 `down`；supervisor：>2 s 判挂死 | 暂停时主循环仍以 20 Hz 空转写心跳 |

**实测：1000 架，每帧 96 KB，sim-core 跑真实 PX4-lite @100 Hz，Gateway 60 Hz tick（`bench_state.out`）**

| 传输 | Gateway CPU | tick 时数据年龄 p50 / p95 / p99 / max（ms） | 推送到达 p50 / p99（ms） | 写端 publish p50 / p99（µs） | 写端 CPU（含物理） | 写端 CPU（无物理，仅传输） | Gateway CPU（无物理） |
|---|---|---|---|---|---|---|---|
| **StateRing** | **1.5%** | **4.73 / 9.53 / 11.41 / 14.96** | —（轮询） | **77 / 136** | 45.0% | **1.5%** | **1.4%** |
| zenoh RingChannel(1)（tcp + 自动 SHM） | 3.2% | 6.17 / 9.97 / 11.96 / 20.43 | — | 117 / 405 | 45.4% | 3.7% | 3.4% |
| zenoh 回调 | 4.5% | 5.10 / 11.11 / 11.88 / 15.80 | 0.55 / 4.16 | 127 / 436 | 47.1% | 3.6% | 4.4% |
| zenoh 关 SHM | 4.0% | 4.67 / 10.75 / 11.51 / 22.54 | — | 131 / 338 | 48.0% | 4.9% | 4.6% |
| zenoh unixsock | 4.1% | 4.83 / 10.84 / 12.41 / 17.84 | — | 148 / 363 | 47.2% | 3.2% | 3.4% |
| UDS 流（长度前缀） | 2.7% | 5.39 / 9.52 / 10.51 / 13.87 | 0.30 / 1.88 | 137 / 448 | 46.4% | 1.9% | 2.5% |
| StateRing @50 Hz | 1.6% | 9.75 / 18.55 / 20.98 / 24.70 | — | 87 / 140 | 26.0% | — | — |
| zenoh @50 Hz | 2.3% | 10.19 / 19.36 / 21.14 / 24.34 | — | 164 / 383 | 25.6% | — | — |

- 年龄的理论下限：100 Hz 源被 60 Hz tick 采样，年龄服从 U(0, 10 ms)，p50 5 ms、p99 约 9.9 ms。StateRing 已经达到下限；其他传输在下限之上加了 0.3–4 ms 的传递抖动。
- 单独的热循环测试：publish 31 µs，read_latest 33 µs（包括 96 KB 拷贝和 numpy 标量字段开销）。

### 3.3 控制平面：命令

**key 与 QoS**（zenoh 会话配置 `namespace: "awr/{world}/{run}"`，下面都是相对 key）：

| key | 类型 | 服务方 | QoS |
|---|---|---|---|
| `ctl/{producer}/cmd` | queryable | sim-core、px4-bridge-k | 调用方：`priority=INTERACTIVE_HIGH, congestion_control=DROP, express=True, timeout=1.0`；reply 的 QoS 随 query（zenoh-python 1.10：reply 的 QoS 参数已废弃，自动跟随 query） |
| `ctl/{producer}/setpoint` | pub/sub | 同上 | `REAL_TIME, DROP, express` |
| `ctl/sim-core/clock` | queryable | sim-core | 同 cmd（play/pause/speed/step/checkpoint/fork） |
| `ctl/sim-core/lease` | queryable | sim-core | 同 cmd（acquire/renew/release；返回 HMAC 签名的 lease token，各生产者本地校验，不需要往返） |
| `ctl/{producer}/roster` | queryable | 各生产者 | INTERACTIVE_LOW |

**为什么按生产者路由，而不是 r27 §3.15 的每机一个 `uav/{id}/cmd/**`**：
- sim-core 名下有 1000 架。要么声明 1000 个 queryable，要么声明通配 queryable；通配会与 px4-bridge 名下的机体冲突，两边都会回复。
- 改为由 roster 决定机体属于哪个生产者，Gateway 直接查表路由。
- WS 对外命名保持 r27 §3.12 的 `uav/{id}/cmd/{op}`，由 Gateway 翻译。V1.0 如果 ANet agent 直接连总线，再由一个 `agent-gateway` 暴露每机命名。

**消息**（msgpack；schema 放在 `packages/contracts/bus/command.schema.json`）：

```python
Command  = {"cid": "uuid7", "op": "goto", "uav": "p600-01" | ["p600-01", …] | "*",
            "args": {...}, "lease": "<token>", "issued_by": "ui:<session>", "t_wall_ns": int, "epoch_seen": int}
Admission = {"cid": str, "status": "accepted" | "rejected" | "duplicate", "code": int, "reason": str | None,
             "t_sim_ns": int, "epoch": int, "per_uav": {"ok": bitmask, "reasons": [...]} | None}   # 批量命令逐机给结果
# 完成与进度不在 reply 里，走事件：evt/{producer}/cmd  kind ∈ {cmd.progress, cmd.succeeded, cmd.failed, cmd.canceled, cmd.timeout}
```

**sim-core 端：步边界锁存（沿用 Crazyflow 的 `staged_cmd → cmd`；`sim/data.py` 的注释写的是"The most recent control input gets staged here until the next controller tick and is then committed"）**：

```python
# queryable 回调运行在 zenoh-python 的回调线程里（Callback 默认 indirect=True），只做入队
bus.serve("ctl/sim-core/cmd", lambda q: inbox.put(("cmd", q)))

def dispatch_cmd(q):                                   # 主循环在步顶调用
    cmd = msgpack.unpackb(q.payload)
    if (prev := cid_cache.get(cmd["cid"])) is not None:
        reply(q, {**prev, "status": "duplicate"})       # Gateway 重启或客户端重发时不会重复执行
    else:
        adm = admission(cmd)                            # lease → 状态前置条件 → 围栏/边界（r24）→ 写入 staged
        cid_cache.put(cmd["cid"], adm, ttl=60)          # OrderedDict，最多 4096 条，随 checkpoint 持久化
        reply(q, adm)
        if adm["status"] == "accepted": events.emit("cmd.accepted", cid=cmd["cid"], uav=cmd["uav"])
    q.drop()                                            # 必须显式 drop，查询方才会 finalize（__init__.pyi: Query.drop）
```

**Gateway 端：在 asyncio 里调用 zenoh（`bench_cmd.py` 中已验证）**：

```python
async def call(self, key: str, msg: dict, *, timeout=1.0, retries=2) -> dict:
    for attempt in range(retries + 1):
        fut = self.loop.create_future()
        def on_reply(r):                                # zenoh 回调线程
            if r.ok is not None:
                self.loop.call_soon_threadsafe(_set, fut, msgpack.unpackb(r.ok.payload.to_bytes()))
        def on_drop():                                  # 查询结束：没有回复即超时或服务不可用
            self.loop.call_soon_threadsafe(_fail, fut, TimeoutError(key))
        self.queriers[key].get(zenoh.handlers.Callback(on_reply, on_drop), payload=msgpack.packb(msg))
        try: return await fut
        except TimeoutError:
            if attempt == retries: raise SimUnavailable(key)
            await asyncio.sleep(0.3)                    # 用同一个 cid 重试，由 sim-core 去重
```

**实测：Gateway 以 50 条命令/s 串行调用；sim-core 为 1000 架 @100 Hz，外加每 0.5 s 一次 200 条的事件突发（`bench_cmd.out`）**

| 传输 | 事件发送 | 准入 RTT p50 / p95 / p99 / max（ms） | 失败 | 事件接收 / 缺口 / 乱序 | Gateway CPU | Gateway 循环延迟 p99 | sim 步长 p50 / p99（µs） |
|---|---|---|---|---|---|---|---|
| zenoh | 每事件一条 | 9.80 / 15.56 / 18.67 / 22.06 | 0 | 7188 / **0** / **0** | 10.8% | 3.98 ms | 4266 / 9787 |
| zenoh | 按步合批 | 10.20 / 16.94 / 21.98 / 25.59 | 0 | 7172 / 0 / 0 | 8.1% | 4.49 ms | 3708 / 8713 |
| UDS | 每事件一条 | 9.46 / 11.00 / 13.49 / 15.44 | 0 | 7832 / 0 / 0 | 2.7% | 3.45 ms | 3827 / 7773 |
| UDS | 按步合批 | 9.35 / 12.59 / 14.15 / 15.65 | 0 | 7846 / 0 / 0 | 2.6% | 5.25 ms | 3799 / 8466 |

- RTT 约为 1 个步长，主要由锁存和 GIL 造成：接收线程要等 numpy 步进让出 GIL（默认切换间隔 5 ms）才能入队，入队后再等下一个步顶。传输本身只占约 0.5 ms（z_bench：query 往返 p50 0.59 ms）。
- UI 命令能接受 10 ms 级的准入延迟。遥操作 setpoint 走取最新值的 mailbox，每步读取，延迟 ≤2 步，远小于 200 ms 的截止时间。
- 如果 V0.4 的 HIL 或 agent 闭环需要更低的准入延迟，可以在步尾再排空一次 inbox，或在 sim-core 里设置 `sys.setswitchinterval(0.001)`（未实测，需要评估切换开销）。
- zenoh 的额外开销集中在 Gateway 的 Python 回调路径上。空闲会话 0.13%（`probe_misc.out`），单条事件 put 2.7 µs。按真实负载（≤5 条命令/s，事件合批后 ≤50 条消息/s）折算，Gateway 多出的 CPU 不到 1%（估算）。

### 3.4 事件平面

```python
class EventPublisher:                                   # packages/runtime/awr_runtime/events.py
    def __init__(self, bus, producer: str, epoch: int, ring: int = 4096): ...
    def emit(self, kind: str, *, severity=0, uav=None, t_sim_ns: int, **data) -> int  # 分配 seq，追加到环和本轮 batch
    def flush(self) -> None                             # 每轮迭代每个 category 一次 put（msgpack 数组），DROP
    # 自动 serve  evt/{producer}/_replay?since=<seq>&epoch=<e>  → 返回环里 seq>since 的事件；超出环范围时回复 {"truncated": true}

Event = {"seq": u64, "epoch": u32, "producer": str, "kind": "safety.geofence", "severity": 0..3,
         "t_sim_ns": i64, "t_wall_ns": i64, "uav": str | None, "cid": str | None, "data": {...}}
```

- **不用 zenoh-ext 的 AdvancedPublisher/AdvancedSubscriber**。它们的语义正好符合需要，但 1.10.1 仍标为 `_unstable`（`zenoh/ext.pyi`，r27 R13）。自研 seq 加 replay 只需约 60 行，全部基于稳定 API。等上游转稳定后再考虑替换。
- **消费者（Gateway `EventSubscriber`）**：
  - 按 (producer, epoch) 跟踪 `last_seq`，检测到缺口就 `await bus.call(f"evt/{p}/_replay", {"since": last_seq, "epoch": e})`；
  - epoch 变化时重置跟踪；
  - 事件并入全局 EventRing 并分配 Gateway 全局 seq，这就是 r27 中 WS `event` 的 seq；断线客户端按这个 seq 补拉。
- **持久化**：
  - recorder 把所有事件写进 MCAP；
  - Gateway 另把 `cmd.*`、`safety.*`、`proc.*` 追加到 `runs/<run>/audit.jsonl`，每 1 s fsync 一次，用于审计。

### 3.5 大块数据平面

- **瓦片与风场**：沿用 r27 的第 12 条和 r09。文件经 HTTP Range 或 CDN 提供，WS 和事件里只传 URL 与版本。
- **LiDAR（V0.2）**：
  - sim-core 每 100 ms 发一次 `svc/geo/lidar` query，payload 是所有装了 LiDAR 的机体位姿（N×7 float32）；
  - geo-worker 在自己的主循环里批量 `cast_rays`，reply 的 payload 是 raw float32；
  - 这条链路两端开启 zenoh SHM：`pool_size` 8 MiB，`message_size_threshold` 16 KiB；
  - 回复通过回调入队，sim-core 在下一步顶锁存（传感器数据晚 1 步可以接受）；
  - UI 只推选中机体的扫描。
- **checkpoint**：放在 `/dev/shm/awr/<run>/ckpt/`（见 §7.4），每 10 s 镜像一份到 `runs/<run>/ckpt/`。

### 3.6 zenoh 会话配置（V0.1）

```json5
// packages/runtime/awr_runtime/zenoh.json5（supervisor 注入 namespace 与端点；children 的 connect 指向 supervisor）
{
  mode: "peer",
  namespace: "awr/<world>/<run>",
  listen:  { endpoints: ["tcp/127.0.0.1:0"] },          // supervisor 固定 "tcp/127.0.0.1:7447"（汇合点）
  connect: { endpoints: ["tcp/127.0.0.1:7447"] },       // children
  scouting: { multicast: { enabled: false }, gossip: { enabled: true } },   // gossip 发现其他 peer 后直连
  transport: {
    link: { tx: { lease: 3000, keep_alive: 4,          // 跨主机时约 3 s 内检出静默断链（r27 R14）
                  queue: { congestion_control: { drop: { wait_before_drop: 1000 } } } } },
    shared_memory: { enabled: false },                 // V0.1：控制消息都很小；避免每个会话 16 MiB 的 /dev/shm 池
  },
}
```

- **汇合点死亡后 peer 仍然直连**（`probe_mesh.py`）：A 为汇合点，B、C 只连 A。kill A 之后，B→C 在 1.9 s 内仍收到 183 条消息（之前是 190 条/2 s）。所以 supervisor 短暂重启不会切断 sim-core 与 api 之间的总线。
- **16 MiB 池**（`probe_zshm.py`）：两个会话交换 64 KB 消息时，/dev/shm 新增 `*.zenoh` 共 17.3 MiB，其中单个池 16,777,216 B，对应 `DEFAULT_CONFIG.json5` L753–760 的 `pool_size`。Docker 默认 /dev/shm 为 64 MiB，4 个发布会话就会占满。

**完整 key 空间**（相对 namespace）：

| key | 类型 | 生产者 → 消费者 | 优先级 / 拥塞 |
|---|---|---|---|
| `proc/{name}/alive` | liveliness | 每个进程（启动时声明） | — |
| `proc/{name}/ready` | liveliness | 每个进程（就绪后声明，例如 geo-worker 场景建好之后） | — |
| `ctl/{producer}/{cmd,clock,lease,roster}` | queryable | sim-core、px4-bridge-k ← api、agent | INTERACTIVE_HIGH / DROP + 重试 |
| `ctl/{producer}/setpoint` | pub | api → 生产者 | REAL_TIME / DROP / express |
| `evt/{producer}/{cmd,safety,mission,sim,health,job,proc}` | pub | 各进程 → api、recorder | INTERACTIVE_LOW（safety 用 INTERACTIVE_HIGH）/ DROP |
| `evt/{producer}/_replay` | queryable | 各生产者 ← api | INTERACTIVE_LOW |
| `state/{producer}/ext` | pub | 生产者 → api（2 Hz，全机一批 msgpack） | DATA_LOW / DROP |
| `state/{producer}/frame` | pub | 远端生产者 → api（V0.5 的 `ZenohStateBus`，payload 为 slot 字节） | DATA / DROP |
| `svc/geo/{raycast,height,los,lidar}` | queryable | geo-worker ← api、sim-core | INTERACTIVE_LOW；lidar 用 BACKGROUND |
| `svc/job/{submit,cancel,status}` | queryable | job-worker ← api | INTERACTIVE_LOW |
| `sys/{procs,restart}` | queryable | supervisor ← api（`/api/sys/procs`） | INTERACTIVE_LOW |

### 3.7 候选方案对比与否决理由

| 方案 | 结论 | 依据 |
|---|---|---|
| sim-core 与 Gateway 同进程（r27 MVP） | **否决**；只保留测试用的 `--inproc` 模式（≤50 架） | C2：1000 架每步 4.2–7.8 ms，全部阻塞事件循环；崩溃耦合；Gateway 饱和时仿真 RTF 跟着下降 |
| `multiprocessing.Queue`（r06 §6 的建议） | 否决 | 要 pickle；有 feeder 线程；队列无界；没有取最新值语义；只能一对一；只适用于 multiprocessing 创建的子进程 |
| `multiprocessing.shared_memory` | 否决，改用普通文件加 mmap | Python 3.12 下 attach 方退出时会 unlink（实测）；名字不可控 |
| UDS 流（状态） | 否决 | 单读者时很快（Gateway 2.7%），但 N 个读者就要写 N 次；流式反压把写者和最慢的读者绑在一起：**压测中 Gateway 事件循环一阻塞，sim-core 就卡在 `sendall`，仿真停摆**（本单元第一次 UDS 运行复现） |
| UDS（控制面，自研 broker） | 否决（性能其实更好） | Gateway CPU 2.6% 对 zenoh 8%（压测）；但要自己写 ≥6 个进程之间的路由、请求关联、在线状态和跨主机支持，V0.5 还得重写一遍 |
| zenoh 承载状态 | 保留为跨主机实现（`ZenohStateBus`，V0.5） | 可以用（p99 12 ms），但 CPU 是 StateRing 的 2–3 倍；每次都要 `to_bytes` 拷贝；有 16 MiB SHM 池；无法无损排空 |
| ZeroMQ（r21 §3.12 提到过） | 否决 | 功能是 zenoh 的子集，还要多一个依赖；zenoh 已定为 V0.5 总线 |
| Redis pub/sub（r07 提到过） | 否决 | 多一个服务；没有取最新值与 seq 恢复语义 |
| zenoh-ext AdvancedPublisher | 暂缓 | `_unstable`；自研 seq 加 replay 只需约 60 行 |

---

## 4. sim-core 执行模型

```python
# sim/runtime/main.py
def main(cfg):
    ctx = init_child("sim-core")                 # PDEATHSIG、父进程检查、SIGTERM→优雅停止、断言 BLAS=1、faulthandler、JSON 日志
    bus = Bus.open("sim-core", ctx)
    ck  = CheckpointStore(ctx.run_dir / "ckpt", keep=3, mirror=ctx.persist_dir / "ckpt", mirror_every_s=10)
    snap, restored = ck.load_latest(skip_poisoned=True), ...
    ring, reused = StateRing.open_or_create(ctx.run_dir / "state.sim-core", capacity=cfg.capacity, slots=8, layout_id=LAYOUT_ID)
    epoch = (snap.epoch if snap else ring.hdr_epoch()) + 1; ring.set_epoch(epoch)
    fleet = FleetSim.from_snapshot(snap) if snap else FleetSim.from_scenario(cfg.scenario)   # SoA + 命名 pipeline（n03/Crazyflow）
    inbox = queue.SimpleQueue()
    bus.serve("ctl/sim-core/cmd",   lambda q: inbox.put(("cmd", q)))
    bus.serve("ctl/sim-core/clock", lambda q: inbox.put(("clock", q)))
    bus.serve("ctl/sim-core/lease", lambda q: inbox.put(("lease", q)))
    bus.serve("ctl/sim-core/roster", roster.serve)                  # 只读、线程安全的快照
    bus.subscribe("ctl/sim-core/setpoint", setpoints.store_latest)  # 每机最新值 + 时间戳（GIL 下的 dict 赋值）
    events = EventPublisher(bus, "sim-core", epoch)
    plan = PlanPool(workers=cfg.plan_workers, world=cfg.world, inbox=inbox)  # spawn、OMP=1、done_callback → inbox
    geo  = GeoClient(bus, inbox)                                            # 非阻塞 query，回复进 inbox
    clock = SimClock.from_snapshot(snap) if snap else SimClock(state=cfg.autostart)
    events.emit("sim.restarted" if snap else "sim.started", t_sim_ns=clock.t, restored_t_sim_ns=snap and snap.t_sim_ns,
                epoch=epoch, restart_count=ctx.restart_count)
    bus.token("proc/sim-core/ready")

    dt = 1 / cfg.physics_hz; pub_every = cfg.physics_hz // cfg.publish_hz; k = 0
    while not ctx.stopping:
        ring.heartbeat(clock.t, clock.state, clock.rate_milli)          # 心跳在主循环里写（supervisor 靠它判挂死）
        for kind, item in drain(inbox, limit=512):                      # ① 步边界锁存：命令/时钟/lease/规划结果/几何结果
            DISPATCH[kind](item)
        if clock.playing:
            for _ in range(clock.steps_due(max_catchup=5)):             # ② 固定步长；落后时最多追 5 步，超过则 RTF<1（不跳步，保证确定性）
                fleet.step(dt, setpoints=setpoints.snapshot())          #   mission→controller→env_wind→aero→integration→collision
                clock.advance(dt); k += 1                               #   →sensors_req→faults→battery→safety→telemetry_tap(仅发布步)
                if k % pub_every == 0:
                    ring.publish(fleet.full, fleet.lite, clock.t, roster.version)
        events.flush()                                                  # ③ 本轮的事件合批发出（DROP，不阻塞）
        slow.run(budget_us=1000)                                        # ④ 慢任务轮转：1 s checkpoint、2 Hz state_ext、10 Hz LiDAR 请求、lease 过期
        ring.set_step_stats(clock.step_p99_us)
        sleep_until(clock.next_deadline() if clock.playing else now() + 0.05)   # 暂停时 20 Hz 空转，心跳照写
    shutdown(ck, ring, events)                                          # SIGTERM：写最终 checkpoint 并镜像到磁盘，发 sim.stopped
```

**不阻塞规则（代码审查清单）**：
1. 主循环里没有网络 I/O 的同步等待，所有 `bus.call` 都走回调入队。
2. 文件 I/O 只允许写 tmpfs 上的 checkpoint（约 1–2 MB，<2 ms，受 `slow` 预算约束）。
3. 日志用 `QueueHandler`，由后台线程写出。
4. 所有 zenoh 发送都用 DROP。
5. 回调线程只做入队或 dict 赋值。
6. `ProcessPoolExecutor` 只用 spawn，因为 fork 带线程的进程不安全，Python 3.12 会给出 DeprecationWarning。

**物理步长与规模**（本机数据；最终由 G8 决定）：
- 选择规则：`physics_hz = max{h ∈ [250, 200, 100] : h × step_cost(N) ≤ 0.6 核}`。
- 1000 架时约 3.6 ms/步，只能选 100 Hz（占 45%）；≤300 架可以跑 250 Hz。
- sim-core 在 StateRing 头部自报 `step_p99_us` 与 RTF，HUD 直接显示。

---

## 5. api-gateway 进程内规则

```python
# apps/api/rt/sources/live.py
class LiveSource:                                         # 实现 r27 的 Source 接口；McapSource 与它并列
    def __init__(self, run_dir, bus, loop):
        self.rings = {}                                   # producer → (StateRing, last_seq, identity)
        self.watch = bus.watch("proc/*/ready", self._on_presence)   # liveliness：生产者上线就 attach，下线就标记
        self.events = EventSubscriber(bus, loop, on_events=self.scheduler.push_events)
    def poll(self, tick_mono_ns) -> None:                 # Scheduler 每个 60 Hz tick 调用一次，≈30–60 µs/生产者
        for p, st in self.rings.items():
            f = st.ring.read_latest(st.last_seq)
            if f:
                st.last_seq = f.frame_seq
                if f.epoch != st.epoch: self.scheduler.bump_epoch(p, f.epoch)   # → WS TIME.epoch+1 与 backfill
                self.channels.swarm_part(p, f.lite); self.channels.full_rows(p, f.full)  # uav/{id}/state 懒生产：有订阅才切片
            age = st.ring.writer_age_ms()
            st.health = "ok" if age < 250 else "stalled" if age < 2000 and pid_alive(st) else "down"
        self.channels.swarm_state.publish(b"".join(parts_in_id_order), t_sim_ns=primary.t_sim_ns)
        if tick % 60 == 0: self._check_identity()         # inode/pid 变化 → 重新 attach
```

**规则**：
- 事件循环里**禁止**任何 >1 ms 的同步调用，包括 Open3D、small_gicp、scipy.optimize、>10 万元素的 numpy 运算，也禁止用 `asyncio.to_thread` 包装它们（C3 实测）。几何查询一律走 `await bus.call("svc/geo/…")`。
- 默认 executor 限制为 4 个线程，只用于文件 I/O。
- 心跳：一个 10 Hz 的 asyncio task 写 `/dev/shm/awr/<run>/hb.api`。事件循环被卡住时心跳停写，supervisor 超过 5 s 就重启 api。
- 挂死取证：`faulthandler.dump_traceback_later(2.5, repeat=False, file=...)`，每 0.5 s 重新布置一次。它的看门狗是 C 线程，不需要 GIL，所以即使某个 C 调用长时间持有 GIL，也能 dump 出所有线程的栈。sim-core 同样处理。这一条是建议，重新布置的开销需要实测。
- WS `serverInfo.capabilities` 里加入 `procHealth`。进程状态变化用 `status` op 推送，TIME 的 state 枚举相应扩展（§7.6）。

---

## 6. Worker 进程

| 进程 | 要点 |
|---|---|
| **plan-pool** | 由 sim-core 创建：先 `os.environ.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")`，再 `ProcessPoolExecutor(W, mp_context=get_context("spawn"), initializer=plan_init, initargs=(world,), max_tasks_per_child=500)`。已实测子进程读到 `OMP_NUM_THREADS=1`。<br>`plan_init` 用 `np.load(esdf_4m.npy, mmap_mode="r")` 映射 ESDF（70 MB，r25），多个 worker 共享页缓存；`AStarPool` 每个进程一份（r25）。<br>`submit(...).add_done_callback(lambda f: inbox.put(("plan", key, f)))`。结果按 `(mission_id, revision)` 去重，过期的直接丢弃。<br>worker 崩溃后立刻抛 `BrokenProcessPool`（实测 7 ms），并且整个进程池不可再用：重建进程池，对应请求重试 1 次；再失败就降级（直线加 LOS 检查，或 TOPP-lite），同时发 `PLANNER_CRASHED` 事件，任务进入 Pause。 |
| **geo-worker** | `o3d.utility.set_max_threads(T)`，`RaycastingScene(nthreads=T)`，T 取 3。<br>场景构建约 5 s（r06，464 万三角形），建好后才声明 `proc/geo-worker/ready`。<br>queryable 回调只入队。主循环串行处理：同一类 LiDAR 请求只保留最新一批（latest-wins），小查询优先。<br>2 万条射线约 6–7 ms；3 线程下每 100 ms 约能服务 10 架带 LiDAR 的机体。<br>AGL、碰撞、安全转场**不依赖**它：sim-core 内置 numpy DSM（C13 裁决），geo-worker 挂掉时仿真照常。<br>心跳阈值：就绪前 30 s，就绪后 5 s。 |
| **job-worker** | SQLite 任务队列 `runs/jobs.db`，并发 1（r07 #15：small_gicp 的 OpenMP 与 Open3D 的 TBB 同时跑会互相拖慢）；`num_threads` 显式传入；nice 10。<br>产物先写 tmp 目录，再原子 rename。<br>进度走 `evt/job-worker/job`，最多 2 Hz。<br>**心跳阈值 120 s**：small_gicp 单次调用期间持有 GIL，例如 5M 点建 KdTree 需要 3–4.7 s，心跳线程也无法运行。<br>崩溃后任务标记为 `failed(resumable)`，重启后不自动重跑，由用户在 UI 上重试。 |
| **px4-bridge-k** | 基于 r21 §3.12 与 r22：`mavsdk==4.0.0` asyncio；`loop.set_default_executor(ThreadPoolExecutor(8·N+16))`；每条命令套 `asyncio.wait_for(…, 3.0)`；订阅写入 latest-value mailbox；每机一把 `asyncio.Lock` 串行化命令。<br>按 30–50 Hz tick 融合出 Full64/Lite32，写入 StateRing `state.px4-bridge-k`，`t_sim_ns` 由 `time_boot_ms` 加偏移映射到世界时钟。<br>声明 `ctl/px4-bridge-k/cmd`，用 lease token 本地校验。<br>r22 的机体生命周期（PENDING→…→LOST→RESTARTING）通过 `evt/px4-bridge-k/health` 发出。<br>桥进程崩溃不影响 PX4 容器，PX4 按 `COM_OBL_RC_ACT` 等 failsafe 自行处置。 |
| **recorder** | 实时模式：以 **LOSSY 加 drain** 的方式读 StateRing（≥20 Hz 轮询，K=8 @100 Hz 足够），出现 overrun 时发 `recorder.gap` 事件。**批处理或超实时评测模式**才注册为 LOSSLESS，此时 sim-core 会等 recorder（这是期望的行为）。<br>订阅 `evt/**`。MCAP 的 `log_time = t_sim_ns`，1 s 一个 chunk，zstd 压缩。<br>放在独立进程，所以 api 重启时录制不中断。 |

---

## 7. 进程监管与重启语义

### 7.1 为什么自研 supervisor（约 250 行）

- supervisord 和 docker `restart:` 都只能看到进程是否退出，**检不出挂死**（liveliness 也检不出，见 §0 第 6 条）。
- 它们也做不到"按心跳文件或 StateRing 头部判定"、"熔断后等待 UI 手动恢复"、"按依赖顺序启停"、"把进程事件发到总线"。
- honcho 或 foreman 不支持重启。circus 已经不维护。
- 自研版本用 `asyncio.create_subprocess_exec` 做 exec，**不用 fork 或 multiprocessing**：每个子进程是干净的解释器，BLAS 环境变量在 import numpy 之前就已生效。

### 7.2 配置

```yaml
# configs/runtime.yaml
run: { id: auto, world: shenzhen, run_dir: /dev/shm/awr/${run.id}, persist_dir: runs/${run.id} }
bus: { rendezvous: "tcp/127.0.0.1:7447", shm: false }
defaults:
  env: { OMP_NUM_THREADS: "1", OPENBLAS_NUM_THREADS: "1", MKL_NUM_THREADS: "1", PYTHONUNBUFFERED: "1" }
  restart: { policy: always, backoff_s: [0.5, 1, 2, 4, 8], max: { count: 5, window_s: 60 } }
  stop: { signal: SIGTERM, grace_s: 5 }
processes:
  sim-core:
    cmd: [python, -m, sim.runtime, --scenario, S1, --physics-hz, auto, --publish-hz, "100"]
    cpus: [1]
    liveness: { ring: state.sim-core, stale_s: 2.0, startup_grace_s: 10 }
    id_range: [0, 1024]
  api:
    cmd: [uvicorn, apps.api.main:app, --host, 0.0.0.0, --port, "8000", --loop, uvloop, --ws, websockets, --workers, "1"]
    cpus: [0]
    liveness: { heartbeat: hb.api, stale_s: 5.0, startup_grace_s: 15 }
    start_after: [sim-core]        # 只是启动顺序，不是硬依赖：api 必须能容忍 sim-core 缺席
  geo-worker:      # V0.2
    cmd: [python, -m, world.geometry.worker, --threads, "3"]
    cpus: [4, 5, 6]
    liveness: { heartbeat: hb.geo-worker, stale_s: 5.0, startup_grace_s: 30 }
  job-worker:
    cmd: [python, -m, awr_jobs.worker]
    nice: 10
    liveness: { heartbeat: hb.job-worker, stale_s: 120 }
    restart: { policy: on-failure }
  px4-bridge-0:    # V0.2，每 16 架一个
    cmd: [python, -m, sim.backends.px4_bridge, --slot, "0", --vehicles, "0-15"]
    liveness: { ring: state.px4-bridge-0, stale_s: 3.0, startup_grace_s: 20 }
    id_range: [1024, 16]
```

### 7.3 状态机与健康信号

```text
 STOPPED ─start─► STARTING ──ready token 或心跳首写──► RUNNING ──SIGTERM(stop)──► STOPPING ─► STOPPED
                     │  startup_grace 超时                │ exit≠0 / 心跳超时(HUNG→SIGTERM→1 s→SIGKILL)
                     └──────────────► BACKOFF ◄────────────┘
                                        │ 等待 backoff[i]；60 s 内第 6 次 ─► FAILED（熔断，等待 sys/restart 手动恢复）
                                        └──► STARTING（env 注入 AWR_RESTART_COUNT、AWR_LAST_EXIT）
```

| 信号 | 来源 | 周期 | 用途 |
|---|---|---|---|
| 退出码 | `waitpid`（asyncio 子进程） | 事件驱动 | 崩溃检测（0 ms 级） |
| 主循环心跳 | StateRing 头部 `heartbeat_ns` 或 `hb.<name>` 文件（16 B mmap） | supervisor 以 5 Hz 读取 | 挂死检测；必须在主循环里写，**不能开后台线程代写**，否则会掩盖主循环挂死 |
| liveliness `proc/*/alive`、`proc/*/ready` | zenoh | 事件驱动（kill 后 6–8 ms，r27） | 让**对端**（api、sim-core）尽快知道某服务是否可用；跨主机时依赖 lease（3 s） |
| 自报指标 | ring 头部的 `step_p99_us`/RTF；api 的 loop-lag p99 | 1 Hz | HUD 与告警；不触发重启 |

子进程侧（`awr_runtime/child.py::init_child`）：
- `prctl(PR_SET_PDEATHSIG, SIGTERM)`，通过 ctypes 调用 libc；
- 立即检查 `os.getppid() == AWR_SUPERVISOR_PID`，防止 supervisor 在 prctl 之前就已经死掉；
- SIGTERM 只设置 `stopping` 标志；
- 断言 BLAS 环境变量；
- 启用 faulthandler；
- 日志以 JSON 行写到 stderr。supervisor 把 stderr 写到 `runs/<run>/logs/<name>.log`（10 MB×5 轮转），并在内存中保留最后 200 行，供 UI 的"进程"面板展示。

### 7.4 sim-core 崩溃恢复细节

- **checkpoint 内容**：
  - FleetSim 的全部 SoA 数组，用原始二进制连续写出；
  - SimClock（t、rate、state）、epoch、事件 seq、rng 状态；
  - 用 msgpack 序列化的任务、Lease、Safety FSM、围栏、cid 幂等表；
  - 写入顺序：先写 `ckpt/<t_sim_ns>.bin.tmp`，再 `os.replace`；保留 3 代；每 10 s 镜像到磁盘。
- **频率**：每 1 s 仿真时间一次，所以回滚窗口 ≤1 s。暂停状态下不写。
- **重启流程**：
  1. supervisor 等待 backoff；
  2. 以 exec 方式重启 sim-core；
  3. `open_or_create` 复用原来的 ring 文件（head 继续递增，读者的 mmap 不失效）；
  4. 加载最新 checkpoint；
  5. epoch 改为 checkpoint 中的 epoch+1；
  6. 发出 `sim.restarted{epoch, restored_t_sim_ns, lost_ms, restart_count}`；
  7. **时钟默认恢复为崩溃前的播放状态**，配置项 `resume_after_crash: play|pause`，演示时建议设为 play。
- **毒性 checkpoint**：如果恢复后 5 s 内再次崩溃，就把该 checkpoint 标记为 `.poison`，下次改用上一代；连续 3 代都中毒时，从剧本初始状态重新开始，并发 `sim.reset_from_scenario`。
- **在途命令**：
  - 已发出但没有收到准入回复的命令：Gateway 在 1 s 超时并重试 2 次后，返回 `failed{code: SIM_UNAVAILABLE}`；
  - 已准入且 `t_accept ≤ restored_t_sim_ns` 的命令：已经包含在 checkpoint 状态里，会继续执行，完成事件照常到达；
  - 已准入但晚于 checkpoint 的命令：Gateway 收到 `sim.restarted` 后，对这些 cid 发 `failed{code: SIM_ROLLBACK}`，UI 用 toast 提示重新下发（V0.4 可以考虑按日志自动重放幂等命令）。
- **客户端可见的表现**：
  - TIME.state 依次经过 `stalled`（>250 ms 没有心跳）→ `restarting`（supervisor 处于 BACKOFF 或 STARTING）→ `playing`；
  - WS epoch 加 1，随后下发 backfill，机体从回滚点继续飞；
  - HUD 提示"仿真内核已恢复（回滚 0.9 s）"。
- **实测**（`supervisor_proto.out`，100 Hz、1000 行 ring）：

| 故障 | 检出 | 恢复出帧（读者看到新 epoch 首帧） | 回滚 |
|---|---|---|---|
| kill -9 | waitpid 立即返回 | **789 ms**（0.5 s 退避 + 约 0.29 s 冷启动：import 178–256 ms，zenoh.open 3–15 ms，Fleet 初始化 7 ms） | t_sim 2.9 s 回到 2.0 s |
| 主循环挂死 | 心跳年龄 **2126 ms**（阈值 2 s，5 Hz 巡检） | 共 **3416 ms**（检出 2.13 s + SIGTERM + 第 2 次重启退避 1 s + 冷启动） | 回到 4.0 s |

### 7.5 各进程的故障语义

| 进程崩溃或挂死 | 对其他进程 | 客户端可见 | 恢复 |
|---|---|---|---|
| **api** | sim-core 不受影响，ring 继续写；recorder 不受影响 | WS 断开，重连（500 ms→10 s 退避，00-index §5.5 T6），sessionId 变化后重新订阅并收到 backfill；在途 call 用同一个 cid 重发，由 sim-core 去重 | 约 1–2 s（uvicorn 冷启动） |
| **sim-core** | 状态冻结在最后一帧（kill 后仍可读，已实测）；api 标记 stalled/down；px4-bridge 不受影响 | 见 §7.4 | 0.8 s（崩溃）/ 3.4 s（挂死）+ 回滚 ≤1 s |
| **plan-pool worker** | sim-core 收到 BrokenProcessPool | 相关任务收到 `PLANNER_CRASHED`，重试或降级 | 重建进程池约 0.3 s |
| **geo-worker** | queryable 消失：liveliness DELETE 之后，对 `svc/geo/*` 的查询直接返回没有回复；LiDAR 降级 | 射线、LOS 工具返回 `SERVICE_UNAVAILABLE`；LiDAR 图层显示"降级" | 约 5 s（场景重建）后声明 ready |
| **job-worker** | 无 | 任务显示 `failed(resumable)` | 重启后处理下一个任务 |
| **px4-bridge-k** | 名下机体的 ring 心跳停止，api 把这些机体标记为 `link_lost` | 机体卡片变灰，事件 `vehicle.link_lost` | 重连 PX4，6.7–9.1 s 回到 READY（r22） |
| **supervisor** | 全部子进程收到 PDEATHSIG 并退出（sim-core 在 SIGTERM 处理里写最终 checkpoint） | 全部断开 | 由容器 `restart: unless-stopped` 拉起；sim-core 从磁盘镜像的 checkpoint 恢复（最多回滚 10 s） |

### 7.6 启停顺序与协议补充

- **启动**：
  1. supervisor 打开 zenoh（监听 7447）；
  2. 创建或清理 run 目录，删除上一次遗留的 `/dev/shm/awr/<run>`；
  3. 并行启动 sim-core、geo-worker、job-worker、px4-bridge；
  4. 最后启动 api。api 必须能容忍生产者还没就绪，UI 显示"启动中"。
- **停止**（SIGTERM 或 SIGINT）：
  1. 发 `sys.shutting_down` 事件；
  2. 先停 api，让它不再接受新命令；
  3. 再停 sim-core，写最终 checkpoint 并镜像到磁盘；
  4. 然后停各 worker；
  5. 每一步等 5 s，超时发 SIGKILL；
  6. 除非设置了 `keep_run_dir`，否则删除 run 目录。
- **`awr.rt.v1` 补充**（交给 r27 规范）：
  - TIME.state 枚举：0 stopped、1 playing、2 paused、3 stepping、**4 stalled、5 restarting、6 failed**；
  - `status` op 推送 `proc.<name>` 的级别与文案；
  - `result.code` 新增 `SIM_UNAVAILABLE`、`SIM_ROLLBACK`、`SERVICE_UNAVAILABLE`、`PLANNER_CRASHED`；`duplicate` 不算错误；
  - WS 的 epoch 由 Gateway 维护为全局 epoch：任何生产者 epoch 变化或发生 seek 时加 1。
- **Docker**：
  - 单容器 `awr-backend`，入口为 `tini -- python -m awr_runtime.supervisor`；
  - `shm_size: 1g`；
  - 可选 `cap_add: [SYS_NICE]`，用于 sim-core 的 nice -5；
  - PX4 SIH 容器仍由 orchestrator 管理（r22）。

---

## 8. 代码落点与接口签名

在 00-index §3.0 的目录中**新增 `packages/runtime/`**，作为共享的 Python 包（editable install）：

| 文件 | 内容 | 来源 |
|---|---|---|
| `packages/runtime/awr_runtime/statering.py` | StateRing（§3.2 签名） | 直接迁移 `.cache/research/g05/statering.py`，读者游标扩到 8 个，加 `open_or_create`、`identity` |
| `packages/runtime/awr_runtime/bus.py` | **唯一 import zenoh 的模块**：`Bus.open(name, ctx)`、`serve(key, handler)`、`async call(key, msg, timeout, retries)`、`call_cb(key, msg, on_reply)`（给非 asyncio 进程用）、`publisher(key, prio, express)`、`subscribe(key, cb, loop=None)`、`token(key)`、`watch(glob, on_change)`；另有 `LocalBus`（进程内实现，单测用） | `bench_cmd.py` 的 asyncio 桥接 |
| `packages/runtime/awr_runtime/events.py` | `EventPublisher`、`EventSubscriber`（seq、replay、合批） | §3.4 |
| `packages/runtime/awr_runtime/heartbeat.py` | `Heartbeat(path).beat()`、`Heartbeat.age_ms(path)`（16 B mmap） | — |
| `packages/runtime/awr_runtime/child.py` | `init_child(name) -> RunCtx`（PDEATHSIG、父进程检查、信号、faulthandler、日志、BLAS 断言） | `supervisor_proto.py::child_main` |
| `packages/runtime/awr_runtime/supervisor.py` | `Proc`、`Supervisor`、`sys/procs` 与 `sys/restart` queryable、日志收集 | `supervisor_proto.py::Proc` |
| `packages/runtime/awr_runtime/checkpoint.py` | `CheckpointStore(dir, keep=3, mirror, mirror_every_s)`：`save(t, epoch, arrays, meta)`、`load_latest(skip_poisoned)`、`poison(t)` | §7.4 |
| `packages/contracts/bus/{keys.json, command.schema.json, event.schema.json, roster.schema.json}` | key 空间与消息 schema（与 `rt/layouts.json` 并列，是唯一真源） | §3.3–3.6 |
| `configs/runtime.yaml` | §7.2 | — |
| `sim/runtime/{main.py, loop.py, dispatch.py, clock.py}` | §4 | — |
| `sim/planning/pool.py` | `PlanPool(workers, world, inbox)` | §6 |
| `world/geometry/worker.py` | geo-worker | r06 §3.4 + §6 |
| `apps/api/rt/sources/live.py`、`apps/api/rt/bus_bridge.py`、`apps/api/sys.py` | LiveSource、命令路由（roster）、`/api/sys/procs` | §5 |
| `apps/recorder/main.py` | recorder（V0.2） | §6 |
| `tools/bench/ipc/` | 把 `bench_state.py`、`bench_cmd.py`、`supervisor_proto.py` 迁成 CI 回归 | 本单元 |

---

## 9. 验收与测试

| 用例 | 方法 | 阈值（本机 1.7 GHz；真机应更好） |
|---|---|---|
| 状态平面容量 | `tools/bench/ipc/bench_state.py shm --n 1000 --hz 100 --load fleet` | Gateway CPU ≤3%；tick 年龄 p99 ≤15 ms；publish p99 ≤300 µs |
| 命令准入 | `bench_cmd.py`，50 cmd/s | 失败 0；RTT p99 ≤2.5 个步长；事件缺口与乱序为 0 |
| 事件恢复 | 注入：丢弃 api 端第 k 条事件 | 1 s 内通过 replay 补齐；缺口超过 4096 条时 UI 提示并走 REST 全量 |
| GIL 隔离 | api 进程在负载下 loop-lag p99 | ≤10 ms（10 个客户端，geo-worker 以 100k 射线 / 10 Hz 运行） |
| 混沌：kill -9 sim-core | supervisor 测试钩子 | 新 epoch 首帧 ≤1.5 s；回滚 ≤1 s；在途命令全部有终态（`SIM_UNAVAILABLE`/`SIM_ROLLBACK`/正常完成） |
| 混沌：sim-core 挂死 | 注入忙循环或长时间持有 GIL 的 C 调用 | ≤2.5 s 检出，≤4 s 恢复出帧；faulthandler 有栈输出 |
| 混沌：kill -9 api | — | 仿真不中断（ring 头部 step_seq 连续）；客户端 ≤3 s 重连；同一 cid 重发得到 `duplicate` |
| 混沌：kill supervisor | — | 所有子进程 ≤1 s 退出；容器重启后从磁盘 checkpoint 恢复 |
| 熔断 | 让 sim-core 连续崩溃 | 第 6 次进入 FAILED，UI 显示最后 200 行 stderr；`sys/restart` 能手动恢复 |
| 慢消费者 | api 事件循环被人为阻塞 3 s | sim-core 步进不受影响（step_seq 连续，RTF ≥0.99） |

---

## 10. 风险与注意事项

| # | 风险 | 对策 |
|---|---|---|
| R1 | seqlock 依赖 x86 TSO；ARM 上可能读到撕裂帧 | ARM 部署改用 `ZenohStateBus`，或加 cffi fence；CI 在 ARM runner 上跑 10 万次撕裂检测 |
| R2 | ring 布局变化导致读写两端不一致 | `layout_id` = layouts.json 的哈希；读者校验不一致就拒绝 attach，并报 `LAYOUT_MISMATCH` |
| R3 | 回调线程抢 GIL，导致准入延迟约等于 1 步 | 已量化（约 10 ms），可以接受；需要更低时步尾再排空一次，或调小 switchinterval |
| R4 | zenoh-python 回调在 Python 线程中执行（`indirect=True`），回调里做重活会拖慢整个进程 | lint 规则：回调函数体只允许 `put`、`call_soon_threadsafe`、dict 赋值 |
| R5 | `Query` 没有 `drop()`，导致查询方一直等到超时 | 统一由 `Bus.serve` 的包装器在 `finally` 里调用 drop；reply 统一走 `Request.reply()` |
| R6 | zenoh SHM 池（每个会话 16 MiB）占满 /dev/shm | V0.1 关闭；V0.2 只在 geo-worker 与 sim-core 之间开启，pool 8 MiB；容器 `shm_size: 1g` |
| R7 | job-worker 与 geo-worker 长时间持有 GIL 被误判为挂死 | 按进程配置心跳阈值（120 s / 5 s）；启动阶段有宽限期 |
| R8 | 毒性 checkpoint 导致崩溃循环 | 恢复后 5 s 内崩溃就标记 poison 并退回上一代；连续 3 代失败则从剧本初始状态重来 |
| R9 | 多个生产者的机体 id 冲突 | supervisor 分配 `id_range`；roster 校验不相交，冲突时拒绝启动 |
| R10 | Python 3.12 fork 带线程的进程 | 只用 exec（supervisor）和 spawn（进程池） |
| R11 | 未来改用 3.13 的 `shared_memory(track=False)` | 不需要：普通文件加 mmap 已足够，而且可观测 |
| R12 | 1000 架时 sim-core 步长 p99 接近 10 ms 的截止时间（本机 1.7 GHz） | 物理 100 Hz、`max_catchup=5`，RTF 可以低于 1 且不跳步；真机 3 GHz 以上约有 2 倍余量；更大规模走 V1.0 分片 |

---

## 11. 对既有文档的修订建议

1. **00-index §3.10**："Planning Service（放在仿真内核进程内）"改为："Planning Service 由 sim-core 调度，在 spawn 进程池（`OMP_NUM_THREADS=1`，W=1–2）中执行，结果在步边界锁存。"单次 25–78 ms 的求解不能放进 100–250 Hz 的步进循环。
2. **00-index §3.11 最后一条**："MVP 在进程内（Gateway 与仿真内核的 IPC 见 §9 G6）"改为："V0.1 起 api 与 sim-core 双进程。状态走 StateRing（mmap seqlock，100 Hz）；命令、事件与在线状态走 zenoh（peer，本机）；V0.5 跨主机时复用同一套 key 空间。"原文把 G5 误写成了 G6，一并更正。
3. **00-index §8 C13**：去掉括号里的"进程模型（G6）"，改为引用本文 §2 与 §6（geo-worker）。
4. **r27 §3.13**：
   - "仿真步进以 100 Hz 运行在独立的 asyncio task 里……可以留在同一进程"改为只适用于 `--inproc` 测试模式；
   - 容量表中"MVP 单进程"一行改为"MVP 双进程加 StateRing"；
   - 表中 V0.6 那一行的"仿真与 Gateway 分成两个进程，经 zenoh 连接"提前到 V0.1，并把状态平面改为 StateRing。
5. **r27 §3.15**：
   - `uav/{id}/cmd/**`（每机 queryable）改为 `ctl/{producer}/cmd` 加 roster 路由；
   - `event/**` 由 BLOCK 改为 DROP 加 seq 加 `_replay`（sim-core 永不阻塞）；
   - `AdvancedPublisher` 暂缓使用；
   - 新增 `proc/*/{alive,ready}`、`svc/geo/*`、`sys/*`。
6. **r06 §6（落点）**："二者之间用 `multiprocessing.Queue` 或 ZeroMQ PUB/SUB"改为 StateRing 加 zenoh；并补充说明 geo-worker 与 sim-core 是两个进程，因为 Open3D 调用会阻塞 sim-core 的固定步长。
7. **r07 §6**："进度通过 Redis pub/sub 或 multiprocessing Queue 回传"改为 `evt/job-worker/job`（zenoh）。
8. **01-design §33 后端技术栈**：补一行"进程模型：supervisor + api + sim-core + workers（见 g05）"，以及一行"Internal IPC：StateRing（状态）+ zenoh（控制与事件，V0.1 起）"。
