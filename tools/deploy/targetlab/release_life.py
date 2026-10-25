"""Audit all demo populations before replacing their templates. Never calls a model.

Example: PYTHONPATH=python python tools/deploy/targetlab/release_life.py NETWORK CASE_DIRECTORY
The case directory contains the two previously paid case-1.json/case-2.json records.
"""

import argparse
import json
from pathlib import Path

from awr.sim.activities.compiler import audit_recipe, persist_recipe, populate, target_configs
from awr.sim.activities.designer import apply_design


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("network", type=Path)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--output", type=Path, default=Path("var/target-life"))
    parser.add_argument("--audit-output", type=Path, default=Path("docs/audit/target-life"))
    args = parser.parse_args()
    network = json.loads(args.network.read_text())
    base = json.loads(Path("scenarios/sandbox/synthcity/t9_scene_life.json").read_text())
    jobs = [
        ("t9_scene_life", "T9 场景生活与交通（实验）", populate(network, 20261022, 24, 6), None),
    ]
    for index, title in ((1, "居民办事购物"), (2, "多点停车与居民生活")):
        case = json.loads((args.cases / f"case-{index}.json").read_text())
        saved_intents = {key: case["design"][key] for key in ("summary", "assignments")}
        doc = apply_design(populate(network, 81, 12, 4), saved_intents)
        jobs.append((f"t{9 + index}_custom_life", f"T{9 + index} DeepSeek 定制 · {title}", doc, case["provider_usage"]))
    jobs.append(
        (
            "t12_life_all_classes",
            "T12 六类目标生活（实验）",
            populate(network, 81, 12, 4, extras={"dog": 2, "uav": 2, "machine": 1, "object": 3}),
            None,
        )
    )
    jobs.append(
        (
            "t13_all_vehicle_types",
            "T13 七种车型与六类目标（实验）",
            populate(
                network,
                81,
                12,
                7,
                extras={"dog": 2, "uav": 2, "machine": 1, "object": 3},
                vehicle_types=["sedan", "suv", "hatchback", "van", "pickup", "truck", "bus"],
            ),
            None,
        )
    )
    staged = []
    for tid, title, doc, usage in jobs:
        audit = audit_recipe(doc)
        print(tid, audit["admitted"], audit["failures"], flush=True)
        if not audit["admitted"]:
            raise SystemExit("Admission failed; no templates were replaced")
        sha = persist_recipe(doc, args.output)
        template = {
            **base,
            "template_id": tid,
            "name_zh": title,
            "seed": doc["seed"],
            "targets": target_configs(doc, sha),
            "summary_zh": doc.get("design", {}).get("summary", title),
        }
        report = {
            "recipe_sha256": sha,
            "world_id": doc["world_id"],
            "world_content_version": doc["world_content_version"],
            "seed": doc["seed"],
            "generator": doc.get("generator"),
            "classes": sorted({a["class"] for a in doc["actors"]}),
            "provider_usage": usage,
            "audit": audit,
            "verification_scope": "offline 1500-second population admission; not visual or real-world validation",
        }
        staged.append((tid, template, report))
    args.audit_output.mkdir(parents=True, exist_ok=True)
    for tid, template, report in staged:
        Path(f"scenarios/sandbox/synthcity/{tid}.json").write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n"
        )
        (args.audit_output / f"{tid}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
