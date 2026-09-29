"""运维 CLI `awr`（M11-FR-018；AWR-19 §7.6、§14.1、§16.2）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rtlib
import yaml

from awr.contracts import LAYOUT_ID
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32
from awr.runtime import principal as P
from awr.runtime.statering import StateRing


def awr(*args: str, env: dict | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "awr.runtime.cli", *args], capture_output=True, text=True, timeout=timeout,
                          env=rtlib.child_env(**(env or {})))


def _free_offset() -> int:
    import socket

    for k in range(1, 9):
        ok = True
        for port in (8000 + 10 * k, 7447 + 10 * k, 5173 + 10 * k):
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                ok = False
            finally:
                s.close()
        if ok:
            return k
    pytest.skip("没有空闲的端口偏移")


def test_doctor_quick_json_passes() -> None:
    k = _free_offset()
    r = awr("doctor", "--quick", "--json", env={"AWR_PORT_OFFSET": str(k)})
    out = json.loads(r.stdout)
    ids = {c["id"]: c for c in out["checks"]}
    for cid in ("DOC-01", "DOC-02", "DOC-04", "DOC-08", "DOC-09", "DOC-10", "DOC-12", "DOC-22", "M11-R1", "M11-R2"):
        assert ids[cid]["level"] == "OK", ids[cid]
    assert r.returncode == 0 and out["ok"] is True and out["elapsed_ms"] < 3000  # --quick ≤ 3 s


def test_doctor_config_error_exit_2() -> None:
    r = awr("doctor", "--quick", env={"AWR_BIND": "0.0.0.0"})  # lan 模式缺 AWR_ORIGINS
    assert r.returncode == 2 and "net.origins" in r.stdout and "修复" in r.stderr


def test_config_print_masks_and_profile() -> None:
    r = awr("config", "print", "--profile", "ci")
    assert r.returncode == 0
    eff = yaml.safe_load(r.stdout)
    assert eff["profile"] == "ci" and eff["derived"]["port_effective"] == 8090
    r = awr("--profile", "field", "config", "print")
    assert r.returncode == 2


def test_ring_dump(shm_dir: Path) -> None:
    p = shm_dir / "state.sim-core"
    w = StateRing.create(p, capacity=16, slots=16, layout_id=LAYOUT_ID)
    try:
        full, lite = np.zeros(4, DRONE_STATE64), np.zeros(4, SWARM_LITE32)
        full["pos"][:, 0] = 5.0
        w.heartbeat(8_000_000, 1, 1000, step_seq=2)
        w.publish(full, lite, 8_000_000, 1)
        r = awr("ring", "dump", str(p))
        assert r.returncode == 0, r.stderr
        d = json.loads(r.stdout)
        assert d["header"]["head"] == 1 and d["header"]["step_seq"] == 2 and d["latest"]["n_rows"] == 4
        assert d["latest"]["first_rows"][0]["pos"][0] == 5.0
    finally:
        w.close()
    bad = shm_dir / "other"
    StateRing.create(bad, capacity=16, slots=16, layout_id=LAYOUT_ID ^ 1).close()
    assert awr("ring", "dump", str(bad)).returncode == 10  # STATE_INCOMPATIBLE


def _fake_run(runs: Path, run_id: str) -> Path:
    d = runs / run_id
    (d / "logs").mkdir(parents=True)
    (d / "secret").write_bytes(b"s" * 32)
    (d / "admin.token").write_text("pw\n")
    (d / "effective-config.yaml").write_text(yaml.safe_dump({"run": {"world": "shenzhen", "shm_root": "/dev/shm/awr"},
                                                              "derived": {"access_mode": "loopback"}}))
    (d / "logs" / "api.log").write_text("\n".join(json.dumps({"lvl": "INFO", "msg": "m", "kv": {"cid": f"c{i}"}})
                                                  for i in range(5)) + "\n")
    (runs / "current").symlink_to(run_id)
    return d


def test_runs_logs_token_status_without_supervisor(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run_id = "r20260101-000000-abcd"
    d = _fake_run(runs, run_id)
    env = {"AWR_RUNS_DIR": str(runs)}
    r = awr("runs", "ls", env=env)
    assert r.returncode == 0 and run_id in r.stdout and "*" in r.stdout
    assert awr("runs", "keep", run_id, env=env).returncode == 0 and (d / "keep").exists()
    assert awr("runs", "unkeep", run_id, env=env).returncode == 0 and not (d / "keep").exists()
    r = awr("runs", "gc", env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["evicted"] == []
    r = awr("logs", "api", "--tail", "2", env=env)
    assert r.returncode == 0 and len(r.stdout.splitlines()) == 2
    r = awr("logs", "api", "--grep", "cid=c3", env=env)
    assert r.returncode == 0 and r.stdout.count("c3") == 1
    r = awr("token", "viewer", env=env)
    assert r.returncode == 0
    payload = P.decode_token(r.stdout.strip(), P.derive_key(b"s" * 32, run_id, "auth"), run_id=run_id,
                             now_s=int(__import__("time").time()))
    assert payload["role"] == "viewer" and payload["mode"] == "loopback"
    assert awr("status", env=env).returncode == 12  # supervisor 不在运行：NOT_RUNNING
    assert awr("stop", env=env).returncode == 12
    assert awr("restart", "api", env=env).returncode == 12
    r = awr("runs", "verify", run_id, env=env)
    assert r.returncode == 1  # 尚无 SHA256SUMS


def test_external_subcommands_not_delivered() -> None:
    assert awr("backup").returncode == 8


def test_data_plugin_registered() -> None:
    # `awr data *` 由 M03 以插件 awr.jobs.awr_cli 注册（M03-M04 实现报告 §1.5）；插件存在时不再落到"未交付"桩
    r = awr("data", "--help")
    assert r.returncode == 0 and "fetch" in r.stdout


def test_data_fallback_when_plugin_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # 没有任何插件注册 data 时，CLI 保留退出码 8 的"未交付"桩（AWR-19 §16.2 DEP_MISSING）
    from awr.runtime import cli

    monkeypatch.setattr(cli, "_PLUGIN_MODULES", ())
    a = cli.build_parser().parse_args(["data", "fetch", "urbanscene3d"])
    assert a.fn(a) == 8


def test_console_script_installed() -> None:
    exe = Path(sys.executable).with_name("awr")
    assert exe.exists()
    r = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "doctor" in r.stdout
