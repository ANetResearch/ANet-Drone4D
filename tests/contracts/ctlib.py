"""Shared helpers for the contract tests (imported via a sys.path shim; pytest runs with --import-mode=importlib)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "packages" / "contracts"
TOOLS = ROOT / "tools" / "contracts"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import contracts_lib as L  # noqa: E402


def load(rel: str):
    return json.loads((CONTRACTS / rel).read_text(encoding="utf-8"))


@cache
def registry():
    from referencing import Registry, Resource

    reg = Registry()
    for p in L.schema_files():
        d = json.loads(p.read_text(encoding="utf-8"))
        reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    return reg


@cache
def validator(rel: str):
    from jsonschema import Draft202012Validator

    return Draft202012Validator(load(rel), registry=registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


def errors(rel: str, inst) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}" for e in validator(rel).iter_errors(inst)]


def node() -> str | None:
    for cand in (shutil.which("node"), str(Path.home() / ".local" / "node" / "bin" / "node")):
        if cand and Path(cand).exists():
            return cand
    return None


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=600)
