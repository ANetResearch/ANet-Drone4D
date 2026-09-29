"""runs REST（M12 §7.3；AWR-17 §4.2 R33–R37、R66–R72、§4.3.9；M12-FR-052、NFR-018；M12-AC-052、AC-026 的服务端部分）。

真实 api 应用（tests/rt 的 GwStack：FakeSim + create_app + uvicorn 线程），`AWR_RUNS_DIR` 指向含合成录制的临时目录：
- 列表字段、兼容性、meta；非法 run 422、不存在 404、路径穿越被拒；
- 段文件下载：已关闭段 immutable，Range 206、越界 416；`.ovw`、`.evx` 同规则；
- keep：operator 持席 200、viewer 403 115；删除：admin 204、operator 403、当前运行 409 105；
- 事件分页：未处于回放打开状态 409 463；
- 书签：viewer 可读不可写（403 115），operator 增改删，标签服务端净化（emoji 与禁用字形被去掉、≤ 64 字符），
  超过 1000 条 409 462，文件符合契约 bookmark 定义。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rt"))

import fakesim
import rechelp
import rtc

pytestmark = pytest.mark.ext


@pytest.fixture(scope="module")
def st(tmp_path_factory: pytest.TempPathFactory):
    import os

    from awr.recorder.synth import synthesize

    runs = tmp_path_factory.mktemp("runsrest")
    old = os.environ.get("AWR_RUNS_DIR")
    os.environ["AWR_RUNS_DIR"] = str(runs)
    s = fakesim.GwStack(n=1)
    cv = (s.ctx.world_info or {}).get("contentVersion") or "synth"
    synthesize(runs / rechelp.RUN, n=4, sim_s=5, content_version=cv)
    synthesize(runs / rechelp.RUN2, n=4, sim_s=3, content_version="other")
    (runs / "not-a-run").mkdir()
    s.runs = runs
    yield s
    s.close()
    if old is None:
        os.environ.pop("AWR_RUNS_DIR", None)
    else:
        os.environ["AWR_RUNS_DIR"] = old


def _h(tok: dict) -> dict:
    return {"authorization": f"Bearer {tok['token']}"}


@pytest.fixture(scope="module")
def toks(st):
    hint = rtc.hint_of("runsrest")
    op = rtc.token(st.base, "operator", hint)
    adm = rtc.token(st.base, "admin", hint, admin_secret=st.settings.admin_password)
    viewer = rtc.token(st.base, "viewer", rtc.hint_of("runsviewer"))
    return {"op": op, "admin": adm, "viewer": viewer}


def test_list_and_meta(st, toks) -> None:
    r = httpx.get(f"{st.base}/api/runs", headers=_h(toks["viewer"]), timeout=10)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["run_id"] for i in items] == [rechelp.RUN2, rechelp.RUN]
    a = items[1]
    assert a["world_id"] == "shenzhen" and a["compatible"] is True and a["current"] is False
    s0 = a["segments"][0]
    assert s0["closed"] and s0["seg"] == 0 and s0["speed_max"] == 20.0 and s0["sidecars"] == {"ovw": True, "evx": True}
    assert s0["epochs"][0]["epoch"] == 1
    assert items[0]["compatible"] is False  # content_version 不同
    m = httpx.get(f"{st.base}/api/runs/{rechelp.RUN}", headers=_h(toks["viewer"]), timeout=10)
    assert m.status_code == 200 and m.json()["run_id"] == rechelp.RUN
    assert httpx.get(f"{st.base}/api/runs/r20990101-000000-ffff", headers=_h(toks["viewer"]), timeout=10).status_code == 404
    bad = httpx.get(f"{st.base}/api/runs/not-a-run", headers=_h(toks["viewer"]), timeout=10)
    assert bad.status_code == 422 and bad.json()["code"] == 110
    for path in ("/api/runs/..%2F..%2Fetc/segments/000.mcap", f"/api/runs/{rechelp.RUN}/segments/..%2Fmeta.json",
                 f"/api/runs/{rechelp.RUN}/segments/0001.mcap", f"/api/runs/{rechelp.RUN}/segments/000.json"):
        assert httpx.get(st.base + path, headers=_h(toks["viewer"]), timeout=10).status_code in (404, 422)
    assert httpx.get(f"{st.base}/api/runs", timeout=10).status_code == 401


def test_segment_files_and_range(st, toks) -> None:
    src = (st.runs / rechelp.RUN / "rec-000.mcap").read_bytes()
    r = httpx.get(f"{st.base}/api/runs/{rechelp.RUN}/segments/000.mcap", headers=_h(toks["viewer"]), timeout=10)
    assert r.status_code == 200 and r.content == src
    assert "immutable" in r.headers["cache-control"] and r.headers["accept-ranges"] == "bytes"
    r = httpx.get(f"{st.base}/api/runs/{rechelp.RUN}/segments/0.mcap", headers=_h(toks["viewer"]) | {"range": "bytes=10-99"},
                  timeout=10)
    assert r.status_code == 206 and r.content == src[10:100] and r.headers["content-range"] == f"bytes 10-99/{len(src)}"
    r = httpx.get(f"{st.base}/api/runs/{rechelp.RUN}/segments/000.mcap", headers=_h(toks["viewer"]) | {"range": f"bytes={len(src)}-"},
                  timeout=10)
    assert r.status_code == 416 and r.json()["code"] == 309
    for ext in ("ovw", "evx"):
        r = httpx.get(f"{st.base}/api/runs/{rechelp.RUN}/segments/000.{ext}", headers=_h(toks["viewer"]), timeout=10)
        assert r.status_code == 200 and r.content == (st.runs / rechelp.RUN / f"rec-000.{ext}").read_bytes()


def test_keep_delete_events(st, toks) -> None:
    r = httpx.post(f"{st.base}/api/runs/{rechelp.RUN2}/keep", json={"keep": True}, headers=_h(toks["op"]), timeout=10)
    assert r.status_code == 200 and r.json()["keep"] is True
    assert json.loads((st.runs / rechelp.RUN2 / "meta.json").read_text())["keep"] is True
    r = httpx.post(f"{st.base}/api/runs/{rechelp.RUN2}/keep", json={"keep": False}, headers=_h(toks["viewer"]), timeout=10)
    assert r.status_code == 403 and r.json()["code"] == 115
    r = httpx.get(f"{st.base}/api/runs/{rechelp.RUN}/events", headers=_h(toks["viewer"]), timeout=10)
    assert r.status_code == 409 and r.json()["code"] == 463
    assert httpx.delete(f"{st.base}/api/runs/{rechelp.RUN2}", headers=_h(toks["op"]), timeout=10).status_code == 403
    r = httpx.delete(f"{st.base}/api/runs/{rechelp.RUN2}", headers=_h(toks["admin"]), timeout=10)
    assert r.status_code == 204 and not (st.runs / rechelp.RUN2).exists()


def test_bookmarks(st, toks) -> None:
    base = f"{st.base}/api/runs/{rechelp.RUN}/bookmarks"
    assert httpx.get(base, headers=_h(toks["viewer"]), timeout=10).json()["items"] == []
    r = httpx.post(base, json={"segment": 0, "t_sim_ns": 1_500_000_000, "label": "x"}, headers=_h(toks["viewer"]), timeout=10)
    assert r.status_code == 403
    label = "front " + chr(0x1F600) + "  arrives " + chr(0x2600) + " " + "y" * 80
    r = httpx.post(base, json={"segment": 0, "t_sim_ns": 1_500_000_000, "label": label}, headers=_h(toks["op"]), timeout=10)
    assert r.status_code == 201
    bid = r.json()["id"]
    lab = r.json()["label"]
    assert chr(0x1F600) not in lab and chr(0x2600) not in lab and len(lab) <= 64 and lab.startswith("front arrives y")
    r = httpx.patch(f"{base}/{bid}", json={"label": "renamed"}, headers=_h(toks["op"]), timeout=10)
    assert r.status_code == 200 and r.json()["label"] == "renamed"
    doc = json.loads((st.runs / rechelp.RUN / "bookmarks.json").read_text())
    assert rechelp.schema_errors("rec/markers.schema.json", doc) == []
    assert httpx.delete(f"{base}/{bid}", headers=_h(toks["op"]), timeout=10).status_code == 204
    assert httpx.delete(f"{base}/{bid}", headers=_h(toks["op"]), timeout=10).status_code == 404
    items = [{"id": f"bm-{i:08x}", "segment": 0, "t_sim_ns": i, "label": "b"} for i in range(1000)]
    (st.runs / rechelp.RUN / "bookmarks.json").write_text(json.dumps({"schema": "awr.run.bookmarks.v1", "items": items}))
    r = httpx.post(base, json={"segment": 0, "t_sim_ns": 1, "label": "z"}, headers=_h(toks["op"]), timeout=10)
    assert r.status_code == 409 and r.json()["code"] == 462
