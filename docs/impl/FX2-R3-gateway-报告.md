# FX2-R3-gateway 修复报告（D1-AC-29：api 内存线性增长与 soak 口径）

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R3-gateway（区域 gateway：`python/awr/api`、`python/awr/runtime`、`python/awr/recorder`、`tools/bench/ipc`；为本区域验收项的测量口径另改了 M16 的 `apps/web/perf/soak.spec.ts`，理由见 4.1） |
| 日期 | 2026-10-02 |
| 依据 | D1 验收报告第 2 轮（§3 D1-AC-29 行、§4.4、§4.5、§5 最后一行、§7）；INT-1 §3、§7；03 §1.3、§8.4（D1-AC-29）、ADR-033；17 §6.12；18 §7.5、§8.8；M11 PRD §6.3.4、§6.5（关键参数）、M11-FR-065；M16 PRD §6.12（soak 行）、M16-FR-058 |
| 环境 | 8 核 Xeon E5-2603 v4、无 GPU；Chromium 151（SwiftShader）；Node 22.12；Python 3.12（`.venv`）；修复期间有其他工作包并行（load 2–7） |
| 约束执行 | 没有安装依赖；没有使用 git；没有跑性能基准与 perf 标记用例的正式判定。诊断用不进判定的后端短跑（ci profile、不钉核、Python/Node 轻量客户端，不开浏览器；改动后的一次在共享锁内）；修复后的快速自测在排他锁内 1 次（第 5.3 节，持锁时长见该节） |

## 1 结论摘要

| 子项 | 根因 | 处理 | 自测（不作判定） |
|---|---|---|---|
| api RSS 线性增长（+46.3%，约 1.8 MB/min，无平台） | 全局 EventRing（65,536 条）的环项是完整的 Python dict：实测约 1.2 KB RSS/条，满环约 80 MB。soak 中事件约 28 条/s，30 min 只填到约 5 万条，所以表现为 30 min 内持续线性增长、到不了平台。另有 `perf/server` 的 600 s 指标窗口每秒存一个以新建字符串为键的 dict（约 2.7 KB/行，前 10 min 爬升约 1.6 MB） | EventRing 改为"事件 JSON 编码一次 + (type, level) 索引 + 每 256 条封块 zlib 压缩"，至少保留最近 65,536 条，已封块压缩字节设 8 MiB 安全上限；指标窗口改为列存（`array('d')` 行，0.42 KB/行）；WS 与 REST 直接拼接已编码的事件字节 | 同一 soak 剧本、1 个客户端、8 min：修复前 api RSS 斜率 2.07 MB/min（事件 27.8 条/s），修复后 0.12 MB/min（事件 28.6 条/s）；soak 事件压缩后约 30 B/条（明文 275 B/条）；按 30 min soak 首尾各 5 min 中位折算约 +2%（阈值 10%） |
| soak 用例缺 RSS 中位口径 | harness 只有自身采样器的首尾两点（`server.json`），而注册表的 `rss_growth_pct` 是 `source: 'script'`，用例从来没写，第 2 轮记为 PERF-E011 | `soak.spec.ts` 每 5 s 读 api 与 sim-core 的 `/proc/<pid>/status`，按首尾各 5 min 中位数写 `rss_growth_pct`（取两者较大者）及分项 | 见 5.3 |
| soak 用例缺帧节奏 | 用例不走 flight60，frame 指标为空（PERF-E011） | soak 结束后在同一浏览器跑一次整景 flight60（live 机群），其快照作为 `snapshot.json` 供 harness 提取帧节奏；soak 页面快照另存 `snapshot-soak.json` | 见 5.3 |
| GPU 池高水位未测 | 用例没有时间序列 | 每 5 s 采样 `__perf.pc.residentPts`、`poolRows`，按 5 min 窗口取最大值，6 个窗口严格递增判为不满足 | 见 5.3 |
| 非预期重连只算最后一个页面 | 切世界是整页导航，`__perf.net.reconnects` 每次从 0 起，原用例只读结束时的页面 | 每次离开页面前累加 | 见 5.3 |
| 孤儿 plan-pool 进程（第 2 轮 4.1b 的 supervisor 一侧；与 29 的 RSS 相关） | ① supervisor 用 `await proc.wait()` 检测退出，而 asyncio 的 `wait()` 要等 stdout 管道关闭；继承了管道的后代（plan-pool 工作进程、resource_tracker）存活时退出检测一直阻塞；② 子进程退出后没有回收其进程组 | 退出检测改为同时每 20 ms 查 `returncode`；子进程退出后对其进程组先 SIGTERM、1 s 后 SIGKILL，停止时等回收完成 | 新用例：子进程被 kill -9 而后代仍持有管道并忽略 SIGTERM 时 ≤ 0.3 s 检出、≤ 3 s 回收；去掉轮询（等价于原实现）时该用例失败（进程一直停在 RUNNING） |

