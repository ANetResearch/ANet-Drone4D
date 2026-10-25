"""`ranked_eval` 保留条款接入生产（K17；D3-AC-22 保留条款、M27-FR-171、ADR-163 v1.3；B33 遗留 1–6、B21 遗留 2）。

- 逐帧摘要：LiDAR 帧取去 run 规范形式（存档帧头 `frame_id` 含 run 段），同一种子、不同 run_id 的两次真实 L2 Env 运行摘要逐字节相同
  （去重成立）；`raw.mcap` 单遍读取（每个 chunk 只解压一次），结果与按通道逐页读取的口径逐行一致；
- `finalize_ranked_eval`：抽样、精简、去重（去重的 Run 只省摘要与预览，精简真值保留），精简后以评测器复评 `evaluation.json` 逐字节一致；
  重入不重读、不重抽；`dry_run` 不写文件；pinned 类别不被改写；隐藏区运行真值按同一规则精简；残留套接字清理后 `copytree` 可用；
- `finalize_board`：按评测行定位运行目录（数据集布局与 mbl_e2e 的 `<out>/runs/<key>`），标准布局外的目录登记后由 `retention_gc` 照常处理；
- 平台复现审计遇到精简后的保留摘要时改由运行目录现算（不因格式不同报错）；
- subworker：处理租约（文件锁 + 心跳）、孤立 EVALUATING 提交的自动恢复（与 `board_v1_formal.py resume_submission` 同语义，旧版与 v1
  两种榜单配置）、提交 Run 的套接字清理与评分后保留。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import shutil
import stat
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from awr.bench import retention as R
from awr.bench.recorder.lidar import canonical_lidar_frame, pack_lidar_frame, unpack_lidar_frame

ROOT = Path(__file__).resolve().parents[3]
ENV_TESTS = ROOT / "tests" / "d3" / "env"
MIN = 60 * 10 ** 9
P = R.RetentionPolicy(day_ns=MIN)
T0 = 1_760_000_000 * 10 ** 9
SEED0 = next(s for s in range(100) if random.Random(s).randrange(2) == 0)               # 两个 Run 的组中抽中第 1 个（rep 0）


def _sock(p: Path) -> None:
    """在 p 处建一个 AF_UNIX 套接字文件（不受套接字路径长度上限约束）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    os.mknod(p, stat.S_IFSOCK | 0o600)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_conf = _load("d3_env_conftest_for_ranked_retention", ENV_TESTS / "conftest.py")
synth_event = _conf.synth_event


# ---------------------------------------------------------------------------------------------------------------------------- 单元
def test_canonical_lidar_frame_strips_only_the_run_segment() -> None:
    from awr.bench.recorder.lidar import LIDAR_RECORD

    pts = np.zeros(3, LIDAR_RECORD)
    pts["x"] = [1.0, 2.0, 3.0]
    hdr = {"schema": "awr.sensor.lidar_frame.v2", "sensor_id": "lidar0", "frame_id": "run-a/uav-01/lidar0/7", "frame_seq": 7, "point_count": 3}
    a = pack_lidar_frame(hdr, pts)
    b = pack_lidar_frame({**hdr, "frame_id": "another-run-id/uav-01/lidar0/7"}, pts)
    assert a != b and canonical_lidar_frame(a) == canonical_lidar_frame(b)
    h, p = unpack_lidar_frame(canonical_lidar_frame(a))
    assert list(h) == list(hdr) and h["frame_id"] == "uav-01/lidar0/7" and p.tobytes() == pts.tobytes()
    plain = pack_lidar_frame({**hdr, "frame_id": "uav01/mid360"}, pts)                  # 无 run 段：原样
    assert canonical_lidar_frame(plain) == plain


def test_clean_run_sockets_unblocks_copytree(tmp_path: Path) -> None:
    run = tmp_path / "r"
    (run / "phys" / "sock").mkdir(parents=True)
    (run / "phys" / "run.sdf").write_text("<sdf/>")
    _sock(run / "phys" / "sock" / "ctl.sock")
    with pytest.raises(shutil.Error):
        shutil.copytree(run, tmp_path / "copy0")
    assert R.clean_run_sockets(run) == ["phys/sock/ctl.sock"]
    assert not (run / "phys" / "sock").exists() and (run / "phys" / "run.sdf").is_file()
    shutil.copytree(run, tmp_path / "copy1")
    assert R.clean_run_sockets(run) == [] and R.clean_run_sockets(tmp_path / "missing") == []


