"""Export verified ALE meshes to an optional, immutable browser asset pack.

Usage: python tools/deploy/targetlab/export_assets.py ALE/assets OUTPUT_DIRECTORY
No model download or primitive substitution. Coordinates stay ENU (+X forward).
The original triangles/UVs are retained; repeated vertices are indexed losslessly.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import numpy as np

SELECTION = {
    "human-1": ("person", "休闲男装 · 短发", "Casual man · short hair"),
    "human-3": ("person", "休闲女装 · 短发", "Casual woman · short hair"),
    "sedan": ("vehicle", "四门轿车", "Four-door sedan"),
    "suv": ("vehicle", "SUV", "SUV"),
    "van": ("vehicle", "高顶厢式货车", "Cargo van"),
    "pickup": ("vehicle", "双排皮卡", "Crew-cab pickup"),
}
for i in range(2, 13):
    SELECTION.setdefault(f"human-{i}", ("person", f"居民外观 {i:02d}", f"Resident appearance {i:02d}"))
SELECTION.update(
    {
        "hatchback": ("vehicle", "五门掀背车", "Five-door hatchback"),
        "bus": ("vehicle", "城市公交车", "City bus"),
        "truck": ("vehicle", "厢式卡车", "Box truck"),
        "bench": ("object", "公园长椅", "Park bench"),
        "litter-bin": ("object", "垃圾桶", "Litter bin"),
        "hydrant": ("object", "消防栓", "Fire hydrant"),
        "bollard": ("object", "隔离柱", "Bollard"),
        "bicycle-rack": ("object", "自行车架", "Bicycle rack"),
        "parked-bicycle": ("object", "停放自行车", "Parked bicycle"),
    }
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def export(source: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((source / "catalog.json").read_text())
    entries = {a["id"]: a for a in catalog["assets"]}
    result = {"format": "awr.target-assets.v1", "assets": []}
    for key, (cls, zh, en) in SELECTION.items():
        spec = entries[key]
        raw = (source / "compiled" / f"{key}.npz").read_bytes()
        image = (source / "compiled" / f"{key}.png").read_bytes()
        for data, field in ((raw, "npz_sha256"), (image, "png_sha256")):
            if digest(data) != spec[field]:
                raise ValueError(f"Source hash mismatch: {key} {field}")
        with np.load(io.BytesIO(raw), allow_pickle=False) as data:
            arrays = {k: data[k].astype("<f4") for k in ("p", "n", "uv", "c")}
            poses = data["poses"][::4].astype("<f4") if "poses" in data else np.empty((0, len(arrays["p"]), 3), "<f4")
        # Deduplication includes every pose so distinct skin trajectories never merge.
        joined = np.concatenate([*arrays.values(), *(p for p in poses)], axis=1)
        _, first, inverse = np.unique(joined, axis=0, return_index=True, return_inverse=True)
        buffers = bytearray()
        layout = {}
        for name, a in [
            *((k, v[first]) for k, v in arrays.items()),
            ("poses", poses[:, first]),
            ("indices", inverse.astype("<u4")),
        ]:
            layout[name] = {"offset": len(buffers), "count": a.size}
            buffers.extend(a.tobytes())
        blob = bytes(buffers)
        geom_hash = digest(blob)
        (out / f"{geom_hash}.bin").write_bytes(blob)
        # Preserve the source texture without resampling or changing its UV layout.
        png = image
        tex_hash = digest(png)
        (out / f"{tex_hash}.png").write_bytes(png)
        item = {
            "id": key,
            "class": cls,
            "name_zh": zh,
            "name_en": en,
            "license": "CC0-1.0",
            "source": spec.get(
                "source", "https://static.makehumancommunity.org/assets/assetpacks/makehuman_system_assets.html"
            ),
            "source_geometry_sha256": spec["npz_sha256"],
            "source_texture_sha256": spec["png_sha256"],
            "geometry_sha256": geom_hash,
            "texture_sha256": tex_hash,
            "vertices": len(first),
            "triangles": len(inverse) // 3,
            "poses": len(poses),
            "layout": layout,
            "dimensions_m": spec["dimensions_m"],
            "stride_m": spec.get("stride_m", 0),
            "bytes": len(blob),
            "texture_bytes": len(png),
        }
        result["assets"].append(item)
        print(f"{key}: {len(first)} vertices, {len(inverse) // 3} triangles, {len(blob)} bytes", flush=True)
    (out / "catalog.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for name in ("humans.lock.json", "vehicles.lock.json", "README.md"):
        (out / name).write_bytes((source / name).read_bytes())
    return result


if __name__ == "__main__":
    export(Path(sys.argv[1]), Path(sys.argv[2]))
