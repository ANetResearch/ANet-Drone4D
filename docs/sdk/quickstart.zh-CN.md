# ANet4D Python SDK 快速上手

本文在 15 min 内完成一次完整的上手流程：安装 `anet4d` SDK，编写一个示例 Agent，在高保真仿真档（`physics = realistic`，Gazebo Sim 锁步）上以 3 架机运行一个 60 s 的 L1 回合，并核对线协议接入与进程内运行的动作流逐字节一致，最后以快速仿真档（`physics = fast`）不改代码地重复同一回合。文中每条命令的输出均来自实际运行，测量条件见第 0 节。English version: [quickstart.en.md](quickstart.en.md)。

SDK 在 V0.3.1 及以前名为 `drone4d`。V0.3.2 中 `drone4d` 作为已弃用的兼容别名保留，它重新导出 `anet4d` 并发出 `DeprecationWarning`，命令 `drone4d-agent` 仍可使用并在标准错误输出提示改用新名；二者将在下一版本移除。请在导入与命令中把 `drone4d` 替换为 `anet4d`。线协议 `drone4d.bench.wire.v1`、套接字路径 `/run/drone4d/agent.sock` 与环境变量 `DRONE4D_AGENT_SOCK` 属于遗留标识符，保持原名。

## 0. 实测记录

| 步骤 | 环境 | 实测 |
|---|---|---|
| 第 2 步至第 4 步（安装、编写、运行） | dmax，全新 `python:3.11-slim` 容器（amd64，`--network none`、`--cpus 2`、`--memory 8g`），离线 wheel 目录 | 容器启动到 3 机 60 s 高保真仿真回合结束，墙钟 49.4 s，其中 Env 回合墙钟 38.6 s |
| 第 4.3 节（进程内运行） | dmax，d3-gz 镜像（Python 3.12，`--cpus 8`） | 同一回合墙钟 36.7 s，动作流 sha256 与第 4.2 节逐机相同 |
| 第 5 节（快速仿真档） | 同第 4.2 节的研究者侧容器，平台侧以 FleetSim 运行 | 容器启动到 3 机 60 s 快速仿真回合结束，墙钟 47.6 s，其中 Env 回合墙钟 39.2 s |
| 干净主机演练（ADR-202） | dmax，全新 `python:3.12-slim` 容器（amd64，2026-10-19） | 从容器启动起算 45.0–51.2 s（上限 900 s），记录见 `docs/impl/D3-SDK-上手演练记录.md` |

实测日期 2026-10-18（UTC），当时 SDK 包名为 `drone4d`；安装输出中的包名与版本按改名后的 `anet4d` 0.3.2 列出，其模块除名称外未变。第 2 节的 `--help` 输出与第 8 节的构建于 2026-10-25（UTC）以 `anet4d` 0.3.2 重新运行。平台侧（第 4.1 节）先于研究者侧启动并等待接入。干净主机的正式演练按 ADR-202 在 dmax 全新 `python:3.12-slim` 容器中执行，记录在 `docs/impl/D3-SDK-上手演练记录.md`。

## 1. 前提条件

- 研究者主机：Linux x86_64，Python 3.11、3.12 或 3.13。Agent 只依赖 `anet4d` 及其依赖，不需要仓库代码。
- 平台侧（运行 Env）：仓库环境（`PYTHONPATH` 含 `python` 与 `tools/sdk/anet4d/src`）、世界包 `oryahovo-crop1` 与已编译事件 `crop-handoff-001`；高保真仿真档另需 d3-gz 镜像（Gazebo Sim 与本仓库的 Gazebo 原生插件）。内部工作区成员可直接使用 dmax 上 `/data/anet-drone4d-d3/d3dev` 中已就位的世界包与事件。
- Agent 与 Env 经本机 Unix 套接字交换帧（线协议 `drone4d.bench.wire.v1`）：两者位于同一主机，或 Agent 位于同一主机的容器中，套接字目录以绑定挂载提供。

