# D3 SDK 干净主机上手演练记录

| 项 | 内容 |
|---|---|
| 用途 | 登记 ANet Drone4D Python SDK（`drone4d`）在干净主机上按快速上手文档完成首个 Run 的步骤与耗时，作为 D3-AC-58"15 min 内运行示例 Agent"条款的文档化测量 |
| 验收编号 | D3-AC-58（`pip install drone4d` 后按文档在 15 min 内以 `make(..., physics="realistic")` 与 `physics="fast"` 运行示例 Agent，同一 Agent 代码在两档上不改动） |
| 依据 | docs/05 §11.2 D3-AC-58、§5.5；ADR-194 (3)、ADR-202（`docs/impl/D3-决策登记.md`）；M26-FR-080、M26-AC-030；快速上手 `docs/sdk/quickstart.zh-CN.md` |
| 口径 | ADR-202 (1)：以 dmax 上新拉起的 `python:3.12-slim`（amd64）容器作为干净主机，不挂载仓库、不预装依赖，只读挂载离线 wheel 目录作为包索引；计时从研究者侧容器启动开始，到示例 Agent 完成首个 Run（平台侧发送 CLOSE、Agent 进程全部以 0 退出、容器退出）为止。上手时间属于功能性测量，不属于 R-D3-63 的性能类条款，共享主机上的测量计入本记录 |
| 阈值 | ≤ 15 min（900 s），docs/05 §11.2 原文 |
| 结论 | 通过。5 次独立演练（高保真仿真档 2 次、快速仿真档 2 次、基线策略 1 次），研究者侧容器启动到首个 Run 结束的墙钟为 45.0 s 至 51.2 s，最大值为阈值的 5.7%；从平台侧启动起算为 50.8 s 至 56.9 s |

## 1 演练环境

| 项 | 取值 |
|---|---|
| 时间 | 2026-10-19 08:03:30 至 08:09:16 UTC；研究者侧镜像于 08:00 UTC 前后拉取 |
| 主机 | dmax（2 × Intel Xeon Gold 6326，64 逻辑核，1 TB 内存，Docker 29.1.4）；共享生产机，演练期间系统负载约 32（64 核），不使用 GPU |
| 研究者侧镜像 | `python:3.12-slim` 的 linux/amd64 版本，经 dmax 配置的 Docker Hub 镜像源以独立名称 `docker.m.daocloud.io/library/python:3.12-slim` 新拉取，不覆盖 dmax 共享标签 `python:3.12-slim`（该标签为 linux/arm64，见第 7 节）。索引摘要 `sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`，镜像 ID `sha256:df18428df8a0f9189c84e7424d214b4c50092af1f9dca5842a00c41c39bda316`，构建时间 2026-10-06T02:04:40Z，119.4 MB；容器内 `uname -m` 为 `x86_64`，`python3 -V` 为 `Python 3.12.15` |
| 研究者侧容器 | 每次演练新建（`--rm`），`--network none --cpus 2 --memory 8g`，非 root 用户（`--user` 为宿主账户，`HOME=/tmp`）；挂载只有 3 项：离线 wheel 目录 `/wheels`（只读）、快速上手文档 `/doc/quickstart.zh-CN.md`（只读，用于取得第 3 节的示例代码）、套接字目录 `/tmp/d4d-sock`；另挂载一个只写记录目录 `/out` 保存安装日志与步骤时刻。未挂载仓库代码，镜像内除 Python 标准发行外无预装包 |
| 平台侧（终端 A） | `d3-gz:dev`（`sha256:7350b8d8b935…`，Python 3.12.3，Gazebo Sim 与 Drone4D 插件），`--network none --cpus 8 --memory 16g`；运行快速上手第 4.1 节的 `run_quickstart.py`，Gazebo Sim 以 `--gz-launcher local` 在同一容器内启动 |
| 源码 | `d3-dev` 的 9ae4c76（`git archive`，不含工作树中未提交的改动）；仓库以只读方式挂载到平台侧容器 |
| 世界与事件 | `oryahovo-crop1`、已编译事件 `crop-handoff-001`（dmax `/data/anet-drone4d-d3/d3dev/worlds`、`d3data/events`，只读）；Run 规格 `configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json`（L1 观测档，3 架 x500，10 Hz 控制步，60 s 回合，种子 1；物理档见第 4 节） |

## 2 离线 wheel 目录

