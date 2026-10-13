"""构建信息 `{version, channel, commit, dirty, built_at, release}`（AWR-04 §15.4、ADR-118；17 §17.20.1）。

单一来源：部署目录 `RELEASE`（WP-16：`release`、`version`、`channel`、`commit`、`dirty`、`built_at`，另有 D1 兼容的 `git`、`web`；
`AWR_RELEASE_FILE` 可覆盖路径）与 `pyproject.toml` 的版本；本机无 `RELEASE` 时按构建环境变量与只读 git 推出（无 git 为 `nogit`），
进程内缓存。show-api（`GET /api/health`、容量与 serverInfo）与 sandbox-pool（容量接口 `build`）共用本模块（D2-INT 自
`awr.sandbox.capacity` 移来，后者重新导出）。
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ROOT

__all__ = ["build_info", "public_build"]

_HEX10 = re.compile(r"^[0-9a-f]{10}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_build_cache: dict[str, Any] | None = None


def _read_kv(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with contextlib.suppress(OSError):
        for line in p.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep:
                out[k.strip()] = v.strip()
    return out


def _version(root: Path) -> str:
    try:
        import tomllib

        with open(root / "pyproject.toml", "rb") as f:
            v = str(tomllib.load(f).get("project", {}).get("version") or "")
    except (OSError, ValueError):
        v = ""
    return v if _VERSION.match(v) else "0.0.0"


def _git_commit(root: Path) -> tuple[str, bool]:
    """只读 git（`rev-parse`、`status --porcelain`）；不可用时 ("nogit", False)。"""
    try:
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=10", "HEAD"], capture_output=True, text=True,
                             timeout=2.0, check=False).stdout.strip()
        if not _HEX10.match(sha):
            return "nogit", False
        st = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                            text=True, timeout=2.0, check=False).stdout.strip()
        return sha, bool(st)
    except (OSError, subprocess.SubprocessError):
        return "nogit", False


def build_info(env: dict[str, str] | None = None, *, root: Path = ROOT, refresh: bool = False) -> dict[str, Any]:
    """`{version, channel, commit, dirty, built_at, release}`（17 §17.20.1）。"""
    global _build_cache
    if _build_cache is not None and not refresh and env is None:
        return dict(_build_cache)
    e = os.environ if env is None else env
    rel = _read_kv(Path(e["AWR_RELEASE_FILE"]) if e.get("AWR_RELEASE_FILE") else root / "RELEASE")
    git = rel.get("git") or ""
    dirty = git.endswith("-dirty")
    commit = git[:-6] if dirty else git
    if rel.get("commit") and (_HEX10.match(rel["commit"]) or rel["commit"] == "nogit"):
        commit, dirty = rel["commit"], rel.get("dirty", "").lower() in ("1", "true") or dirty
    if not (_HEX10.match(commit) or (rel and commit == "nogit")):
        sha = (e.get("AWR_BUILD_SHA") or "")[:10]
        if _HEX10.match(sha):
            commit, dirty = sha, e.get("AWR_BUILD_DIRTY") in ("1", "true")
        elif rel:
            commit, dirty = "nogit", False
        else:
            commit, dirty = _git_commit(root)
    channel = rel.get("channel") or e.get("AWR_BUILD_CHANNEL") or \
        ("demo" if "VITE_AWR_DEMO=public" in rel.get("web", "") else "local")
    if channel not in ("demo", "local", "ci"):
        channel = "local"
    built_at = rel.get("built_at") or e.get("AWR_BUILD_TIME") or ""
    if not built_at:
        sde = e.get("SOURCE_DATE_EPOCH")
        t = int(sde) if sde and sde.isdigit() else int(time.time())
        built_at = datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    release = rel.get("release") or e.get("AWR_RELEASE") or f"local-{commit}"
    version = rel.get("version") if _VERSION.match(rel.get("version", "")) else _version(root)
    out = {"version": version, "channel": channel, "commit": commit, "dirty": bool(dirty), "built_at": built_at,
           "release": release}
    if env is None:
        _build_cache = dict(out)
    return out


def public_build(env: dict[str, str] | None = None) -> dict[str, Any]:
    """公开字段 `{version, channel, release}`（`GET /api/health`、SB-00；提交与构建时刻只给 admin 的 sys/info）。"""
    b = build_info(env)
    return {"version": b["version"], "channel": b["channel"], "release": b["release"]}
