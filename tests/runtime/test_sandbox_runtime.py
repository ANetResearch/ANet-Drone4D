"""D2 运行时库（M11-AC-210 与 M11-FR-201、FR-207、FR-216、FR-217；AWR-04 §4.4.3、§4.4.4、§4.8；17 §17.3.1、§17.10.1）：
RunBinding、沙盒鉴权原语（密钥文件、K_sbauth、沙盒 token、池控制面签名）、cgroup（以临时目录模拟 cgroup v2 文件系统）、
`runtime.yaml` 的 `sandbox:` 段与 public profile v2、procutil、`AWR_DETACHED`。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import yaml

from awr.runtime import cgroup, procutil
from awr.runtime.binding import RunBinding, is_sid
from awr.runtime.config import ConfigError, SandboxCfg, load_runtime_config, sandbox_from_env
from awr.runtime.principal import TokenInvalid
from awr.runtime.sbauth import (
    POOL_PRINCIPAL,
    SandboxScopeError,
    decode_sandbox_token,
    load_or_create_key,
    make_sandbox_token,
    peek_token_payload,
    pool_key,
    sandbox_auth_key,
    sign_pool_request,
    verify_pool_request,
)

SEVEN = ["synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco"]


# ---------------------------------------------------------------- RunBinding
def test_run_binding_shared_and_sandbox(tmp_path: Path) -> None:
    b = RunBinding.from_env({"AWR_RUN": "r20261005-010203-abcd", "AWR_WORLD": "synthcity", "AWR_RUN_DIR": str(tmp_path),
                             "AWR_SECRET_FILE": str(tmp_path / "secret"), "AWR_PERSIST_DIR": str(tmp_path / "p")})
    assert b.kind == "shared" and b.namespace == "awr/synthcity/r20261005-010203-abcd"
    assert b.ring_path == tmp_path / "state.sim-core" and b.tick_hz == 250 and b.clock_profile == "d1_250"
    sb = RunBinding.make("sb-abcdefghij", "shenzhen", secret_path=tmp_path / "s", shm_root=tmp_path)
    assert sb.is_sandbox and sb.clock_profile == "sandbox100" and sb.tick_hz == 100
    assert sb.namespace == "awr/shenzhen/sb-abcdefghij" and sb.run_dir == tmp_path / "sb-abcdefghij"
    back = RunBinding.from_msg(sb.to_msg())
    assert back == sb
    sw = sb.with_world("synthcity")
    assert sw.namespace == "awr/synthcity/sb-abcdefghij" and sw.ring_path == sb.ring_path
    assert is_sid("sb-abcdefghij") and not is_sid("sb-abcdefghi9") and not is_sid("sb-ABCDEFGHIJ")


@pytest.mark.parametrize("bad", [{"run_id": "../x", "world_id": "synthcity"}, {"run_id": "sb-abcdefghij", "world_id": "a/b"},
                                 {"run_id": "sb-abcdefghij", "world_id": "synthcity", "namespace": "awr/other/sb-abcdefghij"}])
def test_run_binding_rejects_untrusted_strings(bad: dict) -> None:
    with pytest.raises(ValueError):
        RunBinding.from_msg(bad)


# ---------------------------------------------------------------- 沙盒鉴权原语
def test_key_file_created_once_0600_and_shared(tmp_path: Path) -> None:
    p = tmp_path / "sbx" / "token.key"
    keys: list[bytes] = []
    ts = [threading.Thread(target=lambda: keys.append(load_or_create_key(p))) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(set(keys)) == 1 and len(keys[0]) == 32
    assert (p.stat().st_mode & 0o777) == 0o600 and (p.parent.stat().st_mode & 0o777) == 0o700
    assert load_or_create_key(p) == keys[0]  # 重启后不变
    assert not [x for x in p.parent.iterdir() if x.name.startswith(".")]  # 临时文件已清理
    (tmp_path / "short.key").write_bytes(b"x")
    with pytest.raises(ValueError):
        load_or_create_key(tmp_path / "short.key")


def test_sandbox_token_roundtrip_and_errors() -> None:
    k = sandbox_auth_key(b"\x01" * 32)
    assert k == sandbox_auth_key(b"\x01" * 32) and k != sandbox_auth_key(b"\x02" * 32)
    now = 1_800_000_000
    tok, p = make_sandbox_token(k, principal_id="p-a", sid="sb-abcdefghij", mode="public", exp_unix_s=now + 60, now_s=now)
    assert p["scope"] == "sandbox" and p["sid"] == p["run"] == "sb-abcdefghij" and p["role"] == "operator"
    assert decode_sandbox_token(tok, k, sid="sb-abcdefghij", now_s=now)["sub"] == "p-a"
    assert peek_token_payload(tok)["sid"] == "sb-abcdefghij" and peek_token_payload("x.y") is None
    with pytest.raises(SandboxScopeError):
        decode_sandbox_token(tok, k, sid="sb-bbbbbbbbbb", now_s=now)
    with pytest.raises(TokenInvalid) as ei:
        decode_sandbox_token(tok, sandbox_auth_key(b"\x02" * 32), sid="sb-abcdefghij", now_s=now)
    assert not isinstance(ei.value, SandboxScopeError)
    with pytest.raises(TokenInvalid):
        decode_sandbox_token(tok, k, now_s=now + 61)
    assert decode_sandbox_token(tok, k, now_s=now + 61, grace_s=600)  # 快照导出的宽限（SB-11）
    for bad in ("", "v2.a.b", "v1.a", tok + "x"):
        with pytest.raises(TokenInvalid):
            decode_sandbox_token(bad, k, now_s=now)


def test_pool_request_signature_window_and_principal() -> None:
    kp = pool_key(b"\x03" * 32)
    m = sign_pool_request(kp, "create", {"world": "synthcity", "prefix_h": "ab"}, now_s=1000.0)
    assert m["principal"] == POOL_PRINCIPAL and verify_pool_request(kp, "create", m, now_s=1003.0)
    assert not verify_pool_request(kp, "list", m, now_s=1000.0)            # op 进入签名：不能跨 op 重放
    assert not verify_pool_request(kp, "create", m, now_s=1006.0)          # ±5 s
    assert not verify_pool_request(pool_key(b"\x04" * 32), "create", m, now_s=1000.0)
    t = dict(m, args={"world": "shanghai", "prefix_h": "ab"})
    assert not verify_pool_request(kp, "create", t, now_s=1000.0)
    other = sign_pool_request(kp, "create", {}, now_s=1000.0, principal="svc:other")
    assert not verify_pool_request(kp, "create", other, now_s=1000.0)
    # 键顺序不影响签名（规范 msgpack）
    m2 = sign_pool_request(kp, "x", {"b": 1, "a": {"y": 2, "x": 1}}, cid="c1", now_s=5.0)
    m3 = dict(m2, args={"a": {"x": 1, "y": 2}, "b": 1})
    assert verify_pool_request(kp, "x", m3, now_s=5.0)


# ---------------------------------------------------------------- cgroup（模拟文件系统）
@pytest.fixture
def cgfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "unit"
    for d in ("core", "sandbox/ctl", "sandbox/plan", "sandbox/sb"):
        (root / d).mkdir(parents=True)
    (root / "sandbox" / "cpu.stat").write_text("usage_usec 100\nnr_periods 20\nnr_throttled 3\nthrottled_usec 7\n")
    (root / "sandbox" / "memory.stat").write_text("anon 1048576\nshmem 4096\nfile 99\n")
    (root / "sandbox" / "memory.current").write_text("2097152\n")
    (root / "sandbox" / "memory.pressure").write_text("some avg10=1.50 avg60=0.20 avg300=0.00 total=12\n"
                                                      "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    monkeypatch.setenv("AWR_CGROUP_FS", str(tmp_path))
    monkeypatch.setenv("AWR_CGROUP_ROOT", "/unit")
    monkeypatch.setenv("AWR_CGROUP_SIMULATE", "1")
    return root


def test_cgroup_join_leaf_limits_read_remove(cgfs: Path) -> None:
    assert cgroup.unit_root() == cgfs and cgroup.delegated()
    assert cgroup.join("sandbox/ctl") and (cgfs / "sandbox/ctl/cgroup.procs").read_text() == str(os.getpid())
    assert cgroup.join_from_env({"AWR_CGROUP": "sandbox/ctl", "AWR_CGROUP_FS": str(cgfs.parent),
                                 "AWR_CGROUP_ROOT": "/unit"}) is True
    assert cgroup.join_from_env({}) is None
    leaf = cgroup.create_leaf("sandbox/sb/sb-abcdefghij", {"cpu_weight": 100, "memory_high": "230M", "memory_max": 320 << 20})
    assert leaf == cgfs / "sandbox/sb/sb-abcdefghij"
    assert (leaf / "cpu.weight").read_text() == "100" and (leaf / "memory.high").read_text() == "230M"
    assert (leaf / "memory.max").read_text() == str(320 << 20)
    assert cgroup.set_limits("sandbox", cpu_max=1.36) and (cgfs / "sandbox/cpu.max").read_text() == "136000 100000"
    assert cgroup.set_limits("sandbox/plan", cpu_max="max") and (cgfs / "sandbox/plan/cpu.max").read_text() == "max"
    st = cgroup.read("sandbox")
    assert st["cpu_stat"]["nr_throttled"] == 3 and st["memory_stat"]["anon"] == 1048576 and st["memory_current"] == 2097152
    assert st["memory_pressure"]["some"]["avg10"] == 1.5 and st["cpu_pressure"] == {}
    assert cgroup.remove_leaf("sandbox/sb/sb-abcdefghij") and not leaf.exists()
    assert cgroup.leaf_path("../escape") is None and not cgroup.join("../escape")


def test_cgroup_without_delegation_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWR_CGROUP_FS", str(tmp_path))
    monkeypatch.setenv("AWR_CGROUP_ROOT", "/missing")
    monkeypatch.delenv("AWR_CGROUP_SIMULATE", raising=False)
    assert cgroup.join("sandbox/ctl") is False and cgroup.create_leaf("sandbox/sb/x") is None
    assert cgroup.read("sandbox") is None and not cgroup.delegated()


def test_cgroup_unit_root_from_proc_self(tmp_path: Path) -> None:
    f = tmp_path / "cg"
    f.write_text("0::/system.slice/anet-drone4d.service/sandbox/ctl\n")
    env = {"AWR_CGROUP_FS": "/sys/fs/cgroup"}
    assert cgroup.unit_root(env, proc_file=f) == Path("/sys/fs/cgroup/system.slice/anet-drone4d.service")
    f.write_text("0::/system.slice/anet-drone4d.service/core\n")
    assert cgroup.unit_root(env, proc_file=f) == Path("/sys/fs/cgroup/system.slice/anet-drone4d.service")
    f.write_text("12:cpu:/x\n")
    assert cgroup.unit_root(env, proc_file=f) is None


# ---------------------------------------------------------------- 配置
def _write(tmp_path: Path, mutate) -> Path:
    from awr.runtime.config import DEFAULT_CONFIG_PATH

    d = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(d)
    p = tmp_path / "rt.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    return p


def test_sandbox_section_defaults_and_formulas() -> None:
    c = load_runtime_config(profile="dev", env={})
    sb = c.sandbox
    assert sb.enabled is False and sb.max_sessions == "auto" and sb.max_sessions_hard == 6
    assert [sb.n_max(2.5), sb.n_max(2.0), sb.n_max(2.5, delegated=False), sb.n_max(8.0)] == [4, 2, 3, 6]
    assert sb.ws_max_for(4) == 16 and sb.ws_max_for(6) == 24 and sb.ws_max_for(0) == 4
    assert sb.limits.vehicles == 12 and sb.limits.targets == 30 and sb.event_ring == 8192
    assert sb.lifetime.max_s == 7200 and sb.queue.max == 20 and sb.rest.setpoint_hz == 50
    assert sb.effective_worlds([]) is None and sb.effective_worlds(["synthcity"]) == ["synthcity"]
    assert c.sandbox_port_effective == 8002 and "sandbox-api" not in [p.name for p in c.enabled_procs()]
    loc = load_runtime_config(profile="sandbox", env={})
    assert loc.sandbox.enabled and loc.sandbox.batch.enabled and "sandbox-pool" in [p.name for p in loc.enabled_procs()]
    ci = load_runtime_config(profile="ci", env={})
    assert ci.sandbox_port_effective == 8092


@pytest.mark.parametrize(("mutate", "path"), [
    (lambda d: d["sandbox"].update(nope=1), "sandbox.nope"),
    (lambda d: d["sandbox"]["limits"].update(vehicles="many"), "sandbox.limits.vehicles"),
    (lambda d: d["sandbox"].update(max_sessions=9), "sandbox"),
    (lambda d: d["sandbox"].update(worlds_allow=["Bad World"]), "sandbox.worlds_allow"),
    (lambda d: d["sandbox"].update(rate_tiers=[2, 1]), "sandbox.rate_tiers"),
])
def test_sandbox_section_rejects_bad_values(tmp_path: Path, mutate, path: str) -> None:
    p = _write(tmp_path, mutate)
    with pytest.raises(ConfigError) as ei:
        load_runtime_config(p, profile="dev", env={})
    assert ei.value.exit_code == 2 and ei.value.path.startswith(path)


def test_sandbox_env_overrides_and_effective_config(tmp_path: Path) -> None:
    env = {"AWR_SANDBOX_ENABLED": "1", "AWR_SANDBOX_MAX_SESSIONS": "3", "AWR_SANDBOX_PORT": "9002",
           "AWR_SANDBOX_STATE_DIR": str(tmp_path / "sbx")}
    c = load_runtime_config(profile="dev", env=env)
    assert c.sandbox.enabled and c.sandbox.max_sessions == 3 and c.sandbox.n_max(2.5) == 3
    assert c.sandbox.state_path() == tmp_path / "sbx"
    with pytest.raises(ConfigError):
        load_runtime_config(profile="dev", env={"AWR_SANDBOX_MAX_SESSIONS": "lots"})
    # 子进程从 supervisor 写出的生效配置读 sandbox 段（AWR_SANDBOX_* 最后覆盖）
    (tmp_path / "effective-config.yaml").write_text(yaml.safe_dump(c.effective_dict(), allow_unicode=True), encoding="utf-8")
    sb = sandbox_from_env({"AWR_PERSIST_DIR": str(tmp_path), "AWR_SANDBOX_PORT": "9102",
                           "AWR_SANDBOX_WORLDS_ALLOW": "synthcity,shanghai"})
    assert sb.max_sessions == 3 and sb.port == 9102 and sb.worlds_allow == ["synthcity", "shanghai"]
    assert sandbox_from_env({}) == SandboxCfg()
    eff = c.effective_dict()
    assert eff["derived"]["sandbox_port_effective"] == 9002


def test_supervisor_env_for_sandbox_layer(tmp_path: Path) -> None:
    c = load_runtime_config(profile="public", env={"AWR_ORIGINS": "https://drone4d.agentnetwork.org.cn"})
    env = procutil.sandbox_env(c, tmp_path)
    assert env["AWR_SANDBOX_STATE_DIR"] == "/opt/anet-drone4d/var/sandbox" and env["AWR_SANDBOX_PORT"] == "18641"
    assert env["AWR_SANDBOX_ENABLED"] == "1" and env["AWR_SANDBOX_WORLDS_ALLOW"] == ",".join(SEVEN)
    assert env["AWR_RENDEZVOUS"] == "tcp/127.0.0.1:18647"
    ce = procutil.config_env(c)
    assert ce["AWR_ACCESS_MODE"] == "public" and ce["AWR_WS_MAX"] == "24" and ce["AWR_WORLDS_ALLOW"] == ",".join(SEVEN)


# ---------------------------------------------------------------- procutil
def test_procutil_files_identity_and_sampling(tmp_path: Path) -> None:
    p = tmp_path / "secret"
    procutil.write_private(p, b"abc")
    assert p.read_bytes() == b"abc" and (p.stat().st_mode & 0o777) == 0o600
    q = tmp_path / "state.json"
    procutil.atomic_write(q, b"{}")
    assert q.read_bytes() == b"{}" and not (tmp_path / ".state.json.tmp").exists()
    me = os.getpid()
    assert procutil.pid_alive(me) and not procutil.pid_alive(0)
    assert procutil.starttime(me) is not None and procutil.starttime(2**22 + 12345) is None
    os.environ["AWR_TEST_ENV_PROBE"] = "1"
    assert procutil.environ_of(me, ["PATH"]).get("PATH")
    s = procutil.sample(me)
    assert s is not None and s[1] > 1
    fd = procutil.pidfd_open(me)
    if fd is not None:
        os.close(fd)
    assert procutil.hb_age_ms(tmp_path / "hb.none", me, 0) is None
    age, ring = procutil.ring_age_ms(None, tmp_path / "state.none", 0)
    assert age is None and ring is None


def test_spawn_logged_detached_with_cgroup_wrapper(tmp_path: Path, cgfs: Path) -> None:
    leaf = cgroup.create_leaf("sandbox/sb/sb-abcdefghij")
    log = tmp_path / "logs" / "child.log"

    async def go() -> tuple[int, str]:
        p = await procutil.spawn_logged([sys.executable, "-c", "import os,sys; print('hi', os.getpgid(0) == os.getpid());"
                                         "sys.stderr.write('err\\n')"], env=dict(os.environ), log_path=log,
                                        cgroup_dir=leaf, nice=5)
        rc = await p.wait()
        return rc, log.read_text()

    rc, text = asyncio.run(go())
    assert rc == 0 and "hi True" in text and "err" in text
    assert (leaf / "cgroup.procs").read_text().strip() == "0"  # sh 包装在 exec 之前写入（模拟目录中只记录内容）

    async def kill() -> bool:
        p = await procutil.spawn_logged([sys.executable, "-c", "import time; time.sleep(30)"], env=dict(os.environ),
                                        log_path=log)

        async def wait_exit(t: float) -> bool:
            try:
                await asyncio.wait_for(p.wait(), t)
                return True
            except TimeoutError:
                return False

        return await procutil.signal_and_wait(p.pid, wait_exit, grace_s=2.0)

    assert asyncio.run(kill())


# ---------------------------------------------------------------- AWR_DETACHED
CHILD = textwrap.dedent("""
    import json, os, sys
    from awr.runtime.child import init_child
    ctx = init_child("sim-core", setup_log=False)
    print(json.dumps({"detached": ctx.detached, "supervised": ctx.supervised, "omp": os.environ.get("OMP_NUM_THREADS")}))
    sys.stdout.flush()
