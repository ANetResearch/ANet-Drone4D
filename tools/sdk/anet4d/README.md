# ANet4D Python SDK

`anet4d` 是 ANet4D 事件基准的 Python SDK（M26-FR-080），独立 wheel，不依赖仓库内的 `awr` 包，支持 Python 3.11 至 3.13。SDK 提供：

| 模块 | 内容 |
|---|---|
| `anet4d.agent` | Agent 基类 `DroneAgent` 与动作构造函数（`act`、`nav_takeoff`、`nav_goto`、`gimbal_look_at`、`out_msg`、`report` 等） |
| `anet4d.serve`、`anet4d.wire` | 容器入口与线协议 `drone4d.bench.wire.v1`（遗留标识，沿用原名）的 Agent 侧；命令行 `anet4d-agent` 与 `python -m anet4d.serve` 等价 |
| `anet4d.bench` | 本地开发 Env 的构造函数 `make`（需要仓库环境）与内部工作区 bench-api 客户端 `BenchClient` |
| `anet4d.runner` | 进程内运行一回合：`run_episode(env, agents)` |
| `anet4d.baselines` | 基线策略 `static_guard`、`coverage_scan`、`track_share` 与 r6 参考行 |
| `anet4d.perception` | 参考检测器 `ref_det_v1`（只用 CPU）与 L2 感知适配层，定稿权重与模型卡随包发布 |

## 安装

```bash
pip install --no-index --find-links /wheels "anet4d[l2]"
```

核心依赖为 `httpx` 与 `msgpack`；extras `l2` 增加 `numpy` 与 `pillow`，供 L2 观测下的参考检测器使用。`anet4d` 尚未发布到 PyPI，wheel 随版本发布附件提供，也可从源码构建（见快速上手第 8 节）。

## 旧包名 `drone4d`

V0.3.1 及以前的发行包与导入包名为 `drone4d`。V0.3.2 的 wheel 中保留 `drone4d` 作为已弃用的兼容别名：`import drone4d` 与 `drone4d.<子模块>` 得到与 `anet4d` 相同的模块对象，并发出 DeprecationWarning；`python -m drone4d.serve` 与命令 `drone4d-agent` 转发到 `anet4d.serve`，启动时在 stderr 提示改用新名。别名在下一版本移除，请改为 `import anet4d`、`python -m anet4d.serve` 与 `anet4d-agent`。线协议、模式标识（`drone4d.bench.*.v1` 等）、套接字路径 `/run/drone4d/agent.sock` 与环境变量 `DRONE4D_AGENT_SOCK` 属于遗留标识，不随更名改变。

## 最小示例

研究者实现 `reset(config)` 与 `step(obs, inbox)`；观测与动作是 `bench/obs.v1`、`bench/act.v1` 的普通 dict：

```python
from anet4d.agent import DroneAgent, act, gimbal_look_at, nav_goto, nav_takeoff


class MyAgent(DroneAgent):
    def reset(self, config):
        super().reset(config)
        self.plaza = config["brief"]["anchors"][0]["pos_enu_m"]

    def step(self, obs, inbox):
        if obs["k"] == 0:
            return act(0, nav=nav_takeoff(60.0))
        p = self.plaza
        return act(obs["k"], nav=nav_goto((p[0] - 40, p[1] - 40, p[2] + 60), 8.0), gimbal=gimbal_look_at("eo", p))
```

Agent 进程经 Unix 套接字接入平台侧的 Env：

```bash
python -m anet4d.serve my_agent:MyAgent --sock /tmp/d4d-sock/uav-01/agent.sock
python -m anet4d.serve --baseline track_share --sock /tmp/d4d-sock/uav-02/agent.sock
```

在仓库环境中也可进程内运行（开发运行，不排名）：

```python
from anet4d.bench import make
from anet4d.runner import run_episode

env = make(run_spec, seed=1, worlds_root="worlds", events_root="d3data/events", out_dir="runs/dev-1", physics="realistic")
manifest = run_episode(env, lambda uav: MyAgent())
```

## bench-api 客户端

```python
from anet4d.bench import BenchClient

with BenchClient("https://<bench host>", token="<token>") as bc:
    run = bc.run("cap-crop-3uav-demo")
    f = bc.frame("cap-crop-3uav-demo", "uav-01", "cam0", 60)          # 交付字节（JPEG），sha256 已校验
    img = f.image()                                                  # numpy (H, W, 3) uint8，需要 extras "l2"
```

令牌只经 `Authorization: Bearer` 发送，不写入 URL；错误以 `BenchError(code, name, status, detail)` 抛出（原因码见 AWR-17 §18.12）。

## 文档

- 快速上手（中文）：`docs/sdk/quickstart.zh-CN.md`
- Quickstart (English): `docs/sdk/quickstart.en.md`
- 文档站：https://docs.agentnetwork.org.cn/anet4d/

许可：Apache-2.0。
