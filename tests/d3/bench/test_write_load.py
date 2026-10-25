"""1 h 生产负载写带宽与 MCAP 阻塞测量（D3-AC-22；tools/bench/d3/write_load.py）的本机用例：`io.stat` 与 `/proc/<pid>/io` 解析、
累计量增量与计数重置、不重叠窗口速率的按重叠分摊与采样缺口、p95 口径、调度器 1 min 遥测、写入器阻塞统计（清单 `recorder` 块）、
运行并发分布、guard 独占级别、`analyze` 的通过与不通过判定（p95 超预算、阻塞 > 1%、窗口不足 1 h、Run 失败），宿主机采样器对
cgroup 目录的读取，以及合成负载经调度器与同一 `RunWriter` 的端到端短程（≤ 3 个正式 Run 并发、精简后只留摘要）。正式 1 h 测量
在 dmax 独占卡上执行，其产物由 `analyze` 用例的结构约定覆盖。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("write_load", ROOT / "tools" / "bench" / "d3" / "write_load.py")
wl = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("write_load", wl)
_spec.loader.exec_module(wl)

MB = 1_000_000


# ---------------------------------------------------------------------------------------------------------------- 解析
def test_parse_io_stat_and_proc_io() -> None:
    st = wl.parse_io_stat("8:0 rbytes=10 wbytes=4096 rios=1 wios=2 dbytes=0 dios=0\n259:0 rbytes=0 wbytes=7 rios=0 wios=1 dbytes=0 dios=0\n")
    assert st["8:0"]["wbytes"] == 4096 and st["259:0"]["wbytes"] == 7 and st["8:0"]["wios"] == 2
    assert wl.parse_io_stat("") == {}
    io = wl.parse_proc_io("rchar: 100\nwchar: 200\nsyscr: 1\nsyscw: 2\nread_bytes: 4096\nwrite_bytes: 8192\ncancelled_write_bytes: 0\n")
    assert io["write_bytes"] == 8192 and io["wchar"] == 200


def test_disk_of_resolves_partition_and_disk(tmp_path: Path) -> None:
    part, disk = wl.disk_of(tmp_path)
    assert part and disk is not None and ":" in disk
    assert wl.disk_of(tmp_path / "missing") == (None, None)


# ---------------------------------------------------------------------------------------------------------------- 速率
def test_intervals_dict_scalar_reset_and_missing_ids() -> None:
    recs = [{"t": 0.0, "load": {"a": 0, "b": 100}, "dev_sectors_w": 0},
            {"t": 1.0, "load": {"a": 2 * MB, "b": 100 + MB}, "dev_sectors_w": 4},
            {"t": 2.0, "load": {"a": 3 * MB, "c": 5 * MB}, "dev_sectors_w": 10},      # b 退出、c 新出现：都不计
            {"t": 3.0, "load": {"a": 1 * MB, "c": 6 * MB}, "dev_sectors_w": 12}]      # a 回退（重置）：不计
    assert wl.intervals(recs, "load") == [(0.0, 1.0, 3 * MB), (1.0, 2.0, 1 * MB), (2.0, 3.0, 1 * MB)]
    assert wl.intervals(recs, "dev_sectors_w", scale=512.0) == [(0.0, 1.0, 2048.0), (1.0, 2.0, 3072.0), (2.0, 3.0, 1024.0)]
    assert wl.intervals([{"t": 0.0}, {"t": 1.0}], "load") == []


def test_bucket_rates_apportions_by_overlap_and_skips_gaps() -> None:
    iv = [(0.0, 1.0, 10 * MB), (1.0, 2.5, 30 * MB), (2.5, 4.0, 0.0)]
    r = wl.bucket_rates(iv, 0.0, 4.0, 1.0)
    assert r == pytest.approx([10.0, 20.0, 10.0, 0.0])            # 1–2.5 s 的 30 MB 按 1:0.5 分到第 2、3 窗口
    assert wl.bucket_rates(iv, 0.0, 4.0, 2.0) == pytest.approx([15.0, 5.0])
    # 滑动窗口：2 s 窗、1 s 步长 → [0,2)、[1,3)、[2,4)
    assert wl.bucket_rates(iv, 0.0, 4.0, 2.0, 1.0) == pytest.approx([15.0, 15.0, 5.0])
    with pytest.raises(ValueError):
        wl.bucket_rates(iv, 0.0, 4.0, 2.5, 1.0)
    gap = [(0.0, 1.0, MB), (3.0, 4.0, MB)]                          # 1–3 s 无采样：不以 0 速率填充
    assert wl.bucket_rates(gap, 0.0, 4.0, 1.0) == pytest.approx([1.0, 1.0])
    assert wl.bucket_rates(iv, 0.0, 0.5, 1.0) == []


def test_rate_stats_p95_matches_numpy() -> None:
    xs = list(np.random.default_rng(3).gamma(2.0, 10.0, 3600))
    s = wl.rate_stats(xs)
    assert s["n"] == 3600 and s["p95"] == pytest.approx(np.percentile(xs, 95), abs=1e-3)
    assert s["max"] == pytest.approx(max(xs), abs=1e-3) and s["mean"] == pytest.approx(np.mean(xs), abs=1e-3)
    assert wl.rate_stats([])["p95"] is None


def test_coverage_counts_gaps() -> None:
    recs = [{"t": float(t)} for t in [*range(0, 50), *range(60, 101)]]
    assert wl.coverage(recs, 0.0, 100.0) == pytest.approx(0.89)
    assert wl.coverage(recs, 0.0, 100.0, max_gap_s=20.0) == pytest.approx(1.0)


def test_measured_1min_tail_of_samples(tmp_path: Path) -> None:
    p = tmp_path / "io_samples.jsonl"
    assert wl.measured_1min(p) is None
    lines = [json.dumps({"kind": "header", "schema": wl.SAMPLE_SCHEMA})]
    lines += [json.dumps({"t": float(t), "load": {"x": t * 25 * MB}, "proc": {}}) for t in range(0, 200)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert wl.measured_1min(p) == pytest.approx(25.0)
    assert wl.measured_1min(p, now=20.0) is None                     # 不足 30 s
    p.write_text("\n".join(lines[:11]) + "\n", encoding="utf-8")
    assert wl.measured_1min(p) is None


# ---------------------------------------------------------------------------------------------------------------- 写入器统计
def _rec_block(block_raw: float, block_truth: float, wall: float, *, dropped: int = 0, blocked: int = 0) -> dict:
    """`EnvRecorder.finish` 的清单 `recorder` 块（drone4d.bench.recorder_stats.v1）的相关字段。"""
    raw = {"bytes": 10, "messages": 5, "blocked": blocked, "block_s": block_raw, "block_max_s": block_raw, "max_depth": 3, "max_depth_bytes": 300,
           "write_s": 1.0, "wall_s": wall}
    tr = {**raw, "block_s": block_truth, "block_max_s": block_truth, "max_depth": 7}
    return {"raw": raw, "truth": tr, "writer_block_s": block_raw + block_truth, "writer_block_frac": (block_raw + block_truth) / wall,
            "dropped": dropped, "options": {"queue_msgs": 256}}


def test_recorder_brief_and_block_stats() -> None:
    b = wl.recorder_brief(_rec_block(0.5, 1.5, 1000.0, blocked=2))
    assert b["writer_block_s"] == 2.0 and b["writer_block_frac"] == pytest.approx(0.002) and b["blocked"] == 4 and b["max_depth"] == 7
    assert b["block_max_s"] == 1.5 and b["wall_s"] == 1000.0
    runs = [{"recorder": wl.recorder_brief(_rec_block(0.0, 0.0, 1000.0))}, {"recorder": b},
            {"recorder": wl.recorder_brief(_rec_block(10.0, 5.0, 1000.0, dropped=1))}, {"ok": False}]
    s = wl.block_stats(runs)
    assert s["n_runs"] == 3 and s["max_frac"] == pytest.approx(0.015) and s["aggregate_frac"] == pytest.approx(17.0 / 3000.0, abs=1e-6)
    assert s["dropped_total"] == 1 and s["block_max_s"] == 10.0
    assert wl.block_stats([])["max_frac"] is None


def test_synthetic_run_uses_run_writer_and_reports_blocking(tmp_path: Path) -> None:
    r = wl.synthetic_run(tmp_path / "a", sim_s=0.5, mbps=4.0, msg_kb=64)
    rec = r["recorder"]
    assert rec["dropped"] == 0 and rec["raw"]["mode"] == "async" and r["n_ticks"] == 5
    assert rec["raw"]["bytes"] + rec["truth"]["bytes"] >= 0.5 * 4.0 * MB    # 不可压缩载荷：文件 ≥ 载荷
    assert rec["writer_block_s"] == pytest.approx(rec["raw"]["block_s"] + rec["truth"]["block_s"], abs=1e-5)
    # 在途上限 1：每个 tick 的多条消息突发必然阻塞生产者
    r2 = wl.synthetic_run(tmp_path / "b", sim_s=0.3, mbps=40.0, msg_kb=64, queue_msgs=1)
    assert r2["recorder"]["raw"]["blocked"] + r2["recorder"]["truth"]["blocked"] > 0 and r2["recorder"]["writer_block_frac"] > 0
    assert r2["recorder"]["dropped"] == 0


def test_concurrency_time_weighted() -> None:
    runs = [{"t_admit": 0.0, "t_end": 50.0}, {"t_admit": 0.0, "t_end": 100.0}, {"t_admit": 50.0, "t_end": None}, {"t_admit": None}]
    c = wl.concurrency(runs, 0.0, 100.0)
    assert c["time_frac_by_n"] == {"2": 1.0} and c["mean"] == pytest.approx(2.0) and c["busy_frac"] == 1.0
    c2 = wl.concurrency([{"t_admit": 10.0, "t_end": 30.0}], 0.0, 100.0)
    assert c2["time_frac_by_n"] == {"0": 0.8, "1": 0.2} and c2["busy_frac"] == 0.2


def test_guard_exclusivity_levels() -> None:
    assert wl.guard_exclusivity(None)["counted"] is False
    a = wl.guard_exclusivity({"gpu": 1, "level": "A", "pre_free_min_mib": 40000, "run_free_min_mib": 30000, "run": {"external_pids": []}})
    assert a["counted"] is True and a["need_mib"] == 10000
    b = wl.guard_exclusivity({"gpu": 1, "level": "B", "pre_free_min_mib": 40000, "run_free_min_mib": 30000, "run": {"external_pids": ["1"]}})
    assert b["counted"] == "pending_user_confirmation" and b["b_free_ok"] is True
    b2 = wl.guard_exclusivity({"gpu": 1, "level": "B", "pre_free_min_mib": 12000, "run_free_min_mib": 2000})
    assert b2["counted"] is False and b2["b_free_ok"] is False
    assert wl.guard_exclusivity({"gpu": 1, "level": "invalid", "pre_free_min_mib": 1, "run_free_min_mib": 1})["counted"] is False


# ---------------------------------------------------------------------------------------------------------------- analyze
def _fake_out(tmp: Path, *, rate_mbps: float = 40.0, rate_fn=None, duration_s: float = 3600.0,
              block: tuple[float, float] = (0.2, 0.3), rc: int = 0, n_runs: int = 9, budget_measured: float | None = 892.6) -> Path:
    """构造一次负载的 load.json、io_samples.jsonl 与 Run 记录：`load` 口径第 i 秒写入 `rate_fn(i)`（缺省恒为 `rate_mbps`）MB。"""
    out = tmp / "d3" / "load"
    out.mkdir(parents=True)
    t0 = 1_800_000_000.0
    lines = [json.dumps({"kind": "header", "schema": wl.SAMPLE_SCHEMA, "period_s": 1.0, "disk": "8:0", "partition": "sda1", "data_path": "/data"})]
    cum = 0.0
    for i in range(int(duration_s) + 600):
        if i:
            cum += (rate_fn(i) if rate_fn else rate_mbps) * MB
        lines.append(json.dumps({"t": t0 + i, "load": {"host": int(cum)}, "proc": int(cum * 0.99), "d3": {"host": int(cum)},
                                 "dev_sectors_w": int(cum / 512 * 1.2)}))
    (out / wl.SAMPLES).write_text("\n".join(lines) + "\n", encoding="utf-8")
    budget = min(300.0, 0.5 * budget_measured) if budget_measured else 300.0
    runs = []
    for k in range(n_runs):
        ta = t0 + k * 400.0
        runs.append({"name": f"r{k:03d}", "seed": k + 1, "class": "eval", "est": {"write_mbps": 41.8}, "t_admit": ta, "utc_admit": wl.utc(ta),
                     "t_end": ta + 1200.0, "rc": rc if k == 0 else 0,
                     "info": {"ok": rc == 0 or k != 0, "wall_s": 1200.0, "rtf": 0.3, "mean_write_MBps": 4.5, "mcap_MB_per_sim_s": 15.0,
                              "bytes": {"run_dir_before_retention": 5_400_000_000},
                              "recorder": wl.recorder_brief(_rec_block(block[0], block[1], 1200.0))}})
    doc = {"schema": wl.RUN_SCHEMA, "job": {"worker": "env", "spec": "configs/bench.d3/x.json", "retention": "compact"},
           "scheduler": {"gpu": 1, "write_budget_mbps": budget, "budget_source": "measured" if budget_measured else "cap_only",
                         "measured_disk_mbps": budget_measured, "events": []},
           "window": {"t0": t0, "t1": t0 + duration_s, "duration_s": duration_s, "utc0": wl.utc(t0)}, "runs": runs}
    (out / wl.LOAD_DOC).write_text(json.dumps(doc), encoding="utf-8")
    return out


def test_analyze_passes_under_budget(tmp_path: Path) -> None:
    out = _fake_out(tmp_path)
    res, checks = wl.analyze(out, guard={"gpu": 1, "level": "B", "pre_free_min_mib": 40000, "run_free_min_mib": 33000})
    c = {x["id"]: x for x in checks}
    assert all(x["pass"] for x in checks)
    assert c["AC22-write-p95"]["value"] == pytest.approx(40.0, abs=1e-3) and c["AC22-write-p95"]["threshold"] == 300.0
    assert c["AC22-write-p95"]["counted"] == "pending_user_confirmation" and c["AC22-window"]["counted"] is True
    assert c["AC22-mcap-block"]["value"] == pytest.approx(0.5 / 1200.0, abs=1e-6)
    assert res["write_rate_MBps"]["load"]["60s"]["n"] == 355 and res["write_rate_MBps"]["load"]["10s"]["n"] == 360 and res["write_rate_MBps"]["dev"]["1s"]["p50"] == pytest.approx(48.0, rel=1e-3)
    assert res["proc_vs_load_bytes_ratio"] == pytest.approx(0.99, abs=1e-3) and res["budget"]["source"] == "measured"
    assert res["load"]["est_over_measured"] == pytest.approx(41.8 / 4.5, abs=0.01) and res["window"]["coverage"] == 1.0
    assert res["load"]["concurrency"]["mean"] == pytest.approx((400 * 1 + 400 * 2 + 2800 * 3) / 3600, abs=1e-3)   # 每 400 s 准入一个、各 1200 s


def test_analyze_judges_sustained_windows_not_writeback_batches(tmp_path: Path) -> None:
    # 回写批次：每 5 s 一次落盘 200 MB（均值 40 MB/s）：1 s p95 = 200 只记录，60 s p95 = 40，通过
    wb = _fake_out(tmp_path / "wb", rate_fn=lambda i: 200.0 if i % 5 == 0 else 0.0)
    res, checks = wl.analyze(wb)
    c = {x["id"]: x for x in checks}
    assert res["write_rate_MBps"]["load"]["1s"]["p95"] == pytest.approx(200.0) and c["AC22-write-p95"]["value"] == pytest.approx(40.0)
    assert c["AC22-write-p95"]["pass"] is True
    # 少数分钟超预算（每 20 分钟中 30 s 写 900 MB/s）：滑动 60 s p95 仍 ≤ 300
    rare = _fake_out(tmp_path / "rare", rate_fn=lambda i: 900.0 if i % 1200 < 30 else 40.0)
    assert {x["id"]: x["pass"] for x in wl.analyze(rare)[1]}["AC22-write-p95"] is True
    # 持续超预算的时段占 10%：p95 > 300，不通过
    often = _fake_out(tmp_path / "often", rate_fn=lambda i: 400.0 if i % 600 < 90 else 40.0)
    c = {x["id"]: x for x in wl.analyze(often)[1]}
    assert c["AC22-write-p95"]["pass"] is False and c["AC22-write-p95"]["value"] > 300.0
    assert c["AC22-write-p95"]["counted"] is False                                   # 无 guard：共享卡开发参考


def test_analyze_fails_on_blocking_short_window_and_failed_run(tmp_path: Path) -> None:
    blk = {x["id"]: x["pass"] for x in wl.analyze(_fake_out(tmp_path / "b", block=(8.0, 5.0)))[1]}   # 13 s / 1200 s > 1%
    assert blk["AC22-mcap-block"] is False and blk["AC22-write-p95"] is True
    short = {x["id"]: x["pass"] for x in wl.analyze(_fake_out(tmp_path / "s", duration_s=1800.0))[1]}
    assert short["AC22-window"] is False and short["AC22-write-p95"] is False
    bad = {x["id"]: x for x in wl.analyze(_fake_out(tmp_path / "f", rc=1))[1]}
    assert bad["AC22-runs-lossless"]["pass"] is False and bad["AC22-runs-lossless"]["value"]["failed"] == ["r000"]


def test_analyze_cli_writes_p1_part(tmp_path: Path) -> None:
    out = _fake_out(tmp_path)
    g = tmp_path / "guard.json"
    g.write_text(json.dumps({"gpu": 1, "level": "A", "pre_free_min_mib": 40000, "run_free_min_mib": 33000}), encoding="utf-8")
    assert wl.main(["analyze", "--out", str(out), "--guard", str(g)]) == wl.EXIT_OK
    doc = json.loads((out / "write_load.json").read_text(encoding="utf-8"))
    assert doc["schema"] == wl.pm.PART_SCHEMA and doc["part"] == "write_load" and all(c["counted"] is True for c in doc["checks"])
    assert doc["protocol"]["judged_windows"] == ["60s"] and doc["result"]["budget"]["budget_MBps"] == 300.0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert wl.main(["analyze", "--out", str(empty)]) == wl.EXIT_ENV


# ---------------------------------------------------------------------------------------------------------------- 采样器与端到端
def test_sampler_reads_cgroup_io_stat_and_proc_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cg = tmp_path / "docker-abc.scope"
    cg.mkdir()
    s = wl.Sampler(tmp_path, "d3-B23-")
    (cg / "io.stat").write_text(f"{s.disk} rbytes=0 wbytes=12345 rios=0 wios=3 dbytes=0 dios=0\n9:9 rbytes=0 wbytes=99 rios=0 wios=1\n")
    (cg / "cgroup.procs").write_text(f"{os.getpid()}\n")
    s.refresh_s = 0.0
    monkeypatch.setattr(wl, "list_containers", lambda prefix: {"abc": ("d3-B23-load", os.getpid())})
    monkeypatch.setattr(wl, "cgroup_dir_of", lambda pid: cg)
    rec = s.sample()
    assert rec["load"] == {"abc": 12345} and rec["d3"] == {"abc": 12345}
    assert rec["proc"] == 0 and rec["n_threads"] >= 1                   # 首次采样无增量
    with (tmp_path / "f.bin").open("wb") as f:                          # 本线程产生写入（write_bytes 在脏页时记账）
        f.write(os.urandom(1 << 20))
        f.flush()
        os.fsync(f.fileno())
    rec2 = s.sample()
    assert rec2["proc"] >= 0 and rec2["load"] == {"abc": 12345}
    assert rec["dev_sectors_w"] is None or rec["dev_sectors_w"] >= 0


def test_load_refuses_non_d3_directory(tmp_path: Path) -> None:
    assert wl.main(["load", "--out", str(tmp_path / "x"), "--worker", "synthetic", "--duration-s", "1"]) == wl.EXIT_ENV
    assert wl.main(["load", "--out", str(tmp_path / "d3" / "x"), "--duration-s", "1"]) == wl.EXIT_ARGS   # env 需 --events-root


def test_load_synthetic_end_to_end_through_scheduler(tmp_path: Path) -> None:
    out = tmp_path / "d3" / "load"
    rc = wl.main(["load", "--out", str(out), "--worker", "synthetic", "--synthetic-mbps", "4", "--synthetic-msg-kb", "64",
                  "--run-duration-s", "1.5", "--duration-s", "3", "--measured-disk-mbps", "892.6", "--start-grace-s", "20", "--tick-s", "0.2"])
    assert rc == wl.EXIT_OK and (out / wl.STOP).is_file()
    doc = json.loads((out / wl.LOAD_DOC).read_text(encoding="utf-8"))
    assert doc["scheduler"]["write_budget_mbps"] == 300.0 and doc["scheduler"]["budget_source"] == "measured"
    adm = [r for r in doc["runs"] if r.get("t_admit")]
    assert len(adm) >= 3 and all(r["rc"] == 0 and r["class"] == "eval" and r["gpu"] == 1 for r in adm)
    assert all(r.get("cancelled") for r in doc["runs"] if not r.get("t_admit"))
    # 单卡并发正式 Run ≤ 3（各 1/3 卡）
    ev = sorted([(r["t_admit"], 1) for r in adm] + [(r["t_end"], -1) for r in adm], key=lambda x: (x[0], x[1]))
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    assert peak <= 3 and all(r["share"] == pytest.approx(1 / 3) for r in adm)
    one = adm[0]["info"]
    assert one["ok"] and one["recorder"]["dropped"] == 0 and one["retention"]["mode"] == "compact" and one["retention"]["compacted"]
    run_dir = out / "runs" / adm[0]["name"]
    assert not (run_dir / "raw.mcap").exists() and (run_dir / "frames.digest").is_file()       # ranked_eval 精简
    assert "--gpus" in adm[0]["docker_args"] and "d3.slice/d3-eval.slice" in adm[0]["docker_args"]


def test_retention_compact_failure_falls_back_to_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import awr.bench.retention as ret

    def boom(*_a, **_k):                                            # dmax 实测：frames_digest 不识别 lidar-v2 编码（KeyError）
        raise KeyError("lidar-v2")
    monkeypatch.setattr(ret, "compact_ranked", boom)
    run = tmp_path / "runs" / "r001"
    (run / "truth").mkdir(parents=True)
    (run / "raw.mcap").write_bytes(b"not an mcap")
    (run / "truth" / "run_r001.mcap").write_bytes(b"x" * 100)
    (run / "manifest.json").write_text("{}", encoding="utf-8")
    r = wl._retention(run, tmp_path / "runs", "compact")
    assert r["compacted"] is False and r["error"] and r["fallback"]["deleted_bytes"] == 111
    assert not (run / "raw.mcap").exists() and not (run / "truth" / "run_r001.mcap").exists() and (run / "manifest.json").is_file()
    assert wl._retention(run, tmp_path / "runs", "keep") == {"mode": "keep"}