新发现（不在本区域，8.1 提给 sim）：`soak-shenzhen` 的 ladder 四组 `orbit.turns = 120`，而 sim-core 的 orbit 命令校验 `turns ∈ [0, 100]`，于是 200 架的环绕命令全部以 110 被拒，M10 把航迹置 SUSPENDED 后反复重试：8 min 内 `cmd.rejected` 3905 条（8.1 条/s）、`track.state` 11763 条（24.5 条/s），占全部事件的 85%。soak 中 200 架实际上没有在环绕，事件量也比正常高一个数量级。本包的修复不依赖这一点（环的内存与事件率无关地有界），但 soak 要测到"S1 + 200 架环绕"的稳态，需要 sim 修正剧本或校验上限。

## 2 D1-AC-29：api RSS 线性增长的定位

### 2.1 方法

不改产品代码的诊断（工具在 `runs/fx2r3-gateway/diag/tools/`）：

- `run_backend.py`：supervisor `--profile ci --only sim-core,api`、`AWR_SCENARIO=soak-shenzhen`、`net.port_offset=13`，每 10 s 读 `/proc/<pid>/status` 的 VmRSS，并经 `GET /api/events` 按类型与生产者统计事件；可选 `rt_client.mjs` 客户端；
- `sitecustomize.py`（经 `PYTHONPATH` 只注入 api 进程）：`AWR_DIAG_TM` 时启动后 25 s 开 `tracemalloc`，每 60 s 与基线快照比较（按行与按调用栈）；`AWR_DIAG_CONT` 时每 60 s 扫描 `awr.*`、`starlette`、`uvicorn`、`fastapi`、`websockets` 各对象 `__dict__` 中容器的长度，列出与基线相比变化的容器。

### 2.2 数据

| 运行 | 条件 | api RSS | 事件 | 结论 |
|---|---|---|---|---|
| r1 | 无客户端，tracemalloc（6 帧） | 101 → 157 MB（7 min；含 tracemalloc 自身开销） | 30–40 条/s | 增量最大的分配点：`runtime/bus.py:116`（事件 msgpack 解包出的 data，4.6 MB / 7.5 万块）、`api/rt/events.py:119`（EventRing 的事件 dict，2.9 MB / 2.6 万块）、`api/rt/metrics.py:117`（指标窗口行，0.8 MB）；三者合计占全部追踪增量的 90% 以上，前两者随事件数线性增长 |
| r2 | 1 个 `rt_client`，容器扫描，修复前 | 97.6 MB（60 s）→ 111.4 MB（461 s），2.07 MB/min | 2938 → 14049 条（27.8 条/s） | 只有两个容器持续变长：`EventIngest.ring` 2816 → 13710 条、`Metrics.ring` 28 → 449 行（上限 600）；channel 表只在启动时 +12（任务 channel），会话、在途表、限流表、幂等表、静态文件缓存都不变。RSS 增量 / 事件增量 = 1.24 KB/条 |
| r3 | 同 r2，修复后（共享锁内） | 94.5 MB（60 s）→ 95.3 MB（461 s），0.12 MB/min | 7010 → 18453 条（28.6 条/s） | `EventRing` 环内 1.8 万条只占约 0.6 MB；剩余斜率主要是指标窗口在前 10 min 填满（列存后约 0.25 MB）与分配器噪声 |

