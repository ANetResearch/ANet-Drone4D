# r17 研究笔记：WindNinja / FastEddy / OpenFOAM-dev / OpenVDB（物理风场）

> 研究单元：r17 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §17–21（Environment Engine、E(x,y,z,t)、风场分级、风场 Web 可视化）、§37（刷新频率）、§41（World Package `environment/wind/`）、§46–47（V0.3 视觉环境 / V0.4 物理环境）
> 仓库快照（shallow clone，位于 `refs/weather/`）：`windninja` @ `17fc5fc`（2026-09-22，186 stars，VERSION 4.0.0）、`FastEddy-model` @ `e0cd2f3`（2026-08-12，129 stars，v5.0.1）、`OpenFOAM-dev` @ `5e2f2ea`（2026-09-25，2247 stars，`WM_PROJECT_VERSION=dev`）、`openvdb` @ `86b5ea9`（2026-09-23，3418 stars，13.1.0 于 2026-09-16 发布，Python 包版本 13.1.1-dev）。
> 本文中的路径都相对各自仓库根目录。本机没有 GPU、没有 OpenFOAM、没有 WindNinja 二进制。结论来自源码精读，外加 6 组 Python 原型实测，所有脚本在 `/data/projs/anet-drone/.cache/research/r17/`：
> - `r17_dsm.py`：UrbanScene3D 点云生成 DSM 和 2.5D 实体掩码
> - `r17_masscons2.py`：WindNinja 质量守恒算法移植到笛卡尔 MAC 网格，用 pyamg 求解
> - `r17_dirinterp.py`：比较两种方向插值误差
> - `r17_turb.py`、`r17_turb_box.py`：Dryden 滤波器验证，冻结 von Kármán 湍流盒
> - `r17_query_bench.py`、`r17_windfield_api.py`：WindField 查询接口与性能
> - `r17_vdb_io.py`：pyopenvdb 10.0.1 的读写，从 Ubuntu deb 解包到本地，没有做全局安装
>
> 凡是估算都标注"估算"。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **firelab/windninja** 4.0.0 | 复杂地形诊断风模型。输入 DEM + 来向风速（或站点 / NWP），输出地面以上某高度的风矢量栅格（ASCII/SHP/KMZ/GeoTIFF），可选输出 3D VTK。包含两套求解器：质量守恒（自研 FEM）和动量（包装 OpenFOAM 11，RNG k-ε） | **port**：把质量守恒变分法移植成笛卡尔体素版 Level 2 求解器（本文已实现并实测）；廓线、粗糙度表、风向约定、DSM 转 STL 一并移植。**adopt**：山区真实场景可在 Docker 里直接调用 CLI | L0/L1 参数：V0.2；L2：V0.3–V0.4；CLI：V0.5+ | 5/5（对 MVP 价值最高） |
| **OpenFOAM/OpenFOAM-dev** | 通用 CFD。本项目只用它离线算 RANS 城市风场库（Level 3） | **adopt**：作为离线工具，在 Docker 中运行，建议锁定 OpenFOAM 13 发行版。**port**：`turbineSiting` / `windAroundBuildings` 两个教程的算例模板、ABL 边界条件公式、`boxUniform` 规则网格采样 | V0.6–V1.0 | 4/5 |
| **AcademySoftwareFoundation/openvdb** 13.1 | 稀疏体数据格式与工具库（含 NanoVDB 和 `vdb_tool`） | **adopt（可选工具）**：`.vdb` 仅作交换/导出格式（Houdini/Blender/ParaView）；`vdb_tool points2ls→ls2mesh` 用于点云→水密网格（供 snappyHexMesh 和碰撞 SDF 使用）。**port**：`BoxSampler` 三线性插值、`VelocityIntegrator` RK4 流线积分。**不作为运行时格式** | 导出：V0.6+；点云 SDF：V0.5+ | 3/5 |
| **NCAR/FastEddy-model** 5.0.1 | 常驻 GPU 的可压 LES（CUDA/HIP + MPI），支持建筑解析（URBAN 扩展） | **reference**：用于理解 LES 定位、建筑拖曳项和虚拟塔输出；MVP 不用。本机无 GPU，无法运行 | V1.0+（Level 4 LES，可选） | 2/5 |

**实现者先读这 10 条（每条都有源码或实测依据）：**

1. **Level 2 不需要等 OpenFOAM。** WindNinja 的质量守恒法（`src/ninja/ninja.cpp` 的 `discretize` / `setBoundaryConditions` / `solve` / `computeUVWField`）只是一个带权 Poisson 投影。我已把它移植到笛卡尔 MAC 网格（体素掩码直接来自 UrbanScene3D 点云），用 `scipy.sparse` + `pyamg` 求解，**不需要 GPU**：
   - San Francisco，4 m 网格，1.34M 格：AMG 建立 17–32 s；每个风向 9 次 CG 迭代，7–12 s；峰值内存 0.85 GB。
   - Shenzhen，8 m 网格，4.64M 格：AMG 建立 104 s；每个风向 10 次迭代，约 30 s。
   - 求解后散度 ≤ 3e-6。
2. **原设计里"方向 × 风速"的库可以砍掉一维。** 质量守恒解对输入风速严格线性，而且 **û(θ+180°) = −û(θ)**（实测 max 误差 1.9e-6）。因此 Level 2 只需算 **180° 范围内 6 个扇区**（30° 步长），存成归一化场（参考风速 1 m/s），运行时乘以风速即可。Level 3（RANS）是非线性的，需要 360°；但高 Re 时近似与 Re 无关，风速维度 1–3 档就够。
3. **方向插值必须用"随流对齐（flow-aligned）"混合，不能直接对矢量做 lerp。** 做法是把每个扇区的场分解到各自来流坐标系的 (∥, ⊥, w) 分量上，混合分量后再旋转回目标方向。SF 实测（10 m/s，平均矢量误差）：

   | 扇区步长 | 直接 lerp | flow-aligned |
   |---|---|---|
   | 15° | 0.141 m/s | 0.005 m/s |
   | 30° | 0.562 m/s | 0.019 m/s |
   | 60° | 2.21 m/s | 0.071 m/s |

   直接 lerp 还会带来系统性的风速偏低。
4. **VDB 不适合做运行时格式。** 风场是稠密的，SF 活跃体素占 93%，稀疏树几乎省不了空间。同一场每方向的实测体积：

   | 格式 | 体积 |
   |---|---|
   | dense f32 | 16.1 MB |
   | dense f16 | 8.0 MB |
   | f16 + zstd-3 | 6.4 MB（解压 45 ms） |
   | VDB blosc f32 | 12.7 MB |
   | VDB half | 6.6 MB |

   运行时用 **dense f16 + zstd**，外加 JSON manifest；VDB 只做导出。
5. **给前端的可视化网格用 2× 降采样的 RGBA16F Data3DTexture**（u, v, w, 实体率）。SF 为 1.34 MB，gzip 后 0.80 MB。在 WebGL2 核心规范里 half-float 可以线性过滤，float32 不行（需要扩展），所以用 half-float。
6. **Level 1 湍流有两种实现，各有用途。**
   - 每个 agent 一条 **Dryden**（MIL-F-8785C 低空）滤波器，用于飞控扰动。已实测：σ_v、σ_w 与目标误差 ≤ 1%，σ_u 偏低约 6%，这是积分时间尺度长导致的样本误差。
   - 全场共享的**冻结 von Kármán 湍流盒**（FFT 谱合成，64³ × 4 m 生成只要 0.25 s，1.6 MB），用于多机空间相关和前端粒子一致性。
   - 盒子必须用**修正波数** sin(kΔ)/Δ 做无散投影。否则中心差分散度与梯度之比为 0.55；修正后为 1e-7。
