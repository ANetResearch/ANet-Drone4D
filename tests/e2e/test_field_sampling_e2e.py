"""感知量化读数与场采样接口的真实栈用例（WP-13b，M16；D2-AC-52、D2-AC-53 的服务端与对拍部分；AWR-04 §15.10.3、§15.10.4、
§15.11.4；17 §17.20.6；M19-FR-030 至 FR-035；d2-WP-19b-to-M16 第 2 条）。

真实 supervisor（`sandbox` profile）+ sandbox-pool + sandbox-api，池以真实 sim-core 拉起 T1（两架 P600 与静止的人），只经公开 REST：

| 用例 | 断言 |
|---|---|
| SB-74 剖面 | 四种指标（声学、视觉、热、雷达）形状：32 点对数网格、6 个仰角格、`v_enc`、每架机一项；声学与雷达随距离单调不增；单位与合并方式 |
| SB-78 对拍 | 以本用例独立实现的前端插值规则（add 对 ln r 线性、mul 对 (ln r, ln v) 线性、仰角最近格、截止距离）在剖面上插值，与服务端显式采样的精确场值比较：dB 量 ≤ 0.5 dB、线性量 ≤ 2%（D2-AC-53）；显式采样的点位由独立实现的 Halton 图案复现（锚点一致） |
| SB-75 感知体 | 每个识别物、每个启用的敏感体、会话中的每个机型一组 `r_eff`（8 方位 × 6 仰角）与过渡宽度；识别物锚点的显式采样只落在配置半径内，P 在 [0, 1]，远小于 r_eff 的点 P > 0.5、远大于的点 P < 0.5 |
| SB-73 读数 | ≤ 12 对、斜距升序、§15.10.4 全部字段；`p_ac = 1/(1 + e^(−m/2))`、`n_eq = θ/角分辨率`、`ratio = θ/θ_th`、`p_any = 1 − Π(1 − p_m)`（启用的模态）；`uav` 过滤；判据模式与 `would_discover` 一致（几何模式下不在配置体内即为否） |
| 参数校验 | 未知指标 422（564，契约合入前为 110 + `detail.code = 564`）；n > 4096 422；未知识别物 404（543）|
| 内部身份未配置 | 本栈没有密钥文件：带身份令牌创建 403 `115`（D2-AC-51 的"未配置密钥文件"一项） |
| 实时 topic | `field/acoustic`、`field/volumes`、`target/{id}/quant` 经会话 WS 到达（网关拆分与兴趣下推属 M11、M08，D2-INT 起转发；第 2 轮起未到达即失败） |
"""

from __future__ import annotations

import itertools
import json
import math
import sys
import time

import httpx
import numpy as np
import pytest
from awrproc import ROOT, world_ready
from sbstack import SandboxStack, sdk

sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import identity as idfx

pytestmark = [pytest.mark.needs_data, pytest.mark.slow]

A = sdk()
KINDS = ("acoustic", "visual", "thermal", "radar")
EL_DEG = (-75.0, -45.0, -15.0, 15.0, 45.0, 75.0)
NONE = 0xFFFF
GOLDEN = 2.39996


@pytest.fixture(scope="module")
def sess():
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    st = SandboxStack(max_sessions=2).start()
    s = None
    try:
        s = A.Sandbox(st.base, principal_hint="MSIXFIELDSAMPLEHINTAAAAA", auto_keepalive=False, timeout_s=30.0).create("synthcity", "t1_point_watch")
        s.wait_ready(90)
        s.rt()                                     # 首个连接：时钟播放
        time.sleep(6.0)                            # 机体起飞、感知帧就绪
        yield st, s
    finally:
        if s is not None:
            s.close()
        st.stop()


def _get(s: A.Session, rel: str, **params) -> httpx.Response:
    return httpx.get(s.sb.http.base + s.path(rel), params=params or None, headers={"authorization": "Bearer " + s.token}, timeout=15.0)


def _post(s: A.Session, rel: str, body: dict) -> httpx.Response:
    return httpx.post(s.sb.http.base + s.path(rel), json=body, headers={"authorization": "Bearer " + s.token}, timeout=15.0)


# ---------------------------------------------------------------- 独立实现：解码、插值、图案（与 AWR-04 §15.11.2、§15.11.4 同式）
def _dq(q: int, enc: str, off: float) -> float:
    if q == NONE:
        return math.nan
    return 10.0 ** (q / 4000.0 - 8.0) if enc == "log" else q / 10.0 + off


