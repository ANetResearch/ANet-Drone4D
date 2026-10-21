"""P0a 内部样例包（D3-AC-56"样例包（每个基线 1 个代表运行的事件回放、3 机私有视角视频、MCAP 与指标）在内部工作区可浏览"；
`tools/bench/d3/sample_pack.py`；契约 `bench/sample_pack.v1`、`bench/tracks.v1`；AWR-17 §18.8.3、§18.8.4）。

- 覆盖与契约：报告 `representative_runs` 的每组每基线 1 个样例，文档通过 `sample_pack.v1` 校验、不含种子与种子序号字段；
- 安装：运行装入 `<data_root>/dataset/<event>/runs/<run_id>/`（`find_run` 可见），录制与帧索引为硬链接，不安装真值层、
  秘密种子文件、gz 运行时与容器日志；运行与派生物随原型榜引用 pinned；
- 私有视角视频：只取交付给 Agent 的相机通道，帧数 = 消息数，H.264，首帧为关键帧，帧率与 t0 与录制一致；L1 运行无视频；
- 事件回放：`tracks_obs.bin` 布局（56 B 记录、尾部 sha256）与导航解逐样本一致；云台按 Env 云台模型重放（与
  `awr.agentsdk.env.camera.Gimbal` 逐 tick 一致）；磁带为录制附件原样；
- 秘密种子检索：安装后的文件含种子时拒绝写出样例包（退出码 1）；
- 续跑与复核：派生物索引一致时跳过重建；`--verify` 发现被篡改的文件；评测摘要与报告不符时拒绝。
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

pytest.importorskip("av")
pytest.importorskip("mcap")
pytest.importorskip("foxglove_schemas_protobuf")
pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return sys.modules[name]


sp = _load("sample_pack", ROOT / "tools" / "bench" / "d3" / "sample_pack.py")
sr = _load("sample_round", Path(__file__).with_name("sample_round.py"))
SCHEMA = json.loads((ROOT / "packages" / "contracts" / "bench" / "sample_pack.v1.schema.json").read_text(encoding="utf-8"))
TRACKS = json.loads((ROOT / "packages" / "contracts" / "bench" / "tracks.v1.schema.json").read_text(encoding="utf-8"))


@pytest.fixture()
def rnd(tmp_path: Path) -> dict:
    return sr.build_round(tmp_path / "repo")


def _build(rnd: dict, root: Path, **kw):
    opts = sp.BuildOpts(root, **{k: kw.pop(k) for k in ("force", "link") if k in kw})
    return sp.build_pack(rnd["report"], root, base=rnd["base"], evalstore=rnd["evalstore"], opts=opts, log=lambda *_: None, **kw)


def test_pack_covers_every_baseline_and_validates(rnd: dict, tmp_path: Path) -> None:
    import jsonschema

    root = tmp_path / "lab"
    pack, build = _build(rnd, root)
    assert build["passed"] and pack is not None, build
    jsonschema.validate(pack, SCHEMA)
    assert read(root / "samples" / "p0a-sp-round" / "pack.json") == pack
    assert {(s["group"], s["policy"]) for s in pack["samples"]} == {(g, p) for g in ("l1", "l2") for p in sr.POLICIES}
    assert pack["event_id"] == sr.EVENT and pack["world_id"] == sr.WORLD and pack["board_version"] == "sp-l2"
    assert pack["board_versions"] == {"l1": "sp-l1", "l2": "sp-l2"}
    text = json.dumps(pack)
    assert "seed_index" not in text and '"seed"' not in text and not any(str(s) in text for s in sr.SEEDS)
    for s in pack["samples"]:
        kinds = sorted(f["kind"] for f in s["files"])
        want = ["manifest", "mcap", "metrics", "tape", "tape", "tracks"] + (["video", "video"] if s["group"] == "l2" else [])
        assert kinds == sorted(want), (s["run_id"], kinds)
        assert s["metrics"]["ec"] == (0.5 if s["policy"] == "static_guard" else 1.0) and s["sim_s"] == 3.0
        assert s["board_version"] == f"sp-{s['group']}"
    assert build["seeds_checked"] == 2 and build["coverage"] == {"expected": 4, "built": 4}


def read(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def test_installed_run_layout_links_and_retention(rnd: dict, tmp_path: Path) -> None:
    from awr.bench import retention
    from awr.bench.runs_store import find_run

    root = tmp_path / "lab"
    pack, _b = _build(rnd, root)
    s = next(x for x in pack["samples"] if x["group"] == "l2" and x["policy"] == "track_share")
    d = find_run(root / "dataset", s["run_id"])
    assert d == root / "dataset" / sr.EVENT / "runs" / s["run_id"]
    src = rnd["runs"][("L2", "track_share")]
    assert (d / "raw.mcap").stat().st_ino == (src / "raw.mcap").stat().st_ino == (d / "derived" / "raw.mcap").stat().st_ino
    assert (d / "frames.idx").stat().st_ino == (src / "frames.idx").stat().st_ino
    for gone in ("truth", "phys", "agents/uav-01/container.log"):
        assert not (d / gone).exists(), gone
    assert not list(d.rglob("run_seed.json"))
    for keep in ("manifest.json", "run_manifest.json", "evaluation.json", "task.json", "agents/uav-01/acts.jsonl", "agents/obs_audit.json",
                 "net/summary.json"):
        assert (d / keep).is_file(), keep
    rec = retention.load_record(d)
    assert rec["pinned_by"] == ["leaderboard:sp-l2"]
    assert rec["derived"]["video"]["retention"] == "pinned" and rec["derived"]["files"]["pinned_by"] == ["leaderboard:sp-l2"]
    idx = read(d / "derived" / "index.json")
    assert idx["schema"] == sp.INDEX_SCHEMA and {it["kind"] for it in idx["items"]} == {"video", "files"}
    assert {it["path"]: it["layer"] for it in idx["items"]}["event/tape.bin"] == "run"
    # 安装后的运行目录与派生物中没有秘密种子
    for f in d.rglob("*"):
        if f.is_file() and f.suffix not in (".mp4",):
            assert not sp._bin_hits(f.read_bytes(), sr.SEEDS), f


def test_private_view_videos(rnd: dict, tmp_path: Path) -> None:
    import av

    root = tmp_path / "lab"
    pack, build = _build(rnd, root)
    s = next(x for x in pack["samples"] if x["group"] == "l2" and x["policy"] == "static_guard")
    vids = {f["path"]: f for f in s["files"] if f["kind"] == "video"}
    assert set(vids) == {"video/uav-01_eo.mp4", "video/uav-02_eo.mp4"}                  # slam_cam 不交付给 Agent，不进入视频
    d = root / "dataset" / sr.EVENT / "runs" / s["run_id"] / "derived"
    for path, f in vids.items():
        assert f["frames"] == sr.N and f["fps"] == 10.0 and f["t0_ns"] == 0 and (f["width"], f["height"]) == (64, 48)
        assert f["uav"] == path.split("/")[1].split("_")[0] and f["cam"] == "eo" and f["layer"] == "obs"
        pv = sp.probe_video(d / path)
        assert pv == {"codec": "h264", "width": 64, "height": 48, "frames": sr.N, "first_keyframe": True}
        with av.open(str(d / path)) as c:
            frames = list(c.decode(video=0))
            assert len(frames) == sr.N
            assert [round(fr.time * 10) for fr in frames] == list(range(sr.N))         # 帧 i 在 t0 + i / fps
    assert all(not [f for f in x["files"] if f["kind"] == "video"] for x in pack["samples"] if x["group"] == "l1")
    assert build["runs"][0]["checks"]["raw_mcap_bytes"] is True


def test_tracks_obs_layout_and_event_tape(rnd: dict, tmp_path: Path) -> None:
    import jsonschema

    root = tmp_path / "lab"
    pack, _b = _build(rnd, root)
    s = next(x for x in pack["samples"] if x["group"] == "l1" and x["policy"] == "track_share")
    d = root / "dataset" / sr.EVENT / "runs" / s["run_id"] / "derived"
    b = (d / "tracks_obs.bin").read_bytes()
    t = sp.parse_tracks(b)
    hd = t["header"]
    jsonschema.validate(hd, TRACKS)
    assert hd == {"run_id": s["run_id"], "hz": 10, "t0_ns": 0, "n_samples": sr.N,
                  "uavs": [{"uav_id": u, "primary_camera": "eo"} for u in sr.UAVS]}
    assert len(b) == 12 + len(json.dumps(hd, sort_keys=True, separators=(",", ":"))) + 2 * sr.N * 56 + 32
    for u in sr.UAVS:
        rows = t["rows"][u]
        for k in (0, 7, 30):
            nv = sr.nav(u, k)
            assert rows[k][:3] == tuple(nv["pos_enu_m"]) and rows[k][3:7] == pytest.approx(tuple(nv["q_wb_xyzw"]), abs=1e-6)
            assert rows[k][9] == 1.0 and rows[k][12] == 0
        assert rows[0][11] & 1 and not rows[20][11] & 1                                # 起降点静止为地面，爬升后离地
        assert rows[0][7:9] == pytest.approx((-30.0, 0.0))                             # Env 云台初值
    # 消息记录：k ≥ 20 的记录失败，近 5 s 窗口内成功比例下降
    q = [r[10] for r in t["rows"]["uav-01"]]
    assert q[0] == 3 and q[30] < 3
    tape = {f["path"]: f for f in s["files"] if f["kind"] == "tape"}
    assert set(tape) == {"event/tape.bin", "event/tape.json"}
    assert (d / "event" / "tape.bin").read_bytes() == sr.TAPE
    assert read(d / "event" / "tape.json")["content_sha256"] == sr.TAPE_SHA


def test_gimbal_replay_matches_env_model() -> None:
    from awr.agentsdk.env.camera import CameraModel, Gimbal

    g = sp.GimbalSpec((-90.0, 30.0), (-150.0, 150.0), 90.0)
    nav = sr.nav("uav-01", 5)
    p, y = sp.gimbal_command({"mode": "look_at", "target_enu_m": [40.0, -5.0, 0.0]}, nav, g)
    d = [40.0 - nav["pos_enu_m"][0], -5.0 - nav["pos_enu_m"][1], 0.0 - nav["pos_enu_m"][2]]
    assert p == pytest.approx(math.degrees(math.atan2(d[2], math.hypot(d[0], d[1]))))
    assert y == pytest.approx(-90.0)                                                    # 机头朝北，目标正东
    assert sp.gimbal_command({"mode": "angles", "pitch_deg": 50.0, "yaw_deg": 200.0}, None, g) == (30.0, -150.0)
    cmds = [(6, p, y), (16, -10.0, 20.0), (17, 10.0, 170.0)]
    series = sp.gimbal_series(40, 0.1, cmds, g)
    ref = Gimbal(cam=CameraModel(pitch_range_deg=(-90.0, 30.0), yaw_range_deg=(-150.0, 150.0), rate_max_dps=90.0))
    want = [(ref.pitch_deg, ref.yaw_deg)]
    pend = list(cmds)
    for k in range(1, 40):                                                              # core.Env.step：先限速转向，再接受生效的指令
        ref.slew(0.1)
        for c in [c for c in pend if c[0] <= k]:
            ref.command(c[1], c[2])
            pend.remove(c)
        want.append((ref.pitch_deg, ref.yaw_deg))
    assert series == pytest.approx(want)


def test_seed_leak_blocks_the_pack(rnd: dict, tmp_path: Path) -> None:
    src = rnd["runs"][("L2", "static_guard")]
    cfg = read(src / "agents" / "uav-02" / "config.json")
    cfg["debug"] = {"note": f"seed {sr.SEEDS[1]}"}
    (src / "agents" / "uav-02" / "config.json").write_text(json.dumps(cfg))
    root = tmp_path / "lab"
    pack, build = _build(rnd, root)
    assert pack is None and not build["passed"] and not (root / "samples" / "p0a-sp-round" / "pack.json").exists()
    assert any("secret seed" in e for e in build["errors"])
    assert str(sr.SEEDS[1]) not in json.dumps(build)                                     # 记录只给出种子位置
    rc = sp.main(["--report", str(rnd["report"]), "--data-root", str(tmp_path / "lab2"), "--base", str(rnd["base"]),
                  "--evalstore", str(rnd["evalstore"])])
    assert rc == 1


def test_resume_reuses_current_runs_and_verify(rnd: dict, tmp_path: Path) -> None:
    root = tmp_path / "lab"
    _build(rnd, root)
    _p, b2 = _build(rnd, root)
    assert b2["passed"] and all(r["reused"] for r in b2["runs"])
    _p, b3 = _build(rnd, root, force=True)
    assert b3["passed"] and not any(r["reused"] for r in b3["runs"])
    # 只有生成器版本不同：重建运行目录与其余派生物，视频保留不重新编码
    pack = read(root / "samples" / "p0a-sp-round" / "pack.json")
    s2 = next(x for x in pack["samples"] if x["group"] == "l2")
    der = root / "dataset" / sr.EVENT / "runs" / s2["run_id"] / "derived"
    ino = (der / "video" / "uav-01_eo.mp4").stat().st_ino
    idx = read(der / "index.json")
    (der / "index.json").write_text(json.dumps({**idx, "generator": "old"}))
    _p, b4 = _build(rnd, root)
    r4 = next(r for r in b4["runs"] if r["run_id"] == s2["run_id"])
    assert b4["passed"] and not r4["reused"] and r4["videos_reused"] and (der / "video" / "uav-01_eo.mp4").stat().st_ino == ino
    assert r4["videos"]["uav-01_eo"]["frames"] == sr.N and not list(der.parent.parent.glob(".*.video"))
    assert sp.verify_pack(root, "p0a-sp-round")["ok"]
    assert sp.main(["--report", str(rnd["report"]), "--data-root", str(root), "--verify"]) == 0
    pack = read(root / "samples" / "p0a-sp-round" / "pack.json")
    s = pack["samples"][0]
    f = root / "dataset" / sr.EVENT / "runs" / s["run_id"] / "derived" / "evaluation.json"
    f.write_text(f.read_text() + " ")
    res = sp.verify_pack(root, "p0a-sp-round")
    assert not res["ok"] and any("evaluation.json" in x for x in res["bad"])


def test_rejects_runs_that_do_not_match_the_report(rnd: dict, tmp_path: Path) -> None:
    src = rnd["runs"][("L2", "track_share")]
    with open(src / "raw.mcap", "ab") as f:                                             # 录制文件被截断或追加：字节数与写入器统计不符
        f.write(b"x")
    rep = read(rnd["report"])
    rep["runs"][1]["evaluation_sha256"] = "0" * 64
    rnd["report"].write_text(json.dumps(rep))
    pack, build = _build(rnd, tmp_path / "lab")
    assert pack is None and build["coverage"] == {"expected": 4, "built": 2}
    assert any("evaluation.json sha256 differs" in e for e in build["errors"])
    assert any("raw.mcap size differs" in e for e in build["errors"])
    rep["schema"] = "other"
    rnd["report"].write_text(json.dumps(rep))
    assert sp.main(["--report", str(rnd["report"]), "--data-root", str(tmp_path / "lab")]) == 2


def test_parallel_build_matches_serial(rnd: dict, tmp_path: Path) -> None:
    a, _ = _build(rnd, tmp_path / "a")
    b, _ = _build(rnd, tmp_path / "b", jobs=2)
    strip = [{k: v for k, v in s.items()} for s in a["samples"]]
    assert [x["files"] for x in b["samples"]] == [x["files"] for x in strip]