逐条核对（微基准，同一份事件）：dict 形式 1048 B/条（tracemalloc 口径），RSS 口径约 1.2–2.3 KB/条；JSON 明文 275 B/条（soak 实际事件的均值）；每 256 条一块 zlib level 1 压缩后 30.4 B/条，每块 0.59 ms（块 128：32 B/条、0.28 ms；块 512：29.8 B/条、1.2 ms）。

与验收数据吻合：第 2 轮 soak 中 api 从 93 MB 线性升到 151 MB（约 1.8 MB/min），按 1.24 KB/条折算约 24 条/s，与本包测得的 soak 事件率（28 条/s）一致；65,536 条要 39 min 才填满，30 min 的 soak 看不到平台。验收报告建议排查的"按连接或按世界累积的缓存"经容器扫描排除。

### 2.3 事件构成（r3，8 min，sim-core 产生 100%）

| 类型 | 条数 | 条/s | 说明 |
|---|---|---|---|
| `track.state` | 11763 | 24.5 | 绝大多数是 `WORKING → SUSPENDED`（`reason: rejected:110`）与恢复的往复 |
| `cmd.rejected` | 3905 | 8.1 | `op: orbit`、`code: 110`（参数越界），8.1 提给 sim |
| `uav.state` | 991 | 2.1 | |
| `coverage.progress` | 675 | 1.4 | |
| 其余 | 1147 | 2.4 | `cmd.accepted/running/succeeded`、`lease.acquired`、`sim.stage.overbudget`、`plan.ready` 等 |

## 3 修复

### 3.1 EventRing：编码一次、封块压缩、内存有界（`python/awr/api/rt/events.py`）

- 新增 `EventRing`：`append(seq, type, level, item)` 中 `item` 是事件的 WS JSON（UTF-8，`ensure_ascii=False`，与控制面 `jdump` 同一格式），
  seq 连续、由位置换算；最新未满块为明文（`list[bytes]` + 类型下标 `array('I')` + 级别 `bytearray`），满 256 条封块：`zlib.compress(level 1)`
  加 n + 1 个偏移；淘汰按块，去掉最旧一块后仍不少于 65,536 条才淘汰（因此至少保留最近 65,536 条，最多多一个块），已封块压缩字节超过 8 MiB
  时提前淘汰（只在异常大且难以压缩的事件下触发，`stats.evicted_bytes_cap` 计数）。
- `iter_since(seq, types, level_min)`：先用索引过滤，块内没有命中项时不解压；`since(seq, limit)` 供 resume 补发。
- `EventIngest.append`：分配全局 seq → `encode_event` 一次 → 入环 → 按各连接 filter 放入 `pending_events`（共享同一份字节）；
  返回 seq（原来返回 dict，调用方都不用返回值）。`encode_event` 对 msgpack bin、numpy 标量与数组、集合按 FastAPI `jsonable_encoder`
  的方式转换（原来这类值会在 `flush_events` 的 `jdump` 处抛错）。
- `set_capacity(maxlen)`：测试用（原来测试直接把 `ring` 换成 `deque(maxlen=8)`）。
- 非有限浮点数（NaN、Infinity，含 numpy 标量与数组）写成 `null`：NaN 不是合法 JSON，原来 WS 会下发 `NaN`（浏览器 `JSON.parse` 整条失败），
  REST 的 `JSONResponse(allow_nan=False)` 会返回 500。

CPU：编码加入环（含摊销的压缩）17.7 µs/条，原路径（每连接 dict 展开 + `jdump`）14.2 µs/条·连接，单客户端持平、多客户端更省；
`GET /api/events` 一页 1000 条 2.7 ms（不再逐条转 dict 再序列化）。28 条/s 时约 0.5 ms/s；570 条/s 压测时约 10 ms/s（1% 核）。

