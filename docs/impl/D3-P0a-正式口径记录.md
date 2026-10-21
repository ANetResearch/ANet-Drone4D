# D3 P0a 正式口径记录（B10）

| 项 | 内容 |
|---|---|
| 任务 | B10 P0a 正式口径：L1 + L2 × 3 个基线 × 10 个秘密种子 × 2 次重复（D3 收尾与封板，批次 B07，执行环境 dmax GPU 2） |
| 验收编号 | D3-AC-56（最小基准闭环端到端）为主；同一轮次为 D3-AC-17（重复运行逐字节一致）、D3-AC-18（`evaluation.json` 逐字节重算、评测耗时、运行清单）、D3-AC-57（榜单值由存储分量重算、两次计算逐字节一致）、D3-AC-58（基线在 L1、L2 上可运行、动作流逐字节一致、区分度）提供正式运行 |
| 轮次 | 第 2 轮 `b10-formal-r2`（本记录的判定依据）：正式轮次 2026-10-21 05:13:21 至 13:45:18 UTC，13:46 UTC 发布到内部工作区；探针 ident 至 ident7（§2.1.3）在 02:31 至 04:59 UTC；第 1 轮 `b10-formal`（2026-10-20 11:26:43 至 2026-10-21 02:00:46 UTC）因墙钟条款不通过、机型未映射与 L2 无 report 由第 2 轮取代，结果保留在 §9 |
| 依据 | AWR-05（docs/05）v1.6：§10 P0a 退出标准 5、§11.2 D3-AC-12、17、18、56、57、58 原文；ADR-161（观测白名单与种子保密）、ADR-175（P0a 最小基准闭环）、ADR-182、ADR-191（分轨 × 赛道、无合成权重）、ADR-194（封板范围）、ADR-196（Detour 后端与正式磁带守卫）、ADR-198（LiDAR 逐点时刻的动态实体）；2026-10-18 用户决定（性能数字只在独占卡上计；D3-AC-56 为共享卡原型条款） |
| 代码基线 | `d3-dev` HEAD `2e1a845` 加本任务的工作树改动（§2，未提交）；dmax 代码根 `/data/anet-drone4d-d3/d3dev/b10r2`（独立子目录，启动时由 `run_mbl_e2e.sh` 一次同步，执行期间不再改动） |
| 镜像 | 与第 1 轮相同（SDK、gz 与 Agent 镜像内容未改动）：宿主 `d3-compile:b10`（sha256:2002e8265482…，含 Node 22.12、navsvc 依赖与 warp-lang 1.17.0）；Agent `d3-agent-py:b10`（sha256:977aa7e1a7ec…）；gz `d3-gz:b10`（sha256:e8549393d460…）；内部提交 `d3-example-close-track:b10`（sha256:fded888a6ead…）。Python 代码（Env、传感器、评测）由代码根挂载，不在镜像内 |
| 世界与事件 | `oryahovo-crop1` contentVersion `7d8c8b895e55`（dmax `/data/anet-drone4d-d3/d3dev/worlds`）；`crop-handoff-001` v1 由本轮 strict 编译（Detour 后端），磁带 `content_sha256 = b1285649605e08efe8445da1e73f23a1a22f8eafaac310210e220706f6a08e54`（与第 1 轮、K06 相同）；3600 tick、43 个演员、事件类型 `rendezvous_handoff` |
| 硬件 | dmax：2 × Xeon Gold 6326（64 线程），NVIDIA A40 48 GB（GPU 2），驱动 580.95.05，Docker 29.1.4；共享生产机，执行期间系统 1 分钟负载抽样 8 至 13（64 线程）；宿主容器 `--cpus 24 --memory 64g` |
| 结论 | 第 2 轮 D3-AC-56 **通过**：`mbl_e2e.json` 10 项判定全部通过，`formal = true`、`formal_passed = true`；单 Run 墙钟比 L1 ≤ 0.852、L2 ≤ 2.491、内部提交 ≤ 2.240（阈值 3.0）；机队为 3 架 `p600_bench`；L2 基线 report 被接受。本轮为 D3-AC-17、18、57、58 提供的正式运行条款全部通过（§6）。原型榜与内部提交已发布到内部工作区（§5.3）。样例包（B20）与 Playwright `lab/mbl`（F09）不在本任务 |

## 0. 摘要

`mbl_e2e.py` 正式口径（`formal = true`）一次跑完 7 个阶段：120 个正式 Run 全部首次 COMPLETED，内部提交 DONE 并上榜，报告 `out/b10-formal-r2/mbl_e2e.json` 的 10 项判定全部通过。

| 项 | 结果 | 阈值或条款 | 判定 |
|---|---|---|---|
| 一键闭环 | world → compile → lockstep → runs → board → submit → report，无人工介入 | 一条命令 | 通过 |
| 正式 Run | 120 / 120 COMPLETED（L1、L2 × 3 基线 × 10 秘密种子 × 2 重复），0 次平台重跑 | 全部 COMPLETED | 通过 |
| 机型 | 120 个正式 Run 与 10 个提交 Run 的 `tier_info.vehicle` 均为 `p600_bench`（3.935 kg、1500 rad/s） | 3 架 `p600_bench` | 通过 |
| gz 锁步 | 预检、120 个正式 Run 与 10 个提交 Run 共 131 个均满足 `iterations = 25 × (n_ticks + 预滚)`，0 次重启 | Realistic 档锁步 | 通过 |
| 白名单审计 | 132 个 Run（正式、预检、冒烟、提交）共 1,405,992 条消息，未知字段 0、schema 违规 0、JPEG 段 0、LiDAR 标记 0、解码 0 | 未知字段 0 | 通过 |
| 单 Run 墙钟比 | L1 0.584–0.852；L2 2.120–2.491（中位 2.310）；内部提交 2.129–2.240 | ≤ 3.0 | 通过 |
| `evaluation.json` 重算 | 120 / 120 逐字节一致，两次重算一致 | 逐字节一致 | 通过 |
| 重复一致 | 60 / 60 对重复的 `pose`、`obs/*`、`act/*` 流逐条一致（L2 使用 Warp LiDAR） | 逐字节一致 | 通过 |
| 榜单重算 | 两个榜单版本各连算两次，`board.json` 与 `details.json` 与已发布文件逐字节一致；评测行与评测文件一致 | 逐字节一致 | 通过 |
| 基线区分度 | L1、L2 上 `track_share` EC 1.000 均高于 `coverage_scan`（0.400、0.325）；L1 四项、L2 三项（EC、AO、CE）存在不含 0 的配对差值区间 | EC 更高；至少一项不全部两两并列 | 通过 |
| L2 report | 722 条全部被接受并附证据（第 1 轮 738 条全部被拒，§2.3）；L2 EU 0.018、0.036 | 基线 report 附证据 | 通过 |
| 原型榜 | `p0a-formal-r2-l1`、`p0a-formal-r2-l2` 各 3 个参考行；L2 另有内部提交条目 `d3-internal/sub-olenynk5qm7amst4`；配置标注 `prototype = true`、`gpu_class = shared`，工作区页面标题为"原型排行榜" | 参考行 + ≥ 1 个内部提交；共享卡标注原型 | 通过 |
| 秘密种子 | 只在 evalstore（0600）与服务端榜单配置中；785 个文本产物检索命中 0；120 份运行清单 `seed_commit` 与 HMAC 复算一致 | 不泄露 | 通过 |

