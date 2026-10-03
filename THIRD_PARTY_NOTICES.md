# Third-Party Notices

ANet-Drone4D is released under the ANet Open Source License (ANet-Drone4D), a modified Apache License 2.0
([LICENSE](LICENSE)). This file lists what in this repository comes from, or follows, other people's work, and the terms
that apply to it. Those terms are not changed by LICENSE ([LICENSE](LICENSE), condition 3).

- **Section 1, included**: files in this repository that contain third-party material.
- **Section 2, adapted**: code written for this repository that ports an algorithm, policy, format or formula from
  another project and names it in the source file.
- **Section 3, dependencies**: packages installed from npm or PyPI. They are not part of this repository; production
  builds of the Web sandbox bundle some of them.
- **Section 4, data**: datasets that the tools can download. None of them is in this repository.

Where a row says "see the original repository", follow the upstream's own terms. To report a missing or wrong
attribution, email hi@anet0.com.

本文件列出仓库中包含或改写的第三方内容及其许可；数据集不随仓库分发（见第 4 节）。

## 1. Included in this repository

| Material | Location | Upstream | License |
|---|---|---|---|
| shadcn/ui components, `base-mira` style: 45 component files installed with the shadcn CLI and patched by the codemods in `tools/shadcn/codemods/` (see `tools/shadcn/PATCHES.md`) | `apps/web/src/ui/components/ui/` | [shadcn-ui/ui](https://github.com/shadcn-ui/ui) at `984f435`; npm `shadcn` 4.21.0 | MIT, Copyright (c) 2023 shadcn |
| shadcn registry mirror (base-mira items, colour tables, `init` payload) for offline installs | `tools/shadcn/registry-mirror/` | shadcn/ui registry | MIT, Copyright (c) 2023 shadcn |
| `shadcn/tailwind.css`, inlined | `apps/web/src/styles/shadcn-tailwind.css` | npm `shadcn` 4.21.0 | MIT, Copyright (c) 2023 shadcn |
| Transitions.dev motion tokens: the duration, distance, scale and easing scale and the 32 recipe variable groups, generated verbatim from `skills/transitions-dev/_root.css`; recipes mapped onto Base UI state attributes | `apps/web/src/styles/motion/tokens.css`, `apps/web/src/styles/motion/base-ui.css`, `apps/web/src/styles/motion/recipes.css`, `apps/web/src/lib/tokens/motion.gen.ts` | [Jakubantalik/transitions.dev](https://github.com/Jakubantalik/transitions.dev) at `e2d5551` | Transitions.dev license, see below |
| P600 airframe mesh, simplified to at most 5,000 triangles and converted to glTF by `tools/vehicles/stl2glb.py` (modified) | `apps/web/public/models/p600.glb` (committed; rebuilt by `make vehicles-models`) | [amov-lab/Prometheus](https://github.com/amov-lab/Prometheus) `Simulator/gazebo_simulator/gazebo_models/uav_models/p600/meshes/p600.stl` at `5dcd8cf` | Apache-2.0 (full text in [LICENSE](LICENSE)) |
| PX4 SIH flight recordings used as golden data (output data, no PX4 source) | `tests/golden/sih_x500_px4-1.18rc1/` | recorded with the `px4io/px4-sitl:v1.18.0-rc1` image of [PX4/PX4-Autopilot](https://github.com/PX4/PX4-Autopilot) | PX4-Autopilot is BSD-3-Clause |

**Transitions.dev.** Its [terms](https://transitions.dev/terms.html) allow the transitions to be used and modified in
personal and commercial projects and shipped to users as part of a product. They do not allow republishing the
collection, or a substantial part of it, as a transition library, template pack or component kit. If you reuse the
motion files above outside ANet-Drone4D, use them inside a product, not as a collection of their own. The
transitions-dev CLI and the Refine tool are MIT-licensed; neither is included here.

**The P600 low-poly models** (`p600_lowpoly.glb`) are generated procedurally by `tools/vehicles/lowpoly.py` and do not
derive from the Prometheus mesh.

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

### Apache License 2.0 (Prometheus)

The full text is reproduced in [LICENSE](LICENSE), after the additional conditions. Prometheus ships no NOTICE file.
Changes made here: the P600 mesh was simplified and converted to glTF, and the ground-station semantics were
reimplemented in Python.

### Licenses referenced by URL

| Upstream | License | Text |
|---|---|---|
| Transitions.dev | Transitions.dev terms | https://transitions.dev/terms.html |
| lieflat-charts | PolyForm Noncommercial License 1.0.0 | https://polyformproject.org/licenses/noncommercial/1.0.0 |
| openlidarviewer | GNU Affero General Public License v3.0 only | https://www.gnu.org/licenses/agpl-3.0.html |
| WindNinja | Public domain statement of the Rocky Mountain Research Station | https://github.com/firelab/windninja/blob/master/LICENSE |
| UrbanScene3D | Dataset terms quoted in section 4 | https://github.com/Linxius/UrbanScene3D |