7. **Taylor 冻结假设的速度 V 必须取相对空速，并且设下限。** 多旋翼悬停时对地速度 ≈ 0，Dryden 的时间常数 L/V 会发散。V 取 |U_mean − v_drone|，并 clamp 到 ≥ 0.5 m/s。
8. **OpenFOAM-dev 已经没有 `simpleFoam`。** `bin/simpleFoam` 只是一个提示脚本，会转去执行 `foamRun -solver incompressibleFluid`。ABL 的 U、k、ε 边界条件都继承自 `inletOutlet`（`src/atmosphericModels/derivedFvPatchFields/*`），所以**四个侧面可以合并成一个 patch，网格只划一次，扫描风向时只改 `atmosphericBoundaryLayerProperties` 里的 `flowDir`**。`snappyHexMeshConfig -cylindricalBackground` 可以自动生成对风向旋转不变的圆柱背景网格。`sampledSets::boxUniform` 可以直接把结果采样到规则 3D 网格。
9. **质量守恒模型没有分离和回流。** 它本质上是"势流修正"。建筑背风区的风速会被高估；楼角的 w 在 α_v=1 时会出现 ±10 m/s（SF）乃至 ±34 m/s（深圳 381 m 塔）的尖峰。建议：
   - 城市场景用 α_v ≈ 1.5–2。实测 α_v=2 时 |w|p99 从 1.35 降到 0.86，max 从 10.3 降到 5.9。
   - 可选叠加 WindNinja `flowSeparation` 式的"背风阴影"尾流修正。
   - 需要安全级精度时，升级到 Level 3。
10. **查询性能完全够用。** numpy 三线性查询约 0.2–0.3 ms/次（N ≤ 100 个点）。WindField 完整栈（10 架机、100 Hz）：L0 0.04 ms/tick，L1 0.35 ms/tick，L2 1.07 ms/tick。风场采样频率取 50–100 Hz，然后零阶保持给 100–1000 Hz 的物理步长。**不要每个物理步都查询。**

---

## 1. 仓库概览

| 项 | WindNinja | FastEddy | OpenFOAM-dev | OpenVDB |
|---|---|---|---|---|
| 维护方 | USFS Missoula Fire Lab | NSF NCAR RAL | OpenFOAM Foundation（CFD Direct） | ASWF |
| 语言 / 规模 | C++，`src/ninja/` 约 67.5k 行（`ninja.cpp` 5709、`ninjafoam.cpp` 4851） | C + CUDA；`SRC/HYDRO_CORE/hydro_core.c` 3619 行，外加各 `*.cu` | C++，17.7k 个文件 | C++17；`openvdb/`、`nanovdb/`、`openvdb_cmd/` |
| 依赖 | Boost、GDAL 3.x（NetCDF/PROJ/GEOS/CURL）、NetCDF、Qt6（GUI 可关）、OpenMP；动量求解需要 OpenFOAM 11 | CUDA（或 HIP）、MPI、netCDF | 自带 wmake、Scotch 等；Foundation 提供 apt 源和 Docker | TBB、Blosc、zlib、Boost（可选）、nanobind（Python） |
| 构建 | CMake。`-DNINJA_GUI=OFF -DNINJAFOAM=ON`；`Dockerfile` 基于 ubuntu 20.04 + openfoam8（已过时）；`scripts/build_deps_ubuntu_2404.sh` 用 openfoam11 | `SRC/FEMAIN/Makefile`（NCAR HPC）、`Makefile.hip`；`make WITH_URBAN=1` | `./Allwmake`（数小时），建议直接用官方 Docker 或 apt `openfoam13` | CMake。`pyproject.toml` 用 scikit-build-core；Python 包**不在 PyPI 上**。Ubuntu noble 仓库有 `python3-openvdb 10.0.1`（模块名 `pyopenvdb`） |
| GPU | 不需要 | **必须**（NVIDIA 或 AMD） | 不需要（CPU MPI） | 可选（NanoVDB CUDA） |
| 2026 活跃度 | 4.0.0：Qt6、C API、OpenFOAM 11、RNG k-ε、LANDFIRE 2024 | 5.0（2026-05）、5.0.1（2026-08） | 最后提交 2026-09-25，持续开发 | 13.1.0（2026-09-16）：SIMD、NanoVDB、`vdb_tool` 扩展 |
| 本机可运行 | 否（缺 GDAL/Boost dev；无 root） | 否（无 GPU） | 否（未安装） | **是**：pyopenvdb 10.0.1 放在本地 venv 中，用 numpy<2 |

---

## 2. 源码结构与关键模块

### 2.1 WindNinja

**主流程** `ninja::simulate_wind()`（`src/ninja/ninja.cpp` L245）依次执行：

1. `readInputFile`、`set_position`、`checkInputs`
2. `mesh.buildStandardMesh`
3. `initializationFactory::makeInitialization(input)->initializeFields(u0, v0, w0)`
4. `checkForNullRun`
5. `discretize()`：组装刚度矩阵 SK
6. `setBoundaryConditions()`
7. `solve()`：PCG；失败则 `solveMinres()`
8. `computeUVWField()`
9. `matched()`：站点匹配外循环，最多 `nMaxMatchingIters` 次，默认 150
10. `prepareOutput`、`writeOutputFiles`

| 模块 / 函数 | 要点 |
|---|---|
| `Mesh::buildStandardMesh`（`mesh.cpp` L210） | 地形跟随的六面体结构网格：节点位于 DEM 格心，`XORD = j·res + 0.5·res`，i=0 是南边。垂向按几何增长分层：`get_z = (H−elev)·(g^(k−n+1) − g^(1−n))/(1 − g^(1−n)) + elev`，g=`vertGrowth`=1.3，CLI 默认 20 层（`cli.cpp` L2070）。`compute_domain_height`：首层高度 = res/maxAspectRatio(400)；域顶 ≥ 3×(风高 + 植被高) + DEM 最大值。`compute_cellsize`：coarse/medium/fine 分别对应**水平 4000/10000/20000 格**。这个分辨率很粗，城市场景必须显式设 `mesh_resolution` |
| `discretize()`（L1508） | 控制方程写在注释里：`∂x(Rx ∂xΦ)+∂y(Ry ∂yΦ)+∂z(Rz ∂zΦ)+H=0`，`Rx=Ry=1/(2αH²)`，`Rz=1/(2αV²)`，`H=∇·u0`。8 节点三线性六面体 FEM，27 点模板；矩阵对称，CRS 只存上三角（`NZND` 公式）。α_V 场由 `Stability` 给出（`stabilityFlag`、`alpha_stability`）；中性时 α=1 |
| `setBoundaryConditions()`（L1955） | 侧面四个面和顶面是"流通边界"，取 **Φ=0**（Dirichlet，按"置 1 对角、清零行列"处理）。地面不单独处理，即自然边界 ∂Φ/∂n=0，法向无通量 |
| `solve()`（L676） | 共轭梯度 + **SSOR 预条件**（`Preconditioner`，失败时退回 Jacobi，再失败换 MINRES）。停止条件是残差 2 范数 < `stop_tol=1e-1`，而且是**绝对值**；`MAXITS=100000` |
| `computeUVWField()`（L2041） | 在高斯点求 ∇Φ，再用 "stress smoothing"（Thompson）按反距离加权平均到节点：`u=u0+∂xΦ/(2αH²)`，`w=w0+∂zΦ/(2αV²)` |
| `domainAverageInitialization`（`domainAverageInitialization.cpp`） | 风向从地理北转换到投影北：`inputDirection_proj = wrap0to360(dir_geog − dem.getAngleFromNorth())`。中性边界层 `u* = 0.4·U/ln((z_in + h_veg − d)/z0)`，`bl_height = 0.2·u*/f`，其中 `f = 1.4544e-4·sin(lat)` |
| `windProfile::getWindSpeed`（`windProfile.cpp` L51） | 四种廓线：uniform；logarithmic `U·ln((AGL−d)/z0)/ln(z_in/z0)`；power law；Monin–Obukhov（稳定度函数 van Ulden & Holtslag）。低于 7·z0 时线性插值；高于 ABL 顶取常值 |
| `ninja::set_uniVegetation`（L3831） | 粗糙度表：grass z0=0.01 m（h=0，d=0）；brush z0=0.43 m（h=2.3，d=1.8）；trees z0=1.0 m（h=15.4，d=12.0） |
| `wind_sd_to_uv`（`ninjaMathUtility.h` L163） | 风向用气象约定，**来向**，从北顺时针；u 指东，v 指北 |
| 输出 | `writeAsciiOutputFiles`（L2798）写 `*_vel.asc` 和 `*_ang.asc`（AAIGRID，指定输出高度处的 2D 场），还可写 `ascii_out_uv`、`ascii_out_json`。**3D 场只在 `write_vtk_output=true` 时由 `volVTK.cpp` 写出**：ASCII `STRUCTURED_GRID`，POINTS 是投影坐标加 ASL 高度，`VECTORS wind_vectors` 是 u,v,w |
| CLI | `src/ninja/cli.cpp` L246–373，必需参数：`elevation_file`（asc/lcp/tif/img）、`initialization_method`（`domainAverageInitialization` / `pointInitialization` / `wxModelInitialization`）、`input_speed`+`_units`、`input_direction`、`input_wind_height`、`output_wind_height`、`vegetation`、`mesh_choice` 或 `mesh_resolution`、`write_*_output`。还有 `momentum_flag` + `number_of_iterations`（默认 300）、`non_neutral_stability`、`alpha_stability`、`input_points_file` / `output_points_file`（按点输出） |
| C API | `src/ninja/windninja.h`：`NinjaInitializeArmy`、`NinjaMakeDomainAverageArmy(speedList, directionList, …)`（**天然支持批量风向×风速**）、`NinjaSetInMemoryDem`、`NinjaStartRuns`、`NinjaGetOutputSpeedGrid/DirectionGrid` |
| 动量求解（`ninjafoam.cpp`） | `GenerateNewCase`（L4431）：`WriteFoamFiles` → `writeBlockMesh` → `NinjaElevationToStl`（`stl_create.cpp`，DEM 转 STL）→ `writeMoveDynamicMesh` → `MoveDynamicMesh`（把 block 网格投影到地形上，**不用 snappy**）→ `RefineSurfaceLayer` → `RenumberMesh`；随后 `SimpleFoam()` 调用 `foamRun`，最后 `Sample`。`SetInlets`（L939）按风向象限把 1–2 个侧面设为 inlet；`ComputeDirection`（L972）把来向转成去向单位矢量 |
| OpenFOAM 11 模板 | `data/ninjafoam/11/`：`momentumTransport` 用 **RNGkEpsilon**（Cmu 0.085，C1 0.92，C2 1.68，σk=σε=0.7179）；`fvSolution` 中 p 用 GAMG，U/k/ε 用 smoothSolver GaussSeidel，SIMPLE residualControl 1e-5；`fvSchemes` 中 `div(phi,U)` 为 `bounded Gauss linearUpwind grad(U)`，k/ε 用 upwind |
| 自定义边界条件 | `src/ninjafoam/11/inletBC/logProfile*`：`u* = U·0.41/ln(z_in/z0)`，`U(z) = u*/0.41·ln(AGL/z0)`，`k = u*²/√Cmu`，`ε = u*³/(κ(AGL+z0))` |
| `flowSeparation.cpp` | 沿风向做"阴影投射"扫描 DEM，给定分离角 φ，标记背风分离区（separated=1）。可以直接移植成城市背风尾流掩码 |

