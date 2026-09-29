"""configs/runtime.yaml 加载、profile 叠加与结构校验（M11-AC-006；M11-FR-015；OPS-AC-005）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from awr.runtime.config import DEFAULT_CONFIG_PATH, ConfigError, load_runtime_config


def write(tmp_path: Path, mutate) -> Path:
    d = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(d)
    p = tmp_path / "rt.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    return p


@pytest.mark.parametrize("profile", ["dev", "demo", "ci", "perf"])
def test_repo_config_loads_for_every_profile(profile: str) -> None:
    c = load_runtime_config(profile=profile, env={})
    names = [p.name for p in c.procs]
    assert names[:2] == ["sim-core", "api"] and {"recorder", "agent-runtime", "job-worker", "replay-worker"} <= set(names)
    api = c.proc("api")
    assert "--ws-per-message-deflate" in api.cmd and "262144" in api.cmd and api.start_after == ["sim-core"]
    sim = c.proc("sim-core")
    assert sim.cpus == [1] and sim.nice == -5 and sim.liveness.mode == "ring" and sim.liveness.stale_s == 2.0
    assert c.proc("job-worker").nice == 10 and c.restart_for(c.proc("job-worker")).policy == "on-failure"
    assert c.defaults.restart.backoff_s == [0.5, 1, 2, 4, 8] and c.defaults.restart.max.count == 5
    assert c.bus.shm is False and c.access_mode == "loopback"
    assert ("vite" in [p.name for p in c.enabled_procs()]) == (profile == "dev")
    if profile in ("ci", "perf"):
        assert c.net.port_offset == 9 and c.port_effective == 8090 and c.rendezvous_effective == "tcp/127.0.0.1:7537"
        assert c.run.keep_run_dir is True and c.quota.runs_gb == 5
        assert c.cpu.pin == ("off" if profile == "ci" else "on")
    assert c.expand("${net.port_effective}") == str(c.port_effective)


def test_precedence_argv_env_profile_base() -> None:
    c = load_runtime_config(profile="ci", env={})
    assert c.net.port_offset == 9  # profile > 基础值
    c = load_runtime_config(profile="ci", env={"AWR_PORT_OFFSET": "3"})
    assert c.net.port_offset == 3  # 环境变量 > profile
    c = load_runtime_config(profile="ci", env={"AWR_PORT_OFFSET": "3"}, argv=["net.port_offset=5"])
    assert c.net.port_offset == 5 and c.port_effective == 8050  # 命令行 > 环境变量
    c = load_runtime_config(env={"AWR_PROFILE": "demo", "AWR_ORIGINS": "http://10.0.0.2:8000", "AWR_BIND": "0.0.0.0"})
    assert c.profile == "demo" and c.access_mode == "lan" and "10.0.0.2" in c.allowed_hosts


@pytest.mark.parametrize(("mutate", "env", "argv", "path"), [
    (lambda d: d["net"].update(foo=1), {}, [], "net.foo"),
    (lambda d: d["procs"][0].update(bogus=True), {}, [], "procs[sim-core].bogus"),
    (lambda d: d["net"].update(port="eighty"), {}, [], "net.port"),
    (lambda d: None, {"AWR_PORT": "abc"}, [], "net.port"),
    (lambda d: None, {"AWR_BIND": "0.0.0.0"}, [], "net.origins"),
    (lambda d: None, {"AWR_ACCESS_MODE": "lan"}, [], "net.origins"),
    (lambda d: None, {"AWR_REAL_OPS": "1"}, [], "real_ops.enabled"),
    (lambda d: d["real_ops"].update(enabled=True), {}, [], "real_ops.enabled"),
    (lambda d: d["bus"].update(rendezvous="tcp/0.0.0.0:7447"), {}, [], "bus.rendezvous"),
    (lambda d: d["bus"].update(shm=True), {}, [], "bus.shm"),
    (lambda d: d["procs"].append(dict(d["procs"][0])), {}, [], ""),
    (lambda d: d["procs"][1].update(start_after=["nope"]), {}, [], ""),
    (lambda d: d["procs"][1].update(id_range=[512, 16]), {}, [], ""),
    (lambda d: d["profiles"]["ci"].update(procs=[]), {}, [], "profiles.ci.procs"),
    (lambda d: None, {}, ["net.port_offset=12"], "net.port_offset"),
])
def test_invalid_configs_exit_2_with_key_path(tmp_path: Path, mutate, env, argv, path) -> None:
    p = write(tmp_path, mutate)
    with pytest.raises(ConfigError) as ei:
        load_runtime_config(p, profile="ci" if "profiles.ci" in path else "dev", env=env, argv=argv)
    assert ei.value.exit_code == 2
    if path:
        assert ei.value.path == path, str(ei.value)


def test_field_profile_rejected() -> None:
    with pytest.raises(ConfigError) as ei:
        load_runtime_config(profile="field", env={})
    assert ei.value.exit_code == 2 and "field" in str(ei.value)
    with pytest.raises(ConfigError):
        load_runtime_config(env={"AWR_PROFILE": "field"})


def test_effective_config_masks_secrets(tmp_path: Path) -> None:
    p = write(tmp_path, lambda d: d["procs"][1].update(env={"api_key": "s3cr3t", "AWR_TOKEN_TTL": "bearer.abc", "PLAIN": "x"}))
    eff = load_runtime_config(p, env={}).effective_dict()
    env = next(x for x in eff["procs"] if x["name"] == "api")["env"]
    assert env == {"api_key": "***", "AWR_TOKEN_TTL": "***", "PLAIN": "x"}
    assert "s3cr3t" not in yaml.safe_dump(eff)
    assert eff["derived"]["port_effective"] == 8000


def test_supervisor_cli_exits_2_on_bad_config(tmp_path: Path) -> None:
    p = write(tmp_path, lambda d: d["net"].update(nope=1))
    r = subprocess.run([sys.executable, "-m", "awr.runtime.supervisor", "-c", str(p)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 2 and "net.nope" in r.stderr and "修复" in r.stderr
