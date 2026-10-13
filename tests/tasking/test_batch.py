"""M20-AC-027（P1）、D2-AC-32 的编排部分；M20-FR-072、NFR-004：批处理（sandbox100，inline 作业在 `request_tick + L_sim` 生效）下，
同一会话配置与种子跑两次：`relay_stats.json`、事件序列（`t_wall_ns` 置 null）与全部作业结果摘要逐字节一致；会话配置的机巢与任务段
由 M20 装配（机巢 order 30 → 机队 40 → 识别物 50 → 任务 60），接力在批处理中派遣、到站并统计。"""

from __future__ import annotations

from pathlib import Path

import pytest

from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.batch import run_batch

CONFIG = {
    "schema": "awr.sandbox.snapshot.v1", "contracts": "1.1.0", "world_id": "synthcity", "seed": 11,
    "calendar": {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"},
    "nests": [{"id": "n-a", "pos": [0.0, 0.0, None], "kind": "box", "slots": 4}],
    "vehicles": [{"id": f"p600-0{k}", "model_id": "p600_mid360", "nest_id": "n-a"} for k in (1, 2, 3)],
    "targets": [{"class": "person", "pos": [0.0, 900.0, 0.0]}],
    "tasks": [{"type": "relay_watch", "label": "接力", "params": {"target": "person-01"}, "common": {}}],
}


def _run(out: Path, until: float = 600.0) -> dict:
    with R.isolated_registry() as reg:
        from awr.sim import safety
        from awr.sim.perception import plugin as pp
        from awr.sim.sensors import plugin as sp
        from awr.sim.targets import plugin as tp
        from awr.sim.tasking import plugin as kp

        prev = safety.SERVICE
        if "safety" in reg.blocks:                 # 首次导入时 M09 自动装配到了当前（隔离）登记表
            svc = safety.SERVICE
        else:
            svc = safety.install(metrics=False)
            safety.SERVICE = svc
        tp.install()
        sp.install().sandbox = True
        pp.install()
        rt = kp.install()
        try:
            s = run_batch(until, CONFIG, seed=11, out=out, clock="sandbox100", plugins=(), load_world=False)
            s["_digests"] = list(rt.jobs.digests)
            s["_relay"] = {T.tid: (T.relay.state, T.relay.on_station, T.relay.first_capture) for T in rt.tasks.values() if T.relay is not None}
            return s
        finally:
            kp.uninstall()
            pp.uninstall()
            sp.uninstall()
            tp.uninstall()
            safety.uninstall(svc)
            safety.SERVICE = prev


@pytest.mark.slow
def test_batch_relay_byte_identical(tmp_path: Path) -> None:
    a = _run(tmp_path / "a")
    b = _run(tmp_path / "b")
    assert a["exit_code"] == 0 and a["config_problems"] == [], a["config_problems"]
    assert a["n_uav"] == 3 and a["n_target"] == 1
    assert a["_relay"] and next(iter(a["_relay"].values()))[1] is not None      # 在岗机已到站
    assert (tmp_path / "a" / "relay_stats.json").read_bytes() == (tmp_path / "b" / "relay_stats.json").read_bytes()
    assert (tmp_path / "a" / "events.jsonl").read_bytes() == (tmp_path / "b" / "events.jsonl").read_bytes()
    assert a["_digests"] == b["_digests"] and any(k == "tasking.station" for k, _d in a["_digests"])
    kinds = (tmp_path / "a" / "events.jsonl").read_text(encoding="utf-8")
    for k in ("task.created", "relay.dispatch", "nest.launch"):
        assert f'"{k}"' in kinds
