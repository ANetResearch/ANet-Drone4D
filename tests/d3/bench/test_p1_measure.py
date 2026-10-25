"""P-1 补测脚本（D3-AC-47；tools/bench/d3/p1_measure.py）的本机用例：fio 结果解析与写入路径限制、设备写速率、写带宽预算、
pmon 解析与 ADR-195 独占级别、磁带存储密度（22 B 布局，≤ 25 B/演员/s）、纹素与临界斜距、DSM 与网格对比统计、可见度统计、
会话显存分解、标准轨采集规格、汇总判定（GPU 性能项按 guard 级别计入）。GPU 子命令（jpeg、r3 --visibility、m25）在 dmax 上执行，
其结果由 summarize 用例的结构约定覆盖。"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("p1_measure", ROOT / "tools" / "bench" / "d3" / "p1_measure.py")
pm = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("p1_measure", pm)
_spec.loader.exec_module(pm)

H64 = "0" * 64


# ---------------------------------------------------------------------------------------------------------------- disk
FIO_DOC = {"fio version": "fio-3.37", "jobs": [{"jobname": "d3w", "job_runtime": 240000,
                                                 "write": {"bw_bytes": 1_250_000_000, "iops": 1192.1, "io_bytes": 300_000_000_000, "runtime": 60000,
                                                           "clat_ns": {"percentile": {"50.000000": 2_900_000, "99.000000": 11_000_000,
                                                                                      "99.900000": 25_000_000}}}}]}


def test_parse_fio_group_reporting() -> None:
    m = pm.parse_fio(FIO_DOC)
    assert m["bw_MBps"] == 1250.0 and m["iops"] == 1192.1 and m["runtime_ms"] == 60000
    assert m["clat_ms"] == {"p50": 2.9, "p99": 11.0, "p99_9": 25.0} and m["fio_version"] == "fio-3.37"


def test_fio_cmd_and_d3_directory_guard(tmp_path: Path) -> None:
    assert pm.d3_dir_ok(Path("/data/d3/scratch/fio")) and pm.d3_dir_ok(Path("/data/anet-drone4d-d3/d3dev/d00/fio"))
    assert not pm.d3_dir_ok(Path("/tmp/fio")) and not pm.d3_dir_ok(Path("/d3")) and not pm.d3_dir_ok(Path("/data/other"))
    c = pm.fio_cmd("fio", tmp_path, "direct", runtime_s=60, size_gib=4, numjobs=4)
    assert "--direct=1" in c and "--rw=write" in c and "--end_fsync=1" in c and "--time_based=1" in c and "--numjobs=4" in c
    assert "--direct=0" in pm.fio_cmd("fio", tmp_path, "buffered", runtime_s=60, size_gib=4, numjobs=1)
    with pytest.raises(ValueError):
        pm.fio_cmd("fio", tmp_path, "random", runtime_s=60, size_gib=4, numjobs=1)


def test_disk_refuses_non_d3_directory_and_limits(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert pm.main(["disk", "--dir", str(tmp_path / "x"), "--fio", "/bin/true", "--out", str(out)]) == pm.EXIT_ENV
    d3 = tmp_path / "d3" / "fio"
    assert pm.main(["disk", "--dir", str(d3), "--fio", "/bin/true", "--runtime-s", "601", "--out", str(out)]) == pm.EXIT_ENV
    assert pm.main(["disk", "--dir", str(d3), "--fio", "/bin/true", "--numjobs", "9", "--out", str(out)]) == pm.EXIT_ENV
    assert not (out / "disk.json").exists()


def test_diskstats_sectors_written() -> None:
    text = ("   8       0 sda 100 0 200 10 5000 0 123456 900 0 1000 910 0 0 0 0\n"
            "   8       1 sda1 90 0 180 9 4900 0 120000 880 0 990 890 0 0 0 0\n")
    assert pm.diskstats_sectors_written("sda1", text) == 120000
    assert pm.diskstats_sectors_written("sda", text) == 123456
    assert pm.diskstats_sectors_written("nvme0n1", text) is None


def test_write_budget_rule() -> None:
    b = pm.write_budget(900.0)
    assert b["budget_MBps"] == 300.0 and b["covers_3_concurrent_runs"] and b["concurrent_std_runs_at_budget"] == 7
    b = pm.write_budget(100.0)
    assert b["budget_MBps"] == 50.0 and not b["covers_3_concurrent_runs"] and b["concurrent_std_runs_at_budget"] == 1
    assert pm.write_budget(None) == {"budget_MBps": None}


# ---------------------------------------------------------------------------------------------------------------- 独占级别
PMON = """# gpu         pid   type     sm    mem    enc    dec    jpg    ofa     fb   ccpm    command
# Idx           #    C/G      %      %      %      %      %      %     MB     MB    name
    1    3528289     C      -      -      -      -      -      -    656      0    python
    1    3528334     C      -      -      -      -      -      -    658      0    python
    1    4100001     G     37      9      -      -      -      -    950      0    python3
    1          -     -      -      -      -      -      -      -      -      -    -