### 2.2 FastEddy（仅定位）

- 主程序 `SRC/FEMAIN/FastEddy.c`：`fempi_LaunchMPI` → `parameters_init` → `gridInit` / `hydro_coreInit` / `timeInit` → `cuda_*Init` → 时间推进批次（`NtBatch`）。
- 数值方法：可压方程，RK3（Wicker–Skamarock 2002），3/5/6 阶混合平流（`advectionSelector`）；Smagorinsky 或 1.5 阶 TKE 亚格子（`turbulenceSelector`、`TKESelector`）；MOST 近地层（`surflayerSelector`）；入流用 cell perturbation（`cellpert*`）。
- **URBAN 扩展**（`SRC/EXTENSIONS/URBAN/CUDA/cuda_urbanDevice.cu` 中的 `cudaDevice_UrbanDragMethod`）：建筑用体素掩码加拖曳项 `F_u = −c_d·δ·|u|·u·mask` 表示；温度和密度向基态松弛，`−c_t(θ−θ_b)`，并设限幅因子 1.25。这是一种"浸没体"近似。
- 案例 `tutorials/examples/Example10_REALCASE_Dallas_urban.in`：896×898×58 格，Δx=5 m，dt=0.01 s，180k 步，16 路 GPU 分解。
- 输出：netCDF，维度 `time/zIndex/yIndex/xIndex`，变量 `u v w theta TKE_0 xPos yPos zPos`（`IO/io_netcdf.c`）。"虚拟塔"（`towerIOSelector`，`HYDRO_CORE/CUDA/cuda_towersDevice.cu`）每个时间步写出一条完整垂直廓线，可以用来**标定 Dryden/von Kármán 参数**，或回放真实阵风时间序列。
- 定位：一次运行就是 GPU 小时到 GPU 天，只能离线生成"时变阵风数据集"，和 MVP 的交互式环境不在一个量级。

### 2.3 OpenFOAM-dev（Level 3 相关部分）

| 位置 | 要点 |
|---|---|
| `tutorials/incompressibleFluid/windAroundBuildings/` | 建筑 OBJ + `surfaceFeatures` → `blockMesh` → `snappyHexMesh` → `foamRun`。背景网格 350×280×140 m，划 25×20×10；建筑面加密 level 3，加密盒 level 2。入口 U 为 `fixedValue`，出口 `pressureInletOutletVelocity`，p 用 `totalPressure`，k/ε 用 inletOutlet。这是最小的城市算例 |
| `tutorials/incompressibleFluid/turbineSiting/` | **真实地形 ABL 算例**：`constant/atmosphericBoundaryLayerProperties`（flowDir、zDir、Uref=10、Zref=20、z0、zGround）；internalField 和入/出口都用 `atmosphericBoundaryLayerVelocity/TurbulentKineticEnergy/TurbulentEpsilon`（`libatmosphericModels.so`）；地形壁面 `nutAtmosphericBoundaryLayerWallFunction`；top/sides 为 `slip`；`kEpsilon` 的 σε=1.11（Hargreaves & Wright 2007）；Allrun 为 `blockMesh`、`decomposePar`、`snappyHexMesh`、`createZones`、`foamRun`、`reconstructPar` |
| `src/atmosphericModels/atmosphericBoundaryLayer/atmosphericBoundaryLayer.H` | `U = (U*/κ)·ln((z−zg+z0)/z0)`，`k = U*²/√Cμ`，`ε = U*³/(κ(z−zg+z0))`，`U* = κ·Uref/ln((Zref+z0)/z0)`。κ=0.41，Cμ=0.09 |
| `src/atmosphericModels/derivedFvPatchFields/*` | U、k、ε 三个 ABL 边界条件都是 `public inletOutletFv…`，出流时自动变成零梯度。这是"单一侧面 patch 扫描全风向"的关键 |
| `applications/utilities/preProcessing/snappyHexMeshConfig` | 从 `constant/geometry/*.stl|obj` 自动写出 `blockMeshDict`、`surfaceFeaturesDict`、`snappyHexMeshDict`。常用参数：`-bounds`、`-nCells`、`-refinementLevel`、`-refinementBoxes`、`-refinementDists`、`-layers`、`-insidePoint`、`-xMinPatch…`，以及 **`-cylindricalBackground`**（绕 z 轴的圆柱背景网格） |
| `applications/utilities/preProcessing/setAtmBoundaryLayer` | 用 ABL 模型初始化整个域的 U、k、ε，可加快收敛 |
| `src/sampling/sampledSet/boxUniform/boxUniform.C` | 在 box 内按 `nPoints (nx ny nz)` 均匀采样。落在网格外的点（建筑内部）会被跳过；`samplingSegments = i + j·nx + k·nx·ny`，所以可以反推 (i,j,k)。配合 `type sets; interpolationScheme cellPoint; setFormat raw|csv|vtk;` 使用 |
| `bin/simpleFoam` | 只是提示脚本："superseded… `foamRun -solver incompressibleFluid`"。**旧教程和 WindNinja 的 2.2.0/9 模板都不能直接照搬** |
| `applications/solvers/potentialFoam` | 势流解，和 α_h=α_v 时的质量守恒法等价，可以作为 Level 2 的"OpenFOAM 同网格版本" |
| `etc/caseDicts/functions/fields/` | `writeCellCentres`、`turbulenceFields`（k→I）、`writeVTK` 等函数对象 |

### 2.4 OpenVDB / NanoVDB

- **树结构**：`openvdb/openvdb/openvdb.h` 中 `FloatTree = tree::Tree4<float,5,4,3>`。含义是：Root（哈希表）→ Internal 32³ → Internal 16³ → Leaf 8³ 体素，一片叶子覆盖 8³，第一级内部节点覆盖 128³，第二级覆盖 4096³。`Vec3SGrid` 存 `Vec3f`，`HalfTree` 存 half。
- **文件头**（`io/Archive.cc` 的 `writeHeader`，L812）：
  - int64 magic：`0x56444220`（" BDV"）
  - uint32 文件版本：当前 224（实测 pyopenvdb 10 为 224）
  - lib major/minor
  - hasGridOffsets 标志
  - 16 字节 UUID
  - 之后是 grid descriptors 和按叶压缩的数据：`COMPRESS_ZIP=1`、`COMPRESS_ACTIVE_MASK=2`、`COMPRESS_BLOSC=4`（`io/Compression.h`）。
  - 默认写出 "blosc + active values"；`grid.saveFloatAsHalf=True` 时以 half 存盘。