## 2. 安装 SDK

`anet4d` 尚未发布到 PyPI。wheel 随版本发布附件提供，也可按第 8 节从源码构建；把 wheel 与其依赖放入同一目录后安装：

```bash
python3 -m venv ~/d4d
. ~/d4d/bin/activate
pip install --no-index --find-links /wheels "anet4d[l2]"
```

```text
Successfully installed anyio-4.15.1 certifi-2026.7.22 anet4d-0.3.2 h11-0.16.0 httpcore-1.0.9 httpx-0.28.1 idna-3.20 msgpack-1.2.2 numpy-2.4.6 pillow-11.3.0 typing_extensions-4.16.0
```

核心依赖只有 `msgpack`（线协议）与 `httpx`（bench-api 客户端）。extras `l2` 增加 `numpy` 与 `pillow`，供 L2 观测下的参考检测器 `ref_det_v1` 使用；只运行 L1 时可安装不带 extras 的 `anet4d`。主机可访问包索引时，去掉 `--no-index` 即可在线获取依赖。

确认容器入口可用：

```bash
anet4d-agent --help
```

```text
usage: python -m anet4d.serve [-h] [--baseline BASELINE] [--sock SOCK]
                              [factory]

positional arguments:
  factory              pkg.module:Factory

options:
  -h, --help           show this help message and exit
  --baseline BASELINE  built-in baseline policy name
  --sock SOCK          default $DRONE4D_AGENT_SOCK or /run/drone4d/agent.sock
```

`anet4d-agent` 与 `python -m anet4d.serve` 等价。

## 3. 编写 Agent

Agent 是 `anet4d.agent.DroneAgent` 的子类，每架机一个实例，实现两个方法：

- `reset(config)`：回合开始时调用一次，`config` 为 `bench/agent_config.v1`（任务简报、机型限幅、传感器、决策时延与通信参数）；
- `step(obs, inbox)`：每个控制步（10 Hz）调用一次，`obs` 为 `bench/obs.v1`，`inbox` 为本步收到的机间消息，返回 `bench/act.v1`。

观测与动作都是普通 dict；`anet4d.agent` 提供 `act`、`nav_takeoff`、`nav_goto`、`gimbal_look_at`、`out_msg`、`report` 等动作构造函数。在工作目录中保存以下文件为 `plaza_watch.py`（与仓库中的 `docs/sdk/examples/plaza_watch.py` 相同）：

