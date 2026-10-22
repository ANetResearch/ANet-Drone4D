"""`relay_scan` Agent：航带扫描搜索与单机接力跟踪（排行榜 v1 的第二个内部示例提交，D3-AC-43）。

- 扫描：任务区（简报给出搜索先验时为先验搜索区）外接框按 `coverage_scan` 的口径铺航带（间距取相机俯视地面足迹宽的 30%，即 70%
  重叠，按任务包络中值高度计算，各机得到同一组航带，不小于 10 m）；与 `coverage_scan` 的连续条带分区不同，航带按机序交错分配（第 i 架机取第 i、i + n、… 条，有先验时先按先验
  质量降序排列），各机同时覆盖任务区的不同部分；每条航带从靠近上一条终点的一端进入，云台看航向前方 10 m。
- 跟踪：目击、跟踪声明与 report 规则沿用 `track_share`（每秒广播一次目击与声明，report 后广播告知，他机在抑制时长内不重复报同类型）。
  每个目标只由一架机跟踪：他机 3 s 内声明了该目标时，本机只在自己也看到该目标且（距离, 机号）更小时接手；无人声明时，只有本机是
  已知位置中最近的空闲机（他机每秒广播位置与忙闲，3 s 内有效）或本机自己看到目标时才接手。声明方停止声明 3 s 后由最近的空闲机接力；
  其余机继续扫描。跟踪站位、丢失后的外推与 Re-ID 词表下的守候时长同 `track_share`。
- 巡航高度取任务包络中值并按机号分层（基线公共规则）；策略内无随机数、无墙钟依赖，同一输入的动作流逐字节一致。

容器入口：`python -m drone4d.serve relay_scan.agent:Agent`。
"""

from __future__ import annotations

import math

from drone4d.agent import decode_msg, out_msg
from drone4d.baselines import TrackShare
from drone4d.baselines.track_share import STANDOFF_M

__all__ = ["Agent", "scan_waypoints"]

OVERLAP = 0.7                 # 相邻航带的地面足迹重叠率（与 coverage_scan 相同）
MIN_SPACING_M = 10.0
LOOK_AHEAD_M = 10.0
ARRIVE_M = 6.0
CLAIM_S = 3.0                 # 他机跟踪声明与位置广播的有效期
EXTRAP_S = 10.0               # 丢失后按最后已知速度外推的上限（track_share）


def scan_waypoints(config: dict, alt_agl_m: float, area: list[tuple[float, float]], prior, uav: str, peers: list[str]) -> list[tuple[float, float]]:
    """本机的扫描航点序列（航带两端交替）：航带间距由相机水平视场与名义巡航高度（任务包络中值，不含机间分层，各机算出同一组
    航带）确定，航带按机序交错分配。"""
    cam = next(s for s in config["sensors"] if s["kind"] == "camera")
    hfov = 2.0 * math.atan((cam["width"] / 2.0) / cam["K"][0])
    spacing = max(MIN_SPACING_M, 2.0 * alt_agl_m * math.tan(hfov / 2.0) * (1.0 - OVERLAP))
    xs, ys = zip(*(prior.area if prior is not None else area), strict=True)
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    lanes, x = [], x0 + spacing / 2.0
    while x < x1:
        lanes.append(x)
        x += spacing
    lanes = lanes or [(x0 + x1) / 2.0]
    order = prior.lane_order(lanes, y0, y1, spacing) if prior is not None else list(range(len(lanes)))
    fleet = sorted([uav, *peers])
    i, n = fleet.index(uav), len(fleet)
    mine = order[i::n] or [order[i % len(order)]]
    wps: list[tuple[float, float]] = []
    end = None
    for j in mine:
        a, b = (lanes[j], y0), (lanes[j], y1)
        if end is not None and math.dist(end, b) < math.dist(end, a):
            a, b = b, a
        wps += [a, b]
        end = b
    return wps