## 1. 执行方式与配置

### 1.1 一条命令

```
CPUS=24 GPU=2 REPO_DIR=/data/anet-drone4d-d3/d3dev/b10r2 IMAGE=d3-compile:b10 tools/deploy/dmax/run_mbl_e2e.sh b10-formal-r2
```

在本机仓库根目录执行（脚本以自身位置为仓库根，启动时同步代码到 `REPO_DIR`）。宿主容器 `d3-board-mbl-b10-formal-r2`（`--gpus device=2`、`--cpus 24`、`--memory 64g`、`--network none`）内运行一次 `tools/bench/d3/mbl_e2e.py --config configs/bench.d3/mbl-formal.yaml --out out/b10-formal-r2 --data-root benchroot --worlds-root /data/anet-drone4d-d3/d3dev/worlds`。gz 实例（`d3-gz-b10-formal-r2-*`，`--cpus 2 --memory 2g`，无 GPU、无网络）与 Agent 隔离容器（`d3-agent-b10-formal-r2-*`，std 资源档：1 核、2 GB、seccomp `d3-agent-v1`、无网络、`OMP_NUM_THREADS=1`）经 docker 套接字作为兄弟容器启动，每个 Run 结束即删除。宿主容器的 CPU 配额由第 1 轮的 16 提高到 24（L2 每个 Run 的宿主进程含主循环、LiDAR 工作进程、真值与观测记录线程，3 路并行；配额不影响输出）。

### 1.2 配置 `configs/bench.d3/mbl-formal.yaml`

| 键 | 取值 | 说明 |
|---|---|---|
| `round` | `b10-formal-r2` | Run 标识前缀，容器名由此派生 |
| `groups` | L1、L2 两组，run_spec 为 `configs/bench.d3/crop-handoff-001.{l1,l2}-3uav.run_spec.json`，`jobs` 均为 3 | 3 机、360 s、`ranked = true`、`noise = realistic`、通信 `fspl-nlos@1` |
| `fleet_vehicle` | `p600_bench` | 机队机型改为排行榜标准轨机型（§2.2）；第 1 轮为 `null`（x500） |
| `lidar_backend` | `warp` | L2 网格 LiDAR 使用 M25 GPU 产品路径（§2.1.2）；第 1 轮为 Open3D CPU 参考 |
| `baselines` | `static_guard`、`coverage_scan`、`track_share` | 参考基线，平台镜像以 `--baseline <name>` 启动 |
| `seeds` × `repeats` | 10 × 2 | 秘密种子 `secrets.randbits(63)`，只存 `out/b10-formal-r2/evalstore/secrets.json`（目录 0700、文件 0600）与服务端榜单配置；第 2 轮重新抽取 |
| `physics` | `realistic` | gz 锁步，物理 250 Hz，每 tick 25 步 |
| `wall_ratio_max` | 3.0 | 单个正式 Run 墙钟 ≤ 仿真时长 × 3（共享卡，原型） |
| `board` | 版本 `p0a-formal-r2-l1`、`p0a-formal-r2-l2`，当前版本 L2，`n_boot = 10000` | 配置标注 `track.prototype = true`、`gpu_class = shared`；第 1 轮的 `p0a-formal-l1`、`p0a-formal-l2` 保留为历史版本 |
| `submission` | `d3-example-close-track:b10`，L2 组，`worker = inline` | 扫描 → 冒烟（20 s，种子 0）→ 正式评测（10 个秘密种子各 1 次）→ 评分 |

配置指纹 `7a304add9d92`（`lidar_backend`、`fleet_vehicle` 进入指纹，续跑不能换后端或机型；组内并行度不进入指纹）。数据根为本轮专用的 `/data/anet-drone4d-d3/d3dev/b10r2/benchroot`。

## 2. 第 1 轮之后的修正（审查意见逐条）

第 1 轮审查给出 4 条意见。前 3 条属实，均已修正并以第 2 轮重跑验证；第 4 条（代码、测试与风格无问题，共享文件已报告）无须处理。

### 2.1 意见 1：D3-AC-56 墙钟条款不通过（属实）

第 1 轮 L2 组 60 个 Run 墙钟比 3.66–4.73（中位 4.11），内部提交 4.32–4.51。剖析（py-spy，`--idle --threads`，50 Hz）显示 Env 主循环单线程串行：等待 Agent 动作约 23%（Agent 为 std 资源档单核，基线每步计算约 110–125 ms，是不可压缩的下限），其余为 M25 相机渲染与读回、网格 LiDAR 等待、观测记录（整段规范 JSON 与 base64）、IMU 合成、可见度统计与 gz 推进依次执行。修正分两类：

#### 2.1.1 主循环提速（输出逐字节不变）

