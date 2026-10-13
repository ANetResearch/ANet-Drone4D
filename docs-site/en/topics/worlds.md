# Worlds and data

## World Package v1

The World is the core asset of ANet Drone4D: a versioned directory anchored to a place, serving physics and queries (geometry) as well as
display (point cloud). `worldpkg` normalizes a raw point cloud (units, up axis, leveling, north, origin, normals, ground model, height above
ground, classification) and writes it as World Package v1:

```text
worlds/<id>/
├── world.json             # manifest: name, dataset and license, content version, layers, initial camera, statistics and QA summary
├── coordinate.json        # definition of the only metric frame world (ENU at the anchor): anchor, north, scale source, source transform, ground model
├── visual/pointcloud/     # display octree: Potree 2.0 compatible container with the 12-byte ANET_Q16 point encoding
├── geometry/
│   ├── pointcloud/        # normalized source points (for queries and derivation)
│   └── terrain/           # DSM (2 m) and DTM (10 m) rasters
├── semantic/              # semantic class table and zones (zones.geojson, World ENU meters)
├── environment/env.json   # environment defaults: land cover, wind field parameters and similar
└── qa/report.json         # build QA report
```

The geometry query service provides height, height above ground, clearance, line of sight, ray hits and path checks on the World; safety,
planning and "click the ground to go there" all use it. Each world's `contentVersion` (`cv` in the build output) is a content hash used for
caching and recording compatibility. `make validate` runs deep validation and strict JSON Schema validation on all worlds and passes only
with zero errors.

## The seven worlds

| id | World | Points | Extent (east-west × north-south × height, m) | Area | Highest point (above ground) | Source |
|---|---|---|---|---|---|---|
| `synthcity` | ANet Synthetic City | 3,924,500 | 1,200 × 1,200 × 355 | 1.44 km² | 352 m | Procedurally generated, no third-party data |
| `shenzhen` | Shenzhen | 5,000,141 | 1,848 × 1,999 × 391 | 3.69 km² | 374 m | UrbanScene3D |
| `shanghai` | Shanghai | 5,000,202 | 7,736 × 6,211 × 646 | 48.05 km² | 637 m | UrbanScene3D |
| `newyork` | New York | 5,000,065 | 2,928 × 3,167 × 293 | 9.27 km² | 287 m | UrbanScene3D |
| `sanfrancisco` | San Francisco | 5,000,091 | 7,278 × 7,512 × 558 | 54.67 km² | 443 m | UrbanScene3D |
| `suzhou` | Suzhou | 4,999,857 | 4,407 × 687 × 156 | 3.03 km² | 152 m | UrbanScene3D |
| `chicago` | Chicago | 5,000,391 | 4,176 × 8,038 × 451 | 33.56 km² | 445 m | UrbanScene3D |

The raw data of the six cities differ in units, up axis and tilt (San Francisco is 10.15 m per unit and needs a +90° rotation about Z,
Chicago needs scaling by 1000 and leveling by 2.03°, Suzhou is Y up), and ingest normalizes all of them to World ENU. Each of the six cities
builds in about 27 to 35 s on a CPU. All seven worlds use illustrative anchors (`anchor.kind = "synthetic"`): latitude and longitude only
drive the sun position, sky and interface display, never navigation (see {doc}`architecture`).

## ANet Synthetic City

The synthetic demo city is generated from a fixed seed by `python/awr/world/ingest/synthetic.py`: 1.2 km × 1.2 km, about 3.9 million points,
with roads, a river, parks and two 300-meter-class towers. It contains no third-party data and is released under this repository's
license, so its world package, screenshots and recordings can be shared freely. `make demo-world` builds it in about 25 s; without the
six-city data it is the default world with scenario S0, and the shared show of the public instance runs scenario S7 on it.

## UrbanScene3D

The six built-in cities are built from the sampled point clouds of the virtual cities of [UrbanScene3D](https://vcc.tech/UrbanScene3D)
(Lin et al., ECCV 2022).

**Dataset terms.** The authors of UrbanScene3D allow non-commercial use only, prohibit redistribution of the data or any modified version,
and require works that use it to cite the original paper. This repository therefore contains no UrbanScene3D points and no world packages,
rasters or recordings built from them; `worlds/`, `runs/` and `data/raw/` are never committed. A few test fixtures keep only metadata of built
worlds (octree node counts and bounding boxes, manifest fields, one test camera path), without point data. `make fetch-data` downloads
`UrbanScene3D-virtual_cities-sampled.7z` from the authors' [release page](https://github.com/Linxius/UrbanScene3D/releases/tag/v0.0.1) and
verifies each sha256 against `configs/data.yaml`; read and accept the terms before use. The commercial permission in this project's license
does not extend to the dataset; the authors' terms are quoted in section 4 of
[THIRD_PARTY_NOTICES.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/THIRD_PARTY_NOTICES.md).

**Used under authorization of the dataset rights holders.** The ANet Drone4D project has authorization from the rights holders of the
UrbanScene3D dataset to use world packages built from the six cities in the project's own deployments: since V0.2-demo the public instance
offers the six cities together with the synthetic demo city, the deployment bundles include the six-city world packages, and project
screenshots may show the six cities. The authorization is not passed on to users through this repository or its license: the repository
still contains no UrbanScene3D data, and anyone who downloads it remains bound by the authors' terms above.

When using the six cities, please cite:

```bibtex
@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}
```

## Future data sources

| Stage | Current | Next |
|---|---|---|
| Capture | UrbanScene3D sampled point clouds of six cities and the procedurally generated synthetic city | Livox MID-360 capture sessions with RTK (V0.5) |
| Reconstruction | Engine Adapter v2, Recon IR and the job state machine, end to end with a mock engine | GPU reconstruction engines (V0.5) |
| World format | World Package v1 with geometry queries and progressive streaming in the browser | COPC and 3D Tiles export (V0.5); triangle mesh world asset import with metric calibration (D3) |