- **Python**：
  - v12.0.0 起改用 nanobind，模块名从 `pyopenvdb` 改为 `openvdb`（CHANGES L990–992）。
  - API 在 `openvdb/openvdb/python/pyGrid.h` 与 `pyOpenVDBModule.cc`：`Vec3SGrid()`、`copyFromArray(arr, ijk, tolerance)`、`copyToArray`、`getConstAccessor().getValue(ijk)`、`transform = createLinearTransform(voxelSize|matrix)`、`grid['key']=value`（元数据）、`vectorType`、`read/readAll/write(path, grids=[...])`。
  - 测试用例：`python/test/TestOpenVDB.py`。
- **工具**：
  - `tools/Interpolation.h`：`BoxSampler::trilinearInterpolation`（L714），`GridSampler`，`DualGridSampler`，`StaggeredBoxSampler`（MAC 网格）。
  - `tools/VelocityFields.h`：`VelocitySampler`，`VelocityIntegrator::rungeKutta<Order>`（RK1–4），用于流线和粒子积分。
  - `tools/VolumeAdvect.h`：体积平流。
- **`openvdb_cmd/vdb_tool`**：
  - `-read points.ply -points2ls -dilate -gauss -erode -ls2mesh -write surface.stl`：点云→水密网格
  - `mesh2ls`：网格→窄带 SDF
  - 读写格式支持 ply、stl、obj、vdb、abc、gltf 等（README L101–102、L743–746）
- **NanoVDB**：
  - `nanovdb/nanovdb/NanoVDB.h` 是线性化、无指针的只读树。
  - `PNanoVDB.h`（3604 行）是可移植头文件，支持 C/HLSL/GLSL，依赖 SSBO 形式的 `uint` buffer。**理论上可以移植到 WGSL，但 WebGL2 没有 SSBO**。
  - Python（`nanovdb/python/`）：`readGrid`、`writeGrid`（`.nvdb`，可选 codec），`createNanoGrid`，`PySampleFromVoxels`（nearest、trilinear 采样器）。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 统一坐标与风向约定（先定死，否则全链路出错）

| 约定 | 定义 | 来源 |
|---|---|---|
| 世界坐标 | ENU 米制：x=东，y=北，z=上。z 以场景基准地面为 0（写进 `coordinate.json` 的 `ground_z`） | 设计 §7 Geographic |
| 风向 | **气象来向** `dir_from`，单位度，从北顺时针。270 表示西风，吹向东 | WindNinja `wind_sd_to_uv` |
| 去向单位矢量 | `e(θ) = (−sin θ, −cos θ)`。OpenFOAM 的 `flowDir` 使用去向 | WindNinja `NinjaFoam::ComputeDirection` |
| 横向单位矢量 | `n(θ) = (−e_y, e_x)`，即 e 逆时针旋转 90° | 本文约定 |
| three.js | Y-up：`three(x,y,z) = (E, U, −N)` | r11 |
| 体数据轴序 | numpy `(nz, ny, nx, C)` C-order 等价于 Data3DTexture 的 `(depth, height, width)`，x 变化最快 | 本文 |
| 格心 / 节点 | 我们的场以格心为准：`p = origin + (i+0.5)·Δ`。**VDB 的 index 坐标就是体素中心，写 VDB 时 transform 要平移 +0.5Δ** | 实测 `indexToWorld((100,90,10)) = (400,360,40)` |

### 3.2 Level 0：常值风 + 垂直廓线

"常值风"在飞行高度上不能真的取常值：参考风速 8 m/s@10 m 到了 40 m 会变成 11.7 m/s（实测 L0 输出）。

```text
U(z) = U_ref · f(z)                                      # z：离基准地面高度
  log:     f = ln((z−d)/z0) / ln((z_ref−d)/z0),  z ≤ d+z0 时 f=0（或线性过渡）
  power:   f = (z/z_ref)^α,  α 取 0.14 开阔 / 0.22 郊区 / 0.30–0.40 城区（工程常用值）
  uniform: f = 1
W_mean(x,y,z) = U(z) · (e_x(θ), e_y(θ), w_const)
```

| 下垫面（预设） | z0 / m | d / m | 来源 |
|---|---|---|---|
| 草地 grass | 0.01 | 0 | WindNinja `set_uniVegetation` |
| 灌木 brush | 0.43 | 1.8 | 同上 |
| 林地 trees | 1.0 | 12.0 | 同上 |
| 城区 urban | 0.5–1.0 | 约 0.7·h̄_bldg | 文献经验值（Grimmond & Oke）；Level 2/3 已显式解析建筑，此时 d 取 0 |

### 3.3 Level 1：阵风 + 湍流 + 垂直风

**(a) 离散阵风（1−cos）。** 有两种形式：

- MIL-F-8785C 的"爬升–保持"：`v = V_m/2·(1−cos(π x/d_m))`（0≤x≤d_m），x > d_m 时保持 V_m。
- CS-25 的"全波"事件式：在 0≤x≤2d_m 内同样取 `V_m/2·(1−cos(π x/d_m))`，结束后回到 0。

x 是飞行器穿越阵风走过的距离，`x = ∫V dt`。事件按 Poisson 过程触发，速率 λ（例如 1/20 s），幅值取 `V_m·U(0.5,1)`，默认 d_m=60 m。

**(b) Dryden（MIL-F-8785C 低空，h < 1000 ft）**：每个 agent 各有独立状态，已向量化。

| 量 | 公式（h 以 ft 计，clamp ≥ 10 ft） |
|---|---|
| 尺度 | `L_w = h`；`L_u = L_v = h/(0.177+0.000823h)^1.2` |
| 强度 | `σ_w = 0.1·W20`；`σ_u = σ_v = σ_w/(0.177+0.000823h)^0.4` |
| W20（20 ft 风速） | light 15 kt = 7.7 m/s；moderate 30 kt = 15.4 m/s；severe 45 kt = 23.1 m/s |
| 纵向 u | `H_u = σ_u·√(2L_u/(πV)) · 1/(1+(L_u/V)s)` |
| 横向 v / 垂向 w | `H = σ·√(L/(πV)) · (1+√3(L/V)s)/(1+(L/V)s)²` |

离散化用闭式解，不依赖 scipy：

```python
# u：精确的 OU 过程，方差严格为 σ_u²
a = exp(-V*dt/Lu);  xu = a*xu + σu*sqrt(1-a*a)*N(0,1)
# v、w：A=[[0,1],[-1/T²,-2/T]] 的精确 ZOH，T=L/V，K=σ√(L/(πV))，输入 η~N(0,π/dt)（单边 PSD=1 rad/s）
e = exp(-dt/T)
x1' = e*((1+dt/T)*x1 + dt*x2)          + T²*(1 - e*(1+dt/T))*η
x2' = e*(-(dt/T²)*x1 + (1-dt/T)*x2)    + dt*e*η
y   = (K/T²)*(x1' + √3·T·x2')
# V = max(|U_mean − v_vehicle|, 0.5)   ← Taylor 冻结假设用相对空速；悬停时退化为平均风速
# 输出 (u',v',w') 在平均风坐标系（纵向 e、横向 n、竖直）中，最后旋转回 ENU
```

实测（`r17_turb.py`，每组 3000 s）：

| h / m | V / m·s⁻¹ | W20 | dt | σ 目标 (u,v,w) | σ 实测 | 纵向积分时间 T_int（理论 L/V） |
|---|---|---|---|---|---|---|
| 50 | 8 | 7.7 | 0.01 | 1.23 / 1.23 / 0.77 | 1.16 / 1.24 / 0.77 | 20.6 s（25.3） |
| 20 | 3 | 15.4 | 0.01 | 2.77 / 2.77 / 1.54 | 2.56 / 2.75 / 1.54 | 27.0 s（38.7） |
| 120 | 12 | 7.7 | 0.01 | 1.02 / 1.02 / 0.77 | 0.96 / 1.03 / 0.76 | 19.2 s（22.9） |

