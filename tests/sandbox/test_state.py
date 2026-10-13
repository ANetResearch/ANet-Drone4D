"""状态落盘（M17-FR-027、NFR-009）：原子写出（0600）、≤ 1 Hz 合批、损坏文件改名保留并以空表启动。"""

from __future__ import annotations

from pathlib import Path

from awr.sandbox.state import StateStore


def test_save_load_and_rate(tmp_path: Path) -> None:
    st = StateStore(tmp_path / "s" / "state.json")
    assert st.load() is None and not st.flush_due(10.0)
    st.mark()
    assert st.flush_due(10.0)
    st.save({"v": 1, "x": [1, 2]}, now_mono=10.0)
    assert (tmp_path / "s" / "state.json").stat().st_mode & 0o777 == 0o600
    assert st.load() == {"v": 1, "x": [1, 2]}
    st.mark()
    assert not st.flush_due(10.5) and st.flush_due(11.0)


def test_corrupt_file_is_moved_aside(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("{not json")
    st = StateStore(p)
    assert st.load() is None
    assert not p.exists() and list(tmp_path.glob("state.json.bad-*"))
