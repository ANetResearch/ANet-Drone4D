"""上线验收检查 verify.py 的本机检查与度量函数（tools/deploy/emax/d3-public；D3-AC-21）。站点检查在真实 nginx 上的用例见
test_d3_public_nginx_e2e.py。

覆盖：公开组合根检查（env 文件 AWR_D3_ENABLED、进程命令行中的 D3 进程、进程环境 AWR_D3_ENABLED=1；以合成 /proc 目录）；磁盘与内容
检查（labctl 布局）；稳态速率（nginx limit_rate 的"按秒补额度、预支 1 s"波形下斜率等于设定）；源地址区间解析；drill.py 的 WS
推送时延统计（时钟偏移取最小往返 pong、按阶段切分、p95 增幅判据、每轮增幅）、沙盒 RTF 与单步 p99 的按阶段对比、客户端核分配。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from d3public_fixtures import load, published, write  # noqa: E402

labctl = load("labctl")
verify = load("verify")
drill = load("drill")


def _proc(root: Path, pid: int, cmd: str, env: dict[str, str] | None) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
    if env is not None:
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


def test_check_root_env_file_processes_and_environ(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "docker_d3", lambda: [])
    monkeypatch.setattr(verify.os, "geteuid", lambda: 0)
    proc = tmp_path / "proc"
    _proc(proc, 101, "/opt/anet-drone4d/current/.venv/bin/python -m awr.runtime.supervisor --profile public", {"AWR_PROFILE": "public"})
    _proc(proc, 102, "/opt/anet-drone4d/current/.venv/bin/python -m awr.api.main", {"AWR_D3_ENABLED": "0"})
    _proc(proc, 103, "nginx: worker process", None)
    env = write(tmp_path, "env", "# public\nAWR_PROFILE=public\nAWR_D3_ENABLED=0\n")
    rep = verify.check_root(env, proc)
    assert rep["ok"], rep["problems"]
    assert rep["detail"]["AWR_D3_ENABLED"] == "0" and rep["detail"]["processes"] == 3
    env.write_text("AWR_D3_ENABLED=1\n", encoding="utf-8")
    assert any("AWR_D3_ENABLED=1" in p for p in verify.check_root(env, proc)["problems"])
    env.write_text("", encoding="utf-8")
    _proc(proc, 104, "python -m awr.api.bench_main --port 18650", {})
    _proc(proc, 105, "python -m awr.sim.core", {"AWR_D3_ENABLED": "1"})
    probs = verify.check_root(env, proc)["problems"]
    assert any("D3 进程" in p and "bench_main" in p for p in probs) and any("进程环境" in p for p in probs)
    monkeypatch.setattr(verify, "docker_d3", lambda: ["d3-gz-abcdefgh"])
    assert any("d3-*" in p for p in verify.check_root(env, proc)["problems"])


def test_check_disk_and_content(tmp_path):
    root = tmp_path / "srv"
    v, dist = published(labctl, tmp_path / "src", "2026.10.19-1")
    labctl.push(v, dist, str(root))
    labctl.activate(labctl.Layout(root), "2026.10.19-1")
    assert verify.check_disk(root)["ok"]
    c = verify.check_content(root)
    assert c["ok"] and c["detail"]["test_files"] == 0 and c["detail"]["truth_files"] == 0
    write(root / "lab-static" / "v" / "2026.10.19-1", "runs/test/leak.bin", b"x")
    assert not verify.check_content(root)["ok"]
    assert verify.check_content(tmp_path / "empty")["problems"] == ["state.json 中没有当前版本"]


def _nginx_marks(rate: float, after: int, total: int, *, bufd: int = 2 << 20, step: float = 0.05) -> list[tuple[float, int]]:
    """模拟 nginx limit_rate：前 after 字节立即可发，之后额度 = rate ×（整秒数 + 1）（预支 1 s），客户端另多见 bufd 字节缓冲。"""
    marks, t = [(0.0, 0)], 0.0
    t_after = None
    while marks[-1][1] < total:
        t += step
        sent = after if t_after is None else after + min(total - after, int(rate * (int(t - t_after) + 1)))
        if t_after is None:
            t_after = t
        marks.append((round(t, 3), min(total, sent + (bufd if sent < total else 0))))
    return marks


def test_steady_rate_matches_limit_despite_preallowance_and_buffers():
    mib = 1 << 20
    marks = _nginx_marks(2 * mib, 8 * mib, 32 * mib)
    naive = verify.rate_between(marks, 8 * mib + mib)
    steady = verify.steady_rate(marks, 8 * mib)
    assert naive > 2 * mib * 1.05                                   # 越过阈值后的头几秒平均速率高于设定
    assert abs(steady / (2 * mib) - 1) < 0.06
    assert verify.steady_rate(marks[:20], 8 * mib) is None          # 窗口不足
    assert verify.parse_ips("127.0.0.2-127.0.0.4,10.0.0.1") == ["127.0.0.2", "127.0.0.3", "127.0.0.4", "10.0.0.1"]


def test_ws_latency_offset_phases_and_gate():
    off = 5_000_000_000                                              # gw_t0（单调 ns）
    pongs = [(off + 100_000, off + 1_100_000, 600_000), (off + 2_000_000, off + 2_200_000, 2_100_000)]   # 第 2 个往返最小
    times, phases = [], []
    t = off + 10_000_000
    for kind, lat_ms, n in (("base", 10.0, 400), ("load", 10.8, 400), ("base", 10.0, 400), ("load", 10.6, 400)):
        t0 = t
        for i in range(n):
            srv = t - off - int(lat_ms * 1e6) + (i % 10) * 100_000
            times.append((0, t, srv))
            t += 100_000_000
        phases.append({"kind": kind, "t0_ns": t0, "t1_ns": t})
    rep = drill.ws_latency({"times": times, "pongs": pongs, "errors": []}, phases)
    assert rep["offset_rtt_ms"] == 0.2 and rep["ok"]
    assert 0.0 < rep["p95_increase"] <= 0.1 and rep["base"]["n"] == 800
    # 每轮增幅（相邻基线与下载阶段）：第 1 轮 +8 %、第 2 轮 +6 % 附近
    assert len(rep["round_increase"]) == 2 and rep["round_increase"][0] > rep["round_increase"][1] > 0.0
    worse = [(k, r, s - 3_000_000 if any(p["kind"] == "load" and p["t0_ns"] <= r < p["t1_ns"] for p in phases) else s)
             for k, r, s in times]
    assert not drill.ws_latency({"times": worse, "pongs": pongs, "errors": []}, phases)["ok"]
    assert drill.ws_latency({"times": [], "pongs": [], "errors": []}, phases)["ok"] is False


def test_drill_cpu_sets_keep_probe_off_download_cores():
    """WS 探针独占一个核，不与下载进程和演练进程共享；三者都不落在模拟 emax 的 0-3 号核上。"""
    assert drill.parse_cpus("5-7") == {5, 6, 7} and drill.parse_cpus("0,2-3") == {0, 2, 3}
    probe, main, dl = (drill.parse_cpus(x) for x in (drill.PROBE_CPUS, drill.MAIN_CPUS, drill.DL_CPUS))
    emax = drill.parse_cpus("0-3")
    assert not probe & (main | dl) and not (probe | main | dl) & emax and dl <= main


def test_d2_by_phase_separates_download_regression_from_host_noise():
    """沙盒 RTF 与单步 p99 按阶段切分：下载阶段与基线相当时无回退；下载阶段单步 p99 明显变大时判为回退。"""
    phases = [{"kind": k, "t0_ns": int(t0 * 1e9), "t1_ns": int((t0 + 10) * 1e9)} for k, t0 in (("base", 100), ("load", 110))]

    def samples(load_p99: float, load_rtf: float) -> list[dict]:
        out = []
        for i in range(20):
            t = 100.5 + i
            load = t >= 110
            ring = {"rtf": load_rtf if load else 0.99, "step_p99_us": load_p99 if load else 5000.0}
            out.append({"t": t, "sessions": {"sb-a": {"ring": ring}, "sb-b": {"ring": dict(ring)}}})
        return out

    ok = drill.d2_by_phase(samples(5200.0, 0.985), phases)
    assert ok["no_regression"] is True and [r["n"] for r in ok["phases"]] == [20, 20]
    assert ok["base"] == {"rtf_mean": 0.99, "step_p99_us_median": 5000.0}
    assert drill.d2_by_phase(samples(7000.0, 0.99), phases)["no_regression"] is False
    assert drill.d2_by_phase(samples(5000.0, 0.95), phases)["no_regression"] is False
    assert drill.d2_by_phase([], phases)["no_regression"] is None
