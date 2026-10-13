"""M19-AC-024（D2-AC-52、53）：场与读数的线上形式（17 §17.20.6；M19-FR-034、FR-035；M19-NFR-008）。

- 只在有订阅时生成与编码（兴趣下推 `streams.field` 指标码、`streams.quant` 识别物 target_no；无订阅时零生成、零发布）；
- 剖面 1 Hz【墙钟】：首帧与每 5 s 关键帧带数组（同机型以 `same` 引用），其余帧 rev 未变的机体只给 `{id, rev}`；新订阅
  （`streams.fsub` 变化）立即关键帧；感知体表 2 s 检查、变更与 30 s 关键帧；逐对读数 2 Hz【仿真】；
- 三类载荷通过 schema（契约 1.2.0 合入前用 `tests/perception/schemas/` 的草案）；帧大小：剖面 ≤ 1.2 KB（12 架、3 个机型）、
  读数帧 ≤ 1.5 KB；12 架、30 识别物、声学场 + 感知体 + 选中识别物读数 60 s 的每订阅者平均带宽 ≤ 4 KB/s；
- REST SB-73 至 SB-75 与 topic 同值（读数解码后逐位相等；剖面、感知体表为同一缓存状态）；SB-78 显式采样（P1）：点数、
  确定性、识别物锚点只留配置体内的点；参数非法 422（564，契约合入前以 110 回复并在 detail 中给出 564）；识别物不存在 404（543）。
"""

from __future__ import annotations

import itertools
import json
import math
from pathlib import Path
from types import SimpleNamespace

import msgpack
import numpy as np
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from plib import FakeEnv, FakeWorld, PBench

from awr.api.problem import ApiProblem
from awr.api.rest import perception as PR
from awr.api.security import ApiPrincipal
from awr.contracts import bus_keys
from awr.sim.perception import field as F
from awr.sim.perception.quant import expand_soa

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SID = "sb-k3m7q2x7ab"
BASE = f"/api/sandbox/v1/sessions/{SID}"
FIELD_KEY = bus_keys.STATE_PERCEPTION.rsplit("/", 1)[0] + "/field"
QUANT_KEY = bus_keys.STATE_PERCEPTION.rsplit("/", 1)[0] + "/target_quant"


def _validator(name: str):
    from ctlib import registry
    from jsonschema import Draft202012Validator
    from referencing import Resource

    p = ROOT / "packages" / "contracts" / "rt" / "payloads" / f"{name}.schema.json"
    if not p.is_file():
        p = HERE / "schemas" / f"{name}.schema.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    reg = registry().with_resource(d["$id"], Resource.from_contents(d))
    return Draft202012Validator(d, registry=reg)


def _valid(name: str, inst: dict) -> None:
    errs = [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:160]}" for e in _validator(name).iter_errors(inst)]
    assert not errs, errs[:5]


class Scene:
    """12 架（P600、AWR-Q9、AWR-F1 各 4 架）、n 个识别物的台架；场服务的墙钟由 `now` 驱动。"""

    def __init__(self, n_uav: int = 12, n_tgt: int = 30, *, seed: int = 11) -> None:
        from awr.sim.fleet.profiles import ProfileTable

        self.b = PBench(n_uav, FakeWorld(), env=FakeEnv(band="day", bg_dba=40.0, mor_m=8000.0))
        prof = ProfileTable()
        self.b.ctx.profiles = prof
        rng = np.random.default_rng(seed)
        models = ["p600_mid360", "awr_q9", "awr_f1"]
        for i in range(n_uav):
            self.b.S.profile_id[i] = prof.ids.index(models[i % 3])
            self.b.platform(i, pos=(float(rng.uniform(-600, 600)), float(rng.uniform(-600, 600)), float(rng.uniform(40, 120))))
        self.tids = []
        for k in range(n_tgt):
            cls = ("person", "dog", "vehicle")[k % 3]
            self.tids.append(self.b.add({"class": cls, "pos": [float(rng.uniform(-500, 500)), float(rng.uniform(-500, 500)), None],
                                         "motion": {"mode": "static"}, "criterion_mode": "physical" if k % 2 else "geometric"}))
        self.svc = self.b.prt.field
        self.now = 0
        self.svc.clock = lambda: self.now
        self.pub: list[tuple[str, dict[str, bytes]]] = []

        def sink(key: str, raw: bytes) -> None:
            m = msgpack.unpackb(raw, raw=False)
            self.pub.append((key, m["items"]))

        self.svc.pub.sink = sink
        self.b.step(1)

    def streams(self, **st) -> None:
        self.b.ctx.streams = {k: np.asarray(v, np.uint16) for k, v in st.items()}

    def tick(self, seconds: float = 0.1) -> None:
        """推进仿真与墙钟各 `seconds`，每 0.1 s 调用一次场服务（sim-core 慢任务 10 Hz）。"""
        n = round(seconds / 0.1)
        for _ in range(n):
            self.b.step(10)
            self.now += 100_000_000
            self.svc.tick(self.b.ctx)

    def items(self, key: str) -> list[dict[str, bytes]]:
        return [it for k, it in self.pub if k == key]

    def close(self) -> None:
        self.b.close()


