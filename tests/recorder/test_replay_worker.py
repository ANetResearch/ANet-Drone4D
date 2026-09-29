"""replay-worker（M12 §6.7、§7.4；FR-041 至 FR-051；M12-AC-041、AC-045、AC-049、AC-050、AC-051 的功能部分）：

- open：兼容录制打开并定位段首（gen = 1，回放环 flags REPLAY，复合帧 + backfill）；world / content_version 不一致 122，
  段文件不存在 305，CORRUPT 段 461；进行中的段（无 summary）可回放到最后一个完整 chunk；
- seek：复合帧的 Lite32 行与 Full64 行与录制消息逐字节一致（G6a），块时刻 t_b ≤ t 且相差 ≤ 一个块间隔，gen + 1，回复发出
  之后才写环头部 segment；越界 110；
- play / pause / speed：回放时钟按倍率推进，事件以 producer = replay、epoch = gen 发布且保留原字段；低频 env、state_ext
  发往 `state/replay/*`；倍速越界 110；到已关闭段段尾 ENDED，seek 后回到 PAUSED；
- query：事件分页取回全部事件，条数与 `.evx` 记录数一致、顺序按时刻；`env_at` 取不晚于 t 的关键帧；
- close 后回放环删除，空闲超过 idle_exit_s 后宿主 `alive()` 为 False。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import msgpack
import numpy as np
import pytest
import rechelp

from awr.contracts import bus_keys
from awr.contracts.layouts import DRONE_STATE64
from awr.recorder.formats import BLOCK_HDR, EVX_REC, EVX_SUPERSEDED, split_prefix
from awr.runtime.statering import RING_FLAG_REPLAY, SLOT_REPLAY

pytestmark = pytest.mark.ext


@pytest.fixture
def hs(std_run: dict, runs_dir: Path, tmp_path: Path):
    h, bus = rechelp.host(runs_dir, tmp_path / "shm", idle_exit_s=0.3)
    yield h, bus
    h.close()
    bus.close()


def _recorded(std_run: dict) -> dict[str, list[tuple[int, bytes]]]:
    out: dict[str, list[tuple[int, bytes]]] = {}
    for t, lt, d in rechelp.messages(Path(std_run["dir"]) / "rec-000.mcap"):
        out.setdefault(t, []).append((lt, bytes(split_prefix(d)[3])))
    return out


def test_open_and_compatibility(std_run: dict, runs_dir: Path, tmp_path: Path, hs) -> None:
    h, _bus = hs
    rep = h.handle("open", {"cid": "c1", "run": rechelp.RUN, "segment": 0})
    assert rep["status"] == "accepted" and rep["code"] == 0, rep
    assert (rep["data_start_ns"], rep["data_end_ns"]) == (0, 30_000_000_000)
    assert rep["speed_max"] == 20.0 and rep["gen"] == 1 and rep["decimation_s"] is None
    assert rep["lineage"] == [{"epoch": 1, "t_from_ns": 0, "t_to_ns": 30_000_000_000}]
    bf = rep["backfill"]
    assert bf["env"] and bf["roster"] and msgpack.unpackb(bf["roster"])["entries"][0]["id"] == "sim-0000"
    assert h.ring.header().flags & RING_FLAG_REPLAY
    f = h.ring.read_latest(0)
    assert f.flags & SLOT_REPLAY and f.t_sim_ns == 0 and f.n_rows == 60 and len(f.full) == 2 * 64
    assert h.ring.header().clock_state == 2 | 0x80
    assert h.handle("roster", {})["entries"][0]["producer"] == "replay"
    h.handle("close", {})
    # 不兼容：世界不同、content_version 不同
    h2, b2 = rechelp.host(runs_dir, tmp_path / "shm2", world_id="suzhou")
    assert h2.handle("open", {"run": rechelp.RUN, "segment": 0})["code"] == 122
    h2.close()
    b2.close()
    h3, b3 = rechelp.host(runs_dir, tmp_path / "shm3", content_version="abc123")
    assert h3.handle("open", {"run": rechelp.RUN, "segment": 0})["code"] == 122
    assert h3.handle("open", {"run": rechelp.RUN, "segment": 7})["code"] == 305
    h3.close()
    b3.close()


def test_corrupt_segment_rejected(std_run: dict, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    d = runs / rechelp.RUN
    shutil.copytree(std_run["dir"], d)
    m = json.loads((d / "meta.json").read_text())
    m["segments"][0]["state"] = "CORRUPT"
    (d / "meta.json").write_text(json.dumps(m))
    h, bus = rechelp.host(runs, tmp_path / "shm")
    assert h.handle("open", {"run": rechelp.RUN, "segment": 0})["code"] == 461
    h.close()
    bus.close()


def test_seek_is_byte_exact(std_run: dict, hs) -> None:
    h, _ = hs
    h.handle("open", {"run": rechelp.RUN, "segment": 0})
    h.ring.set_segment(1)
    rec = _recorded(std_run)
    blocks = dict(rec["/swarm/uav/state_block"])
    full_at: dict[int, list[bytes]] = {}
    for k in ("/uav/sim-0001/state", "/uav/sim-0002/state"):
        for t, d in rec[k]:
            full_at.setdefault(t, []).append(d)
    rng = np.random.default_rng(3)
    last_seq = 0
    for i, t in enumerate(sorted(rng.integers(0, 30_000_000_000, 20).tolist())):
        rep = h.handle("seek", {"cid": f"s{i}", "t_ns": int(t)})
        assert rep["status"] == "accepted" and rep["gen"] == i + 2
        tb = rep["t_sample_ns"]
        assert tb <= t and t - tb < 40_000_000
        f = h.ring.read_latest(last_seq)
        last_seq = f.frame_seq
        assert f.t_sim_ns == tb and f.flags & SLOT_REPLAY
        blk = blocks[tb]
        n = BLOCK_HDR.unpack_from(blk, 0)[0]
        assert f.lite == blk[16:16 + n * 32]  # Lite32 与录制逐字节一致
        assert sorted(np.frombuffer(f.full, DRONE_STATE64)["agent_no"].tolist()) == [1, 2]
        rows = sorted(f.full[k * 64:(k + 1) * 64] for k in range(len(f.full) // 64))
        assert rows == sorted(full_at[tb])  # Full64 与录制逐字节一致
        assert h.ring.header().segment == i + 1 and h.pending_segment == rep["gen"]  # 回复之前不写新 gen
        h.ring.set_segment(h.pending_segment)  # 宿主在回复发出之后写入（drain_ctl）
        h.pending_segment = None
        assert rep["worker_ms"] is not None
    assert h.handle("seek", {"t_ns": 31_000_000_000})["code"] == 110


def test_segment_written_after_reply(std_run: dict, hs) -> None:
    """经总线服务：回复发出之后宿主才把 gen 写入环头部 segment。"""
    h, bus = hs
    from awr.runtime.bus import LocalBus

    cli = LocalBus.open("api", namespace=bus.namespace)
    got = {}

    def done(rep, err) -> None:
        got["rep"] = rep
        got["seg_at_reply"] = h.ring.header().segment if h.ring is not None else None

    cli.call_cb(bus_keys.ctl_replay_worker("open"), {"v": 1, "cid": "o", "run": rechelp.RUN, "segment": 0}, done, timeout=5)
    for _ in range(200):
        h.step()
        if "rep" in got:
            break
        time.sleep(0.005)
    assert got["rep"]["gen"] == 1 and got["seg_at_reply"] in (0, None)
    h.step()
    assert h.ring.header().segment == 1
    cli.close()


def _warm(h, until_ns: int, timeout_s: float = 5.0) -> None:
    """等预读线程解压到 until_ns（测试以假墙钟快速步进，真实运行中预读窗口领先 max(2 s, 1.5 s × rate)）。"""
    h.src.readahead.restart(h.src.t_play, 20.0)
    end = time.monotonic() + timeout_s
    while h.src.readahead.ready_until() < until_ns and time.monotonic() < end:
        time.sleep(0.01)
    h.src.readahead.restart(h.src.t_play, h.src.rate)


def test_play_events_lowfreq_and_end(std_run: dict, hs) -> None:
    h, bus = hs
    from awr.runtime.bus import LocalBus

    cli = LocalBus.open("api", namespace=bus.namespace)
    evs: list[dict] = []
    env: list[bytes] = []
    ext: list[bytes] = []
    subs = [cli.subscribe("evt/replay/*", lambda k, raw: evs.extend(msgpack.unpackb(raw, raw=False))),
            cli.subscribe(bus_keys.state_ext("replay"), lambda k, raw: ext.append(raw)),
            cli.subscribe("state/replay/env", lambda k, raw: env.append(raw))]
    h.handle("open", {"run": rechelp.RUN, "segment": 0})
    h.handle("seek", {"t_ns": 5_000_000_000})
    assert h.handle("speed", {"speed": 25})["code"] == 110
    assert h.handle("speed", {"speed": 10})["speed"] == 10
    assert h.handle("play", {})["state"] == "playing"
    _warm(h, 30_000_000_000)
    now = time.monotonic_ns()
    h.src.last_ns = now
    for _ in range(250):  # 1 s 墙钟 @ ×10 → 10 s 仿真
        now += 4_000_000
        h.src.step(now)
        h.events.flush()
    time.sleep(0.05)
    assert abs(h.src.t_play - 15_000_000_000) <= 50_000_000
    assert h.ring.header().clock_state == 1 | 0x80 and h.ring.header().rate_milli == 10_000
    kinds = {e["kind"] for e in evs}
    assert "mission.item_reached" in kinds and "safety.geofence" in kinds
    assert all(e["producer"] == "replay" and e["epoch"] == h.src.gen for e in evs)
    rec_ev = [msgpack.unpackb(d, raw=False) for _t, d in _recorded(std_run)["/event"]]
    by_t = {(e["t_sim_ns"], e["kind"]): e for e in rec_ev}
    for e in evs:
        o = by_t[(e["t_sim_ns"], e["kind"])]
        assert {k: e[k] for k in ("kind", "severity", "t_sim_ns", "t_wall_ns", "uav", "cid", "data")} == \
               {k: o[k] for k in ("kind", "severity", "t_sim_ns", "t_wall_ns", "uav", "cid", "data")}
    assert min(e["t_sim_ns"] for e in evs) > 5_000_000_000 and max(e["t_sim_ns"] for e in evs) <= 15_000_000_000
    assert env and ext
    assert h.handle("pause", {})["state"] == "paused"
    h.handle("speed", {"speed": 20})
    h.handle("play", {})
    for _ in range(500):
        now += 4_000_000
        h.src.step(now)
    assert h.src.state == "ended" and h.ring.header().clock_state == 5 | 0x80
    h.handle("seek", {"t_ns": 1_000_000_000})
    assert h.src.state == "paused"
    for s in subs:
        s.close()
    cli.close()


def test_query_events_and_env(std_run: dict, hs) -> None:
    h, _ = hs
    h.handle("open", {"run": rechelp.RUN, "segment": 0})
    items: list[dict] = []
    cur = None
    q = {"kind": "events", "from_ns": 0, "to_ns": 30_000_000_000, "limit": 37}
    for _ in range(100):
        rep = h.handle("query", {"query": dict(q, cursor=cur) if cur is not None else q})
        items += rep["items"]
        cur = rep["next_cursor"]
        if cur is None:
            break
    evx = (Path(std_run["dir"]) / "rec-000.evx").read_bytes()
    n = (len(evx) - 32) // 16
    valid = [EVX_REC.unpack_from(evx, 32 + 16 * i) for i in range(n)]
    valid = [r for r in valid if not r[3] & EVX_SUPERSEDED]
    assert len(items) == len(valid)
    assert [e["t_sim_ns"] for e in items] == sorted(e["t_sim_ns"] for e in items)
    assert sorted(e["mseq"] for e in items) == sorted(r[1] for r in valid)
    crit = h.handle("query", {"query": {"kind": "events", "from_ns": 0, "to_ns": 30_000_000_000, "level_min": 3}})["items"]
    assert [e["t_sim_ns"] for e in crit] == [7_000_000_000, 14_000_000_000, 21_000_000_000, 28_000_000_000]
    last = h.handle("query", {"query": {"kind": "events", "to_ns": 10_000_000_000, "limit": 5}})["items"]
    assert len(last) == 5 and last[-1]["t_sim_ns"] <= 10_000_000_000
    kf = h.handle("query", {"query": {"kind": "env_at", "t_ns": 12_300_000_000}})["keyframe"]
    assert msgpack.unpackb(kf)["t_ns"] == 12_000_000_000


def test_close_and_idle_exit(std_run: dict, hs) -> None:
    h, _ = hs
    h.handle("open", {"run": rechelp.RUN, "segment": 0})
    path = h.ring_path
    assert h.alive()
    h.handle("close", {})
    assert h.ring is None and h.src.state == "idle"
    assert h.handle("seek", {"t_ns": 0})["code"] == 463
    from awr.runtime.statering import LocalRing, RingNotReady

    with pytest.raises(RingNotReady):
        LocalRing.attach(path, expect_layout_id=0)
    time.sleep(0.35)
    assert not h.alive()


def test_open_segment_replays_to_last_chunk(std_run: dict, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    d = runs / rechelp.RUN
    shutil.copytree(std_run["dir"], d)
    p = d / "rec-000.mcap"
    size = p.stat().st_size
    with open(p, "r+b") as f:
        f.truncate(size * 2 // 3)
    m = json.loads((d / "meta.json").read_text())
    m["segments"][0]["state"] = "OPEN"
    (d / "meta.json").write_text(json.dumps(m))
    h, bus = rechelp.host(runs, tmp_path / "shm")
    rep = h.handle("open", {"run": rechelp.RUN, "segment": 0})
    assert rep["status"] == "accepted", rep
    assert 10_000_000_000 < rep["data_end_ns"] < 30_000_000_000
    assert not h.src.seg_closed
    h.handle("seek", {"t_ns": rep["data_end_ns"] - 1_000_000_000})
    h.handle("speed", {"speed": 5})
    h.handle("play", {})
    _warm(h, rep["data_end_ns"])
    now = time.monotonic_ns()
    h.src.last_ns = now
    for _ in range(500):
        now += 4_000_000
        h.src.step(now)
    assert h.src.state in ("playing", "buffering") and h.src.t_play == rep["data_end_ns"]  # W09：OPEN 段保持
    h.close()
    bus.close()
