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

运行收尾（K17；D3-AC-22、M27-FR-171、M27 §7.3）：
- 每个正式 Run 结束后清理运行目录中残留的 AF_UNIX 套接字（`retention.clean_run_sockets`），并写运行清单 `run_manifest.json`
  （`bench/run_manifest.v1`）：`attempt`（1 起，平台原因重跑为 2）、`restart_count`、`timing{queued_unix_ns, started_unix_ns,
  ended_unix_ns}`（另有 Env 墙钟、仿真时长与实时因子）；排名运行只含 `seed_commit`。旧版榜单配置（非 v1）只有在配置给出
  `evalstore`（含 `k_eval` 的服务端文件，`mbl_e2e` 原型榜写入）时才签发种子承诺值并写清单，否则排名运行不写清单（不以明文种子代替）；
- 评分完成（DONE）后对本提交的排名 Run 执行 `ranked_eval` 保留（`retention.finalize_ranked_eval`：每个"提交 × test 事件"抽 1 个
  Run 保留完整录制 30 天，其余精简为逐帧摘要与 `truth_min`，重复运行摘要相同时去重），结果写 `submissions/<id>/retention.json`；
  开发榜（`track.dev`）与非排名运行不处理；保留步骤失败只记日志与该文件，不改变提交状态。

租约与孤立提交的恢复（K17；B21 遗留 2）：领取提交时取得 `submissions/<id>/worker.lock` 的排他文件锁（非阻塞，取不到即视为已由
其他进程领取），处理期间持有，并由心跳线程每 `heartbeat_s`（缺省 60 s）刷新租约 `submissions/<id>/lease.json`
（`drone4d.bench.subworker_lease.v1`：worker、主机、进程号、心跳与到期时刻，租期 `lease_s` 缺省 600 s）。每次领取新提交之前先
检查停在 EVALUATING 的提交：文件锁可取得且租约已到期（无租约文件时以提交文档的最后更新时刻起算）即视为处理进程已不存在，按
`board_v1_formal.py resume_submission` 同一语义自动恢复：已完成且已记 GPU 用量的 Run 沿用其评测结果（墙钟超限的按 `termination.json`
复原为无效行），其余 Run 删除残留后重新运行（中断的 Run 计入平台重跑），再评分并上榜；已记 GPU 用量却没有评测结果与终止记录的
Run 无法判定，提交 FAILED 320 `orphan_unresolved`（需人工处理）。恢复记入 `_meta.recovered[]`。

排行榜 v1（`aggregate.is_v1`：`drone4d.bench.board_config.v1`，`d3-score-1`；D3-AC-43、57；M27-FR-160、FR-166、FR-183）：

- 领取时的版本衔接（B18 决定）：登记的榜单版本已不是当前版本时，当前版本为 v1 且接收该分轨与所选赛道（`aggregate.admits`）
  即改登记到当前版本（`_meta.board_version`，`_meta.moved[]` 留痕），否则 REJECTED 680 `board_frozen`（不计入配额）；
- 准入：配置的服务端运行参数（`v1_problems`：`worlds_root`、`events_root`、`events{<test 事件>: {world_id, version, starts[],
  events_root?}}`、冒烟规格 `smoke.run_spec`，冒烟事件不得为 test 事件）不全时 FAILED 320 `board_config`（平台原因）；
  再以 `spec_rules.check_ranked_eval` 对该分轨的排名 Run 规格求值（633、682）；
- 冒烟：`smoke.run_spec`（dev 事件、Fast 档、公开冒烟种子 0），不在 test 事件上运行；
- 正式评测：按 `aggregate.expand_runs` 展开"所选赛道 × test 事件 × 种子序号 k × 重复 rep"，每个 Run 以 `aggregate.run_spec_for`
  生成排名规格（`ranked = true`、`physics = realistic`），种子明文按（赛道、事件、k）取自 EvalStore（先以 `verify_lock` 复核封板记录，
  失败关闭），`seed_commit = HMAC-SHA256(K_eval, run_id ‖ ":" ‖ seed)` 写入运行清单；Run 标识 `<submission_id>-<event>-k<k>r<rep>`，
  评测行只含种子序号与重复序号，日志中的种子明文以 `<seed>` 代替；
- 只收 gz 锁步档：评测行的 `evaluation.physics` 取运行清单的实际物理档，任一行不是 gz 锁步档时不写入（FAILED 320 `physics`）；
- 评分：每个所选赛道以 `lane_metrics(secret = K_board)` 计算本提交的四项指标与置信区间（与榜单同一口径），上榜后回填名次与层级。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from awr.agentsdk.env.containers import check_launch, host_path, parse_docker_run
from awr.bench.boardstore import append_rows, current_version, gz_row, load_config, publish, slim_evaluation, versions
from awr.bench.eval.aggregate import (
    BoardConfigError,
    admits,
    board_key,
    expand_runs,
    is_v1,
    rng_scope,
    run_spec_for,
    verify_lock,
)
from awr.bench.platform_audit import is_frozen
from awr.bench.retention import RankedRun, clean_run_sockets, finalize_ranked_eval
from awr.bench.spec_rules import check_ranked_eval, check_submission_eval, reason
from awr.bench.submissions import SubmissionStore, WallLimitExceeded, guarded_run, run_duration_s, wall_limit_s

__all__ = ["HEARTBEAT_S", "IMAGE_MAX_B", "LEASE_S", "REGISTRY_GATES", "REGISTRY_READY", "REGISTRY_WAIT_S", "SCAN_SEVERITIES", "V1_EVENT_KEYS",
           "Lease", "ScanError", "ScannerUnavailable", "TrivyScanner", "process_one", "recover_orphans", "registry_gate", "scan_image",
           "seed_commit", "v1_problems", "vulnerability_findings"]