@pytest.fixture
def scene():
    s = Scene()
    yield s
    s.close()


def test_no_subscription_no_generation(scene):
    scene.streams()
    scene.tick(3.0)
    assert scene.pub == []
    st = scene.svc.stats
    assert st["profiles"] == st["volumes"] == st["quant"] == 0


def test_profile_topic_keyframes_rev_and_size(scene):
    scene.streams(field=[F.KIND_CODE["acoustic"]])
    scene.tick(0.1)
    fr = msgpack.unpackb(scene.items(FIELD_KEY)[-1]["acoustic"], raw=False)
    _valid("field_profile", fr)
    assert fr["keyframe"] and fr["kind"] == "acoustic" and fr["v_enc"] == "lin" and len(fr["uav"]) == 12
    full = [u for u in fr["uav"] if "v_r" in u]
    refs = [u for u in fr["uav"] if "same" in u]
    assert len(full) == 3 and len(refs) == 9                              # 三个机型各一组数组，其余引用
    raw = scene.items(FIELD_KEY)[-1]["acoustic"]
    assert len(raw) <= 1200, len(raw)                                      # M19-NFR-008：剖面帧 ≤ 1.2 KB（12 架）
    n0 = len(scene.items(FIELD_KEY))
    scene.tick(0.5)
    assert len(scene.items(FIELD_KEY)) == n0                               # 1 Hz：0.5 s 内不再发
    scene.tick(0.6)
    fr2 = msgpack.unpackb(scene.items(FIELD_KEY)[-1]["acoustic"], raw=False)
    _valid("field_profile", fr2)
    assert not fr2["keyframe"] and all(set(u) == {"id", "rev"} for u in fr2["uav"])
    assert len(scene.items(FIELD_KEY)[-1]["acoustic"]) < 500
    scene.tick(4.5)
    assert any(msgpack.unpackb(it["acoustic"], raw=False)["keyframe"] for it in scene.items(FIELD_KEY)[n0 + 1:])   # 5 s 关键帧
    # 新订阅（订阅代数变化）立即关键帧
    k = len(scene.items(FIELD_KEY))
    scene.streams(field=[F.KIND_CODE["acoustic"]], fsub=[7])
    scene.tick(0.1)
    assert len(scene.items(FIELD_KEY)) == k + 1 and msgpack.unpackb(scene.items(FIELD_KEY)[-1]["acoustic"], raw=False)["keyframe"]


@pytest.mark.parametrize("kind", F.FIELD_KINDS)
def test_all_kinds_pass_schema(scene, kind):
    scene.streams(field=[F.KIND_CODE[kind]])
    scene.tick(0.1)
    fr = msgpack.unpackb(scene.items(FIELD_KEY)[-1][kind], raw=False)
    _valid("field_profile", fr)
    assert fr["unit"] == F.UNIT[kind] and fr["v_enc"] == F.V_ENC[kind]
    if kind in ("thermal", "radar"):
        assert fr["ref"]["sensor"] in ("lwir640", "mmw77")


