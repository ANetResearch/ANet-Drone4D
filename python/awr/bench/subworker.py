"""内部提交流水线（AWR-17 §18.23；M27 §6.7.9；D3-AC-56、59）：`python -m awr.bench.subworker --data-root <dir> [--once]
[--trivy-cache <dir>]`（命令行入口缺省要求漏洞扫描器，离线库目录亦可由 `AWR_TRIVY_CACHE` 给出；`--no-trivy` 只用于开发自检）。

按创建先后逐个处理 RECEIVED 的提交（所属榜单版本被平台复现审计冻结的跳过，`awr.bench.platform_audit`），每个阶段开始前重读状态
（撤回即停止）：

0. 镜像接入门（D3-AC-59；B25 遗留，`registry_gate`）：宿主 `regctl.py materialize`（d3-registry → 宿主 docker）为每个 RECEIVED
   提交写 `submissions/<id>/registry.json`；受邀成员（`_meta.role = invited`）的提交只有该记录 `status ∈ {pulled, present}` 才领取，
   `foreign_image`（镜像不在本团队命名空间）与 `digest_is_manifest`（以清单摘要提交）即 REJECTED 692 `scan`（`detail.check` 取该
   状态），尚无记录或 `not_found`、`pull_failed`、`error` 时留在 RECEIVED 等待接入，超过 `registry_wait_s`（自提交起，缺省 1800 s）
   仍未就绪即 REJECTED 692 `scan` `image_missing`（附 `registry_status`）；`--registry-gate all`（与 materialize 伴随容器同时部署）
   对全部角色要求接入记录，`off` 关闭（开发自检）。等待中的提交不阻塞队列中其后的提交；
   领取与准入：配置了漏洞扫描器时先自检（离线库缺失、损坏或库龄 > 30 天、扫描镜像缺失时抛 `ScannerUnavailable`，提交留在
   RECEIVED，库刷新后继续；库龄 > 7 天告警）；非开发榜（`track.dev` 不为真）再以 `spec_rules.check_submission_eval` 对榜单轮次
   配置求值，不满足时 REJECTED（633 或 682）；
1. SCANNING（D3-AC-59；ADR-204）：`docker image inspect <digest>`：镜像须已在 dmax（私有仓库推送或本机构建）、linux/amd64、
   未压缩大小 ≤ 4 GB（十进制，`IMAGE_MAX_B`）；再经 `TrivyScanner`：`docker save` 导出归档，在隔离的 d3-trivy 容器中以只读离线库扫描，
   HIGH 及以上且已有修复版本而镜像未修复的漏洞（`FixedVersion` 非空）存在即拒绝；不满足时 REJECTED 692 `scan`
   （`detail.check` 为 image_missing、inspect、platform、size、vulnerabilities）。扫描器自身故障（导出失败、超时、非 0 退出、输出无法
   解析）属平台原因：重试一次，仍失败则 FAILED 320（不放行）。扫描记录写入 `submissions/<id>/scan.json`（尺寸、库的 UpdatedAt 与
   DownloadedAt、Trivy 版本、可修复与无修复版本的漏洞计数）；
2. VALIDATING：冒烟运行（Fast 档、`smoke_s` 秒、公开冒烟种子 0）：每架机须完成握手并返回动作、无协议错误、容器以 0 退出；
   否则 REJECTED 692 `smoke`；
3. EVALUATING：按榜单版本的评测配置（`boards/<ver>/config.json`，秘密种子）逐种子正式运行（Realistic 档、隔离容器）；
   平台原因失败（Env 或物理后端异常）自动重跑一次并计入 `runs.platform_rerun`，仍失败则 FAILED；
   每个 Run（含冒烟）在墙钟上限 `10 × duration_s + 600 s` 内执行（`awr.bench.submissions.guarded_run`，D3-AC-59）：超限即终止（693），
   冒烟超限 REJECTED 693，正式 Run 超限记为无效行（`ranked_valid = false`，`invalid_reasons = [wall_limit]`）且不重跑；
   Run 的 GPU 时间计入提交方团队的每周 GPU 时间配额（平台原因失败的尝试不计）；
4. SCORING：评测行追加到 `rows.jsonl`，按 `score_version` 聚合本提交的四项指标与置信区间；`visibility = board` 时重算榜单并
   回填名次与层级 → DONE。

运行目录 `dataset/<event>/runs/<submission_id>-r<k>`（k 为种子序号，不暴露种子值）。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from awr.agentsdk.env.containers import check_launch, host_path, parse_docker_run
from awr.bench.boardstore import append_rows, load_config, publish, slim_evaluation
from awr.bench.platform_audit import is_frozen
from awr.bench.spec_rules import check_submission_eval, reason
from awr.bench.submissions import SubmissionStore, WallLimitExceeded, guarded_run, run_duration_s, wall_limit_s

__all__ = ["IMAGE_MAX_B", "REGISTRY_GATES", "REGISTRY_READY", "REGISTRY_WAIT_S", "SCAN_SEVERITIES", "ScanError", "ScannerUnavailable",
           "TrivyScanner", "process_one", "registry_gate", "scan_image", "vulnerability_findings"]

IMAGE_MAX_B = 4 * 1000 ** 3              # D3-AC-59"体积 > 4 GB 拒绝"：十进制 GB，按 `docker image inspect` 的 Size（未压缩）计（ADR-204 (4)）
SCAN_SEVERITIES = ("HIGH", "CRITICAL")   # "HIGH 及以上"
DB_WARN_DAYS, DB_MAX_DAYS = 7.0, 30.0    # 库龄告警与拒绝扫描（ADR-204 (3)）；库龄取 trivy-db 与 trivy-java-db 中较旧者的 UpdatedAt
LIST_MAX = 50                            # reason 与扫描记录中列出的漏洞条数上限（计数不受限）
REGISTRY_READY = ("pulled", "present")   # materialize 结论：镜像已在宿主且来源合规
REGISTRY_REJECT = ("foreign_image", "digest_is_manifest")   # 提交方原因、重试不会改变的结论
REGISTRY_WAIT_S = 1800.0                 # 自提交起等待镜像接入的上限
REGISTRY_GATES = ("off", "invited", "all")
_RFC3339 = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


class ScannerUnavailable(RuntimeError):
    """扫描器不可用（离线库缺失、损坏或过期，扫描镜像缺失）：不领取提交，提交留在 RECEIVED。"""


class ScanError(RuntimeError):
    """单次扫描的平台故障（`docker save` 失败、扫描超时、非 0 退出、输出无法解析）：重试一次，仍失败则 FAILED 320。"""


def _reason(why: str, **detail) -> dict:
    return {"code": 692, "name": "SUBMISSION_REJECTED", "detail": {"why": why, **detail}}


def _parse_time(s: str) -> dt.datetime:
    """Trivy metadata.json 的 RFC 3339 时间（纳秒小数位）→ aware datetime。"""
    m = _RFC3339.match(s or "")
    if not m:
        raise ValueError(f"bad timestamp {s!r}")
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return dt.datetime.fromisoformat(f"{m.group(1)}.{frac}{tz}")


def _utc(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return _utc(dt.datetime.now(dt.UTC))


def vulnerability_findings(report: dict, severities: tuple[str, ...] = SCAN_SEVERITIES) -> dict:
    """Trivy JSON 报告（SchemaVersion 2）→ `{fixable[], fixable_count, unfixed_count, by_severity}`。

    "HIGH 及以上未修复漏洞"取镜像未修复、上游已有修复版本的漏洞（`FixedVersion` 非空或 `Status = fixed`），与 Trivy
    `--ignore-unfixed` 的口径相同；上游尚无修复版本的漏洞（affected、will_not_fix 等）只计数（`unfixed_count`），不作为拒绝条件。
    同一漏洞在多个目标（层或清单文件）中重复出现时按 (漏洞, 包, 已装版本) 去重。"""
    sev = {x.upper() for x in severities}
    fixable: dict[tuple, dict] = {}
    unfixed: set[tuple] = set()
    for res in report.get("Results") or []:
        for v in res.get("Vulnerabilities") or []:
            if str(v.get("Severity", "")).upper() not in sev:
                continue
            key = (v.get("VulnerabilityID"), v.get("PkgName"), v.get("InstalledVersion"))
            if v.get("FixedVersion") or v.get("Status") == "fixed":
                fixable.setdefault(key, {"id": key[0], "pkg": key[1], "installed": key[2], "fixed": v.get("FixedVersion") or "",
                                         "severity": str(v["Severity"]).upper(), "target": res.get("Target", "")})
            else:
                unfixed.add(key)
    rows = sorted(fixable.values(), key=lambda r: (r["severity"] != "CRITICAL", str(r["id"]), str(r["pkg"])))
    by = {s: sum(r["severity"] == s for r in rows) for s in sorted(sev)}
    return {"fixable": rows, "fixable_count": len(rows), "unfixed_count": len(unfixed), "by_severity": by}


@dataclass
class TrivyScanner:
    """提交镜像漏洞扫描（ADR-204；D3-AC-59）：`docker save` 导出 → 隔离的 d3-trivy 容器以只读离线库扫描归档。

    扫描容器：`--network none`、只读根文件系统、`/tmp` 为 tmpfs、`--cap-drop ALL`、`no-new-privileges`、非 root、CPU 与内存上限，
    命名 `d3-subworker-scan-<提交>`（组件 subworker，启动前按 D3 容器清单规则自检）；只读挂载离线库与镜像归档，不挂载 docker 套接字，
    `--skip-db-update --skip-java-db-update --offline-scan`，`--cache-backend memory`（不写缓存目录）。
    `cache_dir`、`work_dir` 为本进程可见路径；本进程在容器中时 `-v` 源路径按 `AWR_HOST_PATH_MAP` 换算为宿主路径。"""
    cache_dir: Path
    work_dir: Path
    image: str = "d3-trivy:dev"
    docker: str = "docker"
    severities: tuple[str, ...] = SCAN_SEVERITIES
    cpus: float = 4.0
    mem_gb: float = 6.0
    tmp_gb: float = 2.0
    timeout_s: float = 900.0
    warn_days: float = DB_WARN_DAYS
    max_days: float = DB_MAX_DAYS

    def db_status(self, now: dt.datetime | None = None) -> dict:
        """→ `{db, java_db: {updated_at, downloaded_at}, age_days, warning}`；库缺失、损坏或库龄 > `max_days` 时抛 ScannerUnavailable。"""
        now = now or dt.datetime.now(dt.UTC)
        out: dict = {}
        ages = []
        for key, sub, db in (("db", "db", "trivy.db"), ("java_db", "java-db", "trivy-java.db")):
            d = Path(self.cache_dir) / sub
            try:
                meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
                up, down = _parse_time(meta["UpdatedAt"]), _parse_time(meta["DownloadedAt"])
            except (OSError, ValueError, KeyError) as e:
                raise ScannerUnavailable(f"trivy {sub}: metadata.json missing or invalid ({type(e).__name__})") from None
            if not (d / db).is_file():
                raise ScannerUnavailable(f"trivy {sub}: {db} missing")
            out[key] = {"version": meta.get("Version"), "updated_at": _utc(up), "downloaded_at": _utc(down)}
            ages.append((now - up).total_seconds() / 86400.0)
        out["age_days"] = round(max(ages), 3)
        if out["age_days"] > self.max_days:
            raise ScannerUnavailable(f"trivy db is {out['age_days']:.1f} days old (> {self.max_days:g}); refresh with "
                                     "tools/deploy/dmax/images/trivy/fetch.sh --db-only")
        out["warning"] = out["age_days"] > self.warn_days
        return out

    def ready(self, now: dt.datetime | None = None) -> dict:
        """领取提交前的自检：离线库可用且扫描镜像存在；→ `db_status()`。"""
        db = self.db_status(now)
        r = subprocess.run([self.docker, "image", "inspect", self.image, "--format", "{{.Id}}"], capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise ScannerUnavailable(f"scanner image {self.image} missing (tools/deploy/dmax/images/trivy/build.sh)")
        db["scanner_image"] = r.stdout.strip()
        return db

    def argv(self, name: str, archive: Path) -> list[str]:
        """扫描容器的完整 `docker run` 参数表。"""
        return [self.docker, "run", "--rm", "--name", name, "--label", "d3.component=subworker", "--network", "none", "--read-only",
                "--tmpfs", f"/tmp:rw,size={self.tmp_gb:g}g,mode=1777", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "256", "--cpus", f"{self.cpus:g}", "--memory", f"{self.mem_gb:g}g", "--user", "65534:65534",
                "-v", f"{host_path(Path(self.cache_dir))}:/cache:ro", "-v", f"{host_path(archive)}:/in/image.tar:ro", self.image,
                "image", "--input", "/in/image.tar", "--scanners", "vuln", "--severity", ",".join(self.severities), "--format", "json",
                "--quiet", "--skip-db-update", "--skip-java-db-update", "--offline-scan", "--skip-version-check", "--disable-telemetry",
                "--cache-dir", "/cache", "--cache-backend", "memory", "--timeout", f"{int(self.timeout_s)}s", "--exit-code", "0"]

    def scan(self, digest: str, name: str) -> dict:
        """→ Trivy JSON 报告；平台故障抛 ScanError。归档在扫描结束后删除，扫描容器超时后强制删除。"""
        name = re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:120]
        work = Path(self.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        archive = work / f"{name}.tar"
        argv = self.argv(name, archive)
        errs = [x for x in check_launch(parse_docker_run(argv, "subworker-scan", component="subworker")) if x.level == "error"]
        if errs:
            raise ScanError("container_manifest: " + "; ".join(f"{x.rule} {x.message}" for x in errs))
        try:
            r = subprocess.run([self.docker, "save", "-o", str(archive), digest], capture_output=True, text=True, check=False,
                               timeout=self.timeout_s)
            if r.returncode != 0:
                raise ScanError(f"docker save exit {r.returncode}")
            os.chmod(archive, 0o644)                                    # 扫描容器以 65534 读取
            try:
                r = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=self.timeout_s + 60)
            except subprocess.TimeoutExpired:
                subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False)
                raise ScanError("trivy timeout") from None
            if r.returncode != 0:
                raise ScanError(f"trivy exit {r.returncode}: {r.stderr.strip()[-300:]}")
            try:
                rep = json.loads(r.stdout)
            except ValueError:
                raise ScanError("trivy output is not JSON") from None
            if not isinstance(rep, dict) or rep.get("SchemaVersion") != 2:
                raise ScanError("unexpected trivy report schema")
            return rep
        except subprocess.TimeoutExpired:
            raise ScanError("docker save timeout") from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                archive.unlink()


def scan_image(digest: str, docker: str = "docker", *, trivy: TrivyScanner | None = None, name: str = "d3-subworker-scan",
               record: dict | None = None) -> dict | None:
    """→ None 表示通过，否则为 REJECTED 的 reason。`trivy` 为 None 时只做 `docker image inspect` 检查（测试替身与开发自检）；
    `record` 给出时就地填入扫描记录（inspect 摘要与 Trivy 结果）。Trivy 平台故障抛 ScanError（不在此处放行或拒绝）。"""
    rec = record if record is not None else {}
    r = subprocess.run([docker, "image", "inspect", digest, "--format", "{{json .}}"], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return _reason("scan", check="image_missing")
    try:
        info = json.loads(r.stdout)
    except ValueError:
        return _reason("scan", check="inspect")
    size = int(info.get("Size", 0))
    rec["inspect"] = {"os": info.get("Os"), "architecture": info.get("Architecture"), "size_b": size, "size_max_b": IMAGE_MAX_B}
    if info.get("Os") != "linux" or info.get("Architecture") != "amd64":
        return _reason("scan", check="platform")
    if size > IMAGE_MAX_B:
        return _reason("scan", check="size", size_b=size, size_max_b=IMAGE_MAX_B)
    if trivy is None:
        rec["trivy"] = None
        return None
    t0 = time.monotonic()
    rep = trivy.scan(digest, name)
    f = vulnerability_findings(rep, trivy.severities)
    rec["trivy"] = {"version": (rep.get("Trivy") or {}).get("Version"), "severities": list(trivy.severities), "wall_s": round(time.monotonic() - t0, 3),
                    "fixable_count": f["fixable_count"], "unfixed_count": f["unfixed_count"], "by_severity": f["by_severity"],
                    "fixable": f["fixable"][:LIST_MAX]}
    if f["fixable_count"]:
        return _reason("scan", check="vulnerabilities", count=f["fixable_count"], by_severity=f["by_severity"],
                       vulnerabilities=[{k: v[k] for k in ("id", "pkg", "installed", "fixed", "severity")} for v in f["fixable"][:LIST_MAX]])
    return None


def registry_gate(data_root: Path, doc: dict, *, now_ns: int, mode: str = "invited", wait_s: float = REGISTRY_WAIT_S) -> tuple[str, dict | None]:
    """镜像接入门（模块文档第 0 步）：→ (`ready` | `wait` | `reject`, reject 时的 reason)。

    接入记录 `submissions/<id>/registry.json` 由宿主 `regctl.py materialize` 写入（`awr.d3.registry_materialize.v1`）；记录的
    `image_digest` 与提交不同（不应出现）时按无记录处理。`mode = invited` 只约束受邀成员，内部成员无记录时照常领取（宿主本机构建的
    镜像），有记录时同样按记录判定；`all` 约束全部角色；`off` 一律放行。"""
    if mode not in REGISTRY_GATES:
        raise ValueError(f"registry gate {mode!r} (expected one of {REGISTRY_GATES})")
    if mode == "off":
        return "ready", None
    sid = doc["submission_id"]
    try:
        rec = json.loads((Path(data_root) / "submissions" / sid / "registry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rec = None
    if not isinstance(rec, dict) or rec.get("image_digest") != doc.get("image_digest"):
        rec = None
    status = rec.get("status") if rec else None
    if status in REGISTRY_READY:
        return "ready", None
    if status in REGISTRY_REJECT:
        extra = {"config_digest": rec["local_image_id"]} if status == "digest_is_manifest" and rec.get("local_image_id") else {}
        return "reject", _reason("scan", check=status, **extra)
    invited = (doc.get("_meta") or {}).get("role") == "invited"
    if rec is None and mode == "invited" and not invited:
        return "ready", None
    age_s = (int(now_ns) - int(doc["_meta"]["created_unix_ns"])) / 1e9
    if age_s > wait_s:
        return "reject", _reason("scan", check="image_missing", registry_status=status or "none", waited_s=round(age_s, 1))
    return "wait", None


def _alive(st: SubmissionStore, sid: str) -> bool:
    return st.get(sid)["state"] not in ("WITHDRAWN", "REJECTED", "FAILED", "DONE")


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def process_one(data_root: Path, *, docker: str = "docker", smoke_s: float = 20.0, n_boot: int = 10_000, run_job=None,
                trivy: TrivyScanner | None = None, log=print, registry: str = "invited", registry_wait_s: float = REGISTRY_WAIT_S) -> str | None:
    """处理一个 RECEIVED 提交；→ submission_id 或 None（队列为空或全部在等待镜像接入）。

    `trivy` 为 None 时不做漏洞扫描（测试替身与开发自检；命令行入口缺省要求扫描器，见 `main`）；扫描器不可用时抛
    ScannerUnavailable，提交保持 RECEIVED。`registry`、`registry_wait_s`：镜像接入门（`registry_gate`）。"""
    from awr.bench.eval.aggregate import lane_metrics
    if run_job is None:
        from awr.bench.runner import run_job

    data_root = Path(data_root)
    st = SubmissionStore(data_root / "submissions")
    d = None
    for x in st.all():                                       # 平台复现审计冻结的榜单版本：其提交留在 RECEIVED，解冻后继续
        if x["state"] != "RECEIVED" or is_frozen(data_root / "boards" / x["_meta"]["board_version"]):
            continue
        gate, why = registry_gate(data_root, x, now_ns=st.clock(), mode=registry, wait_s=registry_wait_s)
        if gate == "reject":                                 # 接入结论为提交方原因或等待超时：不领取，直接拒绝
            st.transition(x["submission_id"], "REJECTED", reason=why, expect="RECEIVED")
            log(f"{x['submission_id']}: REJECTED registry {why['detail']}")
            return x["submission_id"]
        if gate == "ready":
            d = x
            break
    if d is None:
        return None
    sid, team = d["submission_id"], d["_meta"]["team"]
    db = trivy.ready() if trivy is not None else None       # 库缺失或过期：抛 ScannerUnavailable，不领取
    if db is not None and db["warning"]:
        log(f"warning: trivy db is {db['age_days']:.1f} days old (> {trivy.warn_days:g}); refresh with tools/deploy/dmax/images/trivy/fetch.sh --db-only")
    bdir = data_root / "boards" / d["_meta"]["board_version"]
    cfg = load_config(bdir)
    if not (cfg.get("track") or {}).get("dev"):              # 正式榜：提交恒为排名运行，按榜单轮次配置求组合规则（633、682）
        bad = reason(check_submission_eval(cfg))
        if bad:
            st.transition(sid, "REJECTED", reason=bad, expect="RECEIVED")
            log(f"{sid}: REJECTED config {bad['code']} {bad['detail']['why']}")
            return sid
    rs = cfg["run_spec"]
    policy = {"kind": "image", "image_digest": d["image_digest"], "entry": d["entry"]}
    common = {"worlds_root": Path(cfg["worlds_root"]), "events_root": Path(cfg["events_root"]), "agent_image": cfg.get("agent_image", "d3-agent-py:dev"),
              "docker": docker}
    work = data_root / "submissions" / sid
    st.transition(sid, "SCANNING", expect="RECEIVED")
    rec: dict = {"submission_id": sid, "image_digest": d["image_digest"], "started_at": _now(), "db": db,
                 "errors": []}
    for attempt in (0, 1):
        try:
            bad = scan_image(d["image_digest"], docker, trivy=trivy, name=f"d3-subworker-scan-{sid}", record=rec)
            break
        except ScanError as e:                               # 平台原因：重试一次，仍失败则 FAILED（不放行）
            rec["errors"].append(str(e))
            log(f"{sid}: scan attempt {attempt} failed: {e}")
            if attempt == 1:
                rec.update(verdict="error", ended_at=_now())
                _write_json(work / "scan.json", rec)
                st.transition(sid, "FAILED", reason={"code": 320, "name": "INTERNAL", "detail": {"why": "platform", "stage": "scan"}})
                return sid
    rec.update(verdict="reject" if bad else "pass", reason=bad, ended_at=_now())
    _write_json(work / "scan.json", rec)
    if bad:
        st.transition(sid, "REJECTED", reason=bad)
        log(f"{sid}: REJECTED scan {bad['detail']}")
        return sid
    if not _alive(st, sid):
        return sid
    st.transition(sid, "VALIDATING", expect="SCANNING")
    try:
        r = guarded_run(run_job, st, sid, rs, limit_s=wall_limit_s(smoke_s), policy=policy, seed=0, run_id=f"{sid}-smoke", run_dir=work / "smoke",
                        physics="kinematic", duration_s=smoke_s, **common)
        ag = r["agents"]
        fail = {u: s for u, s in ag.items() if s.get("acts", 0) == 0 or s.get("protocol_errors") or s.get("exited") not in (None, "bye")
                or s.get("exit_code") not in (0, None)}
    except WallLimitExceeded as e:                           # 冒烟超墙钟上限：终止并拒绝（693）
        st.transition(sid, "REJECTED", reason=e.reason(stage="smoke"))
        log(f"{sid}: REJECTED smoke wall limit {e.limit_s:g} s")
        return sid
    except Exception as e:                                   # 握手超时、容器启动失败等均归为冒烟失败
        fail = {"error": getattr(e, "code", None) or type(e).__name__}
    if fail:
        st.transition(sid, "REJECTED", reason=_reason("smoke", agents=sorted(fail)))
        log(f"{sid}: REJECTED smoke {fail}")
        return sid
    if not _alive(st, sid):
        return sid
    st.transition(sid, "EVALUATING", expect="VALIDATING")
    rows, reruns = [], 0
    event = rs["event"]["id"]
    for k, seed in enumerate(cfg["seeds"]):
        run_dir = data_root / "dataset" / event / "runs" / f"{sid}-r{k}"
        wall = None
        for attempt in (0, 1):
            try:
                res = guarded_run(run_job, st, sid, rs, limit_s=wall_limit_s(run_duration_s(rs, common["events_root"])), policy=policy, seed=int(seed),
                                  run_id=f"{sid}-r{k}", run_dir=run_dir, physics=cfg.get("physics"), gz=cfg.get("gz") or None, **common)
                break
            except WallLimitExceeded as e:                   # 超墙钟上限：终止（693），记为无效行，不重跑
                wall = e
                log(f"{sid}: run r{k} exceeded the wall limit {e.limit_s:g} s")
                break
            except Exception:                               # 平台原因：重跑一次
                log(f"{sid}: run r{k} attempt {attempt} failed\n{traceback.format_exc()[-1500:]}")
                if attempt == 1:
                    st.transition(sid, "FAILED", reason={"code": 320, "name": "INTERNAL", "detail": {"why": "platform", "run": k}})
                    return sid
                reruns += 1
        if wall is not None:
            rows.append({"entry_id": f"{team}/{sid}", "team": team, "display_name": sid, "baseline": False, "track": cfg["track_id"],
                         "lane": cfg["lane"], "event_type": cfg.get("event_type", cfg["lane"]), "event": event, "seed": k, "submission_id": sid,
                         "evaluation": {"components": dict.fromkeys(("ec", "eu", "ao", "ce")), "ranked": True, "ranked_valid": False,
                                        "invalid_reasons": ["wall_limit"]}, "termination": wall.reason()})
            continue
        ev = json.loads((Path(res["run_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
        rows.append({"entry_id": f"{team}/{sid}", "team": team, "display_name": sid, "baseline": False, "track": cfg["track_id"],
                     "lane": cfg["lane"], "event_type": cfg.get("event_type", cfg["lane"]), "event": event, "seed": k, "submission_id": sid,
                     "evaluation": slim_evaluation(ev)})
    st.transition(sid, "SCORING", expect="EVALUATING")
    append_rows(bdir, rows)
    met, _boots = lane_metrics(rows, key=(f"{team}/{sid}", bdir.name, cfg["lane"]), n_boot=n_boot)
    valid = sum(r["evaluation"]["ranked_valid"] for r in rows)
    lane = {"lane": cfg["lane"], "metrics": met, "runs": {"total": len(rows), "valid": valid, "invalid": len(rows) - valid, "platform_rerun": reruns},
            "status": "valid" if valid >= 0.8 * len(rows) else "invalid", "composite": None}
    st.transition(sid, "DONE", result={"lanes": [lane]})
    if st.get(sid)["visibility"] == "board":
        board = publish(bdir, st.board_view(), n_boot=n_boot)
        e = next((x for x in board["entries"] if x["entry_id"] == f"{team}/{sid}"), None)
        if e is not None:
            lane["metrics"] = e["metrics"]
            if e["dominance_tier"] is not None:
                lane["dominance_tier"] = e["dominance_tier"]
            doc = st.get(sid)
            doc["result"] = {"lanes": [lane]}
            st._put(doc)
    log(f"{sid}: DONE {json.dumps({m: v['value'] for m, v in met.items()})}")
    return sid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.bench.subworker")
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--once", action="store_true", help="process at most one submission and exit")
    ap.add_argument("--poll-s", type=float, default=15.0)
    ap.add_argument("--smoke-s", type=float, default=20.0)
    ap.add_argument("--n-boot", type=int, default=10_000)
    ap.add_argument("--trivy-cache", type=Path, default=os.environ.get("AWR_TRIVY_CACHE") or None,
                    help="offline Trivy cache with db/ and java-db/ (env AWR_TRIVY_CACHE)")
    ap.add_argument("--trivy-image", default=os.environ.get("AWR_TRIVY_IMAGE") or "d3-trivy:dev")
    ap.add_argument("--scan-dir", type=Path, default=None, help="where image archives are staged (default <data-root>/scan)")
    ap.add_argument("--no-trivy", action="store_true", help="skip the vulnerability scan (development only)")
    ap.add_argument("--registry-gate", choices=REGISTRY_GATES, default=os.environ.get("AWR_REGISTRY_GATE") or "invited",
                    help="require the host materialize record submissions/<id>/registry.json (status pulled or present) before claiming: "
                         "invited members only (default), all roles, or off (env AWR_REGISTRY_GATE)")
    ap.add_argument("--registry-wait-s", type=float, default=REGISTRY_WAIT_S,
                    help="reject with 692 image_missing when the image is still not materialized this long after submission")
    a = ap.parse_args(argv)
    if a.no_trivy:
        trivy = None
    elif a.trivy_cache is None:
        ap.error("the vulnerability scan needs --trivy-cache or AWR_TRIVY_CACHE (use --no-trivy for development only)")
    else:
        trivy = TrivyScanner(cache_dir=a.trivy_cache, work_dir=a.scan_dir or a.data_root / "scan", image=a.trivy_image)

    def log(m: str) -> None:
        print(m, flush=True)

    while True:
        try:
            sid = process_one(a.data_root, smoke_s=a.smoke_s, n_boot=a.n_boot, trivy=trivy, log=log, registry=a.registry_gate,
                              registry_wait_s=a.registry_wait_s)
        except ScannerUnavailable as e:                       # 不领取提交；库刷新或扫描镜像就绪后继续
            log(f"scanner unavailable: {e}")
            if a.once:
                return 3
            time.sleep(a.poll_s)
            continue
        if a.once:
            return 0
        if sid is None:
            time.sleep(a.poll_s)


if __name__ == "__main__":
    sys.exit(main())
