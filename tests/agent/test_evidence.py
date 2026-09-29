"""M14-AC-020、021：证据链（篡改任一字段可检出；id 64 位十六进制；t_wall 不影响 id；半截行截断并补 gap）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from awr.agent.runtime.evidence import EvidenceLog, canonical_json, chain_file_name, record_id


class Clk:
    def __init__(self) -> None:
        self.t = 0

    def now_ns(self) -> int:
        return self.t


def _log(path: Path | None = None) -> tuple[EvidenceLog, Clk]:
    c = Clk()
    lg = EvidenceLog("bafyreitestchain", path, c)
    for i in range(5):
        c.t = i * 1_000_000_000
        lg.append("agent.task.quote", {"task_id": "T-0001", "i": i, "score": 0.1 + i / 3})
    return lg, c


def test_chain_ids_and_verify() -> None:
    lg, _ = _log()
    assert lg.verify()
    for r in lg.rows:
        assert r["id"].startswith("sha256:") and len(r["id"]) == 7 + 64
    assert lg.rows[0]["prev"] == "genesis" and lg.rows[1]["prev"] == lg.rows[0]["id"]


def test_tamper_any_field_detected() -> None:
    lg, _ = _log()
    for field, mut in [("type", "x"), ("t_sim_ns", 1), ("seq", 9), ("prev", "genesis2"), ("chain", "other")]:
        rows = copy.deepcopy(lg.rows)
        rows[2][field] = mut
        assert not EvidenceLog.verify_rows(rows)
    rows = copy.deepcopy(lg.rows)
    rows[3]["payload"]["score"] = float.fromhex(float.hex(rows[3]["payload"]["score"])[:-1] + "1")  # 浮点末位
    assert not EvidenceLog.verify_rows(rows)


def test_wall_time_not_in_preimage() -> None:
    lg, _ = _log()
    r = dict(lg.rows[1])
    r["t_wall_ns"] = "1"
    assert record_id(r) == lg.rows[1]["id"]
    assert canonical_json({"b": 1, "a": [1.5, "中"]}) == '{"a":[1.5,"中"],"b":1}'.encode()


def test_persistence_and_torn_tail(tmp_path: Path) -> None:
    p = tmp_path / chain_file_name("bafyreitestchain")
    lg, c = _log(p)
    lg.close()
    data = p.read_bytes()
    last_nl = data.rstrip(b"\n").rfind(b"\n")
    cut = last_nl + 1 + (len(data) - last_nl - 1) // 2
    p.write_bytes(data[:cut])  # 截断最后一行一半
    lg2 = EvidenceLog.open("bafyreitestchain", p, c)
    assert lg2.verify() and lg2.gaps == 1
    assert lg2.rows[-1]["type"] == "anet.evidence.gap" and lg2.rows[-1]["payload"]["truncated_bytes"] > 0
    assert lg2.rows[-1]["seq"] == 4 and len(lg2.rows) == 5
    lg2.append("agent.task.state", {"task_id": "T-0001"})
    lg2.close()
    lg3 = EvidenceLog.open("bafyreitestchain", p, c)
    assert lg3.verify() and lg3.gaps == 0 and len(lg3.rows) == 6
    lines = [json.loads(x) for x in p.read_bytes().splitlines()]
    assert EvidenceLog.verify_rows(lines)


def test_corrupt_middle_truncates(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    lg, c = _log(p)
    lg.close()
    lines = p.read_bytes().splitlines(keepends=True)
    lines[2] = lines[2].replace(b'"i":2', b'"i":7')
    p.write_bytes(b"".join(lines))
    lg2 = EvidenceLog.open("bafyreitestchain", p, c)
    assert lg2.verify() and lg2.rows[-1]["type"] == "anet.evidence.gap" and len(lg2.rows) == 3


def test_sync_close_queries_and_canonical_errors(tmp_path: Path) -> None:
    import math

    import pytest

    seen: list[str] = []

    def boom(line: dict) -> None:
        seen.append(line["id"])
        raise RuntimeError("listener bug")  # 监听器异常不影响追加

    c = Clk()
    lg = EvidenceLog("bafyreiq", tmp_path / "q.jsonl", c, on_append=boom, keep=3)
    for i in range(5):
        lg.append("agent.task.state", {"task_id": f"T-000{i % 2}"})
    assert len(seen) == 5 and len(lg.rows) == 3 and lg.verify()
    lg.sync()
    lg.sync()
    assert [r["payload"]["task_id"] for r in lg.for_task("T-0000")] == ["T-0000", "T-0000"]
    assert lg.ids() == [r["id"] for r in lg.rows]
    lg.close()
    lg.close()
    lg.sync()
    with pytest.raises(ValueError):
        canonical_json({"x": math.nan})
    with pytest.raises(TypeError):
        canonical_json({1: "x"})
    assert EvidenceLog("c", None, None).verify() and EvidenceLog("c", None, None).append("t", {}).startswith("sha256:")