def test_volumes_topic(scene):
    scene.streams(field=[F.KIND_CODE["volumes"]])
    scene.tick(0.1)
    raw = scene.items(FIELD_KEY)[-1]["volumes"]
    fr = msgpack.unpackb(raw, raw=False)
    _valid("field_volumes", fr)
    assert fr["keyframe"] and len(fr["tgt"]) == 30 and fr["models"] == ["awr_f1", "awr_q9", "p600_mid360"]
    assert len(raw) <= 20_000                                              # 关键帧 ≤ 20 KB（30 识别物）
    b0 = fr["tgt"][0]["bodies"][0]
    assert len(b0["r_eff_dm"]) == 3 and len(b0["w_m"]) == 3
    n0 = len(scene.items(FIELD_KEY))
    scene.tick(2.1)
    later = [msgpack.unpackb(it["volumes"], raw=False) for it in scene.items(FIELD_KEY)[n0:] if "volumes" in it]
    assert all(not f["keyframe"] for f in later)                           # 无变化：不发或只发变化的识别物
    assert all(len(f["tgt"]) < 30 for f in later)


def test_quant_topic_soa_and_rest_parity(scene):
    tid = scene.tids[1]
    tno = int(scene.b.trt._by_id(tid).target_no)
    scene.streams(quant=[tno])
    scene.tick(0.1)
    raw = scene.items(QUANT_KEY)[-1][tid]
    m = msgpack.unpackb(raw, raw=False)
    assert m["enc"] == "soa1" and m["n"] == 12
    assert len(raw) <= 1536, len(raw)                                      # 读数帧 ≤ 1.5 KB
    fr = expand_soa(m)
    _valid("target_quant", fr)
    assert fr == scene.svc.last_quant[tid]
    rng = [p["range_m"] for p in fr["pairs"]]
    assert rng == sorted(rng)
    # 2 Hz【仿真】
    n0 = len(scene.items(QUANT_KEY))
    scene.tick(0.3)
    assert len(scene.items(QUANT_KEY)) == n0
    scene.tick(0.3)
    assert len(scene.items(QUANT_KEY)) == n0 + 1
    # REST 同值（同一时刻的计算）
    rest = scene.b.reg.queries["perception/quant"].fn({"op": "perception/quant", "args": {"target": tid}}, scene.b.ctx)
    assert rest["code"] == 0
    again = expand_soa(msgpack.unpackb(msgpack.packb(__import__("awr.sim.perception.quant", fromlist=["encode_soa"]).encode_soa(rest["item"]),
                                                     use_single_float=True), raw=False))
    assert again == rest["item"]


def test_bandwidth_per_subscriber(scene):
    """12 架、30 识别物：声学场 + 感知体 + 选中识别物读数 60 s，每订阅者平均 ≤ 4 KB/s（M19-NFR-008）。"""
    tid = scene.tids[3]
    tno = int(scene.b.trt._by_id(tid).target_no)
    scene.streams(field=[F.KIND_CODE["acoustic"], F.KIND_CODE["volumes"]], quant=[tno])
    for k in range(12):
        scene.b.S.vel[k] = (3.0, 1.0, 0.0)
    tot = 0
    for _s in range(60):
        for k in range(12):
            scene.b.S.pos[k] += scene.b.S.vel[k] * 1.0
        scene.tick(1.0)
    for _key, items in scene.pub:
        tot += sum(len(v) for v in items.values())
    assert tot / 60.0 <= 4096, tot / 60.0


# ---------------------------------------------------------------- REST
class _Bus:
    def __init__(self, b: PBench) -> None:
        self.b = b

    async def call(self, key, msg, timeout=1.5, retries=0, retry_gap=0.0):
        assert key == bus_keys.CTL_QUERY
        spec = self.b.reg.queries.get(msg["op"])
        return {"v": 1, "code": 213} if spec is None else spec.fn(msg, self.b.ctx)


@pytest.fixture
def api():
    s = Scene(4, 3)
    app = FastAPI()

    @app.exception_handler(ApiProblem)
    async def _p(request: Request, exc: ApiProblem):
        return JSONResponse({"code": exc.code, "detail": exc.detail}, status_code=exc.status)

    def principal(request: Request) -> None:
        request.state.principal = ApiPrincipal("p-view", "viewer", "j", 0, "public", SID, "sandbox")

    app.include_router(PR.sandbox_router, prefix="/api/sandbox/v1/sessions/{sid}", dependencies=[Depends(principal)])
    app.state.awr = SimpleNamespace(kind="show", bus=_Bus(s.b), gateway=None)
    with TestClient(app) as c:
        yield SimpleNamespace(s=s, c=c)
    s.close()


