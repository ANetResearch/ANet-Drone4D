"""drone4d SDK 取帧（docs/05 §10.2 P0a-1；M26-FR-080；AWR-17 §18.8）：以 bench-api 进程内应用为传输的端到端读取。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("mcap_protobuf")
pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/sdk/drone4d/src"))

from drone4d.bench import BenchClient, BenchError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from awr.api.bench_main import create_app  # noqa: E402
from awr.bench.settings import BenchSettings  # noqa: E402
from awr.sensorsvc import capture as cap  # noqa: E402

sys.path.insert(0, str(ROOT / "tests/d3/sensorsvc"))
from test_capture import SPEC, _StubCamera  # noqa: E402


@pytest.fixture()
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import awr.sensorsvc.camera.raster as raster
    import awr.sensorsvc.camera.scene as scene

    monkeypatch.setattr(raster, "RasterCamera", _StubCamera)
    monkeypatch.setattr(scene, "load_appearance", lambda d: None)
    (tmp_path / "worlds/w1").mkdir(parents=True)
    (tmp_path / "worlds/w1/world.json").write_text(json.dumps({"contentVersion": "abc123def456"}))
    cap.run_capture(SPEC, tmp_path, log=lambda s: None)
    app = create_app(BenchSettings(data_root=tmp_path, secure_cookie=False))
    tok = app.state.auth.issue_token("alice", "researcher")[1]
    ev = app.state.auth.issue_token("eve", "evaluator")[1]
    return TestClient(app), tok, ev


def test_sdk_frame_retrieval(bench) -> None:
    http, tok, ev = bench
    bc = BenchClient(token=tok, client=http)
    run = bc.run("cap-test")
    assert {"uav_id": "uav-02", "sensor_id": "cam0", "latest_seq": 30} in run["frames"]
    items = list(bc.frame_index("cap-test", "uav-01", "cam0"))
    assert [i["seq"] for i in items] == list(range(1, 21))
    f = bc.frame("cap-test", "uav-01", "cam0", 5)
    assert f.t_ns == items[4]["t_ns"] and f.sha256 == items[4]["sha256"] and f.image().shape == (48, 64, 3)
    d = bc.frame("cap-test", "uav-01", "cam0.depth", 5)
    assert d.depth().shape == (48, 64) and float(d.depth().mean()) == pytest.approx(49.95, abs=0.15)     # realistic 档观测深度带噪
    seqs = [x.seq for x in bc.frames("cap-test", "uav-02", "cam0", 1, 30, stride=7)]
    assert seqs == [1, 8, 15, 22, 29]
    with pytest.raises(BenchError) as e:
        bc.truth_frame("cap-test", "uav-01", "cam0", 5)
    assert e.value.code == 115
    t = BenchClient(token=ev, client=http).truth_frame("cap-test", "uav-01", "cam0", 5)
    assert t.array("instance").shape == (48, 64) and (t.array("semantic") == 0).all()
    assert np.allclose(t.array("gt_depth"), 49.95, atol=1e-3) and not np.allclose(t.array("gt_depth"), d.depth())
    with pytest.raises(BenchError) as e:
        BenchClient(token="d3bt_" + "x" * 43, client=http).runs()
    assert e.value.status == 401 and e.value.code == 302