σ_u 偏低 5–8% 来自长相关时间下的样本误差，每组只有约 120 个独立样本。dt=0.02 时 σ_v 偏高约 10%，所以**推荐 dt ≤ 0.01 s，或者 v/w 也改用 Van Loan 精确噪声协方差**。单 agent 用纯 Python 实现约 60 µs/步，**必须按 agent 批量向量化**（见 `r17_windfield_api.py` 的 `WindField.dryden`）。

**(c) von Kármán**（MIL-HDBK-1797 有理近似，与 MATLAB Aerospace Blockset 同形，写成 8785C 尺度记法；**本机未做数值验证**，实现时用上面的方差检验法复核）：

```text
H_u = σ_u√(2L_u/(πV)) · (1+0.25(L_u/V)s) / (1+1.357(L_u/V)s+0.1987(L_u/V)²s²)
H_v = σ_v√(L_v/(πV))  · (1+2.7478(L_v/V)s+0.3398(L_v/V)²s²)
                      / (1+2.9958(L_v/V)s+1.9754(L_v/V)²s²+0.1539(L_v/V)³s³)
H_w：同 H_v，把 L_v 换成 L_w
```

**(d) 冻结湍流盒**（多机共享，前后端一致）。Dryden 为每架机产生互不相关的序列，两架相距 5 m 的无人机"感受"到的湍流完全无关，前端粒子也没法与之对应。推荐在 V0.6 多机时切换到下面的方案：

```python
def vk_box(N=64, dx=4.0, L=30.0, seed=7):                   # 256 m 周期盒
    k = 2π·fftfreq(N,dx);  K = |k|
    E = (K L)^4 / (1+(K L)^2)^(17/6)                          # von Kármán 能谱形状
    amp = sqrt(E / (4π K²))                                  # 单位体积 → 每模态幅值
    p = sin(k·dx)/dx                                         # *修正波数 → 离散（中心差分）无散
    ξ = CN(0,1)^3;  a = (ξ − p (p·ξ)/|p|²) · amp             # Helmholtz 投影
    u = Re(ifftn(a)); u /= std(u)                            # 每分量单位 rms
    return float16(u)                                        # (3,N,N,N)
u_turb(x,t) = diag(σ_u,σ_v,σ_w)·R(θ) · trilerp_periodic(box, R(θ)ᵀ(x − U_mean·t))
# σ 取 Dryden 同一套公式，或取 Level 3 的 k：σ = sqrt(2k/3)
```

实测数据：

| N | Δx | 生成时间 | 大小（f16） | 其他 |
|---|---|---|---|---|
| 64 | 4 m | 0.25 s | 1.6 MB | 纵向积分尺度约 24.6 m（L=30） |
| 128 | 4 m | 2.0 s | 12.6 MB | — |

散度与梯度之比：用普通波数投影为 0.55，用修正波数为 1e-7。

分发方式：服务端生成后把 `seed + 参数` 下发给前端，并直接推送 f16 盒作为 Data3DTexture。前端粒子与物理使用同一份湍流。

### 3.4 Level 2：质量守恒诊断风（WindNinja 算法的笛卡尔体素移植）

**变分问题**（Sasaki / MATHEW / WindNinja；推导详见 `doc/forthofer_thesis.pdf`）：

```text
min ∫ α_h²[(u−u0)²+(v−v0)²] + α_v²(w−w0)² dV    s.t. ∇·u = 0
=> u = u0 + c_h ∂λ/∂x,  v = v0 + c_h ∂λ/∂y,  w = w0 + c_v ∂λ/∂z,   c_h = 1/(2α_h²), c_v = 1/(2α_v²)
=> −[c_h(λ_xx+λ_yy) + c_v λ_zz] = ∇·u0
边界：侧面 4 面 + 顶面（流通）λ=0；地面和建筑壁面（不可穿透）法向通量=0，即该面系数为 0
```

**离散（本文实现 `r17_masscons.py` / `r17_masscons2.py`）：**

- MAC 交错网格：λ 在格心；U、V、W 在面心。实体掩码 `solid[k,j,i] = z_c < DSM(i,j) − ground`，即 2.5D 填充，忽略挑檐。
- 面开放标记：`ox[:, :, 1:-1] = fluid[..., 1:] & fluid[..., :-1]`；边界面在内侧为流体时开放（Dirichlet ghost λ=0）；地面封闭。
- 初始场 u0：按 §3.2 的 log 廓线沿 e(θ) 铺开；接触实体的面置 0。
- 只对流体格组装 7 点 SPD 稀疏矩阵：`diag = Σ_open a_f`，非对角为 `−a_f`，`a_x = c_h/Δx²`、`a_z = c_v/Δz²`；边界开放面只加对角。
- 求解：`pyamg.smoothed_aggregation_solver(A, symmetry='symmetric')` 作为 CG 预条件器（`rtol=1e-6`）。**AMG 层级只依赖几何，所有风向复用同一套。**
- 回代：`U = U0 + c_h·∇λ|face·open`；格心速度取两侧面的平均；实体格置 0。

**实测**（8 核 CPU，参考风速 10 m/s@10 m，z0=0.5）：

| 场景 | 网格 | 格数 / 流体格 | 矩阵 nnz | AMG 建立 | 每方向（CG 迭代次数） | 对照 | 峰值内存 |
|---|---|---|---|---|---|---|---|
| San Francisco 4 m | 186×180×40（顶 160 m） | 1.34M / 1.25M | 8.6M | 17–32 s | **7–12 s（9 次）** | Jacobi-PCG 411 次 / 68 s；纯 numpy 无矩阵 413 次 / 136–181 s | 0.85 GB |
| Shenzhen 8 m | 232×250×80（顶 640 m） | 4.64M / 4.55M | 31.6M | 104 s | **29–31 s（10 次）** | — | 约 3 GB（估算） |

求解后散度 ≤ 3.3e-6。

**α_v 敏感性**（SF，270°）：

| α_v | 含义 | \|w\| 最大值 | \|w\| p99 |
|---|---|---|---|
| 0.5 | 不稳定 | 16.4 m/s | 2.12 m/s |
| 1.0 | 中性，WindNinja 默认 | 10.3 m/s | 1.35 m/s |
| 2.0 | 稳定 | 5.9 m/s | 0.86 m/s |

**建议城市默认 α_v=1.5–2**，并对 |w| 做 p99.9 截断，避免无人机在楼角被"弹飞"。

**两个关键性质**（因为问题是线性的，严格成立，实测验证）：

- `û(θ; s) = s·û(θ; 1)`：风速维度可以删掉。
- `û(θ+180°) = −û(θ)`：`max|f(90)+f(270)| = 1.9e-6`，只需算半圈。

所以 **Level 2 库只需 6 个 30° 扇区**。SF 约 2 min 算完，Shenzhen 约 5 min 算完（含 AMG 建立）。

**局限：没有分离、尾流和回流**，背风区风速偏高。可选的轻量补救（V0.4+）：

```text
wake = flowSeparation(DSM, θ, φ=15°)      # WindNinja 阴影扫描法：沿风向逐行推进"遮蔽高度"
u0[wake & z<H_wake(i,j)] *= β              # β 取 0.2–0.4；或按 Röckle/QUIC-URB 腔体、尾流椭球参数化
再做一次质量守恒投影                       # 保证无散
```

QUIC-URB / QES-Winds（犹他大学，GPU 质量守恒城市风）是同类算法的成熟外部参考，本次未 clone。

### 3.5 Level 3：OpenFOAM 离线城市风场库

```bash
# 0) 几何：点云 → DSM → 水密 STL（任选其一）
python dsm_to_stl.py city.ply --cell 2 --clean median3,minH=2 → constant/geometry/city.stl
   # 做法参照 WindNinja NinjaElevationToStl：每个 DSM 格 2 个三角形，外圈缓冲一格
vdb_tool -read city.ply -points2ls voxel=1 radius=1.5 -dilate -gauss -erode -ls2mesh -write city.stl
   # 真 3D 形体，保留挑檐；可选，V0.5+
# 1) 网格：只划一次，所有风向共用
snappyHexMeshConfig -cylindricalBackground -refinementLevel 1 \
    -refinementSurfaces '((city 3))' -insidePoint '(x y z_top-1)'
blockMesh && snappyHexMesh -overwrite && checkMesh
# 2) 逐个风向求解
for d in 000 015 ... 345; do
  写入 constant/atmosphericBoundaryLayerProperties:
      flowDir (−sin d, −cos d, 0); zDir (0 0 1); Uref 10; Zref 10; z0 uniform 0.5; zGround uniform <zmin>;
  rm -rf 0 && cp -r 0.orig 0 && setAtmBoundaryLayer        # 用 ABL 廓线初始化整场
  foamRun -solver incompressibleFluid                       # （或 decomposePar + mpirun -np 8 … -parallel）
  foamPostProcess -func windGrid -latestTime                # system/windGrid，见下
  python sets_to_npy.py → dir_$d.f16.zst（u,v,w,k）+ solid 掩码
done
```