按 ADR-202 (1)，wheel 目录只含 `drone4d` 及其依赖（Python 3.12、x86_64），共 11 个文件，24.5 MB。依赖 wheel 与 A03 交付的离线目录逐字节相同（按 A03 的 sha256 清单校验通过），`drone4d` wheel 由 9ae4c76 按快速上手第 8 节构建：

| 文件 | 字节 | sha256 |
|---|---|---|
| `anyio-4.15.1-py3-none-any.whl` | 132079 | `6152fdbbf9a77fdec97731721bebf7c4c44f7c29b424b0065826173efc7ed101` |
| `certifi-2026.7.22-py3-none-any.whl` | 136983 | `62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775` |
| `drone4d-0.2.0.dev0-py3-none-any.whl` | 103451 | `b8e9ddd3c7a54da4e387505605f8a9f3e5345d47c2ebfd88c323503fbc000b9d` |
| `h11-0.16.0-py3-none-any.whl` | 37515 | `63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86` |
| `httpcore-1.0.9-py3-none-any.whl` | 78784 | `2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55` |
| `httpx-0.28.1-py3-none-any.whl` | 73517 | `d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad` |
| `idna-3.20-py3-none-any.whl` | 69583 | `ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c` |
| `msgpack-1.2.2-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl` | 420619 | `77c2e018417dc1d66f235e383877ee885b60ade9d29e494dd581e08af2cb1923` |
| `numpy-2.5.3-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl` | 16717410 | `b7e18c623bb5c95acb3b3328861272816ba199fb531921c5d6d0b675f1fde9e3` |
| `pillow-11.3.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl` | 6645236 | `676b2815362456b5b3216b4fd5bd89d362100dc6f4945154ff172e206a22c024` |
| `typing_extensions-4.16.0-py3-none-any.whl` | 45571 | `481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8` |

`drone4d` wheel 的构建（快速上手第 8 节，在 9ae4c76 的 `git archive` 展开目录中执行，`umask 022`，setuptools 84.0.0，Python 3.12.3）：

```bash
rm -rf var/sdk && mkdir -p var/sdk && cp -r tools/sdk/drone4d var/sdk/src
SOURCE_DATE_EPOCH=1760745600 python -m pip wheel --no-deps --no-build-isolation -w var/sdk/dist var/sdk/src
sha256sum var/sdk/dist/*.whl
```

```text
b8e9ddd3c7a54da4e387505605f8a9f3e5345d47c2ebfd88c323503fbc000b9d  var/sdk/dist/drone4d-0.2.0.dev0-py3-none-any.whl
```

该 wheel 与快速上手第 8 节登记的 wheel（sha256 `b8b54b30…`）的 25 个成员内容逐字节相同、成员顺序与时间戳相同，差异只在 21 个成员（全部 18 个 `.py` 模块与 `WHEEL`、`entry_points.txt`、`top_level.txt`）的 Unix 权限位：第 8 节的登记值在 `umask 002` 下构建（权限 0664），本次在 `umask 022` 下构建（权限 0644）。实测确认：同一源码在 `umask 022` 与 `umask 002` 下分别得到 `b8e9ddd3…` 与 `b8b54b30…`；包数据（权重 `ref_det_v1.npz`、`model_card.json`）的权限位取自源文件。处理建议见第 8 节。

## 3 演练步骤

两个终端的命令均取自快速上手文档，只替换主机路径；附录 A 为驱动脚本全文。

| 步骤 | 文档章节 | 终端 | 操作 |
|---|---|---|---|
| A1 | 第 4.1 节 | 平台侧 | 启动 d3-gz 容器，运行 `run_quickstart.py --physics realistic --gz-launcher local --sock-root /tmp/d4d-sock`（快速仿真档为 `--physics fast`，不带 gz 参数），等待 3 个套接字输出 `waiting for an agent` |
| B0 | — | 研究者侧 | `docker run` 新建 `python:3.12-slim` 容器（计时起点） |
| B1 | 第 2 节 | 研究者侧 | `python3 -m venv ~/d4d` 并激活 |
| B2 | 第 2 节 | 研究者侧 | `pip install --no-index --find-links /wheels "drone4d[l2]"` |
| B3 | 第 2 节 | 研究者侧 | `drone4d-agent --help`，输出与文档逐字相同 |
| B4 | 第 3 节 | 研究者侧 | 把文档第 3 节的 Python 代码块保存为 `plaza_watch.py`（以脚本从文档中截取，代替复制粘贴）；文件 sha256 `ab86e0d6d963d216d19dc0ee7bf745bb4df03957dc94f5af0821178e529bc09b`，与仓库 `docs/sdk/examples/plaza_watch.py` 相同 |
| B5 | 第 4.2 节 | 研究者侧 | 每架机启动一个 `python -m drone4d.serve plaza_watch:PlazaWatch --sock /tmp/d4d-sock/<uav>/agent.sock` 进程并 `wait`；基线演练改为 `--baseline track_share`（第 6 节） |
| B6 | — | 研究者侧 | 平台侧 60 s 回合结束发送 CLOSE，3 个 Agent 进程以 0 退出，容器退出（计时终点） |

