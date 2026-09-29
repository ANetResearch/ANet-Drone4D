"""runs/ 配额与磁盘余量（AWR-19 §13.2；M11-FR-014；OPS-FR-035）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from awr.runtime import quota as Q


def make_run(root: Path, name: str, mb: float, keep: bool = False) -> Path:
    d = root / name
    (d / "logs").mkdir(parents=True)
    (d / "logs" / "x.log").write_bytes(b"\0" * int(mb * (1 << 20)))
    if keep:
        (d / "keep").touch()
    return d


def test_gc_oldest_first_with_exemptions(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    make_run(root, "r20250101-000000-aaaa", 3)
    make_run(root, "r20250102-000000-bbbb", 3, keep=True)
    make_run(root, "r20250103-000000-cccc", 3)
    make_run(root, "r20250104-000000-dddd", 3)
    make_run(root, "r20250105-000000-eeee", 3)  # 当前运行
    (root / "jobs").mkdir()
    (root / "jobs" / "big.bin").write_bytes(b"\0" * (4 << 20))
    (root / "jobs.db").write_bytes(b"\0" * 1024)
    (root / "notarun").mkdir()
    audit = []
    res = Q.gc_runs(root, 9.5 / 1024, "r20250105-000000-eeee", ["r20250104-000000-dddd"], audit=lambda k, d: audit.append(d["run"]))
    assert res.evicted == ["r20250101-000000-aaaa", "r20250103-000000-cccc"]
    assert audit == res.evicted
    assert (root / "r20250102-000000-bbbb").exists() and (root / "jobs" / "big.bin").exists() and (root / "notarun").exists()
    assert res.over_quota_keep is False and res.used_bytes <= res.quota_bytes


def test_keep_runs_over_quota_only_warn(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    make_run(root, "r20250101-000000-aaaa", 2, keep=True)
    make_run(root, "r20250102-000000-bbbb", 2)
    res = Q.gc_runs(root, 1 / 1024, "r20250102-000000-bbbb")
    assert res.evicted == [] and res.over_quota_keep is True


def test_disk_status_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AWR_DISK_FREE_OVERRIDE_GB", "4")
    ds = Q.disk_status(tmp_path)
    assert ds.free_gb == 4 and ds.low and ds.warn
    monkeypatch.delenv("AWR_DISK_FREE_OVERRIDE_GB")
    assert Q.disk_status(tmp_path / "missing" / "dir").free_gb > 0
