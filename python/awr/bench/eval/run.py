"""运行数据装载（Env 运行目录 + 事件版本目录）：检测真值映射、report、真值机队轨迹、链路记录与磁带状态。

识别真值行 `l1`：L1 运行取检测真值映射（truth/l1.jsonl），L2 运行取相机帧可见度归约（truth/l2.jsonl，含 `bbox_xywh`），两者键相同
（uav、seq、actor、h_unocc_px、vis）；`vis_boxes[(uav, seq)]` 为 L2 帧内可见演员框，供 EU 的框证据关联。

运行真值（评测器 0.3.0）：只读 `truth.RUN_TRUTH_FILES`（缺省在 `<run_dir>/truth/`；已移入隐藏区的 test 运行由 `truth_dir` 给出
`hidden/<eid>/truth/runs/<run_id>/`），经 `TruthAccess` 读取（test 事件经 HiddenStore 审计）；`inputs.truth_sha256` 只覆盖这些文件，
排名运行精简后复评逐字节一致（`truth` 模块说明）。"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.bench.eval.truth import RUN_TRUTH_FILES, TruthAccess, truth_digest

__all__ = ["RunData", "load_run"]

TICK_NS = 100_000_000
PRESENT = (1, 2)                     # moving、waiting：可被观测（在车内、建筑内、缺席与离场不计）


def _jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return _lines(p.read_bytes())


def _lines(b: bytes | None) -> list[dict]:
    if not b:
        return []
    return [json.loads(x) for x in b.decode("utf-8").splitlines() if x.strip()]


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
    truth_blobs: dict[str, bytes] = field(default_factory=dict, repr=False)     # 读取到的运行真值（`RUN_TRUTH_FILES`）

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

    def hashes(self, *, event_truth_sha256: str | None = None) -> dict:
        """`evaluation.json` 的 `inputs`：`truth_sha256` 只覆盖 `RUN_TRUTH_FILES`（`truth.truth_digest`）；`event_truth_sha256` 由评测器按
        计分事件集给出（`truth.event_truth_sha256`），缺省为 plan.json 注释的摘要。"""
        d = self.run_dir
        agent_files = [p for p in (d / "agents").rglob("*") if p.is_file()] + [d / "reports.jsonl", d / "net" / "records.jsonl"]
        if event_truth_sha256 is None:
            plan = json.loads((self.event_dir / "plan.json").read_text(encoding="utf-8"))
            event_truth_sha256 = hashlib.sha256(json.dumps(plan.get("annotations", []), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"raw_mcap_sha256": _sha_files(agent_files), "truth_sha256": truth_digest(self.truth_blobs),
                "tape_sha256": self.tape.hashes()["content_sha256"], "event_truth_sha256": event_truth_sha256}


def load_run(run_dir: Path, event_dir: Path, *, truth_dir: Path | None = None, access: TruthAccess | None = None) -> RunData:
    """装载运行目录与事件版本目录。`truth_dir` 缺省为 `<run_dir>/truth`；`access` 缺省直接读文件（见模块说明）。"""
    from awr.sim.events.tape import open_tape

    d = Path(run_dir)
    access = access or TruthAccess()
    td = Path(truth_dir) if truth_dir is not None else d / "truth"
    blobs = {n: access.read_bytes(td / n) for n in RUN_TRUTH_FILES if (td / n).is_file()}
    if "fleet.npz" not in blobs:
        raise FileNotFoundError(f"run truth {td / 'fleet.npz'} is missing")
    l1 = _lines(blobs.get("l1.jsonl"))
    l2 = _lines(blobs.get("l2.jsonl"))
    boxes: dict[tuple[str, int], list[dict]] = {}
    for r in l2:
        boxes.setdefault((r["uav"], int(r["seq"])), []).append(r)
    fz = np.load(io.BytesIO(blobs["fleet.npz"]))
    fleet = {k: fz[k] for k in fz.files}
    access.authorize_dir(Path(event_dir))
    return RunData(d, Path(event_dir), json.loads((d / "manifest.json").read_text()), json.loads((d / "task.json").read_text()), l2 or l1,
                   _jsonl(d / "reports.jsonl"), _jsonl(d / "net" / "records.jsonl"), _lines(blobs.get("contacts.jsonl")), fleet,
                   open_tape(Path(event_dir)), {(r["uav"], int(r["seq"]), r["det_id"]): r for r in l1}, boxes, blobs)
