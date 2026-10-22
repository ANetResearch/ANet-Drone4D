"""Import CC0 GLB target classes using locked source files, preserving source triangles.

The small dog's walk is an authored procedural deformation, not captured animation.
This command merges entries into an existing target asset pack/catalog.
"""

import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

SPECS = [
    ("dog-small", "dog", "小型犬", "Small dog", "small-dog-standing-ce18ca9c", "dog-small.glb", "x"),
    (
        "forklift",
        "machine",
        "电动叉车",
        "Electric forklift",
        "electric-counterbalance-forklift-7acc0547",
        "forklift.glb",
        "-z",
    ),
    (
        "quadcopter",
        "uav",
        "四旋翼无人机",
        "Quadcopter",
        "quadcopter-with-flight-electronics-a25968c1",
        "quadcopter.glb",
        "-z",
    ),
]


def export(source, out):
    out.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((out / "catalog.json").read_text())
    lock = []
    expected = {
        a["id"]: a["sha256"] for a in json.loads(Path(__file__).with_name("extra-assets.lock.json").read_text())
    }
    for key, cls, zh, en, slug, filename, forward in SPECS:
        path = source / filename
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha != expected[key]:
            raise ValueError(f"Source hash mismatch: {key}")
        scene = trimesh.load(path, force="scene")
        parts = []
        images = []
        rotation = (
            np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]])
            if forward == "x"
            else np.array([[0, 0, -1], [1, 0, 0], [0, 1, 0]])
        )
        for node in sorted(scene.graph.nodes_geometry):
            trans, name = scene.graph[node]
            m = scene.geometry[name].copy()
            m.apply_transform(trans)
            mat = m.visual.material
            im = getattr(mat, "baseColorTexture", None)
            factor = getattr(mat, "baseColorFactor", None)
            factor = np.ones(4) if factor is None else np.asarray(factor) / 255
            if im is None:
                im = Image.new("RGBA", (8, 8), tuple((factor * 255).astype("uint8")))
                factor = np.ones(4)
            im = im.convert("RGBA").resize((256, 256))
            tile = len(images)
            images.append(im)
            uv = getattr(m.visual, "uv", None)
            uv = np.zeros((len(m.vertices), 2)) if uv is None else uv.copy()
            parts.append(
                (
                    (m.vertices @ rotation.T)[m.faces].reshape(-1, 3),
                    (m.vertex_normals @ rotation.T)[m.faces].reshape(-1, 3),
                    uv[m.faces].reshape(-1, 2),
                    factor[:3],
                    tile,
                )
            )
        cols = math.ceil(math.sqrt(len(images)))
        rows = math.ceil(len(images) / cols)
        atlas = Image.new("RGBA", (cols * 256, rows * 256))
        ps = []
        ns = []
        uvs = []
        cs = []
        for j, im in enumerate(images):
            atlas.paste(im, ((j % cols) * 256, (j // cols) * 256))
        for p, n, uv, color, tile in parts:
            uv = np.clip(uv, 1 / 512, 1 - 1 / 512)
            uv[:, 0] = (uv[:, 0] + tile % cols) / cols
            uv[:, 1] = (uv[:, 1] + rows - 1 - tile // cols) / rows
            ps.append(p)
            ns.append(n)
            uvs.append(uv)
            cs.append(np.broadcast_to(color, p.shape))
        p = np.concatenate(ps)
        p -= np.r_[(p[:, :2].min(0) + p[:, :2].max(0)) / 2, p[:, 2].min()]
        poses = []
        stride = 0.0
        if cls == "dog":
            # Four leg regions with smooth shoulder weights; feet lift only in swing.
            # Source geometry stays intact; deterministic baked morphs keep all viewers aligned.
            h = np.ptp(p[:, 2])
            front = p[:, 0] > 0
            left = p[:, 1] > 0
            weight = np.clip((h * 0.55 - p[:, 2]) / (h * 0.3), 0, 1)
            stride = 0.24
            for t in np.arange(16) / 16 * 2 * np.pi:
                q = p.copy()
                phase = t + np.where(front == left, 0, np.pi)
                q[:, 0] += np.sin(phase) * stride / 4 * weight
                q[:, 2] += np.maximum(0, np.cos(phase)) * 0.035 * weight
                poses.append(q)
        arrays = {"p": p, "n": np.concatenate(ns), "uv": np.concatenate(uvs), "c": np.concatenate(cs)}
        poses = np.array(poses, dtype="<f4") if poses else np.empty((0, len(p), 3), "<f4")
        joined = np.concatenate([*arrays.values(), *(v for v in poses)], axis=1)
        _, first, inverse = np.unique(joined, axis=0, return_index=True, return_inverse=True)
        blob = bytearray()
        layout = {}
        for k, v in [
            *((k, v[first].astype("<f4")) for k, v in arrays.items()),
            ("poses", poses[:, first]),
            ("indices", inverse.astype("<u4")),
        ]:
            layout[k] = {"offset": len(blob), "count": v.size}
            blob.extend(v.tobytes())
        geom = hashlib.sha256(blob).hexdigest()
        (out / f"{geom}.bin").write_bytes(blob)
        tmp = out / f"{key}.png"
        atlas.save(tmp)
        png = tmp.read_bytes()
        tex = hashlib.sha256(png).hexdigest()
        tmp.rename(out / f"{tex}.png")
        entry = {
            "id": key,
            "class": cls,
            "name_zh": zh,
            "name_en": en,
            "license": "CC0-1.0",
            "source": f"https://www.innerscene.com/tools/library/3d-parts/{slug}",
            "source_geometry_sha256": sha,
            "source_texture_sha256": sha,
            "geometry_sha256": geom,
            "texture_sha256": tex,
            "vertices": len(first),
            "triangles": len(inverse) // 3,
            "poses": len(poses),
            "layout": layout,
            "dimensions_m": np.ptp(p, axis=0).tolist(),
            "stride_m": stride,
            "bytes": len(blob),
            "texture_bytes": len(png),
        }
        catalog["assets"] = [a for a in catalog["assets"] if a["id"] != key] + [entry]
        lock.append(
            {
                "id": key,
                "file": filename,
                "sha256": sha,
                "source": entry["source"],
                "license": "CC0-1.0",
                "animation": "authored 16-pose deformation" if poses.size else "static source",
            }
        )
        print(key, entry["dimensions_m"], entry["triangles"], flush=True)
    (out / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    (out / "extra-assets.lock.json").write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    export(Path(sys.argv[1]), Path(sys.argv[2]))