IMAGE_MAX_B = 4 * 1000 ** 3              # D3-AC-59"体积 > 4 GB 拒绝"：十进制 GB，按 `docker image inspect` 的 Size（未压缩）计（ADR-204 (4)）
SCAN_SEVERITIES = ("HIGH", "CRITICAL")   # "HIGH 及以上"
DB_WARN_DAYS, DB_MAX_DAYS = 7.0, 30.0    # 库龄告警与拒绝扫描（ADR-204 (3)）；库龄取 trivy-db 与 trivy-java-db 中较旧者的 UpdatedAt
LIST_MAX = 50                            # reason 与扫描记录中列出的漏洞条数上限（计数不受限）
REGISTRY_READY = ("pulled", "present")   # materialize 结论：镜像已在宿主且来源合规
REGISTRY_REJECT = ("foreign_image", "digest_is_manifest")   # 提交方原因、重试不会改变的结论
REGISTRY_WAIT_S = 1800.0                 # 自提交起等待镜像接入的上限
REGISTRY_GATES = ("off", "invited", "all")
V1_EVENT_KEYS = ("world_id", "version", "starts")   # 排行榜 v1 配置 `events.<test 事件>` 的必备键（服务端运行参数，不含秘密）
LEASE_S = 600.0                          # 租期：心跳超过此时长未刷新视为处理进程已不存在
HEARTBEAT_S = 60.0                       # 心跳间隔
LEASE_SCHEMA = "drone4d.bench.subworker_lease.v1"
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


# ------------------------------------------------------------------------------------------------------------ 租约
class Lease:
    """一个提交的处理租约（模块文档"租约与孤立提交的恢复"）：`acquire()` 以非阻塞排他文件锁领取（取不到返回 False），
    成功后写租约并启动心跳线程；`release()` 停止心跳、删除租约并释放文件锁。可作上下文管理器使用。"""

    def __init__(self, data_root: Path, sid: str, *, lease_s: float = LEASE_S, heartbeat_s: float = HEARTBEAT_S,
                 clock=time.time_ns) -> None:
        self.dir = Path(data_root) / "submissions" / sid
        self.sid, self.lease_s, self.heartbeat_s, self.clock = sid, float(lease_s), float(heartbeat_s), clock
        self.worker = f"{socket.gethostname()}:{os.getpid()}"
        self._f = None
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.claimed_unix_ns: int | None = None

    @property
    def path(self) -> Path:
        return self.dir / "lease.json"

    def acquire(self) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        f = (self.dir / "worker.lock").open("a")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        self.claimed_unix_ns = self.clock()
        self.beat()
        self._t = threading.Thread(target=self._run, name=f"lease-{self.sid}", daemon=True)
        self._t.start()
        return True

    def beat(self) -> None:
        now = self.clock()
        _write_json(self.path, {"schema": LEASE_SCHEMA, "submission_id": self.sid, "worker": self.worker, "host": socket.gethostname(),
                                "pid": os.getpid(), "claimed_unix_ns": self.claimed_unix_ns, "heartbeat_unix_ns": now,
                                "expires_unix_ns": now + int(self.lease_s * 1e9)})

    def _run(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            with contextlib.suppress(OSError):
                self.beat()

    def release(self) -> None:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=5.0)
        with contextlib.suppress(OSError):
            self.path.unlink()
        if self._f is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
            self._f = None

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    @staticmethod
    def expired(data_root: Path, doc: dict, *, now_ns: int, lease_s: float = LEASE_S) -> bool:
        """租约是否已到期：租约文件的 `expires_unix_ns` ≤ now；无租约文件时以提交文档的 `_meta.updated_unix_ns` + 租期判定。"""
        try:
            lease = json.loads((Path(data_root) / "submissions" / doc["submission_id"] / "lease.json").read_text(encoding="utf-8"))
            return int(lease["expires_unix_ns"]) <= now_ns
        except (OSError, ValueError, KeyError, TypeError):
            return int(doc["_meta"].get("updated_unix_ns") or 0) + int(lease_s * 1e9) <= now_ns


# ------------------------------------------------------------------------------------------------------------ 运行收尾
def _write_submission_manifest(run_dir: Path, rs: dict, policy: dict, *, run_id: str, seed: int, seed_commit: str | None, worlds_root: Path,
                               events_root: Path, physics: str | None, gz: dict | None, experiment_id: str, split: str, attempt: int,
                               timing: dict, log) -> bool:
    """提交 Run 的运行清单（模块文档"运行收尾"）；→ 是否写出。排名运行缺种子承诺值时不写；清单缺陷只记日志（种子明文已脱敏）。"""
    from awr.agentsdk.env.spec import EnvSpec
    from awr.bench.eval.evaluator import run_manifest, write_run_manifest

    run_dir = Path(run_dir)
    if rs.get("ranked") and not seed_commit:
        log(f"{run_id}: run_manifest not written (ranked run without a seed commitment: the board config has no evalstore)")
        return False
    try:
        ev = json.loads((run_dir / "evaluation.json").read_text(encoding="utf-8")) if (run_dir / "evaluation.json").is_file() else None
        rs_p = {**rs, "policy": {k: v for k, v in policy.items() if k in ("kind", "name", "image_digest", "params")}}
        spec = EnvSpec.from_run_spec(rs_p, run_id=run_id, seed=int(seed), worlds_root=Path(worlds_root), events_root=Path(events_root),
                                     out_dir=run_dir, physics=physics, gz=gz or None, seed_commit=seed_commit)
        doc = run_manifest(spec, run_dir, ev, experiment_id=experiment_id, attempt=int(attempt), restart_count=int(attempt) - 1, split=split,
                           timing=timing)
        write_run_manifest(run_dir, doc)
        return True
    except Exception as e:                                   # 清单缺陷不改变 Run 状态
        log(_scrub(f"{run_id}: run_manifest not written: {type(e).__name__}: {e}"[:600], seed))
        return False