| 改动 | 位置 | 做法 | 一致性依据 |
|---|---|---|---|
| LiDAR 工作进程池 | `awr/agentsdk/env/l2.py` `_LidarPool`、`L2Config.lidar_workers` | CPU 参考每架机一个工作进程（至多 4 个），作业 i 交给进程 i mod n，结果按机合并 | 扫描只取决于帧序号、位姿节点与种子，不跨帧、跨机保留状态；`test_lidar_worker_process_matches_in_process` |
| 相机先提交后取回 | `l2.py` `_cameras`、`_setup` | 先对全部机体 `RasterCamera.submit`（持久映射 PBO 读回环，6 个槽位），再按机序 `result` → 效果链 → 编码；同步 `render` 保留为回退 | `test_async_render_path_matches_sync_and_obs_hash`（观测摘要、交付帧、`truth/l2.jsonl` 与同步路径逐字节相同） |
| 真值可见度归约后台化 | `l2.py` `_deferred_rows`（线程 `l2-truth`），`core.py` `_flush_truth` | 可见度行在后台线程计算，读回槽位在计算后归还；每次推进之前与关闭时按序写出 | 真值层写入顺序与不推迟时相同（同一测试比较 `truth/l2.jsonl`） |
| 观测记录后台化 | `core.py` `async_obs_record`、`_flush_obs`；`remote.py` | 隔离容器运行（`run_remote_episode`）时观测记录在线程 `env-obs-rec` 中写出，与 Agent 计算重叠；下一次 `step` 之前汇合 | 观测交付后只经线协议编码、不再修改；同一测试比较 `obs.sha256` 与 raw.mcap 各通道消息数 |
| 观测摘要增量哈希 | `awr/agentsdk/env/recorder.py` `canonical_sha256` | 以随机占位符生成 JSON 骨架，对文本片段与 base64 块增量求 sha256，不拼接整段规范 JSON；占位符计数不符时回退整段编码 | `test_canonical_sha256_equals_full_encoding`（与整段 `canonical_json` 的 sha256 相同） |
| IMU 偏置增量缓存 | `awr/sensorsvc/imu.py` `BiasWalk` | 偏置随机游走按容量倍增缓存累加和，续接段以上一段末值为首项 `cumsum` | `test_bias_walk_cache_is_bitwise_equal_to_full_sum`（与整段重算逐位一致） |
| IMU、GNSS 与点云等待重叠 | `l2.py` `sense_all` | IMU、GNSS 在等待 LiDAR 结果期间合成（计数器式随机数按键生成，不依赖点云） | 帧内容与键序不变；探针 ident6 与 ident5 逐字节一致（§2.1.3） |
| 位姿查找 | `l2.py` `_Track.state_at`、`trim_before` | 列表上的 `bisect` 代替 `np.searchsorted`（避免每次把列表转数组） | 同一下标 |

#### 2.1.2 L2 网格 LiDAR 使用 M25 GPU 产品路径（点云按 D3-AC-12 判定）

剖析显示 CPU 参考（Open3D）的单机扫描在 3 路并行的正式负载下约 70–80 ms（工作进程忙 25.6%、每 tick 约 290 ms），微基准中 3 个进程同时扫描约 37 ms；以 CPU 亲和把每个进程限制在 1、2、4 核对耗时无改善；LiDAR 等待约占主循环 13%。`awr.sensorsvc.lidar.warp_mesh` 是 M25 的 GPU 产品路径（M25-FR-070、D3-AC-12，S12 实现，S20、S21 的标准轨在线测量即用此路径），与 CPU 参考同一算法与随机键，内核无浮点原子，每帧结果只取决于该帧输入与帧键、与批大小无关（M25-AC-012）。本任务把它接入 Env：

- `L2Config.lidar_backend`（`cpu` | `warp`，函数 `lidar_backend`）：未给出时取 Env 专用变量 `AWR_ENV_LIDAR_BACKEND`，再缺省 `cpu`；Env 不读 sensorsvc 的 `AWR_LIDAR_BACKEND`（其缺省 `auto` 会随机器是否有 GPU 改变点云）。`warp` 时一个工作进程每 tick 一次 `scan_batch` 扫描全部机体（`_scan_jobs`），运行清单记录 `l2.lidar_backend`。
- `mbl_e2e.py` 配置键 `lidar_backend`（缺省 `cpu`，进入指纹），`Round` 把它写入 `AWR_ENV_LIDAR_BACKEND`，基线与内部提交的全部 Run 使用同一后端；每个 Run 的记录带 `lidar_backend`。
- 实测（dmax GPU 2，与 Env 工作进程相同的调用 `_scan_jobs`，探针 ident5 的轨迹，3 机 × 40 tick）：Warp 每 tick 3 机 10.4 ms（p90 23.1 ms），CPU 参考 129.8 ms（逐机串行）；120 帧 1,824,474 个点，两后端的点集（逐帧点数与点时刻）完全相同，坐标最大差 5.7e-6 m（D3-AC-12：同一表面 p99 ≤ 1 mm、最大 ≤ 1 cm）。
- 对 Agent 观测的影响只在 LiDAR 点坐标的浮点末位：探针 ident7（Warp）与 ident6（CPU 参考）同一种子的动作流、真值层、位姿、report 与四项指标逐字节相同，只有 `obs.sha256` 不同。

#### 2.1.3 探针测量

L2 组 3 路并行（宿主 `--cpus 24`，3 个基线同时运行，360 s，dmax GPU 2），墙钟比为 `run_job` 计时（含 gz 与 Agent 容器启动）÷ 仿真时长。种子取第 1 轮正式种子集中的序号 0 或 5（序号 5 为第 1 轮 `static_guard` 最慢的种子之一）。

| 探针 | 种子序号 | 机型 | 代码状态 | coverage_scan | static_guard | track_share | 输出一致性 |
|---|---|---|---|---|---|---|---|
| 第 1 轮正式 | 0 | x500 | 第 1 轮 | 4.13 | 4.09 | 4.03 | — |
| ident2 | 0 | x500 | 2.1.1 中 LiDAR 进程池、相机先提交、偏置缓存、增量哈希、bisect | 2.56 | 2.79 | 2.81 | 与第 1 轮正式 Run 的 `obs.sha256`、`acts.jsonl`、`truth/l2.jsonl`、`truth/fleet.npz`、`pose_sha256` 与 `evaluation.json`（除 `raw_mcap_sha256`）逐字节相同 |
| 第 1 轮正式 | 5 | x500 | 第 1 轮 | 3.67 | 4.66 | 4.18 | — |
| ident3 | 5 | p600_bench | ident2 + 机型映射（§2.2） | 2.57 | 2.84 | 2.76 | 机型改变物理，不对拍 |
| ident4 | 5 | p600_bench | ident3 + 守卫帧证据（§2.3） | 2.56 | 2.93 | 2.73 | L2 report 被接受 |
| ident5 | 5 | p600_bench | ident4 + 真值归约与观测记录后台化 | 2.49 | 2.81 | 2.92 | — |
| ident6 | 5 | p600_bench | ident5 + IMU、GNSS 与点云等待重叠 | 2.46 | 2.75 | 2.66 | 与 ident5 逐字节相同（观测、动作、真值、report、位姿、评测） |
| ident7 | 5 | p600_bench | ident6 + Warp LiDAR（§2.1.2） | 2.24 | 2.41 | 2.32 | 与 ident6 只有 LiDAR 点坐标不同（见上） |