# ---------------------------------------------------------------------------------------------------------------------------- 真实 L2 运行
def _episode(synth_event, run_dir: Path, run_id: str, seed: int = 3, duration_s: float = 2.0) -> None:
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.spec import EnvSpec

    t_l2 = _load("d3_env_test_l2_for_ranked_retention", ENV_TESTS / "test_l2.py")
    w, ev = synth_event
    spec = EnvSpec(run_id, seed, w, ev, t_l2.FLEET, obs_level="L2", physics="kinematic", duration_s=duration_s, out_dir=run_dir,
                   ranked=True, seed_commit="hmac:0123abcd")
    env = BenchEnv(spec, l2=lambda e: t_l2._l2(e, False))
    obs, _ = env.reset()
    while env.agents:
        acts = {u: {"nav": {"kind": "goto", "target_enu": [0.0, 60.0, 30.0], "speed_mps": 4.0}, "gimbal": {"pitch_deg": -45.0}} for u in obs}
        obs, *_ = env.step(acts)
    env.close()


@pytest.fixture(scope="module")
def l2_pair(synth_event, tmp_path_factory) -> dict:
    """同一种子、不同 run_id 的两次 L2 运行（数据集布局，同一"提交 × 事件 × 种子"的两次重复）与评测器复评所需的世界查询。"""
    from awr.agentsdk.env.core import _open_world
    from awr.bench.eval.evaluator import evaluate_run

    root = tmp_path_factory.mktemp("k17") / "d3"
    w, ev = synth_event
    runs = {}
    for rep in (0, 1):
        rid = f"sub-k17-ev-k0r{rep}"
        d = root / "dataset" / "ev-k17" / "runs" / rid
        _episode(synth_event, d, rid)
        world = _open_world(w)
        (d / "evaluation.json").write_text(json.dumps(evaluate_run(d, ev, world), ensure_ascii=False, indent=1) + "\n")
        runs[rep] = d
    src = tmp_path_factory.mktemp("k17src")
    for d in runs.values():                                                            # 未精简副本（复评对照）
        shutil.copytree(d, src / d.name)
    return {"root": root, "runs": runs, "src": src, "event_dir": ev, "world_dir": w}


def _paged_digest_rows(run: Path) -> list[tuple[str, str, int, str, str]]:
    """按通道逐页读取的口径（v1 实现的读取方式，LiDAR 行同样取规范形式）：与单遍读取逐行比较。"""
    from awr.bench.recorder.lidar import LIDAR_CODEC
    from awr.bench.recorder.read import FrameStore

    fs = FrameStore(run)
    rows = []
    try:
        for uav, sensor in fs.idx.channels():
            info = fs.channel_info(uav, sensor)
            if info.get("agent_delivered", "true") == "false":
                continue
            for r in fs.idx.span(uav, sensor, 0, 2 ** 32 - 1):
                f = fs.frame(uav, sensor, int(r["seq"]))
                b = canonical_lidar_frame(f.payload) if info.get("codec") == LIDAR_CODEC else f.payload
                rows.append((uav, sensor, f.seq, hashlib.sha256(b).hexdigest(), str(len(b))))
    finally:
        fs.close()
    return sorted(rows, key=lambda x: (x[0], x[1], x[2]))


def test_frames_digest_dedups_lidar_runs_and_reads_each_chunk_once(l2_pair, monkeypatch) -> None:
    from awr.bench.recorder.read import FrameStore
    from awr.recorder.chunk_cache import ChunkCache

    a, b = l2_pair["runs"][0], l2_pair["runs"][1]
    fa, fb = FrameStore(a), FrameStore(b)
    try:
        la = fa.frame("uav-01", "lidar0", 0).payload
        lb = fb.frame("uav-01", "lidar0", 0).payload
        n_chunks = len(fa.seg.chunks)
    finally:
        fa.close()
        fb.close()
    assert la != lb and canonical_lidar_frame(la) == canonical_lidar_frame(lb)       # 存档帧头含 run 段，规范形式相同

    loads = []
    real = ChunkCache._load
    monkeypatch.setattr(ChunkCache, "_load", lambda self, ci: loads.append(ci) or real(self, ci))
    da = R.frames_digest(a)
    assert sorted(loads) == list(range(n_chunks)) and n_chunks > 1                    # 每个 chunk 只解压一次
    monkeypatch.setattr(ChunkCache, "_load", real)
    assert da == R.frames_digest(b)                                                     # 同一帧流的两个 Run 逐字节相同
    lines = da.decode().splitlines()
    assert lines[0] == "#drone4d.bench.frames_digest.v1"
    rows = [tuple(x.split("\t")) for x in lines[1:]]
    sensors = {r[1] for r in rows}
    assert {"eo", "lidar0", "obs"} <= sensors
    assert [r for r in rows if r[1] != "obs"] == [(u, s, str(q), h, n) for u, s, q, h, n in _paged_digest_rows(a)]