def _retain(data_root: Path, d: dict, runs: list[tuple[Path, str, int, int]], *, log) -> dict | None:
    """DONE 之后的 `ranked_eval` 保留（模块文档"运行收尾"）：`runs` 为 (运行目录, 事件, 种子序号, 重复序号)；结果写
    `submissions/<id>/retention.json`。失败只记日志。"""
    sid = d["submission_id"]
    try:
        out = finalize_ranked_eval([RankedRun(Path(rd), sid, ev, int(k), int(rep)) for rd, ev, k, rep in runs], root=Path(data_root),
                                   tenant=d["_meta"]["team"])
        ph = [x.get("phase") for x in out.values()]
        res = {"submission_id": sid, "t_utc": _now(), "runs": out, "totals": {p: ph.count(p) for p in sorted(set(ph), key=str)},
               "dup": sum(1 for x in out.values() if x.get("dup_of"))}
    except Exception as e:                                   # 保留步骤失败不改变提交状态（retention_gc 与回填可补做）
        res = {"submission_id": sid, "t_utc": _now(), "error": f"{type(e).__name__}: {e}"[:600]}
        log(f"{sid}: ranked_eval retention failed: {res['error']}")
    _write_json(Path(data_root) / "submissions" / sid / "retention.json", res)
    if "totals" in res:
        log(f"{sid}: ranked_eval retention {json.dumps(res['totals'])}, dup {res['dup']}")
    return res


# ------------------------------------------------------------------------------------------------------------ 排行榜 v1
def seed_commit(k_eval: bytes, run_id: str, seed: int) -> str:
    """排名运行的种子承诺值 `HMAC-SHA256(K_eval, run_id ‖ ":" ‖ seed)`（M27-FR-005）。"""
    return hmac.new(k_eval, f"{run_id}:{int(seed)}".encode(), hashlib.sha256).hexdigest()


def v1_problems(cfg: dict, track: str, lanes: list[str] | None = None) -> list[str]:
    """排行榜 v1 轮次配置中提交评测所需的服务端运行参数（不含秘密、不属于冻结项）的检查：`worlds_root`、`events_root`、所选赛道
    每个 test 事件的 `events.<id>`（`V1_EVENT_KEYS`，起飞位姿个数等于分轨机数）与冒烟规格 `smoke.run_spec`（dev 事件，不得为
    test 事件）。→ 问题列表，空为可评测。"""
    p = [f"{k} is required" for k in ("worlds_root", "events_root") if not cfg.get(k)]
    t = next((x for x in cfg.get("tracks", []) if x.get("track") == track), None)
    if t is None:
        return [*p, f"track {track} is not registered"]
    evs = cfg.get("events") or {}
    test = set()
    for ln in cfg["lock"]["lanes"]:
        test |= set(ln["test_events"])
        if lanes and ln["lane"] not in lanes:
            continue
        for e in ln["test_events"]:
            x = evs.get(e)
            if not isinstance(x, dict) or any(x.get(k) in (None, "", []) for k in V1_EVENT_KEYS):
                p.append(f"events.{e} needs {list(V1_EVENT_KEYS)}")
            elif len(x["starts"]) != int(t["n_uav"]):
                p.append(f"events.{e} gives {len(x['starts'])} start poses, track {track} flies {t['n_uav']}")
    sm = (cfg.get("smoke") or {}).get("run_spec")
    if not isinstance(sm, dict) or not (sm.get("event") or {}).get("id"):
        p.append("smoke.run_spec (a run spec on a dev event) is required")
    elif sm["event"]["id"] in test:
        p.append(f"smoke.run_spec runs on test event {sm['event']['id']}: smoke runs only on dev events")
    return p