`raw.mcap` 的字节差异只在内嵌的编译报告元数据（`host`、`wall_ms`、`rss_mb`、`created_at`）与文件尾统计，因此比较 `evaluation.json` 时去掉 `raw_mcap_sha256`。ident6 的主线程剖析（py-spy 60 s，2793 个样本）：等待 Agent 动作 49.6%、等待 LiDAR 12.8%、相机 12.5%、gz 推进 10.4%、观测发送 5.5%、IMU 3.4%；Warp 后端去掉了 LiDAR 等待，其余以 Agent 计算为下限。

### 2.2 意见 2：`p600_bench` 未映射，机队为 x500（属实）

| 改动 | 内容 |
|---|---|
| 机型目录 | 新增变体 `vehicles/d3/variants/p600_bench.yaml`（基机型 `p600_mid360@2.0.0`；挂载 eo→`cam0`、倒装 MID-360→`lidar0`、`gnss0`、`imu0`、热成像 `therm0` 取 `vehicles/d3/mounts/p600_bench_lwir.yaml`（lwir640@1.0.0，13 mm，0.11 kg）；去掉 `nir_ill` 以满足 MNT-1 挂载质量规则）与目录条目 `vehicles/d3/catalog.d3.json`（tags `bench_standard`、`thermal`）。派生值：整机质量 3.935 kg、轴距 419.9 mm、推力 1661.4 g、悬停油门 0.502、推重比 1.99、续航 0.165 h，与 AWR-20 §15.16.1 一致（`tests/d3/catalog/test_overlay.py::test_p600_bench_derived_values_and_rig`） |
| gz 映射 | 新模块 `awr/phys/gz/vehicles.py`（原实现在 `tools/bench/d3/gz_lockstep.py`，F-09，该脚本改为从本模块导入）：`p600_bench` 在 `x500_d4d` 几何上换用质量 3.935 kg、惯量 J_x500 × (m / 2.064) × (0.300 / 0.246)²、最大转速 1500 rad/s（k_f 不变）；`quad_for_fleet` 拒绝混合机队；`derive_model_sdf` 生成派生模型、`apply_vehicle` 改写 run.sdf 的模型引用与 4 × 机队规模个 `maxRotVelocity`（替换次数不符时报错） |
| Env 接入 | `awr/agentsdk/env/core.py` `_GzTier.create`：按机队机型取映射参数，映射机型的派生模型写入 `<run_dir>/phys/models/<模型名>/` 并只读挂载到 gz 实例 `/opt/d4d/models/<模型名>`（本机进程时经 `GZ_SIM_RESOURCE_PATH`），`tier_info.vehicle` 记录映射参数；x500 与未登记机型行为不变 |
| 部署 | `run_mbl_e2e.sh` 同步 `native/gz/models/x500_d4d`（派生模型的基础） |
| 测试 | `tests/d3/phys/test_gz_vehicles.py`（质量与最大转速与目录同源、推重比、惯量缩放、机型引用解析与混合机队拒绝、`_GzTier` 写模型、挂载与改写 run.sdf） |

第 2 轮 120 个正式 Run 的运行清单 `tier_info.vehicle.name` 全部为 `p600_bench`（L1、L2 各 60 个），锁步核对的 gz 迭代数与第 1 轮相同（`iterations = 25 × (3600 + 20)`），派生模型由 `apply_vehicle` 写入每个 Run 的 run.sdf。

### 2.3 意见 3：L2 基线不发 report，L2 EU 为 0（属实，原因在 Env 守卫）

L2 基线实际发出了 report，但全部被 Env 守卫拒绝：第 1 轮 L2 组 60 个 Run 的动作回执中共 738 条 report 回执，全部为 `rejected`（663 `evidence_unknown`），L1 组为 0 条。原因是 `ActionGuard` 只从 L1 检测（`saw(dets)`）登记已交付的 (传感器, 帧序号)，L2 的框证据（`sensor`、`frame_seq`、`bbox_xywh`，无 `det_id`）引用的帧从未登记。修正：`ActionGuard.saw_frames(frames)` 登记 L2、L2-depth 交付的全部传感器帧，`BenchEnv._observe` 在 L2 时调用（`awr/agentsdk/env/guard.py`、`core.py`）；测试 `tests/d3/env/test_l2.py::test_l2_frame_evidence_reports_are_accepted`。探针 ident6 中 3 个基线的 L2 report 分别为 5、17、12 条全部被接受。第 2 轮 L2 组 60 个 Run 共 722 条 report 被接受（L1 组 704 条），全部附证据；L2 上 `static_guard`、`track_share` 的 EU 点估计为 0.018、0.036（§3.3）。

### 2.4 本任务的测试

本机（`.venv-d3`，无 CUDA 与 warp-lang）：`PYTHONPATH=python .venv-d3/bin/python -m pytest -q tests/d3/env tests/d3/baselines/test_act_reports.py tests/d3/bench/test_platform_audit.py tests/d3/bench/test_recorder.py tests/d3/phys/test_gz_vehicles.py tests/d3/phys/test_gz_lockstep.py tests/d3/phys/test_gz_export_checks.py tests/d3/sensorsvc/test_imu.py tests/d3/catalog/test_overlay.py tests/d3/slam/test_slam_traj.py tests/d3/slam/test_rig.py tests/d3/rerun/test_online_vs_offline.py tests/d3/sensorsvc/test_mesh_lidar_gpu.py -p no:warnings`：230 通过、16 跳过（warp 依赖 8、平台 7、opt-in 1）。dmax GPU 2（`d3-compile:b10` 容器，warp-lang 1.17.0）：`tests/d3/sensorsvc/test_mesh_lidar_gpu.py`、`tests/d3/sensorsvc/test_lidar_gpu_cpu_diff.py` 23 通过、2 跳过（数据）。`ruff check python tests tools` 与 `check_py_imports.py` 通过。新增与修改的测试：

| 测试 | 内容 |
|---|---|
| `tests/d3/env/test_l2.py` | 异步渲染路径与同步路径逐字节相同、增量摘要等于整段编码、L2 框证据 report 被接受、LiDAR 后端只由配置或 Env 专用变量确定、Warp 批扫描与逐帧扫描逐机相同、运行清单记录后端 |
| `tests/d3/sensorsvc/test_imu.py` | 偏置增量缓存与整段累加逐位一致 |
| `tests/d3/phys/test_gz_vehicles.py` | 机型映射（§2.2） |
| `tests/d3/catalog/test_overlay.py` | `p600_bench` 派生值与 rig |
| `tests/d3/env/test_mbl_e2e.py` | 锁步不变量计预滚、种子 0 不脱敏（第 1 轮）；正式配置：轮次 r2、机队 `p600_bench`、榜单版本、`lidar_backend = warp` 进入指纹、非法后端报错 |

## 3. 正式轮次（第 2 轮）

### 3.1 阶段

