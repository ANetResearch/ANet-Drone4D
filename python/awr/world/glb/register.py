"""配准记录生成（M22 §6.6.2、§6.6.3、§7.6.2；FR-006 至 FR-016）。

本版提供两种方法：`manual`（以已复核的配准量直接生成记录，用于从 r2 实测结果起步与人工修订）与 `inherit-frame`（共帧继承参照世界
已接受的记录）。`osm-road-correlation` 与 `osm-footprint-correlation` 的搜索（§6.6.2）在后续工作包中实现，届时以同一记录格式
输出。记录的 `result` 由构建直接使用；`status` 只有经 `--accept` 才为 `accepted`。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from awr.world.glb.normalize import frame_of_relative, synthesize
from awr.world.glb.reader import Scene, load_scene
from awr.world.glb.recipe import REG_SCHEMA, Recipe, file_sha256, load_recipe, load_registration, verify_raw
from awr.world.ingest.types import ConfigError

__all__ = ["register_frame_of", "register_manual", "write_registration"]


def _record(rec: Recipe, sc: Scene, method: str, params: dict[str, Any], wf, *, frame_of: dict | None, h_msl_m: float | None,
            scale_status: str, evidence: list[str]) -> dict[str, Any]:
    src = rec.doc["source"]["glb"]
    res = {"m_per_unit": wf.m_per_unit, "yaw_deg": params["yaw_deg"], "t_osm_m": list(params["t_osm_m"]),
           "osm_ref": {"lat_deg": params["osm_ref"][0], "lon_deg": params["osm_ref"][1]},
           "leveling": {"primary": params.get("level_primary", "dem"), "applied_g": list(params["g"]), "tilt_deg": round(wf.leveled_deg, 4),
                        "needs_control_points": False},
           **wf.to_json()}
    res["anchor"]["h_msl_m"] = h_msl_m
    out = {"schema": REG_SCHEMA, "world_id": rec.world_id, "status": "draft",
           "inputs": {"glb": {"name": Path(src["path"]).name, "sha256": src["sha256"]}},
           "method": method, "result": res, "metrics": params.get("metrics", {}), "scale_status": scale_status, "evidence": evidence[:8]}
    if frame_of:
        out["frame_of"] = frame_of
    return out


def register_manual(rec: Recipe, params: dict[str, Any], *, h_msl_m: float | None = None, evidence: list[str] | None = None) -> dict:
    """params：`s`（m/单位）、`yaw_deg`、`g`（gE, gN）、`t_osm_m`（tE, tN）、`osm_ref`（lat, lon）。"""
    sc = load_scene(verify_raw(rec.doc["source"]["glb"]["path"], rec.doc["source"]["glb"]["sha256"]))
    V, _, _ = sc.merged()
    wf = synthesize(V, s=params["s"], yaw_deg=params["yaw_deg"], g=tuple(params["g"]), t_osm_m=tuple(params["t_osm_m"]),
                    osm_ref=tuple(params["osm_ref"]))
    return _record(rec, sc, "manual", params, wf, frame_of=None, h_msl_m=h_msl_m, scale_status="landmark", evidence=evidence or [])


def register_frame_of(rec: Recipe, ref_id: str, *, h_msl_m: float | None = None, evidence: list[str] | None = None) -> dict:
    """共帧继承：复合参照世界已接受记录的 s、yaw、g、t_osm 与两份 GLB 根节点矩阵的相对相似变换。"""
    ref = load_recipe(ref_id)
    ref_reg = load_registration(ref.registration_path)
    r = ref_reg["result"]
    sc = load_scene(verify_raw(rec.doc["source"]["glb"]["path"], rec.doc["source"]["glb"]["sha256"]))
    ref_sc = load_scene(verify_raw(ref.doc["source"]["glb"]["path"], ref.doc["source"]["glb"]["sha256"]), geometry=False)
    rel = frame_of_relative(sc, ref_sc)
    params = {"s": r["m_per_unit"], "yaw_deg": r["yaw_deg"], "g": tuple(r["leveling"]["applied_g"]), "t_osm_m": tuple(r["t_osm_m"]),
              "osm_ref": (r["osm_ref"]["lat_deg"], r["osm_ref"]["lon_deg"]), "level_primary": "inherited"}
    V, _, _ = sc.merged()
    wf = synthesize(V, s=params["s"], yaw_deg=params["yaw_deg"], g=params["g"], t_osm_m=params["t_osm_m"], osm_ref=params["osm_ref"], rel=rel)
    params["yaw_deg"] = r["yaw_deg"]
    fo = {"world_id": ref_id, "registration_sha256": file_sha256(ref.registration_path),
          "S_rel": [[round(v, 12) for v in row] for row in rel]}
    return _record(rec, sc, "inherit-frame", params, wf, frame_of=fo, h_msl_m=h_msl_m, scale_status="landmark", evidence=evidence or [])


def write_registration(rec: Recipe, doc: dict, *, accept_by: str | None = None) -> Path:
    if accept_by:
        doc["status"] = "accepted"
        doc["accepted_by"] = accept_by
        doc["accepted_at"] = datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    p = rec.registration_path
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def parse_pair(s: str) -> tuple[float, float]:
    try:
        a, b = (float(v) for v in s.split(","))
    except ValueError as e:
        raise ConfigError(f"expected two comma-separated numbers, got {s!r}") from e
    return a, b
