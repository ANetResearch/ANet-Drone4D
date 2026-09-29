"""生成器与校验器版本（AWR-16 §3.5 第 2 条条件 ③、§16.1）。

numpy 锁定版本变化时由同一提交提升 `WORLDPKG_MIN_VERSION`，经 `--missing` 条件 ③ 触发重建（M03 §11 R-1）。
"""

from __future__ import annotations

WORLDPKG_MIN_VERSION = "0.1.0"
VALIDATOR_VERSION = "0.1.0"
SCHEMA_MAJOR = 1


def version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v).split("-")[0].split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def generator_outdated(version: str) -> bool:
    return version_tuple(version) < version_tuple(WORLDPKG_MIN_VERSION)