第 2 步的安装输出（5 次相同）：

```text
Successfully installed anyio-4.15.1 certifi-2026.7.22 drone4d-0.2.0.dev0 h11-0.16.0 httpcore-1.0.9 httpx-0.28.1 idna-3.20 msgpack-1.2.2 numpy-2.5.3 pillow-11.3.0 typing_extensions-4.16.0
```

与文档第 2 节的输出相同（Python 3.12 的 numpy 为 2.5.3，文档登记的是 Python 3.11 的 2.4.6，第 8 节已说明）。安装后 `pip list` 共 12 个包：上述 11 个与 venv 自带的 `pip 25.0.1`。

## 4 耗时

单位 s。B 列为第 3 节对应步骤的耗时；"容器起算"为 B0 至 B6，即 ADR-202 的计时口径；"平台起算"自 A1 起算，包含平台侧启动与世界加载。

| 演练 | 物理档 | Agent | A1 就绪 | B0 容器启动 | B1 venv | B2 安装 | B3 + B4 | B5 运行 | B6 退出 | 容器起算 | 平台起算 | Env 墙钟 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| realistic-1 | realistic | PlazaWatch | 6.75 | 0.25 | 3.43 | 3.39 | 0.12 | 42.45 | 0.34 | 49.98 | 56.73 | 37.12 |
| realistic-2 | realistic | PlazaWatch | 5.75 | 0.21 | 2.73 | 3.56 | 0.12 | 37.97 | 0.42 | 45.00 | 50.76 | 35.41 |
| fast-1 | fast | PlazaWatch | 5.78 | 0.20 | 3.12 | 3.49 | 0.12 | 40.37 | 0.46 | 47.76 | 53.54 | 39.74 |
| fast-2 | fast | PlazaWatch | 5.27 | 0.18 | 3.22 | 3.19 | 0.12 | 41.32 | 0.48 | 48.52 | 53.79 | 40.69 |
| baseline-track_share | realistic | track_share | 5.76 | 0.20 | 3.18 | 3.58 | 0.09 | 43.65 | 0.47 | 51.17 | 56.93 | 41.07 |

- 容器起算最大 51.17 s，阈值 900 s，余量 848.8 s；
- 安装（B1 + B2）为 6.0 s 至 6.8 s，其余时间基本是 60 s 仿真回合本身（Env 墙钟 35.4 s 至 41.1 s，另含 gz 世界加载与 Agent 接入握手）；
- 本记录测量的是命令执行耗时；阅读文档与输入命令的人工时间不计入（ADR-202 的计时口径），15 min 阈值中扣除上表后约 14 min 可用于阅读第 1 节至第 4 节。

## 5 结果核对

平台侧每次输出一行 `drone4d.quickstart.summary.v1` 摘要，逐项结果：

| 演练 | n_ticks | 超时 | 线协议错误 | 异常退出 | 动作回执 | `acts_sha256`（uav-01 / uav-02 / uav-03，前 8 位） | `pose_sha256`（前 8 位） |
|---|---|---|---|---|---|---|---|
| realistic-1 | 600 | 0 / 0 / 0 | 0 / 0 / 0 | 无 | nav 668、gimbal 665，全部 `applied` | dccb1358 / 8bfa3f71 / 687b45b8 | e8e90507 |
| realistic-2 | 600 | 0 / 0 / 0 | 0 / 0 / 0 | 无 | 同上 | dccb1358 / 8bfa3f71 / 687b45b8 | e8e90507 |
| inproc-realistic | 600 | 0 / 0 / 0 | — | — | — | dccb1358 / 8bfa3f71 / 687b45b8 | e8e90507 |
| fast-1 | 600 | 0 / 0 / 0 | 0 / 0 / 0 | 无 | nav 621、gimbal 618，全部 `applied` | 5ecd1215 / c9a4b125 / 9aaa44f0 | a510bf26 |
| fast-2 | 600 | 0 / 0 / 0 | 0 / 0 / 0 | 无 | 同上 | 5ecd1215 / c9a4b125 / 9aaa44f0 | a510bf26 |
| baseline-track_share | 600 | 0 / 0 / 0 | 0 / 0 / 0 | 无 | nav 668、gimbal 665，全部 `applied` | 184002e1 / 9cbc7ab1 / b595a542 | 879e5b50 |

