"""tests/runtime 公共辅助函数（测试模块经 sys.path 垫片导入：pytest 使用 --import-mode=importlib）。"""

from __future__ import annotations

import json
import os
import socket
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAKE_CHILD = Path(__file__).with_name("fake_child.py")
CONTRACTS = ROOT / "packages" / "contracts"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def open_pair(kind: str, namespace: str, loop=None):
    """(服务方 a, 调用方 b)；zenoh 时 a 监听随机回环端口，b 连接 a。"""
    from awr.runtime.bus import LocalBus, ZenohBus

    if kind == "local":
        return (LocalBus.open("server", namespace=namespace, loop=loop), LocalBus.open("client", namespace=namespace, loop=loop))
    ep = f"tcp/127.0.0.1:{free_port()}"
    a = ZenohBus.open("server", namespace=namespace, listen=[ep], connect=[], loop=loop)
    b = ZenohBus.open("client", namespace=namespace, listen=["tcp/127.0.0.1:0"], connect=[ep], loop=loop)
    return a, b


def wait_until(pred, timeout: float = 5.0, step: float = 0.01) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


@cache
def _registry():
    from referencing import Registry, Resource

    reg = Registry()
    for p in sorted(CONTRACTS.rglob("*.schema.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    return reg


@cache
def validator(rel: str, ref: str | None = None):
    """契约 schema 校验器；ref 为 `#/$defs/<name>` 片段时校验该定义。"""
    from jsonschema import Draft202012Validator

    d = json.loads((CONTRACTS / rel).read_text(encoding="utf-8"))
    schema = {"$ref": f"{d['$id']}{ref}"} if ref else d
    return Draft202012Validator(schema, registry=_registry())


def schema_errors(rel: str, inst, ref: str | None = None) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:160]}" for e in validator(rel, ref).iter_errors(inst)]


def child_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("AWR_SUPERVISOR_PID", None)
    env.update({k: str(v) for k, v in extra.items()})
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


PY = sys.executable