""")


def test_detached_child_survives_foreign_supervisor_pid_and_logs_to_file(tmp_path: Path) -> None:
    log = tmp_path / "sim-core.log"
    env = {k: v for k, v in os.environ.items() if k not in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}
    # 受监管但父进程不是 supervisor：普通子进程以退出码 3 退出（孤儿自检）
    r = subprocess.run([sys.executable, "-c", CHILD], env=env | {"AWR_SUPERVISOR_PID": "1"}, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 3
    # 脱离模式：不设 PDEATHSIG、不做孤儿自检，BLAS 缺省补 1，输出写 AWR_LOG_FILE
    r = subprocess.run([sys.executable, "-c", CHILD], env=env | {"AWR_SUPERVISOR_PID": "1", "AWR_DETACHED": "1",
                                                                 "AWR_LOG_FILE": str(log)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    line = [x for x in log.read_text().splitlines() if x.startswith("{")][-1]
    assert yaml.safe_load(line) == {"detached": True, "supervised": True, "omp": "1"}
    assert (log.stat().st_mode & 0o777) == 0o600


def test_detached_child_keeps_running_after_parent_exit(tmp_path: Path) -> None:
    """拉起者（模拟池）退出后脱离模式的子进程继续运行（M17-FR-025；池崩溃不连带终止沙盒）。"""
    marker = tmp_path / "alive"
    child = (f"import time, pathlib\nfrom awr.runtime.child import init_child\n"
             f"init_child('sim-core', setup_log=False)\n"
             f"for _ in range(100):\n    pathlib.Path({str(marker)!r}).write_text(str(time.time()))\n    time.sleep(0.05)\n")
    parent = (f"import subprocess, sys, os\n"
              f"p = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True,"
              f" stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,"
              f" env=dict(os.environ, AWR_DETACHED='1', AWR_SUPERVISOR_PID=str(os.getpid())))\n"
              f"print(p.pid)\n")
    r = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=60)
    pid = int(r.stdout.strip())
    try:
        time.sleep(1.0)
        assert procutil.pid_alive(pid)
        t1 = float(marker.read_text())
        time.sleep(0.3)
        assert float(marker.read_text()) > t1
    finally:
        with __import__("contextlib").suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