def test_finalize_compacts_dedups_and_reevaluates_byte_identical(l2_pair) -> None:
    from awr.agentsdk.env.core import _open_world
    from awr.bench.eval.evaluator import evaluate_run

    root, runs = l2_pair["root"], l2_pair["runs"]
    rr = [R.RankedRun(runs[rep], "sub-k17", "ev-k17", 0, rep) for rep in (0, 1)]
    plan = R.finalize_ranked_eval(rr, root=root, policy=P, now=T0, rng=random.Random(SEED0), dry_run=True)     # 只返回计划
    assert plan[runs[1].name]["phase"] == "planned" and plan[runs[1].name]["dup_of"] == runs[0].name
    assert plan[runs[0].name]["phase"] == "sampled" and plan[runs[1].name]["bytes_freed"] > 0
    assert (runs[1] / "raw.mcap").exists() and not (runs[1] / "retention.json").exists() and not (root / "audit").exists()

    out = R.finalize_ranked_eval(rr, root=root, policy=P, now=T0, rng=random.Random(SEED0), tenant="team-k")
    s, dup = runs[0], runs[1]
    assert out[s.name]["phase"] == "sampled" and (s / "raw.mcap").exists()
    assert out[dup.name] == {**out[dup.name], "phase": "compacted", "dup_of": s.name}
    assert out[dup.name]["frames_digest_sha256"] == out[s.name]["frames_digest_sha256"]
    assert not (dup / "raw.mcap").exists() and not (dup / "frames.idx").exists() and not (dup / "frames.digest").exists()
    assert {q.name for q in (dup / "truth").iterdir()} <= set(R.TRUTH_MIN_FILES) and (dup / "truth_min.json").is_file()
    assert not any(p.name.endswith(".mcap") for p in (dup / "truth").iterdir())
    assert out[dup.name]["bytes_after"] < 0.2 * out[dup.name]["bytes_before"]
    rec = R.load_record(dup)
    assert rec["retention"] == "ranked_eval" and rec["tenant"] == "team-k" and rec["ranked_eval"]["dup_of"] == s.name

    # 精简后复评 evaluation.json 逐字节一致（评测器读取的输入全部保留）
    world = _open_world(l2_pair["world_dir"])
    got = json.dumps(evaluate_run(dup, l2_pair["event_dir"], world), ensure_ascii=False, indent=1) + "\n"
    assert got == (dup / "evaluation.json").read_text(encoding="utf-8") == (l2_pair["src"] / dup.name / "evaluation.json").read_text()

    # 重入：沿用登记结论，不重读、不重抽
    again = R.finalize_ranked_eval(rr, root=root, policy=P, now=T0 + 1, rng=random.Random(1))
    assert again[s.name]["phase"] == "sampled" and again[s.name]["reused"] and again[dup.name]["phase"] == "compacted"
    assert R.load_record(s)["ranked_eval"]["sample_until_unix_ns"] == T0 + 30 * MIN

    # 抽样录制 30 天后精简为完整摘要（首次运行，不去重）
    R.retention_gc(root, policy=P, now=T0 + 30 * MIN)
    assert not (s / "raw.mcap").exists() and (s / "frames.digest").read_bytes() == R.frames_digest(l2_pair["src"] / s.name)
    got = json.dumps(evaluate_run(s, l2_pair["event_dir"], world), ensure_ascii=False, indent=1) + "\n"
    assert got == (s / "evaluation.json").read_text(encoding="utf-8")


def _fake_run(d: Path, *, frames: int, extra_truth: bool = True) -> Path:
    """无 raw.mcap 的运行目录（观测摘要决定逐帧摘要）。"""
    (d / "agents" / "uav-01").mkdir(parents=True)
    (d / "agents" / "uav-01" / "obs.sha256").write_text("".join(f"{k} {hashlib.sha256(bytes([frames, k])).hexdigest()}\n" for k in range(4)))
    (d / "agents" / "uav-01" / "acts.jsonl").write_text('{"k":0}\n')
    for n in ("manifest.json", "task.json", "evaluation.json"):
        (d / n).write_text("{}")
    (d / "evaluation.0.2.0.json").write_text("{}")
    (d / "termination.json").write_text("{}")
    (d / "raw.mcap").write_bytes(b"x" * 1000)
    (d / "truth").mkdir()
    for n in ("fleet.npz", "l1.jsonl", "contacts.jsonl"):
        (d / "truth" / n).write_bytes(b"t")
    if extra_truth:
        (d / "truth" / f"run_{d.name}.mcap").write_bytes(b"m" * 500)
    return d


