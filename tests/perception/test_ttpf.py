"""M19-AC-003：TTPF 与锚点。x = 1.75 时 P = 0.900（±0.001）；人 1080P/48 mm R 级平视 857 m、天底 458 m、4K 1412 m、LWIR 100 mm
431 m（±1%）；与独立参考实现 `ref_ttpf.py` 一致；契约夹具 `fixtures/perception/ttpf_cases.json`。"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
import ref_ttpf as REF

from awr.contracts._paths import contracts_root
from awr.sim.perception import gx40, level_of, lwir640, n_req_for, p_prime_um, ttpf
from awr.sim.perception.johnson import level_req_of
from awr.sim.perception.params import N50_PX, N_P90
from awr.sim.targets import critical_dim

PERSON = (0.5, 0.3, 1.75)


def test_ttpf_p90_ratio_and_thresholds():
    assert abs(ttpf(1.75 * 8.0, 8.0) - 0.900) <= 0.001
    assert abs(REF.ratio_at(0.9) - 1.75) < 0.002
    for lv, n in (("D", 3.5), ("R", 14.0), ("I", 22.4)):
        assert N_P90[lv] == n and N50_PX[lv] == REF.n50_px(lv)
        assert abs(ttpf(n, N50_PX[lv]) - 0.9) <= 0.0015


def test_matches_independent_reference_on_random_inputs():
    rng = np.random.default_rng(3)
    for _ in range(500):
        n = float(rng.uniform(0, 60))
        lv = str(rng.choice(["D", "R", "I"]))
        assert abs(ttpf(n, N50_PX[lv]) - REF.ttpf(n, REF.n50_px(lv))) < 1e-12
    a = rng.uniform(0, 60, 64)
    assert np.allclose(ttpf(a, 8.0), [REF.ttpf(x, 8.0) for x in a], atol=1e-12)


def test_contract_fixture():
    cases = json.loads((contracts_root() / "fixtures" / "perception" / "ttpf_cases.json").read_text(encoding="utf-8"))["cases"]
    assert cases
    for c in cases:
        assert abs(ttpf(c["n"], c["n50_px"]) - c["p"]) < 1e-9


@pytest.mark.parametrize(("res", "eps_deg", "want"), [("1080p", 0.0, 857.0), ("1080p", 90.0, 458.0), ("4k", 0.0, 1412.0)])
def test_eo_anchors(res, eps_deg, want):
    d = float(critical_dim(np.array(PERSON), math.radians(eps_deg), 0.0, worst=True))
    r = REF.range_for(d, 48.0, 14.0, p_prime_um(gx40(res), 48.0))
    assert abs(r - want) / want <= 0.01


def test_lwir_anchor():
    d = float(critical_dim(np.array(PERSON), 0.0, 0.0, worst=True))
    sn = lwir640(100.0)
    r = REF.range_for(d, 100.0, 14.0, p_prime_um(sn, 100.0))
    assert abs(r - 431.0) / 431.0 <= 0.01


def test_levels_and_requirements():
    assert level_of(3.49) == 0 and level_of(3.5) == 1 and level_of(14.0) == 2 and level_of(22.4) == 3
    assert list(level_of(np.array([0.0, 4.0, 15.0, 30.0]))) == [0, 1, 2, 3]
    # 优先级：任务覆盖 > 识别物 n_req_px > 识别物 level_req > 缺省 R（M19-FR-015）
    assert n_req_for("I", 30.0, task_level="D") == 3.5
    assert n_req_for("I", 30.0, task_n_px=9.0) == 9.0
    assert n_req_for("I", 30.0) == 30.0
    assert n_req_for("I") == 22.4
    assert n_req_for(None) == 14.0
    assert level_req_of(22.4) == "I" and level_req_of(18.0) == "R" and level_req_of(2.0) == "R"
