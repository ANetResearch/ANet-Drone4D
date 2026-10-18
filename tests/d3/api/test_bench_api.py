"""bench-api 骨架与帧接口（AWR-17 §18.3、§18.4、§18.8；API-FR-094、114；API-AC-070、083、084 的本机部分）。"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")

from fastapi.testclient import TestClient

from awr.api.bench_main import create_app
from awr.bench.recorder.channels import parse_truth_topic
from awr.bench.recorder.frames_idx import build_frames_idx
from awr.bench.recorder.truth import unpack_camera_gt
from awr.bench.recorder.writer import RunWriter
from awr.bench.runs_store import truth_paths
from awr.bench.settings import BenchSettings

P = "/api/bench/v1"
ORIGIN = "https://bench.example"


def _jpeg(i: int) -> bytes:
    return b"\xff\xd8" + np.random.default_rng(i).bytes(3000) + b"\xff\xd9"


@pytest.fixture()
def env(tmp_path: Path):
    s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=(ORIGIN,), secure_cookie=False)
    run = s.dataset_dir / "capture" / "runs" / "cap-0001"
    with RunWriter(run / "raw.mcap") as w:
        for seq in range(1, 6):
            w.write_camera("uav-01", "cam0", seq, seq * 100_000_000, _jpeg(seq), depth_m=np.full((2, 3), seq, np.float32))
    build_frames_idx(run / "raw.mcap", run / "frames.idx")
    tm, ti = truth_paths(run)
    with RunWriter(tm) as w:
        for seq in range(1, 6):
            rec = {"schema": "drone4d.frame.v1", "frame_id": f"cap-0001/uav-01/cam0/{seq}", "ground_truth": {"objects": []}}
            w.write_camera_truth("uav-01", "cam0", seq, seq * 100_000_000, rec, {"inst": np.full(6, seq, "<u2").tobytes()})
    build_frames_idx(tm, ti, parse=parse_truth_topic)
    (run / "run_manifest.json").write_text(json.dumps({"state": "SUCCEEDED", "kind": "capture", "world_id": "oryahovo-crop1"}))
    app = create_app(s)
    c = TestClient(app)
    _tid, tok = app.state.auth.issue_token("alice", "researcher", ("d3",))
    _tid2, tok_inv = app.state.auth.issue_token("bob", "invited")
    app.state.tok_eval = app.state.auth.issue_token("eve", "evaluator")[1]
    return c, app, tok, tok_inv


def test_auth_required_and_problem_json(env) -> None:
    c, _app, _tok, _ = env
    r = c.get(f"{P}/meta")
    assert r.status_code == 401 and r.json()["code"] == 301 and r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["AWR-Bench-API"] == "1"
    r = c.get(f"{P}/meta", headers={"Authorization": "Bearer d3bt_nope"})
    assert r.status_code == 401 and r.json()["code"] == 302
    assert c.get("/api/v1/worlds").status_code == 404                          # bench-api 只应答 /api/bench/v1/**


def test_session_cookie_csrf_and_short_token(env) -> None:
    c, app, tok, tok_inv = env
    assert c.post(f"{P}/session", json={"token": "d3bt_wrong-wrong"}).json()["code"] == 302
    r = c.post(f"{P}/session", json={"token": tok})
    assert r.status_code == 204 and "d3_session" in r.cookies
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert c.get(f"{P}/session").json() == {"principal": "alice", "role": "researcher", "projects": ["d3"], "via": "session"}
    r = c.post(f"{P}/session/short-token", json={"aud": "frames"})
    assert r.status_code == 403 and r.json()["code"] == 303                     # Cookie 写请求缺 Origin 与 X-D3-CSRF
    h = {"Origin": ORIGIN, "X-D3-CSRF": "1"}
    r = c.post(f"{P}/session/short-token", json={"aud": "frames"}, headers=h)
    assert r.status_code == 200
    st = r.json()["token"]
    assert st.startswith("d3st_v1.") and all(ch.isalnum() or ch in "-_." for ch in st)
    bare = TestClient(app)
    assert bare.get(f"{P}/runs/cap-0001/frames/uav-01/cam0/1", headers={"Authorization": f"Bearer {st}"}).status_code == 200
    assert bare.get(f"{P}/meta", headers={"Authorization": f"Bearer {st}"}).json()["code"] == 302    # 短期令牌只用于帧接口
    r = bare.get(f"{P}/meta", headers={"Authorization": f"Bearer {tok_inv}"})
    assert r.status_code == 403 and r.json()["code"] == 115
    assert c.delete(f"{P}/session", headers=h).status_code == 204
    c.cookies.clear()
    assert c.get(f"{P}/session").status_code == 401


def test_frame_endpoints(env) -> None:
    c, _app, tok, _ = env
    h = {"Authorization": f"Bearer {tok}"}
    runs = c.get(f"{P}/runs", headers=h).json()["items"]
    assert runs[0]["run_id"] == "cap-0001" and runs[0]["has_frames"]
    st = c.get(f"{P}/runs/cap-0001", headers=h).json()
    assert {"uav_id": "uav-01", "sensor_id": "cam0", "latest_seq": 5} in st["frames"]
    idx = c.get(f"{P}/runs/cap-0001/frames/uav-01/cam0?seq_from=2&seq_to=3", headers=h).json()
    assert [i["seq"] for i in idx["items"]] == [2, 3] and idx["items"][0]["sha256"] == hashlib.sha256(_jpeg(2)).hexdigest()
    r = c.get(f"{P}/runs/cap-0001/frames/uav-01/cam0/4", headers=h)
    assert r.status_code == 200 and r.content == _jpeg(4) and r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-frame-t-ns"] == "400000000" and r.headers["etag"] == f'"{hashlib.sha256(_jpeg(4)).hexdigest()}"'
    assert "immutable" in r.headers["cache-control"]
    assert c.get(f"{P}/runs/cap-0001/frames/uav-01/cam0/4", headers={**h, "If-None-Match": r.headers["etag"]}).status_code == 304
    r = c.get(f"{P}/runs/cap-0001/frames/uav-01/cam0/4", headers={**h, "Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == _jpeg(4)[:10] and r.headers["content-range"] == f"bytes 0-9/{len(_jpeg(4))}"
    assert c.get(f"{P}/runs/cap-0001/frames/uav-01/cam0/4", headers={**h, "Range": "bytes=99999-"}).json()["code"] == 309
    d = c.get(f"{P}/runs/cap-0001/frames/uav-01/cam0.depth/3", headers=h)
    assert d.headers["x-frame-size"] == "3x2" and np.frombuffer(d.content, "<f4")[0] == 3.0
    r = c.get(f"{P}/runs/cap-0001/frames/uav-01/cam0:range?seq_from=1&seq_to=5&stride=2", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/x-d3frames" and r.content[:4] == b"D3FR"
    (hl,) = struct.unpack_from("<I", r.content, 8)
    assert json.loads(r.content[12:12 + hl])["n"] == 3
    r = c.get(f"{P}/runs/cap-0001/frames/uav-01/cam0:range?seq_from=0&seq_to=400", headers=h)
    assert r.status_code == 422 and r.json()["code"] == 698
    for url in (f"{P}/runs/nope/frames/uav-01/cam0/1", f"{P}/runs/cap-0001/frames/uav-09/cam0/1", f"{P}/runs/cap-0001/frames/uav-01/cam0/99",
                f"{P}/runs/..%2F..%2Fkeys/frames/uav-01/cam0/1"):
        assert c.get(url, headers=h).json()["code"] == 305


def test_revoked_token_kills_sessions_and_short_tokens(env) -> None:
    c, app, tok, _ = env
    c.post(f"{P}/session", json={"token": tok})
    st = c.post(f"{P}/session/short-token", json={"aud": "frames"}, headers={"Origin": ORIGIN, "X-D3-CSRF": "1"}).json()["token"]
    tid = app.state.auth.list_tokens()[0]["id"]
    assert app.state.auth.revoke_token(tid)
    assert c.get(f"{P}/session").json()["code"] == 302
    assert TestClient(app).get(f"{P}/runs/cap-0001/frames/uav-01/cam0/1", headers={"Authorization": f"Bearer {st}"}).json()["code"] == 302
    keys = app.state.settings.keys_dir
    assert (keys / "bench_tokens.json").stat().st_mode & 0o777 == 0o600 and tok not in (keys / "bench_tokens.json").read_text()


def test_truth_frames_evaluator_only(env) -> None:
    c, app, tok, _ = env
    url = f"{P}/runs/cap-0001/truth/frames/uav-01/cam0/3"
    r = c.get(url, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403 and r.json()["code"] == 115
    r = c.get(url, headers={"Authorization": f"Bearer {app.state.tok_eval}"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/x-d3gt" and r.headers["x-frame-t-ns"] == "300000000"
    rec, blocks = unpack_camera_gt(r.content)
    assert rec["frame_id"] == "cap-0001/uav-01/cam0/3" and np.frombuffer(blocks["inst"], "<u2")[0] == 3
    assert c.get(f"{P}/runs/cap-0001/truth/frames/uav-01/cam0/9", headers={"Authorization": f"Bearer {app.state.tok_eval}"}).json()["code"] == 305


def test_event_tape_evaluator_only(env) -> None:
    pytest.importorskip("zstandard")
    from awr.sim.events import tape as tp

    c, app, tok, _ = env
    run = app.state.settings.dataset_dir / "capture" / "runs" / "cap-0001"
    url = f"{P}/runs/cap-0001/truth/event/tape"
    ev = {"Authorization": f"Bearer {app.state.tok_eval}"}
    assert c.get(url, headers=ev).json()["code"] == 305                                   # 运行没有事件
    tdir = app.state.settings.data_root / "events" / "ev-x" / "v3"
    f = np.zeros((10, 1), tp.RECORD)
    hd = {"schema": "drone4d.tape.v1", "event_id": "ev-x", "event_version": 3, "n_ticks": 10, "duration_ms": 1000,
          "actors": [{"idx": 0, "id": "p", "class": "person", "scripted": True, "profile": "person", "size_mm": [1, 1, 1], "appearance": {}}]}
    h = tp.write_tape(tdir / "tape.bin", hd, f, n_scripted=1)
    (tdir / "tape.json").write_text(json.dumps({"content_sha256": h["content_sha256"]}))
    m = json.loads((run / "run_manifest.json").read_text())
    m["event"] = {"event_id": "ev-x", "version": 3, "content_sha256": h["content_sha256"], "offset_s": 12.5}
    (run / "run_manifest.json").write_text(json.dumps(m))
    assert c.get(f"{P}/runs/cap-0001", headers={"Authorization": f"Bearer {tok}"}).json()["event"] == {"event_id": "ev-x", "version": 3, "offset_s": 12.5}
    r = c.get(url, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403 and r.json()["code"] == 115
    r = c.get(url, headers=ev)
    assert r.status_code == 200 and r.content == (tdir / "tape.bin").read_bytes() and r.headers["x-event-offset-ns"] == "12500000000"
    assert r.headers["content-type"] == "application/x-d4dtape" and r.headers["etag"] == f'"{h["content_sha256"]}"'
    m["event"]["content_sha256"] = "0" * 64
    (run / "run_manifest.json").write_text(json.dumps(m))
    r = c.get(url, headers=ev)
    assert r.status_code == 409 and r.json()["code"] == 614


def test_truth_frame_decode_raw(tmp_path: Path) -> None:
    """BN-52 `decode=raw`：zstd 与 bshuf4-zstd 块解为原始数组，codec 记为 raw；未知取值 400。"""
    import zstandard

    from awr.bench.recorder.depthcodec import bshuf4_zstd

    s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=(ORIGIN,), secure_cookie=False)
    run = s.dataset_dir / "capture" / "runs" / "cap-0002"
    with RunWriter(run / "raw.mcap") as w:
        w.write_camera("uav-01", "cam0", 1, 100_000_000, _jpeg(1))
    build_frames_idx(run / "raw.mcap", run / "frames.idx")
    inst = np.arange(6, dtype="<u2")
    dep = np.linspace(1, 6, 6, dtype="<f4")
    rec = {"schema": "drone4d.frame.v1", "frame_id": "cap-0002/uav-01/cam0/1", "agent_view": {"width": 3, "height": 2},
           "ground_truth": {"instance": {"dtype": "u16", "codec": "zstd", "ref": "i.zst"},
                            "gt_depth": {"dtype": "f32", "unit": "m", "codec": "bshuf4-zstd", "ref": "d.zst"}, "objects": []}}
    tm, ti = truth_paths(run)
    with RunWriter(tm) as w:
        w.write_camera_truth("uav-01", "cam0", 1, 100_000_000, rec, {"i.zst": zstandard.ZstdCompressor().compress(inst.tobytes()),
                                                                     "d.zst": bshuf4_zstd(dep.tobytes())})
    build_frames_idx(tm, ti, parse=parse_truth_topic)
    (run / "run_manifest.json").write_text(json.dumps({"state": "SUCCEEDED", "kind": "capture"}))
    app = create_app(s)
    h = {"Authorization": f"Bearer {app.state.auth.issue_token('eve', 'evaluator')[1]}"}
    c = TestClient(app)
    url = f"{P}/runs/cap-0002/truth/frames/uav-01/cam0/1"
    r2, b2 = unpack_camera_gt(c.get(url + "?decode=raw", headers=h).content)
    assert r2["ground_truth"]["instance"]["codec"] == "raw" and b2["i.zst"] == inst.tobytes()
    assert r2["ground_truth"]["gt_depth"]["codec"] == "raw" and b2["d.zst"] == dep.tobytes()
    r0, _ = unpack_camera_gt(c.get(url, headers=h).content)
    assert r0["ground_truth"]["instance"]["codec"] == "zstd"
    assert c.get(url + "?decode=png", headers=h).status_code == 400                             # 参数校验失败（problem+json）