- 文档一致性：高保真仿真档与快速仿真档的三机 `acts_sha256` 与快速上手第 4.2 节、第 5 节登记的完整值逐字节相同，`uav-01.log` 的 5 行检测计数与第 4.2 节相同；文档输出在 9ae4c76 上仍然成立。
- 跨环境一致：干净容器（Python 3.12.15，wheel 安装）经线协议运行，与平台侧进程内运行（`inproc-realistic`，d3-gz 的 Python 3.12.3，源码树，第 4.3 节）的动作流与位姿摘要逐字节相同。
- 重复一致：同档两次独立演练（每次新建容器、重新安装）的动作流与位姿摘要逐字节相同。
- 同一 Agent 代码在两档运行：`plaza_watch.py` 不改动，平台侧只改 `--physics`，研究者侧命令不变。
- 基线：`track_share` 从 wheel 中加载，经线协议完成同一 60 s 回合；60 s 内处于搜索阶段，未提交 report。
- 运行目录 `manifest.json`、`agents/<uav>/acts.jsonl`、`raw.mcap`、`phys/` 与 `truth/` 均生成。

## 6 与 D3-AC-58 原文的对照

| 条款 | 本记录的结论 | 其余证据 |
|---|---|---|
| `pip install drone4d` 后按文档在 15 min 内以 `physics="realistic"` 运行示例 Agent | 通过：49.98 s、45.00 s（≤ 900 s） | 快速上手第 0 节；`tests/d3/agentsdk_pkg/test_quickstart.py` |
| 以 `physics="fast"`（P0b 起）运行示例 Agent | 通过：47.76 s、48.52 s（≤ 900 s） | `tests/d3/phys/test_fast.py`（P07） |
| 同一 Agent 代码在两档与 L1、L2 上不改动 | 两档 L1 通过（同一文件，sha256 见第 3 节 B4）；L2 需 d3-sensorsvc 与 GPU，不在本记录（dmax-cpu） | `tests/d3/agentsdk_pkg/`（kinematic L2）；B10 |
| 三基线在 L1 与 L2 上可运行，同种子动作流逐字节一致 | 本记录补充：`track_share` 自 wheel 安装后在干净容器中经线协议运行通过 | `tests/d3/baselines/`（A02） |
| `ref_det_v1` 只用 CPU、`std` 档每步墙钟预算 | 不在本记录 | 快速上手第 6 节、A02 |
| `crop-handoff-001` 上 EC 排序与区分度 | 不在本记录 | B10 |
| report 附证据并通过 `bench/act.v1` 校验 | 本记录全部动作经 Env 动作守卫校验，回执全部 `applied` | A02 |
| SDK 与基线源码随文档发布，文档通过 copy-lint | 不在本记录 | `tests/d3/agentsdk_pkg/test_package.py`（A03） |

## 7 与真实干净主机的差异（ADR-202 (3)）

| 项 | 本演练 | 真实干净主机 | 影响 |
|---|---|---|---|
| 包来源 | 离线 wheel 目录（`--no-index --find-links`） | PyPI 或版本发布附件 | 网络下载时间不计入；24.5 MB 依赖在 10 Mbit/s 链路上约 20 s，不改变结论 |
| 操作系统 | Debian（`python:3.12-slim`）容器，共享宿主内核 | 研究者自有 Linux 主机 | 无系统级预装；与主机发行版的差异不影响纯 Python 与 manylinux wheel |
| 架构 | 显式使用 linux/amd64 镜像 | 研究者主机架构 | dmax 的共享标签 `python:3.12-slim`（`afad9ec00dc4`）为 linux/arm64，在 x86_64 上以模拟方式运行，pip 找不到 x86_64 wheel，平台侧报 665 `hello_timeout`（A03 实测，已列入快速上手第 9 节排错表）。演练按 ADR-202 改用 amd64 镜像，共享标签未改动 |
| 平台侧 | dmax 上已就位的世界包、已编译事件与 d3-gz 镜像 | 内部工作区成员使用同一 dmax 环境 | 与快速上手第 1 节的前提一致；平台侧就绪 5.3 s 至 6.8 s，已计入"平台起算" |
| 人工时间 | 不计入 | 计入 | 见第 4 节 |

