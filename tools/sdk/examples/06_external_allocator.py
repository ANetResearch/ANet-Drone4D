"""06 外部分配器：读取 Allocator 快照，按"最近的空闲机"贪心分配工作项，提交外部计划（服务端做同一可行性检查）。"""

from __future__ import annotations

import math

from _common import sandbox


def _pos(wi: dict) -> list:
    if wi.get("pos"):
        return wi["pos"]
    reg = wi.get("region") or [[0, 0]]
    return [sum(p[0] for p in reg) / len(reg), sum(p[1] for p in reg) / len(reg), 0.0]


def main() -> None:
    sb = sandbox()
    with sb.create("synthcity", "t3_area_patrol") as s:
        snap = s.tasking.snapshot()
        free = {v["id"]: v for v in snap.get("vehicles", [])}
        assignments = []
        for k, wi in enumerate(sorted(snap.get("work_items", []), key=lambda w: -w.get("priority", 0))):
            if not free:
                break
            p = _pos(wi)
            vid = min(free, key=lambda v: math.dist(free[v]["pos"][:2], p[:2]))
            assignments.append({"wid": wi["wid"], "uav": vid, "order": k})
            free.pop(vid)
        rep = s.tasking.submit({"based_on_revision": snap["revision"], "allocator": "external", "assignments": assignments})
        print("计划", rep)


if __name__ == "__main__":
    main()