| 阶段 | 开始（UTC） | 结束（UTC） | 耗时 | 结果 |
|---|---|---|---|---|
| world | 05:13:21 | 05:13:23 | 1.2 s | `validate_world`：84 条规则，0 error，0 warning |
| compile | 05:13:23 | 05:13:38 | 15.4 s | strict + ranked：门 1 warn、门 2 pass、门 3 warn，状态 compiled |
| lockstep | 05:13:38 | 05:14:03 | 25.2 s | 预检 `track_share`、L1、种子 0、20 s：realistic，迭代数 5500 = 25 × (200 + 20)，0 次重启；墙钟比 1.175 |
| runs | 05:14:03 | 11:22:21 | 22098.1 s | 120 / 120 COMPLETED，全部首次成功（L1 05:14–06:46，L2 06:46–11:22） |
| board | 11:22:21 | 11:22:23 | 2.0 s | `p0a-formal-r2-l1`、`p0a-formal-r2-l2` 各 3 个参考条目、60 条评测行；L2 为当前版本 |
| submit | 11:22:23 | 13:45:15 | 8571.8 s | `sub-olenynk5qm7amst4`：RECEIVED → SCANNING → VALIDATING → EVALUATING → SCORING → DONE，10 个正式 Run 有效 10、平台重跑 0，条目上榜 |
| report | 13:45:18 | — | — | 10 项判定全部通过；`passed = true`、`formal = true`、`formal_passed = true` |

种子集承诺 `seed_set_commit = 2c67fffc53017324e2ce82463dff5339b126160a9e1e68ecb19f034275b43460`。runs 阶段 6.1 h（第 1 轮 9.8 h），submit 阶段 2.4 h（第 1 轮 4.7 h）。

### 3.2 墙钟

| 组 | 基线 | Run 数 | 墙钟比 最小 / 中位 / 最大 | 单 Run 墙钟（s） |
|---|---|---|---|---|
| L1 | static_guard | 20 | 0.733 / 0.816 / 0.849 | 263.9–305.6 |
| L1 | coverage_scan | 20 | 0.584 / 0.637 / 0.671 | 210.4–241.5 |
| L1 | track_share | 20 | 0.761 / 0.827 / 0.852 | 273.8–306.7 |
| L2 | static_guard | 20 | 2.232 / 2.309 / 2.372 | 803.4–853.9 |
| L2 | coverage_scan | 20 | 2.120 / 2.159 / 2.234 | 763.3–804.4 |
| L2 | track_share | 20 | 2.310 / 2.356 / 2.491 | 831.7–896.6 |
| 内部提交 | close_track（L2） | 10 | 2.129 / 2.197 / 2.240 | 766.6–806.4（冒烟为 kinematic 20 s，墙钟比 1.975，不计） |

墙钟 `wall_s` 为 `run_job` 计时（Env 构造至关闭，含 gz 与 Agent 容器启动），仿真时长 360 s。120 个正式 Run 与 10 个提交 Run 全部 ≤ 3.0；L2 最大 2.491（余量 17%），第 1 轮同组为 3.656–4.727。L2 60 个 Run 的 `l2.lidar_backend` 均为 `warp`。

### 3.3 四项指标（原型榜参考行，交汇与交接赛道 `rendezvous_handoff`）

点估计与 95% 置信区间（两阶段分层自助法，`d3-score-0`，10000 次重采样）；`repeat_delta` 为同一种子两次重复之间的最大差，全部为 0。

| 分轨 | 基线 | EC | EU | AO_real v0 | CE v0 |
|---|---|---|---|---|---|
| std3-l1 | static_guard | 1.000 [1.000, 1.000] | 0.033 [0.008, 0.059] | 0.752 [0.739, 0.772] | 0.201 [0.163, 0.243] |
| std3-l1 | coverage_scan | 0.400 [0.325, 0.475] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.068 [0.060, 0.075] |
| std3-l1 | track_share | 1.000 [1.000, 1.000] | 0.078 [0.038, 0.117] | 0.765 [0.752, 0.782] | 0.139 [0.110, 0.170] |
| std3-l2 | static_guard | 1.000 [1.000, 1.000] | 0.018 [0.000, 0.045] | 0.630 [0.613, 0.650] | 0.153 [0.125, 0.192] |
| std3-l2 | coverage_scan | 0.325 [0.250, 0.400] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.067 [0.059, 0.075] |
| std3-l2 | track_share | 1.000 [1.000, 1.000] | 0.036 [0.000, 0.073] | 0.650 [0.627, 0.673] | 0.147 [0.110, 0.180] |

全部 120 个 Run `ranked_valid = true`，`invalid_reasons` 为空。与第 1 轮（x500）相比，`coverage_scan` 的 EC 由 0.725（L1）、0.750（L2）降到 0.400、0.325，AO 由 0.173、0.087 降到 0；`static_guard`、`track_share` 的 EC 不变。两轮之间改变的是机型物理（质量 3.935 kg 对 2.064 kg、惯量与最大转速）与秘密种子集，固定扫描航线的飞行时序随之改变；本任务未逐项分析其对 `coverage_scan` 的影响（§7）。L2 的 EU 由 0 变为 0.018、0.036（L2 report 被接受，§2.3）。

## 4. 复核

复核脚本 `verify_formal.py`（dmax `/data/anet-drone4d-d3/d3dev/b10r2/verify/`，第 1 轮同一脚本加机型与 LiDAR 后端统计），在 `d3-compile:b10` 容器 `d3-B10-verify` 中运行（`--network none`；Run 级复核在提交阶段期间以 `--cpus 4`、`nice 19` 运行，榜单复核在报告之后以 `--cpus 8 --skip-eval` 运行），结果合并写入 `out/b10-formal-r2/verify/b10_verify.json`。

