"""沙盒场景模板、S7 与验收工具的公共常量与小工具（WP-13，M16；AWR-04 §10.4、ADR-094、ADR-112）。

- 世界、时区与模板 id（与 17 §17.4.2、M15 文案键、M17 `TemplateCatalog` 一致）；
- sim-core 组合根插件清单（D1 四个 + D2 五个，D2 一律写 `.plugin` 入口：`awr.sim.catalog` 与 `awr.sim.fixedwing` 的包本身
  导入无副作用，只有 `.plugin` 才登记功率模型与目录，见 d2-WP-13-to-M17 第 1 条）；
- 规范化 JSON 输出（与 D1 剧本生成器同一编码规则 `dumps_canonical`，同一输入逐字节一致）；
- 平面几何（多边形面积、方向、点在多边形内、正方形与矩形）。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

SCENARIOS = ROOT / "scenarios"
SANDBOX = SCENARIOS / "sandbox"
ANCHORS = SANDBOX / "anchors"
S7_PATH = SCENARIOS / "s7-synthcity-relay.json"
S7_ID = "s7-synthcity-relay"

WORLDS: tuple[str, ...] = ("synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco")
TZ: dict[str, str] = {"synthcity": "Asia/Shanghai", "shenzhen": "Asia/Shanghai", "suzhou": "Asia/Shanghai",
                      "shanghai": "Asia/Shanghai", "newyork": "America/New_York", "chicago": "America/Chicago",
                      "sanfrancisco": "America/Los_Angeles"}
ACOUSTIC_BG: dict[str, str] = {w: "urban" for w in WORLDS} | {"suzhou": "suburban"}

# 模板 id（17 §17.4.2 `template` 取值；M15 文案键 sandbox.template.<id>）
T1, T2, T3, T4, T5, T6 = "t1_point_watch", "t2_polyline", "t3_area_patrol", "t4_scan_gsd", "t5_area_guard", "t6_manual"
TEMPLATE_IDS: tuple[str, ...] = (T1, T2, T3, T4, T5, T6)
DEFAULT_TEMPLATE = T5

CONTRACTS_VERSION = "1.1.0"

D1_PLUGINS = ("awr.environment.stage", "awr.sim.safety", "awr.sim.mission", "awr.sim.sensors.plugin")
D2_PLUGINS = ("awr.sim.catalog.plugin", "awr.sim.fixedwing.plugin", "awr.sim.targets.plugin", "awr.sim.perception.plugin",
              "awr.sim.tasking.plugin")
SANDBOX_PLUGINS = D1_PLUGINS + D2_PLUGINS


def plugins_env() -> str:
    """`AWR_PLUGINS` 的取值（批处理与模板检查用）。"""
    return ",".join(SANDBOX_PLUGINS)


# ---------------------------------------------------------------- JSON
def dumps_canonical(obj: Any) -> str:
    """与 D1 剧本生成器相同的规范化编码（键序保持构造顺序、短数组一行、浮点按生成时舍入）。"""
    from awr.datasets.scenarios.authoring import dumps_canonical as _d

    return _d(obj)


def write_canonical(obj: Any, path: Path) -> bool:
    """写出规范化 JSON；内容未变时不写。返回是否写入。"""
    s = dumps_canonical(obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == s:
        return False
    path.write_text(s, encoding="utf-8")
    return True


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rnd(v: float, nd: int = 1) -> float:
    """坐标舍入（0.1 m）；-0.0 归零，整数值写成整数。"""
    x = round(float(v), nd)
    if x == 0:
        return 0
    return int(x) if x == int(x) else x


def yaw(a: float) -> float:
    """航向舍入到 0.001 rad，并保持在契约范围 [−π, π] 内（舍入后超界时向零取 0.001）。"""
    v = round(math.atan2(math.sin(float(a)), math.cos(float(a))), 3)
    if abs(v) > math.pi:
        v = math.copysign(3.141, v)
    return rnd(v, 3)


def pt(p: Any, nd: int = 1) -> list:
    return [rnd(v, nd) for v in p]


# ---------------------------------------------------------------- 平面几何
def signed_area(P: Any) -> float:
    P = [tuple(map(float, p[:2])) for p in P]
    return 0.5 * sum(P[i][0] * P[(i + 1) % len(P)][1] - P[(i + 1) % len(P)][0] * P[i][1] for i in range(len(P)))


def area(P: Any) -> float:
    return abs(signed_area(P))


def ccw(P: list) -> list:
    """逆时针（契约：多边形 World ENU、逆时针）。"""
    return list(P) if signed_area(P) > 0 else list(reversed(P))


def rect(cx: float, cy: float, hx: float, hy: float) -> list[list[float]]:
    """轴对齐矩形（逆时针，从西南角开始）。"""
    return [pt((cx - hx, cy - hy)), pt((cx + hx, cy - hy)), pt((cx + hx, cy + hy)), pt((cx - hx, cy + hy))]


def square(cx: float, cy: float, side: float) -> list[list[float]]:
    return rect(cx, cy, side / 2.0, side / 2.0)


def inside(xy: Any, P: Any) -> bool:
    """射线法（不含边界的精确处理，模板几何用足够）。"""
    x, y = float(xy[0]), float(xy[1])
    n = len(P)
    c = False
    for i in range(n):
        x1, y1 = float(P[i][0]), float(P[i][1])
        x2, y2 = float(P[(i + 1) % n][0]), float(P[(i + 1) % n][1])
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def dist(a: Any, b: Any) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def centroid(P: Any) -> tuple[float, float]:
    xs = [float(p[0]) for p in P]
    ys = [float(p[1]) for p in P]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def dist_to_polygon_edge(xy: Any, P: Any) -> float:
    """点到多边形边界的最短距离。"""
    best = math.inf
    n = len(P)
    px, py = float(xy[0]), float(xy[1])
    for i in range(n):
        ax, ay = float(P[i][0]), float(P[i][1])
        bx, by = float(P[(i + 1) % n][0]), float(P[(i + 1) % n][1])
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best