**边界条件**：侧面合并成一个 patch `sides`（圆柱背景网格天然如此）。

| 场 | sides | top | 地形 / 建筑 |
|---|---|---|---|
| U | `atmosphericBoundaryLayerVelocity`（inletOutlet 派生） | `slip` | `noSlip` |
| p | `totalPressure p0 0`（或 `freestreamPressure`） | `slip` | `zeroGradient` |
| k | `atmosphericBoundaryLayerTurbulentKineticEnergy` | `slip` | `kqRWallFunction` |
| ε | `atmosphericBoundaryLayerTurbulentEpsilon` | `slip` | `epsilonWallFunction` |
| nut | `calculated` | `calculated` | 地面 `nutAtmosphericBoundaryLayerWallFunction`（z0），建筑立面理想情况下用 `nutkWallFunction`；DSM 单曲面时统一用 ABL 壁函数 |

**湍流模型**：`kEpsilon` 取 σε=1.11（Hargreaves & Wright，见 `turbineSiting`），或 `RNGkEpsilon`（WindNinja v11 模板，缓解驻点 k 过高）。`fvSchemes`、`fvSolution` 直接照搬 `windninja/data/ninjafoam/11/system/`。

**system/windGrid**（按 `#includeFunc` 的查找规则，case 的 `system/` 优先于 `etc/caseDicts`；需要在装有 OpenFOAM 的环境里确认）：

```text
type sets; libs ("libsampling.so"); writeControl writeTime;
interpolationScheme cellPoint; setFormat raw; fields (U k);
sets { grid { type boxUniform; box (x0 y0 z0) (x1 y1 z1); nPoints (nx ny nz); axis xyz; } }
```

建筑内部的点会被跳过，用坐标反推 (i,j,k)，缺失处即为 solid。

**成本（估算，本机没有 OpenFOAM）**：

- 1–2 km 城区，建筑近壁 2–4 m：2–5M 格。
- simpleFoam 类求解约 1–2 s/迭代/M 格/核；8 核时约 0.5–1 s/迭代。1500 次迭代约 15–30 min/方向，24 方向约 6–12 h。
- 用相邻方向的解作初值，可再减 30–50%（估算）。
- Level 3 库：24 方向 × (u,v,w,k) f16 + zstd，SF 规模约 200 MB（估算）。

### 3.6 扇区插值与风速缩放（运行时核心）

```python
def sample_library(pts, dir_from, speed):
    a0, a1 = 相邻两个扇区(dir_from); w = 角距离比例          # 支持跨 0/360
    out = 0
    for a, wt in ((a0, 1-w), (a1, w)):
        f = trilerp(field[a], pts)                           # 归一化场（参考风速 1 m/s）
        p = f·e(a);  q = f·n(a)                              # 分解到该扇区来流坐标
        out += wt * (p·e(dir_from) + q·n(dir_from), f_w)     # 在目标来流坐标下重组
    return speed * out            # L2 精确成立；L3 需按风速档 s_i 选最近的一档或插值（Re 无关近似）
# L2：只存 [0°,180°) 的扇区，θ≥180° 时取 −field[θ−180]
```

| 场景 | 插值 | 直接 lerp：平均 / p95 / 风速偏差 | flow-aligned：平均 / p95 / 偏差 |
|---|---|---|---|
| SF 270/285→277.5 | 15° | 0.141 / 0.164 / −0.141 | **0.005 / 0.015 / 0.000** |
| SF 270/300→285 | 30° | 0.562 / 0.652 / −0.562 | **0.019 / 0.060 / −0.001** |
| SF 240/300→270 | 60° | 2.209 / 2.562 / −2.209 | **0.071 / 0.223 / −0.002** |
| Shenzhen 270/285→277.5 | 15° | 0.177 / 0.203 / −0.177 | **0.008 / 0.022 / 0.000** |

单位为 m/s，参考风速 10 m/s。最大误差集中在楼角：SF 30° 时 1.49，Shenzhen 15° 时 1.08。

结论：**L2 用 30° 扇区；L3 因为尾流对方向敏感，用 15°–22.5°。**

UI 改风向或风速时，服务端对 (θ, s) 做 2–5 s 的一阶平滑过渡，避免物理出现阶跃，与前端 transitions.dev 的动效时长一致。

### 3.7 World Package 存储格式（`worlds/<scene>/environment/wind/`）

```text
manifest.json
{ "level": 2, "solver": "masscons-mac-v1", "alpha_h": 1, "alpha_v": 1.5,
  "grid": { "origin_enu": [x0,y0,z0], "spacing": [dx,dy,dz], "shape_zyx": [nz,ny,nx],
            "cell_centered": true, "dtype": "f16", "channels": ["u","v","w"] },        # L3 另加 "k"
  "ref": { "speed": 1.0, "height": 10.0, "z0": 0.5, "profile": "log" },
  "sectors_deg": [0,30,60,90,120,150], "antisymmetric": true,                           # L3: 0..345/15, false
  "speeds": [1.0],                                                                     # L3: [3,8,15]
  "files": ["dir_000.f16.zst", ...], "solid": "solid.u8.zst",
  "vis": { "downsample": 2, "files": ["vis/dir_000.rgba16f.gz", ...] },
  "stats": { "w_p999": 3.1, "div_max": 3e-6 }, "created": "...", "source": "UrbanScene3D/SanFrancisco" }
```

| 每方向体积（SF，1.34M 格 × 3 分量） | 大小 | 说明 |
|---|---|---|
| dense f32 | 16.07 MB | — |
| **dense f16（运行时 mmap）** | 8.04 MB | f16 与 f32 最大误差 0.0078 m/s |
| f16 + zstd-3 / zstd-19 / gzip-6 | 6.37 / 4.94 / 5.79 MB | 压缩耗时 0.09 / 3.8 / 0.69 s；解压 45 / 66 / 88 ms |
| VDB（blosc + active，f32） | 12.71 MB | pyopenvdb 写 0.11 s，readAll 0.06 s |
| VDB `saveFloatAsHalf` | 6.61 MB | 往返误差 0.0078 m/s |
| **Web 可视化 2× 降采样 RGBA16F** | 1.34 MB（gzip 0.80） | (u, v, w, 实体率) |

VDB 导出片段（pyopenvdb 10 实测可用；v12+ 把 `import pyopenvdb` 改成 `import openvdb`）：

```python
g = vdb.Vec3SGrid((0,0,0)); g.copyFromArray(np.transpose(f,(2,1,0,3)).copy(), ijk=(0,0,0), tolerance=(0,0,0))
g.name='wind'; g.vectorType='contravariant absolute'; g.saveFloatAsHalf=True
g.transform = vdb.createLinearTransform(matrix=[[dx,0,0,0],[0,dy,0,0],[0,0,dz,0],[x0+dx/2,y0+dy/2,z0+dz/2,1]])
g['dir_from_deg']=270.0; g['ref_speed']=1.0
vdb.write('wind_270.vdb', grids=[g, solid_bool_grid])
```

注意：pyopenvdb 10 的 `Vec3SGrid.copyFromArray` 要求 `tolerance` 传 vec3，传标量会报 TypeError。

### 3.8 服务端 WindField 接口（Environment Service 的一部分）