| 项 | 方法 | 结果 |
|---|---|---|
| `evaluation.json` 逐字节重算 | 每个正式 Run 以 `evaluate_run` 重算两次（与 `run_job` 相同的序列化），与运行目录中的文件逐字节比较 | 120 / 120 与存储一致，120 / 120 两次重算一致；单次评测最长 19.3 s（4 路并行），`run_job` 内记录的评测耗时 L1 最长 4.4 s、L2 最长 5.7 s |
| 重复运行一致 | 同组、同基线、同种子序号的两次重复，比较逐帧摘要（`platform_audit.frames_digest`）中 `obs/uav-0{1,2,3}`、`act/uav-0{1,2,3}` 与 `pose` 流 | 60 / 60 对逐条一致（L1 30 对、L2 30 对） |
| 机型与 LiDAR 后端 | 运行清单 `tier_info.vehicle.name`、`l2.lidar_backend` | L1、L2 各 60 个 `p600_bench`；L2 60 个 `warp`；10 个提交 Run 同为 `p600_bench`、`warp` |
| 动作与报告 | 汇总 `agents/wire_stats.json` 与 `reports.jsonl` | 1,296,000 条动作，协议错误 0、超时 0、迟到 0、非零退出 0；1426 条 report 全部附证据（L1 704 条、L2 722 条） |
| 评测行与评测文件 | 榜单 `rows.jsonl` 每行的 `evaluation` 与 `slim_evaluation(evaluation.json)` 比较 | 两个榜单版本的 60 + 60 条参考行全部一致；L2 另有 10 条内部提交行 |
| 榜单重算 | 每个榜单版本的 `config.json` 与 `rows.jsonl` 复制到同名临时目录，`boardstore.publish` 连算两次 | `p0a-formal-r2-l1`、`p0a-formal-r2-l2` 各连算两次，`board.json` 与 `details.json` 每次都与已发布文件逐字节一致；各参考条目的点估计、区间与 `repeat_delta` 与 `lane_metrics` 复算一致 |
| 基线区分度 | 与榜单相同的重采样种子（`lane_metrics`）与配对差值区间（`_paired_diff_ci`），三个基线两两比较四项指标 | L1：四项指标都有不含 0 的配对差值区间（如 EC `coverage_scan − track_share` [−0.675, −0.525]，EU `static_guard − coverage_scan` [0.008, 0.059]，CE `static_guard − track_share` [0.013, 0.114]）；L2：EC、AO、CE 有不含 0 的区间（EC `coverage_scan − track_share` [−0.75, −0.60]，AO `static_guard − coverage_scan` [0.613, 0.650]，CE `coverage_scan − track_share` [−0.114, −0.042]），EU 三者两两并列（区间均含 0） |

## 5. 原型榜与内部提交

### 5.1 榜单版本

| 榜单版本 | 分轨 | 赛道 | 条目 | 评测行 | 当前版本 |
|---|---|---|---|---|---|
| `p0a-formal-r2-l1` | `std3-l1`（3x-p600_bench，L1，realistic，exogenous，python，mesh） | `rendezvous_handoff` | 3 个参考行 | 60 | 否 |
| `p0a-formal-r2-l2` | `std3-l2`（3x-p600_bench，L2，realistic，exogenous，python，mesh） | `rendezvous_handoff` | 3 个参考行 + 1 个内部提交 | 60 + 10 | 是 |

两个版本的评分版本为 `d3-score-0`（原型，`weights = null`，不计算合成主得分）。服务端配置 `config.json`（0600）记录 `track.prototype = true`、`gpu_class = shared`、`dev = false`、`fleet_ref = 3x-p600_bench`、种子集承诺与本轮指纹；`board.json` 本身不携带原型标记，原型标注由工作区 `/lab/board` 页面标题"原型排行榜"（`lab.board.title`）给出。

### 5.2 内部提交

| 项 | 内容 |
|---|---|
| 提交 | `sub-olenynk5qm7amst4`，团队 `d3-internal`，提交人 `mbl-e2e`，分轨 `std3-l2`，可见性 board |
| 镜像 | `d3-example-close-track:b10`，摘要 `sha256:fded888a6ead6d4e82dd97f22133d61619564fcec02f3967b47ce682d9c4fa8d`（入口 `python -m drone4d.serve close_track.agent:Agent`） |
| 流水线 | 2026-10-21 11:22:23 RECEIVED → SCANNING → VALIDATING（冒烟 20 s，种子 0）→ EVALUATING（10 个秘密种子各 1 次，Realistic 档，L2，`p600_bench`，Warp LiDAR）→ SCORING → 13:45:15 DONE，共 8571.8 s |
| 正式 Run | 10 个，全部有效，平台重跑 0，锁步核对全部通过，墙钟比 2.129–2.240 |
| 上榜条目 | `d3-internal/sub-olenynk5qm7amst4`：EC 1.000 [1.000, 1.000]、EU 0.015 [0.000, 0.046]、AO 1.000 [1.000, 1.000]、CE 0.192 [0.159, 0.226]，四项名次均为 1、统计并列组 0，非支配第 1 层 |

提交 Run 不做重复（`subworker.process_one` 每个种子 1 次），因此提交条目的 `repeat_delta` 为 null。

### 5.3 发布到内部工作区

轮次结束后（2026-10-21 13:46 UTC），将两个榜单版本目录（`board.json`、`details.json`、`rows.jsonl`、`listing.json`、`config.json`）与提交文档 `sub-olenynk5qm7amst4.json`、`scan.json` 复制到工作区数据根 `/data/anet-drone4d-d3/lab/data`（属主 10001），并将 `boards/current` 由 `p0a-formal-l2` 改为 `p0a-formal-r2-l2`。在 `d3-bench` 容器内以工作区代码核对：版本列表为 `p0a-formal-l1`、`p0a-formal-l2`、`p0a-formal-r2-l1`、`p0a-formal-r2-l2`、`proto-2`，当前版本 `p0a-formal-r2-l2`，可见条目为 3 个参考行与 `d3-internal/sub-olenynk5qm7amst4`，配置 `prototype = true`、`gpu_class = shared`。第 1 轮版本与 `proto-2` 保留且只读。

## 6. 与验收条款的对照

### 6.1 D3-AC-56 最小基准闭环端到端

