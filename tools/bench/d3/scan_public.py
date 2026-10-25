#!/usr/bin/env python3
"""tools/bench/d3/scan_public.py：公开构建产物与公开静态目录扫描（D3-AC-21、D3-AC-01；扩展点 15、16）。

依据：AWR-05 §7.9 实现约束 1–3、§12.3 第 15 处、ADR-129；AWR-14 §21.2.3 规则 1、§21.14.2；AWR-17 §18.8.5 规则 3（M27-FR-136）。

子命令：
  build <dist> [--profile lab|demo]
      公开构建产物（`/lab/` 独立静态构建或 D2 演示构建）逐文件按字节计数禁用字符串，任一 > 0 不通过：
      - 两种构建：D3-AC-21 原文 `/leaderboard`、`/submissions`、`/board`、`/registry`、`/api/bench/v1/`，以及 bench 客户端拼接
        路径所用的基址 `/api/bench`（只扫带尾斜杠的完整前缀会漏检 `BENCH + '/runs'` 式拼接，G01 实测）；
      - `lab`（缺省）另加 ADR-129 v1.2 的 `/api/bench/v1/runs`、`/api/bench/v1/events`、`/api/bench/v1/session`，以及
        AWR-14 §21.14.2 规则 1 的"只发起 /lab/ 与 /lab-static/ 的 GET、0 个 WS"在产物上的旁证：`/api/`、`new WebSocket`、
        `EventSource(` 为 0。说明：ADR-129 的 `/events`、`/session` 指 bench 接口路径；裸字符串 `/session` 会命中 Tailwind 的
        容器查询类名（`@container/sessionbar`），不作判据。
      产物目录同时按 `static` 的规则统计 test 集文件与真值文件（构建目录本身也是公开静态目录）。
  static <dir> [--test-ids FILE] [--max-file-mb 64] [--budget-gb 3]
      公开静态目录（`/lab-static/` 与 `/lab/<version>/`）：test 集文件数（D3-AC-21："公开静态目录中 test 集文件数为 0"）、真值
      文件数（M27-FR-136）、> 64 MB 的单文件数与总字节数（Q-D3-02，同一 inode 只计一次，对应 `rsync --link-dest` 去重）。
      test 集文件的判定：(1) 路径中有名为 `test` 或 `hidden` 的段（含文件主名）；(2) JSON 文件中任一层的 `"split": "test"`
      （run_manifest.v1 等），该文件所在目录整棵子树都计为 test 集文件；(3) `--test-ids` 文件列出的事件 id 或运行 id 出现在
      路径段中。真值文件：含 MCAP 真值通道前缀 `/eval/hidden/` 的文件，或路径中有 `truth` 段的文件。
  parity <dir_a> <dir_b>
      两棵产物树逐字节对拍（只在一侧的文件、内容不同的文件）。
  d2-parity [--ref v0.2.2] [--work DIR]
      扩展点 15 的构建对拍（"开关缺省关闭时 D2 演示构建产物逐字节不变"，UX-AC-082）：`git archive <ref>` 与当前工作树的
      `apps/web`、`docs/media`、`packages/contracts` 分别放到 `<work>/ref` 与 `<work>/cur`（两棵树路径结构相同：rolldown
      的模块顺序与绝对路径的相对次序有关，两侧须同构），第三方包逐项链接本检出的 node_modules（两侧共用），工作区包
      `@awr/*` 指向各自副本（契约改动因此计入对拍），以相同的构建信息（ref 的提交与提交时刻、channel = demo）各做一次
      `VITE_AWR_DEMO=public` 构建，再逐字节对拍；`cur` 的产物另按 `build --profile demo` 扫描。

用法：
    python tools/bench/d3/scan_public.py build .cache/deploy/emax/lab-dist --profile lab
    python tools/bench/d3/scan_public.py static /srv/drone4d/lab-static --test-ids configs/bench/test_ids.txt
    python tools/bench/d3/scan_public.py d2-parity --ref v0.2.2
每个子命令可加 `--json <path>` 写出报告（`awr.d3.scan_public.report.v1`）；退出码 0 通过、1 有发现、2 参数错误或产物缺失。
git 目录不在工作树内时（本仓库的外置 GIT_DIR），d2-parity 读取环境变量 GIT_DIR。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = "awr.d3.scan_public.report.v1"

#: D3-AC-21 原文：公开构建产物中出现次数须为 0
AC21_STRINGS: tuple[str, ...] = ("/leaderboard", "/submissions", "/board", "/registry", "/api/bench/v1/")
#: bench 客户端的基址（net/bench.ts `BENCH`）：拼接路径时不带尾斜杠
BENCH_BASE = "/api/bench"
#: ADR-129（v1.2）对 /lab/ 构建另检查的 bench 接口路径
ADR129_LAB_STRINGS: tuple[str, ...] = ("/api/bench/v1/runs", "/api/bench/v1/events", "/api/bench/v1/session")
#: AWR-14 §21.14.2 规则 1 的产物旁证：/lab/ 构建不含任何接口路径与 WS/SSE 客户端
LAB_NO_REQUEST_STRINGS: tuple[str, ...] = ("/api/", "new WebSocket", "EventSource(")

PROFILES: dict[str, tuple[str, ...]] = {
    "demo": (*AC21_STRINGS, BENCH_BASE),
    "lab": (*AC21_STRINGS, BENCH_BASE, *ADR129_LAB_STRINGS, *LAB_NO_REQUEST_STRINGS),
}

#: 路径段（含文件主名）等于这些值即为 test 集文件
TEST_SEGMENTS = frozenset({"test", "hidden"})
#: MCAP 真值通道前缀（M27 §7.5.1 `/eval/hidden/**`）
TRUTH_MARKER = b"/eval/hidden/"
TRUTH_SEGMENTS = frozenset({"truth"})
MAX_FILE_MB = 64
BUDGET_GB = 3.0
#: 只读入这么大的 JSON 判断 split（更大的 JSON 不是清单）
JSON_SPLIT_MAX_BYTES = 8 << 20
CONTEXT = 48

#: 当前工作树复制到对拍目录时跳过的目录与文件
COPY_IGNORE = ("node_modules", "dist", "tsconfig.tsbuildinfo", "test-results", "playwright-report", ".vite", "coverage")
#: 两侧构建都清除的开关（对拍的是"开关缺省关闭"）
PARITY_CLEAR_ENV = ("VITE_AWR_TEST_SWITCHES", "VITE_AWR_LAB_ONLY", "VITE_AWR_LAB_PUBLIC", "VITE_AWR_EXT_ROUTES", "AWR_BUILD_CHANNEL",
                    "AWR_BUILD_SHA", "AWR_BUILD_DIRTY", "AWR_BUILD_TIME", "AWR_RELEASE", "SOURCE_DATE_EPOCH")


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def walk(root: Path) -> Iterator[Path]:
    """root 下的普通文件（递归，按相对路径排序；不跟随符号链接目录）"""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_file():
                out.append(p)
    yield from sorted(out, key=lambda p: p.relative_to(root).as_posix())


def rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


# ---------------------------------------------------------------------------------------------------------------------
# build：禁用字符串


def count_hits(data: bytes, needles: Iterable[str], *, max_ctx: int = 5) -> dict[str, dict]:
    """每个禁用字符串的非重叠出现次数与前 max_ctx 处上下文"""
    out: dict[str, dict] = {}
    for n in needles:
        nb = n.encode("utf-8")
        i = data.find(nb)
        cnt = 0
        ctx: list[dict] = []
        while i >= 0:
            cnt += 1
            if len(ctx) < max_ctx:
                ctx.append({"offset": i, "context": data[max(0, i - CONTEXT): i + len(nb) + CONTEXT].decode("utf-8", "replace")})
            i = data.find(nb, i + len(nb))
        if cnt:
            out[n] = {"count": cnt, "hits": ctx}
    return out


def scan_build(dist: Path, profile: str = "lab", *, test_ids: frozenset[str] = frozenset()) -> dict:
    """公开构建产物：禁用字符串计数（PROFILES[profile]）与 test 集、真值文件统计"""
    needles = PROFILES[profile]
    counts = dict.fromkeys(needles, 0)
    findings: list[dict] = []
    files = list(walk(dist))
    for p in files:
        data = p.read_bytes()
        for n, h in count_hits(data, needles).items():
            counts[n] += h["count"]
            findings.append({"file": rel(p, dist), "string": n, **h})
    static = scan_static(dist, test_ids=test_ids)
    ok = all(v == 0 for v in counts.values()) and static["ok"]
    info_p = dist / "build-info.json"
    build_info = None
    if info_p.is_file():
        try:
            build_info = json.loads(info_p.read_text(encoding="utf-8"))
        except ValueError:
            build_info = None
    return {"kind": "build", "profile": profile, "dist": str(dist), "files": len(files), "strings": counts, "findings": findings,
            "test_files": static["test_files"], "truth_files": static["truth_files"], "build_info": build_info, "ok": ok}


# ---------------------------------------------------------------------------------------------------------------------
# static：test 集与真值文件、单文件上限与总量


def _has_test_split(v: object) -> bool:
    if isinstance(v, dict):
        if v.get("split") == "test":
            return True
        return any(_has_test_split(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_test_split(x) for x in v)
    return False


def _segments(relpath: str) -> list[str]:
    parts = relpath.split("/")
    stem = parts[-1].split(".", 1)[0]
    return [*parts[:-1], parts[-1], stem]


def load_test_ids(path: Path | None) -> frozenset[str]:
    """--test-ids：每行一个事件 id 或运行 id（# 起注释，空行忽略）"""
    if path is None:
        return frozenset()
    ids = (ln.split("#", 1)[0].strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return frozenset(i for i in ids if i)


def scan_static(root: Path, *, test_ids: frozenset[str] = frozenset(), max_file_mb: float = MAX_FILE_MB,
                budget_gb: float = BUDGET_GB) -> dict:
    files = list(walk(root))
    test_dirs: list[str] = []
    test_files: list[dict] = []
    truth_files: list[dict] = []
    oversize: list[dict] = []
    seen_inodes: set[tuple[int, int]] = set()
    total = 0
    limit = int(max_file_mb * 1024 * 1024)
    # (2) JSON 清单中的 split = test：整棵目录子树
    for p in files:
        if p.suffix.lower() != ".json" or p.stat().st_size > JSON_SPLIT_MAX_BYTES:
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if _has_test_split(doc):
            d = rel(p.parent, root) if p.parent != root else ""
            test_dirs.append(d)
    for p in files:
        r = rel(p, root)
        st = p.stat()
        key = (st.st_dev, st.st_ino)
        if key not in seen_inodes:
            seen_inodes.add(key)
            total += st.st_size
        if st.st_size > limit:
            oversize.append({"file": r, "bytes": st.st_size})
        segs = _segments(r)
        why = None
        if TEST_SEGMENTS.intersection(segs):
            why = "segment"
        elif test_ids and test_ids.intersection(segs):
            why = "test-id"
        else:
            for d in test_dirs:
                if d == "" or r == d or r.startswith(d + "/"):
                    why = f"split:{d or '.'}"
                    break
        if why:
            test_files.append({"file": r, "reason": why})
        if TRUTH_SEGMENTS.intersection(segs):
            truth_files.append({"file": r, "reason": "segment"})
        else:
            with open(p, "rb") as f:
                if _contains(f, TRUTH_MARKER):
                    truth_files.append({"file": r, "reason": "eval-hidden-channel"})
    budget = int(budget_gb * 1024 ** 3)
    ok = not test_files and not truth_files and not oversize and total <= budget
    return {"kind": "static", "root": str(root), "files": len(files), "bytes_total": total, "budget_bytes": budget,
            "max_file_bytes": limit, "test_files": len(test_files), "truth_files": len(truth_files), "oversize_files": len(oversize),
            "test_detail": test_files[:200], "truth_detail": truth_files[:200], "oversize_detail": oversize[:200], "ok": ok}


def _contains(f: io.BufferedReader, needle: bytes, chunk: int = 1 << 20) -> bool:
    tail = b""
    while True:
        b = f.read(chunk)
        if not b:
            return False
        if needle in tail + b:
            return True
        tail = b[-(len(needle) - 1):]


# ---------------------------------------------------------------------------------------------------------------------
# parity：两棵产物树逐字节对拍


def tree_digest(root: Path) -> dict[str, str]:
    return {rel(p, root): sha256_bytes(p.read_bytes()) for p in walk(root)}


def parity(a: Path, b: Path) -> dict:
    da, db = tree_digest(a), tree_digest(b)
    only_a = sorted(set(da) - set(db))
    only_b = sorted(set(db) - set(da))
    differ = sorted(k for k in set(da) & set(db) if da[k] != db[k])
    manifest = "\n".join(f"{v}  {k}" for k, v in sorted(da.items())).encode()
    return {"kind": "parity", "a": str(a), "b": str(b), "files_a": len(da), "files_b": len(db), "only_a": only_a, "only_b": only_b,
            "differ": differ, "tree_sha256_a": sha256_bytes(manifest),
            "tree_sha256_b": sha256_bytes("\n".join(f"{v}  {k}" for k, v in sorted(db.items())).encode()),
            "ok": not only_a and not only_b and not differ}


# ---------------------------------------------------------------------------------------------------------------------
# d2-parity：D2 标签构建与当前工作树（开关缺省关闭）的演示构建对拍


def git(args: list[str], *, cwd: Path = ROOT) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def resolve_ref(ref: str) -> tuple[str, str]:
    """(10 位提交, 提交时刻 UTC YYYY-MM-DDTHH:MM:SSZ)"""
    sha = git(["rev-parse", "--short=10", f"{ref}^{{commit}}"]).decode().strip()
    iso = git(["log", "-1", "--format=%ct", f"{ref}^{{commit}}"]).decode().strip()
    when = datetime.datetime.fromtimestamp(int(iso), datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return sha, when


# 两侧导出的子树：@awr/contracts 是 npm 工作区包（node_modules/@awr/contracts -> ../../packages/contracts），两侧都须导出自己的
# packages/contracts，否则两侧都解析到本检出的契约，契约改动对演示构建的影响测不出来（G05 发现 5）
PARITY_PATHS = ("apps/web", "docs/media", "packages/contracts")


def export_ref(ref: str, dst: Path, paths: tuple[str, ...] = PARITY_PATHS) -> None:
    data = git(["archive", "--format=tar", f"{ref}^{{commit}}", *paths])
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(dst, filter="data")


def export_worktree(dst: Path, paths: tuple[str, ...] = PARITY_PATHS) -> None:
    for p in paths:
        shutil.copytree(ROOT / p, dst / p, ignore=shutil.ignore_patterns(*COPY_IGNORE), symlinks=True)


def link_node_modules(tree: Path, root: Path = ROOT) -> None:
    """副本的 node_modules：第三方包逐项链接本检出（两侧共用同一份依赖），工作区包 ``@awr/*`` 保留相对链接（如
    ``../../packages/contracts``），从而指向副本自己导出的子树（同 mk/d3.mk 的 GATE_REF 导出）。"""
    nm = tree / "node_modules"
    nm.mkdir()
    for e in sorted((root / "node_modules").iterdir()):
        if e.name in (".vite-temp", "@awr"):
            continue
        (nm / e.name).symlink_to(e, target_is_directory=e.is_dir())
    ws = root / "node_modules" / "@awr"
    if ws.is_dir():
        (nm / "@awr").mkdir()
        for e in sorted(ws.iterdir()):
            (nm / "@awr" / e.name).symlink_to(os.readlink(e) if e.is_symlink() else e, target_is_directory=True)
    web_nm = root / "apps" / "web" / "node_modules"
    if web_nm.is_dir():
        (tree / "apps" / "web" / "node_modules").symlink_to(web_nm, target_is_directory=True)


def parity_env(sha: str, when: str, base: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in (base if base is not None else os.environ).items() if k not in PARITY_CLEAR_ENV}
    env.update({"VITE_AWR_DEMO": "public", "AWR_BUILD_CHANNEL": "demo", "AWR_BUILD_SHA": sha, "AWR_BUILD_DIRTY": "false",
                "AWR_BUILD_TIME": when, "AWR_RELEASE": f"local-{sha}"})
    return env


def vite_build(tree: Path, out: Path, env: dict[str, str], *, timeout_s: float = 900) -> dict:
    vite = ROOT / "node_modules" / ".bin" / "vite"
    t0 = time.monotonic()
    r = subprocess.run([str(vite), "build", "--outDir", str(out), "--emptyOutDir", "--logLevel", "warn"], cwd=tree / "apps" / "web",
                       env=env, capture_output=True, text=True, timeout=timeout_s)
    return {"returncode": r.returncode, "seconds": round(time.monotonic() - t0, 1), "stderr_tail": r.stderr[-2000:]}


def d2_parity(ref: str, work: Path | None, *, keep: bool = False) -> dict:
    sha, when = resolve_ref(ref)
    own = work is None
    work = Path(tempfile.mkdtemp(prefix="awr-d2-parity-")) if work is None else work
    try:
        for side in ("ref", "cur", "out-ref", "out-cur"):
            shutil.rmtree(work / side, ignore_errors=True)
        (work / "ref").mkdir(parents=True)
        (work / "cur").mkdir(parents=True)
        export_ref(ref, work / "ref")
        export_worktree(work / "cur")
        for side in ("ref", "cur"):
            link_node_modules(work / side)
        env = parity_env(sha, when)
        builds = {side: vite_build(work / side, work / f"out-{side}", env) for side in ("ref", "cur")}
        rep: dict = {"kind": "d2-parity", "ref": ref, "ref_commit": sha, "build_time": when, "work": str(work), "builds": builds}
        if any(b["returncode"] != 0 for b in builds.values()):
            rep.update({"ok": False, "error": "build failed"})
            return rep
        rep["parity"] = parity(work / "out-ref", work / "out-cur")
        rep["scan"] = scan_build(work / "out-cur", "demo")
        rep["ok"] = rep["parity"]["ok"] and rep["scan"]["ok"]
        return rep
    finally:
        if own and not keep:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------------


def summary(rep: dict) -> list[str]:
    k = rep["kind"]
    st = "PASS" if rep["ok"] else "FAIL"
    if k == "build":
        bad = {s: c for s, c in rep["strings"].items() if c}
        return [f"{st} build[{rep['profile']}] {rep['dist']}: {rep['files']} files, strings {bad or 'all 0'}, "
                f"test files {rep['test_files']}, truth files {rep['truth_files']}",
                *(f"  {f['file']}: {f['string']} x{f['count']} … {f['hits'][0]['context']!r}" for f in rep["findings"][:20])]
    if k == "static":
        return [f"{st} static {rep['root']}: {rep['files']} files, {rep['bytes_total']} bytes (budget {rep['budget_bytes']}), "
                f"test files {rep['test_files']}, truth files {rep['truth_files']}, > {rep['max_file_bytes']} B: {rep['oversize_files']}",
                *(f"  test {x['file']} ({x['reason']})" for x in rep["test_detail"][:20]),
                *(f"  truth {x['file']} ({x['reason']})" for x in rep["truth_detail"][:20]),
                *(f"  oversize {x['file']} {x['bytes']}" for x in rep["oversize_detail"][:20])]
    if k == "parity":
        return [f"{st} parity {rep['a']} <> {rep['b']}: {rep['files_a']}/{rep['files_b']} files, only_a {len(rep['only_a'])}, "
                f"only_b {len(rep['only_b'])}, differ {len(rep['differ'])}",
                *(f"  only_a {x}" for x in rep["only_a"][:20]), *(f"  only_b {x}" for x in rep["only_b"][:20]),
                *(f"  differ {x}" for x in rep["differ"][:20])]
    lines = [f"{st} d2-parity ref {rep['ref']} ({rep['ref_commit']}, {rep['build_time']}), work {rep['work']}"]
    for side, b in rep["builds"].items():
        lines.append(f"  build {side}: rc {b['returncode']} in {b['seconds']} s" + (f"\n{b['stderr_tail']}" if b["returncode"] else ""))
    if "parity" in rep:
        lines += ["  " + x for x in summary(rep["parity"])]
        lines += ["  " + x for x in summary(rep["scan"])]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="公开构建产物的禁用字符串与 test 集文件")
    b.add_argument("dist", type=Path)
    b.add_argument("--profile", choices=sorted(PROFILES), default="lab")
    b.add_argument("--test-ids", type=Path)
    s = sub.add_parser("static", help="公开静态目录：test 集与真值文件、单文件上限与总量")
    s.add_argument("root", type=Path)
    s.add_argument("--test-ids", type=Path)
    s.add_argument("--max-file-mb", type=float, default=MAX_FILE_MB)
    s.add_argument("--budget-gb", type=float, default=BUDGET_GB)
    p = sub.add_parser("parity", help="两棵产物树逐字节对拍")
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    d = sub.add_parser("d2-parity", help="D2 标签构建与当前工作树演示构建（开关缺省关闭）对拍")
    d.add_argument("--ref", default="v0.2.2")              # 同 mk/deploy.mk D2_REF（ADR-209）
    d.add_argument("--work", type=Path, help="对拍工作目录（缺省临时目录，结束后删除）")
    d.add_argument("--keep", action="store_true", help="保留临时工作目录")
    for x in (b, s, p, d):
        x.add_argument("--json", type=Path, help="报告写到该文件")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            if not a.dist.is_dir():
                print(f"scan_public: no build at {a.dist}", file=sys.stderr)
                return 2
            rep = scan_build(a.dist, a.profile, test_ids=load_test_ids(a.test_ids))
        elif a.cmd == "static":
            if not a.root.is_dir():
                print(f"scan_public: no directory {a.root}", file=sys.stderr)
                return 2
            rep = scan_static(a.root, test_ids=load_test_ids(a.test_ids), max_file_mb=a.max_file_mb, budget_gb=a.budget_gb)
        elif a.cmd == "parity":
            if not a.a.is_dir() or not a.b.is_dir():
                print(f"scan_public: parity needs two directories ({a.a}, {a.b})", file=sys.stderr)
                return 2
            rep = parity(a.a, a.b)
        else:
            rep = d2_parity(a.ref, a.work, keep=a.keep)
    except subprocess.CalledProcessError as e:
        print(f"scan_public: {' '.join(map(str, e.cmd))} failed: {e.stderr.decode(errors='replace') if e.stderr else e}", file=sys.stderr)
        return 2
    rep = {"schema": SCHEMA, "at": utc_now(), **rep}
    print("\n".join(summary(rep)))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
