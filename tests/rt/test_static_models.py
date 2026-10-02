"""机型 hero 模型的静态路由（AWR-17 §5.1；M06-FR-035；M11-FR-082；FX-WEB1 任务 4）。

前端按序尝试 `/models/<file>.glb`（构建时随前端复制的副本，`apps/web/public/models/` → `dist/models/`）与
`/vehicles/<model>/model/<file>.glb`（产品路由）。两条路由都只暴露 `*.glb`：200 `model/gltf-binary`、no-cache + ETag、304；
产品路由带 `?v=` 时 immutable。缺失文件与非 glb 名称返回 404 `305`（problem+json），不得落到 SPA 回退的 `index.html`，
否则 GLTFLoader 解析失败、P600 一律退回低模（INT-1 之前的现象）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from awr.api.static import IMMUTABLE, build_static_router, spa_route

GLB = b"glTF" + bytes(60)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "models").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    (dist / "models" / "p600.glb").write_bytes(GLB)
    (dist / "models" / "notes.txt").write_text("x", encoding="utf-8")
    vehicles = tmp_path / "vehicles"
    (vehicles / "p600" / "model").mkdir(parents=True)
    (vehicles / "p600" / "model" / "p600.glb").write_bytes(GLB + b"v")
    worlds = tmp_path / "worlds"
    worlds.mkdir()
    monkeypatch.setenv("AWR_VEHICLES_DIR", str(vehicles))
    app = FastAPI()
    router, _wf = build_static_router(worlds, dist)
    app.include_router(router)
    app.add_api_route("/{path:path}", spa_route(dist), methods=["GET", "HEAD"])
    return TestClient(app)


def test_web_bundled_model(client: TestClient) -> None:
    r = client.get("/models/p600.glb")
    assert r.status_code == 200 and r.content == GLB
    assert r.headers["content-type"] == "model/gltf-binary"
    assert r.headers["Cache-Control"] == "no-cache" and r.headers["ETag"]
    assert client.get("/models/p600.glb", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304
    assert client.head("/models/p600.glb").status_code == 200


@pytest.mark.parametrize("path", ["/models/missing.glb", "/models/notes.txt", "/models/sub/p600.glb"])
def test_web_model_missing_is_not_the_spa(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json") and r.json()["code"] == 305


def test_vehicle_product_route(client: TestClient) -> None:
    r = client.get("/vehicles/p600/model/p600.glb")
    assert r.status_code == 200 and r.content == GLB + b"v"
    assert r.headers["content-type"] == "model/gltf-binary" and r.headers["Cache-Control"] == "no-cache"
    assert client.get("/vehicles/p600/model/p600.glb", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304
    r = client.get("/vehicles/p600/model/p600.glb?v=abc")
    assert r.status_code == 200 and r.headers["Cache-Control"] == IMMUTABLE
    for bad in ("/vehicles/p600/model/missing.glb", "/vehicles/P600/model/p600.glb", "/vehicles/p600/model/params.yaml"):
        r = client.get(bad)
        assert r.status_code == 404 and r.json()["code"] == 305, bad


def test_spa_fallback_still_serves_routes(client: TestClient) -> None:
    r = client.get("/world/shenzhen")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    r = client.get("/models")
    assert r.status_code == 404 and r.json()["code"] == 305
