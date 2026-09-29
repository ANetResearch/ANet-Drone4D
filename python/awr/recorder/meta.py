"""`runs/<run>/meta.json` 的原子更新（M12 §7.5.4、FR-038；AWR-16 §13.7；契约 `rec/meta.schema.json`）。

supervisor 在运行开始时写初始 meta（M11-R-to-M12 第 3 条）；recorder 在段开闭与每 10 s（墙钟）以"写临时文件后原子改名"
更新 `segments[]` 的 `t_end_ns`、`bytes`、`bytes_per_sim_s`、`decimation`、`lineage`，任意时刻 kill -9 后文件都可解析
（M12-AC-038）。

契约 `segments[]` 为 additionalProperties: false，M12 §7.5.4 的逐段补充字段（`events_per_sim_s`、`gaps`、`sidecars`、
`speed_max`、`detail_coverage`、`sim_segment`）按现行 schema 写在顶层：`sidecars{"<k>": {ovw, evx, sim_segment,
events_per_sim_s, speed_max, detail_coverage}}`（`sidecars` 为 object）、`gaps[{segment, t_from_ns, t_to_ns, overrun,
dropped}]`、`events_per_sim_s` 与 `speed_max` 取全运行值、`detail_coverage{"<k>": "all" | "interest"}`、`source = "sim"`；
逐段字段登记请求见实现报告（M12-to-M00）。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from awr.contracts import CONTRACTS_VERSION, LAYOUT_ID

__all__ = ["MetaFile", "atomic_write_json", "seg_file"]


def seg_file(k: int) -> str:
    return f"rec-{k:03d}.mcap"


def atomic_write_json(path: Path, obj: Any, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    data = (json.dumps(obj, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _default_meta(run_id: str, binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "awr.run.meta.v1", "schema_version": "1.0.0", "run_id": run_id, "created_wall_ns": str(time.time_ns()),
        "keep": False, "binding": binding,
        "sim": {"kernel": "numpy", "numpy_version": "", "python_version": "", "fastmath": False, "world_seed": 0},
        "scenario": None, "vehicles_profiles": [], "presets_sha256": "0" * 64,
        "recording_policy": {"swarm_hz": 25, "full_hz": 125, "marked_ids": [], "full_all_if_n_le": 50, "state_ext_hz": 2,
                             "safety_hz": 5, "keyframe_every_s": 5},
        "segments": [],
    }


class MetaFile:
    """线程安全（主线程与 writer 线程都会更新）；每次 `save()` 整体原子重写。"""

    def __init__(self, persist_dir: Path, run_id: str, binding: dict[str, Any] | None = None) -> None:
        self.path = Path(persist_dir) / "meta.json"
        self.run_id = run_id
        self.lock = threading.RLock()
        self.data = self._load(binding)

    def _load(self, binding: dict[str, Any] | None) -> dict[str, Any]:
        with contextlib.suppress(OSError, ValueError):
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("schema") == "awr.run.meta.v1":
                d.setdefault("segments", [])
                return d
        b = binding or {"world_id": "", "content_version": "", "coordinate_sha256": "0" * 64, "layout_id": LAYOUT_ID,
                        "contracts_version": CONTRACTS_VERSION}
        return _default_meta(self.run_id, b)

    # ------------------------------------------------------------ 读取
    @property
    def binding(self) -> dict[str, Any]:
        return dict(self.data.get("binding") or {})

    def segments(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(s) for s in self.data.get("segments", [])]

    def next_segment_no(self) -> int:
        with self.lock:
            segs = self.data.get("segments", [])
            return (max(int(s["segment"]) for s in segs) + 1) if segs else 0

    def segment(self, k: int) -> dict[str, Any] | None:
        with self.lock:
            for s in self.data.get("segments", []):
                if int(s["segment"]) == k:
                    return s
        return None

    # ------------------------------------------------------------ 写入
    def set_binding(self, binding: dict[str, Any]) -> None:
        with self.lock:
            self.data["binding"] = {**self.data.get("binding", {}), **binding}

    def set_policy(self, **kw: Any) -> None:
        with self.lock:
            self.data.setdefault("recording_policy", {}).update(kw)

    def open_segment(self, k: int, *, epoch_start: int, t_start_ns: int, sim_segment: int, detail_coverage: str = "all") -> None:
        with self.lock:
            segs = [s for s in self.data.setdefault("segments", []) if int(s["segment"]) != k]
            segs.append({"segment": k, "file": seg_file(k), "state": "OPEN", "epoch_start": int(epoch_start),
                         "t_start_ns": int(t_start_ns), "t_end_ns": None, "bytes": 0, "bytes_per_sim_s": 0.0,
                         "decimation": {"active": False},
                         "lineage": [{"epoch": int(epoch_start), "t_start_ns": int(t_start_ns), "t_end_ns": None}]})
            segs.sort(key=lambda s: int(s["segment"]))
            self.data["segments"] = segs
            side = self.data.setdefault("sidecars", {})
            side[f"{k:03d}"] = {"ovw": True, "evx": True, "sim_segment": int(sim_segment), "events_per_sim_s": 0.0,
                                "speed_max": 20.0, "detail_coverage": detail_coverage}
            self.data.setdefault("detail_coverage", {})[f"{k:03d}"] = detail_coverage
            self.data["source"] = self.data.get("source") or "sim"
            self.data.setdefault("gaps", [])

    def update_segment(self, k: int, *, t_end_ns: int | None = None, nbytes: int | None = None, events: int | None = None,
                       decimation: dict[str, Any] | None = None, state: str | None = None) -> None:
        with self.lock:
            s = self.segment(k)
            if s is None:
                return
            if t_end_ns is not None:
                s["t_end_ns"] = int(t_end_ns)
            if nbytes is not None:
                s["bytes"] = int(nbytes)
            dur = ((s["t_end_ns"] or s["t_start_ns"]) - s["t_start_ns"]) / 1e9
            if dur > 0:
                s["bytes_per_sim_s"] = round(s["bytes"] / dur, 3)
            if decimation is not None:
                s["decimation"] = decimation
            if state is not None:
                s["state"] = state
                if state != "OPEN" and s["lineage"] and s["lineage"][-1].get("t_end_ns") is None:
                    s["lineage"][-1]["t_end_ns"] = s["t_end_ns"]
            side = self.data.setdefault("sidecars", {}).setdefault(f"{k:03d}", {})
            if events is not None and dur > 0:
                side["events_per_sim_s"] = round(events / dur, 6)
            bps = s.get("bytes_per_sim_s") or 0.0
            eps = side.get("events_per_sim_s") or 0.0
            sm = 20.0
            if bps > 0:
                sm = min(sm, 64 * 1024 * 1024 / bps)
            if eps > 0:
                sm = min(sm, 5000.0 / eps)
            side["speed_max"] = round(sm, 3)
            vals = [v.get("speed_max", 20.0) for v in self.data["sidecars"].values() if isinstance(v, dict)]
            self.data["speed_max"] = min(vals) if vals else 20.0
            tot_ev = sum(float(v.get("events_per_sim_s", 0.0)) for v in self.data["sidecars"].values() if isinstance(v, dict))
            self.data["events_per_sim_s"] = round(tot_ev, 6)

    def add_lineage(self, k: int, *, epoch: int, restored_t_ns: int, last_t_ns: int) -> None:
        with self.lock:
            s = self.segment(k)
            if s is None:
                return
            lin = s.setdefault("lineage", [])
            if lin and lin[-1].get("t_end_ns") is None:
                lin[-1]["t_end_ns"] = int(last_t_ns)
                lin[-1].setdefault("invalid", []).append([int(restored_t_ns), int(last_t_ns)])
            lin.append({"epoch": int(epoch), "t_start_ns": int(restored_t_ns), "t_end_ns": None})

    def add_gap(self, k: int, *, t_from_ns: int, t_to_ns: int, overrun: int, dropped: dict[str, int]) -> None:
        with self.lock:
            self.data.setdefault("gaps", []).append({"segment": k, "t_from_ns": int(t_from_ns), "t_to_ns": int(t_to_ns),
                                                     "overrun": int(overrun), "dropped": dict(dropped)})

    def set_marked(self, ids: list[str]) -> None:
        with self.lock:
            self.data.setdefault("recording_policy", {})["marked_ids"] = list(ids)

    def save(self) -> None:
        with self.lock:  # 主线程与 writer 线程都会保存：整个写入持锁，最后一次保存总是最新内容
            atomic_write_json(self.path, self.data)
