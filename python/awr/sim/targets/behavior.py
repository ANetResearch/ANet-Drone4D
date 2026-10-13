"""识别物的感知态与被发现后的行为（M18-FR-013、FR-014；M18-M19 PRD §6.4；AWR-04 §7.4）。

状态 UNAWARE、SUSPICIOUS、REACTING、ALERTED、CALMING（契约 `Awareness`）；转移由 runtime.py 的判定驱动，本模块给出每个识别物的
动态状态 `TState`、行为段 `Resp` 与运动覆盖的计算：

| 段 | 类型 | 运动覆盖 |
|---|---|---|
| onset（进入 REACTING 时一次） | none、freeze（驻足 duration_s）、look_at（驻足并转向发现者，转向率受限） | hold、look |
| response（ALERTED、CALMING 中持续，≤ 2 项并行） | ignore（原运动）；flee（背离发现者水平投影 dist_m，speed_mps，区域边界滑行）；approach（靠近至 dist_m）；return（uav：先背离 dist_m（缺省 100 m）再回到路径起点或放置点）；emit（临时改声源，duration_s）；hide（P1：移向 search_m 内最近遮蔽格，到达后 occlusion 0.8） | move |
| 广播（同类，alert_radius_m 内，UNAWARE） | a + 0.1，注视广播源 5 s，不计发现 | look |

运动类 response 完成后驻足直至平复（return 完成后恢复原运动）；回到 UNAWARE 时恢复原运动模式（从当前位置续行）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .model import TargetConfig
from .motion import MotionState, Override

__all__ = ["BROADCAST_LOOK_S", "MOTION_RESPONSES", "Resp", "TState", "find_cover"]

MOTION_RESPONSES = ("flee", "approach", "return", "hide")
BROADCAST_LOOK_S = 5.0
RETURN_FLEE_M = 100.0
ARRIVE_M = 2.0
HIDE_OCCLUSION = 0.8


@dataclass
class Resp:
    kind: str
    params: dict
    t0: float
    odo0: float = 0.0
    done: bool = False
    phase: int = 0
    goal: tuple[float, float] | None = None


@dataclass
class TState:
    row: int
    target_no: int
    id: str
    cfg: TargetConfig
    rev: int
    ms: MotionState
    home: NDArray
    base_alert: float
    alert: float
    aware: int = 0
    episode: int = 0
    disc_n: int = 0
    disc_set: set[str] = field(default_factory=set)
    dwell: dict[str, int] = field(default_factory=dict)
    hit_info: dict[str, dict] = field(default_factory=dict)
    calm_t0: float = -1.0
    onset_kind: str = "none"
    onset_until: float = -1.0
    focus: str | None = None
    focus_pt: tuple[float, float] | None = None
    responses: list[Resp] = field(default_factory=list)
    emit_until: float = -1.0
    emit_over: dict | None = None
    look_until: float = -1.0
    look_pt: tuple[float, float] | None = None
    hidden: bool = False
    last_disc: dict | None = None
    occ: tuple | None = None   # (内外矩阵行, 参与者 id, 体元信息)；无机体在体内时为 None
    inside_n: int = 0
    sound_on: bool = False
    sound_phase: float = 0.0
    snd_key: tuple | None = None
    snd_bands: Any = None
    detail_rev_sent: int = -1
    detail_cfg_t: float = -1e18
    keepout_r: float = 0.0
    hold_after: bool = False   # 运动类 response 完成后驻足

    def occupied(self) -> list[list[str]]:
        """各敏感体内的我方机 id（detail 的 occupied_by，按敏感体下标）。"""
        out: list[list[str]] = [[] for _ in self.cfg.raw.get("sensitivity") or []]
        if self.occ is not None:
            ins, ids, meta = self.occ
            rows = ins.tolist()
            for q, (i, _kind) in enumerate(meta):
                out[i] = [ids[j] for j, v in enumerate(rows[q]) if v]
        return out

    @property
    def occupied_by(self) -> list[list[str]]:
        return self.occupied()

    # ------------------------------------------------------------ 行为
    def behavior_phase(self, t_s: float) -> dict | None:
        """detail.behavior：当前段、类型与剩余时间。"""
        if self.aware == 2 and self.onset_kind != "none":
            return {"phase": "onset", "type": self.onset_kind, "t_left_s": round(max(0.0, self.onset_until - t_s), 2)}
        live = [r for r in self.responses if not r.done]
        if self.aware in (3, 4) and live:
            r = live[0]
            left = None
            if r.kind == "emit":
                left = round(max(0.0, self.emit_until - t_s), 2)
            return {"phase": "response", "type": r.kind, "t_left_s": left}
        if self.look_until > t_s:
            return {"phase": "onset", "type": "look_at", "t_left_s": round(self.look_until - t_s, 2)}
        return None

    def start_onset(self, t_s: float, focus: str | None, focus_pt: tuple[float, float] | None) -> None:
        on = (self.cfg.raw.get("reaction") or {}).get("onset") or {"kind": "none"}
        self.onset_kind = str(on.get("kind", "none"))
        self.onset_until = t_s + (0.0 if self.onset_kind == "none" else float(on.get("duration_s", 0.0) or 0.0))
        self.focus, self.focus_pt = focus, focus_pt

    def start_response(self, t_s: float) -> None:
        resp = list((self.cfg.raw.get("reaction") or {}).get("response") or [])
        self.responses = [Resp(str(r.get("kind")), dict(r), t_s, self.ms.odo_m) for r in resp]
        self.hold_after = False
        for r in self.responses:
            if r.kind == "emit":
                self.emit_over = dict(r.params.get("sound") or {})
                self.emit_until = t_s + float(r.params.get("duration_s", 0.0) or 0.0)
            elif r.kind == "ignore":
                r.done = True

    def clear_behavior(self) -> None:
        self.onset_kind, self.onset_until = "none", -1.0
        self.responses = []
        self.emit_over, self.emit_until = None, -1.0
        self.hold_after = False
        self.focus, self.focus_pt = None, None
        if self.hidden:
            self.hidden = False

    def override(self, t_s: float, focus_now: tuple[float, float] | None, world: Any) -> tuple[Override | None, bool]:
        """当前运动覆盖与是否允许原运动（base_active）。focus_now 为发现者当前水平位置（不在机群时为最后已知）。"""
        pt = focus_now or self.focus_pt
        if self.look_until > t_s and self.aware in (0, 3, 4) and self.look_pt is not None and not self.responses:
            return Override("look", self.look_pt), False
        if self.aware == 2:
            if self.onset_kind == "look_at":
                return Override("look", pt), False
            if self.onset_kind == "freeze":
                return Override("hold"), False
            return None, True
        if self.aware not in (3, 4):
            return None, True
        if self.emit_over is not None and t_s >= self.emit_until:
            self.emit_over = None
            for r in self.responses:
                if r.kind == "emit":
                    r.done = True
        motion = [r for r in self.responses if r.kind in MOTION_RESPONSES]
        if not motion:
            return None, True
        r = motion[0]
        m = self.cfg.raw.get("motion") or {}
        vmax = float(m.get("speed_max_mps", 1.0)) or 1.0
        if r.done:
            if r.kind == "return":
                return None, True
            return Override("hold"), False
        if r.kind == "flee":
            if self.ms.odo_m - r.odo0 >= float(r.params.get("dist_m", 100.0)) or pt is None:
                r.done = True
                return Override("hold"), False
            return Override("move", pt, float(r.params.get("speed_mps", vmax)), away=True), False
        if r.kind == "approach":
            if pt is None:
                return Override("hold"), False
            return Override("move", pt, vmax, away=False, stop_within_m=float(r.params.get("dist_m", 20.0))), False
        if r.kind == "return":
            spd = float(r.params.get("speed_mps", vmax))
            if r.phase == 0:
                if pt is None or self.ms.odo_m - r.odo0 >= float(r.params.get("dist_m", RETURN_FLEE_M)):
                    r.phase = 1
                    path = m.get("path") or []
                    r.goal = (float(path[0][0]), float(path[0][1])) if path else (float(self.home[0]), float(self.home[1]))
                else:
                    return Override("move", pt, spd, away=True), False
            g = r.goal or (float(self.home[0]), float(self.home[1]))
            if math.hypot(g[0] - self.ms.pos[0], g[1] - self.ms.pos[1]) <= ARRIVE_M:
                r.done = True
                self.ms.seg, self.ms.dirn, self.ms.path_done, self.ms.dwell_until = 0, 1, False, -1.0
                return None, True
            return Override("move", g, spd, away=False, stop_within_m=0.5), False
        if r.kind == "hide":
            if r.goal is None:
                r.goal = find_cover(world, self.ms.pos, float(r.params.get("search_m", 50.0)))
                if r.goal is None:
                    r.goal = (float(self.ms.pos[0]), float(self.ms.pos[1]))
            if math.hypot(r.goal[0] - self.ms.pos[0], r.goal[1] - self.ms.pos[1]) <= ARRIVE_M:
                r.done = True
                self.hidden = True
                return Override("hold"), False
            return Override("move", r.goal, vmax, away=False, stop_within_m=0.5), False
        return None, True


def find_cover(world: Any, pos: NDArray, search_m: float, step_m: float = 2.0) -> tuple[float, float] | None:
    """search_m 内最近的遮蔽格：本格为地面（DSM − DTM < 0.5 m）且相邻格 DSM − DTM > 3 m（贴墙处，P1 hide）。"""
    if world is None or search_m <= 0.0:
        return None
    n = int(search_m // step_m)
    xs = np.arange(-n, n + 1) * step_m
    gx, gy = np.meshgrid(xs, xs)
    r = np.hypot(gx, gy).ravel()
    keep = r <= search_m
    px = pos[0] + gx.ravel()[keep]
    py = pos[1] + gy.ravel()[keep]
    rr = r[keep]
    order = np.argsort(rr, kind="stable")
    px, py = px[order], py[order]
    try:
        xy = np.c_[px, py]
        hag = world.height_dsm(xy) - world.ground_dtm(xy)
        cand = np.flatnonzero(hag < 0.5)
        if cand.size == 0:
            return None
        off = np.array([[step_m, 0.0], [-step_m, 0.0], [0.0, step_m], [0.0, -step_m]])
        for k in cand[:2000]:
            nb = xy[k] + off
            if np.any(world.height_dsm(nb) - world.ground_dtm(nb) > 3.0):
                return float(px[k]), float(py[k])
    except Exception:
        return None
    return None
