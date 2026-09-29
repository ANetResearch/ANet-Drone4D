"""集成验证（MS1/MS2）：运行时实际产出的载荷符合契约（请求 M11-R-to-M00 第 1–3 条合入后的回归）。

- `rt/payloads/sys_procs.schema.json`：状态枚举与 supervisor 一致（AWR-17 §4.3.12、AWR-19 §4.2，含 STOPPING），字段名取 17；
- `bus/event.schema.json`：批量子调用事件的外层 `batch_id`（M11 §6.4.12、M11-FR-061）；
- `perf/bench-result.schema.json`：`records[].ipc` 为可选的数值映射，现有基准输出保持有效。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctlib

from awr.runtime.events import EventPublisher
from awr.runtime.supervisor import STATES


def test_sys_procs_state_enum_matches_supervisor():
    enum = ctlib.load("rt/payloads/sys_procs.schema.json")["properties"]["items"]["items"]["properties"]["state"]["enum"]
    assert set(enum) == set(STATES) == {"STOPPED", "STARTING", "RUNNING", "STOPPING", "BACKOFF", "FAILED"}


def test_sys_procs_item_with_17_field_names():
    item = {"name": "sim-core", "state": "STOPPING", "pid": 1234, "restarts": 0, "last_exit": None, "uptime_s": 12.5,
            "hb_age_ms": 3.2, "cpu_pct": 41.0, "rss_mb": 310.0, "log_tail": ["line"], "last_reason": None,
            "on_demand": False, "layer": "core", "since_wall_ns": "1790000000000000000"}
    assert ctlib.errors("rt/payloads/sys_procs.schema.json", {"items": [item]}) == []
    assert ctlib.errors("rt/payloads/sys_procs.schema.json", {"items": [{**item, "state": "EXITED"}]}) != []


def test_event_batch_id_from_publisher():
    pub = EventPublisher(None, "sim-core", 1, serve_replay=False)  # type: ignore[arg-type]  # 只 emit，不经总线
    pub.emit("fleet.batch.progress", t_sim_ns=1_000_000, cid="c1:uav01", batch_id="c1", counts={"accepted": 1})
    pub.emit("roster.added", t_sim_ns=2_000_000, uav="uav02")
    for ev in pub._ring:
        assert ctlib.errors("bus/event.schema.json", ev) == [], ev
    assert pub._ring[0]["batch_id"] == "c1" and "batch_id" not in pub._ring[1]


def test_bench_result_ipc_optional():
    rec = {"n": 1000, "dur_s": 15.0, "rtf": 1.0, "cpu_core": 0.3, "step_us": {"p50": 1.0, "p99": 2.0, "max": 3.0},
           "catchup_saturated": 0, "stage_ms_per_s": {}, "rss_mb": {"start": 1.0, "end": 2.0}, "kernel": "numpy",
           "load": {"start": 0.5, "max": 1.0, "mean": 0.7}}
    doc = {"schema": "awr.bench.result.v1", "tool": "bench_state", "run_id": "r", "git_sha": "none",
           "host": {"cpu_model": "x", "cores": 8, "python": "3.12"}, "records": [rec]}
    assert ctlib.errors("perf/bench-result.schema.json", doc) == []
    doc["records"][0]["ipc"] = {"publish_p99_us": 151.7, "read_cpu_core": 0.0157, "cmd_rtt_p99_ms": None}
    assert ctlib.errors("perf/bench-result.schema.json", doc) == []
    doc["records"][0]["ipc"] = {"publish_p99_us": "fast"}
    assert ctlib.errors("perf/bench-result.schema.json", doc) != []
    for p in sorted((ctlib.ROOT / "runs" / "perf").glob("bench_*/bench-result.json")):  # 本机已有的基准输出（可无）
        assert ctlib.errors("perf/bench-result.schema.json", json.loads(p.read_text())) == [], p
