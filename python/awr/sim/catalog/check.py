"""目录校验命令 `python -m awr.sim.catalog.check`（CAT-FR-022；AWR-20 §8；进入 `make ci`）。

逐项执行并汇总（不在第一个失败处停止）：

1. 目录索引：schema 与 CI-1 至 CI-4（`loader.Catalog.problems`）；
2. 全部机型（目录列出的包，含隐藏机型）：Vehicle Package schema 1.1、按构型的检查（多旋翼 VH-1 至 VH-7，VTOL VH-1 至 VH-3 与
   VT-1 至 VT-4，固定翼与 VTOL FW-1 至 FW-8、MT-5）、签名 SG-1 至 SG-3、挂载 MNT-1、MT-2、SN-7（告警）、SN-9、derived 比对与 E 级值
   （M08 `profiles.validate_doc`，在隔离登记表中登记 M21 的检查后执行）；
3. 传感器：每个机型的传感器组经 M13 `spec.load_rig` 装配（SN-1 至 SN-6、SN-8、`spec_ref` 合并）；
4. 目录 REST 视图（`rest_view.json`）与源数据一致（`--write-view` 重新生成）；
5. no-emoji 由 `make lint` 负责。

退出码：0 通过，1 有违规，2 工具错误。`--json` 输出机器可读结果；`--vehicles DIR` 指定机型目录（缺省 `AWR_VEHICLES_DIR` 或
`vehicles/`）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

__all__ = ["main", "run_checks"]


def run_checks(vdir: Path | None = None, *, write_view: bool = False, view_path: Path | None = None,
               check_view: bool = True) -> dict[str, Any]:
    from ..fleet import profiles as PF
    from ..fleet.stages import registry as R
    from . import checks as C
    from .loader import Catalog, CatalogError

    t0 = time.perf_counter()
    if vdir is not None:
        os.environ["AWR_VEHICLES_DIR"] = str(vdir)
    out: dict[str, Any] = {"ok": True, "catalog": [], "models": {}, "sensors": {}, "view": None}
    try:
        cat = Catalog.load(vdir)
    except CatalogError as e:
        out["ok"] = False
        out["catalog"] = e.problems
        return out
    out["catalog"] = cat.problems()
    if out["catalog"]:
        out["ok"] = False
    from awr.sim.sensors.spec import SensorSpecError, load_rig

    with R.isolated_registry():
        C.register(R)
        for pid, (doc, f) in sorted(cat.docs.items()):
            checks, _derived, probs = PF.validate_doc(doc, pid=pid, vdir=f.parent)
            warn = [c for c in checks if c.get("pass") and c.get("msg") and c["id"] == "SN-7"]
            out["models"][pid] = {"problems": probs, "checks": len(checks), "warnings": [w["msg"] for w in warn]}
            if probs:
                out["ok"] = False
            smap = doc.get("sensors") or {}
            if smap:
                try:
                    rig = load_rig(f.parent, dict(smap))
                    out["sensors"][pid] = [s.name for s in rig.specs]
                except SensorSpecError as e:
                    out["sensors"][pid] = {"error": e.to_json()}
                    out["ok"] = False
    if write_view or check_view:
        from .view import VIEW_PATH, build_view, canonical_bytes
        from .view import write_view as _write

        path = Path(view_path or VIEW_PATH)
        view = build_view(cat)
        if write_view:
            _write(path, view)
            out["view"] = {"path": str(path), "written": True, "etag": view["etag"]["models"]}
        else:
            want = canonical_bytes(view) + b"\n"
            have = path.read_bytes() if path.exists() else b""
            same = have == want
            out["view"] = {"path": str(path), "up_to_date": same, "etag": view["etag"]["models"]}
            if not same:
                out["ok"] = False
    out["elapsed_s"] = round(time.perf_counter() - t0, 3)
    return out


def _print(res: dict) -> None:
    for p in res["catalog"]:
        print(f"catalog {p['rule']} {p.get('path')}: {p.get('message')}")
    for pid, m in res["models"].items():
        status = "ok" if not m["problems"] else "FAIL"
        print(f"model {pid}: {status} ({m['checks']} checks)")
        for p in m["problems"]:
            print(f"  - {p}")
        for w in m["warnings"]:
            print(f"  warning: {w}")
    for pid, s in res["sensors"].items():
        if isinstance(s, dict):
            print(f"sensors {pid}: FAIL {s['error']}")
    v = res.get("view")
    if v is not None:
        if v.get("written"):
            print(f"view written {v['path']} (etag {v['etag']})")
        else:
            print(f"view {v['path']}: {'up to date' if v['up_to_date'] else 'STALE (run --write-view)'}")
    print("catalog check:", "PASS" if res["ok"] else "FAIL", f"({res.get('elapsed_s', 0)} s)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.sim.catalog.check", description="机型与传感器目录校验（AWR-20 §8）")
    ap.add_argument("--vehicles", default=None, help="机型目录（缺省 AWR_VEHICLES_DIR 或 vehicles/）")
    ap.add_argument("--write-view", action="store_true", help="重新生成目录 REST 视图 rest_view.json")
    ap.add_argument("--no-view", action="store_true", help="不检查 REST 视图")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        res = run_checks(Path(a.vehicles) if a.vehicles else None, write_view=a.write_view, check_view=not a.no_view)
    except Exception as e:  # 工具错误
        print(f"catalog check: tool error {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    else:
        _print(res)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