def _decode(frame: dict, item: dict) -> tuple[list[float], list[float], list[float]]:
    vr = [_dq(int(x), frame["v_enc"], float(frame.get("v_off", 0.0))) for x in item.get("v_r") or []]
    vr += [math.nan] * (len(frame["r_m"]) - len(vr))
    fe = [x / 100.0 for x in item.get("f_el") or [0]]
    fe = fe * 6 if len(fe) == 1 else fe
    cu = [float(x) for x in item.get("cut_el_m") or [0]]
    cu = cu * 6 if len(cu) == 1 else cu
    return vr, fe, cu


def _items(frame: dict) -> list[dict]:
    """展开 `same` 引用（同帧中数组相同的机体引用前一项）。"""
    out: list[dict] = []
    for it in frame["uav"]:
        if "same" in it and "v_r" not in it:
            ref = out[int(it["same"])]
            out.append({**ref, "id": it["id"], "model_id": it.get("model_id", ref.get("model_id"))})
        else:
            out.append(it)
    return out


def _interp(frame: dict, item: dict, r: float, e_deg: float) -> float:
    vr, fe, cu = _decode(frame, item)
    rm = frame["r_m"]
    j = min(range(6), key=lambda k: abs(EL_DEG[k] - e_deg))
    if cu[j] > 0 and r > cu[j]:
        return math.nan
    if r < rm[0] or r > rm[-1]:
        return math.nan
    k = max(0, min(len(rm) - 2, math.floor(math.log(r / rm[0]) / math.log(rm[-1] / rm[0]) * (len(rm) - 1))))
    while k > 0 and rm[k] > r:
        k -= 1
    while k < len(rm) - 2 and rm[k + 1] < r:
        k += 1
    a, b = vr[k], vr[k + 1]
    if not (math.isfinite(a) and math.isfinite(b)):
        return math.nan
    t = (math.log(r) - math.log(rm[k])) / (math.log(rm[k + 1]) - math.log(rm[k]))
    if frame["combine"] == "add":
        return a + (b - a) * t + fe[j]
    if a <= 0 or b <= 0:
        return math.nan
    return math.exp(math.log(a) + (math.log(b) - math.log(a)) * t) * fe[j]


def _halton(i: int, base: int) -> float:
    f, out = 1.0, 0.0
    while i > 0:
        f /= base
        out += f * (i % base)
        i //= base
    return out


def _pattern(n: int, j: int, r_min: float, r_max: float) -> list[tuple[tuple[float, float, float], float]]:
    out = []
    for i in range(1, n + 1):
        h1, h2, h3 = _halton(i, 2), _halton(i, 3), _halton(i, 5)
        z = 1.0 - 2.0 * h2
        phi = 2.0 * math.pi * h3 + j * GOLDEN
        s = math.sqrt(max(0.0, 1.0 - z * z))
        out.append(((s * math.cos(phi), s * math.sin(phi), z), r_min * (r_max / r_min) ** h1))
    return out


# ---------------------------------------------------------------- SB-74
def test_profiles_shape_units_and_monotone(sess) -> None:
    _st, s = sess
    units = {"acoustic": ("dba", "add"), "visual": ("arcmin", "mul"), "thermal": ("px", "mul"), "radar": ("db", "add")}
    for k in KINDS:
        r = _get(s, f"perception/field/{k}")
        assert r.status_code == 200, r.text[:300]
        f = r.json()
        assert f["kind"] == k and (f["unit"], f["combine"]) == units[k]
        assert len(f["r_m"]) == 32 and f["r_m"][0] == pytest.approx(2.0) and list(f["el_deg"]) == list(EL_DEG)
        ratios = np.diff(np.log(f["r_m"]))
        assert np.allclose(ratios, ratios[0], rtol=1e-6), "radial grid not log-uniform"
        items = _items(f)
        assert {it["id"] for it in items} >= {"p600-01", "p600-02"}
        for it in items:
            vr, fe, cu = _decode(f, it)
            assert len(fe) == 6 and len(cu) == 6
            fin = [v for v in vr if math.isfinite(v)]
            assert fin, (k, it["id"])
            if k in ("acoustic", "radar", "visual", "thermal"):
                assert all(b <= a + 1e-6 for a, b in itertools.pairwise(fin)), (k, it["id"], fin[:8])
            assert it.get("shell_m") is None or it["shell_m"] >= 0