### 3.2 发送路径（`python/awr/api/rt/session.py`、`python/awr/api/rest/sys.py`）

- `ClientSession.flush_events`：单条 `{"op":"event",` + 事件对象去掉开头的 `{`；多条 `{"op":"events","items":[...]}`，按 256 项分批；
  字段与顺序与原来的 `{"op": "event", **e}` 一致。`event_ok(kind, level)` 改为按索引判断。
- `GET /api/events`：按索引过滤后直接拼接响应体 `{"items":[...],"next_since":N,"oldest_seq":M}`，返回 `Response`（OpenAPI 仍按注解）；
  410 语义不变。
- `ws.py` 的 resume 补发不变（`gw.events.since()` 现在返回字节列表）。

### 3.3 指标窗口列存（`python/awr/api/rt/metrics.py`）

`perf/server` 的 600 项窗口原来每秒存一个以 `f"{sec}.{k}"` 新建字符串为键的 dict（30 个字段约 2.7 KB/行），改为字段名 → 列号表
（只登记一次）加每秒一行 `array('d')`（缺失为 NaN，`window()` 中剔除），0.42 KB/行，600 行约 0.25 MB。`window()` 的输出不变
（NaN 不再进入分位数，原来会使该字段的分位数为 NaN）。

### 3.4 诊断字段

`GET /api/rt/inspect` 的 `event_ring` 增加 `count`、`bytes`（环内条数与存储字节）。

### 3.5 supervisor：退出检测与后代回收（`python/awr/runtime/supervisor.py`）

第 2 轮 4.1b 记录了被 SIGKILL 的 sim-core 留下的孤儿 plan-pool 进程（本机曾累积 72 个、约 6.5 GB），建议之一是"supervisor 以独立进程组
启动 sim-core、回收时 killpg"，这部分在本区域。核对时发现一个更严重的相关缺陷：

- supervisor 的 `_wait` 用 `await proc.wait()` 检测子进程退出。asyncio 的 `Process.wait()` 只有在进程退出**且**全部管道断开后才返回
  （`base_subprocess._call_connection_lost` 才唤醒等待者），而子进程的后代（multiprocessing spawn 的工作进程与 resource_tracker）
  继承了 stdout/stderr。后代存活时，子进程即使已被 kill -9，supervisor 也一直认为它在 RUNNING：不重启、不写 crash、`sys/procs` 不变。
  本机在诊断期间看到的孤儿对（`multiprocessing.spawn` + `resource_tracker`，ppid 1，pgid 与会话 = 已退出的 sim-core 的 pid）的
  fd 1、2 仍是从 sim-core 继承的输出端。plan-pool 工作进程在初始化时设置了 PDEATHSIG，多数情况下会随 sim-core 退出，所以平时不易暴露；
  用例复现见下（去掉轮询时，子进程 kill -9 后一直停在 RUNNING）。
- 修复：`_wait` 在等 `proc.wait()` 的同时每 `EXIT_POLL_S`（20 ms）查一次 `proc.returncode`（SIGCHLD 时即写入），先到者为准；
  随后 `_reap_group(pgid)`：子进程以新会话启动（pgid = pid），对该组 `killpg(SIGTERM)`，`REAP_GRACE_S`（1 s）后 `killpg(SIGKILL)`；
  组内已无进程时 ESRCH 直接返回（组 id 在仍有成员时不会被复用，不会误杀）。resource_tracker 忽略 SIGTERM，工作进程退出、管道 EOF
  后会自行清理共享资源并退出，所以先 SIGTERM 再 SIGKILL。`stop()` 在停完全部进程后等回收任务完成（≤ 2 s），避免 supervisor 退出后留孤儿。
- 代价：每个子进程每 20 ms 一次定时器唤醒（6 个进程约 300 次/s，可忽略）；kill -9 的检出时延上界由"等管道关闭"变为 20 ms。

