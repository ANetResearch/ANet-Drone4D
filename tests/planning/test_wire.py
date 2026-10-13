"""规划作业线上编码（M10-FR-202；17 §17.10.2；M10-NFR-202）：`ctl/plan/submit` 的请求体与结果字节往返后逐字节相同，
`result_sha256` 与进程模式一致。"""

from __future__ import annotations

import msgpack
import numpy as np
import pytest
from geo import tiny

from awr.sim.planning.jobs import PlanRequest, PlanResult, packb, query_timeout_s, result_digest, unpackb
from awr.sim.planning.worker import set_world, worker_main


@pytest.mark.parametrize("a", [np.arange(12, dtype=np.float64).reshape(3, 4), np.zeros((0, 3), np.float32),
                               np.array([1, 2, 3], np.int64), np.array([[True, False]]),
                               np.arange(24, dtype=">f8").reshape(2, 3, 4), np.array(1.5),
                               np.asfortranarray(np.arange(6.0).reshape(2, 3))])
def test_ndarray_roundtrip_bytes_dtype_shape(a: np.ndarray) -> None:
    b = unpackb(packb({"a": a, "t": (a, [a])}))
    for x in (b["a"], b["t"][0], b["t"][1][0]):
        assert isinstance(x, np.ndarray) and x.dtype == a.dtype and x.shape == a.shape
        assert np.ascontiguousarray(x).tobytes() == np.ascontiguousarray(a).tobytes()
        assert x.flags.writeable


def test_numpy_scalars_keep_type_and_digest() -> None:
    d = {"f32": np.float32(0.1), "i64": np.int64(-7), "u8": np.uint8(200), "b": np.bool_(True), "f64": np.float64(0.3),
         "f": 0.25, "s": {3, 1, 2}}
    b = unpackb(packb(d))
    assert type(b["f32"]) is np.float32 and b["f32"] == np.float32(0.1)
    assert type(b["i64"]) is np.int64 and type(b["u8"]) is np.uint8 and type(b["b"]) is np.bool_
    assert b["s"] == [1, 2, 3]
    d2 = dict(d, s=[1, 2, 3])
    assert result_digest((), None, b) == result_digest((), None, d2)


def test_object_arrays_refused() -> None:
    with pytest.raises(TypeError):
        packb({"a": np.array([object()], dtype=object)})


def test_request_wire_roundtrip_and_shape() -> None:
    req = PlanRequest("safe_transit:m-1:3", "safe_transit", ("synthcity", "cv1", "ab" * 32), ("p600-01",),
                      {"start": np.array([1.0, 2.0, 3.0]), "goal": [4.0, 5.0, 6.0], "zones": ["border"],
                       "keepouts": [{"id": "tg-3/visual", "shape": "cylinder", "center_enu_m": [0.0, 0.0, 0.0],
                                     "r_m": 50.0, "z_min_m": 0.0, "z_max_m": 120.0}]},
                      {"v_max_mps": 12.0}, 1, 4242, 50, "track:m-1:p600-01")
    msg = req.to_wire()
    assert set(msg) == {"v", "job_id", "kind", "budget_ms", "args"} and msg["v"] == 1
    assert isinstance(msg["args"], bytes)
    raw = msgpack.packb(msg, use_bin_type=True)                           # 总线 `pack` 的同一编码
    back = PlanRequest.from_wire(msgpack.unpackb(raw, raw=False))
    assert back.job_id == req.job_id and back.kind == req.kind and back.world_key == req.world_key
    assert back.vehicle_ids == req.vehicle_ids and back.priority == 1 and back.request_tick == 4242
    assert back.budget_ms == 50 and back.dedupe_key == req.dedupe_key and back.limits == req.limits
    assert np.array_equal(back.payload["start"], req.payload["start"]) and back.payload["keepouts"] == req.payload["keepouts"]


@pytest.mark.parametrize("bad", [{}, {"job_id": "x"}, {"job_id": "x", "kind": "k", "budget_ms": 1, "args": b"\xc1"},
                                 {"job_id": "x", "kind": "k", "budget_ms": 1, "args": packb({"world_key": [1, 2]})},
                                 {"job_id": "x", "kind": "k", "budget_ms": 1, "args": packb({"payload": [1]})}])
def test_request_wire_rejects_malformed(bad: dict) -> None:
    with pytest.raises(ValueError):
        PlanRequest.from_wire(bad)


def test_result_bytes_roundtrip_keeps_digest(tmp_path) -> None:
    """真实转场结果（轨迹含控制点、1 s 采样与折线数组）：编码往返后按同一规则重算的哈希与 worker 给出的相同。"""
    w = tiny(str(tmp_path))
    key = (w.world_id, w.content_version, w.coordinate_sha256)
    set_world(key, w)
    r = worker_main(PlanRequest("t", "safe_transit", key, ("v",), {"start": [-120.0, -20.0, 30.0],
                                                                 "goal": [30.0, -20.0, 60.0]}, {"v_max_mps": 12.0},
                                0, 0, 50, "t"))
    assert r.ok
    b = PlanResult.from_bytes(r.to_bytes())
    assert b.result_sha256 == r.result_sha256 == result_digest(b.trajectories, b.items)
    assert b.status == r.status and b.code == r.code and b.items is None and isinstance(b.trajectories, tuple)
    assert np.array_equal(b.trajectories[0]["ctrl_pts"], r.trajectories[0]["ctrl_pts"])
    g = worker_main(PlanRequest("g", "preview", key, ("a",), {
        "generator": "orbit", "vehicles": [{"vehicle_id": "a", "home_enu_m": [-160.0, -110.0, 0.5]}],
        "params": {"center_enu_m": [-40.0, -20.0], "radius_m": 60.0, "z_m": 130.0, "turns": 1}, "zones": ["border"]},
        {"v_max_mps": 12.0}, 0, 0, 2000, "g"))
    gb = PlanResult.from_bytes(g.to_bytes())
    assert gb.items is not None and gb.result_sha256 == g.result_sha256
    assert result_digest(gb.trajectories, gb.items, {"region": gb.extra.get("region")}) == g.result_sha256


def test_query_timeout_formula() -> None:
    assert query_timeout_s(200) == pytest.approx(5.6) and query_timeout_s(2000) == pytest.approx(11.0)
