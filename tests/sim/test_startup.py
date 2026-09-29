"""启动序列（M08-AC-024 的功能部分、AC-009 的拒绝启动；M08-FR-005、FR-007、FR-042；NFR-008）。

- 机型目录含非法 profile（P600 质量 1.505 kg）时 SimCore 构造即抛 `ProfileError`（353），`main()` 路径写
  `sim-core.json{status: failed, code: 353}` 并以非 0 退出（不进入主循环）；
- 正常启动：`sim.started{kernel, n, epoch, segment}` 与每个 profile 的 `sim.profile.loaded`；`meta.json` 的 `sim` 段含
  kernel、fastmath = false、版本字段，旁路 `sim-core.json` 记录实际 CPU 亲和性、nice 与 numba 预热耗时；
- numba 预热（缓存命中）≤ 2.0 s 与冷启动时限属 perf 用例（并行阶段不跑）。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest
import yaml
from simlib import CoreHarness

from awr.sim.fleet import kernels_l1 as KL
from awr.sim.fleet.profiles import CODE_PROFILE_INVALID, ProfileError
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.main import _write_profile_failure

ROOT = Path(__file__).resolve().parents[2]


def _bad_vehicles(tmp: Path) -> Path:
    d = tmp / "vehicles"
    shutil.copytree(ROOT / "vehicles", d, ignore=shutil.ignore_patterns("*.glb", "*.stl", "model"))
    p = d / "p600" / "params.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    doc["mass_kg"]["value"] = 1.505
    p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return d


def test_invalid_profile_refuses_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AWR_VEHICLES_DIR", str(_bad_vehicles(tmp_path)))
    with R.isolated_registry() as reg, pytest.raises(ProfileError) as ei:
        CoreHarness(n=1, reg=reg, ready=False, seat=False)
    e = ei.value
    assert e.code == CODE_PROFILE_INVALID == 353
    assert any("VH-1" in p or "VH-2" in p for p in e.problems), e.problems
    _write_profile_failure(tmp_path, e)
    side = json.loads((tmp_path / "sim-core.json").read_text(encoding="utf-8"))
    assert side["status"] == "failed" and side["code"] == 353 and side["profile_id"] == e.profile_id


def test_started_events_and_meta(tmp_path) -> None:
    (tmp_path / "meta.json").write_text(json.dumps({"schema": "awr.meta.v1", "sim": {}}), encoding="utf-8")
    seen: list[tuple[str, dict]] = []
    import awr.runtime.events as E

    orig = E.EventPublisher.emit

    def rec(self, kind, **kw):
        seen.append((kind, kw))
        return orig(self, kind, **kw)

    E.EventPublisher.emit = rec
    try:
        with R.isolated_registry() as reg:
            h = CoreHarness(n=2, reg=reg, persist_dir=tmp_path, ready=False, seat=False)
            try:
                started = [kw for k, kw in seen if k == "sim.started"]
                assert started and started[0]["n"] == 2 and started[0]["kernel"] in ("numba", "numpy")
                assert started[0]["epoch"] == h.core.epoch
                loaded = {kw["profile_id"] for k, kw in seen if k == "sim.profile.loaded"}
                assert loaded == {"x500", "x500_sih", "p600_mid360"}
                meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
                sim = meta["sim"]
                assert sim["kernel"] == h.core.fleet.kernel and sim["fastmath"] is False
                assert {"numpy_version", "python_version", "kernel_version", "numba_version", "fleet_config"} <= set(sim)
                assert {v["profile_id"] for v in meta["vehicles_profiles"]} == loaded
                side = json.loads((tmp_path / "sim-core.json").read_text(encoding="utf-8"))
                assert side["affinity"] == sorted(os.sched_getaffinity(0)) and side["nice"] == os.getpriority(os.PRIO_PROCESS, 0)
                assert side["warmup_s"] >= 0.0
            finally:
                h.close()
    finally:
        E.EventPublisher.emit = orig


@pytest.mark.perf
def test_numba_warmup_cached_under_2s() -> None:
    if not KL.HAVE_NUMBA:
        pytest.skip("numba 不可用")
    assert KL.warmup()
    t0 = time.perf_counter()
    KL.warmup()
    assert time.perf_counter() - t0 <= 2.0
