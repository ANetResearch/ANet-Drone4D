"""recorder 进程逻辑（RecorderCore + LocalRing + LocalBus；M12 §6.6；FR-030 至 FR-039；M12-AC-030、AC-031 功能部分、
AC-033、AC-035、AC-036、AC-038、AC-039、AC-064）：

- `rec/start`（总线 ctl/recorder/start）开录、`rec/stop` 关段：段 CLOSED、meta.json 符合契约 schema、status 回复带 perf；
- 剧本 `record = true` 自动开录，ladder 剧本（`record = false`）不录；
- 磁盘守卫：可用空间 < 5 GB 时 rec/start 返回 460，录制中停录并发 `rec.stopped{disk_low}`；
- 标记机：N > 50 时兴趣集 marks 在 250 ms 去抖后生效，1 s 内开始写 Full64，并写 `awr.marks` metadata；
- LOSSY drain：环覆盖（> 32 帧未读）产生 `recorder.gap` 事件与 meta.gaps；
- 环头部 segment 变化换段（R02），生产者纪元变化写谱系（R03）；
- 录制中任意时刻 meta.json 可解析，未关闭段为 OPEN。
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import msgpack
import pytest
import rechelp

from awr.contracts import bus_keys
from awr.recorder.app import RecorderCore
from awr.recorder.config import RecorderCfg
from awr.recorder.mcap_index import SegmentIndex
from awr.runtime.bus import LocalBus

pytestmark = pytest.mark.ext


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000_000

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def rig(tmp_path: Path):
    ns = f"awr/shenzhen/{rechelp.RUN}-{secrets.token_hex(2)}"
    rec_bus = LocalBus.open("recorder", namespace=ns)
    sim_bus = LocalBus.open("sim-core", namespace=ns)
    api_bus = LocalBus.open("api", namespace=ns)
    shm = tmp_path / "shm"
    shm.mkdir()
    persist = tmp_path / "runs" / rechelp.RUN
    persist.mkdir(parents=True)
    sim = rechelp.FakeSimRing(shm / "state.sim-core", sim_bus, n=60)
    clock = Clock()
    made: list[RecorderCore] = []

    def make(**kw) -> RecorderCore:
        core = RecorderCore(bus=rec_bus, ring_path=shm / "state.sim-core", persist_dir=persist, run_id=rechelp.RUN,
                            world_id="shenzhen", cfg=RecorderCfg(flush_wall_s=0.2), mono_ns=clock,
                            ring_cls=type(sim.ring), **kw)
        made.append(core)
        return core

    evs: list[dict] = []
    sub = api_bus.subscribe("evt/recorder/*", lambda k, raw: evs.extend(msgpack.unpackb(raw, raw=False)))

    class R:
        pass

    r = R()
    r.sim, r.api, r.make, r.clock, r.persist, r.evs = sim, api_bus, make, clock, persist, evs

    def run(core: RecorderCore, frames: int, per_step: int = 3) -> None:
        for _ in range(frames // per_step):
            sim.tick(per_step)
            clock.t += 20_000_000
            core.step()

    r.run = run
    yield r
    for c in made:
        c.close()
    sub.close()
    sim.close()
    for b in (rec_bus, sim_bus, api_bus):
        b.close()


def _call(r, core: RecorderCore, op: str) -> dict:
    got: dict = {}
    r.api.call_cb(bus_keys.ctl_recorder(op), {"v": 1, "cid": f"c-{op}", "principal": {}}, lambda rep, err: got.setdefault("r", rep),
                  timeout=2)
    for _ in range(100):
        core.step()
        if "r" in got:
            break
    return got["r"]


def test_manual_start_stop_and_meta(rig) -> None:
    core = rig.make(autostart=False)
    rig.run(core, 30)
    assert core.state == "OFF"
    rep = _call(rig, core, "start")
    assert rep["status"] == "accepted" and rep["state"] == "RECORDING" and rep["segment"] == 0
    rig.run(core, 400)  # 3.2 s 仿真
    meta = json.loads((rig.persist / "meta.json").read_text())
    assert meta["segments"][0]["state"] == "OPEN"
    st = _call(rig, core, "status")
    assert st["state"] == "RECORDING" and "queue_bytes" in st["perf"] and len(st["marked"]) == 0
    rep = _call(rig, core, "stop")
    assert rep["state"] == "OFF"
    meta = json.loads((rig.persist / "meta.json").read_text())
    assert rechelp.schema_errors("rec/meta.schema.json", meta) == []
    seg = meta["segments"][0]
    assert seg["state"] == "CLOSED" and seg["bytes"] > 0 and seg["t_end_ns"] > 3_000_000_000
    ix = SegmentIndex.build(rig.persist / "rec-000.mcap")
    assert ix.segment_end()["reason"] == "stop"
    blk = ix.ch("/swarm/uav/state_block")
    assert ix.count(blk) >= 75 and ix.ch("/sim/roster") is not None
    ix.close()
    core.events.flush()
    kinds = [e["kind"] for e in rig.evs]
    assert "rec.started" in kinds and "rec.stopped" in kinds


def test_autostart_scenario_and_ladder(rig) -> None:
    ladder = rig.make(scenario={"record": False, "vehicles": []})
    rig.run(ladder, 60)
    assert ladder.state == "OFF"
    ladder.close()
    core = rig.make(scenario={"record": True, "vehicles": [{"vehicle_id": "sim-0003", "marked": True}]})
    rig.run(core, 60)
    assert core.state == "RECORDING"
    assert 3 in core.marked and len(core.marked) == 1


def test_disk_guard(rig, monkeypatch) -> None:
    core = rig.make(autostart=False)
    rig.run(core, 10)
    monkeypatch.setenv("AWR_DISK_FREE_OVERRIDE_GB", "4")
    assert _call(rig, core, "start")["code"] == 460
    monkeypatch.setenv("AWR_DISK_FREE_OVERRIDE_GB", "100")
    assert _call(rig, core, "start")["code"] == 0
    rig.run(core, 60)
    monkeypatch.setenv("AWR_DISK_FREE_OVERRIDE_GB", "4")
    core._next_disk = 0
    rig.run(core, 6)
    assert core.state == "OFF"
    core.events.flush()
    stops = [e for e in rig.evs if e["kind"] == "rec.stopped"]
    assert stops and stops[-1]["data"]["reason"] == "disk_low" and stops[-1]["severity"] == 2
    seg = json.loads((rig.persist / "meta.json").read_text())["segments"][0]
    assert seg["state"] == "CLOSED"
    assert not list(rig.persist.glob("*.tmp"))


def test_interest_marks_start_full64(rig) -> None:
    core = rig.make(autostart=False)
    rig.run(core, 10)
    _call(rig, core, "start")
    rig.run(core, 30)
    assert core.marked == frozenset()
    pub = rig.api.publisher(bus_keys.CTL_INTEREST)
    pub.put(msgpack.packb({"v": 1, "seq": 1, "detail": [5], "marks": [5, 7], "topics": []}))
    rig.run(core, 15)  # 去抖 250 ms 之前
    assert core.marked == frozenset()
    rig.run(core, 60)
    assert core.marked == frozenset({5, 7})
    rig.run(core, 120)
    _call(rig, core, "stop")
    ix = SegmentIndex.build(rig.persist / "rec-000.mcap")
    full5 = ix.ch("/uav/sim-0005/state")
    assert full5 is not None and ix.count(full5) >= 100
    marks = [m for n, m in ix.metadata if n == "awr.marks"]
    assert marks[-1]["marked"] == "5,7"
    ix.close()


def test_overrun_gap_rotation_and_lineage(rig) -> None:
    core = rig.make(autostart=False)
    rig.run(core, 10)
    _call(rig, core, "start")
    rig.run(core, 60)
    rig.sim.tick(80)  # 未 drain 的 80 帧 > K = 32：LOSSY 覆盖
    rig.clock.t += 20_000_000
    core.step()
    core.events.flush()
    gaps = [e for e in rig.evs if e["kind"] == "recorder.gap"]
    assert gaps and gaps[0]["data"]["overrun"] >= 40
    # checkpoint 恢复：生产者纪元 + 1、时间回退
    rig.sim.ring.set_epoch(2)
    rig.sim.t -= 400_000_000
    rig.run(core, 60)
    # 剧本重置：segment + 1
    rig.sim.ring.set_segment(1)
    rig.sim.ring.set_epoch(3)
    rig.sim.t = 0
    rig.run(core, 60)
    assert core.seg_k == 1
    assert core.writer.drain(5)  # 上一段在 writer 线程 finish 后才置 CLOSED
    meta = json.loads((rig.persist / "meta.json").read_text())
    assert [s["state"] for s in meta["segments"]] == ["CLOSED", "OPEN"]
    assert meta["gaps"] and meta["gaps"][0]["overrun"] >= 40
    assert meta["segments"][0]["lineage"][-1]["epoch"] == 2
    _call(rig, core, "stop")
    ix = SegmentIndex.build(rig.persist / "rec-000.mcap")
    assert ix.segment_end()["reason"] == "scenario_reset" and len(ix.lineage()) == 1
    ix.close()
    assert rechelp.schema_errors("rec/meta.schema.json", json.loads((rig.persist / "meta.json").read_text())) == []


def test_meta_parseable_while_recording(rig) -> None:
    core = rig.make(autostart=False)
    rig.run(core, 10)
    _call(rig, core, "start")
    for k in range(20):
        rig.run(core, 12)
        core._next_meta = 0
        m = json.loads((rig.persist / "meta.json").read_text())
        assert m["segments"][0]["state"] == "OPEN"
        assert k < 2 or m["segments"][0]["t_end_ns"] is not None
