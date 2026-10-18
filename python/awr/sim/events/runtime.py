"""事件运行期接入（M23 §6.12）：磁带运动源、环境轨投递与事件状态。插件登记见 `awr.sim.events.plugin`（只出现在 D3_PLUGINS）。

- `TapeMotionSource`（M18 扩展点 3）：`pose(tid, t_ns)` 取磁带脚本演员在事件时刻 offset + t 的插值状态（只解 S 子块）；
  状态 1 移动、2 等待为 enabled，其余（0 缺席、3 在车内、4 在建筑内、5 离场）不运行判据（§6.12.2）。hidden 恒为 false。
- `EventRuntime.event_clock`（stage 199，每主 tick）：把事件时刻落在 (t_now, t_now + dt] 内的环境轨操作经
  `CommandEngine.submit_internal` 投递为 `env/set`，在下一步边界生效（§6.11）；首次调用补投 t ≤ t_now + dt 的全部操作。
- 识别物 id：M18 要求 `^[a-z0-9][a-z0-9-]{0,31}$`，演员 id 转小写、`_` 改 `-`（重名时加序号），映射写入状态与 `event_run`。
- 装配：会话配置段 `event` = `{event_id, version, tape_uri, tape_content_sha256, mode, offset_s?}`；装配时全量校验磁带（614）
  与世界版本（615）；识别物以 `motion.mode = timeline`、`reaction_policy = <mode>` 加入（`enabled` 保持 true，由运动源逐 tick
  决定是否参与判定）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import numpy as np

from awr.sim.events.errors import EventError
from awr.sim.events.tape import TapeReader, open_tape

__all__ = ["EventRuntime", "TapeMotionSource", "target_id"]

_ENABLED = (1, 2)


def target_id(actor_id: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9-]", "-", actor_id.lower()).strip("-")[:28] or "actor"
    if not base[0].isalnum():
        base = "a" + base[:27]
    tid, k = base, 1
    while tid in taken:
        k += 1
        tid = f"{base}-{k}"
    taken.add(tid)
    return tid


class TapeMotionSource:
    """M18 运动源：同一 t_ns 的多次查询共用一次 `actor_states`（脚本演员子集）。"""

    def __init__(self, reader: TapeReader, ids: dict[str, int], offset_ns: int = 0) -> None:
        self.r = reader
        self.idx = dict(ids)                     # tid -> 演员 idx
        self.offset_ns = int(offset_ns)
        self._sel = np.array(sorted(set(self.idx.values())), np.int64)
        self._row = {int(i): j for j, i in enumerate(self._sel)}
        self._t: int | None = None
        self._st: Any = None

    def states(self, t_ns: int):
        if self._t != t_ns:
            self._st = self.r.actor_states(self.offset_ns + int(t_ns), ids=self._sel)
            self._t = int(t_ns)
        return self._st

    def pose(self, tid: str, t_ns: int):
        k = self.idx.get(tid)
        if k is None:
            return None
        st = self.states(t_ns)
        j = self._row[k]
        return st.pos_enu_m[j].copy(), float(st.yaw_rad[j]), st.vel_enu_mps[j].copy(), int(st.state[j]) in _ENABLED, False


class EventRuntime:
    def __init__(self) -> None:
        self.reader: TapeReader | None = None
        self.spec: dict = {}
        self.source: TapeMotionSource | None = None
        self.tids: dict[str, str] = {}           # 演员 id -> 识别物 id
        self.ops: list[tuple[int, dict]] = []    # (事件时刻 ns, env/set 参数)
        self.next_op = 0
        self.applied: list[dict] = []
        self.track: list[bytes] = []             # 磁带 tick 上的脚本演员毫米定点位置（scripted_track_sha256）
        self.t_ns = 0

    @property
    def offset_ns(self) -> int:
        return round(float(self.spec.get("offset_s", 0.0)) * 1e9)

    # ------------------------------------------------------------ 装配
    def load(self, section: dict, *, world_content_version: str | None, data_root: Path | None = None) -> TapeReader:
        for k in ("event_id", "version", "tape_uri", "tape_content_sha256", "mode"):
            if k not in section:
                raise EventError(611, f"event section misses {k!r}")
        if section["mode"] not in ("exogenous", "interactive"):
            raise EventError(611, f"unknown event mode {section['mode']!r}")
        if section["mode"] == "interactive":
            raise EventError(611, "interactive mode is not available in P0a (exogenous only)")
        p = Path(section["tape_uri"])
        if not p.is_absolute() and data_root is not None:
            p = Path(data_root) / p
        r = open_tape(p.parent if p.name == "tape.bin" else p, verify=False)
        r.verify(expect_content_sha256=str(section["tape_content_sha256"]))
        if world_content_version is not None:
            r.check_world(world_content_version)
        self.reader, self.spec = r, dict(section)
        taken: set[str] = set()
        self.tids = {a["id"]: target_id(a["id"], taken) for a in r.actors_info(scripted_only=True)}
        idx = {a["id"]: a["idx"] for a in r.actors_info(scripted_only=True)}
        self.source = TapeMotionSource(r, {t: idx[a] for a, t in self.tids.items()}, self.offset_ns)
        off = self.offset_ns
        self.ops = sorted(((int(e["t_ms"]) * 1_000_000 - off, {k: v for k, v in e["op"].items() if k != "kind"})
                           for e in r.header.get("env_track") or []), key=lambda x: x[0])
        self.next_op, self.applied, self.track = 0, [], []
        return r

    def target_configs(self) -> list[dict]:
        assert self.reader is not None
        out = []
        for a in self.reader.actors_info(scripted_only=True):
            L, W, H = a["size_m"]
            out.append({"id": self.tids[a["id"]], "label": a["id"], "class": a["cls"], "pos": list(a["pos0_enu_m"]),
                        "size_m": {"l": L, "w": W, "h": H}, "motion": {"mode": "timeline"}, "reaction_policy": self.spec["mode"]})
        return out

    # ------------------------------------------------------------ 运行
    def event_clock(self, t_now_ns: int, dt_ns: int, submit) -> None:
        """投递 (t_now, t_now + dt] 内的环境操作；`submit(cid, args) -> dict`。磁带 tick 上记录脚本演员轨迹。"""
        self.t_ns = int(t_now_ns)
        while self.next_op < len(self.ops) and self.ops[self.next_op][0] <= t_now_ns + dt_ns:
            t_op, args = self.ops[self.next_op]
            cid = f"m23e:{self.next_op}"
            res = submit(cid, args) or {}
            self.applied.append({"t_ms": (t_op + self.offset_ns) // 1_000_000, "cid": cid, "status": res.get("status", "unknown")})
            self.next_op += 1
        if self.source is not None and (self.offset_ns + t_now_ns) % 100_000_000 == 0:
            st = self.source.states(t_now_ns)
            self.track.append(np.round(st.pos_enu_m * 1000.0).astype("<i8").tobytes())

    def status(self) -> dict:
        r = self.reader
        if r is None:
            return {"v": 1, "code": 0, "loaded": False}
        t_ev = self.offset_ns + self.t_ns
        env = r.env_at(t_ev)
        return {"v": 1, "code": 0, "loaded": True, "event_id": r.header["event_id"], "version": r.header["event_version"],
                "t_ms": t_ev // 1_000_000, "mode": self.spec["mode"],
                "env": {"wind_speed_ref_mps": env.wind_speed_mps, "wind_dir_from_deg": env.wind_dir_from_deg,
                        "turb_sigma_mps": env.gust_sigma_mps, "rain_mmh": env.rain_mm_h, "mor_bg_m": env.visibility_m,
                        "fog_top_agl_m": env.fog_top_agl_m, "cloud_cover": env.cloud},
                "comms_active": [z["id"] for z in r.comm_zones_at(t_ev)]}

    def run_record(self) -> dict:
        r = self.reader
        if r is None:
            return {"v": 1, "loaded": False}
        return {"v": 1, "tape_content_sha256": self.spec["tape_content_sha256"], "mode": self.spec["mode"],
                "event_id": r.header["event_id"], "version": r.header["event_version"], "offset_s": self.offset_ns / 1e9,
                "targets": dict(self.tids), "scripted_track_sha256": hashlib.sha256(b"".join(self.track)).hexdigest(),
                "track_ticks": len(self.track), "env_ops_applied": list(self.applied)}