def test_finalize_pinned_hidden_truth_and_errors(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    runs = {n: _fake_run(root / "dataset" / "ev-t" / "runs" / n, frames=1) for n in ("sub-a-k0r0", "sub-a-k0r1", "sub-a-k1r0", "sub-a-k1r1")}
    R.register_run(runs["sub-a-k1r1"], retention="pinned")                             # 类别 pinned：不改写、不精简
    hid = root / "hidden" / "ev-t" / "truth" / "runs" / "sub-a-k0r1"                   # seal-truth 之后的运行真值
    hid.mkdir(parents=True)
    for n in ("fleet.npz", "l1.jsonl", "run_seed.json", "run_sub-a-k0r1.mcap"):
        (hid / n).write_bytes(b"h" * 10)
    real = R.frames_digest
    monkeypatch.setattr(R, "frames_digest", lambda d: (_ for _ in ()).throw(OSError("bad chunk")) if Path(d).name == "sub-a-k1r0" else real(d))
    rr = [R.RankedRun(d, "sub-a", "ev-t" if n[-3] == "0" else "ev-u", int(n[-3]), int(n[-1])) for n, d in runs.items()]
    b_hid = R.run_bytes(runs["sub-a-k0r1"], root)["total"] + 40                        # 精简前字节含隐藏区运行真值
    dry = R.finalize_ranked_eval(rr, root=root, policy=P, now=T0, rng=random.Random(SEED0), dry_run=True)
    assert dry["sub-a-k0r1"]["bytes_before"] == b_hid and dry["sub-a-k0r1"]["bytes_after"] > 0
    assert dry["sub-a-k0r1"]["bytes_freed"] == 1000 + 500 + 10
    out = R.finalize_ranked_eval(rr, root=root, policy=P, now=T0, rng=random.Random(SEED0))    # ev-t 组抽中 k0r0
    assert out["sub-a-k1r1"]["phase"] == "pinned" and R.load_record(runs["sub-a-k1r1"])["retention"] == "pinned"
    assert (runs["sub-a-k1r1"] / "raw.mcap").exists()
    assert out["sub-a-k1r0"]["phase"] == "error" and "bad chunk" in out["sub-a-k1r0"]["error"] and (runs["sub-a-k1r0"] / "raw.mcap").exists()
    assert [n for n, x in out.items() if x["phase"] == "sampled"] == ["sub-a-k0r0"]
    other = "sub-a-k0r1"
    d = runs[other]
    assert out[other]["phase"] == "compacted"
    for keep in ("evaluation.json", "evaluation.0.2.0.json", "termination.json", "truth/fleet.npz", "truth_min.json"):
        assert (d / keep).exists(), keep
    assert not (d / "raw.mcap").exists() and not (d / "truth" / f"run_{other}.mcap").exists()
    assert sorted(p.name for p in hid.iterdir()) == ["fleet.npz", "l1.jsonl", "run_seed.json"]   # 隐藏区：只删非精简真值
    tm = json.loads((d / "truth_min.json").read_text())
    hidden = [f for f in tm["files"] if f.get("store") == "hidden"]                    # 清单只记文件名与字节数，不读内容
    assert {f["path"] for f in hidden} == {"truth/fleet.npz", "truth/l1.jsonl", "truth/run_seed.json"}
    assert all("sha256" not in f and f["bytes"] == 10 for f in hidden)


def test_finalize_board_external_runs_root_and_gc(tmp_path: Path) -> None:
    """mbl_e2e 原型榜布局：基线 Run 在 `<out>/runs/<key>`（run_id 带轮次前缀），数据根在别处；登记后 retention_gc 照常处理抽样到期。"""
    from awr.bench.boardstore import append_rows

    root, out = tmp_path / "bench", tmp_path / "round"
    bdir = root / "boards" / "r-l1"
    bdir.mkdir(parents=True)
    (bdir / "config.json").write_text(json.dumps({"board_version": "r-l1", "track": {"dev": True}, "mbl_round": {"round": "rnd", "out": str(out)}}))
    rows = []
    for k in range(2):
        for rep in (1, 2):
            key = f"l1-static_guard-k{k:02d}-p{rep}"
            _fake_run(out / "runs" / key, frames=k)
            rows.append({"entry_id": "baseline/static_guard", "team": "D3 reference", "baseline": True, "event": "ev-m", "seed": k, "rep": rep,
                         "run_id": f"rnd-{key}", "evaluation": {"ranked": True, "ranked_valid": True, "components": {}}})
    rows.append({**rows[0], "run_id": "rnd-missing", "seed": 9})
    rows.append({**rows[0], "run_id": "rnd-dev", "evaluation": {"ranked": False}})      # 非排名运行不处理
    append_rows(bdir, rows)
    dry = R.finalize_board(root, bdir, dry_run=True, rng=random.Random(3), now=T0, policy=P)
    assert dry["unresolved"] == ["rnd-missing"] and not (root / R.EXTERNAL_INDEX).exists()
    res = R.finalize_board(root, bdir, rng=random.Random(3), now=T0, policy=P)
    t = res["totals"]
    assert t["runs"] == 4 and t.get("sampled") == 1 and t.get("compacted") == 3 and t["dup"] == 2           # 每个种子的第 2 次重复去重
    idx = (root / R.EXTERNAL_INDEX).read_text().splitlines()
    assert sorted(idx) == sorted(str((out / "runs" / f"l1-static_guard-k{k:02d}-p{rep}").resolve()) for k in range(2) for rep in (1, 2))
    smp = next(Path(p) for p in idx if R.load_record(Path(p))["ranked_eval"]["phase"] == "sampled")
    assert R.load_record(smp)["ranked_eval"]["submission_id"] == "r-l1:baseline/static_guard"
    R.retention_gc(root, policy=P, now=T0 + 30 * MIN)
    assert not (smp / "raw.mcap").exists() and R.load_record(smp)["ranked_eval"]["phase"] == "compacted"
    assert R.main(["--data-root", str(root), "finalize", "--board", "r-l1", "--out", str(tmp_path / "f.json")]) == 1   # 有未定位的行
    assert json.loads((tmp_path / "f.json").read_text())["totals"]["runs"] == 4


def test_finalize_board_groups_same_baseline_entry_per_track(tmp_path: Path) -> None:
    """同名基线条目出现在两个分轨（B21 v1.1）：按"分轨 × 条目 × 事件"分组，各自抽样，并只在本分轨的重复运行间去重。"""
    from awr.bench.boardstore import append_rows

    root = tmp_path / "d3"
    bdir = root / "boards" / "v9"
    bdir.mkdir(parents=True)
    (bdir / "config.json").write_text(json.dumps({"board_version": "v9", "track": {"dev": False}}))
    rows = []
    for lv, frames in (("l1", 1), ("l2", 2)):
        for rep in (0, 1):
            rid = f"v9-{lv}-track_share-ev-b-k0r{rep}"
            _fake_run(root / "dataset" / "ev-b" / "runs" / rid, frames=frames)
            rows.append({"entry_id": "baseline/track_share", "team": "D3 reference", "baseline": True, "event": "ev-b", "seed": 0, "rep": rep,
                         "run_id": rid, "track": f"std3-{lv}", "evaluation": {"ranked": True, "ranked_valid": True, "components": {}}})
    rows.append({**rows[-1], "run_id": "v9-l2-track_share-ev-b-k1r0", "seed": 1, "rep": 0})              # 待平台原因重跑：本次不处理
    _fake_run(root / "dataset" / "ev-b" / "runs" / "v9-l2-track_share-ev-b-k1r0", frames=3)
    append_rows(bdir, rows)
    res = R.finalize_board(root, bdir, rng=random.Random(0), now=T0, policy=P, exclude={"v9-l2-track_share-ev-b-k1r0"})
    assert res["runs"].pop("v9-l2-track_share-ev-b-k1r0") == {"phase": "excluded"}
    assert (root / "dataset" / "ev-b" / "runs" / "v9-l2-track_share-ev-b-k1r0" / "raw.mcap").exists()
    t = res["totals"]
    assert t["runs"] == 5 and t["excluded"] == 1 and t["sampled"] == 2 and t["compacted"] == 2 and t["dup"] == 2
    for lv in ("l1", "l2"):
        r0, r1 = (res["runs"][f"v9-{lv}-track_share-ev-b-k0r{k}"] for k in (0, 1))
        assert r1["dup_of"] == f"v9-{lv}-track_share-ev-b-k0r0" and r0["dup_of"] is None
        assert {r0["phase"], r1["phase"]} == {"sampled", "compacted"}
    rec = R.load_record(root / "dataset" / "ev-b" / "runs" / "v9-l2-track_share-ev-b-k0r0")
    assert rec["ranked_eval"]["submission_id"] == "v9:std3-l2:baseline/track_share"


def test_finalize_board_legacy_rows_digest_cache_and_jobs(tmp_path: Path, monkeypatch) -> None:
    """旧版榜单配置的提交行（无 run_id）按 `<submission_id>-r<k>` 定位；摘要缓存于 `digest_dir`（进程池预算），再次执行不重读录制。"""
    from awr.bench.boardstore import append_rows

    root = tmp_path / "d3"
    bdir = root / "boards" / "proto-x"
    bdir.mkdir(parents=True)
    (bdir / "config.json").write_text(json.dumps({"board_version": "proto-x", "track": {"dev": False}}))
    rows = []
    for k in range(3):
        _fake_run(root / "dataset" / "ev-x" / "runs" / f"sub-legacy-r{k}", frames=k)
        rows.append({"entry_id": "t/sub-legacy", "team": "t", "baseline": False, "event": "ev-x", "seed": k, "submission_id": "sub-legacy",
                     "evaluation": {"ranked": True, "ranked_valid": True, "components": {}}})
    append_rows(bdir, rows)
    cache = tmp_path / "digests"
    dry = R.finalize_board(root, bdir, dry_run=True, digest_dir=cache, jobs=2, rng=random.Random(0), now=T0, policy=P)
    assert dry["unresolved"] == [] and dry["totals"]["runs"] == 3 and len(list(cache.glob("*.digest"))) == 3
    assert not (root / "dataset" / "ev-x" / "runs" / "sub-legacy-r0" / "retention.json").exists()
    monkeypatch.setattr(R, "frames_digest", lambda d: (_ for _ in ()).throw(AssertionError("digest must come from the cache")))
    res = R.finalize_board(root, bdir, digest_dir=cache, rng=random.Random(0), now=T0, policy=P)
    assert res["totals"]["sampled"] == 1 and res["totals"]["compacted"] == 2 and res["totals"]["errors"] == 0
    for name, x in res["runs"].items():
        assert x["frames_digest_sha256"] == dry["runs"][name]["frames_digest_sha256"]


def test_platform_audit_reference_falls_back_to_run_dir(tmp_path: Path) -> None:
    from awr.bench import platform_audit as PA

    d = _fake_run(tmp_path / "runs" / "base-s1", frames=2)
    (d / "truth" / "fleet.npz").unlink()
    (d / "manifest.json").write_text(json.dumps({"run_id": "base-s1", "event": {"id": "ev"}}))
    want = PA.frames_digest(d)
    (d / "frames.digest").write_bytes(R.frames_digest(d))                              # 精简后的保留摘要（retention 文本格式）
    row = {"entry_id": "baseline/static_guard", "event": "ev", "seed": 1, "run_dir": str(d)}
    got, src = PA.reference_digest(tmp_path / "board", {}, row)
    assert src == "run_dir" and got.streams == want.streams


# ---------------------------------------------------------------------------------------------------------------------------- subworker
def test_lease_lock_heartbeat_and_expiry(tmp_path: Path) -> None:
    from awr.bench.subworker import Lease

    a = Lease(tmp_path, "sub-aaaa", lease_s=0.5, heartbeat_s=0.05)
    assert a.acquire()
    b = Lease(tmp_path, "sub-aaaa")
    assert not b.acquire()                                                              # 文件锁：已由其他进程（描述符）领取
    l0 = json.loads(a.path.read_text())
    time.sleep(0.2)
    l1 = json.loads(a.path.read_text())
    assert l1["schema"] == "drone4d.bench.subworker_lease.v1" and l1["heartbeat_unix_ns"] > l0["heartbeat_unix_ns"]
    doc = {"submission_id": "sub-aaaa", "_meta": {"updated_unix_ns": 0}}
    assert not Lease.expired(tmp_path, doc, now_ns=time.time_ns(), lease_s=0.5)
    a.release()
    assert not a.path.exists() and Lease.expired(tmp_path, doc, now_ns=time.time_ns(), lease_s=0.5)   # 无租约：按文档更新时刻
    assert b.acquire()
    b.release()


class _FakeJob:
    """提交 Run 替身：写运行清单（物理档）与 evaluation.json；记录调用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, rs, *, policy, seed, run_id, run_dir, physics=None, **_kw) -> dict:
        self.calls.append(run_id)
        d = Path(run_dir)
        (d / "agents" / "uav-01").mkdir(parents=True, exist_ok=True)
        (d / "agents" / "uav-01" / "obs.sha256").write_text(f"0 {hashlib.sha256(str(seed).encode()).hexdigest()}\n")
        (d / "manifest.json").write_text(json.dumps({"run_id": run_id, "physics": physics, "n_ticks": 10}))
        _sock(d / "phys" / "sock" / "ctl.sock")
        g = (int(hashlib.sha256(run_id.encode()).hexdigest()[:6], 16) % 100) / 1000.0
        ev = {"components": {"ec": 0.5 + g, "eu": 0.3, "ao": 0.4, "ce": 0.2}, "ranked": True, "ranked_valid": True, "invalid_reasons": [],
              "physics": physics}
        (d / "evaluation.json").write_text(json.dumps(ev))
        return {"run_id": run_id, "run_dir": str(d), "run_wall_s": 1.0, "agents": {"uav-01": {"acts": 5, "exited": "bye", "exit_code": 0}}}


def _legacy_board(root: Path) -> Path:
    from awr.bench.boardstore import append_rows, publish

    b = root / "boards" / "proto-k"
    b.mkdir(parents=True)
    rs = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "k"}, "world": {"id": "w"}, "event": {"id": "ev-k", "version": 1},
          "fleet": [{"uav_id": "uav-01", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [0.0, 0.0, 0.0], "yaw_deg": 0.0}}],
          "policy": {"kind": "reference", "name": "track_share"}, "obs_level": "L1", "physics": "realistic", "duration_s": 20.0, "ranked": True}
    cfg = {"run_spec": rs, "seeds": [11, 12, 13], "events_root": str(root / "events"), "worlds_root": str(root / "worlds"), "physics": "realistic",
           "gz": {}, "track": {"fleet_ref": "1x-x500", "obs_level": "L1", "dev": False}, "track_id": "std1-l1", "lane": "rendezvous_handoff",
           "event_type": "rendezvous_handoff", "event": "ev-k"}
    (b / "config.json").write_text(json.dumps(cfg))
    ev = {"components": {"ec": 0.4, "eu": 0.1, "ao": 0.3, "ce": 0.2}, "ranked": True, "ranked_valid": True, "invalid_reasons": [], "physics": "realistic"}
    append_rows(b, [{"entry_id": "baseline/static_guard", "team": "D3 reference", "display_name": "Static guard", "baseline": True,
                     "track": "std1-l1", "lane": "rendezvous_handoff", "event_type": "rendezvous_handoff", "event": "ev-k", "seed": k,
                     "evaluation": ev} for k in range(3)])
    publish(b, n_boot=200)
    return b


def _evaluating(root: Path, bv: str, *, lanes: list[str] | None = None, track: str = "std1-l1") -> tuple[object, str]:
    from awr.bench.submissions import SubmissionStore

    st = SubmissionStore(root / "submissions")
    req = {"track": track, "image_digest": "sha256:" + "a" * 64, "entry": {"cmd": ["python", "-m", "agent"]}, **({"lanes": lanes} if lanes else {})}
    d = st.create(req, owner="ann", team="team-k", board_version=bv, tracks={track}, lanes=set(lanes or ["rendezvous_handoff"]))
    sid = d["submission_id"]
    st.set_visibility(sid, "board")
    for s in ("SCANNING", "VALIDATING", "EVALUATING"):
        st.transition(sid, s)
    return st, sid


def test_orphaned_legacy_submission_is_recovered(tmp_path: Path) -> None:
    from awr.bench.subworker import Lease, process_one

    root = tmp_path
    _legacy_board(root)
    st, sid = _evaluating(root, "proto-k")
    runs = root / "dataset" / "ev-k" / "runs"
    job = _FakeJob()
    job({}, policy={}, seed=11, run_id=f"{sid}-r0", run_dir=runs / f"{sid}-r0", physics="realistic")   # 已完成且已记账
    st.charge_gpu(sid, 5.0, run_id=f"{sid}-r0")
    (runs / f"{sid}-r1" / "truth").mkdir(parents=True)                                 # 中断的 Run：残留目录
    (runs / f"{sid}-r1" / "truth" / "run_seed.json").write_text("{}")
    job.calls.clear()
    holder = Lease(root, sid)
    assert holder.acquire()                                                             # 原进程仍持有文件锁：不视为孤立
    assert process_one(root, run_job=job, n_boot=200, lease_s=0.0, log=lambda m: None) is None and st.get(sid)["state"] == "EVALUATING"
    holder.release()
    logs: list[str] = []
    assert process_one(root, run_job=job, n_boot=200, lease_s=0.0, log=logs.append) == sid
    d = st.get(sid)
    assert d["state"] == "DONE" and job.calls == [f"{sid}-r1", f"{sid}-r2"]
    assert d["result"]["lanes"][0]["runs"] == {"total": 3, "valid": 3, "invalid": 0, "platform_rerun": 1}
    rec = d["_meta"]["recovered"][0]
    assert rec["reused"] == 1 and rec["interrupted"] == [f"{sid}-r1"] and rec["rerun"] == 2
    assert [h["state"] for h in d["_meta"]["history"]][-2:] == ["SCORING", "DONE"]
    for k in range(3):                                                                  # 套接字已清理；评分后保留已执行
        assert not (runs / f"{sid}-r{k}" / "phys" / "sock").exists()
    ret = json.loads((root / "submissions" / sid / "retention.json").read_text())
    assert ret["totals"] == {"compacted": 2, "sampled": 1} and not (root / "submissions" / sid / "lease.json").exists()
    assert any("run_manifest not written" in m for m in logs)                           # 旧版配置无 evalstore：排名运行不写清单


def test_orphan_with_charged_run_without_outputs_fails_closed(tmp_path: Path) -> None:
    from awr.bench.subworker import recover_orphans

    root = tmp_path
    _legacy_board(root)
    st, sid = _evaluating(root, "proto-k")
    st.charge_gpu(sid, 5.0, run_id=f"{sid}-r0")
    job = _FakeJob()
    assert recover_orphans(root, run_job=job, n_boot=200, lease_s=0.0, log=lambda m: None) == sid
    d = st.get(sid)
    assert d["state"] == "FAILED" and d["reason"]["detail"] == {"why": "orphan_unresolved", "run": f"{sid}-r0"} and job.calls == []


def test_orphaned_v1_submission_is_recovered_with_wall_limit_row(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "tests" / "d3" / "board"))
    from _board_v1 import TEST_EVENTS, seal

    from awr.bench.boardstore import load_rows, publish
    from awr.bench.subworker import _v1_runs, process_one

    root = tmp_path
    bdir, cfg, _store = seal(root)
    starts = [{"kind": "pose", "pos_enu_m": [76.0, y, 0.0], "yaw_deg": 90.0} for y in (-78.0, -82.0, -86.0)]
    smoke = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "smoke"}, "world": {"id": "oryahovo-crop1"},
             "event": {"id": "dev-01", "version": 1}, "fleet": [{"uav_id": f"uav-0{i + 1}", "vehicle_ref": "p600_bench", "start": s}
                                                               for i, s in enumerate(starts)],
             "policy": {"kind": "image"}, "obs_level": "L1", "physics": "realistic", "duration_s": 20.0}
    cfg = {**cfg, "worlds_root": str(root / "worlds"), "events_root": str(root / "events"), "gz": {}, "agent_image": "d3-agent-py:dev",
           "smoke": {"run_spec": smoke}, "events": {e: {"world_id": "oryahovo-crop1", "version": 1, "starts": starts} for e in TEST_EVENTS}}
    (bdir / "config.json").write_text(json.dumps(cfg))
    publish(bdir, n_boot=200)
    st, sid = _evaluating(root, "v1", lanes=["handoff"], track="std3-l2")
    d = st.get(sid)
    runs = _v1_runs(cfg, d, {"kind": "image", "image_digest": d["image_digest"], "entry": d["entry"]})
    assert len(runs) == 2 * 5 * 2
    job = _FakeJob()
    done, wall, cut = runs[0], runs[1], runs[2]
    run_of = {it["run_id"]: root / "dataset" / it["event"] / "runs" / it["run_id"] for it in runs}
    job({}, policy={}, seed=1, run_id=done["run_id"], run_dir=run_of[done["run_id"]], physics="realistic")
    st.charge_gpu(sid, 5.0, run_id=done["run_id"])
    run_of[wall["run_id"]].mkdir(parents=True)                                         # 墙钟超限（已记账，termination.json）
    (run_of[wall["run_id"]] / "termination.json").write_text(json.dumps({"run_id": wall["run_id"], "code": 693, "name": "RUN_WALL_LIMIT_EXCEEDED",
                                                                       "detail": {"why": "wall_limit", "limit_s": 4200.0, "stage": "run"}}))
    st.charge_gpu(sid, 9.0, run_id=wall["run_id"])
    run_of[cut["run_id"]].mkdir(parents=True)                                          # 中断（未记账）
    job.calls.clear()
    assert process_one(root, run_job=job, n_boot=200, lease_s=0.0, log=lambda m: None) == sid
    d = st.get(sid)
    assert d["state"] == "DONE", d.get("reason")
    assert sorted(job.calls) == sorted(it["run_id"] for it in runs[2:])
    rows = [r for r in load_rows(bdir) if r.get("submission_id") == sid]
    assert len(rows) == 20 and [r["run_id"] for r in rows] == [it["run_id"] for it in runs]                # 与 subworker 相同的 Run 次序
    w = next(r for r in rows if r["run_id"] == wall["run_id"])
    assert w["evaluation"]["ranked_valid"] is False and w["termination"]["detail"] == {"why": "wall_limit", "limit_s": 4200.0}
    lane = d["result"]["lanes"][0]
    assert lane["runs"]["platform_rerun"] == 1 and lane["runs"]["invalid"] == 1
    assert d["_meta"]["recovered"][0]["reused"] == 2
    ret = json.loads((root / "submissions" / sid / "retention.json").read_text())
    assert ret["totals"].get("sampled") == 2 and sum(ret["totals"].values()) == 20                          # 每个"提交 × test 事件"抽 1 个
