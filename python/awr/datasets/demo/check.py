"""演示前检查（M16-FR-004；M16 §6.16；13 §4.4.1 的 7 项 + 服务器侧 2 项；错误码 DEMO-E001–E006）。

| 项 | 内容 | 不通过 |
|---|---|---|
| svc | 服务端：api 与 zenoh 端口未被占用、没有 demo 运行占用 | fail，DEMO-E004，退出码 11 |
| worlds | 六城有效：`validate_world(deep)` 零错误 | fail，DEMO-E001，退出码 6 |
| scenarios | 剧本与 catalog 静态有效（V-SC-01、V-SC-13） | fail，DEMO-E002/E003，退出码 2 |
| access | 访问路径：打印 SSH 转发命令（远端浏览器无法由服务器自检） | manual |
| tier | 浏览器档位：启动后在 HUD 核对 `__perf.meta.tier` 与 `deviceClass` | manual |
| load | 1 分钟 loadavg < 2 且无测试、构建、worldpkg 进程 | warn，DEMO-E005（`--force` 可继续） |
| warmup | 着色器预热：演示机首次打开后刷新一次 | manual |
| ext | 扩展段开关：最近一次 G3/G4 报告中 D1-AC-16、18、22 的状态 | warn，DEMO-E006（提示卡中对应子段标"跳过"） |
| shm、disk | `/dev/shm` ≥ 1 GiB（DOC-10）、磁盘余量（DOC-12），复用 `awr doctor` 的检查 | fail，退出码 9、7 |

`exit_code()` 取最严重的不通过项映射到 19 §16.2；warn 与 manual 不影响退出码。
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

__all__ = ["EXT_AC", "ROOT_HINT", "CheckItem", "exit_code", "ext_status", "run_checks"]

ROOT_HINT = Path(__file__).resolve().parents[4]

Status = Literal["pass", "fail", "warn", "manual"]
EXT_AC = {"D1-AC-16": "D6a S3 多机协同", "D1-AC-18": "D6c 回放", "D1-AC-22": "D6b Mock 重建"}
BUSY_PATTERNS = ("pytest", "vitest", "vite build", "worldpkg", "playwright", "tsc -p", "make build", "make test", "make ci")


@dataclass
class CheckItem:
    id: str
    title_zh: str
    status: Status
    detail: str
    fix: str | None = None
    code: str | None = None          # DEMO-E00x
    exit: int = 0                    # 19 §16.2（只在 fail 时有意义）

    def to_json(self) -> dict:
        return asdict(self)


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _busy_processes(me: int) -> list[str]:
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if any(p in cmd for p in BUSY_PATTERNS) and "awr.datasets.demo" not in cmd:
            out.append(f"{d.name}:{cmd[:80]}")
    return sorted(out)[:8]


def _check_service(offset: int) -> CheckItem:
    ports = [8000 + 10 * offset, 7447 + 10 * offset]
    busy = [p for p in ports if not _port_free(p)]
    if busy:
        return CheckItem("svc", "服务端端口与运行", "fail", f"端口被占用：{busy}", "make stop；或设置 AWR_PORT_OFFSET=<k> 后重试",
                         "DEMO-E004", 11)
    return CheckItem("svc", "服务端端口与运行", "pass", f"{ports} 空闲（启动后以 /api/sys/procs 复核）")


def _check_worlds(worlds_dir: Path, deep: bool) -> CheckItem:
    from awr.world.package.validate import validate_world

    from ..urbanscene3d.cities import CITY_IDS

    bad, t0 = [], time.monotonic()
    for wid in CITY_IDS:
        p = worlds_dir / wid
        if not (p / "world.json").exists():
            bad.append(f"{wid}: missing")
            continue
        try:
            r = validate_world(p, deep=deep)
        except Exception as e:
            bad.append(f"{wid}: {e}")
            continue
        if not r.ok:
            bad.append(f"{wid}: {len(r.errors)} errors ({r.errors[0]['rule']})")
    dt = time.monotonic() - t0
    if bad:
        return CheckItem("worlds", "六城 World Package 有效", "fail", "; ".join(bad), "make worlds", "DEMO-E001", 6)
    return CheckItem("worlds", "六城 World Package 有效", "pass", f"六城 validate{' --deep' if deep else ''} 零错误（{dt:.1f} s）")


def _check_scenarios(repo: Path) -> CheckItem:
    from ..scenarios.catalog import catalog_errors, load_catalog, scenario_files, schema_errors

    root = Path(os.environ.get("AWR_SCENARIOS_DIR") or repo / "scenarios")
    try:
        cat = load_catalog(root)
    except (OSError, json.JSONDecodeError) as e:
        return CheckItem("scenarios", "剧本与剧本清单", "fail", f"catalog.json 不可读：{e}",
                         "python -m awr.datasets.scenarios generate", "DEMO-E002", 2)
    cerr = catalog_errors(cat, root)
    if cerr:
        return CheckItem("scenarios", "剧本与剧本清单", "fail", cerr[0], "python -m awr.datasets.scenarios check", "DEMO-E002", 2)
    serr = []
    for sid, p in scenario_files(root).items():
        e = schema_errors(json.loads(p.read_text(encoding="utf-8")), "scenario/scenario.schema.json")
        if e:
            serr.append(f"{sid}: V-SC-01 {e[0]}")
    if serr:
        return CheckItem("scenarios", "剧本与剧本清单", "fail", serr[0], "python -m awr.datasets.scenarios check", "DEMO-E003", 2)
    return CheckItem("scenarios", "剧本与剧本清单", "pass", f"{len(scenario_files(root))} 个剧本与 catalog 有效")


def _check_load(max_load: float) -> CheckItem:
    la = os.getloadavg()[0]
    busy = _busy_processes(os.getpid())
    if la >= max_load or busy:
        why = f"1 分钟 loadavg {la:.2f}" + (f"；进程：{', '.join(busy)}" if busy else "")
        return CheckItem("load", "负载与后台进程", "warn", why, "等待测试、构建与 worldpkg 结束；FORCE=1 可继续", "DEMO-E005")
    return CheckItem("load", "负载与后台进程", "pass", f"1 分钟 loadavg {la:.2f}，无测试与构建进程")


def _latest_gate_report(runs_dir: Path) -> tuple[Path | None, dict | None]:
    best: tuple[float, Path, dict] | None = None
    base = runs_dir / "perf"
    if not base.is_dir():
        return None, None
    for p in base.glob("*/report.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("gate") not in ("G3", "G4"):
            continue
        m = p.stat().st_mtime
        if best is None or m > best[0]:
            best = (m, p, d)
    return (best[1], best[2]) if best else (None, None)


def ext_status(runs_dir: Path) -> dict[str, str]:
    """{D1-AC-16/18/22: PASS | FAIL | ... | NONE}：取最近一次 G3/G4 报告中含该编号的用例的最差状态。"""
    _, rep = _latest_gate_report(runs_dir)
    out = dict.fromkeys(EXT_AC, "NONE")
    if not rep:
        return out
    order = {"FAIL": 5, "ENV_UNMET": 4, "WARN": 2, "WAIVED": 3, "NA": 1, "PASS": 0}
    for c in rep.get("cases") or []:
        for ac in c.get("ac_ids") or []:
            if ac in out:
                cur = out[ac]
                if cur == "NONE" or order.get(c.get("status"), 5) > order.get(cur, 0):
                    out[ac] = str(c.get("status"))
    return out


def _check_ext(runs_dir: Path) -> CheckItem:
    st = ext_status(runs_dir)
    skip = [f"{EXT_AC[k]}（{k} {v}）" for k, v in st.items() if v not in ("PASS", "WARN")]
    if skip:
        return CheckItem("ext", "扩展段开关", "warn", "跳过：" + "；".join(skip), "make perf-milestone MS=6 后重试", "DEMO-E006")
    return CheckItem("ext", "扩展段开关", "pass", "D1-AC-16、18、22 均有通过记录")


def _doctor_items() -> list[CheckItem]:
    """DOC-10（/dev/shm，退出码 9）与 DOC-12（磁盘，退出码 7）：复用 `awr doctor` 的检查，不另行实现。"""
    try:
        from awr.runtime.cli import _doctor_checks
        from awr.runtime.config import load_runtime_config
    except Exception as e:
        return [CheckItem("shm", "/dev/shm 与磁盘", "manual", f"awr doctor 不可用：{e}", "awr doctor --quick")]
    try:
        cfg, err = load_runtime_config(profile=os.environ.get("AWR_PROFILE") or "demo"), None
    except Exception as e:
        cfg, err = None, e
    try:
        checks = _doctor_checks(cfg, err, quick=True, deep=False)
    except Exception as e:
        return [CheckItem("shm", "/dev/shm 与磁盘", "manual", f"awr doctor 失败：{e}", "awr doctor --quick")]
    out = []
    for c in checks:
        if c.id not in ("DOC-10", "DOC-12"):
            continue
        st: Status = "fail" if c.level == "ERROR" else ("warn" if c.level == "WARN" else "pass")
        out.append(CheckItem("shm" if c.id == "DOC-10" else "disk", c.title, st, f"{c.id} {c.detail}", c.fix or None,
                             None, int(c.code) or (9 if c.id == "DOC-10" else 7)))
    return out


def run_checks(repo: Path, worlds_dir: Path, *, force: bool = False, deep: bool = True, max_load: float = 2.0,
               runs_dir: Path | None = None, offset: int | None = None, host: str = "<host>",
               with_doctor: bool = True) -> list[CheckItem]:
    """13 §4.4.1 的 7 项 + 服务器侧 2 项（顺序即输出顺序）。`force` 只把负载告警标为已知，不影响不通过项。"""
    off = offset if offset is not None else int(os.environ.get("AWR_PORT_OFFSET", "0") or 0)
    runs_dir = runs_dir or Path(os.environ.get("AWR_RUNS_DIR") or repo / "runs")
    port = 8000 + 10 * off
    items = [
        _check_service(off),
        _check_worlds(worlds_dir, deep),
        _check_scenarios(repo),
        CheckItem("access", "访问路径", "manual",
                  f"ssh -N -L {port}:127.0.0.1:{port} <user>@{host}  然后打开 http://localhost:{port}/world/shenzhen",
                  "远端浏览器无法由服务器自检，需人工确认"),
        CheckItem("tier", "浏览器档位", "manual", "启动后在性能 HUD 核对 __perf.meta.tier 与 deviceClass（真 GPU 为 B，本机为 S）"),
        _check_load(max_load),
        CheckItem("warmup", "着色器预热", "manual", "演示机首次打开 /world/shenzhen 后刷新一次，避免首次操作编译着色器"),
        _check_ext(runs_dir),
    ]
    if with_doctor:
        items += _doctor_items()
    if force:
        for it in items:
            if it.code == "DEMO-E005":
                it.detail += "（FORCE=1，已知并继续）"
    return items


EXIT_ORDER = (11, 6, 2, 9, 7, 8, 4, 5, 14, 1)


def exit_code(items: list[CheckItem]) -> int:
    fails = [it.exit or 1 for it in items if it.status == "fail"]
    if not fails:
        return 0
    for code in EXIT_ORDER:
        if code in fails:
            return code
    return fails[0]
