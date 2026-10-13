"""TS 计算器的对拍夹具（M19-AC-017 的 Python 侧；M19-NFR-006）。

`apps/web/tests/m19/calc_cases.json` 由本文件生成（`AWR_REGEN_GOLDEN=1 pytest tests/perception/test_calc_golden.py`）：
200 组随机统一判据输入（GX40 各输出档与焦距、GX40 NIR 带或不带外挂照明器、LWIR 四种镜头；识别物尺寸、对比度、伪装、热、遮挡；
照度、照度档、MOR、湿度、热交叉；斜距、俯视角、方位、视线、跟踪与相对速度、所需等级）、geometric 可行窗口表与参考作用距离。
vitest `apps/web/tests/m19/calc.test.ts` 以 ≤ 1% 对拍；本用例校验入库文件与当前 Python 实现一致。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pytest

from awr.sim.perception import EnvSample, SensorState, ViewGeom, gx40, gx40_nir, lwir640, perception_level, ref_ranges
from awr.sim.perception.feasibility import golden_rows
from awr.sim.targets import TargetConfig
from awr.sim.targets.model import TargetView

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "apps" / "web" / "tests" / "m19" / "calc_cases.json"


def _sensor(d: dict):
    if d["name"] == "gx40":
        return gx40_nir(d.get("ext", False), d.get("analysis", "1080p")) if d.get("night") else gx40(d.get("analysis", "1080p"))
    return lwir640(float(d["lens"]))


def _case(rng: np.random.Generator) -> dict:
    kind = rng.choice(["eo", "eo", "nir", "lwir"])
    if kind == "eo":
        sd = {"name": "gx40", "analysis": str(rng.choice(["1080p", "4k", "720p", "sxga", "1p3m"]))}
        f = float(np.exp(rng.uniform(math.log(4.8), math.log(48.0))))
        mode = str(rng.choice(["auto", "day", "lowlight"]))
    elif kind == "nir":
        sd = {"name": "gx40", "analysis": "1080p", "night": True, "ext": bool(rng.integers(0, 2))}
        f = float(np.exp(rng.uniform(math.log(4.8), math.log(48.0))))
        mode = "nir"
    else:
        sd = {"name": "lwir640", "lens": float(rng.choice([13.0, 25.0, 75.0, 100.0]))}
        f = sd["lens"]
        mode = "auto"
    tgt = {"size": [float(rng.uniform(0.2, 5.0)), float(rng.uniform(0.2, 2.0)), float(rng.uniform(0.1, 2.0))],
           "contrast0": float(rng.uniform(-0.8, 1.0)), "camouflage": float(rng.uniform(0.0, 0.6)), "reflect850": float(rng.uniform(0.05, 0.8)),
           "thermal": {"mode": str(rng.choice(["relative", "absolute"])), "deltaTK": float(rng.uniform(-5, 50)), "tSurfaceC": float(rng.uniform(-10, 60)),
                       "emissivity": float(rng.uniform(0.6, 0.99)), "hotFraction": float(rng.uniform(0.05, 1.0))},
           "occlusion": float(rng.choice([0.0, 0.0, 0.3, 0.8]))}
    band = str(rng.choice(["day", "low", "night"]))
    env = {"illumLx": float(10 ** rng.uniform(-3, 5)), "band": band, "morM": float(10 ** rng.uniform(2.5, 4.5)), "rhoVGm3": float(rng.uniform(1, 20)),
           "kCross": float(rng.choice([0.5, 1.0])), "tBgC": float(rng.uniform(-5, 35)), "epsBg": float(rng.uniform(0.85, 0.98)),
           "tSkyC": float(rng.uniform(-40, 0))}
    geom = {"rangeM": float(10 ** rng.uniform(1.0, 3.5)), "epsRad": float(rng.uniform(0.0, 1.5)), "betaRad": float(rng.uniform(-math.pi, math.pi)),
            "worst": bool(rng.integers(0, 2)), "kLos": float(rng.choice([1.0, 2 / 3, 1 / 3])), "tracking": bool(rng.integers(0, 2)),
            "vRelPerpMps": float(rng.uniform(0, 20)), "inIllumCone": bool(rng.random() > 0.1)}
    n_req = float(rng.choice([3.5, 14.0, 22.4, 10.0, 30.0]))
    return {"sensor": sd, "fMm": f, "mode": mode, "target": tgt, "env": env, "geom": geom, "nReq": n_req}


def _eval(c: dict) -> dict:
    sn = _sensor(c["sensor"])
    t = c["target"]
    th = t["thermal"]
    cfg = TargetConfig.from_dict({"class": "object", "pos": [0.0, 0.0, 0.0], "size_m": dict(zip("lwh", t["size"], strict=True)),
                                  "contrast0": t["contrast0"], "camouflage": t["camouflage"], "reflect_850": t["reflect850"],
                                  "thermal": {"mode": th["mode"], "delta_t_k": th["deltaTK"], "t_surface_c": th["tSurfaceC"],
                                              "emissivity": th["emissivity"], "hot_fraction": th["hotFraction"]}}, "object-01")
    view = TargetView(1, np.array([0]), np.array([6]), np.zeros((1, 3)), np.zeros(1), np.zeros((1, 3)), np.array([t["size"]]),
                      np.array([t["occlusion"]]), (cfg,), 0)
    g = c["geom"]
    R, e, b = g["rangeM"], g["epsRad"], g["betaRad"]
    p = (R * math.cos(e) * math.cos(b), R * math.cos(e) * math.sin(b), R * math.sin(e))
    en = c["env"]
    env = EnvSample(en["illumLx"], en["band"], en["morM"], en["rhoVGm3"], en["kCross"], en["tBgC"], en["epsBg"], en["tSkyC"])
    r = perception_level(SensorState(sn, c["fMm"], c["mode"]), view, 0,
                         ViewGeom(p, g["kLos"], g["tracking"], g["vRelPerpMps"], True, g["inIllumCone"], g["worst"]), env, c["nReq"])
    return {"level": r.level, "n": r.n, "nEff": r.n_eff, "p": r.p, "gsdM": r.gsd_m, "kC": r.k[1], "kLight": r.k[2], "kBlur": r.k[3],
            "limiting": r.limiting, "dtAppK": r.dt_app_k}


def build() -> dict:
    rng = np.random.default_rng(20261005)
    cases = []
    for _ in range(200):
        c = _case(rng)
        c["expect"] = _eval(c)
        cases.append(c)
    feas = [{k: r[k] for k in ("class", "sensor", "light", "mor_m", "d_lo_m", "d_hi_m", "agl_m", "why")} for r in golden_rows()]
    refs = []
    for key, sd in (("gx40_1080p", {"name": "gx40", "analysis": "1080p"}), ("gx40_4k", {"name": "gx40", "analysis": "4k"}),
                    ("nir_builtin", {"name": "gx40", "analysis": "1080p", "night": True}),
                    ("nir_ill_l", {"name": "gx40", "analysis": "1080p", "night": True, "ext": True}),
                    ("lwir_13", {"name": "lwir640", "lens": 13.0}), ("lwir_100", {"name": "lwir640", "lens": 100.0})):
        refs.append({"key": key, "sensor": sd, "rows": ref_ranges(_sensor(sd))})
    return {"schema": "awr.fixture.m19_calc.v1", "source": "tests/perception/test_calc_golden.py", "cases": cases, "feasibility": feas,
            "ref_ranges": refs}


def _close(a, b, rel=1e-6, abs_=1e-9) -> bool:
    if a is None or b is None or isinstance(a, str) or isinstance(b, str):
        return a == b
    return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=abs_)


def test_ts_golden_is_current():
    fresh = build()
    if os.environ.get("AWR_REGEN_GOLDEN") == "1" or not OUT.is_file():
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(fresh, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    old = json.loads(OUT.read_text(encoding="utf-8"))
    assert len(old["cases"]) == 200
    for o, n in zip(old["cases"], fresh["cases"], strict=True):
        for k, v in n["expect"].items():
            assert _close(o["expect"][k], v), (k, o["expect"][k], v)
    for o, n in zip(old["feasibility"], fresh["feasibility"], strict=True):
        assert o == n
    for o, n in zip(old["ref_ranges"], fresh["ref_ranges"], strict=True):
        assert o["rows"] == n["rows"]


def test_cases_cover_all_levels_and_limits():
    c = json.loads(OUT.read_text(encoding="utf-8")) if OUT.is_file() else build()
    levels = {x["expect"]["level"] for x in c["cases"]}
    lims = {x["expect"]["limiting"] for x in c["cases"]}
    assert levels == {0, 1, 2, 3}
    assert {"pixels", "contrast", "light", "blur", None} <= lims


@pytest.mark.parametrize("n", [0.0, 2.0, 8.0, 14.0, 22.4, 100.0])
def test_ttpf_monotone(n):
    from awr.sim.perception import ttpf

    assert 0.0 <= ttpf(n, 8.0) <= 1.0
