"""Export source wheel groups separately; appearance hashes and old snapshots stay valid."""

import hashlib
import io
import json
import sys
from pathlib import Path

import numpy as np


def export(source, out):
    catalog = json.loads((out / "catalog.json").read_text())
    for asset in catalog["assets"]:
        if asset["class"] != "vehicle":
            continue
        raw = (source / "compiled" / (asset["id"] + ".npz")).read_bytes()
        if hashlib.sha256(raw).hexdigest() != asset["source_geometry_sha256"]:
            raise ValueError("Wheel source hash mismatch")
        with np.load(io.BytesIO(raw), allow_pickle=False) as data:
            if "wheel_centers" not in data or not len(data["wheel_centers"]):
                continue  # No invented rig for an unsegmented source mesh.
            arrays = {k: data[k].astype("<f4") for k in ("p", "n", "uv", "c")}
            _, first, inverse = np.unique(
                np.concatenate(list(arrays.values()), axis=1), axis=0, return_index=True, return_inverse=True
            )
            ids = data["wheel_ids"][first].astype("<i4")
            if len(first) != asset["vertices"] or not np.array_equal(ids[inverse], data["wheel_ids"]):
                raise ValueError("Mesh deduplication and wheel groups disagree")
            centers = data["wheel_centers"].astype("<f4")
            radii = data["wheel_radii"].astype("<f4")
        blob = ids.tobytes() + centers.tobytes() + radii.tobytes()
        sha = hashlib.sha256(blob).hexdigest()
        (out / f"{sha}.wheels.bin").write_bytes(blob)
        asset["wheels"] = {"sha256": sha, "bytes": len(blob), "count": len(radii), "vertices": len(ids)}
        print(asset["id"], len(radii), flush=True)
    (out / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    export(Path(sys.argv[1]), Path(sys.argv[2]))
