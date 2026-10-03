"""演示前检查与一键演示（M16-FR-004、FR-027；M16-AC-006、017；M16-NFR-009）。

- 检查清单：7 项 + 2 项全部给出状态；人为缺失一个世界时退出码 6 并提示 `make worlds`；负载超限时 DEMO-E005 告警；≤ 60 s。
- 提示卡：无 emoji 与禁用字形；×10 墙钟时刻与 12 §7.2 一致。
- `test_make_demo`：世界已构建时 `make demo` 等价启动 ≤ 20 s 打印 READY，且缺省剧本 S1 被加载（`scenario.loaded`）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from awrproc import ROOT, Backend
from e2ehelp import WORLDS, needs_world

from awr.datasets.demo.check import exit_code, ext_status, run_checks
from awr.datasets.demo.script import card_lines, fmt_wall
from awr.datasets.urbanscene3d.cities import CITY_IDS

GLYPH = re.compile("[\U0001f000-\U0001faff" + "".join(f"{chr(a)}-{chr(b)}" for a, b in ((0x2600, 0x27BF), (0x25A0, 0x25FF), (0x2194, 0x21FF))) + chr(0xFE0F) + "]")


@pytest.mark.needs_data
def test_check_items_and_timing(tmp_path) -> None:
    needs_world(*CITY_IDS)
    t0 = time.monotonic()
    items = run_checks(ROOT, WORLDS, deep=True, runs_dir=tmp_path, with_doctor=True, offset=9)
    dt = time.monotonic() - t0
    ids = [it.id for it in items]
    assert ids[:8] == ["svc", "worlds", "scenarios", "access", "tier", "load", "warmup", "ext"]
    assert {"shm", "disk"} <= set(ids)
    assert all(it.status in ("pass", "fail", "warn", "manual") and it.detail for it in items)
    assert next(it for it in items if it.id == "worlds").status == "pass"
    assert next(it for it in items if it.id == "scenarios").status == "pass"
    assert next(it for it in items if it.id == "ext").code == "DEMO-E006"          # 空 runs：扩展段无通过记录
    assert dt <= 60.0, dt


@pytest.mark.needs_data
def test_missing_world_exits_6(tmp_path) -> None:
    needs_world(*CITY_IDS)
    w = tmp_path / "worlds"
    w.mkdir()
    for wid in CITY_IDS[:-1]:
        (w / wid).symlink_to(WORLDS / wid, target_is_directory=True)
    items = run_checks(ROOT, w, deep=False, runs_dir=tmp_path, with_doctor=False, offset=9)
    it = next(i for i in items if i.id == "worlds")
    assert it.status == "fail" and it.code == "DEMO-E001" and it.fix == "make worlds" and CITY_IDS[-1] in it.detail
    assert exit_code(items) == 6


@pytest.mark.needs_data
def test_load_high_warns(tmp_path) -> None:
    needs_world(*CITY_IDS)
    items = run_checks(ROOT, WORLDS, deep=False, runs_dir=tmp_path, with_doctor=False, max_load=0.0, offset=9)
    it = next(i for i in items if i.id == "load")
    assert it.status == "warn" and it.code == "DEMO-E005"
    assert exit_code(items) == 0                                                      # 告警不阻止演示


def test_cli_json_and_exit(tmp_path) -> None:
    env = dict(os.environ, AWR_RUNS_DIR=str(tmp_path), AWR_WORLDS_DIR=str(tmp_path / "none"), AWR_PORT_OFFSET="9")
    r = subprocess.run([sys.executable, "-m", "awr.datasets.demo", "check", "--json", "--quick", "--no-doctor"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 6, r.stderr
    d = json.loads(r.stdout)
    assert d["schema"] == "awr.demo_check.v1" and d["exit_code"] == 6
    assert r.stderr.strip().splitlines()[-1] == "修复：make worlds"


def test_ext_status_reads_latest_gate_report(tmp_path) -> None:
    rep = {"schema": "awr.perf.report.v1", "gate": "G3", "cases": [
        {"id": "e2e.scenarios", "ac_ids": ["D1-AC-15", "D1-AC-16"], "status": "PASS"},
        {"id": "timeline", "ac_ids": ["D1-AC-18"], "status": "FAIL"}]}
    d = tmp_path / "perf" / "p20260929-030000-abcd"
    d.mkdir(parents=True)
    (d / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    st = ext_status(tmp_path)
    assert st == {"D1-AC-16": "PASS", "D1-AC-18": "FAIL", "D1-AC-22": "NONE"}


def test_card_text_is_clean() -> None:
    items = run_checks(ROOT, WORLDS, deep=False, runs_dir=ROOT / "runs" / "none", with_doctor=False, offset=9) \
        if (WORLDS / "shenzhen").exists() else []
    lines = card_lines(items, port=8090, ext={"D1-AC-16": "PASS"})
    text = "\n".join(lines)
    assert not GLYPH.search(text)
    assert "ssh -N -L 8090:127.0.0.1:8090" in text and "0:08" in text and "0:13" in text and "0:42" in text
    assert "1:19" in text and "nofly-sz-t2" in text and "D6a" in text
    assert fmt_wall(786, 10) == "1:19" and fmt_wall(420, 1) == "7:00"


@pytest.mark.slow
@pytest.mark.needs_data
def test_make_demo() -> None:
    """M16-AC-017：世界已构建时 demo 形态（shenzhen + S1 + profile demo）≤ 20 s 打印 READY，缺省剧本 S1 被加载。

    与 `make demo` 相同的进程集合（`AWR_PROFILE=demo` 的 supervisor，含 ext 进程），但端口与 runs 目录取临时值，
    不占用 8000；前端生产构建不在此处重建。"""
    needs_world("shenzhen")
    b = Backend(world="shenzhen", scenario="s1-shenzhen-facade", scenario_profile="demo", profile="demo", only=None)
    try:
        b.start()
        assert b.t_ready_s <= 20.0, b.t_ready_s
        ev = b.wait_event(lambda e: e.get("type") in ("scenario.loaded", "scenario.invalid"), 30.0)
        assert ev["type"] == "scenario.loaded" and ev["data"]["scenario_id"] == "s1-shenzhen-facade"
        import httpx

        r = httpx.get(b.base + "/world/shenzhen", timeout=10)
        assert r.status_code == 200 and "text/html" in r.headers.get("content-type", "")
    finally:
        b.stop()


def test_repo_scenarios_dir_exists() -> None:
    assert (Path(ROOT) / "scenarios" / "catalog.json").exists()
