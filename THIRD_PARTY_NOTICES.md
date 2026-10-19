# Third-Party Notices

ANet-Drone4D is released under the ANet Open Source License (ANet-Drone4D), a modified Apache License 2.0
([LICENSE](LICENSE)). This file lists what in this repository comes from, or follows, other people's work, and the terms
that apply to it. Those terms are not changed by LICENSE ([LICENSE](LICENSE), condition 3).

- **Section 1, included**: files in this repository that contain third-party material.
- **Section 2, adapted**: code written for this repository that ports an algorithm, policy, format or formula from
  another project and names it in the source file.
- **Section 3, dependencies**: packages installed from npm or PyPI, the D3 event lab container images, components that
  run only as separate containers, and research-only components. They are not part of this repository; production
  builds of the Web sandbox bundle some of them.
- **Section 4, data**: datasets and source models that the tools can download or read. None of them is in this
  repository.

Where a row says "see the original repository", follow the upstream's own terms. To report a missing or wrong
attribution, email hi@anet0.com.

本文件列出仓库中包含或改写的第三方内容及其许可；数据集与源模型不随仓库分发（见第 4 节）。D3 事件沙盘（V0.3）的署名与许可义务
（Sketchfab 源模型 CC BY 4.0、OpenStreetMap ODbL、gz-sim 分叉、FAST-LIVO2 独立容器、nvdiffrast 与 Skyfall-GS 仅限研究镜像）
按 docs/05 §14 执行，并由 `tools/lint/check-release.mjs` 的 REL-05 至 REL-09 校验。

## 1. Included in this repository