```python
"""快速上手示例 Agent `PlazaWatch`（docs/sdk/quickstart.zh-CN.md、quickstart.en.md）。

每架机起飞到各自的高度层，飞往分配给本机的公开锚点斜上方站位，云台持续对准锚点，并每 10 s 向标准错误输出累计检测数。
L1 直接读取观测中的检测列表；L2 由参考检测器 `ref_det_v1`（`anet4d.perception.ref_det.L2Perception`，需要 extras `l2`）
从相机图像得到同形的检测。示例不发送机间消息、不提交 report；report 与协同的写法见基线策略 `anet4d.baselines`。

同一份代码不改动地运行于各物理档（`physics = realistic`、`fast`）与观测档（L1、L2）；策略内无随机数、无墙钟依赖，
同一种子两次运行的动作流逐字节一致。
"""

from __future__ import annotations

import sys

from anet4d.agent import DroneAgent, act, gimbal_look_at, nav_goto, nav_takeoff

STANDOFF_M = 30.0                 # 站位相对锚点向西、向南各偏移 30 m（斜视）
LAYER_M = 8.0                     # 机间高度分层间隔：同高度交叉航线会机间碰撞
CRUISE_MPS = 6.0
LOG_EVERY = 100                   # 每 100 步（10 s）输出一次累计检测数


class PlazaWatch(DroneAgent):
    def reset(self, config: dict) -> None:
        super().reset(config)
        self.uav = config["uav_id"]
        brief = config["brief"]
        fleet = sorted([self.uav, *config["peers"]])
        i = fleet.index(self.uav)
        lo, hi = brief["area"]["alt_agl_range_m"]
        self.alt = min(max(0.5 * (lo + hi) + LAYER_M * (i - 0.5 * (len(fleet) - 1)), lo + 2.0), hi - 2.0)
        anchor = brief["anchors"][i % len(brief["anchors"])]["pos_enu_m"]
        self.anchor = (float(anchor[0]), float(anchor[1]))
        self.station = (self.anchor[0] - STANDOFF_M, self.anchor[1] - STANDOFF_M)
        self.cam = next(s["sensor_id"] for s in config["sensors"] if s["kind"] == "camera")
        self.perception = None
        if config["obs_level"] != "L1":
            from anet4d.perception.ref_det import L2Perception

            self.perception = L2Perception(config)
        self.ground: float | None = None
        self.in_mission = config["start"]["state"] == "airborne"     # 空中出生时不起飞，直接飞往站位
        self.seen = {"person": 0, "vehicle": 0}

    def step(self, obs: dict, inbox: list[dict]) -> dict:
        k = obs["k"]
        if self.ground is None:
            self.ground = float(obs["ground_hint"]["dtm_m"])
        dets = self.perception.detect(obs) if self.perception is not None else obs["sensors"].get("detections", [])
        for d in dets:
            self.seen[d["class"]] = self.seen.get(d["class"], 0) + 1
        if k % LOG_EVERY == 0 and k > 0:
            print(f"{self.uav} k={k} detections person={self.seen['person']} vehicle={self.seen['vehicle']}", file=sys.stderr, flush=True)
        if not self.in_mission:
            if k == 0:
                return act(k, nav=nav_takeoff(self.alt))
            climbed = obs["nav"]["pos_enu_m"][2] - self.ground >= self.alt - 3.0
            if obs["status"]["mode"] != "airborne" or not climbed:
                return act(k)                         # 爬升期间不下发新指令，沿用起飞指令
            self.in_mission = True
        z = self.ground + self.alt
        return act(k, nav=nav_goto((*self.station, z), CRUISE_MPS), gimbal=gimbal_look_at(self.cam, (*self.anchor, self.ground)))
```

要点：导航指令使用 Env 交付的导航解（带噪估计），不使用真值；未下发新指令的步沿用上一指令；动作由 Env 侧的动作守卫按 schema 与机型限幅校验，回执写入运行目录的 `agents/<uav>/acts.jsonl`。

## 4. 运行示例 Agent

### 4.1 平台侧：启动 Env 并等待 Agent 接入

在仓库根目录（高保真仿真档在 d3-gz 镜像内）执行。`run_quickstart.py` 以 `anet4d.bench.make(..., physics="realistic")` 构造本地开发 Env（不排名），为每架机监听一个套接字：

```bash
PYTHONPATH=python:tools/sdk/anet4d/src python docs/sdk/examples/run_quickstart.py \
  --spec configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json \
  --worlds-root worlds --events-root d3data/events --physics realistic --gz-launcher local \
  --gz-export-root var/gz-export --duration-s 60 --seed 1 --out runs/quickstart-1 --sock-root /tmp/d4d-sock
```

```text
uav-01: waiting for an agent on /tmp/d4d-sock/uav-01/agent.sock
uav-02: waiting for an agent on /tmp/d4d-sock/uav-02/agent.sock
uav-03: waiting for an agent on /tmp/d4d-sock/uav-03/agent.sock
```

缺省等待 300 s（`--hello-timeout-s`）。`--gz-launcher docker` 时 Gazebo Sim 以 d3-gz 兄弟容器运行，宿主不需要安装 Gazebo Sim。

### 4.2 研究者侧：接入 Agent

在第 2 步的环境中、`plaza_watch.py` 所在目录执行，每架机一个进程：

```bash
for u in uav-01 uav-02 uav-03; do
  python -m anet4d.serve plaza_watch:PlazaWatch --sock /tmp/d4d-sock/$u/agent.sock 2> $u.log &
done
wait
```

`uav-01.log`：

```text
uav-01 k=100 detections person=119 vehicle=0
uav-01 k=200 detections person=701 vehicle=0
uav-01 k=300 detections person=1398 vehicle=0
uav-01 k=400 detections person=2082 vehicle=0
uav-01 k=500 detections person=2886 vehicle=0
```

回合结束后 Env 发送 CLOSE，Agent 进程以 0 退出；平台侧在标准输出打印一行摘要：

```text
{"acts_sha256": {"uav-01": "dccb13582cb385756127c40e8eddc8fba6297bd8311dc8ee910ff91380e1f703", "uav-02": "8bfa3f7133cbbb89be1d7217feacceb6284bf25587407be6014862e0fd191fa4", "uav-03": "687b45b8bb0821efcd455e6dac0f405c7638e3d4d92a0c8526f475cb01c2f112"}, "duration_s": 60.0, "event": "crop-handoff-001", "n_ticks": 600, "obs_level": "L1", "physics": "realistic", "reports": 0, "run_id": "quickstart-1", "schema": "drone4d.quickstart.summary.v1", "tier": "realistic", "timeouts": {"uav-01": 0, "uav-02": 0, "uav-03": 0}, "wall_s": 38.63, "wire": {"uav-01": {"exited": null, "protocol_errors": 0}, "uav-02": {"exited": null, "protocol_errors": 0}, "uav-03": {"exited": null, "protocol_errors": 0}}, "world": "oryahovo-crop1"}
```

| 字段 | 含义 |
|---|---|
| `n_ticks` | 控制步数（10 Hz × 60 s） |
| `timeouts` | 逐机超出每步墙钟预算的步数；超时步沿用上一指令 |
| `wire` | 逐机线协议错误数与异常退出（`null` 为正常） |
| `acts_sha256` | 逐机动作流 `agents/<uav>/acts.jsonl` 的 sha256，用于逐字节比对 |
| `reports` | 被接受的 report 数（示例不提交 report） |

运行目录 `runs/quickstart-1/` 含运行清单 `manifest.json`、动作与回执 `agents/<uav>/acts.jsonl`、录制 `raw.mcap` 与物理档记录 `phys/`。

### 4.3 进程内运行

开发调试时可在仓库环境中直接以进程内 Agent 运行同一回合，不经套接字：

```bash
PYTHONPATH=python:tools/sdk/anet4d/src:docs/sdk/examples python docs/sdk/examples/run_quickstart.py \
  --spec configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json \
  --worlds-root worlds --events-root d3data/events --physics realistic --gz-launcher local \
  --gz-export-root var/gz-export --duration-s 60 --seed 1 --out runs/quickstart-1-inproc --agent plaza_watch:PlazaWatch
```

```text
{"acts_sha256": {"uav-01": "dccb13582cb385756127c40e8eddc8fba6297bd8311dc8ee910ff91380e1f703", "uav-02": "8bfa3f7133cbbb89be1d7217feacceb6284bf25587407be6014862e0fd191fa4", "uav-03": "687b45b8bb0821efcd455e6dac0f405c7638e3d4d92a0c8526f475cb01c2f112"}, "duration_s": 60.0, "event": "crop-handoff-001", "n_ticks": 600, "obs_level": "L1", "physics": "realistic", "reports": 0, "run_id": "quickstart-1", "schema": "drone4d.quickstart.summary.v1", "tier": "realistic", "timeouts": {"uav-01": 0, "uav-02": 0, "uav-03": 0}, "wall_s": 36.7, "world": "oryahovo-crop1"}
```

三架机的 `acts_sha256` 与第 4.2 节相同：Agent 在独立环境（第 0 节为 Python 3.11 容器）中经线协议运行，与在 Env 进程内运行，动作流逐字节一致。等价的 Python 写法：

