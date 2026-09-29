"""TS 对拍夹具（M07-AC-001、M07-AC-011）：Python 编码的关键帧字节与 1 h 锚点序列。

`python tests/environment/envfix.py` 重新生成 `tests/environment/fixtures/{keyframes,anchors_1h}.json`；
`test_keyframe.py::test_fixture_up_to_date` 与 `test_anchors.py::test_fixture_up_to_date` 断言夹具与当前实现一致。
TS 端 `apps/web/tests/environment/{keyframe,anchors}.test.ts` 读取同一夹具。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from awr.environment.anchors import H_NS, AnchorIntegrator, Anchors  # noqa: E402
from awr.environment.field import world_config  # noqa: E402
from awr.environment.keyframe import EnvKeyframe, EnvOp, KeyframeManager  # noqa: E402
from awr.environment.weather.presets import CLOUD2D, DIR, MOR_BG, SPEED_REF, presets  # noqa: E402
from awr.environment.wind.gust import GustEvent  # noqa: E402

OUT = Path(__file__).resolve().parent / "fixtures"
SEC = 1_000_000_000
BOUNDS = ([-924.019, -999.523], [924.019, 999.523])


def _mgr(preset: str = "clear") -> KeyframeManager:
    P = presets()
    return KeyframeManager(world_id="sanfrancisco", seed=123456, epoch=3, config=world_config(None), initial=P.preset_vector(preset),
                           initial_preset=preset, bounds_min=BOUNDS[0], bounds_max=BOUNDS[1])


def _anch(t: int, s: float = 1234.5678, frac: bool = True) -> Anchors:
    return Anchors(t, s, [s * 0.7071 + 0.1, -s * 0.7071 - 0.3, 12.25 if frac else 0.0], s * 0.61, s * 0.07, 0.4321, 0.2109)


def keyframe_cases() -> list[dict]:
    """§6.2.5 的 6 类帧：稳态预设、预设切换（≤ 1 锋面）、4 段路由、任意值 env/set、D1 最坏、事件满 4 项。"""
    out: list[dict] = []
    t = 123_456 * H_NS
    m = _mgr("clear")
    steady = m.kf.with_heartbeat(t, _anch(t))
    out.append({"name": "steady_preset", "limit": 1024, "frame": steady})
    m2 = _mgr("rain")
    kf = m2.apply(EnvOp("preset", name="heavyRain"), t, _anch(t))
    ev = GustEvent(1, 7, t - 40 * H_NS, 1777.123, -1311.25, 4.25, 120.0, 270.0, 2710.5)
    kf.events = [ev]
    out.append({"name": "preset_switch_1front", "limit": 1024, "frame": kf})
    m3 = _mgr("clear")
    m3.kf.to_preset = "clear"
    kf3 = m3.apply(EnvOp("preset", name="thunderstorm"), t, _anch(t))
    assert len(kf3.via) == 4
    out.append({"name": "route_4seg", "limit": 1024, "frame": kf3})
    m4 = _mgr("fog")
    kf4 = m4.apply(EnvOp("set", patch={"wind": {"speed_ref_mps": 7.37, "dir_from_deg": 123.45}, "atmosphere": {"mor_bg_m": 2345.6}},
                         duration_s=3.3), t, _anch(t))
    out.append({"name": "env_set_arbitrary", "limit": 1200, "frame": kf4})
    m5 = _mgr("clear")
    m5.kf.to_preset = "clear"
    base = m5.kf
    s = base.from_.copy() + np.linspace(0.013, 0.271, 21)
    s[DIR] = 187.331
    s[CLOUD2D] = 0.123
    s[MOR_BG] = 12345.67
    base.from_ = s
    base._tr = None
    kf5 = m5.apply(EnvOp("preset", name="thunderstorm"), t, _anch(t))
    kf5.to = kf5.to + 0.01731
    kf5._tr = None
    kf5.events = [GustEvent(1, 100 + i, t - i * H_NS, 1000.123 + i, -1200.456 - i, 5.5 + 0.1 * i, 120.25, 91.7 + i, 2600.75) for i in range(4)]
    kf5.vis = {"streamlines": "/api/env/streamlines/analytic-3fa2c1d9/d271.awsl", "vmax_mps": 20}
    out.append({"name": "d1_worst", "limit": 1536, "frame": kf5})
    kf6 = steady.with_heartbeat(t, _anch(t, 0.0, frac=False))
    out.append({"name": "zero_anchors_integers", "limit": 1024, "frame": kf6})
    return out


def keyframe_fixture() -> dict:
    cases = []
    for c in keyframe_cases():
        f: EnvKeyframe = c["frame"]
        b = f.encode()
        cases.append({"name": c["name"], "limit": c["limit"], "hex": b.hex(), "bytes": len(b), "version": f.version})
    return {"schema": "awr.test.env.keyframes.v1", "cases": cases}


def anchors_fixture(hours: float = 1.0, seed: int = 20260929) -> dict:
    """1 h 仿真的随机 env/set 与预设序列（seed 固定）：变化帧（线上形态）与服务端锚点检查点（M07-AC-011）。"""
    P = presets()
    rng = np.random.default_rng(seed)
    m = _mgr("clear")
    integ = AnchorIntegrator(m.kf.anchors.copy())
    frames = [m.kf.to_wire()]
    checkpoints = []
    k_end = int(hours * 3600 * SEC) // H_NS
    next_op = int(rng.integers(50, 1500))
    next_cp = 997
    key = 0
    for k in range(1, k_end + 1):
        integ.advance_to(m.kf.transition(), k, key)
        t = k * H_NS
        if k == next_op:
            if rng.uniform() < 0.5:
                op = EnvOp("preset", name=str(rng.choice(P.ids)), duration_s=float(rng.choice([0.0, 5.0, 30.0, 61.3])))
            else:
                patch = {"wind": {"speed_ref_mps": round(float(rng.uniform(0, 20)), 3), "dir_from_deg": round(float(rng.uniform(0, 359.9)), 2)},
                         "precip": {"rain_mmh": round(float(rng.uniform(0, 40)), 2)}}
                op = EnvOp("set", patch=patch, duration_s=float(rng.choice([0.0, 3.0, 17.5])), mode=str(rng.choice(["smooth", "exp", "step"])))
            m.apply(op, t, integ.A)
            key += 1
            frames.append(m.kf.to_wire())
            next_op = k + int(rng.integers(100, 6000))
        if k in (next_cp, k_end):
            checkpoints.append(integ.A.to_json())
            next_cp = k + int(rng.integers(3000, 20000))
    return {"schema": "awr.test.env.anchors.v1", "grid_ns": H_NS, "frames": frames, "checkpoints": checkpoints}


def awsl_fixture() -> bytes:
    """small analytic-field AWSL (20 lines) for apps/web/tests/environment decoding"""
    from awr.environment.wind.profile import DEFAULT_PROFILE
    from awr.environment.wind.streamlines import encode_awsl, generate

    lines = generate(250.0, DEFAULT_PROFILE, ((-500.0, -500.0, 0.0), (500.0, 500.0, 300.0)), n_lines=20, seed=3)
    return encode_awsl(lines, 250.0, 0x1234ABCD, 3)


def write_all() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sample.awsl").write_bytes(awsl_fixture())
    print(f"wrote {OUT / 'sample.awsl'}")
    for name, doc in (("keyframes.json", keyframe_fixture()), ("anchors_1h.json", anchors_fixture())):
        (OUT / name).write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"wrote {OUT / name}")


_ = SPEED_REF

if __name__ == "__main__":
    write_all()