| Material | Location | Upstream | License |
|---|---|---|---|
| shadcn/ui components, `base-mira` style: 45 component files installed with the shadcn CLI and patched by the codemods in `tools/shadcn/codemods/` (see `tools/shadcn/PATCHES.md`) | `apps/web/src/ui/components/ui/` | [shadcn-ui/ui](https://github.com/shadcn-ui/ui) at `984f435`; npm `shadcn` 4.21.0 | MIT, Copyright (c) 2023 shadcn |
| shadcn registry mirror (base-mira items, colour tables, `init` payload) for offline installs | `tools/shadcn/registry-mirror/` | shadcn/ui registry | MIT, Copyright (c) 2023 shadcn |
| `shadcn/tailwind.css`, inlined | `apps/web/src/styles/shadcn-tailwind.css` | npm `shadcn` 4.21.0 | MIT, Copyright (c) 2023 shadcn |
| Transitions.dev motion tokens: the duration, distance, scale and easing scale and the 32 recipe variable groups, generated verbatim from `skills/transitions-dev/_root.css`; recipes mapped onto Base UI state attributes | `apps/web/src/styles/motion/tokens.css`, `apps/web/src/styles/motion/base-ui.css`, `apps/web/src/styles/motion/recipes.css`, `apps/web/src/lib/tokens/motion.gen.ts` | [Jakubantalik/transitions.dev](https://github.com/Jakubantalik/transitions.dev) at `e2d5551` | Transitions.dev license, see below |
| P600 airframe mesh, simplified to at most 5,000 triangles and converted to glTF by `tools/vehicles/stl2glb.py` (modified) | `apps/web/public/models/p600.glb` (committed; rebuilt by `make vehicles-models`) | [amov-lab/Prometheus](https://github.com/amov-lab/Prometheus) `Simulator/gazebo_simulator/gazebo_models/uav_models/p600/meshes/p600.stl` at `5dcd8cf` | Apache-2.0 (full text in [LICENSE](LICENSE)) |
| PX4 SIH flight recordings used as golden data (output data, no PX4 source) | `tests/golden/sih_x500_px4-1.18rc1/` | recorded with the `px4io/px4-sitl:v1.18.0-rc1` image of [PX4/PX4-Autopilot](https://github.com/PX4/PX4-Autopilot) | PX4-Autopilot is BSD-3-Clause |
| gz-sim multicopter velocity control, forked for the D3 lockstep physics tier (modified, see the note below) | `native/gz/src/Common.cc`, `native/gz/src/Common.hh`, `native/gz/src/LeeVelocityController.cc`, `native/gz/src/LeeVelocityController.hh`, `native/gz/src/Parameters.hh`, `native/gz/src/Drone4DVelocityControl.cc`, `native/gz/src/Drone4DVelocityControl.hh` | [gazebosim/gz-sim](https://github.com/gazebosim/gz-sim) `src/systems/multicopter_control/` at tag `gz-sim8_8.15.0` | Apache-2.0, Copyright (C) 2019 Open Source Robotics Foundation (full text in [LICENSE](LICENSE)) |
| x500 airframe model for the gz physics export (modified: visuals and sensors removed, landing-gear collisions renamed) | `native/gz/models/x500_d4d/` (upstream license kept in `native/gz/models/x500_d4d/LICENSE`) | `x500_base` of [PX4/PX4-gazebo-models](https://github.com/PX4/PX4-gazebo-models) | BSD-3-Clause, Copyright (c) 2022, Rudis Laboratories |

**Transitions.dev.** Its [terms](https://transitions.dev/terms.html) allow the transitions to be used and modified in
personal and commercial projects and shipped to users as part of a product. They do not allow republishing the
collection, or a substantial part of it, as a transition library, template pack or component kit. If you reuse the
motion files above outside ANet-Drone4D, use them inside a product, not as a collection of their own. The
transitions-dev CLI and the Refine tool are MIT-licensed; neither is included here.

**The P600 low-poly models** (`p600_lowpoly.glb`) are generated procedurally by `tools/vehicles/lowpoly.py` and do not
derive from the Prometheus mesh.

**gz-sim fork.** The seven files in `native/gz/src/` listed above keep the original copyright header of gz-sim
(Copyright (C) 2019 Open Source Robotics Foundation, Apache License 2.0) and state their changes in a "Drone4D
modifications" paragraph under it (Apache License 2.0, Section 4(b)). `Common.cc`, `Common.hh`,
`LeeVelocityController.cc`, `LeeVelocityController.hh` and `Parameters.hh` are copies in which only the namespace
(`multicopter_control` to `d4d_multicopter_control`) and, in `LeeVelocityController.hh`, the include guard are renamed.
`Drone4DVelocityControl.cc` and `Drone4DVelocityControl.hh` derive from `MulticopterVelocityControl.cc` and
`MulticopterVelocityControl.hh`: commands come from the `D4dVelocityCmd` component written by `Drone4DClock` instead of
gz-transport topics, and the `commandFrame` and `init_hover` parameters are added. `Drone4DClock.cc`,
`D4dComponents.hh` and `native/gz/proto/drone4d_gz.proto` are original work of this repository. The header rule is
checked by REL-06 of `tools/lint/check-release.mjs`.

中文：`native/gz/src/` 中由 gz-sim 8.15.0 复制或派生的 7 个文件保留原版权头（Open Source Robotics Foundation，Apache-2.0），
并在版权头下以"Drone4D modifications"段落注明改动；`native/gz/models/x500_d4d/` 派生自 Rudis Laboratories 的 x500_base
（BSD-3-Clause），原许可文件随目录保留。

## 2. Adapted code

| Area | Files | Upstream | Upstream license | What was taken |
|---|---|---|---|---|
| Point cloud fetch cancellation and retry | `apps/web/src/engine/pointcloud/core/StreamPolicy.ts` | [voxelkloud/view](https://github.com/voxelkloud/view) `stream-policy.ts` at `23fbd7f` | MIT, Copyright (c) 2026 Tiago PlanteAI | The policy, ported to this engine |
| Point cloud selection, quality ladder and single-draw point pool | `apps/web/src/engine/pointcloud/core/`, `apps/web/src/engine/pointcloud/render/` | voxelkloud (core, view) | MIT, Copyright (c) 2026 Tiago PlanteAI | Algorithms, reimplemented (`docs/research/n01`, `g02`) |
| GPU eviction policy | `apps/web/src/engine/pointcloud/core/EvictionPolicy.ts` | [Aurtechmx/openlidarviewer](https://github.com/Aurtechmx/openlidarviewer) `src/render/streaming/evictionPolicy.ts` at `75599b4` | AGPL-3.0-only | The policy (evict above 1.5 times the budget, release to 1.15 times, 1 s dwell for visible nodes, class order), reimplemented on typed arrays with a different interface; the upstream file is not reproduced |
| Potree 2.0 container | `python/awr/world/pointcloud/{writer,reader,morton}.py`, `apps/web/src/engine/pointcloud/io/hierarchy.ts`, `apps/web/src/engine/pointcloud/core/NodeStore.ts` | [potree/potree](https://github.com/potree/potree), [potree/PotreeConverter](https://github.com/potree/PotreeConverter) | BSD-2-Clause, Copyright Markus Schütz | File layout (`metadata.json`, `hierarchy.bin`, `octree.bin`), child order and hierarchy chunking, implemented for format compatibility |
| Weather rendering: stateless world-anchored precipitation, fall phase, cloud and weather-map masks | `apps/web/src/engine/environment/` | [Token-Gremlin/natural-disasters](https://github.com/Token-Gremlin/natural-disasters) at `d2bae38`; [SkyeShark/Eanpa-Sky](https://github.com/SkyeShark/Eanpa-Sky) at `a197d3d` | MIT, Copyright (c) 2026 Davi (Token-Gremlin); MIT, Copyright (c) 2026 SkyeShark | Algorithms, ported through the pseudocode in `docs/research/r16-web-weather-fx.md` and rewritten in TypeScript and TSL |
| Wind profile f(z_agl) | `python/awr/environment/wind/profile.py`, `apps/web/src/engine/environment/wind/profile.ts`, `tools/contracts/env_ref.py` | [firelab/windninja](https://github.com/firelab/windninja) `windProfile.cpp` | Public domain (work of the US Government, 17 U.S.C. 105); see the original repository | Profile formula |
| Multicopter control cascade and parameter defaults | `python/awr/sim/fleet/px4lite.py`, `python/awr/sim/fleet/kernels_l1.py`, `python/awr/sim/fleet/params_px4.py` | [PX4/PX4-Autopilot](https://github.com/PX4/PX4-Autopilot) v1.18 | BSD-3-Clause, Copyright (c) 2012 - 2025, PX4 Development Team | Position and attitude control structure, reimplemented in numpy; default parameter values |
| P600 ground-station semantics and airframe geometry | `python/awr/sim/backends/prometheus/`, `vehicles/p600/params.yaml` | [amov-lab/Prometheus](https://github.com/amov-lab/Prometheus) | Apache-2.0 | Control-state enumeration and send guards; rotor positions from `p600.sdf` |
| Chart visual language | `apps/web/src/ui/lf/`, `apps/web/src/styles/lf.css` | [larashero3-dotcom/lieflat-charts](https://github.com/larashero3-dotcom/lieflat-charts) at `eace082` | PolyForm Noncommercial 1.0.0 | The visual language only, no code; see the note below |
| Agent collaboration: TSIR predicates, blackboard, capability registry, receipts | `python/awr/agent/` | [ANetResearch/ANet](https://github.com/ANetResearch/ANet), ANetCore v0.14.0 | Published by Agent Network Research, the producer of ANet-Drone4D | Ported to Python; part of ANet-Drone4D under [LICENSE](LICENSE) |

**lieflat-charts.** The chart components in `apps/web/src/ui/lf/` are an independent React and TypeScript implementation
of the lieflat visual language (hairline marks, rung bars, tick gauges, log-style tables, a streaming canvas line); what
follows lieflat is the look of the figures, not code. No lieflat-charts file, expression or check is included in this
repository. The two parts that used to follow lieflat closely were rewritten as original work in 2026-10 (P4-UI,
ADR-072 in `docs/03-设计基线与决策记录.md`): the deterministic jitter in `apps/web/src/ui/lf/rnd.ts` is now a golden-ratio
Weyl step followed by the MurmurHash3 32-bit finaliser (public domain), and the LF-CHART-01 checks are an AST rule set in
`tools/lint/lf-chart.mjs` (entropy sources, subtree rebuilds and constant element ids) written from AWR-15 §12 and
AWR-18 §13.1 on oxc-parser. lieflat-charts itself is licensed for non-commercial purposes only, under the PolyForm
Noncommercial License 1.0.0 (https://polyformproject.org/licenses/noncommercial/1.0.0); its repository is studied as a
design reference (`docs/research/d01-lieflat-charts.md`) and is not a dependency.

**Published methods** used without third-party code, credited here: eye-dome lighting (Boucheny, 2009); the Dryden
turbulence model (MIL-F-8785C); the PCG hash for GPU particles (Jarzynski and Olano, 2020); the MurmurHash3 32-bit
finaliser (Appleby, public domain); colour vision deficiency simulation (Machado, Oliveira and Fernandes, 2009); the
OKLab colour space (Ottosson, 2020); WCAG 2.x contrast ratios; CAPT concurrent assignment and planning of trajectories (Turpin,
Michael and Kumar, 2014).

The design research behind ANet-Drone4D studied many more projects (`docs/02-refs.md`, `docs/research/00-index.md`).
Those reference repositories are not part of this repository, and their licenses vary, including GPL-3.0, AGPL-3.0 and
non-commercial licenses. The table above lists the adaptations that source files name.

## 3. Dependencies (not included in this repository)

Versions are pinned in `apps/web/package.json`, `package-lock.json`, `pyproject.toml` and `requirements.lock`; the
lock files are the complete list.

**Web sandbox, bundled into production builds**

| Package | Version | License |
|---|---|---|
| three | 0.186.1 | MIT, Copyright (c) 2010-2026 three.js authors |
| react, react-dom | 19.3.0 | MIT |
| @react-three/fiber, @react-three/drei | 9.8.1, 10.7.9 | MIT |
| @base-ui/react (Base UI, the primitives under the shadcn/ui components) | 1.8.0 | MIT, Copyright (c) 2019 Material-UI SAS |
| morphicons (icon morphing) | 1.7.1 | MIT, Copyright (c) 2026 Guillermo |
| lucide (icon geometry, used through `apps/web/src/ui/icons/`; `lucide-react` is not used) | 1.48.0 | ISC, Copyright (c) 2026 Lucide Icons and Contributors; the icons derived from Feather are MIT, Copyright (c) 2013-present Cole Bemis |
| class-variance-authority | 0.7.1 | Apache-2.0 |
| cmdk, react-resizable-panels, cn, tw-animate-css, zustand, camera-controls, stats-gl, @tanstack/react-query, @tanstack/react-table, @tanstack/react-virtual | see lock file | MIT |
| @msgpack/msgpack | 3.1.3 | ISC |
| @fontsource-variable/inter, @fontsource-variable/jetbrains-mono (Inter and JetBrains Mono fonts) | 5.3.0 | SIL Open Font License 1.1 |

**Web development only** (not in production builds): potree-core 2.0.15 (MIT, Copyright (c) Tentone) and
@voxelkloud/react 0.6.0 (MIT, Copyright (c) 2026 Tiago PlanteAI), used only as cross-check oracles in
`apps/web/dev/oracles/`; the build, lint and test tools (Vite, TypeScript, Tailwind CSS, oxlint, Vitest, Playwright,
the shadcn CLI and others) under their own licenses.

**Python runtime** (`pyproject.toml`)

| Package | License |
|---|---|
| fastapi, jsonschema, pydantic, PyYAML, mcap | MIT |
| starlette, uvicorn, websockets, numpy, scipy, zstandard | BSD-3-Clause (numpy also bundles 0BSD, MIT, Zlib and CC0-1.0 parts) |
| numba, laspy | BSD-2-Clause |
| llvmlite | BSD-2-Clause and Apache-2.0 WITH LLVM-exception |
| uvloop | MIT or Apache-2.0 |
| eclipse-zenoh | EPL-2.0 or Apache-2.0 |
| msgpack | Apache-2.0 |
| plyfile | GPL-3.0-or-later |

**Python optional groups**: pyproj (MIT) and cloth-simulation-filter (Apache-2.0) in `geo`; trimesh (MIT) in `tools`;
pytest (MIT), pytest-asyncio (Apache-2.0), httpx (BSD-3-Clause) and pymavlink (LGPL-3.0) in `test`; py7zr
(LGPL-2.1-or-later) in `fetch`; open3d (MIT) and mavsdk (BSD-3-Clause) from V0.2.

Copyleft packages among them are plyfile, pymavlink and py7zr. They are installed separately and are not distributed
with this repository; if you distribute a bundle that contains them, for example a container image, follow their
licenses.

**D3 event lab (V0.3), Python** (`requirements-d3.in`, locked in `requirements-d3.lock`; installed in the internal
container images on the GPU server)

| Package | License |
|---|---|
| open3d-cpu | MIT |
| mcap-protobuf-support, foxglove-schemas-protobuf | MIT |
| rerun-sdk (Rerun SDK and Viewer; telemetry disabled) | MIT or Apache-2.0 |
| pyzmq | BSD-3-Clause; the wheels bundle libzmq (MPL-2.0) |
| pillow | MIT-CMU |
| av (PyAV) | BSD-3-Clause; the wheels bundle FFmpeg (built as LGPL-3.0-or-later) and codec libraries under their own licenses, among them x264 and x265 (GPL-2.0-or-later) |

**D3 container images** (recipes in `tools/deploy/dmax/images/`; the images are used on the internal GPU server and are
not published)

| Image | Component | License |
|---|---|---|
| d3-sensorsvc | moderngl, glcontext (`requirements-gpu.in`) | MIT |
| d3-sensorsvc | nvidia-nvimgcodec-cu12 (nvImageCodec, `requirements-nvjpeg.in`) | Apache-2.0 |
| d3-sensorsvc | nvidia-nvjpeg-cu12, nvidia-cuda-runtime-cu12 (`requirements-nvjpeg.in`) | NVIDIA proprietary license (CUDA Toolkit EULA) |
| d3-sensorsvc | cuda-bindings (`requirements-nvjpeg.in`) | NVIDIA Software License |
| d3-gz | gz-sim 8 (Harmonic) and the gz-* libraries, from the ROS 2 Jazzy vendor packages | Apache-2.0 |
| d3-gz | Ogre-Next, Assimp (gz rendering dependencies) | MIT, BSD-3-Clause |
| d3-gz | ZeroMQ (libzmq), Protocol Buffers, Eigen (linked by `libDrone4DGz.so`) | MPL-2.0, BSD-3-Clause, MPL-2.0 |

**D3 event lab, Node** (`tools/navsvc/package.json`, NavMesh baking and the Detour query service navsvc, ADR-196)

| Package | Version | License |
|---|---|---|
| @recast-navigation/core, @recast-navigation/generators, @recast-navigation/wasm | 0.43.1 | MIT, Copyright (c) 2025 Isaac Mason |
| recastnavigation (Recast and Detour, compiled into the WebAssembly module of @recast-navigation/wasm) | as bundled by 0.43.1 | Zlib, Copyright (c) 2009 Mikko Mononen |

**D3 event lab, Web** (`apps/web/package.json`; loaded on demand by the data inspection panel of the internal workspace
and of the `/lab/` public samples; not loaded by the D2 sandbox)

| Package | Version | License |
|---|---|---|
| @rerun-io/web-viewer (Rerun Web Viewer; the JavaScript loader and the WebAssembly viewer `re_viewer_bg.wasm`, self-hosted; update checks disabled) | 0.38.1 | MIT or Apache-2.0, Copyright (c) 2022 Rerun Technologies AB; the Rust crates compiled into the WebAssembly module under their own licenses |

**Separate containers (not in this repository, not linked)**

| Component | Version | License | Use |
|---|---|---|---|
| [FAST-LIVO2](https://github.com/hku-mars/FAST-LIVO2) (hku-mars), with its dependencies [rpg_vikit](https://github.com/xuankuzcr/rpg_vikit) (xuankuzcr fork) and Sophus | `main@0d2c034`; rpg_vikit `6c886c8`; Sophus `a621ff` | GPL-2.0 (dependencies under their own licenses) | Runs only as a separate container job on internal servers; it reads exported sensor bags and configuration files and writes trajectories and maps as files. The image is used internally only. This repository contains no FAST-LIVO2 source code and no binary linked with it (REL-05). Released trajectories and maps state: "Trajectories and maps generated by FAST-LIVO2 (main@0d2c034)." |

**Research-only components (internal research images only; not in this repository, product images or released
artefacts)**

| Component | Version | License | Use |
|---|---|---|---|
| [nvdiffrast](https://github.com/NVlabs/nvdiffrast) | 0.4.0 | NVIDIA Source Code License (1-Way Commercial); Section 3.3 limits use to research and evaluation | Raster cross-checks in research images (`d3-r3-camera-render`, `d3-sky`). The product camera uses moderngl (EGL/OpenGL). Product image recipes must not reference it (REL-07; image manifest scan in `tools/deploy/dmax/scan_images.sh`). |
| [Skyfall-GS](https://github.com/jayin92/skyfall-gs) | `7319484` | Repository Apache-2.0; the training code derived from Inria 3D Gaussian Splatting (`train.py`, `scene/`, `gaussian_renderer/` and others), `diff-gaussian-rasterization-depth` and `simple-knn` under the Gaussian-Splatting License (non-commercial research and evaluation); the submodules FlowEdit, MoGe and fused-ssim are MIT | Evaluation of the Gaussian appearance layer in the research image `d3-sky` (ADR-186, ADR-187). Only MIT parts may be reused in products; the product 3DGS path uses gsplat (Apache-2.0). |
| FLUX.1 [dev] (Black Forest Labs) | weights | FLUX.1 [dev] Non-Commercial License | Internal research only (ADR-190); never in product images, World Packages, results, public samples, datasets or demos. |

中文：FAST-LIVO2（GPL-2.0）只以独立容器作业使用，镜像只在内部使用，仓库不含其源码与链接产物；nvdiffrast、Skyfall-GS 的
Inria 派生代码与光栅化器、FLUX.1 [dev] 只在内部研究镜像中使用，不进入仓库、产品镜像与任何发布制品。

## 4. Data

### UrbanScene3D

ANet-Drone4D's built-in worlds (Shenzhen, Shanghai, New York, San Francisco, Suzhou, Chicago) are built on your machine
from the sampled virtual-city point clouds of UrbanScene3D. **This repository contains no UrbanScene3D point data and no
World Package, raster or recording built from it.** A few small test fixtures keep metadata of the worlds built from it,
never points: octree node counts and bounding boxes and a 60 s test camera path for three cities
(`apps/web/tests/pointcloud/fixtures/`), and manifest, coordinate and metadata fields of two worlds
(`packages/contracts/fixtures/world/`). `make fetch-data` downloads
`UrbanScene3D-virtual_cities-sampled.7z` from the authors' release
(https://github.com/Linxius/UrbanScene3D/releases/tag/v0.0.1) and checks its sha256; `make worlds` converts it into
`worlds/`, which is never committed. The full dataset is linked from the project page (https://vcc.tech/UrbanScene3D)
and the repository (https://github.com/Linxius/UrbanScene3D).

The authors' terms, quoted from the UrbanScene3D repository:

> UrbanScene3D is publicly accessible for non-commercial uses only. Permission is granted to use the data only if you
> agree:
> - The dataset is provided "AS IS". Despite our best efforts to assure accuracy, we disclaim all liability for any
>   mistakes or omissions;
> - All works that utilize this dataset including any partial use must include a reference to it. Please correctly cite
>   our ECCV22 publication in research papers using the information provided below;
> - You refrain from disseminating this dataset or any altered variations;
> - You are not permitted to utilize this dataset or any derivative work for any commercial endeavors;
> - We reserve all rights that are not explicitly granted to you.

Downloading the data is your decision and your agreement with its authors; the commercial permission in
[LICENSE](LICENSE) does not extend to it. Do not publish World Packages, derived rasters or recordings built from it.
If you use it in research, cite:

```bibtex
@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}
```

中文摘要：UrbanScene3D 仅限非商业使用，禁止再分发原始数据及其任何改动版本（包括由其生成的 World Package、栅格与录制）。
本仓库不包含这些数据的点，也不包含由其生成的 World Package、栅格与录制（少量测试夹具只保留八叉树计数、包围盒、清单字段与一条测试相机路径等元数据）；
`make fetch-data` 从作者发布页下载，`make worlds` 在本机生成，生成物不入库。使用时请引用上面的论文。

**Authorization for ANet-Drone4D deployments.** The ANet-Drone4D project has obtained authorization from the rights
holders of the UrbanScene3D dataset to use World Packages built from it in the project's own deployments (decision
recorded on 2026-10-05, AWR-04 R-D2-28 and ADR-109). Since V0.2-demo the public demo at
https://drone4d.agentnetwork.org.cn serves all six UrbanScene3D cities next to the synthetic city: the deployment
bundle (`tools/deploy/emax/build-local.sh`) carries their World Packages and derived geometry caches to the server,
and the README and product screenshots may show them. The authorization covers the project's own deployments only.
It is not sublicensed by this repository or by [LICENSE](LICENSE): the repository still contains no UrbanScene3D
points and no World Package built from them (`worlds/` and `data/raw/` stay out of git), and if you download the
dataset yourself the authors' terms quoted above apply to you. The ECCV 2022 citation above is kept wherever the
cities are shown (the About page of the application, this file and the README).

中文：UrbanScene3D 经数据集权利方授权使用。ANet-Drone4D 项目已获得 UrbanScene3D 数据集权利方的授权（2026-10-05 记录，
AWR-04 R-D2-28、ADR-109），在本项目自己的部署中使用由六城构建的 World Package：V0.2-demo 起公开演示站
（https://drone4d.agentnetwork.org.cn）与合成城市一并开放六城，部署包附带六城的世界包与派生几何缓存，README 与产品截图
可以使用六城画面。该授权只适用于本项目自己的部署，不经本仓库或 LICENSE 转授：仓库仍不包含任何 UrbanScene3D 点数据与由其
生成的 World Package（`worlds/` 与 `data/raw/` 不入库），你自行下载该数据集时仍须遵守上面引用的作者条款。展示六城的地方
（应用的关于页、本文件与 README）都保留上面的 ECCV 2022 引用。

### D3 GLB worlds: Sketchfab source models (CC BY 4.0)

The D3 event lab builds its World Packages from photogrammetry models published on Sketchfab under the Creative
Commons Attribution 4.0 International license (CC-BY-4.0, https://creativecommons.org/licenses/by/4.0/). The source
models are listed with size and sha256 in `configs/worlds.d3/raw-manifest.json`; they are read from `data/raw/d3-glb/`
and are not in this repository, nor is any World Package built from them.

| World | Model | Author | Source | License |
|---|---|---|---|---|
| `oryahovo-crop1` | "oryahovo - crop #1 - metashape texture problems" | Mitko (https://sketchfab.com/bg3d) | https://sketchfab.com/3d-models/oryahovo-crop-1-metashape-texture-problems-979b09bd0b7744989d5786c2635f24ba | CC-BY-4.0 |
| `smalltown-01` | "An entire small town scanned by a drone" | Mitko (https://sketchfab.com/bg3d) | https://sketchfab.com/3d-models/an-entire-small-town-scanned-by-a-drone-ebdd505832c745e081f03d8e29d17f84 | CC-BY-4.0 |
| `hill-01` (D3.x) | "Anciolina: 3D Hilltop Village Scan, Snowy" | Dok11 (https://sketchfab.com/dok11) | https://sketchfab.com/3d-models/anciolina-3d-hilltop-village-scan-snowy-64ede430838142f5929edfc7eb371efc | CC-BY-4.0 |

The licenses were checked against the Sketchfab model API on 2026-10-18 (ADR-205). The World Packages and everything
derived from them (meshes, tiles, point clouds, rasters, rendered images, event recordings) are adaptations: the models
were modified by metric scaling, levelling, registration and derived layers. Where such material is published (the
`/lab/` pages, public sample slices, datasets, `world_manifest.json`, the offline mode of `drone4d-camera`), it carries
the model title, the author with a link, the source link, the license with a link and the statement
"Modified: metric scaling, levelling and derived layers". The authors do not endorse ANet Drone4D.

### OpenStreetMap (ODbL)

Registration evidence, anchors, levelling planes and road and walkway graphs are derived from OpenStreetMap data,
(c) OpenStreetMap contributors, available under the Open Database License 1.0 (ODbL-1.0,
https://opendatacommons.org/licenses/odbl/1-0/). The extracts (`osm/oryahovo_map.osm` from the OpenStreetMap API 0.6
and `osm/oryahovo_water.osm` from the Overpass API) are listed with sha256 in `configs/worlds.d3/raw-manifest.json`
and recorded in `dataset.derivedFrom[]` of each world. Layers derived from them (anchors, navgraph) are labelled
separately with license ODbL-1.0 and attribution "OpenStreetMap contributors"; a derived database that is published
is shared under the ODbL.

### Elevation data

The D3 worlds near Oryahovo take levelling and `hMsl` from the elevation tile `N43E023.hgt` (1 arc-second), downloaded
from Terrain Tiles on AWS (https://registry.opendata.aws/terrain-tiles/,
`s3.amazonaws.com/elevation-tiles-prod/skadi/N43/N43E023.hgt.gz`, compiled by Mapzen with
[tilezen/joerd](https://github.com/tilezen/joerd)); the file sha256 is recorded in `configs/worlds.d3/raw-manifest.json`.
Attribution required by the data sources (https://github.com/tilezen/joerd/blob/master/docs/attribution.md):

- SRTM terrain data courtesy of the U.S. Geological Survey;
- Europe terrain data produced using Copernicus data and information funded by the European Union - EU-DEM layers.

`hill-01` (D3.x) takes `hMslM` from EU-DEM 25 m (Copernicus) and credits it in the same way.

### MARS-LVIG (internal research only)

The MARS-LVIG dataset (including the HKisland03 sequence, https://mars.hku.hk/dataset.html) is licensed under
CC BY-NC-SA 4.0 for academic use only. It is used only for internal sim-to-real regression on the internal GPU server;
neither the data nor anything derived from it is redistributed, and only statistics and conclusions are cited (ADR-205).
The other public reference sets of `configs/refsets.d3/` are used the same way (docs/05 §6.4.8).

中文：D3 世界包由 Sketchfab 上以 CC BY 4.0 发布的摄影测量模型构建（oryahovo-crop1、smalltown-01 为 Mitko @bg3d，hill-01 为
Dok11 @dok11），源模型与世界包不入库，对外发布的派生制品保留作者、链接与许可，并注明"经修改（米制标定、调平、派生）"；
OpenStreetMap 派生数据（配准证据、anchors、navgraph）单独标注 ODbL-1.0 与"OpenStreetMap contributors"；高程瓦片取自
AWS Terrain Tiles（SRTM 与 EU-DEM）；MARS-LVIG 只作内部回归，不再分发。

## 5. Brand assets

The ANet logo (`apps/web/public/brand/anet-logo.svg`, a byte-identical copy of `docs/media/anet-logo.svg` in
[ANetResearch/ANet](https://github.com/ANetResearch/ANet)) and the Agent Network Research avatar and favicons in
`apps/web/public/brand/` are marks of Agent Network Research, as are their copies in `docs/media/` used by the README. They appear in the user interfaces as
[LICENSE](LICENSE) condition 1b requires; the License grants no trademark rights (Apache License 2.0, Section 6).

## 6. License texts

### MIT License

Applies to the shadcn/ui material (Copyright (c) 2023 shadcn), the voxelkloud adaptations (Copyright (c) 2026 Tiago
PlanteAI), and the natural-disasters (Copyright (c) 2026 Davi (Token-Gremlin)) and Eanpa-Sky (Copyright (c) 2026
SkyeShark) adaptations, each with its own copyright line above.

```text
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### BSD 2-Clause License (Potree, PotreeConverter)

Copyright (c) 2011-2020, Markus Schütz (Potree). Copyright 2020 Markus Schütz (PotreeConverter). All rights reserved.

```text
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
this list of conditions and the following disclaimer in the documentation
and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### BSD 3-Clause License (PX4-Autopilot)

Copyright (c) 2012 - 2025, PX4 Development Team. All rights reserved.

```text
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### Apache License 2.0 (Prometheus, gz-sim)

The full text is reproduced in [LICENSE](LICENSE), after the additional conditions. Prometheus ships no NOTICE file.
Changes made here: the P600 mesh was simplified and converted to glTF, and the ground-station semantics were
reimplemented in Python. gz-sim ships no NOTICE file; the changes to the forked files are stated in each file and in
section 1.

### Licenses referenced by URL

| Upstream | License | Text |
|---|---|---|
| Transitions.dev | Transitions.dev terms | https://transitions.dev/terms.html |
| lieflat-charts | PolyForm Noncommercial License 1.0.0 | https://polyformproject.org/licenses/noncommercial/1.0.0 |
| openlidarviewer | GNU Affero General Public License v3.0 only | https://www.gnu.org/licenses/agpl-3.0.html |
| WindNinja | Public domain statement of the Rocky Mountain Research Station | https://github.com/firelab/windninja/blob/master/LICENSE |
| UrbanScene3D | Dataset terms quoted in section 4; ANet-Drone4D deployments use it under authorization of the rights holders | https://github.com/Linxius/UrbanScene3D |
| Sketchfab source models of the D3 worlds | Creative Commons Attribution 4.0 International (CC-BY-4.0) | https://creativecommons.org/licenses/by/4.0/legalcode |
| OpenStreetMap data | Open Database License 1.0 (ODbL-1.0) | https://opendatacommons.org/licenses/odbl/1-0/ |
| x500_base (PX4-gazebo-models) | BSD 3-Clause License, Copyright (c) 2022, Rudis Laboratories | `native/gz/models/x500_d4d/LICENSE` |
| recastnavigation | zlib License | https://github.com/recastnavigation/recastnavigation/blob/main/License.txt |
| FAST-LIVO2 | GNU General Public License v2.0 | https://www.gnu.org/licenses/old-licenses/gpl-2.0.html |
| nvdiffrast | NVIDIA Source Code License (1-Way Commercial) | https://github.com/NVlabs/nvdiffrast/blob/main/LICENSE.txt |
| Inria 3D Gaussian Splatting code in Skyfall-GS | Gaussian-Splatting License | https://github.com/graphdeco-inria/gaussian-splatting/blob/main/LICENSE.md |
| FLUX.1 [dev] | FLUX.1 [dev] Non-Commercial License | https://huggingface.co/black-forest-labs/FLUX.1-dev/blob/main/LICENSE.md |
| MARS-LVIG | Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International | https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode |
