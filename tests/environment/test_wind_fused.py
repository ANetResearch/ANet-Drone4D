"""env 查询"只求风"融合路径与 numpy 路径逐位相同（FX2-R2；M07-FR-016；`turbulence.wind_fused`）。

env stage 在 5 个 env tick 中的 4 个只求风（`Fields.WIND_PARTS`、GLOBAL 帧），走 numba 融合实现；`Fields.WIND` 位会让
查询走原 numpy 路径（另求光学），两者的风分量（均值、阵风、湍流、合成）必须逐位相同，CALM 与 VALID 标志相同。
覆盖三种廓线、无湍流、湍流盒与 Dryden 三种湍流方式，以及 DTM 地面高（格心双线性，float32 舍入）与常数地面高。
"""

from __future__ import annotations

import numpy as np
import pytest

from awr.environment.field import EnvironmentServiceImpl
from awr.environment.query import EnvFlags, Fields
from awr.environment.wind import turbulence as TB

pytestmark = pytest.mark.skipif(not TB._HAVE_NB, reason="numba unavailable")

BOUNDS = ((-900.0, -1000.0, 0.0), (900.0, 1000.0, 300.0))
COORD = {"ground": {"zM": 2.0}, "anchor": {"hMslM": 12.2}}


class _Grid:
    def __init__(self, a: np.ndarray) -> None:
        self.a = a
        self.x0_m, self.y0_m, self.cell_m = -900.0, -1000.0, 10.0


class _WQ:
    """M04 WorldQuery 的最小替身：DTM 栅格（float32，格心双线性由 env 自己求）。"""

    def __init__(self, seed: int = 3) -> None:
        rng = np.random.default_rng(seed)
        self.g = _Grid((rng.random((200, 180)) * 30.0).astype(np.float32))

    def dtm_grid(self) -> _Grid:
        return self.g

    def ground_dtm(self, xy: np.ndarray) -> np.ndarray:  # 不应被调用（DTM 双线性走 env 的 numba 路径）
        raise AssertionError("M04 path not expected")


def _svc(kind: str, model: str, with_wq: bool) -> EnvironmentServiceImpl:
    env = EnvironmentServiceImpl(world_id="t", world_seed=1, bounds=BOUNDS, initial_preset="heavyRain",
                                 coordinate=COORD, load_assets=False)
    if with_wq:
        env.wq = _WQ()
        env._dtm_nb = None
    cfg = env.kf.config["wind"]
    cfg["level"] = 1
    cfg["profile"]["kind"] = kind
    cfg["turbulence"]["model"] = model
    if model == "box":
        env.turb = TB.TurbBox(TB.box_to_rgba(TB.vk_box(n=8, dx=4.0, L=30.0, seed=5)), 4.0)
    if model == "dryden":
        env.dryden.out[:] = np.random.default_rng(9).normal(size=env.dryden.out.shape)
    env.kf.events = []
    env.on_env_tick_all(0)
    return env


@pytest.mark.parametrize("with_wq", [True, False])
@pytest.mark.parametrize("model", ["off", "box", "dryden"])
@pytest.mark.parametrize("kind", ["log", "power", "uniform"])
def test_wind_only_fused_matches_numpy(kind: str, model: str, with_wq: bool) -> None:
    env = _svc(kind, model, with_wq)
    rng = np.random.default_rng(11)
    n = 257
    pos = np.column_stack([rng.uniform(-950, 950, n), rng.uniform(-1050, 1050, n), rng.uniform(-5.0, 180.0, n)])
    pos[0, 2] = 2.0  # 地面附近（廓线为 0、近地衰减为 0）
    agent = rng.permutation(n).astype(np.int64)
    t = env.t_grid_ns
    a = env.query(pos, t, fields=int(Fields.WIND_PARTS), agent_idx=agent)
    fused = {k: getattr(a, k)[:n].copy() for k in ("wind_mean_mps", "wind_gust_mps", "wind_turb_mps", "wind_mps",
                                                     "gust_long_mps", "source_level")}
    fl_a = a.flags[:n].copy()
    env.fuse_wind = False  # numpy 路径作对拍基准（全量查询此后也走融合路径，FX2-R3）
    b = env.query(pos, t, fields=int(Fields.WIND_PARTS | Fields.WIND), agent_idx=agent)
    env.fuse_wind = True
    for k, v in fused.items():
        assert v.tobytes() == getattr(b, k)[:n].tobytes(), k
    m = np.uint8(int(EnvFlags.VALID) | int(EnvFlags.CALM))
    assert ((fl_a & m) == (b.flags[:n] & m)).all()
    if model != "off":
        assert np.abs(fused["wind_turb_mps"]).max() > 0  # 湍流确实参与