# ---------------------------------------------------------------- SB-78 对拍
@pytest.mark.parametrize("kind", KINDS)
def test_front_end_interpolation_matches_explicit_samples(sess, kind: str) -> None:
    _st, s = sess
    for _attempt in range(3):
        f0 = _get(s, f"perception/field/{kind}").json()
        smp = _post(s, f"perception/field/{kind}/samples", {"anchor": "uav:p600-01", "n": 256})
        assert smp.status_code == 200, smp.text[:300]
        d = smp.json()
        f1 = _get(s, f"perception/field/{kind}").json()
        pts = d["points"]
        if not pts:
            pytest.skip(f"{kind}: no drawable samples")
        pat = _pattern(256, int(d.get("j", 0)), float(d["r_min_m"]), float(d["r_max_m"]))
        # 点位由独立实现的图案复现：anchor = p − u·r 对全部点一致
        anchors = np.array([[p[0] - u[0] * rr, p[1] - u[1] * rr, p[2] - u[2] * rr] for p, (u, rr) in zip(pts, pat, strict=False)])
        assert len(pts) <= 256
        assert len(pts) == 256                    # 飞行器锚点不剔除点（不可绘制处的值为 null）
        spread = float(np.max(np.linalg.norm(anchors - anchors.mean(axis=0), axis=1)))
        assert spread <= 0.05, spread
        worst = 0.0
        n_cmp = 0
        for p, (u, rr) in zip(pts, pat, strict=False):
            e = -math.degrees(math.asin(max(-1.0, min(1.0, u[2]))))   # 自采样点（观察者）看飞行器的仰角
            if p[3] is None:
                continue
            want = float(p[3])
            errs = []
            for fr in (f0, f1):
                item = next(it for it in _items(fr) if it["id"] == "p600-01")
                got = _interp(fr, item, rr, e)
                if math.isfinite(got):
                    errs.append(abs(got - want) if fr["combine"] == "add" else abs(got - want) / max(abs(want), 1e-9))
            if errs:
                worst = max(worst, min(errs))
                n_cmp += 1
        tol = 0.5 if f0["combine"] == "add" else 0.02
        if worst <= tol and n_cmp >= len(pts) // 2:
            print(f"[WP-13b] {kind}: {n_cmp} samples, worst {'dB' if tol == 0.5 else 'rel'} error {worst:.4f}")
            return
        time.sleep(1.0)                            # 剖面刷新（1 Hz）与采样之间机体状态变化：重试
    raise AssertionError(f"{kind}: worst error {worst:.4f} > {tol} over {n_cmp} samples")


# ---------------------------------------------------------------- SB-75 与识别物锚点
def _expand(q: list[int]) -> np.ndarray:
    a = np.asarray(q, float) / 10.0
    if a.size == 1:
        return np.full((8, 6), a[0])
    if a.size == 6:
        return np.repeat(a[None, :], 8, axis=0)
    if a.size == 8:
        return np.repeat(a[:, None], 6, axis=1)
    return a.reshape(8, 6)


def _fe_volume_p(tab: np.ndarray, w: float, az_rel: float, el: float, r: float) -> float:
    """前端表模型（AWR-04 §15.11.4 第 2 条；`engine/perception/field.ts` 的 volumeREff 与 volumeP，独立实现）。"""
    a = (math.degrees(az_rel) % 360.0) / 45.0
    i0 = math.floor(a) % 8
    i1 = (i0 + 1) % 8
    t = a - math.floor(a)
    j = min(range(6), key=lambda k: abs(EL_DEG[k] - math.degrees(el)))
    re = tab[i0, j] * (1 - t) + tab[i1, j] * t
    if re <= 0:
        return 0.0
    ref = float(tab.max())
    ww = max(w * re / ref if ref > 0 else w, 1e-6)
    z = max(-700.0, min(700.0, (r - re) * math.log(9.0) / ww))
    return 1.0 / (1.0 + math.exp(z))


