"""M20-FR-075、M20-AC-032；D2-AC-45（驻留恢复后任务与接力状态一致）：checkpoint 扩展段 `x.tasking`。

- 编码与解码逐字节往返（机巢、任务、工作项、架次、接力控制器与统计）；
- sim-core checkpoint（M08 `capture` / `restore`）带 `x.tasking` 段：在接力运行中捕获，继续运行后恢复到捕获时刻，任务状态、在岗机、
  监控时长与工作项回到捕获时的值，恢复后接力继续（重新下发所需等级、继续统计与交接）；
- 段内只有结构化标记（无 pickle），清单外的对象拒绝编码。
"""

from __future__ import annotations

import copy

import msgpack
import numpy as np
import pytest
from m20core import Core
from test_relay import FakePerception, _target

from awr.sim.runtime.ckpt import capture, capture_ext, restore, restore_ext
from awr.sim.tasking import ckpt as CK
from awr.sim.tasking.runtime import TaskingRuntime


@pytest.fixture
def core(monkeypatch):
    c = Core()
    fp = FakePerception(c)
    monkeypatch.setattr(c.rt, "perception_rt", lambda: fp)
    c.fp = fp
    yield c
    c.close()


def _view(rt: TaskingRuntime) -> dict:
    out = {}
    for tid, T in sorted(rt.tasks.items()):
        r = T.relay
        out[tid] = {"state": T.state, "type": T.type, "items": sorted((w, it.state, it.uav) for w, it in T.items.items()),
                    "relay": None if r is None else (r.state, r.on_station, r.relief, round(r.t_monitor, 3), r.handovers, r.first_capture,
                                                     r.station)}
    out["sorties"] = sorted((u, so.state, so.tid, so.wid, so.cid) for u, so in rt.sorties.items())
    out["nests"] = {nid: sorted((u, a.state) for u, a in n.air.items()) for nid, n in rt.nests.items()}
    return out


def _relay_running(core: Core) -> tuple[str, object]:
    core.nest(n=3)
    tgt = _target(core)
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    r = core.rt.tasks[tid].relay
    assert core.until(lambda: r.first_capture is not None, 400.0, 2.0)
    core.advance(10.0)
    return tid, r


def test_encode_decode_roundtrip(core: Core) -> None:
    tid, r = _relay_running(core)
    rt = core.rt
    blob = CK.encode(rt)
    st = msgpack.unpackb(blob, raw=False, strict_map_key=False)
    assert st["schema"] == CK.SCHEMA
    rt2 = TaskingRuntime()
    rt2.ctx, rt2.S = rt.ctx, rt.S
    CK.decode_into(rt2, blob)
    assert _view(rt2) == _view(rt)
    # 解码时清除在途作业的挂起标记与周期计时（站位作业、预报与天气核对、变焦刷新），其余逐字节往返（含编号序列与调用号映射）
    blob2 = CK.encode(rt2)
    rt3 = TaskingRuntime()
    rt3.ctx, rt3.S = rt.ctx, rt.S
    CK.decode_into(rt3, blob2)
    assert CK.encode(rt3) == blob2
    a, b = msgpack.unpackb(blob, raw=False, strict_map_key=False), msgpack.unpackb(blob2, raw=False, strict_map_key=False)
    ra, rb = a["tasks"][tid]["f"]["relay"]["f"], b["tasks"][tid]["f"]["relay"]["f"]
    assert {k for k in ra if ra[k] != rb[k]} <= {"t_station", "job_t", "t_zoom", "next_forecast", "next_weather"}
    T2 = rt2.tasks[tid]
    assert T2.relay.group == r.group and isinstance(T2.relay.group, tuple)
    assert rt2.relays.stats_wire(T2) == rt.relays.stats_wire(rt.tasks[tid])
    # 编号序列继续：新任务号不与恢复的任务重复
    assert rt2.new_tid() not in rt.tasks


def test_unknown_object_rejected() -> None:
    rt = TaskingRuntime()
    rt.ext_plans = {"x": object()}
    with pytest.raises(TypeError):
        CK.encode(rt)


def test_sim_checkpoint_restores_tasking(core: Core) -> None:
    tid, r = _relay_running(core)
    rt = core.rt
    simc = core.h.core
    assert "x.tasking" in capture_ext(simc)
    arrays, meta = capture(simc)
    arrays = {k: np.array(v, copy=True) for k, v in arrays.items()}
    meta = copy.deepcopy(meta)
    assert "x.tasking" in meta["ext"]
    before = _view(rt)
    t_mon = r.t_monitor
    core.advance(30.0)
    assert rt.tasks[tid].relay.t_monitor > t_mon + 20.0
    # 模拟会话冷启动后的恢复：编排运行时被替换为全新的任务表（驻留恢复时 sim-core 按会话配置重新装配，任务回到 PLANNING）
    rt.tasks.clear()
    rt.sorties.clear()
    restore(simc, arrays, meta)
    assert "tasking" in simc.restored_ext
    assert _view(rt) == before
    T = rt.tasks[tid]
    assert T.state == "RUNNING" and T.relay.t_monitor == pytest.approx(t_mon, abs=1e-6)
    assert rt.reapply_after_restore
    a = T.relay.on_station
    core.fp.req.clear()
    core.advance(5.0)
    assert not rt.reapply_after_restore and core.fp.req.get(a) == "R"     # 所需等级重新下发
    assert T.relay.t_monitor > t_mon + 3.0 and T.relay.G                   # 恢复后继续监控与捕获
    rt.set_soc(a, 0.45)
    assert core.until(lambda: T.relay.handovers == 1, 600.0, 2.0)          # 恢复后交接照常


def test_restore_ext_ignores_unknown_and_missing_segments(core: Core) -> None:
    simc = core.h.core
    assert restore_ext(simc, {"x.unknown": b"\x80"}) == []
    done = restore_ext(simc, {"x.tasking": CK.encode(core.rt)})
    assert done == ["tasking"]


def test_decode_fills_defaults_for_fields_missing_in_older_blobs() -> None:
    """旧版本 checkpoint 中没有的数据类字段（如后加的 `SortieRT.yaw_goal`）恢复时取缺省值。"""
    import msgpack

    from awr.sim.tasking import ckpt as CK
    from awr.sim.tasking.book import SortieRT

    so = SortieRT("p600-01", 0, "nest-a", "s1")
    enc = CK._enc(so)
    del enc["f"]["yaw_goal"], enc["f"]["actions"]
    back = CK._dec(msgpack.unpackb(msgpack.packb(enc, use_bin_type=True), raw=False))
    assert back.yaw_goal is None and back.actions == [] and back.uav == "p600-01"
