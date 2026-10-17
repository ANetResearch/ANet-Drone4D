"""按机采集作业（docs/05 §10.2 P0a-1）：航迹、PoseHistory、触发网格、Run 产物与读回（GPU 以桩替代；真实渲染在 dmax 上验证）。"""

from __future__ import annotations

import json
import math
from pathlib import Path

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
         "cameras": [{"name": "cam0", "size": [64, 48], "hfov_deg": 84, "fps": "10", "gimbal": {"az_deg": 90, "el_deg": -30}}]},
        {"uav": "uav-02", "path": {"kind": "waypoints", "points": [[0, 0, 0, 20, 0], [2, 20, 0, 20, 90]]},
         "cameras": [{"name": "cam0", "size": [64, 48], "fps": "15", "outputs": ["rgb"]}]},
    ],
}


class _StubCamera:
    def __init__(self, scene) -> None:
        self.info = {"GL_RENDERER": "stub", "GL_VERSION": "4.5.0 stub 1.0"}
        self.n = 0

    def render(self, cm, R, pos) -> RenderOut:
        self.n += 1
        h, w = cm.height, cm.width
        rgb = np.zeros((h, w, 3), np.uint8)
        rgb[..., 1] = self.n % 256
        depth = np.full((h, w), float(pos[2]), np.float32)
        return RenderOut(rgb, depth, np.zeros((h, w), np.uint16), np.zeros((h, w), np.uint8), 1.0, 1.0, 1)

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
    assert m["frames"] == 20 + 30 and m["state"] == "SUCCEEDED"
    run = tmp_path / "dataset/capture/runs/cap-test"
    fs = FrameStore(run)
    assert fs.latest_seq("uav-01", "cam0") == 20 and fs.latest_seq("uav-02", "cam0") == 30
    f = fs.frame("uav-02", "cam0", 3)
    assert f.t_ns == 200_000_000 - 4_000_000 and f.payload[:2] == b"\xff\xd8"           # 15 Hz 第 3 帧 t_end = 200 ms
    d = fs.frame("uav-01", "cam0.depth", 5)
    assert np.frombuffer(d.payload, "<f4")[0] == pytest.approx(50.0 - 0.05, abs=1e-3)   # 离地 50 m（DSM 缺省 0）减挂载 −0.05 m
    assert fs.frame("uav-02", "cam0.depth", 1) is None                                  # 只输出 rgb
    ts = TruthStore(*truth_paths(run))
    rec, blocks = unpack_camera_gt(ts.get("uav-01", "cam0", 7)[1])
    assert rec["frame_id"] == "cap-test/uav-01/cam0/7" and rec["ground_truth"]["T_world_sensor"]["frame"] == "ENU<-RDF"
    assert not any(k.endswith("rgb.jpg") for k in blocks) and any(k.endswith("instance.u16.zst") for k in blocks)
    assert "gt_depth.f32.bshuf.zst" in blocks and "depth.f32" not in blocks                      # 真值深度保留，观测深度只在 raw.mcap
    assert json.loads((run / "run_manifest.json").read_text())["fleet"][1]["cameras"][0]["fps"] == "15"
    with pytest.raises(Exception, match="already exists"):
        cap.run_capture(SPEC, tmp_path, log=lambda s: None)
    fs.close()
    ts.close()