def _evalstore(cfg: dict) -> dict:
    """排行榜 v1 的 EvalStore（种子明文与 K_eval，只在服务端读取）：先以明文复核封板记录，缺失或不符时抛 BoardConfigError（失败关闭）。"""
    try:
        store = json.loads(Path(str(cfg.get("evalstore") or "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BoardConfigError(f"board {cfg.get('board_version')}: evalstore is not readable") from None
    p = verify_lock(cfg["lock"], store, seeds_per_event=int(cfg["seeds_per_event"]))
    if p:
        raise BoardConfigError(f"board {cfg.get('board_version')}: " + "; ".join(p))
    return store


def _scrub(text: str, seed: int) -> str:
    """日志文本中的种子明文（十进制与十六进制）以 `<seed>` 代替。"""
    for s in sorted({str(int(seed)), f"{int(seed):x}", f"{int(seed):#x}"}, key=len, reverse=True):
        text = text.replace(s, "<seed>")
    return text


def _run_physics(run_dir: Path) -> str | None:
    """运行清单（Env `manifest.json`）记录的实际物理档；不可读时为 None（由评测行的其他声明与评测器口径把关）。"""
    try:
        return json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8")).get("physics")
    except (OSError, ValueError):
        return None


def _requeue_v1(data_root: Path, st: SubmissionStore, d: dict, log) -> tuple[Path, dict] | None:
    """领取时的版本衔接（模块文档"排行榜 v1"第 1 条）：→ (榜单目录, 配置)；改登记到当前版本时一并返回当前版本；不能衔接时
    REJECTED 680 `board_frozen` 并返回 None。"""
    root = data_root / "boards"
    bv = d["_meta"]["board_version"]
    cfg = load_config(root / bv)
    cur = current_version(root, versions(root))
    if cur is None or cur == bv:
        return root / bv, cfg
    ccfg = load_config(root / cur) if (root / cur / "config.json").is_file() else {}
    if is_v1(ccfg) and admits(ccfg, d["track"], d.get("lanes")) is None and not is_frozen(root / cur):
        doc = st.get(d["submission_id"])
        doc["_meta"]["board_version"] = cur
        doc["_meta"].setdefault("moved", []).append({"from": bv, "to": cur, "t_unix_ns": st.clock()})
        st._put(doc)
        log(f"{d['submission_id']}: board version {bv} is no longer current, re-registered to {cur}")
        return root / cur, ccfg
    st.transition(d["submission_id"], "REJECTED", expect="RECEIVED",
                  reason={"code": 680, "name": "RUN_STATE_CONFLICT", "detail": {"why": "board_frozen", "board_version": bv}})
    log(f"{d['submission_id']}: REJECTED 680 board_frozen ({bv}, current {cur})")
    return None


def _v1_runs(cfg: dict, d: dict, policy: dict) -> list[dict]:
    """提交的正式评测展开（不含种子明文）：→ [{lane, event_type, event, seed_index, rep, run_id, rs, events_root}]。"""
    sid = d["submission_id"]
    etype = {ln["lane"]: ln.get("event_type") or ln["lane"] for ln in cfg["lock"]["lanes"]}
    rs_policy = {"kind": "image", "image_digest": policy["image_digest"]}
    out = []
    for it in expand_runs(cfg, d["track"], d.get("lanes") or None):
        x = cfg["events"][it["event"]]
        rs = run_spec_for(cfg, d["track"], world_id=str(x["world_id"]), event_id=it["event"], event_version=int(x["version"]),
                          starts=list(x["starts"]), policy=rs_policy)
        if x.get("duration_s"):
            rs["duration_s"] = float(x["duration_s"])
        out.append({"lane": it["lane"], "event_type": etype[it["lane"]], "event": it["event"], "seed_index": it["seed_index"], "rep": it["rep"],
                    "run_id": f"{sid}-{it['event']}-k{it['seed_index']}r{it['rep']}", "rs": rs,
                    "events_root": Path(x.get("events_root") or cfg["events_root"])})
    return out


def _v1_base(d: dict, it: dict) -> dict:
    """排行榜 v1 提交 Run 的评测行（不含 evaluation）。"""
    sid, team = d["submission_id"], d["_meta"]["team"]
    return {"entry_id": f"{team}/{sid}", "team": team, "display_name": sid, "baseline": False, "track": d["track"], "lane": it["lane"],
            "event_type": it["event_type"], "event": it["event"], "seed": it["seed_index"], "rep": it["rep"], "run_id": it["run_id"],
            "submission_id": sid}


def _evaluate_v1(data_root: Path, st: SubmissionStore, d: dict, cfg: dict, runs: list[dict], *, policy: dict, common: dict, run_job,
                 log) -> tuple[list[dict], int] | None:
    """EVALUATING（排行榜 v1）：逐 Run 正式运行并生成评测行；→ (评测行, 平台原因重跑次数)，平台原因两次失败时 FAILED 并返回 None。"""
    sid = d["submission_id"]
    store = _evalstore(cfg)
    k_eval = bytes.fromhex(store["k_eval"])
    rows, reruns = [], 0
    for it in runs:
        seed = int(store["seeds"][it["lane"]][it["event"]][it["seed_index"]])
        run_id, run_dir = it["run_id"], data_root / "dataset" / it["event"] / "runs" / it["run_id"]
        base = _v1_base(d, it)
        wall = None
        sc = seed_commit(k_eval, run_id, seed)
        t_q = time.time_ns()
        for attempt in (0, 1):
            t0 = time.time_ns()
            try:
                guarded_run(run_job, st, sid, it["rs"], limit_s=wall_limit_s(run_duration_s(it["rs"], it["events_root"])), policy=policy, seed=seed,
                            run_id=run_id, run_dir=run_dir, physics=cfg["physics"], gz=cfg.get("gz") or None,
                            seed_commit=sc, **{**common, "events_root": it["events_root"]})
                break
            except WallLimitExceeded as e:                   # 超墙钟上限：终止（693），记为无效行，不重跑
                wall = e
                log(f"{sid}: run {run_id} exceeded the wall limit {e.limit_s:g} s")
                break
            except Exception:                               # 平台原因：重跑一次（日志不含种子明文）
                log(_scrub(f"{sid}: run {run_id} attempt {attempt} failed\n{traceback.format_exc()[-1500:]}", seed))
                if attempt == 1:
                    clean_run_sockets(run_dir)
                    st.transition(sid, "FAILED", reason={"code": 320, "name": "INTERNAL", "detail": {"why": "platform", "run": run_id}})
                    return None
                reruns += 1
        clean_run_sockets(run_dir)
        if wall is not None:
            rows.append({**base, "evaluation": {"components": dict.fromkeys(("ec", "eu", "ao", "ce")), "ranked": True, "ranked_valid": False,
                                                "invalid_reasons": ["wall_limit"], "physics": cfg["physics"]}, "termination": wall.reason()})
            continue
        _write_submission_manifest(run_dir, it["rs"], policy, run_id=run_id, seed=seed, seed_commit=sc, worlds_root=common["worlds_root"],
                                   events_root=it["events_root"], physics=cfg["physics"], gz=cfg.get("gz") or None,
                                   experiment_id=f"board-{cfg.get('board_version')}", split="test", attempt=attempt + 1,
                                   timing={"queued_unix_ns": t_q, "started_unix_ns": t0, "ended_unix_ns": time.time_ns()}, log=log)
        ev = json.loads((run_dir / "evaluation.json").read_text(encoding="utf-8"))
        rows.append({**base, "evaluation": slim_evaluation(ev, physics=_run_physics(run_dir))})
    return rows, reruns


def _score_v1(st: SubmissionStore, d: dict, cfg: dict, bdir: Path, rows: list[dict], reruns: int, *, n_boot: int, log) -> None:
    """SCORING（排行榜 v1）：只收 gz 锁步档的评测行；逐赛道计算本提交的四项指标（K_board 置信区间，与榜单同一口径）→ DONE，
    上榜时重算榜单并回填名次与层级。"""
    from awr.bench.eval.aggregate import lane_metrics

    sid, team = d["submission_id"], d["_meta"]["team"]
    bad = sorted(r["run_id"] for r in rows if not gz_row(r))
    if bad:                                                  # 平台以非 gz 锁步档运行：不写入榜单（D3-AC-43）
        st.transition(sid, "FAILED", reason={"code": 320, "name": "INTERNAL", "detail": {"why": "physics", "runs": len(bad)}})
        log(f"{sid}: FAILED {len(bad)} runs are not gz lockstep ({bad[:3]})")
        return
    st.transition(sid, "SCORING", expect="EVALUATING")
    now = st.clock()
    rows = [{**r, "first_listed_unix_ns": now} for r in rows]
    append_rows(bdir, rows)
    secret = board_key(cfg)
    eid = f"{team}/{sid}"
    lanes = []
    for lane in dict.fromkeys(r["lane"] for r in rows):
        rs = sorted((r for r in rows if r["lane"] == lane), key=lambda r: (r["event"], r["seed"], r["rep"], r["run_id"]))
        valid = [r for r in rs if r["evaluation"]["ranked_valid"]]
        met = (lane_metrics(valid, key=(eid, rng_scope(cfg), lane), n_boot=n_boot, secret=secret)[0] if valid
               else {m: {"value": None, "ci95": [0.0, 0.0]} for m in ("ec", "eu", "ao", "ce")})
        lanes.append({"lane": lane, "metrics": met, "runs": {"total": len(rs), "valid": len(valid), "invalid": len(rs) - len(valid),
                                                             "platform_rerun": reruns},
                      "status": "valid" if len(valid) >= 0.8 * len(rs) else "invalid", "composite": None})
    st.transition(sid, "DONE", result={"lanes": lanes})
    if st.get(sid)["visibility"] == "board":
        board = publish(bdir, st.board_view(), n_boot=n_boot)
        ent = {e["lane"]: e for e in board["entries"] if e["entry_id"] == eid and e["track"] == d["track"]}
        for lane in lanes:
            e = ent.get(lane["lane"])
            if e is not None:
                lane["metrics"], lane["status"] = e["metrics"], e["status"]
                if e["dominance_tier"] is not None:
                    lane["dominance_tier"] = e["dominance_tier"]
        doc = st.get(sid)
        doc["result"] = {"lanes": lanes}
        st._put(doc)
    log(f"{sid}: DONE " + json.dumps({ln["lane"]: {m: v["value"] for m, v in ln["metrics"].items()} for ln in lanes}))
    data_root = Path(st.root).parent
    _retain(data_root, st.get(sid), [(data_root / "dataset" / r["event"] / "runs" / r["run_id"], r["event"], r["seed"], r.get("rep", 0))
                                     for r in rows], log=log)


# ------------------------------------------------------------------------------------------------------------ 孤立提交的恢复
def _common(cfg: dict, docker: str) -> dict:
    return {"worlds_root": Path(cfg["worlds_root"]), "events_root": Path(cfg["events_root"]), "agent_image": cfg.get("agent_image", "d3-agent-py:dev"),
            "docker": docker}


def _resume(data_root: Path, st: SubmissionStore, d: dict, *, docker: str, n_boot: int, run_job, log, worker: str) -> None:
    """恢复一个孤立的 EVALUATING 提交（模块文档"租约与孤立提交的恢复"；与 `board_v1_formal.py resume_submission` 同语义）。"""
    sid = d["submission_id"]
    bdir = data_root / "boards" / d["_meta"]["board_version"]
    cfg = load_config(bdir)
    v1 = is_v1(cfg)
    policy = {"kind": "image", "image_digest": d["image_digest"], "entry": d["entry"]}
    common = _common(cfg, docker)
    charged = {g["run_id"] for g in d["_meta"].get("gpu", [])}
    if v1:
        items = [(it, data_root / "dataset" / it["event"] / "runs" / it["run_id"], it["run_id"]) for it in _v1_runs(cfg, d, policy)]
    else:
        ev = cfg["run_spec"]["event"]["id"]
        items = [(k, data_root / "dataset" / ev / "runs" / rid, rid) for k, _seed, rid in _legacy_runs(cfg, d)]
    done: dict[str, dict] = {}
    todo, aborted = [], []
    for it, run_dir, rid in items:
        term = run_dir / "termination.json"
        if rid in charged:                                   # guarded_run 已返回（含墙钟超限）：沿用其结果
            if term.is_file():
                t = json.loads(term.read_text(encoding="utf-8"))
                tr = {"code": t.get("code", 693), "name": t.get("name", "RUN_WALL_LIMIT_EXCEEDED"),
                      "detail": {k: v for k, v in (t.get("detail") or {}).items() if k != "stage"}}
                if v1:
                    done[rid] = {**_v1_base(d, it), "evaluation": {"components": dict.fromkeys(("ec", "eu", "ao", "ce")), "ranked": True,
                                                                   "ranked_valid": False, "invalid_reasons": ["wall_limit"], "physics": cfg["physics"]},
                                 "termination": tr}
                else:
                    done[rid] = _wall_row(cfg, d, it, tr)
            elif (run_dir / "evaluation.json").is_file():
                evd = json.loads((run_dir / "evaluation.json").read_text(encoding="utf-8"))
                done[rid] = ({**_v1_base(d, it), "evaluation": slim_evaluation(evd, physics=_run_physics(run_dir))} if v1
                             else _legacy_row(cfg, d, it, slim_evaluation(evd)))
            else:
                st.transition(sid, "FAILED", reason={"code": 320, "name": "INTERNAL", "detail": {"why": "orphan_unresolved", "run": rid}})
                log(f"{sid}: FAILED orphan_unresolved (run {rid} was charged but has no evaluation or termination record)")
                return
            continue
        if run_dir.exists():
            shutil.rmtree(run_dir)                           # 中断 Run 的残留（含运行真值），重新运行
            aborted.append(rid)
        todo.append(it)
    doc = st.get(sid)
    doc["_meta"].setdefault("recovered", []).append({"t_unix_ns": time.time_ns(), "worker": worker, "reused": len(done), "interrupted": aborted,
                                                     "rerun": len(todo)})
    st._put(doc)
    log(f"{sid}: recovering orphaned EVALUATING submission: {len(done)} runs reused, {len(aborted)} interrupted, {len(todo)} to run")
    if v1:
        got = _evaluate_v1(data_root, st, d, cfg, todo, policy=policy, common=common, run_job=run_job, log=log)
        if got is not None:
            by_id = {**done, **{r["run_id"]: r for r in got[0]}}
            _score_v1(st, st.get(sid), cfg, bdir, [by_id[rid] for _it, _rd, rid in items], got[1] + len(aborted), n_boot=n_boot, log=log)
        return
    got = _evaluate_legacy(data_root, st, d, cfg, cfg["run_spec"], policy=policy, common=common, run_job=run_job, log=log,
                           experiment_id=f"board-{bdir.name}", only=set(todo))
    if got is not None:
        by_k = {k: r for (k, _rd, rid) in items for r in [done.get(rid)] if r is not None}
        _score_legacy(st, st.get(sid), cfg, bdir, {**by_k, **got[0]}, got[1] + len(aborted), n_boot=n_boot, log=log)


def recover_orphans(data_root: Path, *, docker: str = "docker", n_boot: int = 10_000, run_job=None, log=print, lease_s: float = LEASE_S,
                    heartbeat_s: float = HEARTBEAT_S) -> str | None:
    """恢复一个孤立的 EVALUATING 提交（租约到期且文件锁可取得；所属榜单版本被冻结的跳过）；→ submission_id 或 None。
    停在 SCORING 的孤立提交只记日志（评测行可能已写入，需人工核对）。"""
    if run_job is None:
        from awr.bench.runner import run_job
    data_root = Path(data_root)
    st = SubmissionStore(data_root / "submissions")
    now = time.time_ns()
    for x in st.all():
        if x["state"] not in ("EVALUATING", "SCORING") or not Lease.expired(data_root, x, now_ns=now, lease_s=lease_s):
            continue
        if is_frozen(data_root / "boards" / x["_meta"]["board_version"]):
            continue
        sid = x["submission_id"]
        lease = Lease(data_root, sid, lease_s=lease_s, heartbeat_s=heartbeat_s)
        if not lease.acquire():                              # 原进程仍持有文件锁：未孤立
            continue
        try:
            x = st.get(sid)
            if x["state"] == "SCORING":
                log(f"warning: {sid} is orphaned in SCORING (rows may already be appended); resolve by hand")
                continue
            if x["state"] != "EVALUATING":
                continue
            _resume(data_root, st, x, docker=docker, n_boot=n_boot, run_job=run_job, log=log, worker=lease.worker)
            return sid
        finally:
            lease.release()
    return None


def process_one(data_root: Path, *, docker: str = "docker", smoke_s: float = 20.0, n_boot: int = 10_000, run_job=None,
                trivy: TrivyScanner | None = None, log=print, registry: str = "invited", registry_wait_s: float = REGISTRY_WAIT_S,
                lease_s: float = LEASE_S, heartbeat_s: float = HEARTBEAT_S) -> str | None:
    """先恢复一个孤立的 EVALUATING 提交（`recover_orphans`），否则处理一个 RECEIVED 提交；→ submission_id 或 None（队列为空或全部在
    等待镜像接入）。

    `trivy` 为 None 时不做漏洞扫描（测试替身与开发自检；命令行入口缺省要求扫描器，见 `main`）；扫描器不可用时抛
    ScannerUnavailable，提交保持 RECEIVED。`registry`、`registry_wait_s`：镜像接入门（`registry_gate`）；`lease_s`、`heartbeat_s`：
    处理租约（模块文档"租约与孤立提交的恢复"）。"""
    if run_job is None:
        from awr.bench.runner import run_job

    data_root = Path(data_root)
    got = recover_orphans(data_root, docker=docker, n_boot=n_boot, run_job=run_job, log=log, lease_s=lease_s, heartbeat_s=heartbeat_s)
    if got is not None:
        return got
    st = SubmissionStore(data_root / "submissions")
    d = lease = None
    for x in st.all():                                       # 平台复现审计冻结的榜单版本：其提交留在 RECEIVED，解冻后继续
        if x["state"] != "RECEIVED" or is_frozen(data_root / "boards" / x["_meta"]["board_version"]):
            continue
        gate, why = registry_gate(data_root, x, now_ns=st.clock(), mode=registry, wait_s=registry_wait_s)
        if gate == "reject":                                 # 接入结论为提交方原因或等待超时：不领取，直接拒绝
            st.transition(x["submission_id"], "REJECTED", reason=why, expect="RECEIVED")
            log(f"{x['submission_id']}: REJECTED registry {why['detail']}")
            return x["submission_id"]
        if gate == "ready":
            lease = Lease(data_root, x["submission_id"], lease_s=lease_s, heartbeat_s=heartbeat_s)
            if not lease.acquire():                          # 已由其他进程领取
                continue
            if st.get(x["submission_id"])["state"] != "RECEIVED":
                lease.release()
                continue
            d = x
            break
    if d is None:
        return None
    with lease:
        return _process(data_root, st, d, docker=docker, smoke_s=smoke_s, n_boot=n_boot, run_job=run_job, trivy=trivy, log=log)


def _process(data_root: Path, st: SubmissionStore, d: dict, *, docker: str, smoke_s: float, n_boot: int, run_job, trivy: TrivyScanner | None,
             log) -> str:
    """一个已领取的 RECEIVED 提交的全部阶段（模块文档第 0 至 4 步）。"""
    sid = d["submission_id"]
    db = trivy.ready() if trivy is not None else None       # 库缺失或过期：抛 ScannerUnavailable，不领取
    if db is not None and db["warning"]:
        log(f"warning: trivy db is {db['age_days']:.1f} days old (> {trivy.warn_days:g}); refresh with tools/deploy/dmax/images/trivy/fetch.sh --db-only")
    bdir = data_root / "boards" / d["_meta"]["board_version"]
    cfg = load_config(bdir)
    v1 = is_v1(cfg)
    policy = {"kind": "image", "image_digest": d["image_digest"], "entry": d["entry"]}
    if v1:                                                   # 排行榜 v1：版本衔接、运行参数与排名规格准入（模块文档"排行榜 v1"）
        got = _requeue_v1(data_root, st, d, log)
        if got is None:
            return sid
        bdir, cfg = got
        d = st.get(sid)
        bad_cfg = v1_problems(cfg, d["track"], d.get("lanes"))
        try:
            _evalstore(cfg)                                  # EvalStore 缺失或与封板记录不符：失败关闭，不领取评测
        except BoardConfigError as e:
            bad_cfg.append(str(e))
        if bad_cfg:
            st.transition(sid, "FAILED", expect="RECEIVED", reason={"code": 320, "name": "INTERNAL",
                                                                    "detail": {"why": "board_config", "problems": bad_cfg[:5]}})
            log(f"{sid}: FAILED board {bdir.name} config: {bad_cfg[:5]}")
            return sid
        v1_runs = _v1_runs(cfg, d, policy)
        bad = reason(check_ranked_eval(v1_runs[0]["rs"], physics=cfg.get("physics"), policy={"kind": "image"}))
        rs = cfg["smoke"]["run_spec"]
        smoke_roots = {"worlds_root": Path(cfg["smoke"].get("worlds_root") or cfg["worlds_root"]),
                       "events_root": Path(cfg["smoke"].get("events_root") or cfg["events_root"])}
    else:
        bad = None if (cfg.get("track") or {}).get("dev") else reason(check_submission_eval(cfg))
        rs = cfg["run_spec"]
        smoke_roots = {}
    if bad:                                                  # 正式榜：提交恒为排名运行，按榜单轮次配置求组合规则（633、682）
        st.transition(sid, "REJECTED", reason=bad, expect="RECEIVED")
        log(f"{sid}: REJECTED config {bad['code']} {bad['detail']['why']}")
        return sid
    common = _common(cfg, docker)
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
                        physics="kinematic", duration_s=smoke_s, **{**common, **smoke_roots})
        ag = r["agents"]
        fail = {u: s for u, s in ag.items() if s.get("acts", 0) == 0 or s.get("protocol_errors") or s.get("exited") not in (None, "bye")
                or s.get("exit_code") not in (0, None)}
    except WallLimitExceeded as e:                           # 冒烟超墙钟上限：终止并拒绝（693）
        st.transition(sid, "REJECTED", reason=e.reason(stage="smoke"))
        log(f"{sid}: REJECTED smoke wall limit {e.limit_s:g} s")
        return sid
    except Exception as e:                                   # 握手超时、容器启动失败等均归为冒烟失败
        fail = {"error": getattr(e, "code", None) or type(e).__name__}
    finally:
        clean_run_sockets(work / "smoke")
    if fail:
        st.transition(sid, "REJECTED", reason=_reason("smoke", agents=sorted(fail)))
        log(f"{sid}: REJECTED smoke {fail}")
        return sid
    if not _alive(st, sid):
        return sid
    st.transition(sid, "EVALUATING", expect="VALIDATING")
    if v1:
        got = _evaluate_v1(data_root, st, d, cfg, v1_runs, policy=policy, common=common, run_job=run_job, log=log)
        if got is not None:
            _score_v1(st, d, cfg, bdir, *got, n_boot=n_boot, log=log)
        return sid
    got = _evaluate_legacy(data_root, st, d, cfg, rs, policy=policy, common=common, run_job=run_job, log=log, experiment_id=f"board-{bdir.name}")
    if got is not None:
        _score_legacy(st, d, cfg, bdir, *got, n_boot=n_boot, log=log)
    return sid


# ------------------------------------------------------------------------------------------------------------ 旧版榜单配置（非 v1）
def _legacy_k_eval(cfg: dict) -> bytes | None:
    """旧版榜单配置的评测密钥：配置给出 `evalstore`（含 `k_eval` 的服务端文件）时读取，否则 None。"""
    p = cfg.get("evalstore")
    if not p:
        return None
    try:
        return bytes.fromhex(json.loads(Path(str(p)).read_text(encoding="utf-8"))["k_eval"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _legacy_runs(cfg: dict, d: dict) -> list[tuple[int, int, str]]:
    """旧版配置的提交 Run：→ [(种子序号 k, 种子, run_id)]。"""
    return [(k, int(seed), f"{d['submission_id']}-r{k}") for k, seed in enumerate(cfg["seeds"])]


def _legacy_row(cfg: dict, d: dict, k: int, evaluation: dict, **extra) -> dict:
    sid, team = d["submission_id"], d["_meta"]["team"]
    return {"entry_id": f"{team}/{sid}", "team": team, "display_name": sid, "baseline": False, "track": cfg["track_id"], "lane": cfg["lane"],
            "event_type": cfg.get("event_type", cfg["lane"]), "event": cfg["run_spec"]["event"]["id"], "seed": k, "submission_id": sid,
            "evaluation": evaluation, **extra}


def _wall_row(cfg: dict, d: dict, k: int, termination: dict) -> dict:
    return _legacy_row(cfg, d, k, {"components": dict.fromkeys(("ec", "eu", "ao", "ce")), "ranked": True, "ranked_valid": False,
                                   "invalid_reasons": ["wall_limit"]}, termination=termination)


def _evaluate_legacy(data_root: Path, st: SubmissionStore, d: dict, cfg: dict, rs: dict, *, policy: dict, common: dict, run_job, log,
                     experiment_id: str, only: set[int] | None = None) -> tuple[dict[int, dict], int] | None:
    """EVALUATING（旧版配置）：逐种子正式运行；`only` 给出时只运行所列种子序号（恢复）。→ ({k: 评测行}, 平台原因重跑次数)，
    平台原因两次失败时 FAILED 并返回 None。"""
    sid = d["submission_id"]
    event = rs["event"]["id"]
    k_eval = _legacy_k_eval(cfg)
    rows: dict[int, dict] = {}
    reruns = 0
    for k, seed, run_id in _legacy_runs(cfg, d):
        if only is not None and k not in only:
            continue
        run_dir = data_root / "dataset" / event / "runs" / run_id
        sc = seed_commit(k_eval, run_id, seed) if k_eval is not None else None
        wall = None
        t_q = time.time_ns()
        for attempt in (0, 1):
            t0 = time.time_ns()
            try:
                guarded_run(run_job, st, sid, rs, limit_s=wall_limit_s(run_duration_s(rs, common["events_root"])), policy=policy, seed=int(seed),
                            run_id=run_id, run_dir=run_dir, physics=cfg.get("physics"), gz=cfg.get("gz") or None,
                            **({"seed_commit": sc} if sc else {}), **common)
                break
            except WallLimitExceeded as e:                   # 超墙钟上限：终止（693），记为无效行，不重跑
                wall = e
                log(f"{sid}: run r{k} exceeded the wall limit {e.limit_s:g} s")
                break
            except Exception:                               # 平台原因：重跑一次
                log(_scrub(f"{sid}: run r{k} attempt {attempt} failed\n{traceback.format_exc()[-1500:]}", seed))
                if attempt == 1:
                    clean_run_sockets(run_dir)
                    st.transition(sid, "FAILED", reason={"code": 320, "name": "INTERNAL", "detail": {"why": "platform", "run": k}})
                    return None
                reruns += 1
        clean_run_sockets(run_dir)
        if wall is not None:
            rows[k] = _wall_row(cfg, d, k, wall.reason())
            continue
        _write_submission_manifest(run_dir, rs, policy, run_id=run_id, seed=seed, seed_commit=sc, worlds_root=common["worlds_root"],
                                   events_root=common["events_root"], physics=cfg.get("physics"), gz=cfg.get("gz") or None,
                                   experiment_id=experiment_id,
                                   split=str(cfg.get("split") or "dev"), attempt=attempt + 1,
                                   timing={"queued_unix_ns": t_q, "started_unix_ns": t0, "ended_unix_ns": time.time_ns()}, log=log)
        ev = json.loads((run_dir / "evaluation.json").read_text(encoding="utf-8"))
        rows[k] = _legacy_row(cfg, d, k, slim_evaluation(ev))
    return rows, reruns


def _score_legacy(st: SubmissionStore, d: dict, cfg: dict, bdir: Path, by_k: dict[int, dict], reruns: int, *, n_boot: int, log) -> None:
    """SCORING（旧版配置）→ DONE，上榜时重算榜单并回填名次与层级；排名运行随后执行 `ranked_eval` 保留（开发榜除外）。"""
    from awr.bench.eval.aggregate import lane_metrics

    sid, team = d["submission_id"], d["_meta"]["team"]
    rows = [by_k[k] for k in sorted(by_k)]
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
    if (cfg.get("track") or {}).get("dev") or not cfg["run_spec"].get("ranked"):
        return
    data_root = Path(st.root).parent
    event = cfg["run_spec"]["event"]["id"]
    _retain(data_root, st.get(sid), [(data_root / "dataset" / event / "runs" / f"{sid}-r{r['seed']}", event, r["seed"], 0) for r in rows], log=log)


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