def _align(pts: list, pat: list) -> tuple[np.ndarray, list[int]]:
    """配置体外的点被剔除：找出使最多返回点落在 anchor + u·r 上的锚点，返回 (锚点, 每个返回点的图案下标)。"""
    P = np.array([q[:3] for q in pts], float)
    U = np.array([u for u, _r in pat], float) * np.array([r for _u, r in pat], float)[:, None]
    best = (-1, None)
    for k in range(len(pat)):
        a = P[0] - U[k]
        cand = a[None, :] + U                                      # [n, 3]
        d = np.min(np.linalg.norm(P[:, None, :] - cand[None, :, :], axis=2), axis=1)
        hits = int(np.sum(d < 0.01))
        if hits > best[0]:
            best = (hits, a)
            if hits == len(pts):
                break
    a = best[1]
    idx = [int(np.argmin(np.linalg.norm(a[None, :] + U - P[i][None, :], axis=1))) for i in range(len(pts))]
    return a, idx


def test_volumes_table_effective_radius_and_target_anchor_samples(sess) -> None:
    _st, s = sess
    v = _get(s, "perception/volumes")
    assert v.status_code == 200, v.text[:300]
    vol = v.json()
    assert vol["keyframe"] is True and vol["models"] == ["p600_mid360"]
    assert list(vol["az_deg"]) == [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0] and list(vol["el_deg"]) == list(EL_DEG)
    tgt = {t["id"]: t for t in vol["tgt"]}
    assert "person-01" in tgt
    bodies = tgt["person-01"]["bodies"]
    assert bodies and all(b["kind"] in ("visual", "acoustic", "radar") for b in bodies)
    detail = {b["idx"]: b for b in s.targets.state("person-01")["sensitivity"]}
    cfg = next(t for t in s.targets.list()["items"] if t["id"] == "person-01")
    yaw = float(cfg.get("yaw_rad") or 0.0)
    checked = 0
    for b in bodies:
        assert len(b["r_eff_dm"]) == len(vol["models"]) and len(b["w_m"]) == len(vol["models"])
        assert len(b["r_eff_dm"][0]) in (1, 6, 8, 48)
        tab = _expand(b["r_eff_dm"][0])
        w = float(b["w_m"][0])
        # 与 M18 有效半径一致（≤ 1 m）：详情中的 radius_eff_m（截断到配置半径）是表中某一格（参考方向）按配置半径截断后的值；
        # 感知体表本身不截断（AWR-04 §15.11.4 第 2 条，供"物理参考"显示）
        eff = (detail[b["idx"]].get("radius_eff_m") or {}).get("p600_mid360")
        rcfg = float(detail[b["idx"]]["radius_cfg_m"])
        if eff is not None:
            assert float(np.min(np.abs(np.minimum(tab, rcfg) - float(eff)))) <= 1.0, (b["kind"], eff, rcfg, sorted(set(tab.ravel().tolist())))
        smp = _post(s, f"perception/field/{b['kind']}/samples", {"anchor": f"target:person-01:{b['idx']}", "n": 384})
        assert smp.status_code == 200, smp.text[:300]
        d = smp.json()
        pts = d["points"]
        if not pts:
            continue
        assert all(0.0 <= q[3] <= 1.0 for q in pts)
        pat = _pattern(384, int(d["j"]), float(d["r_min_m"]), float(d["r_max_m"]))
        anchor, idx = _align(pts, pat)
        worst = 0.0
        for q, k in zip(pts, idx, strict=True):
            u, r = pat[k]
            assert r <= rcfg + 1e-6                                        # 只落在配置半径内
            az = math.atan2(u[1], u[0]) - yaw
            el = math.asin(max(-1.0, min(1.0, u[2])))                       # 识别物看采样点的仰角
            worst = max(worst, abs(_fe_volume_p(tab, w, az, el, r) - float(q[3])))
        assert worst <= 0.02, (b["kind"], worst)
        print(f"[WP-13b] volume {b['kind']}: {len(pts)} samples inside the body, worst |dP| {worst:.4f}, anchor {np.round(anchor, 2).tolist()}")
        checked += 1
    assert checked >= 1


# ---------------------------------------------------------------- SB-73
def _p_logistic(m: float, s: float = 2.0) -> float:
    return 1.0 / (1.0 + math.exp(-m / s))