```python
class WindConfig(BaseModel):          # REST: PUT /worlds/{id}/environment/wind
    level: Literal[0,1,2,3]; speed: float; dir_from: float; ref_height: float = 10
    profile: Literal['log','power','uniform'] = 'log'; z0: float = 0.5; d: float = 0; power_alpha: float = .25
    w_const: float = 0
    gust_amp: float = 0; gust_len: float = 60; gust_rate: float = 0         # 1−cos，Poisson
    turb: Literal['none','dryden','vonkarman','box'] = 'dryden'
    intensity: Literal['light','moderate','severe'] = 'light'; w20: float | None = None
    seed: int = 0; library: str | None = None                                # L2/L3 manifest

class WindSample(NamedTuple):
    mean: np.ndarray; gust: np.ndarray; turb: np.ndarray; total: np.ndarray  # (N,3) ENU m/s
    valid: np.ndarray                                                        # False = 点在实体内（碰撞/告警）

class WindField(Protocol):
    def configure(self, cfg: WindConfig) -> None
    def query(self, pts: (N,3), t: float, agent_ids: list[str], v_vehicle: (N,3)) -> WindSample
    def query_grid(self, bbox, spacing, t) -> bytes          # 调试 / 切片热力图
    def vis_volume(self) -> VisVolumeRef                     # 前端 RGBA16F 3D 纹理 URL 与元数据
    def summary(self) -> dict                                # UI：speed/dir/gust/σ/level

# 合成：
# total = speed(t)·Φ_L(x; θ(t)) + g(t)·e(θ) + R(θ)·[Dryden_i(h, V_rel) 或 σ(z)⊙Box(x−U t)]
#   Φ_0,1 = f(z)·e(θ)；Φ_2 = 质量守恒库；Φ_3 = CFD 库。L3 的湍流强度用 σ = sqrt(2k/3)，取代 Dryden 默认值
```

- **频率**：物理积分 100–1000 Hz；风场查询 50–100 Hz，每 tick 一次性批量查询全部机体，结果零阶保持。L1 以上的湍流带宽上限约为 V/L_w，量级 0.1–1 Hz，因此足够。
- **WebSocket**：
  - `DroneState` 增加 `wind: {mean, gust, turb, total, airspeed}`，10–50 Hz。
  - `environment.wind` 摘要 1–2 Hz。
  - 配置变更时推送事件 `wind.field.changed {vis_url, sectors, blend, seed}`。
- **风对无人机的力**（V0.4，mock 动力学）：

  ```text
  F = −½ρ C_D A |v_rel| v_rel − k_r T_Σ (v_rel − (v_rel·b_z) b_z)
  ```

  - `v_rel = v − W_total`。
  - 第二项是旋翼诱导阻力，只作用在桨盘平面内。
  - P600 量级的估算参数：C_D·A ≈ 0.05–0.1 m²，k_r ≈ 0.01–0.02 s/m。
  - 进阶做法：在 6 个电机位置分别采样 W，得到风切变产生的力矩。
  - 接入 PX4 SITL/Gazebo 的方式由 sim 单元定。

### 3.9 前端可视化契约（交给 r11/r12 实现）

- **纹理**：`Data3DTexture(RGBA16F, width=nx/2, height=ny/2, depth=nz/2)`，`LinearFilter`。
  - 采样坐标：`uvw = (p_enu − origin)/(extent)`，p_enu 由 three 坐标换算：`(x, −z, y)`。
  - 实体率 a > 0.5 视为建筑内部：粒子重生，箭头隐藏。
- **粒子**：用 compute 实现；WebGL2 回退走 r11 所述的 `instancedArray` + Transform Feedback。

  ```text
  vel  = speed·blend(texA, texB, w_flow_aligned)(p) + σ⊙texTurb(frac((p − U t)/boxSpan))
  p   += vel·dt;  age += dt
  if (outOfBounds(p) || solidFrac(p) > .5 || age > life) respawn(inflowFace(θ), rand)
  颜色按 |vel| 映射到灰→白→红（产品色阶），拖尾 8–16 帧
  ```

  前端只需要"当前 blend 后的一份场"：服务端每次改风向时下发新的 1.3 MB 纹理，或者下发两扇区纹理加权重，在 shader 中混合。
- **流线**：服务端从上风切面播种，按 `VelocityIntegrator::rungeKutta<4>` 积分：`dt = 0.5Δ/|u|`，进入 solid 或出界即停，最多 500 步。结果以折线段（Float32 xyz）下发，适合静态展示。
- **切片热力图**：取给定高度的 |u| 切片，做成 2D 纹理；配色与图例遵循 lieflat-charts 的视觉语言。风玫瑰和阵风时间序列也用 lieflat-charts 绘制。风、指南针、阵风图标用 morphicons，**禁止 emoji**。

---

## 4. 在本项目中的落点与复用方式

| 条目 | 来源 | 用途 | 模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|---|
| 风向约定与 sd<->uv 转换 | WindNinja `ninjaMathUtility.h` | 全链路统一 | `environment/wind/conventions.py`、`web/src/env/wind.ts` | V0.2 | port | 20 行代码，避免方向反号 |
| 垂直廓线与粗糙度预设 | WindNinja `windProfile.cpp`、`set_uniVegetation` | L0 | `environment/wind/profile.py` | V0.2 | port | 公式小，参数可靠 |
| 1−cos 阵风 + Dryden（向量化） | MIL-F-8785C；本文原型 `r17_turb.py`、`r17_windfield_api.py` | L1 飞控扰动 | `environment/wind/turbulence.py` | V0.2–V0.3 | port（本文代码） | 已做方差实测验证 |
| 冻结 von Kármán 湍流盒 | 本文原型 `r17_turb_box.py` | 多机空间相关、前后端一致 | `environment/wind/turbbox.py` + 前端 3D 纹理 | V0.3（视觉）/ V0.6（多机物理） | port | 0.25 s 生成，1.6 MB |
| 质量守恒 L2 求解器 | WindNinja `ninja.cpp`（变分法、BC、α） | UrbanScene3D 内置场景的建筑绕流 | `tools/wind/build_l2_library.py`（离线）+ `WindLibrary` | V0.3–V0.4 | port（笛卡尔 MAC + pyamg） | CPU 几分钟即可，无需 OpenFOAM |
| 背风阴影尾流修正 | WindNinja `flowSeparation.cpp` | 补救 L2 没有尾流的问题 | 同上 | V0.4+ | port | 可选 |
| flow-aligned 扇区插值、风速线性缩放、反对称 | 本文实测 | L2/L3 运行时 | `environment/wind/library.py` | V0.3 | 自研 | 误差降低约 28 倍，库体积减半 |
| ABL U/k/ε 廓线公式 | OpenFOAM `atmosphericBoundaryLayer.H`；WindNinja `logProfile*` BC | L1 的 σ 取值、L3 入口 | `environment/wind/abl.py`、`tools/cfd/templates` | V0.4 / V0.6 | port | 与 L3 一致 |
| OpenFOAM 城市算例模板 | `turbineSiting`、`windAroundBuildings`、`snappyHexMeshConfig`、`boxUniform`、WindNinja `data/ninjafoam/11/system/*` | L3 库 | `tools/cfd/openfoam/`（Docker） | V0.6–V1.0 | adopt（工具）+ port（模板） | 离线、可复现 |
| DSM 转 STL | WindNinja `stl_create.cpp`（`NinjaElevationToStl`） | snappy 的输入 | `tools/cfd/dsm_to_stl.py` | V0.6 | port | 2.5D 城市足够 |
| 点云转水密网格 / SDF | OpenVDB `vdb_tool points2ls/ls2mesh`、`mesh2ls` | L3 真 3D 几何；World 的 SDF 与碰撞 | `world/geometry/sdf/` | V0.5+ | adopt（CLI） | 与设计 §7 的 SDF 对齐 |
| 三线性插值、RK4 流线 | OpenVDB `tools/Interpolation.h`、`tools/VelocityFields.h` | 查询与流线 | `library.py`、`streamlines.py`、WGSL | V0.3 | port | 算法简短 |
| `.vdb` 导出 | pyopenvdb / openvdb Python | 与 Houdini/Blender/ParaView 交换 | `tools/export/wind_to_vdb.py` | V0.6+ | adopt（可选） | 不进运行时 |
| WindNinja CLI（domainAverage、wxModel） | WindNinja `cli.cpp`、`data/cli_*.cfg` | 真实山区（P600 采集的 DEM）、NWP 降尺度 | `tools/wind/windninja_runner.py`（Docker） | V0.5+ | adopt | 地形跟随网格更适合山地 |
| LES 阵风数据集 | FastEddy | 高保真湍流统计、Dryden 标定 | — | V1.0+ | reference | 需要 GPU，成本高 |

---

## 5. 对比与推荐

