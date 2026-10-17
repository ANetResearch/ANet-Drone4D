"""按机采集作业（docs/05 §10.2 P0a-1）：航迹、PoseHistory、触发网格、Run 产物与读回（GPU 以桩替代；真实渲染在 dmax 上验证）。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

pytest.importorskip("PIL")
pytest.importorskip("mcap_protobuf")

from awr.sensorsvc import capture as cap
from awr.sensorsvc.camera.raster import RenderOut

SPEC = {
    "run_id": "cap-test", "event_id": "capture", "world_id": "w1", "duration_s": 2.0,
    "fleet": [
        {"uav": "uav-01", "path": {"kind": "orbit", "center": [0, 0], "radius_m": 100, "agl_m": 50, "period_s": 60},
         "cameras": [{"name": "cam0", "size": [64, 48], "hfov_deg": 84, "fps": "10", "gimbal": {"az_deg": 90, "el_deg": -30}},
                     {"name": "cam1", "profile": "rgb1080-rs", "size": [64, 48], "outputs": ["rgb"]}]},
        {"uav": "uav-02", "path": {"kind": "waypoints", "points": [[0, 0, 0, 20, 0], [2, 20, 0, 20, 90]]},
         "cameras": [{"name": "cam0", "size": [64, 48], "fps": "15", "outputs": ["rgb"]}]},
    ],
}


class _StubCamera:
    rs_calls: ClassVar[list[int]] = []
    actor_calls: ClassVar[list] = []

    def __init__(self, scene) -> None:
        self.info = {"GL_RENDERER": "stub", "GL_VERSION": "4.5.0 stub 1.0"}
        self.n = 0
        self.actors: list[dict] = []

    def set_actors(self, actors: list[dict]) -> None:
        self.actors = actors

    def set_sun(self, az_el) -> None:
        pass

    def render(self, cm, R, pos, *, linear: bool = False, fx=None, rs_nodes=None, actors=None) -> RenderOut:
        """GPU 效果链的替身：线性常量图经参考实现得到交付 RGB 与观测深度。"""
        from awr.sensorsvc.camera.effects import apply_fx_reference

        self.n += 1
        if rs_nodes is not None:
            _StubCamera.rs_calls.append(len(rs_nodes))
        h, w = cm.height, cm.width
        lin = np.full((h, w, 3), 0.02 * (self.n % 10 + 1), np.float32)
        depth = np.full((h, w), float(pos[2]), np.float32)
        rgb, d_obs = apply_fx_reference(lin, depth, cm, fx)
        inst = np.zeros((h, w), np.uint16)
        unocc = None
        if actors is not None:
            _StubCamera.actor_calls.append(actors)
            unocc = np.zeros((h, w), np.uint16)
            for k, i in enumerate(actors.idx):                                   # 每名可见演员占一个 2 × 2 块
                unocc[2 * k:2 * k + 2, 0:2] = i + 1
            inst = unocc.copy()
        return RenderOut(rgb, depth, inst, np.zeros((h, w), np.uint8), 1.0, 1.0, 1,
                         depth_obs=d_obs, meter_sub=lin[::8, ::8] if fx.meter else None, instance_unocc=unocc)

    def release(self) -> None:
        pass


def test_paths_and_history() -> None:
    gz = lambda x, y: 10.0  # noqa: E731
    p, yaw = cap.path_state(SPEC["fleet"][0]["path"], 15.0, gz)                 # 四分之一圈
    assert np.allclose(p, [0, 100, 60], atol=1e-9) and math.degrees(yaw) == pytest.approx(180.0)
    p, yaw = cap.path_state(SPEC["fleet"][1]["path"], 1.0, gz)
    assert np.allclose(p, [10, 0, 30]) and math.degrees(yaw) == pytest.approx(45.0)
    _, fleet = cap.load_spec(SPEC)
    H = cap.build_history(fleet[0], 2.0, gz)
    assert len(H) == 501 and H.t[1] - H.t[0] == 4_000_000
    assert H.gimbal_at(0, 0)[1] == pytest.approx(math.radians(-30))


def test_capture_run_outputs_and_readback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import awr.sensorsvc.camera.raster as raster
    import awr.sensorsvc.camera.scene as scene
    from awr.bench.recorder.read import FrameStore, TruthStore
    from awr.bench.recorder.truth import unpack_camera_gt
    from awr.bench.runs_store import truth_paths

    monkeypatch.setattr(raster, "RasterCamera", _StubCamera)
    monkeypatch.setattr(scene, "load_appearance", lambda d: None)
    w = tmp_path / "worlds" / "w1"
    w.mkdir(parents=True)
    (w / "world.json").write_text(json.dumps({"contentVersion": "abc123def456"}))
    m = cap.run_capture(SPEC, tmp_path, log=lambda s: None)
    assert m["frames"] == 20 + 20 + 30 and m["state"] == "SUCCEEDED"
    assert _StubCamera.rs_calls == [9] * 20                                              # 只有 rgb1080-rs 相机走卷帘
    run = tmp_path / "dataset/capture/runs/cap-test"
    fs = FrameStore(run)
    assert fs.latest_seq("uav-01", "cam0") == 20 and fs.latest_seq("uav-02", "cam0") == 30
    f = fs.frame("uav-02", "cam0", 3)
    assert f.t_ns == 200_000_000 - 4_000_000 and f.payload[:2] == b"\xff\xd8"           # 15 Hz 第 3 帧 t_end = 200 ms
    d = np.frombuffer(fs.frame("uav-01", "cam0.depth", 5).payload, "<f4")
    # realistic 档：离地 50 m（DSM 缺省 0）减挂载 −0.05 m，sigma(50 m) = 0.25 + 1.25 = 1.5 m；3072 像素的均值误差 < 0.15 m
    assert d.mean() == pytest.approx(50.0 - 0.05, abs=0.15) and d.std() == pytest.approx(1.5, rel=0.1)
    assert fs.frame("uav-02", "cam0.depth", 1) is None                                  # 只输出 rgb
    ts = TruthStore(*truth_paths(run))
    rec, blocks = unpack_camera_gt(ts.get("uav-01", "cam0", 7)[1])
    assert rec["frame_id"] == "cap-test/uav-01/cam0/7" and rec["ground_truth"]["T_world_sensor"]["frame"] == "ENU<-RDF"
    assert not any(k.endswith("rgb.jpg") for k in blocks) and any(k.endswith("instance.u16.zst") for k in blocks)
    assert "gt_depth.f32.bshuf.zst" in blocks and "depth.f32" not in blocks                      # 真值深度保留，观测深度只在 raw.mcap
    from awr.bench.recorder.depthcodec import bshuf4_unzstd

    gd = rec["ground_truth"]["gt_depth"]
    assert gd["codec"] == "bshuf4-zstd" and gd["ref"] in blocks
    assert np.allclose(np.frombuffer(bshuf4_unzstd(blocks[gd["ref"]]), "<f4"), 50.0 - 0.05, atol=1e-3)     # 真值深度不加噪
    assert rec["internal"]["noise_tier"] == "realistic" and rec["internal"]["fx_version"] == "fx-basic@1"
    gr, gtr = rec["agent_view"]["gimbal_reading"], rec["ground_truth"]["gimbal_true"]
    assert gr != gtr
    assert json.loads((run / "run_manifest.json").read_text())["fleet"][1]["cameras"][0]["fps"] == "15"
    assert fs.frame("uav-01", "cam1", 1).t_ns == 100_000_000 - (1_000_000 + 20_000_000) // 2              # t_mid = t_end − ⌊(e + T_ro)/2⌋
    rec1, _ = unpack_camera_gt(ts.get("uav-01", "cam1", 1)[1])
    tm1 = rec1["ground_truth"]["timing"]
    assert rec1["agent_view"]["readout_ns"] == 20_000_000 and tm1["rs_nodes"] == 9
    assert tm1["readout_window_ns"] == [89_500_000 - 10_000_000, 89_500_000 + 10_000_000]
    assert "rs_nodes" not in rec["ground_truth"]["timing"]                                # 全局快门不写卷帘字段
    with pytest.raises(Exception, match="already exists"):
        cap.run_capture(SPEC, tmp_path, log=lambda s: None)
    fs.close()
    ts.close()


def _tape(d: Path, content_version: str) -> None:
    """1 名行人：t = 0.5 s 出现于 (5, 0)，沿 +x 以 1 m/s 行走。"""
    from awr.sim.events import tape as tp

    n_t = 50
    f = np.zeros((n_t, 1), tp.RECORD)
    f["container"] = tp.NO_CONTAINER
    f["state"][5:, 0] = 1
    f["x_mm"][:, 0] = 5000 + np.maximum(np.arange(n_t) - 5, 0) * 100
    f["vx_cmps"][5:, 0] = 100
    h64 = "0" * 64
    hd = {"schema": "drone4d.tape.v1", "event_id": "ev-test", "event_version": 1, "event_sha256": h64, "mode": "exogenous", "seed": 0,
          "tick_hz": 10, "n_ticks": n_t, "duration_ms": n_t * 100, "record": tp.record_spec(),
          "codec": {"name": "col-delta-zstd@1", "block_ticks": 100},
          "actors": [{"idx": 0, "id": "person_A", "class": "person", "scripted": True, "profile": "person", "size_mm": [500, 300, 1750],
                      "appearance": {"asset": "person-proxy@1"}}],
          "world": {"id": "w1", "content_version": content_version, "raycast_sha256": h64, "navmesh_sha256": {"person": h64},
                    "anchors_sha256": h64}, "environment": {}, "env_track": [], "comms": {}, "anim": None, "realized": None}
    h = tp.write_tape(d / "tape.bin", hd, f, n_scripted=1)
    (d / "tape.json").write_text(json.dumps({"content_sha256": h["content_sha256"]}))


def test_capture_with_event_tape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("zstandard")
    import awr.sensorsvc.camera.raster as raster
    import awr.sensorsvc.camera.scene as scene
    from awr.bench.recorder.read import TruthStore
    from awr.bench.recorder.truth import unpack_camera_gt
    from awr.bench.runs_store import truth_paths
    from awr.sim.events.errors import EventError

    monkeypatch.setattr(raster, "RasterCamera", _StubCamera)
    monkeypatch.setattr(scene, "load_appearance", lambda d: None)
    w = tmp_path / "worlds" / "w1"
    w.mkdir(parents=True)
    (w / "world.json").write_text(json.dumps({"contentVersion": "abc123def456"}))
    ev = tmp_path / "events" / "ev-test" / "v1"
    ev.mkdir(parents=True)
    _tape(ev, "abc123def456")
    _StubCamera.actor_calls.clear()
    spec = {"run_id": "cap-ev", "event_id": "ev-test", "world_id": "w1", "duration_s": 1.0, "event": {"path": "events/ev-test/v1", "offset_s": 1.0},
            "fleet": [{"uav": "uav-01", "path": {"kind": "orbit", "center": [0, 0], "radius_m": 50, "agl_m": 40, "period_s": 60},
                       "cameras": [{"name": "cam0", "size": [64, 48], "fps": "10", "outputs": ["rgb"]}]}]}
    m = cap.run_capture(spec, tmp_path, log=lambda s: None)
    assert m["event"]["event_id"] == "ev-test" and m["event"]["offset_s"] == 1.0 and m["frames"] == 10
    first = _StubCamera.actor_calls[0]                                            # t_mid ≈ 0.1 s → 事件 1.1 s：x = 5 + 0.6 m
    assert first.idx.tolist() == [0] and first.pos[0, 0] == pytest.approx(5.6, abs=0.01)
    ts = TruthStore(*truth_paths(tmp_path / "dataset/ev-test/runs/cap-ev"))
    rec, _ = unpack_camera_gt(ts.get("uav-01", "cam0", 3)[1])
    ts.close()
    (o,) = rec["ground_truth"]["objects"]
    assert o["actor"] == "person_A" and o["px_unoccluded"] == 4 and rec["internal"]["dynamic_source_id"] == m["event"]["content_sha256"]
    spec2 = dict(spec, run_id="cap-ev2")
    (w / "world.json").write_text(json.dumps({"contentVersion": "000000000000"}))
    with pytest.raises(EventError) as e:
        cap.run_capture(spec2, tmp_path, log=lambda s: None)
    assert e.value.code == 615 and not (tmp_path / "dataset/ev-test/runs/cap-ev2").exists()