class Agent(TrackShare):
    name = "relay_scan"

    def reset(self, config: dict) -> None:
        super().reset(config)
        lo, hi = config["brief"]["area"]["alt_agl_range_m"]
        self.wps = scan_waypoints(config, 0.5 * (float(lo) + float(hi)), self.area, self.prior, self.uav, self.peers)
        self.wi = 0
        self.peer_state: dict[str, tuple[int, tuple[float, float], bool]] = {}   # 机号 → (t, xy, 忙)

    # ------------------------------------------------------------------ 消息
    def on_inbox(self, inbox):
        out = super().on_inbox(inbox)
        for m in inbox:
            p = decode_msg(m)
            if isinstance(p, dict) and p.get("kind") == "state" and m.get("src") != self.uav:
                t = int(p["t"])
                if m["src"] not in self.peer_state or self.peer_state[m["src"]][0] < t:
                    self.peer_state[m["src"]] = (t, (float(p["xy"][0]), float(p["xy"][1])), bool(p["busy"]))
        return out

    def messages(self, obs):
        out = super().messages(obs)
        if obs["k"] % 10 == 0:
            out.append(out_msg("*", "state", {"kind": "state", "t": obs["t_ns"], "xy": [round(self.pos[0], 1), round(self.pos[1], 1)],
                                              "busy": self.track is not None}))
        return out

    # ------------------------------------------------------------------ 任务
    def _free_peer_closer(self, t: int, xy: tuple[float, float], dist: float) -> bool:
        """已知位置（3 s 内）的空闲他机中是否有比本机更近者（距离相同按机号）。"""
        for u, (ts, pxy, busy) in sorted(self.peer_state.items()):
            if busy or (t - ts) / 1e9 >= CLAIM_S:
                continue
            if (math.dist(pxy, xy), u) < (dist, self.uav):
                return True
        return False

    def _pick(self, t: int, mine: set[int]) -> int | None:
        me = self.pos[:2]
        cand = []
        for i, (ts, xy, _v, _src) in sorted(self.shared.items()):
            if (t - ts) / 1e9 > self.hold_s:
                continue
            dist = math.dist(xy, me)
            c = self.claims.get(i)
            if c and c[1] != self.uav and (t - c[0]) / 1e9 < CLAIM_S:
                if i not in mine or (c[2], c[1]) < (dist, self.uav):
                    continue                                       # 他机正在跟踪：不抢（本机看到且更近时接手）
            elif i not in mine and i != self.track and self._free_peer_closer(t, xy, dist):
                continue                                           # 无人声明：由最近的空闲机接手
            cand.append((0 if i in mine else 1, 0 if i == self.track else 1, dist, i))
        return min(cand)[3] if cand else None

    def mission(self, obs, inbox):
        t = obs["t_ns"]
        me = self.pos[:2]
        mine: set[int] = set()
        for tr in self.target_tracks(t, 2.0):
            i = self.target_no(tr)
            if i is not None:
                mine.add(i)
                self.shared[i] = (t, tr.xy, tr.vel, self.uav)
        self.track = self._pick(t, mine)
        if self.track is not None:
            ts, xy, vel, _src = self.shared[self.track]
            dt = min((t - ts) / 1e9, EXTRAP_S)
            px, py = xy[0] + vel[0] * dt, xy[1] + vel[1] * dt
            d = math.dist((px, py), me)
            ux, uy = ((me[0] - px) / d, (me[1] - py) / d) if d > 1e-6 else (1.0, 0.0)
            return (px + STANDOFF_M * ux, py + STANDOFF_M * uy), (px, py)
        if math.dist(self.wps[self.wi], me) < ARRIVE_M:
            self.wi = (self.wi + 1) % len(self.wps)
        w = self.wps[self.wi]
        d = math.dist(w, me)
        ux, uy = ((w[0] - me[0]) / d, (w[1] - me[1]) / d) if d > 1e-6 else (0.0, 1.0)
        return w, (me[0] + LOOK_AHEAD_M * ux, me[1] + LOOK_AHEAD_M * uy)