def test_quant_readout_fields_and_internal_relations(sess) -> None:
    _st, s = sess
    r = _get(s, "perception/quant", target="person-01")
    assert r.status_code == 200, r.text[:300]
    q = r.json()
    assert q["target"] == "person-01" and q["criterion_mode"] in ("geometric", "physical")
    pairs = q["pairs"]
    assert 1 <= len(pairs) <= 12
    ranges = [p["range_m"] for p in pairs]
    assert ranges == sorted(ranges)
    for p in pairs:
        assert {"uav", "model_id", "range_m", "elev_deg", "az_rel_deg", "los", "acoustic", "visual", "p_any", "criterion_mode", "would_discover",
                "dwell_s", "t_dwell_s", "reverse"} <= set(p) | {"criterion_mode"}
        ac, vi = p["acoustic"], p["visual"]
        assert {"lp_dba", "lp_band_db", "band_hz", "thr_dba", "l_bg_dba", "margin_db", "p", "in_body"} <= set(ac)
        assert {"theta_arcmin", "d_vis_m", "n_eq_px", "theta_th_arcmin", "ratio", "c_app", "light_band", "lights_on", "in_fov", "p", "in_body"} <= set(vi)
        assert ac["p"] == pytest.approx(_p_logistic(ac["margin_db"]), abs=0.011)
        assert vi["n_eq_px"] == pytest.approx(vi["theta_arcmin"] / 1.0, rel=0.011)          # 人：角分辨率 1.0 arcmin
        assert vi["ratio"] == pytest.approx(vi["theta_arcmin"] / vi["theta_th_arcmin"], rel=0.011)
        # 角尺寸 θ = D_vis / r（arcmin）
        assert vi["theta_arcmin"] == pytest.approx(vi["d_vis_m"] / p["range_m"] * 180 * 60 / math.pi, rel=0.011)
        ps = [ac["p"], vi["p"]] + ([p["radar"]["p"]] if p.get("radar") else [])
        assert p["p_any"] == pytest.approx(1 - math.prod(1 - x for x in ps), abs=0.011) or p["p_any"] <= 1 - math.prod(1 - x for x in ps) + 0.011
        if q["criterion_mode"] == "geometric" and not ac["in_body"] and not vi["in_body"]:
            assert p["would_discover"] is False
    one = _get(s, "perception/quant", target="person-01", uav=pairs[0]["uav"]).json()
    assert [p["uav"] for p in one["pairs"]] == [pairs[0]["uav"]]


def test_parameter_validation_and_unknown_target(sess) -> None:
    _st, s = sess
    r = _get(s, "perception/field/sonar")
    assert r.status_code == 422, r.text[:300]
    j = r.json()
    assert j["code"] == 564 or (j["code"] == 110 and (j.get("detail") or {}).get("code") == 564), j
    r = _post(s, "perception/field/acoustic/samples", {"anchor": "uav:p600-01", "n": 5000})
    assert r.status_code == 422, r.text[:300]
    r = _post(s, "perception/field/acoustic/samples", {"anchor": "nowhere", "n": 10})
    assert r.status_code == 422, r.text[:300]
    r = _get(s, "perception/quant", target="nobody-99")
    assert r.status_code == 404 and r.json()["code"] == 543, r.text[:300]


def test_identity_disabled_without_a_secret_file(sess, tmp_path) -> None:
    st, _s = sess
    tok, _ = idfx.issue_token(idfx.secret_file(tmp_path))
    r = httpx.post(st.base + "/api/sandbox/v1/sessions", json={"world": "synthcity", "template": "t6_manual", "principal_hint": "MSIXFIELDNOIDHINTAAAAAAA"},
                   headers={"x-awr-identity": tok}, timeout=30.0)
    assert r.status_code == 403 and r.json()["code"] == 115, r.text[:300]


def test_field_and_quant_topics_over_the_session_websocket(sess) -> None:
    _st, s = sess
    topics = ["field/acoustic", "field/volumes", "target/person-01/quant"]
    got: dict[str, int] = {}
    nbytes: dict[str, list[int]] = {}
    for m in s.stream(topics, seconds=8):
        got[m.topic] = got.get(m.topic, 0) + 1
        nbytes.setdefault(m.topic, []).append(len(json.dumps(m.data, default=str)))
    # forwarded since D2-INT (rt/interest.py, rt/detail.py): no frame is a regression now (WP-13b round 2)
    assert got, "the session gateway forwarded none of field/*, target/*/quant"
    assert got.get("field/acoustic", 0) >= 5 and got.get("target/person-01/quant", 0) >= 10, got
    print(f"[WP-13b] topics {got}, max JSON-equivalent bytes {({k: max(v) for k, v in nbytes.items()})}")