```python
import json

from anet4d.bench import make
from anet4d.runner import run_episode
from plaza_watch import PlazaWatch

with open("configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json", encoding="utf-8") as f:
    run_spec = json.load(f)
run_spec.update(ranked=False, duration_s=60)
env = make(run_spec, run_id="dev-1", seed=1, worlds_root="worlds", events_root="d3data/events", out_dir="runs/dev-1",
           physics="realistic", gz={"launcher": "local", "export_root": "var/gz-export"})
manifest = run_episode(env, lambda uav: PlazaWatch())
print(manifest["n_ticks"], manifest["pose_sha256"])
```

## 5. 切换物理档与观测档

Agent 代码不需要改动，只改变运行参数：

| 参数 | 物理档或观测档 | 平台侧要求 |
|---|---|---|
| `--physics realistic` | 高保真仿真档（Gazebo Sim 锁步，正式成绩的唯一来源） | d3-gz 镜像 |
| `--physics fast` | 快速仿真档（FleetSim，开发、训练与大规模搜索，不排名） | 仓库环境与机型目录 `vehicles/`（或以 `AWR_VEHICLES_DIR` 指定） |
| `--physics kinematic` | 运动学开发档（只用于开发与测试，不排名） | 仓库环境即可 |
| `--spec configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json` | L2 相机在环观测，检测由 `ref_det_v1` 从图像得到 | d3-sensorsvc 镜像与 GPU（EGL 渲染）；Agent 侧安装 extras `l2` |

以快速仿真档运行第 4 节的同一回合，平台侧只改 `--physics`，研究者侧的命令与第 4.2 节相同：

```bash
PYTHONPATH=python:tools/sdk/anet4d/src python docs/sdk/examples/run_quickstart.py \
  --spec configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json \
  --worlds-root worlds --events-root d3data/events --physics fast \
  --duration-s 60 --seed 1 --out runs/quickstart-1-fast --sock-root /tmp/d4d-sock
```

```text
{"acts_sha256": {"uav-01": "5ecd1215126d4e78eaa5f394778721f6f7c42ddf83790e1a99efc7807d0238ab", "uav-02": "c9a4b1250911d9beb540ea16f3d3c860c2087f94e4dbeb73ec61969ca3eb9c97", "uav-03": "9aaa44f0ff0256b65a98d4f1634c066b45085be0253bab794ac606b9d13f7937"}, "duration_s": 60.0, "event": "crop-handoff-001", "n_ticks": 600, "obs_level": "L1", "physics": "fast", "reports": 0, "run_id": "quickstart-1", "schema": "drone4d.quickstart.summary.v1", "tier": "fast", "timeouts": {"uav-01": 0, "uav-02": 0, "uav-03": 0}, "wall_s": 39.23, "wire": {"uav-01": {"exited": null, "protocol_errors": 0}, "uav-02": {"exited": null, "protocol_errors": 0}, "uav-03": {"exited": null, "protocol_errors": 0}}, "world": "oryahovo-crop1"}
```

快速仿真档与高保真仿真档的动力学模型不同，动作流不要求跨档一致；同一档内同一种子两次运行逐字节一致。`tests/d3/agentsdk_pkg/` 在临时 venv 中安装 wheel，以同一个 `plaza_watch.py` 运行运动学开发档与快速仿真档的 L1 回合、运动学开发档的 L2 回合（CPU 光栅替身与 wheel 中的参考检测器权重），并在 d3-gz 镜像内运行高保真仿真档回合。

## 6. 基线策略与参考检测器

三个基线策略随 SDK 发布，可直接作为研究起点。源码位于仓库 `tools/sdk/anet4d/` 下，sdist `anet4d-<版本>.tar.gz` 中的 `src/anet4d/` 与之逐字节一致：