| 维度 | WindNinja（质量守恒） | WindNinja（动量） | OpenFOAM RANS | FastEddy LES | 本文 L2 移植 |
|---|---|---|---|---|---|
| 物理 | 无散投影，无动量 | RNG k-ε 稳态 | k-ε 家族稳态，有分离 | 可压 LES，时变湍流 | 同 WindNinja 质量守恒 |
| 建筑 | 地形跟随网格，建筑被抹平 | 地形投影网格，同样没有建筑 | snappy 精确贴体 | 体素拖曳（浸没体） | 2.5D 体素掩码，直角墙 |
| 尾流 / 回流 | 无 | 地形尺度有 | 有 | 有（时变） | 无，可用阴影修正补救 |
| 单方向耗时 | 秒到分钟（估算） | 十分钟到小时（估算） | 15–30 min，8 核（估算） | GPU 小时级 | **7–30 s（实测）** |
| 部署 | GDAL/Boost 构建或 Docker | 再加 OpenFOAM 11 | Docker / apt | CUDA + MPI + HPC | 纯 Python（scipy、pyamg） |
| 2026 活跃 / star | yes / 186 | yes / 186 | yes / 2247 | yes / 129 | — |

**推荐排序**（综合 star、2026 活跃度、契合度）：

1. **WindNinja**：算法移植价值最大，MVP 直接可用。
2. **OpenFOAM-dev**：Level 3 的唯一可行开源选择，star 最多的 CFD 仓库，活跃。建议锁定 **OpenFOAM 13** 发行版的 Docker 镜像，而不是跟踪 dev 分支。
3. **OpenVDB**：star 最多，但只作为交换格式和几何工具。
4. **FastEddy**：只作参考。

---

## 6. 风险与注意事项

1. **WindNinja 构建**：依赖 GDAL/Boost/NetCDF（Qt 可关）。仓库自带的 `Dockerfile` 仍是 ubuntu 20.04 + openfoam8，与 v11 模板不一致，需要按 `scripts/build_deps_ubuntu_2404.sh` 自行制作镜像。输入 DEM 必须带投影（GeoTIFF + CRS）：本地 ENU 要写成横轴墨卡托自定义 CRS，并留意 `getAngleFromNorth` 带来的方位修正。
2. **WindNinja 默认网格过粗**（水平 4000/10000/20000 格），停止准则是绝对残差 `1e-1`。城市场景必须显式设 `mesh_resolution`；由于地形跟随，**建筑会被当成光滑的山**，所以城市场景用本文的笛卡尔移植版，WindNinja 只用于山区。
3. **质量守恒的物理局限**：没有尾流；楼角 w 尖峰（实测 ±10 到 ±34 m/s）。缓解：α_v、截断、阴影修正。UI 上要标注为"诊断风（示意）"，不能作为安全结论。
4. **DSM 质量**：UrbanScene3D 采样点云有空洞（Shenzhen 空列 0.4%，SF 1.9%，已用地面值填充），并存在离群高点。需要先做中值滤波和最小建筑高度阈值。Suzhou 的 up 轴是 y，Chicago 单位疑似 km（见 r03），生成 DSM 前必须先按 World 元数据校正。
5. **OpenFOAM 的坑**：
   - dev 分支 API 变动快（simpleFoam 已移除，改为 `foamRun`）。
   - snappy 要求 STL 干净、insidePoint 不能落在面上。
   - 计算域尺寸建议：顶部 ≥ 5H_max，侧向和出口 ≥ 5H / 15H，阻塞率 < 3%（COST 732 / AIJ 指南，文献值）。
   - k-ε 在建筑迎风角会高估 k，用 RNG 或 realizable 缓解。
   - 水平均匀 ABL 问题：σε=1.11，壁函数 z0 与入口一致。
   - 本机无 root，需要 Docker 环境。
6. **pyamg 的内存与时间**：AMG 建立时间约为单方向求解的 2–3.5 倍，内存约 0.65 KB/格；10M 格以上要换 8 m 以上分辨率，或者分块、嵌套。备选方案是 red-black SOR 的 GPU 版（QES-Winds 思路）或 PETSc。
7. **OpenVDB 的 Python 获取**：PyPI 上没有 wheel。Ubuntu 仓库的 `python3-openvdb 10.0.1` 模块名是 `pyopenvdb`，依赖 numpy<2（本文用独立 venv 加 `dpkg -x` 验证）；conda-forge 或源码构建可得到 v12+ 的 `openvdb` 模块。另外要注意格心 +0.5 体素平移、`vectorType` 元数据、`copyFromArray` 的 tolerance 类型。
8. **NanoVDB 上 Web 的门槛**：`PNanoVDB.h` 依赖 SSBO，WebGL2 回退路径不可用。如果前端将来要做体积云或风体积的 ray march，先用 dense 3D 纹理，NanoVDB 只作为 WebGPU 专用的远期选项。
9. **Dryden 的适用性**：低空公式在 h < 10 ft 时发散（已 clamp）；V 小的时候时间常数很大（已按相对空速 clamp）；多机之间互不相关，需要的话切换到湍流盒。实现 von Kármán 有理近似后必须用方差检验回归测试。
10. **FastEddy**：只支持 CUDA/HIP，预处理依赖 NCAR 的 GeoSpec/SimGrid/GenICBCs 流程，本机无法运行。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§19–20 风场分级重新定义**（可执行、可测试）：
   - **L0**：均匀风 + 垂直廓线（log/power）。原文"8 m/s 270°"需要补上参考高度和 z0。
   - **L1**：L0 + 1−cos 阵风（Poisson）+ Dryden（每机）或冻结湍流盒（共享）+ 垂直风。
   - **L2**：基于 World Geometry 体素的**质量守恒诊断风**（WindNinja 算法），可选阴影尾流修正。原文把 "Building / Mountain / Valley / Updraft / Wake" 列成清单，**这些不应该逐项手写**，而应由几何加同一求解器统一产生。
   - **L3**：OpenFOAM RANS 扇区库。
   - 新增 **L4（远期）**：FastEddy / WRF 降尺度 LES 的时变阵风数据集。
2. **§20 L3 目录结构改为 manifest + 归一化扇区**：`000_05.vdb` 这种"方向 × 风速"命名冗余。L2 的风速维度严格多余，而且只需要半圈。L3 的风速维度近似多余（取 1–3 档）。方向插值必须采用 flow-aligned 方式。运行时格式用 dense f16 + zstd，**VDB 降级为导出格式**（§3.7）。
3. **§18 E(x,y,z,t) 接口补三点**：
   - `valid` / 实体掩码：点落在建筑内时的语义。
   - 时间语义："静态空间模式 × 时变标量（s(t), θ(t)）+ 随机湍流"。
   - 湍流强度字段（L3 由 k 给出）。
4. **§28 DroneState 增加 `wind` 与 `airspeed`**；§47 "Wind → Drone Force" 写明力模型（§3.8）和采样位置（机体中心或各电机）。
5. **§37 频率表补上"环境场查询 50–100 Hz + 零阶保持"**，避免在 1000 Hz 物理步里查风（L2 在 numpy 中约 1 ms/tick，10 机）。
6. **V0.3 和 V0.4 的拆分可以提前**：L0/L1 的物理成本几乎为零，建议 **V0.2 的 mock 动力学就带上风力**。V0.3 同时交付视觉和 UrbanScene3D 的 L2 库（离线几分钟）。OpenFOAM 推迟到 V0.6 以后。
7. **§41 World Package 的 `environment/wind/` 定义清楚**：`manifest.json`、`dir_XXX.f16.zst`、`solid.u8.zst`、`vis/`（RGBA16F）、`turbbox.f16`（或 seed）。§7 的 SDF 建议用 OpenVDB 窄带 level set 生成（`vdb_tool points2ls`），它同时服务碰撞和 CFD 网格。
8. **§21 风可视化**：明确"服务端只下发一份 blend 后的 RGBA16F 体（约 1 MB）加湍流盒纹理（1.6 MB）"，前端做粒子、流线和切片，风速色阶用产品色（科技灰→白→红）。原文"WebGPU 完成"需要补一条 WebGL2 回退路径（RGBA16F 可线性过滤；compute 回退见 r11）。
9. **§33 技术栈表**：
   - "CFD：OpenFOAM" 细化为 "OpenFOAM 13（Docker，`foamRun -solver incompressibleFluid`）"。
   - 新增 "诊断风：WindNinja 算法移植（scipy/pyamg）"。
   - 新增 "山地：WindNinja CLI（Docker）"。
   - "GPU：CUDA" 对风场不是必需项。
10. **坐标与风向约定写入 `coordinate.json` 与接口文档**：气象来向；ENU；three.js Y-up 映射；格心约定。这是本单元发现的最高频出错点。
