"""识别物实体层（M08-AC-208；M08-FR-216、FR-217；ADR-098；17 §17.7.2 第 4 条、§17.7.3）。

- 实体块按冻结字段表登记（`tools/fake/targets_stub.py` 的 M18、M19、M20 列，M08 保留 active、target_no、roster_flags）；
  一字段一写者：重复字段或 M08 保留字段被他人登记即失败；
- 加 30 个识别物后第 31 个返回 505（实体层与经命令处理函数两条路径）；target_no 单调、不复用；
- roster 出现 `kind = target` 条目（agent_no 在 kind 内从 0 计，含 model 与 label），`roster_version` 每次增删 + 1；
- `state/sim-core/targets` 10 Hz ± 5%（d1_250 与 sandbox100），`rows` 为 32·n 字节、按 target_no 升序，与冻结布局的参考打包
  （`FakeTargets.lite_rows`）逐字节相同；`target/remove` 后下一帧不含该行；
- checkpoint：实体数组与条目元数据捕获后恢复逐字节一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

import msgpack
import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts import bus_keys
from awr.contracts.layouts import TARGET_LITE32
from awr.contracts.reasons import Reason
from awr.sim.core.entities import EntityLimitError
from awr.sim.fleet.stages import registry as R
from awr.sim.fleet.stages.tap_targets import pack_rows

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "fake"))
import targets_stub as TS  # noqa: E402


def _register_blocks() -> None:
    R.register_entity_block("target", "targets", "M18", TS.M18_FIELDS)
    R.register_entity_block("target", "perception", "M19", TS.M19_FIELDS)
    R.register_entity_block("target", "relay", "M20", TS.M20_FIELDS)


def _handlers() -> None:
    """M18 `target/add`、`target/remove` 的最小替身：只经实体层增删（运动与判据不在本测试范围）。"""

    def add(msg, apply_tick, ctx):
        t = (msg.get("args") or {}).get("target") or {}
        try:
            m = ctx.entities.add("target", model=str(t.get("class", "object")), label=t.get("label"))
        except EntityLimitError as e:
            return {"status": "rejected", "code": e.code, "detail": {"max": e.limit}}
        tab = ctx.entities["target"]
        tab.cols["tgt_pos"][m.row] = t.get("pos") or (0.0, 0.0, 0.0)
        tab.cols["tgt_enabled"][m.row] = 1
        return {"status": "accepted", "result": {"id": m.id, "agent_no": m.target_no}}

    def remove(msg, apply_tick, ctx):
        tid = (msg.get("args") or {}).get("id")
        if ctx.entities.remove("target", str(tid)) is None:
            return {"status": "rejected", "code": int(Reason.TARGET_NOT_FOUND)}
        return {"status": "accepted"}

    R.register_command_handler("target/add", add, owner="M18")
    R.register_command_handler("target/remove", remove, owner="M18")


def test_entity_block_one_writer() -> None:
    with R.isolated_registry() as reg:
        _register_blocks()
        assert set(reg.entity_blocks["target"]) == {"targets", "perception", "relay"}
        with pytest.raises(ValueError):
            R.register_entity_block("target", "dup", "M19", {"tgt_pos": ("<f8", (3,))})
        with pytest.raises(ValueError):
            R.register_entity_block("target", "bad", "M18", {"target_no": ("<u2", ())})
        with pytest.raises(ValueError):
            R.register_entity_block("vehicle", "x", "M18", {"a": ("u1", ())})


@pytest.mark.parametrize("profile", ["d1_250", "sandbox100"])
def test_targets_roster_publish_limit(profile: str) -> None:
    with R.isolated_registry() as reg:
        _register_blocks()
        _handlers()
        h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": profile})
        try:
            core = h.core
            assert core.fleet.pipeline.spec("tap_targets") is not None
            got: list[dict] = []
            h.bus.subscribe(bus_keys.STATE_TARGETS, lambda k, raw: got.append(msgpack.unpackb(raw, raw=False)))
            rv0 = core.roster.roster_version
            ids = []
            for k in range(30):
                adm = h.cmd("target/add", {"target": {"class": ("person", "dog", "vehicle")[k % 3],
                                                      "pos": [10.0 * k, 5.0, 0.0]}}, uav=None)
                assert adm["status"] == "accepted", adm
                ids.append(adm["detail"]["result"]["id"])
            assert core.roster.roster_version == rv0 + 30
            adm = h.cmd("target/add", {"target": {"class": "person", "pos": [0.0, 0.0, 0.0]}}, uav=None)
            assert adm["status"] == "rejected" and adm["code"] == int(Reason.SANDBOX_ENTITY_LIMIT)
            with pytest.raises(EntityLimitError) as ei:
                core.entities.add("target", model="person")
            assert ei.value.code == 505
            snap = core.roster.snapshot()
            tg = [e for e in snap["entries"] if e["kind"] == "target"]
            assert [e["agent_no"] for e in tg] == list(range(30))
            assert tg[0] == {"agent_no": 0, "id": "person-01", "kind": "target", "model": "person", "label": "person-01"}
            assert msgpack.unpackb(core.roster.snapshot_packed(), raw=False) == snap
            # 10 Hz【仿真】：2 s 内约 20 帧，rows 32·n 字节、按 target_no 升序
            got.clear()
            t0 = core.clock.t_ns
            h.advance(2.0)
            dt = (core.clock.t_ns - t0) * 1e-9
            assert abs(len(got) / dt - 10.0) <= 0.5, (len(got), dt)
            last = got[-1]
            assert last["v"] == 1 and last["roster_version"] == core.roster.roster_version
            rows = np.frombuffer(last["rows"], TARGET_LITE32)
            assert rows.size == 30 and list(rows["agent_no"]) == list(range(30))
            assert np.allclose(rows["pos"][:, 0], [10.0 * k for k in range(30)])
            # 删除：下一帧不含该行，target_no 不复用
            assert h.cmd("target/remove", {"id": ids[3]}, uav=None)["status"] == "accepted"
            got.clear()
            h.advance(0.3)
            rows = np.frombuffer(got[-1]["rows"], TARGET_LITE32)
            assert rows.size == 29 and 3 not in set(rows["agent_no"].tolist())
            m = core.entities.add("target", model="object")
            assert m.target_no == 30
        finally:
            h.close()


def test_pack_rows_matches_frozen_layout() -> None:
    """实体表打包与冻结布局的参考实现（FakeTargets.lite_rows）逐字节相同（含位段与量化）。"""
    from awr.sim.core.entities import EntityTable

    ft = TS.FakeTargets([{"class": "person", "pos": [1.0, 2.0, 0.0], "motion": {"mode": "random_walk", "speed_mps": 1.2}},
                         {"class": "dog", "pos": [-5.0, 3.0, 0.0]}, {"class": "vehicle", "pos": [40.0, -8.0, 0.0]}])
    ft.step(7_300_000_000)
    ft.set_perception(0, 2, True, 1, relay=True)
    ft.set_awareness(1, "ALERTED", discovered=True)
    ft.cfg_rev[2] = 67
    with R.isolated_registry() as reg:
        _register_blocks()
        tab = EntityTable("target", reg.entity_blocks["target"])
        for i, c in enumerate(ft.cfgs):
            m = tab.add(model=c.cls, eid=c.id)
            assert m.row == i and m.target_no == i
        for k, a in ft.block.items():
            if k in tab.cols and k not in ("active", "target_no"):
                tab.cols[k][:] = a
        tab.cols["tgt_cfg_rev"][: len(ft.cfg_rev)] = ft.cfg_rev
        assert pack_rows(tab).tobytes() == ft.lite_rows().tobytes()


def test_entities_checkpoint_roundtrip() -> None:
    from awr.sim.runtime.ckpt import capture, restore

    with R.isolated_registry() as reg:
        _register_blocks()
        h = CoreHarness(n=1, reg=reg)
        try:
            ents = h.core.entities
            for k in range(5):
                m = ents.add("target", model="person", label=f"人-{k + 1:02d}")
                ents["target"].cols["tgt_pos"][m.row] = (k, 2 * k, 0.5)
            ents.remove("target", "person-02")
            arrays, meta = capture(h.core)
            arrays = {k: np.array(v, copy=True) for k, v in arrays.items()}
            before = {k: v.copy() for k, v in ents["target"].cols.items()}
            rv = h.core.roster.roster_version
            ents["target"].cols["tgt_pos"][:] = 99.0
            ents.add("target", model="dog")
            restore(h.core, arrays, meta)
            after = ents["target"].cols
            assert all(np.array_equal(before[k], after[k]) for k in before)
            assert [m.id for m in ents["target"].ordered()] == ["person-01", "person-03", "person-04", "person-05"]
            assert ents["target"].next_no == 5
            assert h.core.roster.roster_version > rv
            tg = [e for e in h.core.roster.snapshot()["entries"] if e["kind"] == "target"]
            assert [e["label"] for e in tg] == ["人-01", "人-03", "人-04", "人-05"]
        finally:
            h.close()