"""


def test_parse_pmon_columns_and_dashes() -> None:
    s = pm.parse_pmon(PMON.splitlines())
    assert [x["pid"] for x in s] == ["3528289", "3528334", "4100001"]
    assert s[0]["sm"] == 0 and s[0]["mem"] == 0 and s[0]["fb_mib"] == 656 and s[0]["type"] == "C"
    assert s[2]["sm"] == 37 and s[2]["mem"] == 9 and s[2]["type"] == "G"


def test_grade_exclusivity_levels() -> None:
    s = pm.parse_pmon(PMON.splitlines())
    ours = {"4100001"}
    g = pm.grade_exclusivity({"3528289", "3528334"}, s, ours)
    assert g["level"] == "B" and g["external_pids"] == ["3528289", "3528334"] and g["external_active_samples"] == 0
    assert pm.grade_exclusivity(set(), [x for x in s if x["pid"] in ours], ours)["level"] == "A"
    busy = [*s, {"pid": "3528289", "sm": 3, "mem": 0}]
    assert pm.grade_exclusivity({"3528289"}, busy, ours)["level"] == "invalid"
    new_ext = [{"pid": "999", "sm": 0, "mem": 0}]                      # 窗口内出现、不属于本次容器：外部
    assert pm.grade_exclusivity(set(), new_ext, ours)["level"] == "B"
    assert pm.b_level_ok(47_433, 2_000) is True and pm.b_level_ok(5_000, 2_000) is False and pm.b_level_ok(None, 1) is None
    assert pm.counted_for("A", None) is True and pm.counted_for("B", True) == "pending_user_confirmation"
    assert pm.counted_for("B", False) is False and pm.counted_for("invalid", True) is False and pm.counted_for(None, None) is False


def test_guard_cli_keeps_wrapped_command() -> None:
    a = pm._args(["guard", "--gpu", "1", "--out", "/tmp/g.json", "--pre-s", "5", "--", "docker", "run", "--name", "x", "img", "sh", "-c", "a && b"])
    assert a.cmd == "guard" and a.gpu == 1 and a.pre_s == 5.0
    assert a.command[a.command.index("docker"):] == ["docker", "run", "--name", "x", "img", "sh", "-c", "a && b"]


# ---------------------------------------------------------------------------------------------------------------- 磁带
def _header(n_ticks: int, n_s: int, n_b: int) -> dict:
    from awr.sim.events import tape as tp

    actors = [{"idx": i, "id": f"p{i:02d}" if i < n_s else f"bg{i - n_s:04d}", "class": "person", "scripted": i < n_s, "profile": "person",
               "size_mm": [500, 300, 1750], "appearance": {"asset": "person-proxy@1"}} for i in range(n_s + n_b)]
    return {"schema": "drone4d.tape.v1", "event_id": "synth-walk", "event_version": 1, "event_sha256": H64, "mode": "exogenous", "seed": 7,
            "tick_hz": 10, "n_ticks": n_ticks, "duration_ms": n_ticks * 100, "record": tp.record_spec(),
            "codec": {"name": "col-delta-zstd@1", "block_ticks": 100}, "actors": actors,
            "world": {"id": "synth", "content_version": "v1", "raycast_sha256": H64, "navmesh_sha256": {"person": H64}, "anchors_sha256": H64},
            "environment": {}, "env_track": [], "comms": {}, "anim": None, "realized": None}


def _walkers(n_ticks: int, n: int, seed: int = 3) -> np.ndarray:
    from awr.sim.events import tape as tp

    rng = np.random.default_rng(seed)
    f = np.zeros((n_ticks, n), tp.RECORD)
    pos = rng.uniform(-400, 400, (n, 2))
    hd = rng.uniform(-np.pi, np.pi, n)
    sp = rng.uniform(0.8, 1.6, n)
    for k in range(n_ticks):
        hd += np.where(rng.random(n) < 0.01, rng.uniform(-0.5, 0.5, n), 0.0)
        v = sp[:, None] * np.stack([np.cos(hd), np.sin(hd)], 1)
        pos += v * 0.1
        f["x_mm"][k], f["y_mm"][k], f["z_mm"][k] = np.round(pos[:, 0] * 1000), np.round(pos[:, 1] * 1000), 12_000
        f["yaw_cdeg"][k] = np.round(np.degrees(hd) * 100)
        f["vx_cmps"][k], f["vy_cmps"][k] = np.round(v[:, 0] * 100), np.round(v[:, 1] * 100)
        f["state"][k], f["container"][k] = 1, tp.NO_CONTAINER
    return f


def test_tape_density_accounts_for_every_byte(tmp_path: Path) -> None:
    pytest.importorskip("zstandard")
    from awr.sim.events import tape as tp

    n_t, n_s, n_b = 3600, 3, 40
    tp.write_tape(tmp_path / "tape.bin", _header(n_t, n_s, n_b), _walkers(n_t, n_s + n_b), n_scripted=n_s)
    d = pm.tape_density(tmp_path)
    assert d["n_actors"] == 43 and d["n_scripted"] == 3 and d["n_background"] == 40 and d["duration_s"] == 360.0 and d["blocks"] == 36
    # 文件 = 魔数 8 + 头长 4 + 规范头 + 块数 4 + 块索引 + 两类子块
    assert d["file_bytes"] == 8 + 4 + d["header_bytes"] + 4 + d["index_bytes"] + d["comp_scripted_bytes"] + d["comp_background_bytes"]
    assert d["raw_bytes"] == 22 * n_t * 43 and d["raw_bytes_per_actor_s"] == 220
    assert d["bytes_per_actor_s"] == pytest.approx(d["file_bytes"] / 43 / 360, abs=1e-3)
    assert d["body_bytes_per_actor_s"] < d["bytes_per_actor_s"] <= pm.TAPE_MAX_BPAS
    assert d["compression_ratio"] > 8


def test_tape_cli_writes_part_and_judges(tmp_path: Path) -> None:
    pytest.importorskip("zstandard")
    from awr.sim.events import tape as tp

    (tmp_path / "t").mkdir()
    tp.write_tape(tmp_path / "t" / "tape.bin", _header(600, 2, 10), _walkers(600, 12), n_scripted=2)
    assert pm.main(["tape", "--tape", str(tmp_path / "t"), "--out", str(tmp_path / "o")]) == pm.EXIT_OK
    doc = json.loads((tmp_path / "o" / "tape.json").read_text())
    assert doc["schema"] == pm.PART_SCHEMA and doc["part"] == "tape"
    c = doc["checks"][0]
    assert c["threshold"] == 25.0 and c["pass"] is True and c["value"] == doc["result"]["tapes"][0]["bytes_per_actor_s"]
    assert pm.main(["tape", "--out", str(tmp_path / "o2")]) == pm.EXIT_ENV


# ---------------------------------------------------------------------------------------------------------------- r3 纹素
def _square(scale: float):
    V = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], np.float64) * scale
    F = np.array([[0, 1, 2], [0, 2, 3]])
    uv = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)
    return V, F, uv


def test_texel_stats_known_square_and_scale() -> None:
    V, F, uv = _square(1.0)
    s = pm.texel_stats(V, F, uv, np.zeros(2, np.int64), np.array([[100, 100]]))
    assert s["texel_m"] == pytest.approx(0.1) and s["texel_m_quantiles"]["p10"] == pytest.approx(0.1)
    assert s["texture_limited_range_m"]["rgb1080-rs"] == pytest.approx(0.1 * pm.RGB1080_FX, abs=0.05)
    assert s["texture_limited_range_m"]["720p_hfov60"] == pytest.approx(0.1 * 640 / math.tan(math.radians(30)), abs=0.05)
    assert s["edge_p50_m"] == pytest.approx(10.0) and s["surface_km2"] == pytest.approx(1e-4) and s["textured_area_fraction"] == 1.0
    s2 = pm.texel_stats(V * 2.0, F, uv, np.zeros(2, np.int64), np.array([[100, 100]]))
    assert s2["texel_m"] == pytest.approx(0.2) and s2["edge_p50_m"] == pytest.approx(20.0)
    s3 = pm.texel_stats(V, F, None, np.full(2, -1), np.array([[0, 0]]))
    assert s3["texel_m"] is None and s3["texture_limited_range_m"] is None


def test_weighted_quantile_and_quantiles_match_numpy() -> None:
    assert pm.weighted_quantile([1.0, 2.0, 3.0], [1.0, 1.0, 10.0], 0.5) == 3.0
    x = np.random.default_rng(0).normal(size=101)
    q = pm.quantiles(x, (0.5, 0.9, 0.99))
    assert q["p50"] == pytest.approx(np.quantile(x, 0.5), abs=1e-6) and q["p99"] == pytest.approx(np.quantile(x, 0.99), abs=1e-6)
    assert pm.quantiles([], (0.5,)) == {"p50": None}


def test_parse_glb_arg(tmp_path: Path) -> None:
    reg = tmp_path / "registration.json"
    reg.write_text(json.dumps({"method": "osm", "result": {"m_per_unit": 8.374}}))
    assert pm.parse_glb_arg(f"town=/x/a.glb@{reg}")[:3] == ("town", Path("/x/a.glb"), 8.374)
    assert pm.parse_glb_arg("hill=/x/b.glb@2.49")[2] == 2.49
    with pytest.raises(ValueError):
        pm.parse_glb_arg("bad=/x/a.glb")


# ---------------------------------------------------------------------------------------------------------------- r3 可见度、r4
def test_visibility_stats_groups_and_fractions() -> None:
    g = pm.occluder_group
    assert (g(5, 0.0, 9.0), g(6, 1.0, 9.0), g(9, 0.0, 9.0), g(33, 0.0, 0.0), g(14, 0.0, 0.0)) == ("elevated", "facade", "ground", "actor", "other")
    assert (g(0, 0.95, 6.0), g(0, 0.1, 6.0), g(0, 0.99, 0.2)) == ("elevated", "facade", "ground")      # 未分类：按几何
    s = [{"ratio": 1.0, "class": "person", "occ_hist": {}}, {"ratio": 0.5, "class": "person", "occ_hist": {"elevated": 30, "facade": 10}},
         {"ratio": 0.0, "class": "vehicle", "occ_hist": {"elevated": 50, "ground": 5, "actor": 5}},
         {"ratio": 0.9, "class": "person", "occ_hist": {"other": 6, "bogus": 4}}]
    v = pm.visibility_stats(s)
    assert v["views"] == 4 and v["fully_visible"] == 0.25 and v["fully_occluded"] == 0.25 and v["partial"] == 0.5
    assert v["median_visible_ratio"] == pytest.approx(0.7)
    f = v["occluder_px_fraction"]
    assert f["elevated"] == pytest.approx(80 / 110, abs=1e-4) and f["facade"] == pytest.approx(10 / 110, abs=1e-4)
    assert f["ground"] == pytest.approx(5 / 110, abs=1e-4) and f["actor"] == pytest.approx(5 / 110, abs=1e-4)
    assert v["by_class"]["vehicle"]["fully_occluded"] == 1.0
    assert pm.visibility_stats([]) == {"views": 0}


def test_dsm_mesh_compare() -> None:
    td = np.array([10.0, 20.0, np.nan, 5.0, 150.0])
    tm = np.array([10.5, 23.0, 7.0, np.nan, np.inf])
    s = pm.dsm_mesh_compare(td, tm, 100.0)
    assert s["rays"] == 5 and s["dsm_hit"] == 0.6 and s["mesh_hit"] == 0.6 and s["hit_disagree"] == 0.4
    assert s["abs_dr_m"]["p50"] == pytest.approx(1.75) and s["abs_dr_gt_2m"] == 0.5 and s["dr_median_m"] == pytest.approx(-1.75)


def test_street_positions_prefers_flat_open_ground() -> None:
    a = np.zeros((100, 100), np.float32)
    a[:, 50:] = 8.0                                                    # 东半部为建筑：nDSM 8 m
    dsm = (a, 0.0, 0.0, 2.0)
    dtm = (np.zeros((20, 20), np.float32), 0.0, 0.0, 10.0)
    pts = pm.street_positions(dsm, dtm, np.random.default_rng(1), 30, inset_m=5.0)
    assert len(pts) == 30 and all(x < 100.0 for x, _ in pts)


# ---------------------------------------------------------------------------------------------------------------- jpeg、m25
def test_jpeg_size_stats_extrapolation() -> None:
    s = pm.jpeg_size_stats([600_000, 700_000, 800_000])
    assert s["frames"] == 3 and s["mean_kB"] == 700.0 and s["max_kB"] == 800.0
    assert s["per_uav_min_MB"] == 420.0 and s["std_run_rgb_GB"] == pytest.approx(0.7 * 3 * 10 * 360 / 1000, abs=0.01)
    assert s["write_MBps_3uav_10hz"] == 21.0
    assert pm.jpeg_size_stats([]) == {"frames": 0}


def test_session_vram_breakdown() -> None:
    st = {"baseline": 1707, "gl_ctx": 1900, "world_resident": 3400, "first_frame": 3520, "cuda_ctx": 3830, "encoder": 3900}
    v = pm.session_vram(st, 4000)
    assert v["gl_ctx_mib"] == 193 and v["world_resident_mib"] == 1500 and v["render_targets_mib"] == 120 and v["cuda_ctx_mib"] == 310
    assert v["encoder_mib"] == 70 and v["session_mib"] == 4000 - 3400 - 310 and v["session_incl_cuda_ctx_mib"] == 600
    assert v["worker_total_mib"] == 2293
    assert pm.session_vram({**st, "encoder": None}, 4000)["session_mib"] is None


def test_standard_spec_is_three_rgb1080_rs_with_mid360(tmp_path: Path) -> None:
    from awr.sensorsvc.capture import load_spec

    base = json.loads((ROOT / "configs" / "capture.d3" / "crop-handoff-001.json").read_text())
    spec = pm.standard_spec(base, run_id="r", event_path=tmp_path / "ev", offset_s=200, duration_s=60, noise="realistic", seed=1)
    _doc, fleet = load_spec(spec)
    assert len(fleet) == 3 and all(len(u.cameras) == 1 and len(u.lidars) == 1 for u in fleet)
    for u in fleet:
        cm = u.cameras[0].model
        assert (cm.width, cm.height, cm.readout_s, cm.fps) == (1920, 1080, 0.020, "10")
        assert cm.fx == pytest.approx(pm.RGB1080_FX, abs=0.01) and u.cameras[0].outputs == frozenset({"rgb"})
        assert u.lidars[0].model_id == "mid360"
    assert spec["event"] == {"path": str(tmp_path / "ev"), "offset_s": 200.0} and spec["noise_profile"] == "realistic"


def test_summarize_m25_medians() -> None:
    def run(tier, g, s):
        return {"tier": tier, "gpu_s_per_sim_s": g, "gpu_s_per_sim_s_gl": g * 0.9, "gpu_s_per_sim_s_nvjpeg": g * 0.1, "sensor_rtf_ceiling": 1 / g,
                "vram": {"session_mib": s, "session_incl_cuda_ctx_mib": s + 300, "worker_total_mib": s + 2000, "world_resident_mib": 1500},
                "gl_gpu_ms": {"p50": 10.0, "p99": 14.0}, "nvjpeg_ms": {"p50": 2.0}, "jpeg": {"mean_kB": 640.0},
                "storage": {"MB_per_sim_s": 40.0, "std_run_GB_est": 14.4}}
    s = pm.summarize_m25([run("clear", 0.4, 300), run("clear", 0.5, 320), run("clear", 0.45, 310), run("harsh", 0.6, 330)])
    assert s["clear"]["runs"] == 3 and s["clear"]["gpu_s_per_sim_s"] == 0.45 and s["clear"]["session_mib"] == 310
    assert s["harsh"]["runs"] == 1


# ---------------------------------------------------------------------------------------------------------------- 汇总
def _part(name: str, checks: list[dict], result: dict | None = None) -> dict:
    return {"schema": pm.PART_SCHEMA, "part": name, "utc_start": "t0", "utc_end": "t1", "host": {}, "result": result or {}, "checks": checks}


def test_summarize_counts_gpu_items_by_guard_level(tmp_path: Path) -> None:
    vram = pm.check("ac47.m25.vram.clear", "v", "c", 0.3, threshold=1.2, passed=True, counted="by_guard")
    tape = pm.check("ac07.tape_density.0", "t", "c", 14.4, threshold=25.0, passed=True)
    m25 = _part("m25", [vram], {"runs": [{"vram": {"worker_total_mib": 2400}}]})
    parts = {"m25": m25, "tape": _part("tape", [tape])}
    gB = {"gpu": 1, "parts": ["m25"], "level": "B", "pre_free_min_mib": 47_400, "run": {"external_pids": ["1"]}}
    d = pm.summarize(parts, [gB])
    c = next(x for x in d["checks"] if x["id"] == "ac47.m25.vram.clear")
    assert c["counted"] == "pending_user_confirmation" and "驻留豁免" in c["exclusivity"] and d["need_mib"] == 2400
    assert next(x for x in d["checks"] if x["part"] == "tape")["counted"] is True and d["all_judged_pass"] is True
    def v(doc: dict) -> dict:
        return next(x for x in doc["checks"] if x["id"] == "ac47.m25.vram.clear")
    assert v(pm.summarize(parts, [{**gB, "level": "A"}]))["counted"] is True
    assert v(pm.summarize(parts, [{**gB, "pre_free_min_mib": 3000}]))["counted"] is False
    nog = pm.summarize(parts, [])
    assert v(nog)["counted"] is False and "共享卡" in v(nog)["exclusivity"]
    assert set(nog["parts_missing"]) == {"disk", "r3", "r4", "jpeg"}
    bad = pm.summarize({**parts, "tape": _part("tape", [{**tape, "pass": False}])}, [gB])
    assert bad["all_judged_pass"] is False
    out = tmp_path / "p1"
    out.mkdir()
    for k, v in parts.items():
        (out / f"{k}.json").write_text(json.dumps(v))
    (out / "guard-gpu.json").write_text(json.dumps(gB))
    r3a = _part("r3", [pm.check("ac47.r3.texel.town", "t", "c", 0.28)], {"texel": {"town": {}}})
    r3b = _part("r3", [pm.check("ac47.r3.visibility", "v", "c", {"views": 200})], {"visibility": {}})
    (out / "r3-texel.json").write_text(json.dumps(r3a))
    (out / "r3-vis.json").write_text(json.dumps(r3b))
    assert pm.main(["summarize", "--out", str(out)]) == pm.EXIT_OK
    doc = json.loads((out / "p1_measure.json").read_text())
    assert doc["schema"] == pm.SCHEMA and "r3" in doc["parts_present"]
    assert set(doc["parts"]["r3"]["result"]) == {"r3-texel", "r3-vis"}
    assert {c["id"] for c in doc["checks"] if c["part"] == "r3"} == {"ac47.r3.texel.town", "ac47.r3.visibility"}