def test_rest_quant_field_volumes(api):
    tid = api.s.tids[0]
    r = api.c.get(f"{BASE}/perception/quant", params={"target": tid})
    assert r.status_code == 200
    q = r.json()
    _valid("target_quant", q)
    assert q["target"] == tid and len(q["pairs"]) == 4
    one = api.c.get(f"{BASE}/perception/quant", params={"target": tid, "uav": "uav-02"}).json()
    assert [p["uav"] for p in one["pairs"]] == ["uav-02"]
    assert api.c.get(f"{BASE}/perception/quant", params={"target": "nobody-01"}).status_code == 404
    assert api.c.get(f"{BASE}/perception/quant", params={"target": "Bad Id"}).status_code == 400
    for kind in F.FIELD_KINDS:
        r = api.c.get(f"{BASE}/perception/field/{kind}")
        assert r.status_code == 200
        fr = r.json()
        _valid("field_profile", fr)
        assert fr["keyframe"] and all("v_r" in u and "same" not in u for u in fr["uav"]) and len(fr["uav"]) == 4
    bad = api.c.get(f"{BASE}/perception/field/sonar")
    assert bad.status_code == 422 and (bad.json()["code"] == 564 or bad.json()["detail"]["code"] == 564)
    v = api.c.get(f"{BASE}/perception/volumes").json()
    _valid("field_volumes", v)
    assert v["keyframe"] and len(v["tgt"]) == 3


def test_rest_profile_same_as_topic_state(api):
    s = api.s
    s.streams(field=[F.KIND_CODE["visual"]])
    s.tick(0.1)
    topic = msgpack.unpackb(s.items(FIELD_KEY)[-1]["visual"], raw=False)
    rest = api.c.get(f"{BASE}/perception/field/visual").json()
    by_id = {u["id"]: u for u in rest["uav"]}
    for k, u in enumerate(topic["uav"]):
        src = topic["uav"][u["same"]] if "same" in u else u
        for f in ("v_r", "f_el", "cut_el_m"):
            assert by_id[u["id"]][f] == src[f]
        assert by_id[u["id"]]["rev"] == u["rev"], k
    assert rest["r_m"] == pytest.approx(topic["r_m"], rel=1e-6)


def test_rest_samples_p1(api):
    tid = api.s.tids[0]
    r = api.c.post(f"{BASE}/perception/field/acoustic/samples", json={"anchor": "uav:uav-01", "n": 256})
    assert r.status_code == 200
    d = r.json()
    assert len(d["points"]) == 256 and d["j"] == 0
    assert api.c.post(f"{BASE}/perception/field/acoustic/samples", json={"anchor": "uav:uav-01", "n": 256}).json() == d   # 确定性
    p0 = np.asarray(api.s.b.S.pos[0], float)
    rr = [math.dist(p[:3], p0) for p in d["points"]]
    assert min(rr) >= 2.0 - 1e-3 and max(rr) <= d["r_max_m"] + 1e-3
    vals = [(rv, p[3]) for rv, p in zip(rr, d["points"], strict=True) if p[3] is not None]
    vals.sort()
    assert all(b[1] <= a[1] + 1e-6 for a, b in itertools.pairwise(vals))   # 声学随距离单调不增
    t = api.c.post(f"{BASE}/perception/field/acoustic/samples", json={"anchor": f"target:{tid}:1", "n": 512, "model_id": "p600_mid360"})
    assert t.status_code == 200
    pts = t.json()["points"]
    assert 0 < len(pts) < 512 and all(0.0 <= p[3] <= 1.0 for p in pts)
    for body in ({"anchor": "uav:uav-01", "n": 5000}, {"anchor": "uav:uav-01", "n": 0}, {"anchor": "drone:1", "n": 10},
                 {"anchor": "uav:uav-01", "n": 10, "extra": 1}, {"anchor": f"target:{tid}:1", "n": 10, "model_id": "awr_v1"}):
        r = api.c.post(f"{BASE}/perception/field/acoustic/samples", json=body)
        assert r.status_code == 422, (body, r.json())
        j = r.json()
        assert j["code"] == 564 or j["detail"]["code"] == 564
