"""内部回放磁带演员源（M27-FR-053、M23-FR-049；M27-AC-009、M23-AC-026；D3-AC-19"内部回放的演员由磁带补出且与磁带位置差 ≤ 5 cm；
前端磁带解码器的脚本演员位置与内部回放差 ≤ 5 cm"）。

- Env L1 运行（合成交接事件）写出 `raw.mcap` 后，`TapeActorSource.from_run` 只凭 MCAP 附件补出脚本演员：在录制时间轴（各机
  `/uav/{id}/obs/nav` 的消息时刻）、整段磁带每演员每秒、tick 之间的随机时刻与每次状态切换前后，回放行的位置与原事件目录
  磁带 `actor_states` 之差 ≤ 5 cm（实测为 f32 量化，≤ 4 mm），ENABLED 位与磁带可见状态一致，识别物 id 与运行期相同；
- 行布局：`awr.TargetLite32.v1`，载荷与运行期 `state/sim-core/targets` 同形；t < 0 全部不启用，末帧之后保持末帧；
- 会话偏移 `offset_ns` 与运行期 M18 运动源 `TapeMotionSource` 同义；
- 附件完整性：缺 `tape.bin` 620，磁带被改或与清单哈希不符 614；
- 前端对拍 golden（packages/contracts/golden/event/tape-replay.{bin,json}）：由本源在合成交接事件磁带上生成的回放行；本文件复算后
  与 golden 逐字节一致，前端 `apps/web/tests/m06/tape_replay.test.ts` 以 TS 磁带解码器对比同一 golden（≤ 5 cm）。
  重新生成：`AWR_WRITE_TAPE_REPLAY_GOLDEN=1 PYTHONPATH=python .venv-d3/bin/python -m pytest -q tests/d3/bench/test_replay_actors.py -k golden`。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("mcap")
pytest.importorskip("zstandard")

from awr.bench.recorder.read import REPLAY_TOL_M, TapeActorSource, run_attachments, tape_target_rows
from awr.contracts.layouts import TARGET_LITE32
from awr.sim.events.errors import EventError
from awr.sim.events.tape import TICK_NS, TapeReader

ROOT = Path(__file__).resolve().parents[3]
ENV_TESTS = ROOT / "tests" / "d3" / "env"
GOLDEN = ROOT / "packages" / "contracts" / "golden" / "event"
F32_TOL_M = 0.004                                    # |坐标| < 131 km 时 f32 量化误差上界（3.9 mm）
RUN_S = 30.0


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_conf = _load("d3_env_conftest_for_replay", ENV_TESTS / "conftest.py")
synth_event = _conf.synth_event


@pytest.fixture(scope="module")
def l1_run(synth_event, tmp_path_factory):
    """合成交接事件上的 Env L1 运行（2 机、30 s）：返回 (运行目录, 清单, 事件版本目录)。"""
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.spec import EnvSpec

    t_l2 = _load("d3_env_test_l2_for_replay", ENV_TESTS / "test_l2.py")
    w, ev = synth_event
    out = tmp_path_factory.mktemp("replay") / "run"
    env = BenchEnv(EnvSpec("t-replay", 5, w, ev, t_l2.FLEET, obs_level="L1", physics="kinematic", duration_s=RUN_S, out_dir=out))
    obs, _ = env.reset()
    while env.agents:
        obs, *_ = env.step({u: {"nav": {"kind": "goto", "target_enu": [0.0, 60.0, 30.0], "speed_mps": 4.0}} for u in obs})
    man = env.close()
    return out, man, ev


def _transitions(r: TapeReader) -> list[int]:
    """脚本演员状态或容器切换所在的 tick 时刻（事件时刻 ns）。"""
    f = np.concatenate([r.block_frames(b, background=False) for b in range(len(r.blocks))])
    ch = (f["state"][1:] != f["state"][:-1]) | (f["container"][1:] != f["container"][:-1])
    return sorted({(int(k) + 1) * TICK_NS for k in np.nonzero(ch.any(axis=1))[0]})


def _probe_times(r: TapeReader, extra=(), *, seed: int = 19) -> list[int]:
    """每演员每秒（整段磁带）、tick 之间的随机时刻、状态切换前后与端点。"""
    dur = r.n_ticks * TICK_NS
    ts = set(range(0, dur + 1, 1_000_000_000))
    ts |= {int(t) for t in np.random.default_rng(seed).integers(0, dur, 200)}
    for k in _transitions(r):
        ts |= {k - 1, k, k + 1_000_000, k + 50_000_000, k - 50_000_000}
    ts |= {-1, dur - 1, dur + 3 * TICK_NS, *map(int, extra)}
    return sorted(t for t in ts if t >= -1)


def _nav_times(run: Path) -> list[int]:
    from mcap.reader import make_reader

    with (run / "raw.mcap").open("rb") as f:
        return sorted({m.log_time for _s, c, m in make_reader(f).iter_messages(log_time_order=False) if c.topic.endswith("/obs/nav")})


def _compare(src: TapeActorSource, ref: TapeReader, times, offset_ns: int = 0) -> dict:
    """逐时刻对比回放行与磁带：返回最大位置差、各状态出现次数；ENABLED、类别、航向、速度逐行断言。"""
    ids = np.arange(ref.n_scripted)
    worst, seen = 0.0, {}
    for t in times:
        rows = src.rows(t)
        st = ref.actor_states(offset_ns + t, ids=ids)
        d = np.linalg.norm(rows["pos"].astype(np.float64) - st.pos_enu_m, axis=1)
        worst = max(worst, float(d.max()))
        assert float(d.max()) <= REPLAY_TOL_M, (t, d)
        en = (rows["state"] >> 7) & 1
        assert en.tolist() == np.isin(st.state, (1, 2)).astype(int).tolist(), t
        assert ((rows["state"] >> 4) & 1).all()                                      # timeline 识别物恒为 MOVING
        dy = (rows["heading_rad"].astype(np.float64) - st.yaw_rad + np.pi) % (2 * np.pi) - np.pi
        assert np.abs(dy).max() <= 1e-6, t
        sp = np.hypot(st.vel_enu_mps[:, 0], st.vel_enu_mps[:, 1])
        assert np.abs(rows["speed_cms"] * 0.01 - sp).max() <= 0.005 + 1e-9, t
        for s in st.state.tolist():
            seen[s] = seen.get(s, 0) + 1
    return {"max_m": worst, "states": seen}


# ---------------------------------------------------------------------- Env 运行的内部回放


def test_env_run_replay_actors_match_tape(l1_run) -> None:
    run, man, ev = l1_run
    want = man["event"]["content_sha256"]
    src = TapeActorSource.from_run(run, expect_content_sha256=want)
    assert src.content_sha256 == want == json.loads((ev / "tape.json").read_text())["content_sha256"]
    att = run_attachments(run / "raw.mcap")
    assert att["tape.bin"] == (ev / "tape.bin").read_bytes()
    ref = TapeReader(ev / "tape.bin")                                              # 原事件目录的磁带（不经 MCAP）

    # roster：脚本演员全集，识别物 id 与运行期 M23 装配（EventRuntime.tids）一致
    from awr.sim.events.runtime import target_id

    taken: set[str] = set()
    live = [target_id(a["id"], taken) for a in ref.actors_info(scripted_only=True)]
    ro = src.roster()
    assert [e["id"] for e in ro] == live and [e["agent_no"] for e in ro] == list(range(ref.n_scripted))
    assert [e["label"] for e in ro] == [a["id"] for a in ref.actors[:ref.n_scripted]]
    assert {e["class"]: e["class_code"] for e in ro} == {"person": 1, "vehicle": 5, "machine": 3}

    # 录制时间轴：各机导航观测的消息时刻（即回放播放头逐帧经过的 t_sim_ns）
    nav = _nav_times(run)
    assert len(nav) == int(RUN_S * 10) + 1 and nav[0] == 0 and nav[-1] == int(RUN_S * 1e9)
    rec = _compare(src, ref, nav)
    assert rec["max_m"] <= F32_TOL_M

    # 整段磁带：每演员每秒、tick 之间随机时刻、状态切换前后（覆盖上车与进入建筑）
    full = _compare(src, ref, _probe_times(ref))
    assert full["max_m"] <= F32_TOL_M
    assert {1, 2, 3, 4} <= set(full["states"]), full["states"]                    # 移动、等待、在车内、在建筑内均被抽到


def test_rows_layout_and_payload(l1_run) -> None:
    import msgpack

    _run, _man, ev = l1_run
    ref = TapeReader(ev / "tape.bin")
    src = TapeActorSource(ref)
    n = ref.n_scripted
    t = 12_345_678_900
    rows = src.rows(t)
    assert rows.dtype == TARGET_LITE32 and rows.dtype.itemsize == 32 and len(rows) == n
    pl = msgpack.unpackb(msgpack.packb(src.payload(t)))
    assert pl["v"] == 1 and pl["t_sim_ns"] == t and len(pl["rows"]) == 32 * n
    back = np.frombuffer(pl["rows"], TARGET_LITE32)
    assert back.tobytes() == rows.tobytes()
    assert back["agent_no"].tolist() == list(range(n))
    assert (back["best"] == 0).all() and (back["flags"] == 0).all() and (back["alertness_u8"] == 0).all()
    np.testing.assert_array_equal(tape_target_rows(ref, t), rows)
    # t < 0：第 0 帧位置、全部不启用；末帧之后保持末帧
    neg = src.rows(-1)
    assert (((neg["state"] >> 7) & 1) == 0).all()
    f0 = ref.frame(0, background=False)
    np.testing.assert_array_equal(neg["pos"], (np.stack([f0[k] for k in ("x_mm", "y_mm", "z_mm")], 1) / 1000.0).astype(np.float32))
    end = src.duration_ns
    assert src.rows(end + 10 * TICK_NS).tobytes() == src.rows(end - TICK_NS).tobytes()
    # 进入建筑（状态 4）的 person_c 在建筑内不启用
    i_c = next(a["idx"] for a in ref.actors if a["id"] == "person_c")
    st = [int(ref.actor_states(k, ids=[i_c]).state[0]) for k in range(0, end, TICK_NS)]
    k_in = st.index(4) * TICK_NS
    assert ((src.rows(k_in)["state"][i_c] >> 7) & 1) == 0 and ((src.rows(k_in - TICK_NS)["state"][i_c] >> 7) & 1) == 1


def test_offset_matches_motion_source(l1_run) -> None:
    """会话偏移：回放行与运行期 M18 运动源在同一 (offset, t) 下的位置相同（f32 下逐位），启用状态相同。"""
    from awr.sim.events.runtime import TapeMotionSource

    _run, _man, ev = l1_run
    ref = TapeReader(ev / "tape.bin")
    off = 37_250_000_000
    src = TapeActorSource(ref, offset_ns=off)
    ms = TapeMotionSource(ref, {tid: i for i, tid in enumerate(src.tids)}, off)
    for t in (0, 150_000_000, 2_000_000_001, 51_234_567_890, 99_900_000_000):
        rows = src.rows(t)
        for i, tid in enumerate(src.tids):
            p, yaw, _v, enabled, hidden = ms.pose(tid, t)
            assert rows["pos"][i].tobytes() == p.astype(np.float32).tobytes()
            assert float(rows["heading_rad"][i]) == np.float32(yaw)
            assert bool((rows["state"][i] >> 7) & 1) == enabled and not hidden
    assert _compare(src, ref, [0, 5_000_000_000, 60_050_000_000], offset_ns=off)["max_m"] <= F32_TOL_M


def _write_run(d: Path, tape: bytes, manifest: bytes | None) -> Path:
    from awr.bench.recorder.writer import RunWriter

    d.mkdir(parents=True, exist_ok=True)
    with RunWriter(d / "raw.mcap") as w:
        w.metadata("run", {"run_id": d.name})
        if tape is not None:
            w.attachment("tape.bin", tape, "application/octet-stream")
        if manifest is not None:
            w.attachment("tape.json", manifest)
    return d


def test_attachment_integrity(l1_run, tmp_path: Path) -> None:
    _run, _man, ev = l1_run
    tape, man = (ev / "tape.bin").read_bytes(), (ev / "tape.json").read_bytes()
    ok = TapeActorSource.from_run(_write_run(tmp_path / "ok", tape, man))
    assert ok.content_sha256 == json.loads(man)["content_sha256"]
    with pytest.raises(EventError) as e:
        TapeActorSource.from_run(_write_run(tmp_path / "none", None, man))
    assert e.value.code == 620
    r = TapeReader(ev / "tape.bin")
    bad = bytearray(tape)
    blk = r.blocks[len(r.blocks) // 2]
    bad[blk.off_s + blk.comp_s // 2] ^= 0x5A
    with pytest.raises(EventError) as e:
        TapeActorSource.from_run(_write_run(tmp_path / "bad", bytes(bad), man))
    assert e.value.code == 614
    with pytest.raises(EventError) as e:
        TapeActorSource.from_run(_write_run(tmp_path / "ok2", tape, man), expect_content_sha256="0" * 64)
    assert e.value.code == 614
    # 无清单附件时以磁带自身复算哈希（结构校验仍执行）；verify=False 不复算
    assert TapeActorSource.from_run(_write_run(tmp_path / "nomanifest", tape, None)).content_sha256 == ok.content_sha256
    assert TapeActorSource.from_run(tmp_path / "ok", verify=False).content_sha256 is None


# ---------------------------------------------------------------------- 前端对拍 golden


def _golden_doc(src: TapeActorSource, tape: bytes) -> dict:
    ref = src.reader
    samples = [{"t_sim_ns": t, "rows": base64.b64encode(src.rows(t).tobytes()).decode()} for t in _probe_times(ref)]
    return {"schema": "awr.golden.tape_replay.v1", "file": "tape-replay.bin", "event_id": ref.header["event_id"],
            "content_sha256": src.content_sha256, "file_bytes": len(tape), "offset_ns": src.offset_ns, "tol_m": REPLAY_TOL_M,
            "row_bytes": TARGET_LITE32.itemsize, "n_scripted": ref.n_scripted, "roster": src.roster(), "samples": samples}


def test_replay_golden(request, tmp_path: Path) -> None:
    """golden 的回放行由本源复算后逐字节一致；golden 磁带的回放行与磁带差 ≤ 5 cm。只在重新生成时编译合成事件。"""
    if os.environ.get("AWR_WRITE_TAPE_REPLAY_GOLDEN") == "1":
        _w, ev = request.getfixturevalue("synth_event")
        tape = (ev / "tape.bin").read_bytes()
        src = TapeActorSource.from_run(_write_run(tmp_path / "gen", tape, (ev / "tape.json").read_bytes()))
        GOLDEN.mkdir(parents=True, exist_ok=True)
        (GOLDEN / "tape-replay.bin").write_bytes(tape)
        (GOLDEN / "tape-replay.json").write_text(json.dumps(_golden_doc(src, tape), ensure_ascii=False, separators=(",", ":")) + "\n",
                                                 encoding="utf-8")
    g = json.loads((GOLDEN / "tape-replay.json").read_text(encoding="utf-8"))
    tape = (GOLDEN / "tape-replay.bin").read_bytes()
    assert len(tape) == g["file_bytes"]
    src = TapeActorSource.from_run(_write_run(tmp_path / "golden", tape, json.dumps({"content_sha256": g["content_sha256"]}).encode()))
    assert _golden_doc(src, tape) == g
    assert g["samples"] and len(g["samples"]) >= g["n_scripted"] and g["roster"] == src.roster()
    times = [s["t_sim_ns"] for s in g["samples"]]
    assert _compare(src, TapeReader(GOLDEN / "tape-replay.bin"), times)["max_m"] <= F32_TOL_M