## 8 发现与后续

1. wheel 字节依赖构建时的 `umask` 与包数据源文件的权限位（第 2 节）。`SOURCE_DATE_EPOCH` 只固定时间戳，快速上手第 8 节的 sha256 只在相同 `umask`（002）与相同源文件权限下复现；`tests/d3/agentsdk_pkg/test_package.py` 的可复现性用例在同一目录、同一 `umask` 下构建两次，覆盖不到该差异。建议第 8 节的构建命令加入 `umask 022` 与 `chmod -R u=rwX,go=rX var/sdk/src`，并把登记值更新为 `b8e9ddd3c7a54da4e387505605f8a9f3e5345d47c2ebfd88c323503fbc000b9d`（9ae4c76 的源码；版本号或 README 变化时随之更新）；可复现性用例改为在 `umask 022` 与 `umask 002` 下各构建一次并比对。
2. 发布 v0.3.0 时 `drone4d.__version__` 改为 `0.3.0`（A03 遗留 1），wheel 文件名与 sha256 随之变化；届时以附录 A 的脚本复跑本演练，并更新第 2 节与第 4 节。
3. L2 观测档的干净主机演练（`crop-handoff-001.l2-3uav`）需要 d3-sensorsvc 与 GPU，随 B10 执行；研究者侧步骤不变，耗时增加部分只来自 L2 回合本身。

## 附录 A 演练脚本

在 dmax 上执行。工作目录 `/data/anet-drone4d-d3/d3dev/a11` 下：`tree/` 为 `git archive` 展开的源码（`python`、`packages/contracts`、`vehicles`、`configs/bench.d3`、`tools/sdk/drone4d`、`docs/sdk`、`native/gz/proto`），`wheels/` 为第 2 节的 wheel 目录，`pylib-a11/` 为平台侧录制所需的 `mcap`、`mcap_protobuf_support`、`lz4`。用法：

```bash
NAME=realistic-1 PHYS=realistic bash drill.sh
NAME=fast-1 PHYS=fast bash drill.sh
NAME=baseline-track_share PHYS=realistic BASELINE=track_share bash drill.sh
NAME=inproc-realistic PHYS=realistic INPROC=1 bash drill.sh
```

