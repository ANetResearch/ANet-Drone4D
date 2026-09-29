"""tests/recorder 辅助：MCAP 消息序列、回放宿主（LocalBus + LocalRing）与 Full64 行解析。"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import numpy as np

from awr.contracts import LAYOUT_ID
from awr.contracts.layouts import DRONE_STATE64

RUN = "r20260929-010203-abcd"
RUN2 = "r20260929-020304-beef"


def messages(path: Path) -> list[tuple[str, int, bytes]]:
    """文件顺序的 (topic, log_time, data)。"""
    from awr.recorder.reindex import iter_records

    topics: dict[int, str] = {}
    out = []
    for kind, rec in iter_records(path):
        if kind == "channel":
            topics[rec.id] = rec.topic
        elif kind == "message":
            ch, _seq, lt, _pt, data = rec
            out.append((topics[ch], lt, bytes(data)))
    return out


def host(runs_dir: Path, run_dir: Path, *, world_id: str = "shenzhen", content_version: str | None = "synth", **cfg: Any):
    from awr.recorder.config import ReplayCfg
    from awr.recorder.replay import ReplayHost
    from awr.runtime.bus import LocalBus
    from awr.runtime.statering import LocalRing

    ns = f"awr/{world_id}/t{secrets.token_hex(3)}"
    bus = LocalBus.open("replay-worker", namespace=ns)
    run_dir.mkdir(parents=True, exist_ok=True)
    h = ReplayHost(bus, run_dir=run_dir, runs_dir=runs_dir, world_id=world_id,
                   current_binding={"world_id": world_id, "layout_id": LAYOUT_ID, "content_version": content_version},
                   cfg=ReplayCfg(**cfg), ring_cls=LocalRing)
    return h, bus


def full_rows(data: bytes) -> np.ndarray:
    return np.frombuffer(data, DRONE_STATE64)


# ---------------------------------------------------------------- 契约 schema 校验
CONTRACTS = Path(__file__).resolve().parents[2] / "packages" / "contracts"


def schema_errors(rel: str, inst: Any) -> list[str]:
    import json

    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    reg = Registry()
    for p in sorted(CONTRACTS.rglob("*.schema.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    schema = json.loads((CONTRACTS / rel).read_text(encoding="utf-8"))
    v = Draft202012Validator(schema, registry=reg)
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}" for e in v.iter_errors(inst)]


# ---------------------------------------------------------------- 假 sim-core：LocalRing 写者 + roster 服务
class FakeSimRing:
    """按 125 Hz tap 写 LocalRing（Full64 + Lite32，SynthFleet 轨迹），服务 ctl/sim-core/roster。"""

    def __init__(self, ring_path: Path, bus: Any, n: int = 60) -> None:
        from awr.contracts import bus_keys
        from awr.contracts.layouts import SWARM_LITE32
        from awr.recorder.synth import SynthFleet
        from awr.runtime.statering import LocalRing

        self.fleet = SynthFleet(n)
        self.ring = LocalRing.create(ring_path, layout_id=LAYOUT_ID)
        self.ring.set_epoch(1)
        self.t = 0
        self.rate = 1.0
        self.roster_version = 1
        assert SWARM_LITE32.itemsize == 32
        self.h = bus.serve(bus_keys.ctl_roster("sim-core"), lambda r: r.reply_msg(self.fleet.roster(self.roster_version)))
        self.ring.heartbeat(0, 1, 1000)

    def tick(self, k: int = 1) -> None:
        from awr.contracts.layouts import SWARM_LITE32

        for _ in range(k):
            full = np.frombuffer(self.fleet.full_bytes(self.t), DRONE_STATE64)
            lite = np.frombuffer(self.fleet.lite_bytes(self.t), SWARM_LITE32)
            self.ring.publish(full, lite, self.t, self.roster_version)
            self.ring.heartbeat(self.t, 1, int(self.rate * 1000))
            self.t += max(8_000_000, round(self.rate) * 4_000_000)

    def close(self) -> None:
        from awr.runtime.statering import LocalRing

        self.h.close()
        path = self.ring.path
        self.ring.close()
        LocalRing.remove(path)
