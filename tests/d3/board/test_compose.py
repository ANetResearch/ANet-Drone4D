"""D3-AC-57 (h) 合成接口：给定测试用评分版本的权重，由存储的分量重算合成值与置信区间，逐字节一致且无需重跑。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from awr.bench import ctl
from awr.bench.boardstore import append_rows, compose_board, publish
from awr.bench.eval.aggregate import build_board, compose

sys.path.insert(0, str(Path(__file__).parent))
from _rows import entries, row

W = {"ec": 0.4, "eu": 0.3, "ao": 0.2, "ce": 0.1}


def _rows() -> list[dict]:
    return ([row("t1/a", s, 0.8 - 0.02 * s, 0.6, 0.5 + 0.01 * s, 0.4) for s in range(6)]
            + [row("baseline/x", s, 0.5, 0.2, 0.3, 0.2 + 0.01 * s, baseline=True) for s in range(6)])


def test_compose_from_stored_components_is_deterministic() -> None:
    rows = _rows()
    c1 = compose(rows, W, board_version="v1", n_boot=800)
    c2 = compose(rows, W, board_version="v1", n_boot=800)
    assert json.dumps(c1) == json.dumps(c2)
    b = entries(build_board(rows, board_version="v1", n_boot=800))
    for c in c1:
        m = b[c["entry_id"]]["metrics"]
        assert c["value"] == pytest.approx(sum(W[k] * m[k]["value"] for k in W), abs=2e-6)
        assert c["ci95"][0] <= c["value"] <= c["ci95"][1]
    only_ec = {c["entry_id"]: c for c in compose(rows, {"ec": 1, "eu": 0, "ao": 0, "ce": 0}, board_version="v1", n_boot=800)}
    assert only_ec["t1/a"]["ci95"] == b["t1/a"]["metrics"]["ec"]["ci95"]          # 与榜单共用同一重采样序列
    scaled = compose(rows, {k: 10 * v for k, v in W.items()}, board_version="v1", n_boot=800)
    assert np.allclose([c["value"] for c in scaled], [c["value"] for c in c1])    # 权重按和归一
    with pytest.raises(ValueError):
        compose(rows, {"ec": -1, "eu": 1, "ao": 1, "ce": 1}, board_version="v1")


def test_ctl_board_compose_does_not_touch_the_board(tmp_path: Path, monkeypatch, capsys) -> None:
    d = tmp_path / "d3/boards/v1"
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    append_rows(d, [{**r, "baseline": True} for r in _rows()])
    publish(d, n_boot=200)
    before = (d / "board.json").read_bytes()
    monkeypatch.setenv("AWR_BENCH_DATA_ROOT", str(tmp_path / "d3"))
    capsys.readouterr()
    assert ctl.main(["board", "compose", "v1", "--weights", "ec=0.4,eu=0.3,ao=0.2,ce=0.1", "--n-boot", "200"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == compose_board(d, W, n_boot=200)
    assert (d / "board.json").read_bytes() == before
    assert ctl.main(["board", "compose", "v1", "--weights", "ec=1"]) == 2                                   # 缺项
    assert ctl.main(["board", "compose", "nope", "--weights", "ec=1,eu=0,ao=0,ce=0"]) == 2
