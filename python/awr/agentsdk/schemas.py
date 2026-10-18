"""`packages/contracts/bench/*.v1` 校验器（共用 `awr.sim.schemas.contracts_registry` 的一次扫描）。

载荷中的二进制字段（`x-awr-bin`，如消息 `payload`）在进程内为 bytes；schema 对这些字段不设类型约束，校验照常通过。
"""

from __future__ import annotations

from functools import cache

__all__ = ["BENCH_ID", "bench_validator"]

BENCH_ID = "https://schemas.anet-drone.dev/bench/1/{name}.v1.schema.json"


@cache
def bench_validator(name: str):
    """name ∈ {act, obs, agent_config, task, evaluation, board, score, run_manifest, ...}。"""
    from jsonschema import Draft202012Validator

    from awr.sim.schemas import contracts_registry

    reg = contracts_registry()
    return Draft202012Validator(reg.contents(BENCH_ID.format(name=name)), registry=reg)