| 名称 | 行为 | 源码 |
|---|---|---|
| `static_guard` | 以公开锚点与任务区中心为观察位，每机一个，到位后云台按 3 × 3 网格扫视 | `src/anet4d/baselines/static_guard.py` |
| `coverage_scan` | 任务区按机数切分为条带，割草机航线覆盖扫描，航带按相机地面足迹 70% 重叠，云台近正下视，不通信 | `src/anet4d/baselines/coverage_scan.py` |
| `track_share` | 检测到与简报外观匹配的目标后转入跟踪，每秒广播目击与跟踪声明，声明方丢失目标时由最近的空闲机接力 | `src/anet4d/baselines/track_share.py` |

公共部分（起飞与到位、多目标关联、report 规则与证据）在 `src/anet4d/baselines/common.py`；r6 参考行登记在 `src/anet4d/baselines/reference.py`。三个基线在 L1 与 L2 上都可运行，同一种子两次运行的动作流逐字节一致，report 均附证据并通过 `bench/act.v1` 校验。以基线代替示例接入第 4.1 节的 Env：

```bash
for u in uav-01 uav-02 uav-03; do
  python -m anet4d.serve --baseline track_share --sock /tmp/d4d-sock/$u/agent.sock 2> $u.log &
done
wait
```

参考检测器 `ref_det_v1` 的推理与 L2 感知适配层在 `src/anet4d/perception/ref_det.py`，定稿权重 `src/anet4d/perception/ref_det_v1.npz`（sha256 `da68c6cb3e91e51bd389aefd4b25e482301fa5633a0c6ecb6ebf7d5e16b584ce`，载入时校验）与模型卡 `model_card.json` 随 wheel 发布。检测器只用 CPU；正式评测 L2 每步墙钟预算为 0.5 s（`STEP_BUDGET_S`），L2 基线在 `std` 资源档容器（2 核、8 GB、无 GPU、BLAS 单线程）内回放 360 s 运行实测的最坏单步为 0.181 s（`STD_STEP_WALL_S`），自定义 Agent 可据此估算计算余量。

## 7. 打包为 Agent 镜像

正式评测中每架机的 Agent 运行在独立的隔离容器中（`--network none`、只读根文件系统、非 root 用户，`std` 资源档 2 核、8 GB、无 GPU），经挂载的 `/run/drone4d/agent.sock` 与 Env 通信。内部提交以平台 Agent 基础镜像为底，加入自己的包：

```dockerfile
FROM d3-agent-py:dev
COPY plaza_watch.py /opt/agent/plaza_watch.py
ENV PYTHONPATH=/opt/drone4d:/opt/agent
CMD ["python", "-m", "anet4d.serve", "plaza_watch:PlazaWatch"]
```

完整示例见 `tools/bench/d3/examples/close_track/`。容器内的 `anet4d` 版本由基础镜像决定；排名运行只接受按摘要引用的镜像。

## 8. 从源码构建 wheel 与 sdist

在仓库根目录执行（输出写入 `var/sdk/dist/`，不在源码树中留下构建目录；需要 setuptools ≥ 77）：

wheel 的字节依赖构建时的 `umask` 与源文件权限位，因此先设置 `umask 022` 并统一源目录权限；下列 sha256 在此条件下复现（2026-10-25 以 `anet4d` 0.3.2 复核）。

```bash
umask 022
rm -rf var/sdk && mkdir -p var/sdk && cp -r tools/sdk/anet4d var/sdk/src
chmod -R u=rwX,go=rX var/sdk/src
SOURCE_DATE_EPOCH=1760745600 python -m pip wheel --no-deps --no-build-isolation -w var/sdk/dist var/sdk/src
(cd var/sdk/src && SOURCE_DATE_EPOCH=1760745600 python -c "import setuptools.build_meta as b; b.build_sdist('../dist')")
ls var/sdk/dist
sha256sum var/sdk/dist/*.whl
```

