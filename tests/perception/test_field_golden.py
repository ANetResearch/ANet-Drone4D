"""TS 对拍夹具（M19-AC-021、AC-022、AC-025 的 Python 侧；M18-AC-025）。

`apps/web/tests/m19/quant_cases.json` 与 `field_cases.json` 由本文件生成（`AWR_REGEN_GOLDEN=1 pytest tests/perception/test_field_golden.py`；
缺失时自动生成），本用例校验入库文件与当前 Python 实现一致：

- quant_cases：200 组声学（24 带、随机声功率、BPF、背景、距离、背向衰减、警觉度、类别频段修正）、单带锚点、200 组视觉、100 组雷达、
  P600 缺省谱形与纯音掩蔽、20 组有效声学半径、紧凑列式 `soa1` 与展开形式（解码逐位相等）；
- field_cases：四种剖面 × 两个机型的 topic 关键帧与精确采样点（前端插值 ≤ 0.5 dB 或 2%，M19-NFR-009）、关键帧加增量帧序列、
  感知体表关键帧与识别物锚点的显式采样（表模型）、10⁴ 个随机点上 4 种敏感体的配置体掩码（与 Python `inside_points`，M18-AC-025）。
vitest：`apps/web/tests/m19/{quant,field}.test.ts`。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import msgpack
import numpy as np
import pytest

from awr.sim.perception import field as F
from awr.sim.perception.quant import (
    FieldEnv,
    QuantTarget,
    QuantUav,
    ReverseQ,
    acoustic_pair,
    encode_soa,
    quant_frame,
    quant_pairs,
    radar_pair,
    visual_pair,
)
from awr.sim.targets import TargetConfig
from awr.sim.targets.acoustic import acoustic_radius, threshold_bands
from awr.sim.targets.envview import TargetEnv, profile_bands
from awr.sim.targets.geometry import body_volumes, inside_points
from awr.sim.targets.signature import P600, signature_from_doc, signature_of

ROOT = Path(__file__).resolve().parents[2]
OUT_Q = ROOT / "apps" / "web" / "tests" / "m19" / "quant_cases.json"
OUT_F = ROOT / "apps" / "web" / "tests" / "m19" / "field_cases.json"


def _env(band: str, bg: float, mor: float = math.inf) -> FieldEnv:
    return FieldEnv(TargetEnv(band, mor, profile_bands("rural", bg), bg, "rural", 1.0, 0, False, 0.0))


def _f(x):
    if x is None:
        return None
    if isinstance(x, (bool, str)):
        return x
    v = float(x)
    return v if math.isfinite(v) else None


def build_quant() -> dict:
    rng = np.random.default_rng(20261006)
    ac = []
    for _ in range(200):
        lwa = float(rng.uniform(70, 100))
        bpf = float(rng.choice([110.0, 120.0, 150.0, 200.0, 220.0]))
        sig = signature_from_doc("x", {"acoustic": {"lwa_hover_dba": lwa, "bpf_hz": bpf}})
        bg = float(rng.uniform(20, 65))
        lbg = list(profile_bands(str(rng.choice(["rural", "suburban", "urban"])), bg))
        inp = {"lwBands": [float(x) for x in sig.lw_bands()], "rM": float(10 ** rng.uniform(0.3, 3.3)), "gBackDb": float(rng.uniform(0, 12)),
               "lBgBands": [float(x) for x in lbg], "masking": [float(x) for x in sig.masking_db()], "hearingThDba": float(rng.choice([15.0, 20.0])),
               "alertness": float(rng.uniform(0, 1)), "advDb": float(rng.choice([0.0, 5.0])),
               "adjDb": [float(x) for x in rng.uniform(-6, 6, 24)] if rng.random() < 0.3 else None}
        q = acoustic_pair(np.array(inp["lwBands"]), inp["rM"], inp["gBackDb"], np.array(inp["lBgBands"]), np.array(inp["masking"]),
                          inp["hearingThDba"], inp["alertness"], inp["advDb"], None if inp["adjDb"] is None else np.array(inp["adjDb"]))
        ac.append({"in": inp, "expect": {"lpDba": q.lp_dba, "lpBandDb": q.lp_band_db, "bandHz": q.band_hz, "thrDba": q.thr_dba, "lBgDba": q.l_bg_dba,
                                         "marginDb": q.margin_db, "p": q.p}})
    vis = []
    for _ in range(200):
        top, side = float(rng.uniform(0.05, 1.0)), float(rng.uniform(0.03, 0.4))
        csky, cgnd = float(rng.uniform(-0.9, -0.2)), float(rng.uniform(0.1, 0.5))
        lit = bool(rng.integers(0, 2))
        sig = signature_from_doc("x", {"visual": {"area_m2": {"top": top, "side": side}, "contrast_sky": csky, "contrast_ground": cgnd}})
        sig = sig.with_lights(lit, False)
        band = str(rng.choice(["day", "low", "night"]))
        mor = float(10 ** rng.uniform(2.5, 4.5))
        inp = {"sig": {"areaTopM2": top, "areaSideM2": side, "contrastSky": csky, "contrastGround": cgnd, "lightsOn": lit, "nightVisibleM": 30.0},
               "rM": float(10 ** rng.uniform(0.7, 3.2)), "elevRad": float(rng.uniform(-1.2, 1.4)), "band": band, "morM": mor,
               "theta0Arcmin": float(rng.uniform(0.5, 3)), "alertness": float(rng.uniform(0, 1)), "acuityArcmin": float(rng.choice([1.0, 2.5, 4.0])),
               "los": bool(rng.random() > 0.1), "inFov": bool(rng.random() > 0.1)}
        q, ok = visual_pair(sig, inp["rM"], inp["elevRad"], _env(band, 40.0, mor), inp["theta0Arcmin"], inp["alertness"], inp["acuityArcmin"],
                            los=inp["los"], in_fov=inp["inFov"])
        vis.append({"in": inp, "expect": {"thetaArcmin": q.theta_arcmin, "dVisM": q.d_vis_m, "nEqPx": q.n_eq_px, "thetaThArcmin": q.theta_th_arcmin,
                                          "ratio": q.ratio, "cApp": q.c_app, "p": q.p, "ok": ok}})
    rad = []
    for _ in range(100):
        rx = {"snr_ref_db": float(rng.uniform(0, 30)), "r_ref_m": float(rng.uniform(50, 1000)), "snr_req_db": float(rng.uniform(5, 20)),
              "fov_az_deg": 60.0, "fov_el_deg": 20.0}
        rcs, r, rain, fov = float(rng.uniform(0.005, 1.0)), float(10 ** rng.uniform(0.5, 3.3)), float(rng.choice([0.0, 5.0, 20.0])), bool(rng.random() > 0.2)
        q = radar_pair(rx, rcs, r, rain, in_fov=fov)
        rad.append({"rx": rx, "rcs": rcs, "rM": r, "rain": rain, "inFov": fov, "expect": {"snrDb": q.snr_db, "marginDb": q.margin_db, "p": q.p}})
    radii = []
    for _ in range(20):
        bg = float(rng.uniform(25, 60))
        lbg = profile_bands("rural", bg)
        a = float(rng.uniform(0, 1))
        g = float(rng.uniform(0, 10))
        thr = threshold_bands(lbg, P600.masking_db(), 20.0, a, 0.0)
        radii.append({"lwBands": [float(x) for x in P600.lw_bands()], "thr": [float(x) for x in thr], "gBackDb": g,
                      "expect": acoustic_radius(P600.lw_bands(), thr, g_back_db=g)})
    # 紧凑列式与展开形式
    us = [QuantUav(f"uav-{i + 1:02d}", ("p600_mid360", "awr_q9", "awr_f1")[i % 3],
                   np.array([float(rng.uniform(-400, 400)), float(rng.uniform(-400, 400)), float(rng.uniform(10, 150))]), i % 5 != 4,
                   signature_of(("p600_mid360", "awr_q9", "awr_f1")[i % 3])) for i in range(12)]
    rx = {"snr_ref_db": 13.2, "r_ref_m": 300.0, "snr_req_db": 13.2, "fov_az_deg": 60.0, "fov_el_deg": 30.0}
    cfg = TargetConfig.from_dict({"class": "person", "motion": {"mode": "static"}, "criterion_mode": "physical", "radar_rx": rx,
                                  "sensitivity": [{"kind": "visual", "shape": "sector", "radius_max_m": 300},
                                                  {"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 300},
                                                  {"kind": "radar", "shape": "sphere", "radius_max_m": 500}]}, "person-01")
    qt = QuantTarget("person-01", cfg, np.zeros(3), 0.3, 1.75, 0.5, {"uav-02": 7}, 2.1)
    rev = {u.id: ReverseQ("eo", "R", 15.2, 14.0, 0.92, None) for u in us[::2]}
    pairs = quant_pairs(qt, us, _env("day", 45.0, 9000.0), los_fn=lambda a, b, h, need: (True, 0.4), reverse_fn=lambda u, t: rev.get(u))
    fr = quant_frame("person-01", "physical", 123_000_000, pairs)
    soa = msgpack.unpackb(msgpack.packb(encode_soa(fr), use_single_float=True), raw=False)
    return {"schema": "awr.fixture.m19_quant.v1", "source": "tests/perception/test_field_golden.py", "acoustic": ac, "visual": vis, "radar": rad,
            "single_band": [{"rM": r, "expect": 88.0 - 20 * math.log10(r) - 11 - 5.0 * r / 1000.0} for r in (50.0, 100.0, 145.7, 300.0)],
            "p600_shape": [float(x) for x in P600.band_shape_db], "p600_masking": [float(x) for x in P600.masking_db()], "radii": radii,
            "soa": {"topic": soa, "expanded": fr}}


def build_field() -> dict:
    out: dict = {"schema": "awr.fixture.m19_field.v1", "source": "tests/perception/test_field_golden.py", "profiles": [], "volumes": None,
                 "masks": []}
    env = _env("day", 45.0, 8000.0)
    for kind in F.FIELD_KINDS:
        for mid in ("p600_mid360", "awr_f1"):
            sig = signature_of(mid)
            rmax = F.r_max_of(kind, sig, env)
            grid = F.frame_grid(rmax)
            item = {"id": "uav-01", "model_id": mid, "rev": 0, "v_r": F._q_vr(F._values(kind, sig, env, grid), F.V_ENC[kind], F.V_OFF[kind]),
                    "f_el": F._q_fel(F._f_el(kind, sig)), "cut_el_m": F._q_cut(F._cut_el(kind, sig, env), float(grid[-1])),
                    "shell_m": round(F.default_shell_m(kind, sig, env), 1)}
            frame = F.profile_frame(kind, 0, grid, env, [item, {"id": "uav-02", "model_id": mid, "rev": 0, "same": 0}])
            frame["keyframe"] = True
            frame = msgpack.unpackb(msgpack.packb(frame, use_single_float=True), raw=False)
            dirs, rr = F.pattern(256, 1, F.R_MIN_UAV, rmax)
            el = -np.degrees(np.arcsin(dirs[:, 2]))
            val = F.field_value(kind, sig, env, rr, el)
            out["profiles"].append({"kind": kind, "model": mid, "frame": frame, "samples": [[float(r), float(e), _f(v)] for r, e, v in zip(rr, el, val, strict=True)],
                                    "delta": {"t_sim_ns": 1, "kind": kind, "unit": F.UNIT[kind], "combine": F.COMBINE[kind], "keyframe": False,
                                              "v_enc": F.V_ENC[kind], "v_off": F.V_OFF[kind], "uav": [{"id": "uav-01", "rev": 0}, {"id": "uav-02", "rev": 0}]}})
    # 感知体表：两个识别物（人：扇形视觉 + 半球声学带背向衰减；狗），三个机型
    sigs = [signature_of(m) for m in ("awr_f1", "awr_q9", "p600_mid360")]
    cfgs = [TargetConfig.from_dict({"class": "person", "motion": {"mode": "static"}, "criterion_mode": "physical", "alertness": 0.6,
                                    "sensitivity": [{"kind": "visual", "shape": "sector", "radius_max_m": 400, "half_angle_deg": 60},
                                                    {"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 300, "gain_back_db": 10.0}]}, "person-01"),
            TargetConfig.from_dict({"class": "dog", "motion": {"mode": "static"}}, "dog-01")]
    yaw = [0.4, -1.2]
    pos = [np.array([10.0, -20.0, 0.0]), np.array([-50.0, 30.0, 2.0])]
    tg = []
    samples = []
    for k, c in enumerate(cfgs):
        bodies = []
        for i, b in enumerate(c.raw["sensitivity"]):
            tabs, ws = [], []
            for sg in sigs:
                vt = F.volume_table(c.raw, b, sg, env, float(c.raw.get("alertness", 0.5)))
                tabs.append(F._q_table(vt.r_eff_m))
                ws.append(round(vt.w_m, 1))
            bodies.append({"idx": i, "kind": b["kind"], "shape": b["shape"], "mode": c.raw.get("criterion_mode", "geometric"), "r_eff_dm": tabs, "w_m": ws})
            # 显式采样（表模型、P600 = 第 3 个机型），由已量化的表计算（与前端解码同一输入）
            tab = F.expand_table(tabs[2])
            w = ws[2]
            dirs, rr = F.pattern(256, k * 4 + i, F.R_MIN_BODY, float(b["radius_max_m"]))
            P = pos[k][None, :] + dirs * rr[:, None]
            vol = body_volumes([b], pos[k], yaw[k], kinds=(b["kind"],))[0]
            keep = inside_points(vol, P)
            az = np.arctan2(dirs[:, 1], dirs[:, 0]) - yaw[k]
            el = np.arcsin(np.clip(dirs[:, 2], -1, 1))
            re = F.table_r_eff(tab, az, el)
            ref = float(np.max(tab))
            pv = F.volume_p(rr, re, w * re / ref if ref > 0 else w)
            samples.append({"target": c.id, "body": i, "model": 2, "pos": [float(x) for x in pos[k]], "yaw": yaw[k], "cfg": b,
                            "points": [[*(float(x) for x in P[n]), float(pv[n]), bool(keep[n])] for n in range(len(rr))]})
        tg.append({"id": c.id, "rev": 0, "bodies": bodies})
    out["volumes"] = {"frame": {"t_sim_ns": 0, "keyframe": True, "models": [s.model_id for s in sigs], "az_deg": list(F.AZ_DEG), "el_deg": list(F.EL_DEG),
                                "tgt": tg}, "samples": samples}
    # 配置体掩码：10⁴ 个随机点 × 4 种形状
    rng = np.random.default_rng(25)
    shapes = [{"kind": "visual", "shape": "sector", "radius_max_m": 300, "half_angle_deg": 50, "azimuth_offset_deg": 30, "z_min_m": -10, "z_max_m": 120},
              {"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 250},
              {"kind": "visual", "shape": "sphere", "radius_max_m": 200},
              {"kind": "acoustic", "shape": "cylinder", "radius_max_m": 280, "z_min_m": 0, "z_max_m": 80}]
    P = np.column_stack([rng.uniform(-400, 400, 10_000), rng.uniform(-400, 400, 10_000), rng.uniform(-80, 260, 10_000)])
    for b in shapes:
        p0, y0 = np.array([12.0, -7.0, 3.0]), 0.7
        vol = body_volumes([b], p0, y0, kinds=(b["kind"],))[0]
        out["masks"].append({"body": b, "pos": [float(x) for x in p0], "yaw": y0, "inside": [int(x) for x in inside_points(vol, P)]})
    out["mask_points"] = [round(float(x), 3) for x in P.reshape(-1)]
    for m in out["masks"]:      # 取整后的点重算（与前端读取的同一坐标）
        b = m["body"]
        vol = body_volumes([b], np.array(m["pos"]), m["yaw"], kinds=(b["kind"],))[0]
        m["inside"] = [int(x) for x in inside_points(vol, np.array(out["mask_points"]).reshape(-1, 3))]
    return out


def _write_or_check(path: Path, fresh: dict) -> dict:
    if os.environ.get("AWR_REGEN_GOLDEN") == "1" or not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fresh, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def _same(a, b, path="") -> None:
    if isinstance(a, dict):
        assert isinstance(b, dict) and set(a) == set(b), path
        for k in a:
            _same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _same(x, y, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            assert a == b, path
        else:
            assert math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9), (path, a, b)
    else:
        assert a == b, (path, a, b)


@pytest.mark.parametrize("which", ["quant", "field"])
def test_ts_golden_is_current(which):
    fresh = json.loads(json.dumps(build_quant() if which == "quant" else build_field()))
    old = _write_or_check(OUT_Q if which == "quant" else OUT_F, fresh)
    _same(old, fresh)
