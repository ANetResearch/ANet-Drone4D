"""运行数据装载（Env 运行目录 + 事件版本目录）：检测真值映射、report、真值机队轨迹、链路记录与磁带状态。

识别真值行 `l1`：L1 运行取检测真值映射（truth/l1.jsonl），L2 运行取相机帧可见度归约（truth/l2.jsonl，含 `bbox_xywh`），两者键相同
（uav、seq、actor、h_unocc_px、vis）；`vis_boxes[(uav, seq)]` 为 L2 帧内可见演员框，供 EU 的框证据关联。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["RunData", "load_run"]

TICK_NS = 100_000_000
PRESENT = (1, 2)                     # moving、waiting：可被观测（在车内、建筑内、缺席与离场不计）


def _jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _sha_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        if p.exists():
            h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


@dataclass
class RunData:
    run_dir: Path
    event_dir: Path
    manifest: dict
    task: dict
    l1: list[dict]
    reports: list[dict]
    net: list[dict]
    contacts: list[dict]
    fleet: dict
    tape: object
    det_actor: dict[tuple[str, int, str], dict]
    vis_boxes: dict[tuple[str, int], list[dict]]

    @property
    def uavs(self) -> list[str]:
        return [str(u) for u in self.fleet["uavs"]]

    @property
    def n_ticks(self) -> int:
        return int(self.manifest["n_ticks"])

    def present(self, actor_ids: list[str]) -> np.ndarray:
        """(n_ticks + 1, n) bool：tick 末时刻在场（可被观测）。"""
        idx = {a["id"]: a["idx"] for a in self.tape.actors_info()}
        sel = [idx[a] for a in actor_ids]
        out = np.zeros((self.n_ticks + 1, len(sel)), bool)
        for k in range(self.n_ticks + 1):
            st = self.tape.actor_states(k * TICK_NS, sel)
            out[k] = np.isin(st.state, PRESENT)
        return out

    def hashes(self) -> dict:
        d = self.run_dir
        agent_files = [p for p in (d / "agents").rglob("*") if p.is_file()] + [d / "reports.jsonl", d / "net" / "records.jsonl"]
        truth_files = [p for p in (d / "truth").rglob("*") if p.is_file() and p.name != "run_seed.json"]
        plan = json.loads((self.event_dir / "plan.json").read_text(encoding="utf-8"))
        ev = json.dumps(plan.get("annotations", []), sort_keys=True, separators=(",", ":")).encode()
        return {"raw_mcap_sha256": _sha_files(agent_files), "truth_sha256": _sha_files(truth_files),
                "tape_sha256": self.tape.hashes()["content_sha256"], "event_truth_sha256": hashlib.sha256(ev).hexdigest()}


def load_run(run_dir: Path, event_dir: Path) -> RunData:
    from awr.sim.events.tape import open_tape

    d = Path(run_dir)
    l1 = _jsonl(d / "truth" / "l1.jsonl")
    l2 = _jsonl(d / "truth" / "l2.jsonl")
    boxes: dict[tuple[str, int], list[dict]] = {}
    for r in l2:
        boxes.setdefault((r["uav"], int(r["seq"])), []).append(r)
    fz = np.load(d / "truth" / "fleet.npz")
    fleet = {k: fz[k] for k in fz.files}
    return RunData(d, Path(event_dir), json.loads((d / "manifest.json").read_text()), json.loads((d / "task.json").read_text()), l2 or l1,
                   _jsonl(d / "reports.jsonl"), _jsonl(d / "net" / "records.jsonl"), _jsonl(d / "truth" / "contacts.jsonl"), fleet,
                   open_tape(Path(event_dir)), {(r["uav"], int(r["seq"]), r["det_id"]): r for r in l1}, boxes)
