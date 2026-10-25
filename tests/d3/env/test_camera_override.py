"""L1 相机覆盖（ADR-217 决策 (1)，D3-AC-18 r6 对照）：run_spec `fleet[].sensors[eo].overrides.hfov_deg` → `EnvSpec.camera`、
Env 的 L1 相机、运行清单 `camera` 与评测器 AO oracle 的相机；排名运行只允许 r6 参考行使用覆盖。"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import pytest

from awr.agentsdk.env.camera import CameraModel
from awr.agentsdk.env.spec import REFERENCE_ROWS, EnvSpec, camera_override, config_sha256

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
BASE_SPEC = ROOT / "configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json"
FX40 = 960.0 / math.tan(math.radians(20.0))                   # 1920 px、HFOV 40°：r6 参考相机 fx ≈ 2637.6


def _with_cam(rs: dict, hfovs, **kw) -> dict:
    out = copy.deepcopy(rs)
    for f, h in zip(out["fleet"], hfovs, strict=True):
        if h is not None:
            f["sensors"] = [{"sensor_id": "eo", "overrides": {"hfov_deg": h}}]
    return {**out, **kw}


# ------------------------------------------------------------------ 规格与相机模型（无仿真依赖）
def test_camera_model_with_overrides() -> None:
    c = CameraModel()
    assert c.with_overrides({}) is c and c.with_overrides(None) is c and c.with_overrides({"hfov_deg": 60.0}) is c
    c40 = c.with_overrides({"hfov_deg": 40.0})
    assert c40.hfov_deg == 40.0 and c40.model_ref == "d4d_eo1080@1+hfov40" and c40.fx == pytest.approx(FX40, abs=0.05)
    assert (c40.width, c40.height, c40.pitch_range_deg, c40.mount_t_bs_m) == (c.width, c.height, c.pitch_range_deg, c.mount_t_bs_m)
    assert c40.with_overrides({"hfov_deg": 50.0}).model_ref == "d4d_eo1080@1+hfov50"       # 型号以标称型号为基
    assert c40.config()["K"][0] == c40.fx and c40.config()["model_ref"] == c40.model_ref
    assert c40.manifest() == {"model_ref": "d4d_eo1080@1+hfov40", "width": 1920, "height": 1080, "hfov_deg": 40.0, "fx": c40.fx}
    with pytest.raises(ValueError, match="outside"):
        c.with_overrides({"hfov_deg": 5.0})
    with pytest.raises(ValueError, match="not supported"):
        c.with_overrides({"hfov_deg": 40.0, "width_px": 1280})


def test_camera_override_rules() -> None:
    base = json.loads(BASE_SPEC.read_text(encoding="utf-8"))           # 排名、policy = track_share（基础基线）
    assert camera_override(base) == {}
    with pytest.raises(ValueError, match="unranked runs and r6 reference rows"):
        camera_override(_with_cam(base, [40.0] * 3))                    # 排名的基础基线：拒绝
    with pytest.raises(ValueError, match="unranked runs and r6 reference rows"):
        camera_override(_with_cam(base, [40.0] * 3, policy={"kind": "image", "image_digest": "sha256:" + "0" * 64}))
    assert camera_override(_with_cam(base, [40.0] * 3, ranked=False)) == {"hfov_deg": 40.0}      # 不排名运行：允许
    for row in sorted(REFERENCE_ROWS):                                  # 排名的 r6 参考行：允许
        assert camera_override(_with_cam(base, [40.0] * 3, policy={"kind": "reference", "name": row})) == {"hfov_deg": 40.0}
    with pytest.raises(ValueError, match="same for every UAV"):
        camera_override(_with_cam(base, [40.0, 40.0, None], ranked=False))
    with pytest.raises(ValueError, match="same for every UAV"):
        camera_override(_with_cam(base, [40.0, 40.0, 50.0], ranked=False))
    with pytest.raises(ValueError, match="outside"):
        camera_override(_with_cam(base, [200.0] * 3, ranked=False))
    other = copy.deepcopy(base)                                         # 其他传感器项与 eo 的其他覆盖键不构成相机覆盖
    for f in other["fleet"]:
        f["sensors"] = [{"sensor_id": "lidar0", "enabled": False}, {"sensor_id": "eo", "overrides": {"role": "agent"}}]
    assert camera_override(other) == {}


def test_reference_rows_match_sdk_registry() -> None:
    from anet4d.baselines import REFERENCE6

    assert set(REFERENCE6) == REFERENCE_ROWS


def test_from_run_spec_and_identity(tmp_path: Path) -> None:
    base = json.loads(BASE_SPEC.read_text(encoding="utf-8"))
    row = _with_cam(base, [40.0] * 3, policy={"kind": "reference", "name": "lawn3_cover"})
    kw = {"run_id": "r", "seed": 1, "worlds_root": tmp_path, "events_root": tmp_path}
    s40 = EnvSpec.from_run_spec(row, **kw)
    s60 = EnvSpec.from_run_spec({**row, "fleet": base["fleet"]}, **kw)
    assert s40.camera == {"hfov_deg": 40.0} and s40.camera_model().model_ref == "d4d_eo1080@1+hfov40"
    assert s60.camera == {} and s60.camera_model() == CameraModel()
    assert config_sha256(s40.resolved()) != config_sha256(s60.resolved())          # 覆盖进入配置哈希与运行哈希
    with pytest.raises(ValueError):
        EnvSpec.from_run_spec(_with_cam(base, [40.0] * 3), **kw)
    syn = EnvSpec("r", 1, tmp_path / "w", tmp_path / "e" / "v1", s40.fleet, camera={"hfov_deg": 40.0})   # 无规范规格时由字段合成
    assert all(f["sensors"] == [{"sensor_id": "eo", "overrides": {"hfov_deg": 40.0}}] for f in syn.resolved()["fleet"])
    assert "sensors" not in EnvSpec("r", 1, tmp_path / "w", tmp_path / "e" / "v1", s40.fleet).resolved()["fleet"][0]


def test_run_spec_contract_accepts_override() -> None:
    pytest.importorskip("jsonschema")
    from awr.agentsdk.schemas import bench_validator

    base = json.loads(BASE_SPEC.read_text(encoding="utf-8"))
    v = bench_validator("run_spec")
    assert not list(v.iter_errors(_with_cam(base, [40.0] * 3, policy={"kind": "reference", "name": "lawn3_cover"})))
    assert list(v.iter_errors(_with_cam(base, [5.0] * 3)))                           # 契约范围 [10, 120]


# ------------------------------------------------------------------ Env 与评测（合成世界、运动学开发档）
def test_env_and_evaluator_use_override(synth_event, tmp_path: Path) -> None:
    from anet4d.baselines import BASELINES
    from test_env import _run, _spec

    from awr.agentsdk.env.core import _open_world
    from awr.bench.eval.evaluator import evaluate_run, run_camera
    from awr.bench.eval.run import load_run

    w, ev = synth_event
    m60 = _run(_spec(synth_event, tmp_path / "n", dur=60.0), lambda u: BASELINES["static_guard"]())
    m40 = _run(_spec(synth_event, tmp_path / "o", dur=60.0, camera={"hfov_deg": 40.0}), lambda u: BASELINES["static_guard"]())
    assert "camera" not in m60                                                       # 标称相机不写 camera（既有运行清单不变）
    assert m40["camera"]["model_ref"] == "d4d_eo1080@1+hfov40" and m40["camera"]["fx"] == pytest.approx(FX40, abs=0.05)
    cfg = json.loads((tmp_path / "o/agents/uav-01/config.json").read_text())
    cam = next(s for s in cfg["sensors"] if s["kind"] == "camera")
    assert cam["model_ref"] == "d4d_eo1080@1+hfov40" and cam["K"][0] == pytest.approx(FX40, abs=0.05)
    assert run_camera(m60) == (CameraModel().fx, "d4d_eo1080@1") and run_camera(m40) == (m40["camera"]["fx"], "d4d_eo1080@1+hfov40")
    world = _open_world(w)
    d60 = evaluate_run(tmp_path / "n", ev, world)
    d40 = evaluate_run(tmp_path / "o", ev, world)
    assert d60["ao"]["oracle"]["camera_models"] == ["d4d_eo1080@1"]
    assert d40["ao"]["oracle"]["camera_models"] == ["d4d_eo1080@1+hfov40"]
    assert evaluate_run(tmp_path / "o", ev, world) == d40                            # 同一输入结果一致
    # 同一种子、同一航迹（静态守位的航点与云台不依赖检测）：同一（机、帧、演员）的无遮挡像素高度按 fx 之比放大
    def h_unocc(d: Path) -> dict:
        return {(r["uav"], r["seq"], r["actor"]): float(r["h_unocc_px"]) for r in load_run(d, ev).l1 if r.get("actor")}

    a, b = h_unocc(tmp_path / "n"), h_unocc(tmp_path / "o")
    both = [k for k in a if k in b and a[k] > 5.0]
    assert len(both) >= 10
    ratios = sorted(b[k] / a[k] for k in both)
    assert ratios[len(ratios) // 2] == pytest.approx(FX40 / CameraModel().fx, rel=0.02)