## 4 soak 用例口径（`apps/web/perf/soak.spec.ts`）

### 4.1 为什么改这个文件

D1-AC-29 整条分给 gateway，验收诊断写明"soak 用例缺 RSS 中位口径与帧节奏"。`rss_growth_pct` 在注册表中是 `source: 'script'`、
gating，只有用例本身能写；harness 与 `cases/ui.mjs` 没有改动（避免与 web-engine 工作包在 harness 上冲突）。

### 4.2 内容

- 每 5 s 一次：JS 堆（CDP，原有）；api 与 sim-core 的 RSS（`/proc/<pid>/status` VmRSS，pid 取自 `GET /api/sys/procs`，viewer 令牌，
  不占操作席位）；`__perf.pc.residentPts`、`poolRows` 与 `__perf.net.reconnects`（一次 `page.evaluate` 取几个数，不做快照）。
  读不到 RSS 时重取 pid，pid 变化记为重启（harness 同时以 PERF-E008 判无效）。
- `heap_growth_pct`、`rss_growth_pct`：末 `windowMin`（5）min 中位数相对首 `windowMin` min 中位数的增长（18 §7.5）；`rss_growth_pct`
  取 api 与 sim-core 的较大者，另写 `rss_api_growth_pct`、`rss_sim_growth_pct` 与首尾中位 MB。`minutes` 小于 15 时窗口取 `minutes / 3`
  （只用于快速自测）。
- GPU 池：按窗口取 `residentPts` 最大值，写 `gpu_pool_hw_growth_pct` 与 `gpu_pool_hw_monotonic`（窗口数 ≥ 3 且严格递增为 1），用例断言
  不严格递增（18 §8.8 "GPU 池高水位不单调上升"）。
- 重连：每次离开页面（切世界、结束）前把当前页面的计数累加，`unexpected_reconnects` 为全部页面之和。
- 帧节奏：soak 结束、指标写入后，同一浏览器 `runFlight60({city: 'shenzhen', scene: 'full', source: 'live'})`，其快照为 `snapshot.json`，
  harness 的 `frameFull()` 指标由此提取（D1-AC-03b 口径，live 机群即 soak 的 S1 + 200 架）；soak 页面的快照另存 `snapshot-soak.json`。
- 采样序列写 `soak-series.json`（pid、重启、各窗口 GPU 池高水位、逐 5 s 样本）。
- 超时由 `minutes + 5` 改为 `minutes + 8` min（flight60 约 75 s 加揭开）；harness 的 `timeoutS` 2400 s 足够。

## 5 自测（不作判定）

### 5.1 诊断短跑（不开浏览器）

见 2.2 的 r2、r3：同一剧本与客户端，api RSS 斜率 2.07 → 0.12 MB/min（−94%）。按 r3 折算 30 min soak：首 5 min 中位（约第 2.5 min）
到末 5 min 中位（约第 27.5 min）之间约 4.2 万条事件 × 约 35 B（压缩块 + 索引）≈ 1.5 MB，加指标窗口约 0.2 MB，合计约 +1.8%（95 MB 基数）。

### 5.2 单元与功能

见第 9 节。

### 5.3 harness 快速自测（排他锁内）

持锁 06:53:08–07:02:59（591 s，< 10 min），含开跑前等 load ≤ 4 约 70 s。方式：用 harness 的 `runCase` 直接跑注册表的 `soak` 用例
（perf profile 钉核、生产构建 `apps/web/dist`、`minutes` 覆盖为 4，首尾窗口取 `minutes / 3` = 80 s；脚本 `runs/fx2r3-gateway/diag/tools/soak_quick.mjs`，
关闭了 PR-11 对页面异常的重跑以控制持锁时长）。证据 `runs/fx2r3-gateway/quick-soak-1/soak/run-1/`。