| 条款（docs/05 §11.2 原文） | 本轮结果 | 判定 |
|---|---|---|
| 在 dmax 上以一条命令执行 | `run_mbl_e2e.sh b10-formal-r2`，宿主进程内一次 `mbl_e2e.py` 完成 7 个阶段 | 通过 |
| crop 世界包 + `crop-handoff-001`（三道门 strict 编译通过） | `validate_world` 0 error；strict + ranked 编译，三道门无 fail | 通过 |
| Realistic 档 gz 锁步 | 预检、120 个正式 Run 与 10 个提交 Run 的 `iterations = 25 × (n_ticks + 预滚)`，0 次重启 | 通过 |
| 3 架 `p600_bench`（P0a 无热成像：`rgb1080-rs` + 倒装 MID-360 + IMU + GNSS + 导航解） | 机队机型 `p600_bench`（目录变体与 gz 映射，§2.2），运行清单 `tier_info.vehicle` 为 3.935 kg、1500 rad/s；传感器组由 Env 的 L2 观测提供（`rgb1080-rs`、倒装 MID-360 与其内置 IMU、GNSS、导航解），不交付热成像 | 通过 |
| 3 个基线策略（L1 与 L2 各一组，经 Python SDK 与隔离容器）× 10 个秘密种子 × 2 次重复 | 120 个正式 Run，隔离容器 Agent，线协议 | 通过 |
| 全部 Run 达到 COMPLETED，生成 `evaluation.json`（EC、EU、AO_real v0 与 CE v0） | 120 / 120 COMPLETED，四项分量齐全 | 通过 |
| 原型榜（交汇与交接赛道）显示四项指标与置信区间及 3 个基线参考行 | 两个榜单版本各 3 个参考行，四项指标均有点估计与 95% 区间 | 通过 |
| 1 个内部成员提交的 Agent 镜像经扫描、冒烟、正式评测与评分后出现在原型榜 | `d3-example-close-track:b10` 登记到 `p0a-formal-r2-l2`，扫描、冒烟、10 个秘密种子的正式评测与评分后 DONE，条目 `d3-internal/sub-olenynk5qm7amst4` 出现在 `board.json` | 通过 |
| 全部 Agent 收到的消息通过 `bench/obs.v1` 校验（未知字段 0） | 132 个 Run、1,405,992 条消息全部通过 `bench/obs.v1` 校验，未知字段 0 | 通过 |
| 样例包在内部工作区可浏览 | 报告给出每组每基线的代表运行（`representative_runs`），样例包由 B20 生成 | 不在本任务（B20） |
| 单个正式 Run 墙钟 ≤ 仿真时长 × 3（共享卡，原型） | L1 最大 0.852；L2 最大 2.491；内部提交最大 2.240 | 通过 |
| Playwright `lab/mbl` | — | 不在本任务（F09） |

### 6.2 D3-AC-17、18、57、58（本轮提供的正式运行部分）

| 验收 | 条款 | 本轮结果 | 判定 |
|---|---|---|---|
| D3-AC-17 | 正式成绩配置（Realistic 档 + M25 GPU 帧 + L2，基线策略）同一 Run 规格两次运行：gz 位姿逐位一致，Agent 收到的观测流与动作流逐字节一致 | L2 30 对重复（3 基线 × 10 种子，M25 GPU 相机帧与 Warp GPU 点云）的 `pose`、`obs/*`、`act/*` 流逐条一致；L1 30 对同样一致 | 通过（本条） |
| D3-AC-18 | `run_manifest.json` 通过 schema 校验（排名运行的清单只含 `seed_commit`） | 120 / 120 经 `check_run_manifest` 校验，`seed_commit` 与 HMAC 复算一致，清单中无 `seed` | 通过（本条） |
| D3-AC-18 | `evaluation.json` 对同一输入逐字节一致 | 120 / 120 重算一致 | 通过（本条） |
| D3-AC-18 | 3 机 L2 360 s 评测 ≤ 120 s | 最长 5.7 s（`run_job` 内），复核时 19.3 s（4 路并行） | 通过（本条，共享主机） |
| D3-AC-57 | 四项指标由 `evaluation.json` 存储的分量按 `score_version` 聚合，与排行榜显示值逐字节一致；同一输入两次计算逐字节一致 | 榜单重算两次与已发布的 `board.json`、`details.json` 逐字节一致，参考条目的四项指标与由 `rows.jsonl`（等于 `slim_evaluation(evaluation.json)`）复算的值一致；`weights = null`，合成值为 null | 通过（本条） |
| D3-AC-58 | 三个基线在 L1 与 L2 上都可运行，同一种子两次运行动作流逐字节一致 | 3 基线 × 2 档 × 10 种子全部 COMPLETED；60 对重复的 `act/*` 流逐条一致 | 通过（本条） |
| D3-AC-58 | 在 `crop-handoff-001` 上 `track_share` 的 EC 均值高于 `coverage_scan` | L1：1.000 > 0.400；L2：1.000 > 0.325 | 通过 |
| D3-AC-58 | 交汇与交接赛道上三个基线至少在一项指标上不全部两两统计并列 | L1：四项指标都至少有一对基线的配对差值区间不含 0；L2：EC、AO、CE 至少有一对不含 0 | 通过 |
| D3-AC-58 | 基线的 report 均附证据并通过 `bench/act.v1` 校验 | 1,296,000 条动作协议错误 0；1426 条 report 全部附证据（L1 704、L2 722） | 通过（本条） |

本轮不覆盖的条款（故障注入、背压、跨环境容差、合成接口、构造用例、SDK 上手时长、`ref_det_v1` 每步墙钟等）由各自的测试与记录判定，见 docs/05 §11.2 各条的验证位置。

## 7. 遗留与后续

| 编号 | 事项 | 归属 |
|---|---|---|
| 1 | **本任务改动需提交。** 改动全部在工作树中未提交（文件清单见任务报告）；HEAD `2e1a845` 之后提交时连同第 1 轮未提交的 `run_mbl_e2e.sh`、`mbl_e2e.py` 修正与 `configs/bench.d3/mbl-formal.yaml`、本记录一起提交 | 提交代理 |
| 2 | **docs/05 回填。** D3-AC-56 行（第 2 轮通过）、§24.5 F-09（机型映射已移入 `awr.phys.gz.vehicles` 与 Env）、Env 的 L2 LiDAR 后端设定与正式口径取 Warp、守卫的 L2 帧证据、§30 共享文件登记；见任务报告 doc_notes | 文档归属方 |
| 3 | **`coverage_scan` 在 `p600_bench` 上 EC 下降。** EC 由 0.725、0.750（x500）降到 0.400、0.325，AO 为 0；条款（`track_share` EC 更高、不全部并列）仍满足，但该基线的扫描航线未按新机型的动力学调参，建议基线归属方核对 | SDK 基线 |
| 4 | **L2 EU 区间含 0。** 第 2 轮 L2 上 report 被接受，EU 点估计 0.018、0.036，但三个基线的 EU 两两并列；EU 依赖 report 的 meet 判定与 `ref_det_v1` 的 L2 检测质量（A09、S26） | SDK 基线、S26 |
| 5 | **AO 观察。** D3-AC-18 的"覆盖扫描 AO_real v0 ≤ 0.05"由 A10 的 r6 对照另行判定（oracle 为评测时计算的 `ao-oracle-v0-grid@1`，AWR-05 §5.6.5；M23 在 P0a 不产出 `observability.npz`），本轮 `coverage_scan` AO 为 0，不作该条判定 | 评测（M27） |
| 6 | **复核脚本入库。** `verify_formal.py` 目前只在 dmax `b10r2/verify/` 与 `b10/verify/`；可并入 `mbl_e2e.py` 的报告阶段（`evaluation.json` 重算、重复一致、榜单重算、基线区分度、机型与后端统计） | B09 归属 |
| 7 | **样例包。** B20 取 `mbl_e2e.json` 的 `representative_runs`（L1 `coverage_scan-k00`、`static_guard-k04`、`track_share-k04`；L2 `coverage_scan-k05`、`static_guard-k04`、`track_share-k04`，均为第 1 次重复） | B20 |
| 8 | **页面标注。** `/lab/board` 按 `config.track.prototype`、`gpu_class` 显示"原型""共享卡"标签；`board.json` 可增加 `track.prototype` 字段 | F09 |
| 9 | **提交 Run 记录的后端字段。** `mbl_e2e.json` 的 `submission_runs` 不带 `lidar_backend`（提交 Run 不经 `execute_run`），后端由提交 Run 的运行清单 `l2.lidar_backend` 给出（10 个均为 `warp`） | B09 归属 |
| 10 | **独占卡性能。** 本轮墙钟为共享卡原型口径；Env 主循环以 Agent 计算（std 资源档单核）为下限，独占卡数字按 2026-10-18 决定另行采集 | S21 归属 |
| 11 | **dmax 清理（封板后）。** 第 2 轮运行目录 `/data/anet-drone4d-d3/d3dev/b10r2/out/b10-formal-r2/runs` 与 `benchroot/dataset` 在 B20 取样后删除；第 1 轮 `/data/anet-drone4d-d3/d3dev/b10/out/b10-formal/runs`（约 324 GB）与 `b10/benchroot/dataset`（约 54 GB）已无引用需要，可先删除；镜像标签 `:b10` 在报告引用结束后删除。探针目录 `b10fix`、`b10fix2` 已删除运行目录，只余日志 | 封板清理 |

