"""`.rrd` 在线写入与离线导出逐实体一致（AWR-05 §6.1.2；M27-FR-075、M27-AC-021；D3-AC-20"从 MCAP 离线导出的 `.rrd` 与在线写入
逐实体一致（`rerun rrd compare`）"）。

- 在线写入经 `McapTee` 接在 `RunWriter` 上，与 MCAP 同步收到同一消息流；运行结束后 `finalize` 补写段属性与相机标定并执行
  `rerun rrd optimize`；判据为两侧 optimize 后 `rerun rrd compare --unordered` 通过，且每个实体的行读出摘要相同；
- 合成运行（2 机，相机 JPEG + L2-depth + 真值帧、LiDAR + 真值节点、IMU、GNSS、导航解、L1 检测、链路、可见度）：写线程内处理
  （`inline`，`review` 档）与每分片一个进程（`process`，`dataset` 档 H.264 + PNG16、`public` 档缩放抽帧）三种组合全部一致，且没有
  任何分片走离线追赶（`catchup == {}`）；
- 在线输出与离线输出同样满足路径校验与 `rerun rrd verify`、种子只在 truth / eval 层、`/fleet/<uav>` 为带噪导航解；
- 判据有效：导航噪声不同的运行比较不一致，并定位到 `/fleet/<uav>` 等实体；
- 保护：分片队列满、分片异常、传感器映射变化时该分片由 MCAP 离线追赶，原因写入清单，比较仍一致；旁路异常不影响 MCAP；
- Env L2-depth 运行（CPU 替身渲染）经 `attach_env` 在运行期写出，`manifest.json` 写出后自动收尾，与离线导出逐实体一致；
- 命令行 `replay --compare` 与 `compare`。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rerun")
pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")
pytest.importorskip("zstandard")

from awr.bench.recorder import rerun_export as rx
from awr.bench.recorder import rerun_online as ro
from awr.bench.recorder.writer import RunWriter, channel_counts

ROOT = Path(__file__).resolve().parents[3]
RUN_ID, WORLD, EVENT = "run-0001", "Campus.V2", "handoff.01"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tx = _load("d3_rerun_export_tests_for_online", ROOT / "tests" / "d3" / "rerun" / "test_export.py")     # 合成运行 make_run


def _online_run(root: Path, monkeypatch, *, run_id: str = RUN_ID, nav_scale: float = 1.0, **kw) -> tuple[Path, ro.OnlineRrd]:
    """运行期接入在线写入：make_run 创建的两个 RunWriter（raw.mcap、truth/run_<id>.mcap）各接一个 McapTee。"""
    on = ro.OnlineRrd(root / "online", run_id=run_id, world=WORLD, event=EVENT, **kw)

    def writer(path, **wkw):
        w = RunWriter(path, **wkw)
        on.attach(w, "truth" if Path(path).parent.name == "truth" else "raw")
        return w

    monkeypatch.setattr(tx, "RunWriter", writer)
    run = tx.make_run(root / "src", run_id=run_id, nav_scale=nav_scale)
    monkeypatch.setattr(tx, "RunWriter", RunWriter)
    return run, on


def _assert_equal(run: Path, man: dict, work: Path, *, entities: bool = True) -> dict:
    """`rerun rrd compare`（两侧归一化后）逐文件通过；`entities` 时每个实体的行摘要相同。"""
    res = ro.compare_online(run, Path(man["files"]["obs.uav-01"]["path"]).parent, work_dir=work, entities=entities)
    for k, r in res["files"].items():
        assert r["compare"], (k, r.get("compare_output"))
        if entities:
            assert r["entity_mismatch"] == [] and r["entities_equal"] == r["entities"] > 0, (k, r)
    assert res["ok"] and res["missing"] == []
    return res


@pytest.fixture(scope="module")
def inline_review(tmp_path_factory) -> tuple[Path, dict, dict]:
    mp = pytest.MonkeyPatch()
    try:
        root = tmp_path_factory.mktemp("online-review")
        run, on = _online_run(root, mp, profile="review", mode="inline")
        man = on.finalize(run, verify=True)
        res = _assert_equal(run, man, root / "cmp")
    finally:
        mp.undo()
    return run, man, res


# ---------------------------------------------------------------------- 在线路径一致性


def test_inline_review_matches_offline_per_entity(inline_review) -> None:
    _run, man, res = inline_review
    assert man["writer"] == "online" and man["online"]["catchup"] == {} and man["online"]["errors"] == []
    assert man["online"]["attached"] == ["raw", "truth"] and man["online"]["optimized"]
    assert set(man["files"]) == set(res["files"]) == {"obs.uav-01", "obs.uav-02", "truth", "eval"}
    assert man["verify"] == {"ok": True, "problems": {}}
    # 逐实体：观测分片含导航解、遥测、相机（Pinhole、静态与逐帧云台位姿、RGB、深度、检测）、LiDAR 与 IMU / GNSS
    ents = {k: r["entities"] for k, r in res["files"].items()}
    assert ents["obs.uav-01"] >= 12 and ents["truth"] >= 8 and ents["eval"] >= 5, ents
    assert res["files"]["obs.uav-01"]["rows"] > 30 * 20                         # IMU 600 行 + 帧与位姿
    on_cam = man["files"]["obs.uav-02"]["cameras"]["cam0"]
    assert on_cam["cam_pose"] == "per_frame" and on_cam["calib_source"] == "agent_view"   # 云台随帧变化：结束时判定，与离线一致
    assert man["files"]["obs.uav-01"]["cameras"]["cam0"]["cam_pose"] == "static"
    assert man["files"]["truth"]["lidar_frames"] == 2 * 30 and man["files"]["truth"].get("lidar_skipped", 0) == 0


def test_online_layers_lint_seed_and_noisy_fleet(inline_review) -> None:
    _run, man, _res = inline_review
    for k, f in man["files"].items():
        p = Path(f["path"])
        layer = "obs" if k.startswith("obs.") else k
        assert rx.lint_rrd(p, layer, uav=f.get("uav"), run_id=RUN_ID, app_id=man["application_id"]) == []
        inv = rx.scan_rrd(p)
        assert inv.application_id == "drone4d-d3-campus-v2-handoff-01" and "." not in inv.application_id
        assert inv.recording_id == RUN_ID and inv.timelines == {"sim_time", "tick"} and inv.version == rx.RERUN_LOCKED
        assert inv.properties["layer"] == layer and inv.properties["rerun_version"] == rx.RERUN_LOCKED
        assert ("seed" in inv.properties) == (layer in ("truth", "eval"))
        assert inv.properties["tape_sha256"] == "e" * 64                       # 运行结束时写出的 manifest.json
    obs = Path(man["files"]["obs.uav-01"]["path"])
    rows = [(t, v["Transform3D:translation"][0]) for t, v in rx.read_entity(obs, "/fleet/uav-01") if "Transform3D:translation" in v]
    assert len(rows) == 30
    for t, tr in rows:
        assert np.allclose(tr, tx._truth_pos("uav-01", t) + np.array(tx.NAV_OFF["uav-01"]), atol=1e-4)
    assert not [e for e in rx.scan_rrd(obs).entities if e.startswith(("/truth", "/eval"))]


def test_process_mode_dataset_profile_one_process_per_shard(tmp_path: Path, monkeypatch) -> None:
    run, on = _online_run(tmp_path, monkeypatch, profile="dataset", mode="process", on_full="block", queue_msgs=64)
    man = on.finalize(run, verify=True)
    assert man["online"]["catchup"] == {} and man["online"]["mode"] == "process"
    pids = {k: f["online"]["pid"] for k, f in man["files"].items()}
    assert len(set(pids.values())) == 4 and os.getpid() not in pids.values()   # 每机一个观测分片进程 + truth + eval
    cs = man["files"]["obs.uav-01"]["cameras"]["cam0"]
    assert cs["video"]["codec"] == "H264" and cs["rgb_samples"] == 30 and cs["keyframes"] == 2
    _assert_equal(run, man, tmp_path / "cmp")


def test_process_mode_public_profile(tmp_path: Path, monkeypatch) -> None:
    run, on = _online_run(tmp_path, monkeypatch, profile="public", mode="process", on_full="block")
    man = on.finalize(run)
    assert set(man["files"]) == {"obs.uav-01", "obs.uav-02"} and man["online"]["catchup"] == {}
    cs = man["files"]["obs.uav-01"]["cameras"]["cam0"]
    assert cs["rgb_samples"] == 15 and cs["depth_frames"] == 6                 # 抽帧按绝对仿真时间分桶，在线与离线一致
    _assert_equal(run, man, tmp_path / "cmp")


def test_compare_detects_difference(inline_review, tmp_path: Path) -> None:
    _run, man, _res = inline_review
    other = tx.make_run(tmp_path / "src2", nav_scale=1.5)                       # 同一身份、导航噪声不同
    res = ro.compare_online(other, Path(man["files"]["obs.uav-01"]["path"]).parent, work_dir=tmp_path / "cmp")
    assert not res["ok"]
    r = res["files"]["obs.uav-01"]
    assert not r["compare"] and "/fleet/uav-01" in r["entity_mismatch"]
    assert "/fleet/uav-01/telemetry/altitude" in r["entity_mismatch"]
    assert res["files"]["truth"]["compare"]                                    # 真值不变


# ---------------------------------------------------------------------- 保护与追赶


def test_queue_full_catches_up_from_mcap(tmp_path: Path, monkeypatch) -> None:
    run, on = _online_run(tmp_path, monkeypatch, profile="review", mode="process", on_full="catchup", queue_msgs=1)
    man = on.finalize(run)
    assert man["online"]["catchup"], "a one-item queue must overflow during the run"
    assert all("queue full" in v for v in man["online"]["catchup"].values())
    for k in man["online"]["catchup"]:
        assert man["files"][k]["online"]["catchup"]
    _assert_equal(run, man, tmp_path / "cmp", entities=False)
    assert sum(channel_counts(run / "raw.mcap").values()) > 0                  # MCAP 不受影响


def test_shard_failure_and_tap_error_do_not_touch_mcap(tmp_path: Path, monkeypatch) -> None:
    calls = {"n": 0}
    orig = ro._Shard._m_truth

    def boom(self, *a):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("injected shard failure")
        return orig(self, *a)

    monkeypatch.setattr(ro._Shard, "_m_truth", boom)
    run, on = _online_run(tmp_path / "a", monkeypatch, profile="review", mode="inline")
    man = on.finalize(run)
    assert set(man["online"]["catchup"]) == {"truth"} and "injected shard failure" in man["online"]["catchup"]["truth"]
    _assert_equal(run, man, tmp_path / "a" / "cmp", entities=False)
    monkeypatch.setattr(ro._Shard, "_m_truth", orig)

    def bad_tap(self, *a):
        raise ValueError("injected tap error")

    monkeypatch.setattr(ro.OnlineRrd, "_route_raw", bad_tap)
    run2, on2 = _online_run(tmp_path / "b", monkeypatch, profile="review", mode="inline")
    counts = channel_counts(run2 / "raw.mcap")
    assert counts["/uav/uav-01/obs/nav"] == 30 and counts["/uav/uav-01/camera/eo/image"] == 30   # MCAP 完整
    man2 = on2.finalize(run2)
    assert set(man2["online"]["catchup"]) == {"obs.uav-01", "obs.uav-02", "truth", "eval"} and man2["online"]["errors"]
    _assert_equal(run2, man2, tmp_path / "b" / "cmp", entities=False)


def test_sensor_map_and_identity_checks(tmp_path: Path, monkeypatch) -> None:
    run, on = _online_run(tmp_path, monkeypatch, profile="review", mode="inline", cams=["aa", "eo"])   # eo → cam1，运行结束为 cam0
    man = on.finalize(run)
    assert set(man["online"]["catchup"]) == {"obs.uav-01", "obs.uav-02", "truth", "eval"}
    assert all("sensor map" in v for v in man["online"]["catchup"].values())
    _assert_equal(run, man, tmp_path / "cmp", entities=False)
    with pytest.raises(ValueError):
        ro.OnlineRrd(tmp_path / "x", run_id=RUN_ID, world=WORLD, event=EVENT, profile="public", layers=["obs", "truth"])
    with pytest.raises(ValueError):
        ro.OnlineRrd(tmp_path / "x", run_id=RUN_ID, world=WORLD, event=EVENT, mode="thread")


# ---------------------------------------------------------------------- Env 运行与命令行


def _open3d_error() -> str | None:
    """Env 合成世界需要 open3d；未安装或无法载入（如镜像缺 libX11）时只跳过 Env 用例。"""
    try:
        import open3d  # noqa: F401
    except (ImportError, OSError) as e:
        return f"{type(e).__name__}: {e}"
    return None


_O3D = _open3d_error()
if _O3D is None:
    synth_event = _load("d3_env_conftest_for_rerun_online", ROOT / "tests" / "d3" / "env" / "conftest.py").synth_event
else:
    @pytest.fixture(scope="session")
    def synth_event():
        pytest.skip(f"[dep] open3d unavailable (Env synthetic world): {_O3D}")


def test_env_l2_depth_run_online(synth_event, tmp_path: Path, monkeypatch) -> None:
    from awr.agentsdk.env.recorder import EnvRecorder

    held: dict = {}
    orig = EnvRecorder._open_mcap

    def open_mcap(self) -> None:
        orig(self)
        held["on"] = ro.attach_env(self, tmp_path / "online", profile="review", mode="process", on_full="block", verify=True)

    monkeypatch.setattr(EnvRecorder, "_open_mcap", open_mcap)
    t_rec = _load("d3_recorder_tests_for_rerun_online", ROOT / "tests" / "d3" / "bench" / "test_recorder.py")
    run = tmp_path / "run-env"
    t_rec._episode(synth_event, run, "L2-depth", duration_s=1.0)
    on = held["on"]
    assert on.error is None and on.finalized is not None                      # manifest.json 写出后自动收尾
    man = on.finalized
    assert man["online"]["catchup"] == {} and man["verify"]["ok"]
    assert set(man["files"]) == {"obs.uav-01", "obs.uav-02", "truth", "eval"}
    assert man["files"]["obs.uav-01"]["cameras"]["cam0"]["sensor_id"] == "eo"
    res = ro.compare_online(run, tmp_path / "online", work_dir=tmp_path / "cmp")
    for k, r in res["files"].items():
        assert r["compare"] and r["entity_mismatch"] == [], (k, r.get("compare_output"), r.get("entity_mismatch"))
    assert res["ok"]


def test_cli_replay_and_compare(tmp_path: Path, capsys) -> None:
    run = tx.make_run(tmp_path / "src")
    rc = ro.main(["replay", "--run", str(run), "--out", str(tmp_path / "o"), "--profile", "review", "--mode", "inline", "--compare",
                  "--verify"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and out["ok"] and out["online"]["catchup"] == {} and out["replay"]["messages"] > 0
    assert all(r["compare"] for r in out["compare"]["files"].values())
    rc = ro.main(["compare", "--run", str(run), "--online", str(tmp_path / "o"), "--no-entities"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and out["ok"]