- 用例本身通过（322.6 s）：soak 4 min 采样 45 次，随后 flight60 正常完成并写出 `snapshot.json`。
- 但 harness 以 PERF-E008 判本次无效并开始重跑，本包随即中止以免超时：sim-core 在后端预热阶段被监管 SIGKILL 两次（`restart_count` 1、2，
  均在用例开始采样之前），日志中的 faulthandler 栈停在 numba 编译（`numba/core/analysis.py`），即主循环里触发了 JIT、心跳超时。原因是
  sim 工作包正在修改内核，numba 缓存失效后首次运行在主循环里编译；第 3 次启动后正常。不是本区域的问题，验收阶段的干净代码与预热缓存下
  不应出现（8.3）。
- 本次数据（不作判定）：

| 指标 | 值 | 说明 |
|---|---|---|
| `rss_growth_pct` | 1.51% | api 93.87 → 95.29 MB（首尾 80 s 中位）；sim-core 244.21 → 244.54 MB（+0.14%）。api 的 +1.4 MB 是第 100 s 切换到上海世界时的一次性台阶（静态文件服务的线程与缓冲），此后 90 s 不变（95.3 MB）；修复前同样 4 min 按 2 MB/min 应增长约 8 MB |
| `heap_growth_pct` | 8.31% | JS 堆，含一次切世界 |
| GPU 池高水位（各 80 s 窗口） | 21922、21922、21922 点 | 不单调上升；`gpu_pool_hw_growth_pct` 0 |
| `unexpected_reconnects` | 0 | 三个页面累加 |
| 结束后 flight60（整景、live、S1 + 200 架） | p50 50.0、p95 66.7、p99 100.0 ms；> 50 ms 15.18%；> 100 ms 0.31%；最大 483 ms | 运行内有其他工作包的负载（开跑前 load 3.5，此前 8.8）；PerfGovernor 停在第 7 步、点数 1 万。与第 2 轮 09a（n200）同型，见 8.2 |

## 6 改动文件清单

| 文件 | 所有者 | 改动 |
|---|---|---|
| `python/awr/api/rt/events.py` | M11 | `EventRing`（编码一次、封块压缩、按块淘汰、8 MiB 上限、索引过滤）、`encode_event`；`EventIngest` 改用之（3.1） |
| `python/awr/api/rt/session.py` | M11 | `pending_events` 为字节；`flush_events` 拼接；`event_ok(kind, level)`（3.2） |
| `python/awr/api/rest/sys.py` | M11 | `GET /api/events` 直接拼接响应体（3.2） |
| `python/awr/api/rt/metrics.py` | M11 | 指标窗口列存（3.3） |
| `python/awr/api/rt/gateway.py` | M11 | inspect `event_ring.count/bytes`（3.4） |
| `python/awr/api/rt/inspect.py` | M11 | 文档串同步（3.4） |
| `python/awr/runtime/supervisor.py` | M11 | 退出检测不依赖管道关闭、子进程退出后回收进程组残留后代、停止时等回收（3.5） |
| `tests/rt/test_event_ring.py`（新增） | M11 | EventRing 单元用例 7 个 |
| `tests/runtime/test_supervisor.py` | M11 | 新用例 `test_descendants_reaped_after_child_killed`（3.5） |
| `tests/runtime/fake_child.py` | M11 | `--grandchild PIDFILE [--grandchild-ignore-term]`：派生同进程组后代 |
| `tests/rt/test_events.py` | M11 | 环截断用例改用 `set_capacity(8)` |
| `apps/web/perf/soak.spec.ts` | M16 用例（越区：D1-AC-29 的测量口径，4.1） | RSS 中位、GPU 池、重连累加、结束后 flight60（4.2） |
| `docs/17-接口与实时协议规范.md` | 文档 | §6.12 EventRing 存储与内存上界；§4.3.14 inspect 的 `event_ring` 字段 |
| `docs/18-性能与测试方案.md` | 文档 | §8.8 soak 行的测量口径 |
| `docs/modules/M11-实时网关PRD.md` | 文档 | M11-FR-012（退出检测与后代回收）、M11-FR-065、§6.3.4 EventRing 行、§6.5 参数表 EventRing 行 |
| `docs/19-部署与运维说明书.md` | 文档 | §4.2 状态机"进程退出"行：不依赖管道关闭、回收残留后代 |
| `docs/modules/M16-演示数据剧本与流畅性测试PRD.md` | 文档 | §6.12 soak 行 |
| `docs/impl/FX2-R3-gateway-报告.md` | 文档 | 本报告 |