## 8. 产物位置（dmax）

| 产物 | 路径（`/data/anet-drone4d-d3/d3dev/b10r2/` 下） |
|---|---|
| 报告 | `out/b10-formal-r2/mbl_e2e.json`（`drone4d.bench.mbl_e2e.v1`）、`out/b10-formal-r2/verify/b10_verify.json` |
| 检查点与日志 | `out/b10-formal-r2/e2e_state.json`、`e2e.log`、`console.log`、`config.effective.json`；复核日志 `verify/verify.log`、`verify/verify2.log` |
| 记录与运行目录 | `out/b10-formal-r2/records/*.json`、`out/b10-formal-r2/runs/<key>/`（120 个） |
| 本轮磁带 | `out/b10-formal-r2/events/crop-handoff-001/v1/` |
| 秘密种子 | `out/b10-formal-r2/evalstore/secrets.json`（0600，只在服务端） |
| 榜单与提交 | `benchroot/boards/p0a-formal-r2-l{1,2}/`、`benchroot/submissions/`、`benchroot/dataset/crop-handoff-001/runs/` |
| 探针日志 | `/data/anet-drone4d-d3/d3dev/b10fix/out/b10fix-ident{,2,3,4,5,6}/`、`/data/anet-drone4d-d3/d3dev/b10fix2/out/b10fix-ident7/`（`e2e.log`、记录；运行目录已删除），L2 report 诊断 `b10fix/diag/` |

## 9. 第 1 轮 `b10-formal`（历史）

第 1 轮由 `d3-dev` HEAD `a4bb77f` 的 `git archive` 快照加 3 处改动执行（`run_mbl_e2e.sh` 的 `IMAGE`、`REPO_DIR`、navsvc 同步与 `AWR_EVENT_NAV=detour`；`mbl_e2e.lockstep_check` 计预滚 tick；`mbl_e2e.redact` 跳过公开种子 0），dmax GPU 3，宿主 `--cpus 16`，代码根 `/data/anet-drone4d-d3/d3dev/b10`，命令 `GPU=3 REPO_DIR=/data/anet-drone4d-d3/d3dev/b10 IMAGE=d3-compile:b10 tools/deploy/dmax/run_mbl_e2e.sh b10-formal`。开发轮 `b10-smoke` 暴露的 4 个问题（宿主镜像缺 Node、代码根与其他任务共享、锁步不变量未计预滚、种子 0 被脱敏）在第 1 轮之前修正，修正保留在第 2 轮代码中。

| 项 | 第 1 轮结果 |
|---|---|
| 阶段 | world 1.0 s、compile 19.1 s、lockstep 24.7 s、runs 35418.7 s（120 / 120 COMPLETED，无平台重跑）、board 1.9 s、submit 16974.9 s（`sub-tnfsalvna4dummb2` DONE 并上榜）、report：10 项判定 9 项通过，`wall_clock` 不通过，`formal_passed = false` |
| 墙钟比 | L1 0.600–0.807；L2 3.656–4.727（中位 4.11）；内部提交 4.32–4.51 |
| 机型 | 运行清单为 x500（`fleet_vehicle: null`），`p600_bench` 未映射 |
| L2 report | 738 条全部被守卫以 663 `evidence_unknown` 拒绝（§2.3），L2 三个基线 EU 均为 0 |
| 四项指标（L1） | static_guard EC 1.000、EU 0.066、AO 0.760、CE 0.205；coverage_scan EC 0.725、EU 0.000、AO 0.173、CE 0.018；track_share EC 1.000、EU 0.050、AO 0.786、CE 0.169 |
| 四项指标（L2） | static_guard EC 1.000、EU 0.000、AO 0.633、CE 0.160；coverage_scan EC 0.750、EU 0.000、AO 0.087、CE 0.010；track_share EC 1.000、EU 0.000、AO 0.657、CE 0.116 |
| 复核 | `evaluation.json` 120 / 120 逐字节重算一致；60 / 60 对重复的 `pose`、`obs/*`、`act/*` 流逐条一致；两个榜单版本各连算两次与已发布文件逐字节一致；白名单审计 132 个 Run、1,405,992 条消息，未知字段 0；120 份运行清单 `seed_commit` 与 HMAC 复算一致 |
| 区分度 | L1、L2 上 `track_share` EC 均高于 `coverage_scan`；L1 四项、L2 三项（EC、AO、CE）存在不含 0 的配对差值区间 |
| 榜单 | `p0a-formal-l1`、`p0a-formal-l2`（数据根 `/data/anet-drone4d-d3/d3dev/b10/benchroot`），2026-10-21 02:09 UTC 复制到工作区数据根并设为当前版本；第 2 轮发布后由 `p0a-formal-r2-l2` 取代（§5.3），两个历史版本保留且只读 |
| 产物 | `/data/anet-drone4d-d3/d3dev/b10/out/b10-formal/`（报告 `mbl_e2e.json`、复核 `verify/b10_verify.json`、120 个运行目录约 324 GB） |