```text
anet4d-0.3.2-py3-none-any.whl
anet4d-0.3.2.tar.gz
7a5fe21c338a0846aecd1a56b1fc8efdeee93582763f46ac595ea86754e53eb9  var/sdk/dist/anet4d-0.3.2-py3-none-any.whl
```

wheel 为纯 Python（`py3-none-any`），包含全部模块、参考检测器权重与模型卡；固定 `SOURCE_DATE_EPOCH` 时两次构建的 wheel 逐字节相同；sdist 的 tar 成员保留文件时间戳，sha256 随复制时刻变化，不作逐字节比对。离线安装所用的 wheel 目录需包含 `anet4d` 与下列依赖（Python 3.11 的 numpy 取 2.4.6，numpy 2.5 起不再支持 Python 3.11）：

| 包 | 版本 | 说明 |
|---|---|---|
| `httpx`、`httpcore`、`h11`、`anyio`、`idna`、`certifi`、`typing_extensions` | 0.28.1、1.0.9、0.16.0、4.15.1、3.20、2026.7.22、4.16.0 | 纯 Python |
| `msgpack` | 1.2.2 | 按解释器版本（cp311、cp312、cp313） |
| `numpy`、`pillow` | 2.5.3（cp311 为 2.4.6）、11.3.0 | extras `l2`，按解释器版本 |

## 9. 排错

| 现象 | 原因与处理 |
|---|---|
| `RuntimeError: anet4d.bench.make needs the ANet4D repository environment (awr package)` | 在只安装了 `anet4d` 的主机上构造 Env。Env 属于平台侧，按第 4.1 节在仓库环境中运行；研究者主机只运行第 4.2 节的 Agent 进程 |
| `ModuleNotFoundError: No module named 'numpy'`（L2 回合开始时） | 未安装 extras `l2`。执行 `pip install "anet4d[l2]"` |
| `ModuleNotFoundError: No module named 'httpx'`（平台镜像中 `from anet4d.bench import make`） | 平台镜像未安装 bench-api 客户端依赖。`run_quickstart.py` 改用等价的 `anet4d.local.make`，不受影响 |
| `ERROR: Could not find a version that satisfies the requirement msgpack>=1.0` | wheel 目录缺少与解释器版本或处理器架构匹配的二进制 wheel。核对 `python -V` 与 `uname -m`，补充对应的 cp311、cp312 或 cp313 x86_64 wheel |
| `awr.agentsdk.wire.WireError: 665 hello_timeout` | 平台侧在 `--hello-timeout-s` 内没有等到全部 Agent 接入。确认第 4.2 节的进程已启动、套接字路径一致；容器镜像架构与主机不一致时会以模拟方式运行并显著变慢，使用与主机相同架构的镜像 |
| `ValueError: agent socket path is … bytes (AF_UNIX limit ~108)` | `--sock-root` 过长，Unix 套接字路径上限约 108 字节。使用 `/tmp/d4d-sock` 等短路径 |
| `awr.phys.PhysError: 630: vehicle 'x500' has no FleetSim profile` | 快速仿真档找不到机型目录。在仓库根目录运行，或以 `AWR_VEHICLES_DIR` 指向含 `x500` 的机型目录 |
| 世界目录为只读挂载时 gz 导出失败 | 以 `--gz-export-root` 指定可写目录（第 4.1 节已指定 `var/gz-export`） |

## 10. 完成本文之后

已具备：

- 一个可安装的 `anet4d` 环境与可复用的 Agent 文件；
- 在高保真仿真档（Gazebo Sim 锁步）与快速仿真档（FleetSim）上以 3 架机完成的 L1 回合与运行目录；
- 线协议接入与进程内运行逐字节一致的核对方法；
- 三个基线策略与参考检测器的源码位置，可在其上修改。

尚不具备：

- 排名成绩：正式评测由基准作业在 dmax 隔离容器中运行，经内部提交进入原型榜；
- L2 相机在环观测的平台侧环境（需要 d3-sensorsvc 镜像与 GPU）。
