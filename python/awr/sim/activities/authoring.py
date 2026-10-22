"""One-shot JSON worker for expensive authoring; not a live simulation state writer."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .bindings import load_recipe
from .compiler import audit_recipe, persist_recipe, populate, target_configs
from .designer import design


def life_network(world_id: str):
    root = Path(os.environ.get("AWR_TARGET_LIFE_DIR", "var/target-life"))
    path = root / f"{world_id}.network.json"
    if not path.exists():
        path = root / "network.json"
    doc = json.loads(path.read_text())
    world_root = Path(os.environ.get("AWR_WORLDS_DIR", "worlds"))
    manifest = json.loads((world_root / world_id / "world.json").read_text())
    if (
        doc["world_id"] != world_id
        or doc["world_content_version"] != manifest["contentVersion"]
        or doc["coordinate_sha256"] != manifest["coordinate"]["sha256"]
    ):
        raise ValueError("Network does not match the current scene version and coordinates")
    return doc


def execute(request: dict) -> dict:
    world = request.get("world")
    if not isinstance(world, str) or not re.fullmatch(r"[a-z0-9-]{1,63}", world):
        raise ValueError("Invalid scene identity")
    body = request["body"]
    if request["operation"] == "rehearsal":
        from .rehearsal import rehearsal

        recipe = load_recipe("life", body["sha256"])
        network = life_network(world)
        if any(recipe.get(k) != network.get(k) for k in ("world_id", "world_content_version", "coordinate_sha256")):
            raise ValueError("Rehearsal scene mismatch")
        root = Path(os.environ.get("AWR_TARGET_LIFE_DIR", "var/target-life")) / "rehearsals"
        return rehearsal(recipe, body["sha256"], body["actor_id"], root)
    if request["operation"] == "scene":
        sha = body["sha256"]
        recipe = load_recipe("life", sha)
        if recipe["world_id"] != world:
            raise ValueError("Scene mismatch")
        return {"recipe_sha256": sha, "facilities": recipe["facilities"]}
    if request["operation"] != "preview":
        raise ValueError("Unknown target authoring operation")
    mode = body.get("mode", "seed")
    if mode not in ("seed", "deepseek"):
        raise ValueError("Invalid authoring mode")
    doc = populate(
        life_network(world),
        body.get("seed", 20261022),
        body.get("people", 12),
        body.get("vehicles", 4),
        extras=body.get("extras") or {},
        vehicle_types=body.get("vehicle_types"),
    )
    usage = None
    if mode == "deepseek":
        doc, usage = design(doc, body.get("prompt", ""))
    audit = audit_recipe(doc)
    sha = persist_recipe(doc) if audit["admitted"] else None
    return {
        "seed": doc["seed"],
        "sha256": sha,
        "targets": target_configs(doc, sha) if sha else [],
        "audit": audit,
        "design": doc.get("design"),
        "provider_usage": usage,
    }


def main() -> None:
    try:
        data = sys.stdin.buffer.read(32769)
        if len(data) > 32768:
            raise ValueError("Authoring request exceeds 32 KiB")
        reply = {"ok": True, "result": execute(json.loads(data))}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        reply = {"ok": False, "error": str(exc)[:240]}
    print(json.dumps(reply, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
