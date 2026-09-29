"""ENU/FLU 只读视图（M08-AC-042；M08-FR-087、NFR-019；AWR-03 §5.1 规则 8、§5.3）。

- 1000 组随机状态：`enu.pos/vel/q_xyzw/omega_flu` 与 tap 写出的 Full64 字段逐元素相等（float32），与 M02
  `ned_frd_to_enu_flu_batch` 逐元素相等；numba 与 numpy 两种换算实现逐位一致；
- 同一 tick（`S.version` 不变）多次访问只换算一次（`refreshes` 计数）；写 p/v/q 的 stage 置脏后再换算；
- 视图写入抛 `ValueError`；
- 静态检查（AST）：`awr/sim/fleet/**` 以外（M08 的 core、runtime、backends、orchestrator 与插件包 safety、mission、
  planning、sensors、`awr.environment`）不访问 FleetState 的 NED/FRD 数组（`S.p`、`self.S.v` 等），只经 `S.enu` 视图。
  该规则同时以请求交 M00 并入 `tools/lint`（见实现报告），在此之前由本用例在 `make test` 中把关。
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from awr.contracts.layouts import DRONE_STATE64
from awr.sim.fleet import kernels_l1 as KL
from awr.sim.fleet.stages.tap import TapStage
from awr.sim.fleet.state import FALLBACK_BLOCKS, FleetState, convert_numpy
from awr.world.georef import frames as F

PKG = Path(__file__).resolve().parents[2] / "python" / "awr"
NED_FIELDS = {"p", "v", "q", "omega", "a_meas", "p_prev", "q_sp", "pos_ref", "home", "tr_x", "tr_v", "tr_a", "target",
              "pos_sp", "vel_cmd", "wind", "land_xy", "ground_z", "force", "thr_sp", "axis_anchor"}
SCAN = ["sim/core", "sim/runtime", "sim/backends", "sim/orchestrator", "sim/safety", "sim/mission", "sim/planning",
        "sim/sensors", "environment"]
# 过渡期例外：M10 跟踪器按 TRAJ 契约写 NED 设定点（M08 §6.4.1 order 027）；M08 已提供 `actions.set_traj_enu`，
# 迁移请求见 .cache/impl/requests/M08-to-M10.md，迁移后删除本条。
ALLOW = {("sim/mission/tracker.py", "tr_x"), ("sim/mission/tracker.py", "tr_v"), ("sim/mission/tracker.py", "tr_a")}


def _random(n: int = 1000, seed: int = 1) -> FleetState:
    rng = np.random.default_rng(seed)
    S = FleetState(n, blocks=dict(FALLBACK_BLOCKS))
    S.active[:] = True
    S.agent_no[:] = np.arange(n)
    S.p[:] = rng.normal(0, 300, (n, 3))
    S.v[:] = rng.normal(0, 5, (n, 3))
    q = rng.normal(size=(n, 4))
    S.q[:] = q / np.linalg.norm(q, axis=1)[:, None]
    S.omega[:] = rng.normal(0, 1, (n, 3))
    S.a_meas[:] = rng.normal(0, 2, (n, 3))
    S.pos_ref[:] = S.p + rng.normal(0, 0.5, (n, 3))
    S.touch()
    return S


def test_views_equal_tap_and_m02() -> None:
    S = _random()
    tap = TapStage(None, lease_owner=lambda: np.zeros(S.capacity, np.uint8), roster_version=lambda: 1)
    idx = np.arange(S.capacity, dtype=np.int32)
    full = np.zeros(idx.size, DRONE_STATE64)
    tap.fill(S, full, np.zeros(idx.size, dtype=tap.last_lite.dtype), idx)
    E = S.enu
    for fld, view in (("pos", E.pos), ("vel", E.vel), ("q", E.q_xyzw), ("omega", E.omega_flu)):
        assert np.array_equal(full[fld], view.astype(np.float32)), fld
    op, ov, oq = np.empty((S.capacity, 3)), np.empty((S.capacity, 3)), np.empty((S.capacity, 4))
    F.ned_frd_to_enu_flu_batch(S.p.copy(), S.v.copy(), S.q.copy(), op, ov, oq)
    assert np.array_equal(op, E.pos) and np.array_equal(ov, E.vel) and np.array_equal(oq, E.q_xyzw)
    assert np.array_equal(E.omega_flu, F.flu_to_frd(S.omega))  # FRD 与 FLU 互换为同一对合置换


def test_numba_and_numpy_convert_identical() -> None:
    if not KL.HAVE_NUMBA:
        pytest.skip("numba 不可用")
    S = _random(seed=2)
    S.enu.refresh(force=True)
    a = {k: getattr(S.enu, k).copy() for k in ("pos", "vel", "acc", "q_xyzw", "q_sp_xyzw", "omega_flu", "pos_ref", "home")}
    convert_numpy(S, np.arange(S.capacity), S.enu)
    for k, v in a.items():
        assert np.array_equal(v, getattr(S.enu, k)), k


def test_refresh_once_per_version_and_readonly() -> None:
    S = _random(50)
    E = S.enu
    n0 = E.refreshes
    for _ in range(5):
        _ = E.pos, E.vel, E.q_xyzw, E.omega_flu
    assert E.refreshes == n0 + 1
    S.p[3, 0] += 1.0
    S.touch()  # 写 p/v/q 的 stage 负责置脏
    assert E.pos[3, 1] == S.p[3, 0] and E.refreshes == n0 + 2
    with pytest.raises(ValueError):
        E.pos[0, 0] = 1.0
    with pytest.raises(ValueError):
        E.q_xyzw[:] = 0.0
    assert E.land_xy(np.array([1]))[0].tolist() == [S.land_xy[1, 1], S.land_xy[1, 0]]


def _is_fleet_state(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "S"
    return isinstance(node, ast.Attribute) and node.attr == "S"


def ned_access_violations(root: Path = PKG, scan: list[str] = SCAN) -> list[str]:
    out = []
    for sub in scan:
        d = root / sub
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in NED_FIELDS and _is_fleet_state(node.value):
                    rel = str(f.relative_to(root))
                    if (rel, node.attr) not in ALLOW:
                        out.append(f"{rel}:{node.lineno} {ast.unparse(node)}")
    return sorted(out, key=lambda x: (x.split(" ")[0].split(":")[0], int(x.split(" ")[0].split(":")[1]), x))


def test_no_ned_access_outside_fleet() -> None:
    bad = ned_access_violations()
    assert not bad, bad[:20]


def test_rule_catches_violation(tmp_path: Path) -> None:
    (tmp_path / "sim" / "safety").mkdir(parents=True)
    (tmp_path / "sim" / "safety" / "x.py").write_text("def f(S, self):\n    a = S.p[0]\n    b = self.S.v\n    c = S.enu.pos\n")
    bad = ned_access_violations(tmp_path, ["sim/safety"])
    assert [b.split(" ")[1] for b in bad] == ["S.p", "self.S.v"]
    assert ned_access_violations(tmp_path, ["sim/fleet"]) == []  # fleet 内不检查（未列入扫描）
