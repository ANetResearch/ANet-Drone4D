"""D3 容器清单审计（D3-AC-22：GPU 容器只绑定一张卡，CPU 池容器（含 geosvc 与 gz physics）不挂载 GPU，全部 D3 容器设 --memory，
gz 与 Worker 使用独立分区与命名；AWR-05 §4.6、§4.7、ADR-178；M24-FR-071）。

1. 仓库内全部启动点（gz 实例 docker 与 local、Agent 隔离容器、tools/deploy/dmax 启动脚本的登记变体）经
   `tools/deploy/dmax/audit_manifests.py` 渲染后无错误级发现，并对每个启动点的关键参数逐项断言。
2. 规则 CM-01 至 CM-08 的正反用例（合成参数表），含 geosvc 与 gz physics 挂载 GPU、多卡与 all、直通设备、命名空间与分区。
3. `docker inspect` 记录的归一化（运行中容器审计）与 `AgentContainers` 启动前自检（664）。
4. 启动脚本审计本身：缺 --memory 的脚本、接受多卡参数的脚本、未登记的 Python 启动点均被报出。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from awr.agentsdk.env.containers import (
    ALLOWED_GPUS,
    COMPONENTS,
    RULES,
    AgentContainers,
    check_launch,
    check_launches,
    component_of,
    launch_from_inspect,
    parse_docker_run,
)
from awr.agentsdk.wire import WireError
from awr.phys.gz.instance import GzInstance

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location("audit_manifests", ROOT / "tools/deploy/dmax/audit_manifests.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_manifests"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def report(audit):
    return audit.run_audit()


def _rules(argv: list[str], **kw) -> set[str]:
    return {f.rule for f in check_launch(parse_docker_run(argv, "t", **kw)) if f.level == "error"}


def _by_site(rep: dict, prefix: str) -> list[dict]:
    return [lc for lc in rep["launches"] if lc["site"].startswith(prefix)]


# ==================================================================== 1. 仓库内启动点


def test_repo_manifests_have_no_errors(audit, report) -> None:
    errs = [f for f in report["findings"] if f["level"] == "error"]
    assert report["pass"] and not errs, errs
    assert report["schema"] == "awr.d3.manifest_audit.v1" and set(report["rules"]) == {"CM-00", *RULES}
    # 每个登记变体都实际渲染：12 个脚本、18 个非拒绝变体，9 个拒绝变体 0 条。docker run 条数：run_d3_tests.sh 主阶段、gz 阶段与
    # 合并阶段 3 条；run_gz_perf.sh startup 与 gzcpu 两个阶段 2 条；run_subworker.sh 缺省与 benchroot 为 materialize 伴随容器 + subworker 2 条，MATERIALIZE=0 为 1 条；
    # registry/regctl.sh up 3 条（registry、authz、front）；lab/profile.sh up 6 条；scan_images.sh 6 个镜像各 2 条 + 汇总 1 条；其余各 1 条
    reg = tuple(f"sh:{k}[" for k in sorted(audit.SHELL_SITES))
    rs = {r["site"]: r for r in report["renders"] if r["site"].startswith(reg)}
    assert len(rs) == 27
    runs = {"run_d3_tests.sh": 3, "run_gz_perf.sh": 2, "run_subworker.sh[default]": 2, "run_subworker.sh[benchroot]": 2, "regctl.sh[up]": 3, "profile.sh[up]": 6,
            "scan_images.sh[default]": 13}
    for site, r in rs.items():
        n = next((v for k, v in runs.items() if k in site), 1)
        assert r["docker_runs"] == (0 if any(k in site for k in ("multi-card", "all-cards", "foreign-card")) else n), site
        assert (r["exit"] != 0) == (r["docker_runs"] == 0), site
    s = report["summary"]
    assert s["python"] == 28 and s["scripts"] >= 6 and s["errors"] == 0      # 含 d3-public 演练的 nginx 与 chown 容器各 1 条、B19 采集与收尾容器各 2 条
    # 告警只有 CM-08（d3.slice 尚未配置配额）与 CM-00（新增脚本尚未登记变体，但已按缺省变体渲染且无错误）
    assert {f["rule"] for f in report["findings"]} <= {"CM-08", "CM-00"}


def test_every_launch_sets_memory_and_cpus(report) -> None:
    for lc in report["launches"]:
        assert lc["memory"] and lc["cpus"] and float(lc["cpus"]) > 0, lc["site"]
        assert lc["name"] and lc["name"].startswith("d3-"), lc["site"]


def test_gz_physics_instances_cpu_pool_and_partitions(report) -> None:
    gz = _by_site(report, "python:gz-instance[docker]")
    assert [lc["name"] for lc in gz] == ["d3-gz-audit-run-a", "d3-gz-audit-run-b"]
    for lc in gz:
        assert lc["gpus"] == [] and lc["devices"] == [] and lc["runtime"] is None
        assert lc["memory"] == "2g" and lc["cpus"] == "2"                        # M24 §8.2：physics 档 --cpus 2 --memory 2g
        assert lc["env"]["GZ_IP"] == "127.0.0.1" and lc["env"]["GZ_PARTITION"] == lc["name"]
        assert lc["command"][:2] == ["gz", "sim"]
    assert len({lc["env"]["GZ_PARTITION"] for lc in gz}) == 2


def test_agent_containers_cpu_pool(report) -> None:
    ag = _by_site(report, "python:agent-containers")
    assert len(ag) == 4 and len({lc["name"] for lc in ag}) == 4
    for lc in ag:
        assert lc["gpus"] == [] and lc["memory"] == "8g" and lc["labels"]["d3.component"] == "agent"
        assert "GZ_PARTITION" not in lc["env"]


def test_scripts_bind_at_most_one_card(report) -> None:
    by = {lc["site"]: lc for lc in _by_site(report, "sh:")}
    assert by["sh:tools/deploy/dmax/run_proto_board.sh[l1]"]["gpus"] == []
    assert by["sh:tools/deploy/dmax/run_proto_board.sh[l2-gpu2]"]["gpus"] == ["device=2"]
    assert by["sh:tools/deploy/dmax/run_proto_board.sh[l2-gpu3]"]["gpus"] == ["device=3"]
    assert by["sh:tools/deploy/dmax/run_mbl_e2e.sh[gpu2]"]["gpus"] == ["device=2"]
    assert by["sh:tools/deploy/dmax/run_mbl_e2e.sh[gpu3]"]["gpus"] == ["device=3"]
    assert by["sh:tools/deploy/dmax/run_mbl_e2e.sh[gpu2]"]["name"] == "d3-board-mbl-audit"
    for site in ("bench_ctl.sh[default]", "run_gz_tests.sh[default]", "run_gz_perf.sh[default]", "run_subworker.sh[default]",
                 "images/trivy/build.sh[default]"):
        assert by[f"sh:tools/deploy/dmax/{site}"]["gpus"] == [], site
    # 统一测试入口：只有主阶段绑定 GPU 参数指定的一张卡，gz 阶段与合并阶段不挂载 GPU
    d3t = {(lc["site"], lc["name"]): lc["gpus"] for lc in _by_site(report, "sh:tools/deploy/dmax/run_d3_tests.sh")}
    assert d3t == {("sh:tools/deploy/dmax/run_d3_tests.sh[default]", "d3-test-main"): ["device=2"],
                   ("sh:tools/deploy/dmax/run_d3_tests.sh[default]", "d3-test-main-gz"): [],
                   ("sh:tools/deploy/dmax/run_d3_tests.sh[default]", "d3-test-main-sum"): [],
                   ("sh:tools/deploy/dmax/run_d3_tests.sh[gpu3]", "d3-test-audit"): ["device=3"],
                   ("sh:tools/deploy/dmax/run_d3_tests.sh[gpu3]", "d3-test-audit-gz"): [],
                   ("sh:tools/deploy/dmax/run_d3_tests.sh[gpu3]", "d3-test-audit-sum"): []}
    # 运行 gz 的宿主容器（local launcher）不在容器层设 GZ_PARTITION；d3-gz-* 命名空间不被测试或宿主容器占用
    for lc in by.values():
        assert "GZ_PARTITION" not in lc["env"] and not lc["name"].startswith("d3-gz-"), lc["site"]


def test_slam_eval_containers_cpu_pool(report) -> None:
    """slam_eval 作业（tools/bench/d3/slam_eval）：FAST-LIVO2 回放、RTK 抽取与读取镜像参数文件的容器为 d3-livo-*，评测与参考点容器为
    d3-eval-*，均在 CPU 池、设 --memory 与 --cpus；两个作业的容器名两两不同。"""
    sl = _by_site(report, "python:slam-")
    assert len(sl) == 16 and len({lc["name"] for lc in sl}) == 16             # 读取镜像文件的一次性容器名带随机后缀，其余按作业区分
    for lc in sl:
        assert lc["gpus"] == [] and lc["devices"] == [] and lc["runtime"] is None, lc["name"]
        assert lc["name"].startswith(("d3-livo-", "d3-eval-")) and "GZ_PARTITION" not in lc["env"], lc["name"]
    names = {lc["name"] for lc in _by_site(report, "python:slam-cli")}
    assert {"d3-livo-audit-run-a-case-livo-r1", "d3-livo-audit-run-a-case-lio-r1", "d3-eval-audit-run-a-case-livo-r1",
            "d3-livo-audit-run-b-rtk", "d3-livo-audit-run-b-regress"} <= names
    rp = [lc for lc in sl if lc["command"] == ["/opt/d3-livo/run_livo.sh"]]                     # 回放：作业 2 种模式 + regress + 直接渲染
    assert len(rp) == 8 and all(lc["name"].startswith("d3-livo-") for lc in rp)


def test_lidar_collect_containers(report) -> None:
    """多机 LiDAR 采集作业（tools/bench/d3/lidar_collect.py all）：采集容器为 GPU Worker（d3-sensorsvc-*，只绑定缺省 GPU 2 一张卡），
    收尾容器为 CPU 池评测容器（d3-eval-*，--memory 16g，不挂载 GPU）；两个作业的容器名两两不同。"""
    col = _by_site(report, "python:lidar-collect[collect]")
    post = _by_site(report, "python:lidar-collect[post]")
    assert len(col) == len(post) == 2 and len({lc["name"] for lc in col + post}) == 4
    for lc in col:
        assert lc["name"].startswith("d3-sensorsvc-audit-run-") and lc["name"].endswith("-collect"), lc["name"]
        assert lc["gpus"] == ["device=2"] and lc["memory"] and lc["cpus"], lc["name"]
    for lc in post:
        assert lc["name"].startswith("d3-eval-audit-run-") and lc["name"].endswith("-post"), lc["name"]
        assert lc["gpus"] == [] and lc["devices"] == [] and lc["memory"] == "16g" and lc["cpus"], lc["name"]
    for lc in col + post:
        assert "GZ_PARTITION" not in lc["env"] and lc["command"][1].endswith("tools/bench/d3/lidar_collect.py"), lc["name"]
        assert lc["command"][2] == lc["name"].rsplit("-", 1)[1], lc["name"]                 # 容器名末段即所执行的阶段


def test_gz_local_subprocess_partition(tmp_path: Path) -> None:
    a = GzInstance(tmp_path / "a", "d3-gz-r1", launcher="local", dry_run=True).local_env()
    b = GzInstance(tmp_path / "b", "d3-gz-r2", launcher="local", dry_run=True).local_env()
    assert a["GZ_IP"] == b["GZ_IP"] == "127.0.0.1" and a["GZ_PARTITION"] == "d3-gz-r1" and b["GZ_PARTITION"] == "d3-gz-r2"
    c = GzInstance(tmp_path / "c", "d3-gz-r3", partition="r3-1", dry_run=True).command()
    assert "GZ_PARTITION=r3-1" in c and "GZ_IP=127.0.0.1" in c


# ==================================================================== 2. 规则正反用例


BASE = ["docker", "run", "-d", "--rm", "--network", "none"]


def test_cm01_cm02_memory_and_cpus() -> None:
    ok = [*BASE, "--name", "d3-eval-1", "--cpus", "1", "-m", "512m", "img"]
    assert _rules(ok) == set()
    assert "CM-01" in _rules([*BASE, "--name", "d3-eval-1", "--cpus", "1", "img"])
    assert "CM-01" in _rules([*BASE, "--name", "d3-eval-1", "--cpus", "1", "--memory=0g", "img"])
    assert "CM-01" in _rules([*BASE, "--name", "d3-eval-1", "--cpus", "1", "--memory", "lots", "img"])
    assert "CM-02" in _rules([*BASE, "--name", "d3-eval-1", "--memory", "1g", "img"])
    assert "CM-02" in _rules([*BASE, "--name", "d3-eval-1", "--memory", "1g", "--cpus=0", "img"])


@pytest.mark.parametrize("gpus", ["all", "2", "device=2,3", '"device=2,3"', "count=1", "device=0", "device=4"])
def test_cm03_single_assigned_card_only(gpus: str) -> None:
    argv = [*BASE, "--name", "d3-sensorsvc-w2", "--cpus", "8", "--memory", "16g", "--gpus", gpus, "img"]
    assert "CM-03" in _rules(argv)


def test_cm03_device_passthrough_runtime_env_and_repeats() -> None:
    w = [*BASE, "--name", "d3-sensorsvc-w2", "--cpus", "8", "--memory", "16g"]
    assert _rules([*w, "--gpus", "device=2", "img"]) == set()
    assert _rules([*w, "--gpus", "'\"device=3\"'", "img"]) == set()                     # §4.6 的引号写法
    assert "CM-03" in _rules([*w, "--gpus", "device=2", "--gpus", "device=3", "img"])
    assert "CM-03" in _rules([*w, "--device", "/dev/nvidia0:/dev/nvidia0", "img"])
    assert "CM-03" in _rules([*w, "--runtime", "nvidia", "img"])
    assert "CM-03" in _rules([*w, "--gpus", "device=2", "-e", "NVIDIA_VISIBLE_DEVICES=all", "img"])
    assert _rules([*w, "--gpus", "device=2", "-e", "NVIDIA_VISIBLE_DEVICES=void", "img"]) == set()
    assert {1, 2, 3} == ALLOWED_GPUS


@pytest.mark.parametrize("comp,name", [("geosvc", "d3-geosvc-w1"), ("gz", "d3-gz-r1"), ("agent", "d3-agent-r1-uav-01"), ("livo", "d3-livo-r1"),
                                       ("env", "d3-env-r1"), ("bench", "d3-bench")])
def test_cm04_cpu_pool_never_mounts_gpu(comp: str, name: str) -> None:
    assert COMPONENTS[comp].pool == "cpu"
    env = ["-e", "GZ_PARTITION=r1", "-e", "GZ_IP=127.0.0.1"] if comp == "gz" else []
    base = [*BASE, "--name", name, "--cpus", "1", "--memory", "1g", "--cgroup-parent", "d3.slice", *env]
    assert _rules([*base, "img"]) == set()
    assert "CM-04" in _rules([*base, "--gpus", "device=2", "img"])
    assert "CM-04" in _rules([*base, "--device", "/dev/nvidia2", "img"])


def test_cm05_gpu_worker_needs_exactly_one_card_and_xval_profile() -> None:
    w = [*BASE, "--name", "d3-sensorsvc-w3", "--cpus", "8", "--memory", "16g"]
    assert "CM-05" in _rules([*w, "img"])
    assert _rules([*w, "--gpus", "device=3", "img"]) == set()
    # gz xval 档（P1）：以标签声明后绑定一张卡；未声明时 gz 按 physics 档不得挂载 GPU
    gz = [*BASE, "--name", "d3-gz-r9", "--cpus", "4", "--memory", "6g", "-e", "GZ_PARTITION=r9-1", "-e", "GZ_IP=127.0.0.1"]
    assert _rules([*gz, "--label", "d3.profile=xval", "--gpus", "device=2", "img", "gz", "sim"]) == set()
    assert "CM-04" in _rules([*gz, "--gpus", "device=2", "img", "gz", "sim"])
    assert "CM-05" in _rules([*gz, "--label", "d3.profile=xval", "img", "gz", "sim"])


def test_cm06_naming() -> None:
    m = ["--cpus", "1", "--memory", "1g"]
    assert "CM-06" in _rules([*BASE, *m, "img"])                                         # 无 --name
    assert "CM-06" in _rules([*BASE, "--name", "board-x", *m, "img"])
    assert "CM-06" in _rules([*BASE, "--name", "d3-unknown-x", *m, "img"])
    assert "CM-06" in _rules([*BASE, "--name", "d3-gz-test", *m, "img"], component="test")
    assert "CM-06" in _rules([*BASE, "--name", "d3-eval-1", *m, "img"], component="livo")
    assert component_of(parse_docker_run([*BASE, "--name", "d3-lab-gateway-1", *m, "img"])) == "lab-gateway"
    assert component_of(parse_docker_run([*BASE, "--name", "x", "--label", "d3.component=geosvc", *m, "img"])) == "geosvc"


def test_cm07_gz_partition_and_worker_separation() -> None:
    gz = [*BASE, "--name", "d3-gz-r1", "--cpus", "2", "--memory", "2g"]
    assert "CM-07" in _rules([*gz, "-e", "GZ_IP=127.0.0.1", "img", "gz", "sim"])
    assert "CM-07" in _rules([*gz, "-e", "GZ_PARTITION=r1", "img", "gz", "sim"])
    assert "CM-07" in _rules([*gz, "-e", "GZ_PARTITION", "-e", "GZ_IP=127.0.0.1", "img"])  # 透传值不可审计
    wk = [*BASE, "--name", "d3-sensorsvc-w2", "--cpus", "8", "--memory", "16g", "--gpus", "device=2"]
    assert "CM-07" in _rules([*wk, "-e", "GZ_PARTITION=shared", "img"])
    # 不同 Run 的 gz 实例共用分区或容器名
    a = parse_docker_run([*gz, "-e", "GZ_PARTITION=p", "-e", "GZ_IP=127.0.0.1", "img"], "t", run="r1")
    b = parse_docker_run([*BASE, "--name", "d3-gz-r1", "--cpus", "2", "--memory", "2g", "-e", "GZ_PARTITION=p", "-e", "GZ_IP=127.0.0.1", "img"],
                         "t", run="r2")
    rules = [f.rule for f in check_launches([a, b]) if f.level == "error"]
    assert rules.count("CM-07") == 1 and rules.count("CM-06") == 1


def test_cm08_d3_slice_is_warning() -> None:
    lc = parse_docker_run([*BASE, "--name", "d3-geosvc-w", "--cpus", "1", "--memory", "1g", "img"], "t")
    f = [x for x in check_launch(lc) if x.rule == "CM-08"]
    assert len(f) == 1 and f[0].level == "warn"
    lc.cgroup_parent = "d3.slice"
    assert not [x for x in check_launch(lc) if x.rule == "CM-08"]


def test_parse_docker_run_forms() -> None:
    lc = parse_docker_run(["docker", "container", "run", "-dit", "--rm=true", "-ePYTHONPATH=/x", "-m2g", "--cpus=1.5", "--name=d3-eval",
                           "-l", "d3.run=r", "-v", "/a:/b", "--", "img:tag", "python", "-m", "x"])
    assert (lc.name, lc.memory, lc.cpus, lc.image) == ("d3-eval", "2g", "1.5", "img:tag")
    assert lc.env == {"PYTHONPATH": "/x"} and lc.labels == {"d3.run": "r"} and lc.command == ["python", "-m", "x"]
    with pytest.raises(ValueError):
        parse_docker_run(["docker", "build", "."])


# ==================================================================== 3. 运行中容器与 AgentContainers 自检


def _inspect(name: str, *, mem: int = 2 << 30, nano: int = 2_000_000_000, reqs=None, devices=None, runtime="runc", env=None,
             cmd=("python3", "-m", "awr.sensorsvc")) -> dict:
    return {"Name": f"/{name}", "Config": {"Image": "d3-sensorsvc:dev", "Env": env or ["NVIDIA_DRIVER_CAPABILITIES=all"], "Labels": {},
                                         "Cmd": list(cmd)},
            "HostConfig": {"Memory": mem, "NanoCpus": nano, "DeviceRequests": reqs, "Devices": devices or [], "Runtime": runtime}}


def test_inspect_records(audit) -> None:
    one = [{"Driver": "", "Count": 0, "DeviceIDs": ["2"], "Capabilities": [["gpu"]]}]
    ok = launch_from_inspect(_inspect("d3-sensorsvc-w2", reqs=one, env=["NVIDIA_VISIBLE_DEVICES=all"]))
    assert ok.gpus == ["device=2"] and ok.memory == str(2 << 30) and ok.cpus == "2" and "NVIDIA_VISIBLE_DEVICES" not in ok.env
    assert {f.rule for f in check_launch(ok)} == set()
    allg = launch_from_inspect(_inspect("d3-sensorsvc-w2", reqs=[{"Count": -1, "Capabilities": [["gpu"]]}]))
    assert allg.gpus == ["all"] and "CM-03" in {f.rule for f in check_launch(allg)}
    nomem = launch_from_inspect(_inspect("d3-geosvc-w", mem=0, reqs=one))
    assert {"CM-01", "CM-04"} <= {f.rule for f in check_launch(nomem)}
    gz = launch_from_inspect(_inspect("d3-gz-r1", devices=[{"PathOnHost": "/dev/nvidia3"}], env=["GZ_IP=127.0.0.1"], cmd=("gz", "sim")))
    assert {"CM-03", "CM-04", "CM-07"} <= {f.rule for f in check_launch(gz)}
    rep = audit.run_audit(inspect=[_inspect("d3-sensorsvc-w2", reqs=one, env=["SECRET_TOKEN=abc", "NVIDIA_DRIVER_CAPABILITIES=all"])])
    live = [lc for lc in rep["launches"] if lc["site"] == "live"]
    assert len(live) == 1 and rep["summary"]["live"] == 1 and rep["pass"]
    assert live[0]["env"]["SECRET_TOKEN"] == "<redacted>" and "abc" not in json.dumps(rep)


def test_agent_containers_refuse_bad_manifest(tmp_path: Path) -> None:
    ac = AgentContainers(tmp_path, "r1", ["uav-01"], "img", mem_gb=0, docker="/nonexistent/docker")
    with pytest.raises(WireError) as e:
        ac.start()
    assert e.value.code == 664 and "CM-01" in str(e.value)
    good = AgentContainers(tmp_path, "r1", ["uav-01"], "img", cpus=2, mem_gb=8)
    lc = parse_docker_run(good.argv("uav-01", tmp_path / "s"), "t", component="agent")
    assert [f.rule for f in check_launch(lc) if f.level == "error"] == []


# ==================================================================== 4. 审计本身的检出能力


def _fake_root(tmp_path: Path, script: str) -> Path:
    d = tmp_path / "root" / "tools/deploy/dmax"
    d.mkdir(parents=True)
    (d / "x.sh").write_text(script)
    return tmp_path / "root"


def test_audit_detects_script_without_memory(audit, tmp_path: Path) -> None:
    root = _fake_root(tmp_path, '#!/usr/bin/env bash\nset -euo pipefail\nD=/data/x\nrsync -a a "dmax:$D/"\n'
                                'ssh dmax "mkdir -p $D && docker run -d --rm --name d3-eval-x --network none --cpus 2 \\\n  --gpus all img python3 -V"\n')
    sites = {"tools/deploy/dmax/x.sh": (audit.Variant("default", "eval"),)}
    ls, fs, renders = audit.audit_scripts(root, sites)
    assert renders == [{"site": "sh:tools/deploy/dmax/x.sh[default]", "exit": 0, "docker_calls": 1, "docker_runs": 1}]
    assert len(ls) == 1 and ls[0].gpus == ["all"] and ls[0].command == ["python3", "-V"]
    rules = {f.rule for f in [*fs, *check_launches(ls)] if f.level == "error"}
    assert {"CM-01", "CM-03", "CM-04"} <= rules


def test_audit_detects_accepted_multi_card_and_unregistered(audit, tmp_path: Path) -> None:
    root = _fake_root(tmp_path, '#!/usr/bin/env bash\nGPU=${GPU:-2}\nssh dmax "docker run --rm --name d3-sensorsvc-x --cpus 2 --memory 1g '
                                '--gpus device=$GPU img"\n')
    sites = {"tools/deploy/dmax/x.sh": (audit.Variant("multi", "sensorsvc", env={"GPU": "2,3"}, reject=True),)}
    _, fs, _ = audit.audit_scripts(root, sites)
    assert [f.rule for f in fs] == ["CM-03"]
    ls, fs, _ = audit.audit_scripts(root, {})                                           # 未登记：缺省变体渲染并告警
    assert [(f.rule, f.level) for f in fs] == [("CM-00", "warn")] and ls[0].gpus == ["device=2"]
    assert {f.rule for f in check_launches(ls) if f.level == "error"} == set()
    (root / "tools/deploy/dmax/y.sh").write_text('#!/usr/bin/env bash\nset -eu\nN=${1:?name}\nssh dmax "docker run --name d3-eval-$N img"\n')
    _, fs, _ = audit.audit_scripts(root, {})
    assert ("CM-00", "error") in {(f.rule, f.level) for f in fs if f.site.startswith("sh:tools/deploy/dmax/y.sh")}


def test_audit_detects_unregistered_python_launcher(audit, tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "python/awr/x").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "python/awr/x/launch.py").write_text('import subprocess\nsubprocess.run(["docker", "run", "--rm", "img"])\n')
    assert audit.discover_python_launchers(root) == ["python/awr/x/launch.py"]
    _, fs = audit.audit_python(tmp_path / "w", root)
    assert [(f.rule, f.site) for f in fs] == [("CM-00", "python:python/awr/x/launch.py")]
    assert sorted(audit.discover_python_launchers()) == sorted(audit.PY_LAUNCHERS)


def test_python_launcher_scan_ignores_comments_and_docstrings(audit, tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "python/awr").mkdir(parents=True)
    (root / "tools/bench").mkdir(parents=True)
    (root / "tools/bench/doc.py").write_text('"""实例启动（docker run 至 HelloAck）。"""\n\n\ndef f():\n'
                                              '    """同上：docker run。"""\n    return 1  # 不经 docker run\n')
    (root / "tools/bench/real.py").write_text('"""说明。"""\nCMD = "docker run --rm img"  # 真实启动\n')
    (root / "tools/bench/broken.py").write_text('docker run (\n')                     # 语法错误：按原文扫描
    assert audit.discover_python_launchers(root) == ["tools/bench/broken.py", "tools/bench/real.py"]


def test_cli_exit_codes(audit, tmp_path: Path) -> None:
    out = tmp_path / "rep.json"
    assert audit.main(["--json", str(out)]) == 0
    assert json.loads(out.read_text())["pass"] is True
    assert audit.main(["--strict-cgroup"]) == 1                                          # d3.slice 未接入时严格模式不通过
    bad = tmp_path / "inspect.json"
    bad.write_text(json.dumps([_inspect("d3-geosvc-w", mem=0)]))
    assert audit.main(["--inspect", str(bad)]) == 1
    assert audit.main(["--inspect", str(tmp_path / "missing.json")]) == 2
