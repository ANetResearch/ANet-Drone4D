"""提交镜像扫描（D3-AC-59；ADR-204）：Trivy HIGH 及以上未修复漏洞拒绝、体积 > 4 GB 拒绝（692），扫描器隔离与离线库时效，
平台故障重试与不放行，榜单轮次配置准入（633、682）。

前半部分以 docker 替身（`fake_scan_docker.py`）驱动 `awr.bench.subworker`；`test_real_trivy_*` 在 dmax 以真实 Trivy v0.75.0 与离线库
扫描现场构建的镜像，需 `AWR_D3_TRIVY_CACHE=<含 db/ 与 java-db/ 的目录>`（可选 `AWR_D3_TRIVY_IMAGE`、`AWR_D3_SCAN_DIR`、
`AWR_D3_SCAN_BIG_IMAGE`=一个 > 4 GB 的本地镜像）。"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from awr.agentsdk.env.containers import check_launch, parse_docker_run
from awr.bench import subworker as sw
from awr.bench.submissions import SubmissionStore
from awr.bench.subworker import (
    IMAGE_MAX_B,
    ScanError,
    ScannerUnavailable,
    TrivyScanner,
    main,
    process_one,
    scan_image,
    vulnerability_findings,
)

CLEAN = "sha256:" + "c" * 64
VULN = "sha256:" + "d" * 64
BIG = "sha256:" + "e" * 64
EDGE = "sha256:" + "f" * 64
ENTRY = {"cmd": ["python", "-m", "anet4d.serve", "--baseline", "track_share"]}
NOW = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.UTC)


def _v(vid: str, sev: str, fixed: str = "", status: str | None = None, pkg: str = "openssl", ver: str = "3.0.1") -> dict:
    return {"VulnerabilityID": vid, "PkgName": pkg, "InstalledVersion": ver, "FixedVersion": fixed, "Severity": sev,
            "Status": status or ("fixed" if fixed else "affected")}


def _report(*vulns: dict, target: str = "img (debian 12.7)") -> dict:
    return {"SchemaVersion": 2, "Trivy": {"Version": "0.75.0"}, "Results": [{"Target": target, "Vulnerabilities": list(vulns)}]}


VULN_REPORT = _report(_v("CVE-2026-0001", "CRITICAL", "3.0.9"), _v("CVE-2026-0002", "HIGH", "3.0.8"), _v("CVE-2026-0003", "HIGH"),
                      _v("CVE-2026-0004", "MEDIUM", "3.0.2"))
CLEAN_REPORT = _report(_v("CVE-2026-0003", "HIGH"), _v("CVE-2026-0005", "CRITICAL", status="will_not_fix"))


def _meta(path: Path, updated: dt.datetime, version: int = 2) -> None:
    path.mkdir(parents=True, exist_ok=True)
    t = updated.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    (path / "metadata.json").write_text(json.dumps({"Version": version, "NextUpdate": t, "UpdatedAt": t, "DownloadedAt": t}))


def _cache(root: Path, db_age_d: float = 1.0, java_age_d: float = 2.0, at: dt.datetime | None = None) -> Path:
    """库龄相对 `at`（缺省为当前时刻：经 `process_one` 的用例以真实时钟判定库龄）。"""
    at = at or dt.datetime.now(dt.UTC)
    c = root / "trivy-cache"
    _meta(c / "db", at - dt.timedelta(days=db_age_d))
    _meta(c / "java-db", at - dt.timedelta(days=java_age_d), 1)
    (c / "db/trivy.db").write_bytes(b"db")
    (c / "java-db/trivy-java.db").write_bytes(b"jdb")
    return c


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> dict:
    state = tmp_path / "docker-state"
    state.mkdir()
    monkeypatch.setenv("FAKE_SCAN_STATE", str(state))
    images = {CLEAN: {"Os": "linux", "Architecture": "amd64", "Size": 130_000_000, "report": CLEAN_REPORT},
              VULN: {"Os": "linux", "Architecture": "amd64", "Size": 130_000_000, "report": VULN_REPORT},
              BIG: {"Os": "linux", "Architecture": "amd64", "Size": IMAGE_MAX_B + 1},
              EDGE: {"Os": "linux", "Architecture": "amd64", "Size": IMAGE_MAX_B},
              "d3-trivy:dev": {"Os": "linux", "Architecture": "amd64", "Size": 160_000_000}}
    (state / "images.json").write_text(json.dumps(images))
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).with_name('fake_scan_docker.py')} \"$@\"\n")
    docker.chmod(0o755)
    trivy = TrivyScanner(cache_dir=_cache(tmp_path), work_dir=tmp_path / "scan", docker=str(docker))
    return {"state": state, "docker": str(docker), "trivy": trivy, "images": images, "root": tmp_path / "d3"}


def _set_image(env: dict, ref: str, **kw) -> None:
    env["images"].setdefault(ref, {}).update(kw)
    (env["state"] / "images.json").write_text(json.dumps(env["images"]))


def _calls(env: dict) -> list[list[str]]:
    p = env["state"] / "calls.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _board(root: Path, *, dev: bool = True, physics: str = "realistic") -> None:
    b = root / "boards" / "v-t"
    b.mkdir(parents=True, exist_ok=True)
    rs = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "t"}, "world": {"id": "w"}, "event": {"id": "e", "version": 1},
          "fleet": [{"uav_id": "uav-01", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [0.0, 0.0, 0.0], "yaw_deg": 0.0}}],
          "policy": {"kind": "reference", "name": "track_share"}, "obs_level": "L1", "physics": "realistic", "duration_s": 20.0, "seeds": [11]}
    cfg = {"run_spec": rs, "seeds": [11], "events_root": str(root / "events"), "worlds_root": str(root / "worlds"), "physics": physics, "gz": {},
           "track": {"fleet_ref": "1x-x500", "obs_level": "L1", "dev": dev}, "track_id": "std2-l1", "lane": "rendezvous_handoff"}
    (b / "config.json").write_text(json.dumps(cfg))


def _submit(root: Path, image: str) -> tuple[SubmissionStore, str]:
    st = SubmissionStore(root / "submissions")
    sid = st.create({"track": "std2-l1", "image_digest": image, "entry": ENTRY}, owner="alice", team="team-a", board_version="v-t",
                    tracks={"std2-l1"}, lanes={"rendezvous_handoff"})["submission_id"]
    return st, sid


def _stop_at_smoke(rs, **kw):                                          # 冒烟阶段即停：只验证扫描阶段放行
    raise RuntimeError("smoke stub")


def _run(env: dict, image: str, **kw) -> tuple[dict, dict | None]:
    _board(env["root"], **kw)
    st, sid = _submit(env["root"], image)
    assert process_one(env["root"], docker=env["docker"], trivy=env["trivy"], run_job=_stop_at_smoke, log=lambda m: None) == sid
    p = env["root"] / "submissions" / sid / "scan.json"
    return st.get(sid), json.loads(p.read_text()) if p.exists() else None


# ==================================================================== 判定口径

def test_threshold_is_4_gb_decimal() -> None:
    assert IMAGE_MAX_B == 4_000_000_000                                   # D3-AC-59"体积 > 4 GB"（ADR-204 (4)，不再是 8 GiB）


def test_findings_fixable_high_and_above_only() -> None:
    dup = {"Target": "usr/lib/python3/dist-packages", "Vulnerabilities": [_v("CVE-2026-0002", "HIGH", "3.0.8")]}
    rep = {**VULN_REPORT, "Results": [*VULN_REPORT["Results"], dup, {"Target": "empty", "Vulnerabilities": None}]}
    f = vulnerability_findings(rep)
    assert [r["id"] for r in f["fixable"]] == ["CVE-2026-0001", "CVE-2026-0002"]      # CRITICAL 在前；MEDIUM 不计；重复目标去重
    assert f["fixable_count"] == 2 and f["unfixed_count"] == 1 and f["by_severity"] == {"CRITICAL": 1, "HIGH": 1}
    assert f["fixable"][0] == {"id": "CVE-2026-0001", "pkg": "openssl", "installed": "3.0.1", "fixed": "3.0.9", "severity": "CRITICAL",
                               "target": "img (debian 12.7)"}
    g = vulnerability_findings(CLEAN_REPORT)                             # 上游无修复版本（affected、will_not_fix）只计数
    assert g["fixable_count"] == 0 and g["unfixed_count"] == 2
    assert vulnerability_findings({"SchemaVersion": 2})["fixable_count"] == 0
    assert vulnerability_findings(_report(_v("CVE-X", "HIGH", status="fixed")))["fixable_count"] == 1   # Status fixed 而无版本号


def test_db_status_age_and_staleness(tmp_path: Path) -> None:
    t = TrivyScanner(cache_dir=_cache(tmp_path, 1.0, 2.5, NOW), work_dir=tmp_path)
    s = t.db_status(NOW)
    assert s["age_days"] == pytest.approx(2.5, abs=1e-3) and not s["warning"]          # 取两个库中较旧者
    assert s["db"]["updated_at"] == "2026-10-17T12:00:00Z" and s["java_db"]["downloaded_at"] == "2026-10-16T00:00:00Z"
    assert TrivyScanner(cache_dir=_cache(tmp_path / "w", 8.0, 1.0, NOW), work_dir=tmp_path).db_status(NOW)["warning"]
    assert TrivyScanner(cache_dir=_cache(tmp_path / "x", 30.0, 1.0, NOW), work_dir=tmp_path).db_status(NOW)["age_days"] == 30.0   # 30 天仍可扫描
    with pytest.raises(ScannerUnavailable, match="days old"):
        TrivyScanner(cache_dir=_cache(tmp_path / "s", 1.0, 30.5, NOW), work_dir=tmp_path).db_status(NOW)
    c = _cache(tmp_path / "m", at=NOW)
    (c / "java-db/trivy-java.db").unlink()
    with pytest.raises(ScannerUnavailable, match="missing"):
        TrivyScanner(cache_dir=c, work_dir=tmp_path).db_status(NOW)
    (c / "db/metadata.json").write_text("{")
    with pytest.raises(ScannerUnavailable, match="metadata"):
        TrivyScanner(cache_dir=c, work_dir=tmp_path).db_status(NOW)


def test_scan_container_is_isolated_and_offline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AWR_HOST_PATH_MAP", f"{tmp_path}=/data/host")
    t = TrivyScanner(cache_dir=tmp_path / "trivy-cache", work_dir=tmp_path / "scan")
    argv = t.argv("d3-subworker-scan-sub-abc", tmp_path / "scan" / "x.tar")
    assert not [x for x in check_launch(parse_docker_run(argv, "scan", component="subworker")) if x.level == "error"]
    i = argv.index("d3-trivy:dev")
    head, cmd = argv[:i], argv[i + 1:]
    for flag in ("--network none", "--cap-drop ALL", "--security-opt no-new-privileges", "--user 65534:65534", "--pids-limit 256"):
        k, v = flag.split(" ")
        assert head[head.index(k) + 1] == v
    assert "--read-only" in head and "--rm" in head and not any("docker.sock" in x for x in argv)
    vols = [head[j + 1] for j, x in enumerate(head) if x == "-v"]
    assert vols == ["/data/host/trivy-cache:/cache:ro", "/data/host/scan/x.tar:/in/image.tar:ro"]   # 宿主路径换算，只读
    for f in ("--skip-db-update", "--skip-java-db-update", "--offline-scan", "--skip-version-check"):
        assert f in cmd
    assert cmd[cmd.index("--severity") + 1] == "HIGH,CRITICAL" and cmd[cmd.index("--cache-backend") + 1] == "memory"
    assert cmd[cmd.index("--format") + 1] == "json" and cmd[cmd.index("--exit-code") + 1] == "0"


# ==================================================================== 扫描阶段（docker 替身）

def test_scan_image_rejects_vulnerable_and_oversize(env: dict) -> None:
    rec: dict = {}
    bad = scan_image(VULN, env["docker"], trivy=env["trivy"], name="d3-subworker-scan-t", record=rec)
    assert bad["code"] == 692 and bad["name"] == "SUBMISSION_REJECTED"
    assert bad["detail"]["why"] == "scan" and bad["detail"]["check"] == "vulnerabilities" and bad["detail"]["count"] == 2
    assert [v["id"] for v in bad["detail"]["vulnerabilities"]] == ["CVE-2026-0001", "CVE-2026-0002"]
    assert rec["trivy"]["version"] == "0.75.0" and rec["trivy"]["unfixed_count"] == 1
    assert not list((env["trivy"].work_dir).glob("*.tar"))              # 归档扫描后删除
    n = len(_calls(env))
    big = scan_image(BIG, env["docker"], trivy=env["trivy"])
    assert big["detail"] == {"why": "scan", "check": "size", "size_b": IMAGE_MAX_B + 1, "size_max_b": IMAGE_MAX_B}
    assert [c[0] for c in _calls(env)[n:]] == ["image"]                 # 超限镜像不导出、不扫描
    assert scan_image(EDGE, env["docker"], trivy=env["trivy"]) is None  # 恰好 4 GB 通过
    assert scan_image(CLEAN, env["docker"], trivy=env["trivy"]) is None
    _set_image(env, CLEAN, Architecture="arm64")
    assert scan_image(CLEAN, env["docker"], trivy=env["trivy"])["detail"]["check"] == "platform"
    assert scan_image("sha256:" + "0" * 64, env["docker"], trivy=env["trivy"])["detail"] == {"why": "scan", "check": "image_missing"}


def test_scan_platform_errors_raise(env: dict) -> None:
    _set_image(env, CLEAN, fail=1)
    with pytest.raises(ScanError, match="trivy exit 1"):
        scan_image(CLEAN, env["docker"], trivy=env["trivy"])
    _set_image(env, CLEAN, report={"SchemaVersion": 1})
    with pytest.raises(ScanError, match="schema"):
        scan_image(CLEAN, env["docker"], trivy=env["trivy"])


def test_pipeline_rejects_vulnerable_image_692(env: dict) -> None:
    d, rec = _run(env, VULN)
    assert d["state"] == "REJECTED" and d["reason"]["code"] == 692 and d["reason"]["detail"]["check"] == "vulnerabilities"
    assert [h["state"] for h in d["_meta"]["history"]] == ["RECEIVED", "SCANNING", "REJECTED"]
    assert rec["verdict"] == "reject" and rec["trivy"]["fixable_count"] == 2 and rec["inspect"]["size_b"] == 130_000_000
    assert rec["db"]["db"]["downloaded_at"] and rec["db"]["scanner_image"] == "d3-trivy:dev" and rec["errors"] == []


def test_pipeline_rejects_oversize_image_692(env: dict) -> None:
    d, rec = _run(env, BIG)
    assert d["state"] == "REJECTED" and d["reason"]["detail"]["check"] == "size" and "trivy" not in rec
    assert rec["verdict"] == "reject" and not any(c[0] in ("save", "run") for c in _calls(env))


def test_pipeline_passes_clean_image_to_smoke(env: dict) -> None:
    d, rec = _run(env, CLEAN)
    assert [h["state"] for h in d["_meta"]["history"]] == ["RECEIVED", "SCANNING", "VALIDATING", "REJECTED"]
    assert d["reason"]["detail"]["why"] == "smoke"                      # 扫描放行，冒烟替身失败
    assert rec["verdict"] == "pass" and rec["trivy"]["fixable_count"] == 0 and rec["trivy"]["unfixed_count"] == 2
    runs = [c for c in _calls(env) if c[0] == "run"]
    assert len(runs) == 1 and runs[0][runs[0].index("--name") + 1].startswith("d3-subworker-scan-sub-")


def test_pipeline_scan_error_retried_once_then_failed(env: dict) -> None:
    _set_image(env, CLEAN, fail=1)                                      # 第一次失败、重试通过
    d, rec = _run(env, CLEAN)
    assert d["state"] == "REJECTED" and d["reason"]["detail"]["why"] == "smoke" and len(rec["errors"]) == 1
    _set_image(env, VULN, fail=5)                                       # 两次均失败：FAILED 320，不放行
    d, rec = _run(env, VULN)
    assert d["state"] == "FAILED" and d["reason"] == {"code": 320, "name": "INTERNAL", "detail": {"why": "platform", "stage": "scan"}}
    assert rec["verdict"] == "error" and len(rec["errors"]) == 2


def test_stale_db_or_missing_scanner_holds_queue(env: dict, tmp_path: Path) -> None:
    _board(env["root"])
    st, sid = _submit(env["root"], CLEAN)
    stale = TrivyScanner(cache_dir=_cache(tmp_path / "old", 31.0, 1.0), work_dir=tmp_path / "scan", docker=env["docker"])
    with pytest.raises(ScannerUnavailable, match="days old"):
        process_one(env["root"], docker=env["docker"], trivy=stale, run_job=_stop_at_smoke, log=lambda m: None)
    no_img = TrivyScanner(cache_dir=env["trivy"].cache_dir, work_dir=tmp_path / "scan", docker=env["docker"], image="d3-trivy:missing")
    with pytest.raises(ScannerUnavailable, match="scanner image"):
        process_one(env["root"], docker=env["docker"], trivy=no_img, run_job=_stop_at_smoke, log=lambda m: None)
    d = st.get(sid)
    assert d["state"] == "RECEIVED" and len(d["_meta"]["history"]) == 1     # 不领取：提交保持 RECEIVED
    logs: list[str] = []
    old = TrivyScanner(cache_dir=_cache(tmp_path / "warn", 10.0, 1.0), work_dir=tmp_path / "scan", docker=env["docker"])
    assert process_one(env["root"], docker=env["docker"], trivy=old, run_job=_stop_at_smoke, log=logs.append) == sid
    assert any(m.startswith("warning: trivy db is 10.0 days old") for m in logs)


# ==================================================================== 榜单轮次配置准入（P01 后续）

def test_formal_board_config_admission(env: dict) -> None:
    d, rec = _run(env, CLEAN, dev=False, physics="kinematic")
    assert d["state"] == "REJECTED" and d["reason"]["code"] == 682 and rec is None
    assert [h["state"] for h in d["_meta"]["history"]] == ["RECEIVED", "REJECTED"]          # 不进入扫描
    d, _ = _run(env, CLEAN, dev=False, physics="sitl")
    assert d["reason"]["code"] == 633
    d, rec = _run(env, CLEAN, dev=False, physics="realistic")
    assert d["reason"]["detail"]["why"] == "smoke" and rec["verdict"] == "pass"
    d, _ = _run(env, CLEAN, dev=True, physics="kinematic")              # 开发榜（Fast 档自检）不做排名准入
    assert d["reason"]["detail"]["why"] == "smoke"


# ==================================================================== 命令行入口

def test_cli_requires_scanner(env: dict, monkeypatch, capsys) -> None:
    monkeypatch.delenv("AWR_TRIVY_CACHE", raising=False)
    with pytest.raises(SystemExit) as e:
        main(["--data-root", str(env["root"]), "--once"])
    assert e.value.code == 2 and "AWR_TRIVY_CACHE" in capsys.readouterr().err
    _board(env["root"])
    _submit(env["root"], CLEAN)
    stale = _cache(env["root"].parent / "stale", 40.0, 1.0)
    monkeypatch.setattr(sw, "TrivyScanner", lambda **kw: TrivyScanner(**kw, docker=env["docker"]))
    assert main(["--data-root", str(env["root"]), "--once", "--trivy-cache", str(stale)]) == 3
    assert "scanner unavailable" in capsys.readouterr().out


# ==================================================================== 真实 Trivy（dmax）

def _real() -> TrivyScanner:
    cache = os.environ.get("AWR_D3_TRIVY_CACHE")
    if not cache:
        pytest.skip("[optin] set AWR_D3_TRIVY_CACHE=<offline trivy cache with db/ and java-db/>")
    if shutil.which("docker") is None:
        pytest.skip("[platform] needs docker")
    t = TrivyScanner(cache_dir=Path(cache), work_dir=Path(os.environ.get("AWR_D3_SCAN_DIR") or "/tmp/d3-scan-test"),
                     image=os.environ.get("AWR_D3_TRIVY_IMAGE", "d3-trivy:dev"), cpus=2.0, mem_gb=4.0)
    t.ready()
    return t


def _build(tmp: Path, tag: str, files: dict[str, str]) -> str:
    ctx = tmp / tag.replace(":", "-")
    for rel, text in files.items():
        (ctx / "fs" / rel).parent.mkdir(parents=True, exist_ok=True)
        (ctx / "fs" / rel).write_text(text)
    (ctx / "Dockerfile").write_text("FROM scratch\nCOPY fs/ /\n")
    subprocess.run(["docker", "build", "-q", "-t", tag, str(ctx)], check=True, capture_output=True, text=True)
    return subprocess.run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], check=True, capture_output=True, text=True).stdout.strip()


def test_real_trivy_rejects_fixable_high_and_passes_clean(tmp_path: Path) -> None:
    t = _real()
    site = "usr/local/lib/python3.12/site-packages"
    tags = ["d3-b14-scan-vuln:test", "d3-b14-scan-clean:test"]
    try:
        vuln = _build(tmp_path, tags[0], {f"{site}/requests-2.19.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: requests\nVersion: 2.19.0\n"})
        clean = _build(tmp_path, tags[1], {"opt/agent/README": "no packages\n"})
        rec: dict = {}
        bad = scan_image(vuln, trivy=t, name="d3-subworker-scan-b14-vuln", record=rec)
        assert bad is not None and bad["code"] == 692 and bad["detail"]["check"] == "vulnerabilities"
        assert "CVE-2018-18074" in {v["id"] for v in bad["detail"]["vulnerabilities"]}      # requests < 2.20.0，HIGH，修复于 2.20.0
        assert rec["trivy"]["version"] == "0.75.0"
        rec = {}
        assert scan_image(clean, trivy=t, name="d3-subworker-scan-b14-clean", record=rec) is None
        assert rec["trivy"]["fixable_count"] == 0
        print(json.dumps({"db": t.db_status(), "vuln": bad["detail"]["count"]}))
    finally:
        subprocess.run(["docker", "rmi", "-f", *tags], capture_output=True, check=False)


def test_real_trivy_reference_agent_image() -> None:
    t = _real()
    ref = os.environ.get("AWR_D3_SCAN_AGENT_IMAGE", "d3-agent-py:dev")
    rec: dict = {}
    bad = scan_image(ref, trivy=t, name="d3-subworker-scan-b14-agent", record=rec)
    print(json.dumps({"image": ref, "reason": bad, "trivy": {k: v for k, v in rec["trivy"].items() if k != "fixable"}, "inspect": rec["inspect"]}))
    assert rec["inspect"]["size_b"] <= IMAGE_MAX_B and rec["trivy"]["version"] == "0.75.0"


def test_real_oversize_image_rejected_without_scan() -> None:
    t = _real()
    big = os.environ.get("AWR_D3_SCAN_BIG_IMAGE")
    if not big:
        pytest.skip("[optin] set AWR_D3_SCAN_BIG_IMAGE=<a local image larger than 4 GB>")
    rec: dict = {}
    bad = scan_image(big, trivy=t, record=rec)
    assert bad["detail"]["check"] == "size" and rec["inspect"]["size_b"] > IMAGE_MAX_B and "trivy" not in rec
