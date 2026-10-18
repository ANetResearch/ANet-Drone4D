# drone4d SDK

`drone4d` 是 Drone4D 基准的 Python SDK（M26-FR-080），不依赖仓库内的 `awr` 包。本版提供内部工作区的帧取回（P0a-1）：

```python
from drone4d.bench import BenchClient

with BenchClient("https://<bench host>", token="d3bt_...") as bc:
    run = bc.run("cap-crop-3uav-demo")
    for ch in run["frames"]:
        print(ch["uav_id"], ch["sensor_id"], ch["latest_seq"])
    f = bc.frame("cap-crop-3uav-demo", "uav-01", "cam0", 60)          # 交付字节（JPEG），sha256 已校验
    img = f.image()                                                  # numpy (H, W, 3) uint8，需要 extras "images"
    for f in bc.frames("cap-crop-3uav-demo", "uav-02", "cam0", 1, 300, stride=10):
        ...
    depth = bc.frame("cap-crop-3uav-demo", "uav-01", "cam0.depth", 60).depth()   # (H, W) float32，米
```

令牌只经 `Authorization: Bearer` 发送，不写入 URL。错误以 `BenchError(code, name, status, detail)` 抛出（原因码见 AWR-17 §18.12）。
## Agent 接口（P0a-3）

研究者实现 `DroneAgent.reset(config)` 与 `step(obs, inbox) -> act`；观测与动作是 `bench/obs.v1`、`bench/act.v1` 的普通 dict，
`drone4d.agent` 提供动作构造函数（`act`、`nav_goto`、`nav_takeoff`、`gimbal_look_at`、`out_msg`、`report` 等）。

```python
from drone4d.agent import DroneAgent, act, nav_goto, nav_takeoff, gimbal_look_at

class MyAgent(DroneAgent):
    def reset(self, config):
        self.cfg = config
        self.plaza = config["brief"]["anchors"][0]["pos_enu_m"]
    def step(self, obs, inbox):
        if obs["k"] == 0:
            return act(0, nav=nav_takeoff(60.0))
        p = self.plaza
        return act(obs["k"], nav=nav_goto((p[0] - 40, p[1] - 40, p[2] + 60), 8.0), gimbal=gimbal_look_at("eo", p))
```

本地开发（不排名）在仓库环境中构造 lockstep Env 并运行：

```python
from drone4d.bench import make
from drone4d.runner import run_episode
from drone4d.baselines import BASELINES

env = make(run_spec, seed=1, worlds_root="worlds", events_root="data/d3/events", out_dir="runs/dev-1", physics="kinematic")
manifest = run_episode(env, lambda uav: BASELINES["track_share"]())
```

基线策略 `static_guard`、`coverage_scan`、`track_share`（`drone4d.baselines`）在 L1 检测级观测上运行，同一种子两次运行的动作流
逐字节一致。正式评测由基准作业驱动，Agent 在隔离容器中经线协议交换帧。