## 7 规格与文档变更

- 没有改动任何阈值（D1-AC-29 的堆 ≤ 20%、RSS ≤ 10%、重连 0 等维持；本包也不建议放宽：api 的增长是缺陷，修复后折算约 2%）。
  没有新增 ADR：依 03 §1.3，参数与口径的变化在对应规范与 PRD 中修订：
  - 17 §6.12、M11 §6.3.4、M11-FR-065、M11 §6.5 参数表：EventRing "65,536 条"改为"至少保留最近 65,536 条（按 256 条一块淘汰）"，新增已封块
    压缩字节上限 8 MiB（本文设定）。依据：2.2 的实测（dict 约 1.2 KB RSS/条、满环约 80 MB；压缩后 soak 事件约 30 B/条、满环约 2–3 MB）。
    M11 §6.3.4 原本就把环项设计为 `json_bytes`，实现此前偏离为 dict，本次回到设计并加压缩。协议字段、顺序、resume 与 410 语义不变。
  - 17 §4.3.14（`GET /api/rt/inspect`）：`event_ring` 增加诊断字段 `count`、`bytes`。
  - M11-FR-012、19 §4.2：supervisor 退出检测不依赖 stdout 管道关闭（20 ms 查退出码）；子进程退出后回收其进程组的残留后代
    （SIGTERM，1 s 后 SIGKILL），停止时等回收完成。依据：3.5 的复现（去掉轮询时 kill -9 后进程一直停在 RUNNING）与第 2 轮 4.1b。
  - 18 §8.8 soak 行、M16 §6.12 soak 行：soak 的测量口径（RSS 首尾 5 min 中位、GPU 池窗口高水位、重连按页面累加、结束后整景 flight60 测帧节奏）。

## 8 遗留问题与建议

### 8.1 提给 sim（剧本与命令校验，影响 D1-AC-29 的被测负载）

`scenarios/soak-shenzhen.json` 的 ladder 四组 `mission.params.turns = 120`（ADR-062：1.2 m/s、一圈 15.7 s、约 31 min），M10
`python/awr/sim/mission/engine.py:822` 原样把 `turns` 放进 orbit 命令，而 `python/awr/sim/core/command.py:807–809` 的校验是
`0 ≤ turns ≤ 100`，于是 200 架的 orbit 命令全部以 110 被拒，航迹 `WORKING → SUSPENDED` 后反复重试（2.3）。后果：soak 测的不是
"200 架环绕"的稳态；事件率约 28 条/s，其中 85% 来自这一往复（正常应在每秒几条）。建议二选一：把 orbit `turns` 上限放到 ≥ 120
（与 ADR-062 一致，需同步 17/12 的命令参数表），或把剧本改为 `turns ≤ 100` 并相应调整速度或改用 `turns = 0`（不限圈数）加 `time_limit_s`；
同时建议 `test_scenarios_static.py` 增加"剧本生成的命令参数满足命令校验"的断言。ladder 剧本（`ladder-shenzhen`）同样检查。

### 8.2 帧节奏子项取决于 D1-AC-03b、09a

soak 结束后的 flight60 是整景、S1 + 200 架，口径与 D1-AC-09a（n200）相近；第 2 轮 09a 的 > 50 ms 14.05%、最大 617 ms 不满足，
03b 的 > 50 ms 10.57%、最大 433 ms 不满足（web-engine、web-ui）。在它们修复之前，soak 的帧节奏子项预计不满足，这不是 soak 本身的退化；
判读时与同轮 `flight60.shenzhen.full`、`ladder.front.n200` 对照（相同即无退化）。