_ALL_OUT = ("wind_mps", "wind_mean_mps", "wind_gust_mps", "wind_turb_mps", "turb_sigma_mps", "turb_l_m", "gust_long_mps",
            "sigma_ext_per_m", "mor_m", "sigma_precip_per_m", "rain_eff_mmh", "snow_eff_mmh", "dust", "temperature_c",
            "pressure_pa", "rho_kgm3", "rh", "flags", "source_level")


@pytest.mark.parametrize("with_wq", [True, False])
@pytest.mark.parametrize("model", ["off", "box", "dryden"])
@pytest.mark.parametrize("kind", ["log", "power"])
def test_full_query_fused_wind_matches_numpy(kind: str, model: str, with_wq: bool) -> None:
    """全量查询（env stage 的 10 Hz 全量 tick，ENV_STAGE_FIELDS）中风由融合路径求、其余字段 numpy：全部输出字段与整段 numpy
    路径逐字节相同；随后写入的 EnvSample32 行缓存（numba 写入核）也与 numpy 写入逐字节相同（FX2-R3，ADR-070）。"""
    from awr.environment import kernels_rows as KR
    from awr.environment.stage import ENV_STAGE_FIELDS

    env = _svc(kind, model, with_wq)
    rng = np.random.default_rng(17)
    n = 300
    pos = np.column_stack([rng.uniform(-950, 950, n), rng.uniform(-1050, 1050, n), rng.uniform(-5.0, 180.0, n)])
    agent = np.sort(rng.choice(env.capacity, n, replace=False)).astype(np.int64)
    t = env.t_grid_ns
    a = env.query(pos, t, fields=ENV_STAGE_FIELDS, agent_idx=agent)
    got = {k: getattr(a, k)[:n].copy() for k in _ALL_OUT}
    env.store_rows(agent, a)
    rows_nb = env.rows.copy()
    env.fuse_wind = False
    b = env.query(pos, t, fields=ENV_STAGE_FIELDS, agent_idx=agent)
    for k in _ALL_OUT:
        assert got[k].tobytes() == getattr(b, k)[:n].tobytes(), k
    if KR.HAVE_NUMBA:
        KR.HAVE_NUMBA = False
        try:
            env.rows[:] = 0
            env.store_rows(agent, b)
        finally:
            KR.HAVE_NUMBA = True
        assert env.rows.tobytes() == rows_nb.tobytes()


@pytest.mark.parametrize("preset", ["fog", "thunderstorm", "blizzard", "sandstorm", "clear"])
@pytest.mark.parametrize("with_wq", [True, False])
def test_full_query_rest_kernel_matches_numpy(preset: str, with_wq: bool) -> None:
    """全量查询中风以外的字段（MIL 湍流谱、光学含雾层与云下降水标志、降水、ISA 热力）由融合核一次写完（ADR-073 第 3 条）：
    雾、雷暴、暴雪、沙尘与晴天各预设下，全部输出字段与整段 numpy 路径逐字节相同（含雾层顶与云底两侧的位置）。"""
    from awr.environment.stage import ENV_STAGE_FIELDS

    env = EnvironmentServiceImpl(world_id="t", world_seed=1, bounds=BOUNDS, initial_preset=preset, coordinate=COORD,
                                 load_assets=False)
    if with_wq:
        env.wq = _WQ()
        env._dtm_nb = None
    env.kf.events = []
    env.on_env_tick_all(0)
    rng = np.random.default_rng(23)
    n = 400
    pos = np.column_stack([rng.uniform(-950, 950, n), rng.uniform(-1050, 1050, n), rng.uniform(-5.0, 900.0, n)])
    pos[:4, 2] = [61.9, 62.0, 62.1, 2.0]  # 雾层顶（fog_top 60 m AGL + 地面 2 m）两侧与地面
    t = env.t_grid_ns
    a = env.query(pos, t, fields=ENV_STAGE_FIELDS, agent_idx=np.arange(n, dtype=np.int64))
    got = {k: getattr(a, k)[:n].copy() for k in _ALL_OUT}
    env.fuse_wind = False
    b = env.query(pos, t, fields=ENV_STAGE_FIELDS, agent_idx=np.arange(n, dtype=np.int64))
    for k in _ALL_OUT:
        assert got[k].tobytes() == getattr(b, k)[:n].tobytes(), k
