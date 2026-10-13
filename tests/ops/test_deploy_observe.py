"""tools/deploy/emax/observe.py：公开站线上观测（D2-AC-27）在模拟环境中的判据与采样。

本机没有 emax：用普通目录模拟 cgroupfs（单元根、core/、sandbox/、会话叶子组）、假的 systemctl、journalctl、`awr status --json` 与
`sbctl.sh --json status`，回环 HTTP 桩提供 show-api 的 `/api/auth/token`、`/api/events` 与 sandbox-api 健康；时钟由测试推进。
覆盖：干净运行通过；OOM（memory.events 与内核日志）、非预期的单元重启与子进程退避重启、共享展示 RTF 低于 0.99、S7 被发现均判不通过；
发布切换与维护窗口内的重启不计；重启与 epoch 变化后的预热窗口不计 RTF；时长不足、覆盖不足、事件游标过期为"未完成"；续跑、
按世界的会话成本、口令不出现在命令行。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("observe", ROOT / "tools" / "deploy" / "emax" / "observe.py")
observe = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["observe"] = observe  # dataclass 在 `from __future__ import annotations` 下按模块名解析注解
_spec.loader.exec_module(observe)

CG = "/system.slice/anet-drone4d.service"
SID_A, SID_B = "sb-aaaaaaaaaa", "sb-bbbbbbbbbb"


class Events:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.gone_once = False
        self.tokens = 0

    def add(self, kind: str, **data) -> None:
        self.items.append({"seq": len(self.items) + 1, "type": kind, "data": data})


class Stub(BaseHTTPRequestHandler):
    ev: Events
    health: ClassVar[dict] = {"ok": True, "pool": "up", "sessions": 1, "n_max": 4, "uptime_s": 1.0}

    def log_message(self, *a) -> None:
        pass

    def _send(self, st: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(st)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        assert self.headers.get("Host") == "drone4d.agentnetwork.org.cn"
        if self.path == "/api/auth/token" and body.get("role") == "viewer":
            self.ev.tokens += 1
            return self._send(200, {"token": f"tok{self.ev.tokens}"})
        return self._send(404, {})

    def do_GET(self) -> None:
        if self.path.startswith("/api/sandbox/v1/health"):
            return self._send(200, self.health)
        if self.path.startswith("/api/events"):
            if self.headers.get("Authorization", "") != f"Bearer tok{self.ev.tokens}":
                return self._send(401, {})
            q = dict(x.split("=", 1) for x in self.path.split("?", 1)[1].split("&"))
            assert "types" not in q  # 游标必须覆盖全部事件
            since = int(q["since"])
            if self.ev.gone_once:
                self.ev.gone_once = False
                return self._send(410, {"code": 319, "detail": {"since": since, "oldest_seq": len(self.ev.items) + 1}})
            items = [e for e in self.ev.items if e["seq"] > since][:1000]
            return self._send(200, {"items": items, "next_since": items[-1]["seq"] if items else since, "oldest_seq": 1})
        return self._send(404, {})


@pytest.fixture
def server():
    ev = Events()
    handler = type("H", (Stub,), {"ev": ev})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", ev
    finally:
        srv.shutdown()
        srv.server_close()


class FakeHost:
    """单元、cgroupfs、supervisor 与沙盒池的模拟状态；tick(dt) 推进时钟并累加计数器。"""

    def __init__(self, tmp: Path) -> None:
        self.t0 = 1_791_000_000.0
        self.t = self.t0
        self.cgfs = tmp / "cgfs"
        self.root = self.cgfs / CG.lstrip("/")
        self.inv = "inv-1"
        self.release = "20261007-0100-abc"
        self.prefix = tmp / "opt"
        (self.prefix / "releases" / self.release).mkdir(parents=True)
        (self.prefix / "current").symlink_to(self.prefix / "releases" / self.release)
        self.t_sim = 0.0
        self.epoch = 3
        self.rtf = 1.0
        self.clock = "PLAYING"            # awr status 的 ring.state（TIMESTATE_NAMES）
        self.hung = False                 # sim-core 卡死：t_sim 不走、心跳不更新，自报 rtf 停在最后的值
        self.hb_t = self.t
        self.restarts = {"sim-core": 0, "show-api": 0, "sandbox-pool": 0, "sandbox-api": 0}
        self.oom_kill = 0
        self.kernel: list[str] = []
        self.usage = {"": 0, "core": 0, "sandbox": 0, "sandbox/sb": 0, "sandbox/plan": 0, "sandbox/ctl": 0}
        self.leaves = {SID_A: 0}
        self.cores = {"core": 0.45, "sandbox/plan": 0.02, "sandbox/ctl": 0.05}
        self.leaf_cores = {SID_A: 0.6, SID_B: 0.8}
        self.sessions = [{"sid": SID_A, "state": "ACTIVE", "world": "synthcity", "epoch": 1, "granted_rate": 1, "anon_mb": 140.0}]
        self.calls: list[tuple] = []
        self.write()

    def write(self) -> None:
        for g in self.usage:
            d = self.root / g if g else self.root
            d.mkdir(parents=True, exist_ok=True)
            (d / "cpu.stat").write_text(f"usage_usec {self.usage[g]}\nuser_usec 0\nsystem_usec 0\nnr_periods 0\nnr_throttled 0\n"
                                        "throttled_usec 0\n")
            (d / "memory.current").write_text("500000000\n")
            (d / "memory.peak").write_text("900000000\n")
            (d / "memory.stat").write_text("anon 400000000\nshmem 50000000\nfile 100\n")
            ok = self.oom_kill if not g else 0
            (d / "memory.events").write_text(f"low 0\nhigh 0\nmax 0\noom {ok}\noom_kill {ok}\noom_group_kill 0\n")
        sb = self.root / "sandbox" / "sb"
        for sid in list(p.name for p in sb.iterdir() if p.name.startswith("sb-")):
            if sid not in self.leaves:
                for f in (sb / sid).iterdir():
                    f.unlink()
                (sb / sid).rmdir()
        for sid, u in self.leaves.items():
            (sb / sid).mkdir(exist_ok=True)
            (sb / sid / "cpu.stat").write_text(f"usage_usec {u}\n")
            (sb / sid / "memory.current").write_text("150000000\n")

    def tick(self, dt: float) -> None:
        self.t += dt
        if not self.hung:
            self.t_sim += dt * self.rtf
            self.hb_t = self.t
        for g, c in self.cores.items():
            self.usage[g] += int(c * dt * 1e6)
        for sid in self.leaves:
            self.leaves[sid] += int(self.leaf_cores[sid] * dt * 1e6)
        self.usage["sandbox/sb"] = sum(self.leaves.values())
        self.usage["sandbox"] = self.usage["sandbox/sb"] + self.usage["sandbox/plan"] + self.usage["sandbox/ctl"]
        self.usage[""] = self.usage["core"] + self.usage["sandbox"]

    def run(self, argv, timeout, env=None, user=None):
        self.calls.append((list(argv), env, user))
        if argv[0] == "systemctl":       # 每次采样先查单元：此时把计数器写进模拟的 cgroupfs（其余时刻不写，测试更快）
            self.write()
            return 0, (f"InvocationID={self.inv}\nNRestarts=0\nMainPID=4242\nActiveState=active\nSubState=running\n"
                       f"ControlGroup={CG}\n")
        if argv[0] == "journalctl":
            out, self.kernel = "\n".join(self.kernel), []
            return 0, out
        if argv[:2] == ["fake", "status"]:
            return 0, json.dumps({"run_id": "r1", "supervisor_pid": 4242,
                                  "ring": {"t_sim_s": round(self.t_sim, 3), "state": self.clock, "rate": 1.0, "epoch": self.epoch,
                                           "writer_pid": 4300, "rtf": round(self.rtf, 3), "step_p99_us": 900},
                                  "procs": [{"name": k, "state": "running", "pid": 1, "restarts": v, "last_exit": None}
                                            for k, v in self.restarts.items()]})
        if argv[:2] == ["fake", "pool"]:
            return 0, json.dumps({"n_max": 4, "q_cores": 2.5, "delegated": True, "accepting": True, "degrade_level": "L0",
                                  "queue_len": 0, "sessions": self.sessions})
        return 127, ""


class FakeRing:
    """进程内 StateRing 读数（observe.RingReader 的替身）：sim-core 自报的 1 s rtf、t_sim、epoch、时钟状态与心跳年龄。"""

    def __init__(self, host: FakeHost) -> None:
        self.h = host

    def read(self) -> dict:
        h = self.h
        return {"t_sim_s": h.t_sim, "epoch": h.epoch, "state": h.clock, "rate": 1.0, "rtf": h.rtf, "age_ms": (h.t - h.hb_t) * 1000.0}


class NoRing:
    def read(self) -> None:
        return None


def make_env(host: FakeHost, base: str, ring=None) -> observe.Env:
    return observe.Env(prefix=host.prefix, env_file=host.prefix / "env", show_base=base, sb_base=base, cgroup_fs=host.cgfs,
                       status_cmd=["fake", "status"], pool_cmd=["fake", "pool"], run=host.run, now=lambda: host.t,
                       mono=lambda: host.t - host.t0, is_root=False, ring=ring if ring is not None else FakeRing(host))


def go(host: FakeHost, base: str, out: Path, *, hours: float = 0.5, samples: int | None = None, script=None, ring=None) -> int:
    """script(k, host) 在第 k 个 10 s 区间开始时调用一次（k 从 1 起）。"""
    env = make_env(host, base, ring)
    seen = [int((host.t - host.t0) // 10)]

    def sleep(dt: float) -> None:
        k = int((host.t - host.t0) // 10) + 1
        if script and k != seen[0]:
            seen[0] = k
            script(k, host)
        host.tick(dt)

    return observe.run(env, out, hours=hours, interval=10.0, status_every=60.0, pool_every=60.0, events_every=30.0,
                       kernel_every=600.0, ring_every=0.5, threaded=False, max_samples=samples, sleep=sleep, log=lambda *_: None)


def rep(out: Path) -> dict:
    return json.loads((out / "report.json").read_text(encoding="utf-8"))


def test_clean_run_passes(tmp_path, server):
    base, ev = server
    host = FakeHost(tmp_path)
    ev.add("env.keyframe", calendar={"t_cal_unix_ns": 1})
    ev.add("relay.gap", tid="t1", cause="battery", duration_s=12.5)
    ev.add("relay.gap", tid="t1", cause="battery", duration_s=30.0)
    ev.add("relay.gap", tid="t1", cause="fov", duration_s=4.0)
    ev.add("target.discovered", target="tg-1")            # 没有我方机体：不计
    assert go(host, base, tmp_path / "o") == observe.EXIT_OK
    r = rep(tmp_path / "o")
    assert r["verdict"] == "pass", r["reasons"]
    assert r["oom"]["oom_kill"] == 0 and r["restarts"]["unexpected"] == 0
    f = r["show_rtf"]
    assert f["source"] == "ring" and f["windows"] >= 170 and f["seconds_samples"] >= 170
    assert f["min"] == pytest.approx(1.0, abs=1e-3) and f["stale_intervals"] == 0
    assert r["s7"]["discovered"] == 0 and not r["s7"]["complete_7d"]
    assert r["s7"]["gaps_by_cause"] == {"battery": {"count": 2, "total_s": 42.5, "max_s": 30.0},
                                         "fov": {"count": 1, "total_s": 4.0, "max_s": 4.0}}
    assert "env.keyframe" not in r["s7"]["event_counts"]          # 只保留观测需要的事件
    c = r["costs"]
    assert c["core"]["cores_mean"] == pytest.approx(0.45, abs=0.01)
    assert c["sessions_by_world"]["synthcity"]["cores_mean"] == pytest.approx(0.6, abs=0.01)
    assert c["unit"]["memory_peak_mb"] == pytest.approx(858.3, abs=0.1)
    assert c["n_max_seen"] == [4] and c["degrade_level_s"]["L0"] > 0
    lines = (tmp_path / "o" / "samples.jsonl").read_text().splitlines()
    assert len(lines) == 180 and json.loads(lines[0])["schema"] == observe.SCHEMA_SAMPLE


def test_short_or_sparse_run_is_incomplete(tmp_path, server):
    base, _ = server
    host = FakeHost(tmp_path)
    assert go(host, base, tmp_path / "o", hours=0.5, samples=60) == observe.EXIT_INCOMPLETE
    r = rep(tmp_path / "o")
    assert r["verdict"] == "incomplete" and any("观测" in x for x in r["reasons"])
    # 覆盖率：采样中断（观测进程停了 20 min）
    lines = (tmp_path / "o" / "samples.jsonl").read_text().splitlines()
    shifted = [json.loads(x) for x in lines]
    for s in shifted[30:]:
        s["t"] += 1200.0
        s["mono"] += 1200.0
    (tmp_path / "o" / "samples.jsonl").write_text("".join(json.dumps(s) + "\n" for s in shifted))
    r = observe.build_report(tmp_path / "o", required_hours=0.1)
    assert r["coverage"] < observe.COVERAGE_MIN and r["verdict"] == "incomplete"


def test_oom_fails(tmp_path, server):
    base, _ = server
    host = FakeHost(tmp_path)

    def script(n, h):
        if n == 50:
            h.oom_kill += 1

    assert go(host, base, tmp_path / "o", script=script) == observe.EXIT_FAIL
    r = rep(tmp_path / "o")
    assert r["oom"]["oom_kill"] == 1 and r["failed"] == ["oom"]


def test_kernel_oom_line_fails(tmp_path, server):
    base, _ = server
    host = FakeHost(tmp_path)
    host.kernel = ["1791000100.0 emax kernel: oom-kill:constraint=CONSTRAINT_MEMCG,oom_memcg=/system.slice/anet-drone4d.service/sandbox/sb/"
                   "sb-aaaaaaaaaa,task=python3,pid=999", "1791000100.0 emax kernel: oom-kill: other.service"]
    assert go(host, base, tmp_path / "o") == observe.EXIT_FAIL
    r = rep(tmp_path / "o")
    assert r["oom"]["kernel_count"] == 1 and r["oom"]["oom_kill"] == 0


@pytest.mark.parametrize("why", ["none", "release", "maintenance"])
def test_unit_restart(tmp_path, server, why):
    base, _ = server
    host = FakeHost(tmp_path)
    out = tmp_path / "o"

    def script(n, h):
        if n == 60:
            if why == "maintenance":
                observe.mark(out, 5, "手工重启", start=h.t, now=lambda: h.t)
            if why == "release":
                (h.prefix / "releases" / "r2").mkdir()
                (h.prefix / "current").unlink()
                (h.prefix / "current").symlink_to(h.prefix / "releases" / "r2")
            h.inv = "inv-2"
            h.epoch += 1
            h.t_sim = 0.0
            h.rtf = 0.5                     # 重启后的预热（不计）
        if n == 66:
            h.rtf = 1.0

    code = go(host, base, out, script=script)
    r = rep(out)
    assert len(r["restarts"]["unit"]) == 1
    if why == "none":
        assert code == observe.EXIT_FAIL and r["restarts"]["unexpected"] == 1 and r["failed"] == ["restarts"]
    else:
        assert code == observe.EXIT_OK, r
        assert r["restarts"]["unit"][0]["expected"] == why and r["show_rtf"]["ok"]


def test_child_backoff_restart_fails(tmp_path, server):
    base, _ = server
    host = FakeHost(tmp_path)

    def script(n, h):
        if n == 40:
            h.restarts["show-api"] += 2

    assert go(host, base, tmp_path / "o", script=script) == observe.EXIT_FAIL
    r = rep(tmp_path / "o")
    assert r["restarts"]["unexpected"] == 2 and r["restarts"]["children"][0]["proc"] == "show-api"


def test_rtf_dip_fails_but_soft_reset_warmup_does_not(tmp_path, server):
    base, _ = server
    host = FakeHost(tmp_path)

    def dip(n, h):
        if n == 100:
            h.rtf = 0.97
        if n == 112:
            h.rtf = 1.0

    assert go(host, base, tmp_path / "o", script=dip) == observe.EXIT_FAIL
    r = rep(tmp_path / "o")
    assert r["failed"] == ["show_rtf"] and r["show_rtf"]["min_1s"] == pytest.approx(0.97, abs=1e-6)
    assert r["show_rtf"]["min_window"] == pytest.approx(0.97, abs=2e-3) and r["show_rtf"]["below_count"] >= 2

    host2 = FakeHost(tmp_path / "b")

    def soft_reset(n, h):                    # 04:00 软重置：epoch + 1、t_sim 回零，随后 1 min 内偏慢
        if n == 100:
            h.epoch += 1
            h.t_sim = 0.0
            h.rtf = 0.9
        if n == 106:
            h.rtf = 1.0

    assert go(host2, base, tmp_path / "b" / "o", script=soft_reset) == observe.EXIT_OK


def test_paused_clock_is_excluded_but_stalled_counts(tmp_path, server):
    base, _ = server
    host = FakeHost(tmp_path)

    def pause(n, h):                          # 管理员暂停 5 min：t_sim 不走，不计
        if n == 60:
            h.clock, h.rtf = "PAUSED", 0.0
        if n == 90:
            h.clock, h.rtf = "PLAYING", 1.0

    assert go(host, base, tmp_path / "o", script=pause) == observe.EXIT_OK

    host2 = FakeHost(tmp_path / "b")

    def stall(n, h):                          # 仿真跟不上（STALLED）：计入并判不通过
        if n == 60:
            h.clock, h.rtf = "STALLED", 0.8
        if n == 72:
            h.clock, h.rtf = "PLAYING", 1.0

    assert go(host2, base, tmp_path / "b" / "o", script=stall) == observe.EXIT_FAIL
    assert rep(tmp_path / "b" / "o")["failed"] == ["show_rtf"]


def test_hung_sim_core_is_caught_by_the_heartbeat(tmp_path, server):
    """sim-core 卡死：自报 rtf 停在 1.0，但心跳不再更新、t_sim 不走：逐秒读数按 0 计，窗口 Δt_sim 为 0。"""
    base, _ = server
    host = FakeHost(tmp_path)

    def hang(n, h):
        if n == 80:
            h.hung = True
        if n == 83:
            h.hung = False

    assert go(host, base, tmp_path / "o", script=hang) == observe.EXIT_FAIL
    f = rep(tmp_path / "o")["show_rtf"]
    assert f["stale_intervals"] >= 1 and f["min_1s"] == 0.0 and f["min_window"] < 0.9


def test_without_the_ring_rtf_is_sparse_and_incomplete(tmp_path, server):
    """不能导入 awr（系统 python3）时：只用 awr status 的 ≥ 300 s 窗口，结论最多为未完成。"""
    base, _ = server
    host = FakeHost(tmp_path)
    assert go(host, base, tmp_path / "o", ring=NoRing()) == observe.EXIT_INCOMPLETE
    r = rep(tmp_path / "o")
    assert r["show_rtf"]["source"] == "status" and r["show_rtf"]["windows"] >= 4
    assert r["show_rtf"]["min_window"] == pytest.approx(1.0, abs=1e-3)
    assert any("稀疏" in x for x in r["reasons"])


def test_s7_discovery_fails_and_event_loss_is_incomplete(tmp_path, server):
    base, ev = server
    host = FakeHost(tmp_path)

    def script(n, h):
        if n == 30:
            ev.add("target.discovered", target="tg-1", uav="awr-v1-01", sensor="eo")

    assert go(host, base, tmp_path / "o", script=script) == observe.EXIT_FAIL
    r = rep(tmp_path / "o")
    assert r["s7"]["discovered"] == 1 and r["failed"] == ["s7"]

    host2 = FakeHost(tmp_path / "b")
    ev.items.clear()
    ev.gone_once = True
    assert go(host2, base, tmp_path / "b" / "o") == observe.EXIT_INCOMPLETE
    r = rep(tmp_path / "b" / "o")
    assert r["s7"]["events_lost"] and any("事件游标" in x for x in r["reasons"])


def test_resume_keeps_cursor_and_start(tmp_path, server):
    base, ev = server
    host = FakeHost(tmp_path)
    ev.add("relay.gap", tid="t1", cause="other", duration_s=5.0)
    out = tmp_path / "o"
    assert go(host, base, out, samples=90) == observe.EXIT_INCOMPLETE
    st = json.loads((out / "state.json").read_text())
    assert st["events_since"] == 1
    host.tick(10.0)
    ev.add("relay.gap", tid="t1", cause="other", duration_s=7.0)
    assert go(host, base, out) == observe.EXIT_OK           # 同一 OUT 续跑：起点不变，0.5 h 后结束
    r = rep(out)
    assert r["s7"]["gaps_by_cause"]["other"]["count"] == 2   # 不重复写入已拉取的事件
    assert json.loads((out / "state.json").read_text())["started_t"] == st["started_t"]


def test_sessions_by_world_and_epoch_bumps(tmp_path, server):
    base, _ = server
    host = FakeHost(tmp_path)

    def script(n, h):
        if n == 30:
            h.leaves[SID_B] = 0
            h.sessions = [*h.sessions, {"sid": SID_B, "state": "ACTIVE", "world": "shanghai", "epoch": 1, "granted_rate": 1,
                                        "anon_mb": 210.0}]
        if n == 90:
            h.sessions = [dict(s, epoch=s["epoch"] + 1) if s["sid"] == SID_B else s for s in h.sessions]

    go(host, base, tmp_path / "o", script=script)
    c = rep(tmp_path / "o")["costs"]
    assert c["sessions_by_world"]["shanghai"]["cores_mean"] == pytest.approx(0.8, abs=0.02)
    assert c["sessions_by_world"]["synthcity"]["cores_mean"] == pytest.approx(0.6, abs=0.02)
    assert c["sandbox_epoch_bumps"] == 1


def test_status_call_keeps_the_environment_off_the_command_line(tmp_path):
    (tmp_path / "env").write_text("AWR_PROFILE=public\nAWR_RUNS_DIR=/opt/anet-drone4d/var/runs\n# c\n"
                                  "AWR_SOME_SECRET=Zq9-not-for-argv\nAWR_ADMIN_SECRET_FILE=/etc/anet-drone4d/admin.secret\n")
    env = observe.Env(prefix=Path("/opt/anet-drone4d"), env_file=tmp_path / "env", is_root=True)
    argv, child, user = observe.status_call(env)
    assert argv == ["/opt/anet-drone4d/current/.venv/bin/python", "-m", "awr.runtime.cli", "status", "--json"]
    assert user == "awr"
    assert child["AWR_PROFILE"] == "public" and child["AWR_SOME_SECRET"] == "Zq9-not-for-argv"
    assert not any("Zq9" in a for a in argv)
    assert observe.status_call(observe.Env(env_file=tmp_path / "env", is_root=False))[2] is None


def test_cli_mark_and_report(tmp_path, server, capsys):
    base, _ = server
    host = FakeHost(tmp_path)
    out = tmp_path / "o"
    assert observe.main(["report", str(out)]) == observe.EXIT_ARGS
    go(host, base, out)
    assert observe.main(["mark", str(out), "--minutes", "10", "--note", "nginx reload"]) == observe.EXIT_OK
    capsys.readouterr()
    assert observe.main(["report", str(out), "--json"]) == observe.EXIT_OK
    r = json.loads(capsys.readouterr().out)
    assert r["schema"] == observe.SCHEMA_REPORT and r["maintenance"][0]["note"] == "nginx reload"
    assert observe.main(["report", str(out), "--hours", "72"]) == observe.EXIT_INCOMPLETE
    assert "D2-AC-27" in capsys.readouterr().out


def test_ring_reader_maps_the_shared_show_ring(tmp_path):
    """RingReader：按 `<runs>/current` 与 effective-config.yaml 的 run.shm_root 找到环文件，只读头部（不登记读者游标）；
    sim-core 重启换新环文件后按 inode 重新映射。"""
    from awr.contracts import LAYOUT_ID
    from awr.runtime.statering import StateRing

    shm, runs = tmp_path / "shm", tmp_path / "runs"
    run = runs / "r20261007-000000-abcd"
    run.mkdir(parents=True)
    (run / "effective-config.yaml").write_text(f"run:\n  shm_root: {shm}\n", encoding="utf-8")
    (runs / "current").symlink_to(run.name)
    ring_file = shm / run.name / "state.sim-core"
    w = StateRing.create(ring_file, layout_id=LAYOUT_ID, epoch=4)
    try:
        w.heartbeat(12_500_000_000, 1, 1000)
        w.set_step_stats(900, 1500, 2000, 4000, 995, 0)
        rr = observe.RingReader(runs)
        r = rr.read()
        assert r is not None, rr.errors
        assert r["state"] == "PLAYING" and r["epoch"] == 4 and r["rate"] == 1.0 and r["rtf"] == pytest.approx(0.995)
        assert r["t_sim_s"] == pytest.approx(12.5) and r["age_ms"] < 2000
        assert all(c["mode"] == 0 for c in w.reader_cursors())            # 没有占用读者游标
        w.close()
        ring_file.unlink()
        w = StateRing.create(ring_file, layout_id=LAYOUT_ID, epoch=5)     # sim-core 重启：新文件
        w.heartbeat(1_000_000_000, 1, 1000)
        assert rr.read()["epoch"] == 5
        rr.close()
        ring_file.unlink()
        assert rr.read() is None and rr.errors == 1                         # 环暂不存在：本次读数缺失，不抛异常
    finally:
        w.close()
