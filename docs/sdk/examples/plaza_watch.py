"""快速上手示例 Agent `PlazaWatch`（docs/sdk/quickstart.zh-CN.md、quickstart.en.md）。

每架机起飞到各自的高度层，飞往分配给本机的公开锚点斜上方站位，云台持续对准锚点，并每 10 s 向标准错误输出累计检测数。
L1 直接读取观测中的检测列表；L2 由参考检测器 `ref_det_v1`（`drone4d.perception.ref_det.L2Perception`，需要 extras `l2`）
从相机图像得到同形的检测。示例不发送机间消息、不提交 report；report 与协同的写法见基线策略 `drone4d.baselines`。

同一份代码不改动地运行于各物理档（`physics = realistic`、`fast`）与观测档（L1、L2）；策略内无随机数、无墙钟依赖，
同一种子两次运行的动作流逐字节一致。
"""

from __future__ import annotations

import sys

from drone4d.agent import DroneAgent, act, gimbal_look_at, nav_goto, nav_takeoff

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
            from drone4d.perception.ref_det import L2Perception

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