### 8.3 其他

- 30 min 的正式 soak 需要在验收阶段跑（本包只在锁内跑了 4 min 的快速自测）。验收时建议同时看 `soak-series.json` 的 api RSS 曲线是否在
  前 10 min 后走平（3 次切世界都在前 12 min 内，各带一次约 1 MB 的静态文件服务台阶）。
- 提给 sim：快速自测中 sim-core 在后端预热阶段因主循环内的 numba 编译超过心跳阈值被 SIGKILL 两次（5.3）。内核改动之后的第一次运行
  都会这样；建议 sim 工作包在交付前预热 `NUMBA_CACHE_DIR`（或在 sim-core 就绪之前完成全部内核的编译），否则验收阶段第一个 live 用例会
  以 PERF-E008 判无效。
- 第 2 轮 4.1b 的孤儿 plan-pool 进程：supervisor 一侧已在 3.5 处理（子进程退出即回收其进程组）。仍建议 sim 核对为什么已设置 PDEATHSIG
  的工作进程仍会成为孤儿（Linux 的 PDEATHSIG 绑定派生它的线程而不是进程；另有 resource_tracker 本身不设该信号）：本包诊断期间看到其他工作包
  的 ci 运行（修复前的代码）在 06:13 与 06:43 各留下 1 对孤儿。supervisor 本身被 SIGKILL 时（不经 stop）子进程各在独立会话中，仍会成为孤儿，
  这由外层（harness 的 `stopBackend` 先 SIGTERM、systemd）负责。本包自己的运行没有留下进程（已核对）。
- `make lint` 在本包最后一次修改后的状态见第 9 节；失败项都在其他工作包进行中的文件里。

## 9 测试

功能测试都在共享锁下运行（`flock -s runs/.perf.lock`），python 代码最后一次修改之后重跑：

| 范围 | 命令 | 结果 |
|---|---|---|
| EventRing 单元 | `pytest tests/rt/test_event_ring.py` | 7 通过 |
| rt、runtime、recorder、contracts、ops，加 e2e S1 与 free（深圳） | `pytest -m "not perf" tests/rt tests/runtime tests/recorder tests/contracts tests/ops "tests/e2e/test_scenarios.py::test_s1" "tests/e2e/test_scenarios.py::test_free[shenzhen]"` | 611 通过（3 个 perf 用例按规则排除）；e2e 两例经真实 supervisor 与 `/api/events` 轮询跑完 S1（覆盖 REST 事件拼接与 supervisor 改动） |
| supervisor 回归的反证 | 同一新用例，把 `EXIT_POLL_S` 改为 100 s（等价于原 `await proc.wait()`） | 失败："api 未进入 BACKOFF（当前 RUNNING）" |
| ruff | `ruff check python/awr/api python/awr/runtime python/awr/recorder tools/bench/ipc tests/rt tests/runtime tests/recorder` | 通过 |
| oxlint、tsc | `npx oxlint --type-aware perf/`、`npx tsc --noEmit` | 通过 |
| make lint | `make lint` | 本区域全部通过；整体失败于其他工作包进行中的改动（本包最后一次运行在 07:40）：ruff 4 处（`python/awr/environment/kernels_rows.py:50`、`python/awr/sim/fleet/kernels_watch.py:191`、`python/awr/sim/mission/runtime.py:444`、`python/awr/sim/sensors/kernels_gimbal.py:87`），oxlint 4 处（`apps/web/src/viewport/dev/featMatrixWgpu.ts` 第 111、381、422、571 行 no-floating-promises），lint-lf 2 处（`apps/web/src/app/boot/WarmStage.tsx:102`、`:115`）；其余 lint 工具全部 ok |
| harness 注册表 | `node perf/harness/run.mjs --check`（make lint 内） | registry OK: 106 cases |
