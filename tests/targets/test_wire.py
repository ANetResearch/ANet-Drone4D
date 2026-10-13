"""线格式（M18-AC-016；M18-FR-018；17 §17.7.3、§17.7.4；契约 rt/layouts.json、rt/payloads/target_detail.schema.json）：

- `state/sim-core/targets`（M08 tap_targets 打包 M18 列）：TargetLite32 行与运行时状态一致（类别码、位置、航向、速度、感知态位、
  判据模式、运动、在体内、声源、启用、警觉度、被发现次数、敏感体数、禁入半径、cfg_rev 模 64）；10 Hz；
- `state/sim-core/target_detail`（2 Hz，全集）：每项通过 `awr.target.detail.v1` schema；首帧与 `cfg_rev` 变化后 ≤ 0.5 s 附 `cfg`
  （完整配置，通过 target.schema.json），之后每 10 s 再附一次；不含配置时单条 ≤ 1 KB（17 §17.7.7）。
"""

from __future__ import annotations

import msgpack
import numpy as np
from ctlib import errors
from simlib import CoreHarness

from awr.contracts import bus_keys
from awr.contracts.layouts import TARGET_LITE32, TARGET_LITE32_BITS
from awr.sim.fleet.stages import registry as R


def _bits(v: int, name: str, field: str) -> int:
    sh, w = TARGET_LITE32_BITS[name][field][:2]
    return (int(v) >> sh) & ((1 << w) - 1)


def test_lite32_and_detail() -> None:
    with R.isolated_registry() as reg:
        from awr.sim.targets import plugin

        rt = plugin.install()
        h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": "sandbox100"})
        try:
            rows_msgs: list[dict] = []
            det_msgs: list[tuple[int, dict]] = []
            h.bus.subscribe(bus_keys.STATE_TARGETS, lambda k, raw: rows_msgs.append(msgpack.unpackb(raw, raw=False)))
            h.bus.subscribe(bus_keys.STATE_TARGET_DETAIL,
                            lambda k, raw: det_msgs.append((len(raw), msgpack.unpackb(raw, raw=False, strict_map_key=False))))
            ids = []
            for t in ({"class": "person", "pos": [40.0, 0.0]}, {"class": "dog", "pos": [-30.0, 20.0], "criterion_mode": "physical"},
                      {"class": "vehicle", "pos": [0.0, 80.0], "motion": {"mode": "path", "path": [[0, 80, None], [200, 80, None]],
                                                                           "loop": "loop"}}):
                adm = h.cmd("target/add", {"target": t}, uav=None)
                assert adm["status"] == "accepted", adm
                ids.append(adm["detail"]["result"]["id"])
            h.takeoff(15.0)
            h.advance(3.0)
            last = rows_msgs[-1]
            rows = np.frombuffer(last["rows"], TARGET_LITE32)
            assert rows.size == 3 and list(rows["agent_no"]) == [0, 1, 2]
            v = rt.view()
            assert np.allclose(rows["pos"], v.pos, atol=2.0)        # 10 Hz 采样之间目标仍在移动
            assert list(rows["class"]) == [1, 4, 5]
            st = [int(x) for x in rows["state"]]
            assert _bits(st[1], "state", "criterion") == 1 and _bits(st[0], "state", "criterion") == 0
            assert _bits(st[0], "state", "enabled") == 1 and _bits(st[2], "state", "moving") == 1
            assert _bits(int(rows["flags"][0]), "flags", "cfg_rev") == 1
            assert rows["n_vol"][0] == 2 and rows["keepout_r_m"][0] > 300.0
            # 10 Hz
            n0, t0 = len(rows_msgs), h.t
            h.advance(2.0)
            assert abs((len(rows_msgs) - n0) / (h.t - t0) - 10.0) <= 0.6
            # detail 2 Hz，全部项通过 schema；首帧带 cfg
            assert det_msgs
            first = det_msgs[0][1]
            assert set(first["items"]) == set(ids)
            for tid in ids:
                assert "cfg" in first["items"][tid]
            for _sz, m in det_msgs[-3:]:
                for item in m["items"].values():
                    assert errors("rt/payloads/target_detail.schema.json", item) == [], item
            for sz, m in det_msgs[2:5]:
                assert all("cfg" not in it for it in m["items"].values())
                assert sz / len(m["items"]) <= 1024   # 17 §17.7.7 的估计（约 1 KB）；PRD NFR-004 的 400 B 见实现报告偏差
            n0, t0 = len(det_msgs), h.t
            h.advance(2.0)
            assert abs((len(det_msgs) - n0) / (h.t - t0) - 2.0) <= 0.6
            # cfg_rev 变化后 ≤ 0.5 s 送达完整配置
            assert h.cmd("target/update", {"id": ids[0], "patch": {"alertness": 0.9}}, uav=None)["status"] == "accepted"
            t_upd = h.t
            h.advance(0.6)
            got = [m for _sz, m in det_msgs if m["t_sim_ns"] * 1e-9 >= t_upd - 1e-9 and "cfg" in m["items"][ids[0]]]
            assert got and got[0]["t_sim_ns"] * 1e-9 - t_upd <= 0.5 + 1e-9
            assert got[0]["items"][ids[0]]["cfg"]["alertness"] == 0.9 and got[0]["items"][ids[0]]["cfg_rev"] == 2
            assert errors("target/target.schema.json", got[0]["items"][ids[0]]["cfg"]) == []
            h.advance(1.0)
            rows = np.frombuffer(rows_msgs[-1]["rows"], TARGET_LITE32)
            assert _bits(int(rows["flags"][0]), "flags", "cfg_rev") == 2
        finally:
            h.close()