```bash
#!/usr/bin/env bash
# A11：SDK 干净主机上手演练（ADR-202）。在 dmax 上按 docs/sdk/quickstart.zh-CN.md 第 2–4 步（与第 5、6 节）实跑：
#   平台侧（终端 A）：d3-gz 容器运行 run_quickstart.py，等待 Agent 接入；
#   研究者侧（终端 B）：新拉起的 python:3.12-slim（amd64）容器，--network none，不挂载仓库，
#   只读挂载离线 wheel 目录与快速上手文档；示例 Agent 文件从文档第 3 节的代码块取得（模拟复制粘贴）。
# 用法：NAME=<运行名> PHYS=realistic|fast [BASELINE=track_share] [INPROC=1] bash drill.sh
set -euo pipefail
D=/data/anet-drone4d-d3/d3dev; W=$D/a11
NAME=${NAME:?}; PHYS=${PHYS:-realistic}; DUR=${DUR:-60}; BASELINE=${BASELINE:-}; INPROC=${INPROC:-}
IMG=${IMG:-docker.m.daocloud.io/library/python:3.12-slim}
R=$W/runs/$NAME
docker rm -f d3-a11-platform d3-a11-agent >/dev/null 2>&1 || true
rm -rf "$R" && mkdir -p "$R/sock" "$R/out" "$R/agent"
U="--user $(id -u):$(id -g) -e HOME=/tmp"
ts() { date -u +%s.%N; }
if [ -n "$INPROC" ]; then AGENT_ARGS="--agent plaza_watch:PlazaWatch"; EXTRA_PP=":/repo/docs/sdk/examples"; else AGENT_ARGS="--sock-root /tmp/d4d-sock"; EXTRA_PP=""; fi
GZ_ARGS=""; [ "$PHYS" = realistic ] && GZ_ARGS="--gz-launcher local --gz-export-root /gzexport"
# 平台侧（终端 A，第 4.1 节）
tA=$(ts)
docker run -d --name d3-a11-platform --network none --cpus 8 --memory 16g $U -e USER=a11 \
  -e PYTHONPATH=/opt/pylib-gz:/opt/pylib-a11:/repo/python:/repo/tools/sdk/drone4d/src$EXTRA_PP \
  -e AWR_CONTRACTS_DIR=/repo/packages/contracts -e AWR_VEHICLES_DIR=/repo/vehicles \
  -v $W/tree:/repo:ro -v $D/worlds:/repo/worlds:ro -v $D/d3data:/repo/d3data:ro -v $D/pylib-gz:/opt/pylib-gz:ro \
  -v $W/pylib-a11:/opt/pylib-a11:ro -v $W/gzexport:/gzexport -v $R/out:/runs -v $R/sock:/tmp/d4d-sock -w /repo d3-gz:dev \
  sh -c "python3 docs/sdk/examples/run_quickstart.py --spec configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json \
    --worlds-root worlds --events-root d3data/events --physics $PHYS $GZ_ARGS \
    --duration-s $DUR --seed 1 --out /runs/quickstart-1 $AGENT_ARGS > /runs/platform.stdout 2> /runs/platform.stderr" >/dev/null
if [ -z "$INPROC" ]; then
  # 等平台侧三个套接字都在监听（第 4.1 节的 waiting 输出）
  for _ in $(seq 1 600); do n=$(grep -c 'waiting for an agent' $R/out/platform.stderr 2>/dev/null || true); [ "${n:-0}" -ge 3 ] && break; sleep 0.5; done
  tReady=$(ts)
  if [ -n "$BASELINE" ]; then SERVE="--baseline $BASELINE"; else SERVE="plaza_watch:PlazaWatch"; fi
  # 研究者侧（终端 B）：计时从 docker run 开始
  t0=$(ts)
  docker run --rm --name d3-a11-agent --network none --cpus 2 --memory 8g $U \
    -v $W/wheels:/wheels:ro -v $W/tree/docs/sdk/quickstart.zh-CN.md:/doc/quickstart.zh-CN.md:ro \
    -v $R/sock:/tmp/d4d-sock -v $R/agent:/out -e SERVE="$SERVE" $IMG sh -c '
      set -e
      mark() { echo "$1 $(date -u +%s.%N)" >> /out/steps.txt; }
      mark container_start
      uname -m > /out/uname.txt; python3 -V > /out/python.txt
      mkdir -p /tmp/work && cd /tmp/work
      # 第 2 步：安装 SDK
      python3 -m venv ~/d4d
      . ~/d4d/bin/activate
      mark venv_done
      pip install --no-index --find-links /wheels "drone4d[l2]" > /out/install.log 2>&1
      tail -n 1 /out/install.log
      mark install_done
      drone4d-agent --help > /out/help.txt
      mark help_done
      # 第 3 步：编写 Agent（从文档第 3 节的代码块保存为 plaza_watch.py）
      python3 - <<EOF
import re
t = open("/doc/quickstart.zh-CN.md", encoding="utf-8").read()
sec = t[t.index("## 3. "):t.index("## 4. ")]
code = re.search(r"\`\`\`python\n(.*?)\`\`\`", sec, re.S).group(1)
open("plaza_watch.py", "w", encoding="utf-8").write(code)
EOF
      sha256sum plaza_watch.py > /out/plaza_watch.sha256
      mark agent_written
      # 第 4.2 节：接入 Agent，每架机一个进程
      for u in uav-01 uav-02 uav-03; do
        python -m drone4d.serve $SERVE --sock /tmp/d4d-sock/$u/agent.sock 2> /out/$u.log &
      done
      wait
      mark run_done
      pip list --format=freeze > /out/freeze.txt' > $R/agent.stdout 2>&1
  t1=$(ts)
fi
docker wait d3-a11-platform > $R/platform.exit
tB=$(ts)
docker logs d3-a11-platform > $R/platform.docker.log 2>&1 || true
docker rm d3-a11-platform >/dev/null
{
  echo "platform_start=$tA"
  [ -z "$INPROC" ] && echo "platform_ready=$tReady" && echo "agent_container_start=$t0" && echo "agent_container_end=$t1" \
    && echo "agent_container_wall_s=$(echo "$t1 - $t0" | bc)" && echo "platform_ready_s=$(echo "$tReady - $tA" | bc)"
  echo "platform_end=$tB"; echo "platform_wall_s=$(echo "$tB - $tA" | bc)"
  echo "image_digest=$(docker image inspect $IMG --format '{{index .RepoDigests 0}}') image_id=$(docker image inspect $IMG --format '{{.Id}} {{.Architecture}}')"
} > $R/timing.txt
cat $R/timing.txt; cat $R/agent.stdout 2>/dev/null || true; cat $R/out/platform.stdout; tail -3 $R/out/platform.stderr
```
