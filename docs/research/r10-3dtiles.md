# R10 研究笔记：3D Tiles 规范 / 3d-tiles-tools / cdb-to-3dtiles / potree23dtiles

> 研究单元：r10 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §7–8、§11、§14–16、§41、§43、§51
>
> 仓库快照（均为 shallow clone，路径相对 `refs/world/`）：
> - `3d-tiles` @ `2177ba1`（2026-08-17，2612 stars）：3D Tiles 1.1 规范（OGC Community Standard），另含 `next/2.0/` 草案（最后更新 2026-08-18）
> - `3d-tiles-tools` @ `4ca692e`（2026-07-24，539 stars，npm `3d-tiles-tools@0.5.4`，TypeScript）
> - `cdb-to-3dtiles` @ `0f9487f`（2024-05-07，92 stars，C++17）
> - `potree23dtiles` @ `a844fba`（2021-10-03，58 stars，Python）
>
> 补充对照（只读引用，不属于本单元 clone 清单）：
> - `refs/web3d/cesium` @ `b3155a8`（2026-09-25）：SSE 与遍历的事实标准实现
> - `refs/web3d/potree`：点预算与优先级
> - **NASA-AMMOS/3DTilesRendererJS** @ `b70e594`（v0.5.3，2026-09-28，2475 stars）：本单元额外 clone 到 `.cache/research/r10/3DTilesRendererJS`，它是 Three.js 生态里 3D Tiles 的事实标准加载器，2026-09 刚加入 Potree 插件
> - **py3dtiles 12.1.1**（PyPI，GitLab 主仓 2026-09-18 仍活跃）：Python 点云切片器，做了实测
> - **CesiumGS/3d-tiles-validator 0.6.1**（475 stars，2026-09-16）：实测校验工具
>
> 本机实测产物：`.cache/research/r10/`。其中 `wp_tiler.py` 是切片原型，`sse_select.py` 是遍历与 SSE 参考实现，`parse_subtree.py` 用来解析 subtree，`ply_stats.py` 统计数据；`wp_sf/` 与 `py3dtiles_sf/` 是两种切片输出，另有两份 validator 报告。
> 所有路径都相对各自仓库根目录。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **3d-tiles**（规范） | HLOD 流式 3D 容器的开放标准。1.1 核心包含 implicit tiling、glTF 内容和 3D Metadata；2.0 草案改为以 glTF 2.1 为基础 | **adopt（作为格式标准）**：World Package 的点云、网格、3DGS 可视层直接采用 3D Tiles 1.1 子集。**port**：SSE 语义、subtree 位流、Morton 索引 | V0.1 起（点云层原生格式），V0.5 地理配准后进入 Cesium，V1.0 评估 2.0 | 5/5 |
| **3d-tiles-tools** | 3D Tiles 的处理工具箱：格式升级（pnts→glb）、打包（3tz/3dtiles）、合并、服务、分析、遍历库。**它不是切片器**，不能把 PLY/LAS 切成八叉树 | **port**：`MortonOrder`、`OctreeCoordinates`、`BufferAvailabilityInfo`、`BinarySubtreeDataResolver`、`BoundingVolumeDerivation`、`GltfTransformPointClouds`。**adopt**：CLI 的 `convert` / `mergeJson` / `serve` / `upgrade` 用于打包、合并和本地服务 | V0.1（移植 implicit 客户端），V0.5（打包与合并） | 4/5 |
| **cdb-to-3dtiles** | OGC CDB（军用仿真地形库）→ 3D Tiles 1.0 转换器，覆盖地形、影像、模型、矢量 | **reference**：大区域"瓦片集的瓦片集"组织方式、REPLACE 地形补洞、父级影像回退、地形贴地查询、meshopt 简化参数。反例：几何误差 `300000/2^L` 与数据无关 | V0.5–V0.6（网格/地形层、多区域组织） | 2/5 |
| **potree23dtiles** | Potree 1.7/2.0 八叉树 → 3D Tiles 1.0 pnts 的脚本，只支持 Windows | **reference**：Potree 2.0 `hierarchy.bin` 解析、ENU→ECEF 根变换。代码不用（违反规范、会丢子树、2021 年后无维护） | V0.1 仅参考 | 1/5 |
| 补充：**3DTilesRendererJS** | Three.js / R3F / Babylon 的 3D Tiles 渲染器，含 implicit、Potree、点云 EDL、LRU、优先队列 | **port**（点云流式核心，因为要兼容 WebGPU）。**adopt**（V0.5+ 网格和外部 3D Tiles 图层，WebGL2 路径） | V0.1 port；V0.5 adopt | 5/5 |
| 补充：**3d-tiles-validator** | 官方校验器 | **adopt**，进 CI | V0.1 | 4/5 |
| 补充：**py3dtiles** | Python 点云切片器 | **reference**。实测 1.1 输出**不合规**，不直接采用 | — | 2/5 |

**关键结论（实现者先读这几条）**

1. **点云层不要等到"后期兼容 3D Tiles"**（原设计 §15），V0.1 起就用 **3D Tiles 1.1 implicit OCTREE** 作为 World Package 点云层的原生格式。具体组成：`tileset.json`、`subtrees/{level}/{x}/{y}/{z}.subtree`、`content/{level}/{x}/{y}/{z}.glb`（POINTS 图元 + `KHR_mesh_quantization`），`refine: "ADD"`，**`geometricError` 取该层点间距 spacing**，并在 subtree 瓦片元数据里存 `pointCount`，让点预算在下载前就能算出来。本单元写的原型 `wp_tiler.py` 把 UrbanScene3D San Francisco（5,000,091 点）切成 1403 个节点、65 个 subtree，共 68 MB，平均 12.27 B/点，耗时 60 s，**官方 3d-tiles-validator 0 错误 0 警告**（§3.6）。
2. **Web 端 LOD 统一采用 SSE 公式**：`SSE_px = GE · H / (d · 2·tan(fovy/2))`。它和 Cesium 的 `Cesium3DTile.getScreenSpaceError`、3DTilesRendererJS 的 `calculateTileViewError` 完全一致；Potree 的节点权重是它的一个特例（§3.1）。点云取 `GE = spacing` 时，**errorTarget 建议 1–2 px**：3DTilesRendererJS 的 `PotreePlugin` 用 1；Potree 的 `minimumNodePixelSize = 150` 换算过来约 1.35 px。网格仍用 Cesium 默认值 16 px。
3. **"疏密自动调节"= SSE 最佳优先 + 硬点预算 + 帧时反馈。** 候选瓦片按 SSE 降序排列，累计点数到预算上限就截断（Potree 语义）。errorTarget 和预算按帧时 EMA 做乘性调节，步长参考 Cesium `memoryAdjustedScreenSpaceError` 的 ×1.02 / ÷1.02。再加 ADD 早裁剪、移动中不发请求、注视点延迟加载（§3.2–3.4）。本机模拟在 1280×720、fov 60° 下的结果：无人机 120 m 高、俯角约 30° 时，errorTarget 1 px 需要约 0.87M 点，2 px 约 0.37M 点；斜距约 1.4 km 俯瞰时 1 px 约 0.35M 点（§3.2.4）。
4. **点云一律用 ADD，网格和 3DGS 用 REPLACE。** ADD 不重复存点，不用等全部子节点到齐，缺了某个子节点也不会出现空洞。REPLACE 要求子节点完整覆盖父节点，cdb-to-3dtiles 用 `fillMissingPositiveLODElevation` 专门补齐缺失的子区域，就是这个原因（§3.10）。
5. **3D Tiles 2.0 草案（2026-08-18）有破坏性变更**：tileset 改为 glTF 2.1 资产；本地坐标系改为 Y-up；**几何误差不再随 tile transform 缩放**；GE ≥ 父级的瓦片"无条件细化"。为保证设计对 1.1 和 2.0 都成立：**只用刚体 transform（不带缩放）**，GE 严格逐级递减，World 清单里显式写 up-axis 和单位（§6）。
6. **02-refs.md 里"3d-tiles-tools：点云/网格 → 3D Tiles"的描述不准确。** 该工具没有切片能力（src 中没有 LAS/PLY 读取或八叉树构建），只做升级、打包、合并、服务和分析。点云切片要自研（本单元原型），或者用 PotreeConverter、py3dtiles。
7. **py3dtiles 12.1.1 `--spec-version 1.1` 实测不合规。** 747 个 glb 共报 748 个 ERROR，原因是 `COLOR_0` 为 `VEC3 UNSIGNED_BYTE`，没有 normalized 也没有 4 字节对齐。它还用带 ×10 缩放的 tile transform，在 1.1 和 2.0 下 GE 会差 10 倍。结论：只作参考。
8. **UrbanScene3D 数据实测。** 只有 xyz 和法线，**没有颜色**。Chicago 的包围盒是 4.2×8.0×0.62，**单位疑似 km**，需要 ×1000。Suzhou 的 y 向只有 155 m，而法线主轴是 z（占 48%），**轴向存疑**，需要人工确认。所以导入阶段必须有"单位、轴向归一化到 ENU Z-up 米"这一步（§3.8）。
9. **可视世界的 3DGS 走标准路线。** `KHR_gaussian_splatting` 已被 Khronos **批准**（Complete, Ratified），SPZ 压缩（`KHR_gaussian_splatting_compression_spz_2`）还在 PR 阶段。CesiumJS 已支持两者（`GaussianSplat3DTileContent.js`）。§8 的 Visual World 可以直接用"3D Tiles + glTF 3DGS"。

---

## 1. 仓库概览

| 项 | 3d-tiles | 3d-tiles-tools | cdb-to-3dtiles | potree23dtiles |
|---|---|---|---|---|
| 最后提交 | 2026-08-17（合并 2.0-changes） | 2026-07-24（0.5.4 于 2026-07-12 发布） | 2024-05-07（只更新了 demo） | 2021-10-03 |
| Star | 2612 | 539 | 92 | 58 |
| 内容与规模 | AsciiDoc 规范约 13.5k 行，JSON Schema，扩展，`next/2.0` 草案 | TS 源码 270 个文件约 37.7k 行，含 CLI、遍历、implicit、metadata、打包、迁移 | C++ 约 5k 行（`CDBTo3DTiles/src`）加 `Core` 椭球数学 | Python 约 800 行，另附 Windows 版 PotreeConverter.exe |
| 依赖 | — | glTF-Transform 4.4、cesium 1.103+、gltf-pipeline、gltfpack、meshoptimizer、draco3d、sharp、better-sqlite3、archiver | GDAL ≥3.0.4、OpenSceneGraph、OpenGL、tinygltf、meshoptimizer、glm、earcut（全是 git submodule，**shallow clone 里为空**） | numpy、pyproj、glob2（未声明） |
| 构建与运行 | 只读 | `npm i 3d-tiles-tools`，Node ≥18（本机 Node 22 可用） | CMake ≥3.15，需要先 `git submodule update --init`，只支持 Linux | 只支持 Windows（依赖 `subprocess.STARTUPINFO` 和 .exe） |
| 与本项目关系 | **格式基石** | implicit 客户端的移植源，打包与合并工具 | 大区域地形组织的参考 | Potree 与 3D Tiles 映射的反面教材 |

**3d-tiles（规范）** 由 CesiumGS 维护，1.0 在 2018 年、1.1 在 2022 年成为 OGC Community Standard。1.1 把 3D Tiles Next 的四项能力并入核心：glTF 作瓦片内容、多内容（`contents` + `group`）、implicit tiling、3D Metadata。同时把 b3dm/i3dm/pnts/cmpt 标记为 deprecated，迁移指南在 `specification/TileFormats/glTF/MIGRATION.adoc`。2026 年仓库新增 `next/2.0/`，内容是 3D Tiles 2.0 草案：以 glTF 2.1 为基础，新增 Vector、Voxels、Time Dynamic、AEC、3DGS 支持。其中 Voxels 和 Time Dynamic 与本项目 `E(x,y,z,t)` 环境场、世界版本回放直接相关。

**3d-tiles-tools** 是 CesiumGS 官方的 TS 工具集，既能当 CLI 用，也能当库用。2026 年有 3 个版本：0.5.2 和 0.5.3（2026-03）重构了包围体计算，0.5.4（2026-07）新增 `serve` 命令和 `mergeJson3tz`，并把 `EXT_mesh_features` / `EXT_structural_metadata` 的实现交给 glTF-Transform 4.4。

**cdb-to-3dtiles** 把 OGC CDB 数据集（每 1° 一个 GeoCell，LOD 取值 −10…23）转成 3D Tiles 1.0，输出 b3dm/i3dm。官方给的 San Diego CDB 转换数据：28.3 GB 输入，47 分钟。2024 年后无功能更新，README 里的 roadmap（输出 3D Tiles Next）没有完成。

**potree23dtiles** 读 PotreeConverter 1.7（`cloud.js` 加每节点 LAS）或 2.0（`metadata.json` + `hierarchy.bin` + `octree.bin`），把每个节点写成一个 pnts，生成 3D Tiles 1.0 的显式 `tileset.json`（`asset.version: "0.0"`，内容字段还是旧的 `url`）。

---

## 2. 源码结构与关键模块

### 2.1 3d-tiles 规范：本项目要读的部分

| 文件 / 章节 | 内容 | 本项目要点 |
|---|---|---|
| `specification/README.adoc` → *Geometric error* / *Refinement* | GE 以米为单位，表示"只渲染本瓦片、不渲染子瓦片时引入的误差"；refine 取 `REPLACE` 或 `ADD`，根瓦片必填，子瓦片可继承 | 点云的 GE 应定义为该层点间距（§3.1） |
| 同上 → *Bounding volumes* | `box` 共 12 个数：中心 + 3 个半轴列向量（OBB）；`sphere` 4 个数；`region` 6 个数（弧度 + 米） | 局部 ENU 世界统一用 `box` |
| 同上 → *Transforms* | 4×4 列主序矩阵，从瓦片局部坐标变换到父坐标，逐级右乘。**"The transform property scales the geometricError by the largest scaling factor from the matrix"** | 2.0 草案撤销了这一条，所以**禁止带缩放的 transform**（§6） |
| 同上 → *glTF transforms* | 顺序为 glTF 节点层级 → **y-up 转 z-up**（绕 X 轴 +π/2）→ tile transform。数据本身是 z-up 时，常规做法是在 glTF 根节点写 z-up→y-up 矩阵 `[1,0,0,0, 0,0,-1,0, 0,1,0,0, 0,0,0,1]`，运行时两次旋转互相抵消 | 原型 glb 采用这种做法（§3.6） |
| 同上 → *Tileset JSON* | `tileset.geometricError` 决定整个 tileset 是否渲染（根瓦片按它计算 SSE）；`root.geometricError` 决定何时细化到根的子节点。`asset.tilesetVersion` 可以作缓存失效参数 | tileset GE 取根 GE 的数倍，保证根一定渲染 |
| 同上 → *External tilesets* | `content.uri` 可以指向另一个 tileset JSON，形成"瓦片集的瓦片集"。该瓦片不能再有 `children`，也不能成环 | 多区域、多架次组合（§4.2） |
| 同上 → *Viewer request volume* | 相机进入该体积后才请求和细化 | 可以按"无人机航线走廊"预取 |
| `specification/ImplicitTiling/README.adoc` | `implicitTiling{subdivisionScheme, subtreeLevels, availableLevels, subtrees.uri}`；子节点 GE 是父节点的一半，包围体按 2 或 8 等分；模板 URI 形如 `{level}/{x}/{y}/{z}`；subtree 位流分三类：tile、content、childSubtree；subtree 二进制头 24 字节 | §3.5 全部照此实现 |
| `specification/ImplicitTiling/AVAILABILITY.adoc` | Morton 交织顺序：**第 0 位放 x，第 1 位放 y，第 2 位放 z**；`levelOffset = (N^level−1)/(N−1)`；全局 Morton 码 = 子树根 Morton 码拼接局部 Morton 码 | 本机脚本验证了规范示例（§3.5） |
| `specification/TileFormats/PointCloud/README.adoc` | pnts 格式：28 字节头，FeatureTable 包含 `POSITION` / `POSITION_QUANTIZED`（反量化 `q·scale/65535 + offset`）、`RGB565`、`NORMAL_OCT16P`、`RTC_CENTER`，**1.1 已 deprecated** | 只做兼容读取，新数据不写 pnts |
| `specification/TileFormats/glTF/README.adoc` + `MIGRATION.adoc` | 点云用 glTF `mode 0 (POINTS)`；`RTC_CENTER` 并入根节点 translation；压缩用 `KHR_mesh_quantization` 或 `EXT_meshopt_compression`（**`KHR_draco_mesh_compression` 只支持三角网格**）；batch 表改为 `EXT_mesh_features` + `EXT_structural_metadata`；**颜色要从 sRGB 转成 linear** | glb 布局（§3.6） |
| `specification/Metadata/Semantics/README.adoc` | 语义包括 `TILE_BOUNDING_BOX`、`TILE_GEOMETRIC_ERROR`、`CONTENT_BOUNDING_BOX`、`TILESET_CRS_GEOCENTRIC`（例如 `"EPSG:4978"`）、`TILESET_CRS_COORDINATE_EPOCH` 等 | implicit 瓦片可以用元数据覆盖 GE 和包围盒 |
| `specification/schema/*.schema.json` | 必填字段：tileset 为 `asset`、`geometricError`、`root`；tile 为 `boundingVolume`、`geometricError`；implicit 为 `subdivisionScheme`、`subtreeLevels`、`availableLevels`、`subtrees`；subtree 为 `tileAvailability`、`childSubtreeAvailability` | 写入器自检用 |
| `next/2.0/CHANGES.md`（2026-08-18） | 破坏性变更清单，详见 §6.1 | 用来界定"兼容边界" |
| `next/2.0/README.md` | Voxels（`EXT_voxels`、`3DTILES_tileset_voxels`）、Time Dynamic（`EXT_node_visibility_conditions`）、3DGS（`KHR_gaussian_splatting` + `_compression_spz_2`）、`EXT_geospatial_crs`、`EXT_georeference` | 环境场和世界版本的远期路线 |

**规范本身不给出 SSE 公式**，`Q-and-A.md` 只说"用 geometricError 计算 SSE 来驱动细化"。公式来自 CesiumJS 实现，是事实标准，见 §3.1。

### 2.2 3d-tiles-tools 源码地图

```text
src/
  cli/main.ts, ToolsMain.ts        yargs CLI：convert / upgrade / merge / mergeJson / mergeJson3tz / combine / gzip /
                                   pipeline / analyze / serve / createTilesetJson / convertPntsToGlb / b3dmToGlb ...
  base/spatial/
    MortonOrder.ts                 encode2D（16 bit/轴）/ encode3D（10 bit/轴，受 JS 32 位位运算限制），fgiesen 位扩展法
    OctreeCoordinates.ts           (level,x,y,z)；parent()=>>1；children() 按 x 最快、z 最慢的顺序；toIndex()=levelOffset+morton
    Octrees.ts                     computeNumberOfNodesForLevels(L) = ((1<<3L)-1)/7
  tilesets/implicitTiling/
    ImplicitTilings.ts             createSubtreeCoordinatesIterator、globalizeOctreeCoords（(root<<localLevel)+local）、substituteTemplateUri
    SubtreeInfos.ts / AvailabilityInfos.ts / BufferAvailabilityInfo.ts / ConstantAvailabilityInfo.ts
                                   位流：byte=idx>>3，bit=idx%8，**LSB-first**
    BinarySubtreeDataResolver.ts   解析 .subtree：readBigUint64LE(8) 得 jsonLen，readBigUint64LE(16) 得 binLen；外部 buffer 经 ResourceResolver 读取
    TemplateUris.ts                对 {level}{x}{y}{z} 做字符串替换
  tilesets/traversal/
    TilesetTraverser.ts            DFS/BFS + callback 返回"是否继续展开子节点"；可以穿透 external tileset
    ExplicitTraversedTile.ts / ImplicitTraversedTile.ts
                                   implicit：asRawTile() 中 GE = rootGE / 2^level；包围体由 BoundingVolumeDerivation 推导；
                                   getChildren()：在子树最后一层时按 childSubtreeAvailability 取下一个 subtree（SubtreeModels.resolve）
    MetadataSemanticOverrides.ts   TILE_* / CONTENT_* 语义覆盖推导出的包围体和 GE
    cesium/BoundingVolumeDerivation.ts
                                   deriveBoundingBox：center = rootCenter + halfAxes·(−1+(2i+1)·2^−L)，halfAxes 各列 ×2^−L；
                                   deriveBoundingRegion；S2（Hilbert）
  tilesets/tileFormats/TileFormats.ts   读写 b3dm/i3dm/pnts/cmpt 的头、feature table 和 batch table
  tilesets/tileTableData/          TileTableDataPnts（量化位置、RGB565、oct16 法线解码）、AttributeCompression.octDecode、Colors（sRGB→linear）
  tilesets/packages/               3TZ（ZIP store + "@3dtilesIndex1@" 索引：MD5(key) 16B + offset 8B，按 MD5 排序，可二分）、
                                   3DTILES（SQLite：CREATE TABLE media(key TEXT PRIMARY KEY, content BLOB)）
  tools/pointClouds/
    GltfTransformPointClouds.ts    build()：Primitive.Mode.POINTS；根节点矩阵用 VecMath.createZupToYupPacked4()，
                                   translation 设为 (x, z, −y)；COLOR_0 为 normalized uint8，按需 RGBA/BLEND；
                                   _FEATURE_ID_n 转为 EXT_mesh_features；applyQuantization() 用 glTF-Transform quantize(16 bit)
                                   并加上 KHR_mesh_quantization（required）
    PntsPointClouds.ts             把 pnts 包装成 ReadablePointCloud
  tools/migration/TileFormatsMigrationPnts.ts   convertPntsToGlb：pnts 转 glb，batch table 转为 property table / per-point 属性
  tools/tilesetProcessing/
    TilesetJsonCreator.ts          createTilesetFromContents：叶子 GE 取默认值，父 GE = 2·max(子 GE)（启发式），根 refine 设为 ADD；
                                   computeTransformFromCartographicPositionDegrees 用 eastNorthUpToFixedFrame 计算 ENU→ECEF
    TilesetMerger / TilesetCombiner / TilesetUpgrader / BasicTilesetProcessor
    BoundingVolumes.ts / external/dito.ts   紧致 OBB（Esri DiTO 算法）
  tools/packageServer/PackageServer.ts   可直接服务目录、.3tz 或 .3dtiles（按需加 gzip Content-Encoding）
```

**要点：**
- 这个仓库**没有切片器**，`createTilesetJson` 只是把现成内容文件拼成一个扁平树。
- `specs/data/tilesetProcessing/implicitProcessing/` 是现成的 implicit 小样本（QUADTREE，subtreeLevels=2，availableLevels=4）。本机用自写脚本 `parse_subtree.py` 解析后结果如下：根 subtree 的 childSubtree 位串为 `0001001001001000`，置位的是第 3、6、9、12 位，对应 Morton 解码后的 (1,1)、(2,1)、(1,2)、(2,2)，与文件名 `2.1.1`、`2.2.1`、`2.1.2`、`2.2.2` 一致；内容文件共 21 个，与 5 + 4×4 吻合。**规范的 Morton 例子全部通过。**
- `MortonOrder.encode3D` 每轴只有 10 bit，对应局部层级 ≤10。只要 Morton 只在 subtree 内部使用（subtreeLevels ≤ 10），就不会越界。全局坐标直接用 (x,y,z) 整数，不做 Morton。

### 2.3 cdb-to-3dtiles 关键实现

| 文件 / 函数 | 作用 | 可借鉴或需规避 |
|---|---|---|
| `CDBTo3DTiles/src/CDBTile.cpp`：`CDBTile(...)`、`createParentTile`、`createChildForNegativeLOD` | CDB LOD 取 −10…23。**负 LOD 是同一 GeoCell 的逐级粗化，是单子节点链；非负 LOD 是四叉树**（UREF/RREF 即行列） | "同范围多级概览链"可以用在超大山区的远景层 |
| `CDBTileset.cpp`：`insertTileRecursively` | 负层级只挂 1 个孩子；正层级按 `childIdx = y*2 + x` 分四叉，缺失的中间节点自动补建 | 稀疏树构建 |
| `CDBTileset.cpp`：`getFitTile` | 找到包含某经纬点的最深可用瓦片 | **地面高程和贴地查询**：无人机起降、模型放置时按最深可用 DEM 瓦片查高度 |
| `CDB.cpp`：`traverseModelsAttributes`、`queryElevationTiles` | 模型贴地；只在叶子节点查"最深地形"，避免地形继续细化后把模型埋进地下 | 贴地必须用最终最细 LOD 的高程 |
| `CDBTo3DTiles.cpp`：`addElevationToTilesetCollection` | 当前瓦片没有影像时，沿父链找影像，用 `indexUVRelativeToParent` 重映射 UV，带缓存 | 纹理 LOD 回退 |
| `CDBTo3DTiles.cpp`：`fillMissingPositiveLODElevation` | **REPLACE 细化时，只要有一个子节点存在，就把其余缺失象限用父网格切片补齐**，否则细化后会出现空洞 | REPLACE 的"完整子覆盖"不变量（§3.10） |
| `CDBElevation.cpp`：`createSimplifiedMesh` | `meshopt_simplify(indices, positions, targetIndexCount = 0.3·N, targetError = 0.01)`，误差按网格尺寸归一化；顶点改为 `positionRTC = p − aabb.center` | 网格 LOD 参数和 RTC 精度处理 |
| `TileFormatIO.cpp`：`convertTilesetToJson` | **`MAX_GEOMETRIC_ERROR = 300000`，每层减半，叶子取 0**，与数据完全无关 | **反例**：GE 必须用真实误差（米）计算 |
| `TileFormatIO.cpp`：`combineTilesetJson` | 每个 GeoCell 一个 tileset，根 tileset 是 ADD，孩子用 region 引用外部 tileset | "瓦片集的瓦片集"：每个区域或架次一个子集 |
| `TileFormatIO.cpp`：`writeToB3DM` / `writeToI3DM` | 写 `RTC_CENTER`、batch table，JSON 按 8 字节对齐补空格 | 二进制对齐规则 |
| `Gltf.cpp` | 根节点矩阵 `{1,0,0,0, 0,0,-1,0, 0,1,0,0, 0,0,0,1}`（z-up→y-up），加 `KHR_materials_unlit` | 与规范做法一致 |
| `CDBGeoCell.cpp`：`getLongitudeExtentInDegree` | 纬度越高，经度宽度越大（1、2、3、4、6、12°） | 全球分区时面积大致均衡；局部 ENU 世界可以不考虑 |

### 2.4 potree23dtiles 关键实现与缺陷

| 文件 / 函数 | 作用 | 问题 |
|---|---|---|
| `data/potreebin.py`：`PotreeBin.read_hierarchy` | 解析 Potree 2.0 `hierarchy.bin`，每节点 22 字节：`type u8, childMask u8, numPoints u32, byteOffset u64, byteSize u64`；type=2 表示代理节点，要按 `hierarchy_byte_offset/size` 再读一块 | 可作参考。**不支持 BROTLI 编码的 octree.bin** |
| `core/potree23dtiles.py`：`read_bin` / `read_las` | 读节点点数据（int32 × scale + offset） | — |
| `covert_ecef` | 投影坐标经 pyproj 转 ECEF，再乘 `inv_wgs84(trans_mat)` 转到根 ENU 局部坐标 | 可作参考。`pj.Proj(init=...)` 已废弃 |
| `core/proj.py`：`wgs84_trans_matrix` | 构造 ENU→ECEF 矩阵（旋转列为 E、N、U，平移为原点 ECEF），转置后按列主序写入 `root.transform` | 与 3d-tiles-tools 的 `eastNorthUpToFixedFrame` 等价 |
| `visit_node` | 每个节点输出一个 pnts；`geometricError = bbox_half_max / 16`；包围盒取立方（half.max） | **点数 <4 的节点 `continue` 会把整棵子树丢掉**；GE 是拍脑袋的启发式 |
| `data/pnts.py`：`Pnts.write` | pnts 写入器 | **JSON 按 4 字节补齐，二进制段不补齐**，违反规范"8 字节边界"要求；`rgb=None` 时会崩；类级字典被原地修改 |
| `core/cmd.py` / `las23dtiles.py` | 调用 `PotreeConverter.exe` | 只能在 Windows 上跑 |

**子节点索引顺序是最容易踩的坑。** Potree 的 `createChildAABB` 用 `(x<<2)|(y<<1)|z`，即 bit2 是 x（`refs/web3d/potree/src/modules/loader/2.0/OctreeLoader.js:293`）。3D Tiles 的 Morton 码用 `x | y<<1 | z<<2`，即 bit0 是 x。r06 也提到 Open3D 是 `x+2y+4z`。potree23dtiles 生成的是显式 children 数组，所以没有暴露这个问题；一旦改成 implicit，就必须交换 bit0 和 bit2（§3.9）。

### 2.5 补充对照：CesiumJS / 3DTilesRendererJS / py3dtiles

**CesiumJS**（`refs/web3d/cesium/packages/engine/Source/`）
- `Scene/Cesium3DTile.js:953` `getScreenSpaceError`：`error = GE·height / (distance·sseDenominator)`；可选动态 SSE `error -= fog(distance, density)·factor`；最后 `error /= pixelRatio`。正交投影时 `error = GE / pixelSize`。GE=0 直接返回 0；distance 用 `max(d, EPSILON7)` 防止除零。
- `Core/PerspectiveFrustum.js:206`：`sseDenominator = 2·tan(fovy/2)`。
- `Scene/Cesium3DTilesetBaseTraversal.js`：DFS。**REPLACE 要等子节点都加载完才细化**（`updateAndPushChildren` 里 `refines = refines && child.contentAvailable`），空瓦片走 `executeEmptyTraversal`。ADD 瓦片总是被选中并加载。子节点按距离排序以利用 early-Z。
- `Scene/Cesium3DTilesetTraversal.js`：
  - `canTraverse`：`sse > memoryAdjustedSSE` 时才遍历子节点。
  - **`meetsScreenSpaceErrorEarly`**：父瓦片为 ADD 时，用父 GE 配合子包围盒计算 SSE，已满足就剔除该子节点。
  - `loadTile` 配合 `isOnScreenLongEnough`：`movementRatio = 60·|Δcam| / diameter`，≥1 时移动中不发请求。
  - `priorityDeferred` 配合 `foveatedTimeDelay = 0.2 s`。
- `Scene/Cesium3DTileset.js`：默认 `maximumScreenSpaceError = 16`，`cacheBytes = 512 MB`，`maximumCacheOverflowBytes = 512 MB`。`processTiles` 中内存超限时 `memoryAdjustedSSE ×= 1.02`，低于 `cacheBytes` 时 `/= 1.02` 并回落到下限。`dynamicScreenSpaceErrorDensity = 2e-4`、`Factor = 24`、`HeightFalloff = 0.25`，相机越贴地平线，`horizonFactor` 越大。`foveatedConeSize = 0.1`，`progressiveResolutionHeightFraction = 0.3`，`skipLevelOfDetail` 默认关闭（开启时 `baseSSE = 1024`、`skipSSEFactor = 16`）。
- `Scene/Model/PointCloudStylingPipelineStage.js` 与 `Shaders/Model/PointCloudStylingStageVS.glsl`：点尺寸衰减 `min(GE/depth · H/sseDenominator, maximumAttenuation)`。GE=0 时用 `baseResolution`，或估算 `cbrt(bboxVolume/points)`。`PointCloudShading` 默认值：`attenuation=false`、`geometricErrorScale=1`、`eyeDomeLighting=true`（强度 1、半径 1）。
- `Scene/GaussianSplat3DTileContent.js`：支持 `KHR_gaussian_splatting` 和 `KHR_gaussian_splatting_compression_spz_2`，并已移除旧的 `KHR_spz_gaussian_splats_compression`。

**3DTilesRendererJS v0.5.3**（`.cache/research/r10/3DTilesRendererJS/src/`）
- `core/renderer/tiles/traverseFunctions.js`：四遍遍历 `markUsedTiles → markUsedSetLeaves → markVisibleTiles → toggleTiles`。
  - ADD 子节点跳过条件：`tile.error·(parentGE/tileGE) ≤ errorTarget`，与 Cesium 的 early 优化等价，并注明"规范未规定"。
  - REPLACE 在子节点全部不在视锥时，父节点也视为不可见，但仍标记为 used，防止闪烁。
  - `resetFrameState` 实现了 2.0 的"GE ≥ 最近可条件细化祖先的 GE → 无条件细化"。
- `three/renderer/tiles/TilesRenderer.js:561`：`sseDenominator = (2/P[5]) / H`，其中 `P[5] = 1/tan(fovy/2)`，可以兼容自定义相机和缩放；`calculateTileViewError` 在多相机时取视锥内最大误差。
- `core/renderer/tiles/TilesRendererBase.js`：默认 `errorTarget = 16`；实验参数 `errorFalloff` 公式为 `error −= falloff·(1−e^{−(d·density)²})`，`density` 默认 2e-4；下载队列 `maxJobsPerOrigin = 25`，解析队列 `maxJobs = 5`；`loadSiblings = true`。
- `core/renderer/utilities/LRUCache.js`：`minSize=6000`、`maxSize=8000`、`minBytesSize=0.3 GB`、`maxBytesSize=0.4 GB`、`unloadPercent=0.05`；卸载顺序依次为：最近未用、更深层优先（保护父节点）、加载早期阶段、外部 tileset 最后。
- `three/plugins/potree/PotreePlugin.js`（**2026-09-18 新增**）：在内存中合成 ADD tileset，`root.geometricError = metadata.spacing`，子节点 GE 减半，`tiles.errorTarget = 1`，点尺寸 = `spacing·1.7·pointScale`。`_updateActiveNodesTexture` 实现了 Potree 的 VNT（可见节点纹理）自适应点尺寸。
- `three/plugins/pointcloud/PointCloudMaterial.js`：GLSL `getActiveDepth()` 在节点纹理上逐层下行，`worldSize = size / 2^(depth+lodOffset)`，`gl_PointSize = worldSize·scale·P[1][1] / −z`，下限 `uMinPointSize` 默认 2，带 EDL。**它通过 `onBeforeCompile` 注入 GLSL，与 WebGPURenderer（NodeMaterial/TSL）不兼容。**
- `core/plugins/SUBTREELoader.js`：客户端 implicit tiling 实现（位流、Morton、child subtree）。

**py3dtiles 12.1.1**（本机在 `.cache/research/r10/venv` 实测）：`py3dtiles convert "San Francisco_sampled_5m.ply" --spec-version 1.1 --jobs 8` 耗时 26.8 s，生成 747 个 glb、74 MB，平均 15.1 B/点（float32 位置 + uint8 RGB，数据无颜色时全填 0，点云呈黑色）。`point_tiler.py` 让根 REPLACE、其余 ADD；`node.py:460` 中 `geometric_error = 10·spacing/scale`，子 tileset 带 ×10 缩放的 transform。**validator 报 748 个 ERROR**，每个 glb 都有 `COLOR_0 {VEC3, UNSIGNED_BYTE}` 格式非法（缺 normalized，且 3 字节元素没有 4 字节对齐）。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 SSE 公式：统一三家实现

**标准公式**（Cesium `Cesium3DTile.getScreenSpaceError`、3DTilesRendererJS `calculateTileViewError`）：

```text
透视：  SSE_px = GE · H_px / ( d · 2·tan(fovy/2) )        d = max(dist(eye, BV), ε)，eye 在 BV 内时 SSE = ∞
正交：  SSE_px = GE / pixelSize,   pixelSize = max(frustumH / H_px, frustumW / W_px)
可选（Cesium 动态 SSE / 3DTilesRendererJS errorFalloff，用于贴地平线视角，远处主动降质）：
        SSE_px −= F · (1 − exp(−(ρ·d)²)),   F = 24, ρ = 2e-4 · horizonFactor
          horizonFactor = (1 − |dir·up|) · (1 − t),  t = clamp((camH − h_close)/(h_far − h_close), 0, 1)
细化判据：SSE_px > τ（errorTarget / maximumScreenSpaceError）
```

- `H_px` 用 **CSS 像素**（Cesium 最后除以 `pixelRatio`）。这样 LOD 与 DPR 无关，另外单独暴露一个 "quality" 系数。
- Three.js 中直接取 `sseDenom = 2 / camera.projectionMatrix.elements[5]`，这是 3DTilesRendererJS 的做法，同时覆盖 zoom 和自定义投影。
- `dist(eye, BV)` 用 AABB 最近点距离；OBB 先把 eye 变换到盒子局部坐标再算。

**三家实现是同一个公式：**

| 实现 | 表达式 | 与标准 SSE 的关系 |
|---|---|---|
| Cesium | `GE·H/(d·2tan)` | 标准式 |
| 3DTilesRendererJS | `GE / (d · (2/P5)/H)` | 代数上相同 |
| Potree `Potree_update_visibility.js` | `weight = r · (H/2) / (tan(fovy/2) · d)`；`minimumNodePixelSize = 150`；另有 `distance < r => weight = ∞` | 即 `GE := r`（包围球半径）的 SSE |

**点云 GE 的语义化定义（本项目采用）。** ADD 细化下，渲染到第 L 层时某区域的点间距约为 `s_L`。所以令 `GE_L = s_L = s_0/2^L`，其中 `s_0 = cube/128`，与 PotreeConverter `indexer.cpp:1440` 一致。这样 **SSE 的含义就是"屏幕上相邻点之间的像素间隙"**，τ 可以直接理解为"允许的点间隙像素数"：
- 3DTilesRendererJS `PotreePlugin.init`：`tiles.errorTarget = 1`。
- Potree：`r = (√3/2)·cube ≈ 110.9·s`，因此 `150 px / 110.9 ≈ 1.35 px`。
- **推荐 τ 取值**：点云默认 1.5 px，范围 [0.75, 8]；网格按 Cesium 16 px；3DGS 建议 4–8 px（待 3DGS 单元确认）。

### 3.2 瓦片选择遍历

#### 3.2.1 两种经典遍历对比

| | Cesium Base（DFS + 标记） | 3DTilesRendererJS（四遍） | Potree（最佳优先 + 预算） |
|---|---|---|---|
| 顺序 | 深度优先，子节点按距离排序 | 递归 DFS | **按 weight 最大堆** |
| 停止条件 | SSE ≤ memoryAdjustedSSE | error ≤ errorTarget，或 maxDepth | 超出点预算，或 `pixelRadius < minNodePixelSize` |
| REPLACE 空洞 | 子节点全部加载完才细化；`loadSiblings` | 父节点保持 active，直到子节点就绪（kick 机制） | 不涉及（只有 ADD） |
| ADD | 总是选中；early cull | 跳过已满足的子节点 | 天然 ADD |
| 预算 | 无硬点预算（内存超限时调整 SSE） | 无硬点预算（LRU 字节预算） | **有硬点预算** |

本项目需要同时满足"点数上限硬保证"和"标准语义"，因此采用**以 SSE 为优先级的最佳优先遍历 + 硬点预算 + ADD early cull**，也就是 Potree 的队列加 Cesium 的误差度量。

#### 3.2.2 伪代码（TS，可直接实现；Python 参考实现见 `.cache/research/r10/sse_select.py`，已跑通）

```ts
// ---------- 数据接口（与存储格式解耦：ImplicitTilesSource / Potree2Source 各实现一份） ----------
interface NodeKey { L: number; x: number; y: number; z: number }            // 3D Tiles implicit 坐标
interface NodeInfo {
  box: AABB;               // 由根盒按层级直接计算（§3.5.4）
  ge: number;              // = ge0 / 2^L（可被 TILE_GEOMETRIC_ERROR 元数据覆盖）
  hasContent: boolean;
  pointCount: number;      // subtree 瓦片元数据 pointCount（下载前已知，用于预算）
}
interface NodeSource {
  info(k: NodeKey): NodeInfo | 'pending' | null;   // 'pending' 表示所在 subtree 未加载，null 表示不可用
  children(k: NodeKey): NodeKey[];                 // 按 tile/childSubtree 可用性过滤
}

// ---------- 每帧（或相机变化且 ≥ 50 ms 间隔时）运行 ----------
function sse(ge: number, d: number, v: View): number {
  if (ge === 0) return 0;
  if (d <= 1e-7) return Infinity;
  return (ge * v.heightCss) / (d * v.sseDenom);            // v.sseDenom = 2 / P[5]
}

function selectNodes(src: NodeSource, v: View, c: LodController): Selection {
  const heap = new MaxHeap<{ k: NodeKey; pri: number }>(e => e.pri);
  heap.push({ k: ROOT, pri: Infinity });
  const render: NodeKey[] = [], want: NodeKey[] = [], wantSubtrees: NodeKey[] = [];
  let points = 0;

  while (heap.size) {
    const { k } = heap.pop()!;
    const info = src.info(k);
    if (info === 'pending') { wantSubtrees.push(k); continue; }
    if (!info || !v.frustum.intersectsBox(info.box)) continue;

    if (info.hasContent) {
      if (points + info.pointCount > c.pointBudget) break;   // 硬预算：剩余候选的优先级都更低
      points += info.pointCount;
      (cache.isReady(k) ? render : want).push(k);            // ADD：不必等祖先或兄弟节点，可以乱序显示
    }
    const d = distToBox(v.eye, info.box);
    const e = sse(info.ge, d, v) - c.falloff(d);             // 可选动态 SSE
    if (e <= c.errorTarget) continue;                        // 本节点已足够细

    for (const ck of src.children(k)) {
      const cbox = boxOf(ck);
      if (!v.frustum.intersectsBox(cbox)) continue;
      const cd = distToBox(v.eye, cbox);
      if (sse(info.ge, cd, v) <= c.errorTarget) continue;    // ADD early cull（Cesium meetsScreenSpaceErrorEarly）
      const pri = sse(info.ge / 2, cd, v) * v.foveation(cbox); // 子节点自身 SSE × 注视点权重（视轴锥外 ×0.5）
      heap.push({ k: ck, pri });                                // 可选：对"已在缓存"的节点略加分，减少选择抖动
    }
  }
  return { render, want, wantSubtrees, points };            // want 已按优先级有序，直接喂给请求队列
}
```

**说明**
- 硬预算用"`break` 截断"，前提是 `pointCount` 在下载前可知。3D Tiles 本身没有这个信息，所以要放进 subtree 的 `tileMetadata`（§3.6）。Potree 的 `hierarchy.bin` 自带 numPoints。
- `render` 只包含已就绪的节点。ADD 天然容忍缺块，父节点没到时子节点也可以先显示，但请求优先级上父节点更高，因为它先出堆。
- 多视图（主视图 + FPV 画中画）时，对每个节点取各相机 SSE 的最大值（3DTilesRendererJS 的多相机做法），两个视图共享同一个缓存和预算。

#### 3.2.3 REPLACE（网格和 3DGS 图层）遍历要点

沿用 Cesium Base 语义：
1. 所有**可见**子节点都已加载（不可见子节点也要加载，见 `loadSiblings`）才允许细化，否则继续显示父节点。
2. 空瓦片（没有内容）不阻塞细化：子节点边到边显示。
3. GE ≥ 最近可条件细化祖先 GE 的瓦片（外部 tileset 或 implicit 根）无条件细化，这是 2.0 规则，3DTilesRendererJS 已实现。
4. 构建期保证"完整子覆盖"（§3.10）。

#### 3.2.4 本机模拟（SF 瓦片集，1280×720，fov 60°，`sse_select.py`）

| 视角 | τ=1 px | τ=2 px | τ=4 px | τ=8 px |
|---|---|---|---|---|
| 俯瞰：水平距 1.27 km，高 600 m，斜距约 1.4 km | 21 瓦片 / 0.35M 点 | 5 / 0.08M | 1 / 0.02M | 1 / 0.02M |
| 无人机：120 m AGL，水平距 212 m，俯角约 30° | 54 / 0.87M（预算 0.5M 时截断为 26 / 0.49M） | 21 / 0.37M | 11 / 0.21M | 5 / 0.08M |
| 街景：15 m AGL，近平视 | 17 / 0.24M | 13 / 0.20M | 9 / 0.14M | 5 / 0.08M |

结论：
- 720p 下 **τ=1–2 px 时通常需要 0.2–0.9M 点**。**点预算默认 1M**（Potree 默认 `pointBudget = 1,000,000`）已经足够，1080p 约 2M。
- **headless 软件 WebGL2** 起步建议 τ=2 px、预算 300k，再交给 §3.3 的控制器自适应。
- 根节点只有 2.04 万点（约 245 KB），**首帧可以在一个请求后出图**。

### 3.3 自适应控制器：帧时反馈与内存反馈（"疏密自动调节"的核心）

```ts
class LodController {
  errorTarget = 1.5;  minET = 0.75; maxET = 8;          // px（点云：GE = spacing）
  pointBudget = 1_000_000; minPB = 150_000; maxPB = 5_000_000;
  targetFrameMs = 1000 / 60;  ema = 16;
  moving = false; idleSince = 0;

  onFrame(frameMs: number, gpuBytes: number, now: number, backlog: number) {
    this.ema = 0.9 * this.ema + 0.1 * frameMs;
    const hi = this.targetFrameMs * 1.15, lo = this.targetFrameMs * 0.80;
    if (this.ema > hi) {                                  // 过载：快速降质（×1.05 / ×0.93）
      this.errorTarget = Math.min(this.errorTarget * 1.05, this.maxET);
      this.pointBudget = Math.max(this.pointBudget * 0.93, this.minPB);
    } else if (this.ema < lo && backlog === 0) {          // 有余量且没有积压请求：缓慢升质（Cesium ÷1.02）
      this.errorTarget = Math.max(this.errorTarget / 1.02, this.minET);
      this.pointBudget = Math.min(this.pointBudget * 1.02, this.maxPB);
    }                                                     // 中间区间不动（滞回，防振荡）
    if (gpuBytes > GPU_BYTES_MAX) this.errorTarget *= 1.02;   // Cesium memoryAdjustedScreenSpaceError
  }
  // 交互期降级，停下后恢复（Potree 与 Cesium progressiveResolution 的思路）
  effectiveET(now: number) {
    return this.moving ? this.errorTarget * 2 : this.errorTarget;  // 静止 300 ms 后自动回到全质量
  }
}
```

参数建议：
- 目标帧率：桌面 60；软件渲染 30（`targetFrameMs = 33`）。
- 滞回区间 [0.8, 1.15] × target。
- 过载时的 ×1.05 / ×0.93 步长约 10 帧内见效；恢复用 ×1.02，避免抖动。
- `GPU_BYTES_MAX`：桌面 512 MB（Cesium `cacheBytes`），软件渲染 128 MB。

### 3.4 加载调度、解码与缓存

| 机制 | 规则 | 参数与来源 |
|---|---|---|
| 请求优先级 | 用 `selectNodes` 的出堆顺序：先视锥内，SSE 降序，距离升序，层级浅者优先 | 3DTilesRendererJS `errorPriorityCallback` |
| 并发 | HTTP/1.1 下 6；HTTP/2 下 16–25；subtree 请求优先于内容请求 | 3DTilesRendererJS `maxJobsPerOrigin=25`、`parseQueue.maxJobs=5` |
| 取消 | 每帧用新的 `want` 集合对比在途请求，不再需要的调用 `AbortController.abort()` | 3DTilesRendererJS 0.5.2 支持 abort signal |
| 移动中暂缓请求 | `60·‖Δeye‖/diameter ≥ 1` 时本帧不请求该瓦片 | Cesium `cullRequestsWhileMovingMultiplier=60` |
| 注视点延迟 | `foveatedFactor = 1 − abs(cos∠(视轴, 瓦片方向))` 大于 `0.1·(1−cos(fov/2))` 的瓦片（即视轴锥外），在相机停止移动未满 0.2 s 时不请求 | Cesium `foveatedConeSize=0.1`、`foveatedTimeDelay=0.2` |
| 解码 | Worker 池（2–4 个）；最小 GLB 解析（JSON chunk → accessor → TypedArray 视图，零拷贝），transferable 回主线程 | 不要走 GLTFLoader（主线程开销大） |
| 上传预算 | 每帧最多上传 4 MB 或 8 个节点，超出的顺延到下一帧 | 防止首帧和快速飞行时出现卡顿尖峰 |
| LRU | 字节预算（桌面 512 MB，软件 128 MB）；每帧最多卸载 5%；**先卸深层节点**，因为 ADD 需要祖先；本帧 used 的节点不卸 | 3DTilesRendererJS `LRUCache`、Cesium `Cesium3DTilesetCache` |
| 渐显 | 新节点 200–300 ms 内点尺寸从 0 渐变（或用抖动 alpha），避免排序和透明混合 | 3DTilesRendererJS `TilesFadePlugin` |

### 3.5 Implicit tiling 客户端（移植 3d-tiles-tools 与 SUBTREELoader）

#### 3.5.1 subtree 二进制布局（小端）

```text
offset  size  field
0       4     magic = 0x74627573 ('subt')
4       4     version = 1
8       8     jsonByteLength   (uint64，含尾部空格填充，使 JSON 结束于 8 字节边界)
16      8     binaryByteLength (uint64，含 0 填充；为 0 表示没有内部 buffer)
24      J     JSON：{buffers[{byteLength,(uri)}], bufferViews[{buffer,byteOffset,byteLength}],
                     tileAvailability{constant|bitstream,availableCount},
                     contentAvailability[{...}], childSubtreeAvailability{...},
                     propertyTables[{class,count,properties{p:{values:bvIdx}}}], tileMetadata: idx}
24+J    B     内部 buffer（第一个 buffer 没有 uri）
约束：bufferView.byteOffset 按 8 对齐；bufferView.byteLength **不含**填充（本单元原型踩过这个坑，validator 报 SUBTREE_AVAILABILITY_INCONSISTENT）
```

#### 3.5.2 位流与索引公式

```text
N = 8（octree）；SL = subtreeLevels
tile/content 位流长度 = (N^SL − 1)/(N − 1) 位；childSubtree 位流长度 = N^SL 位；字节数 = ceil(bits/8)
bit(i) = (bytes[i >> 3] >> (i & 7)) & 1                          // LSB-first
morton3(x,y,z) = Σ_b  x_b<<(3b) | y_b<<(3b+1) | z_b<<(3b+2)      // bit0=x, bit1=y, bit2=z
levelOffset(l) = (8^l − 1)/7
tileIndex(local l, lx,ly,lz) = levelOffset(l) + morton3(lx,ly,lz)
childSubtreeIndex = morton3(相对子树根、深度为 SL 那一层的局部坐标)
全局 <-> 局部：subtreeRootLevel = floor(L/SL)·SL；d = L − subtreeRootLevel；
             subtree 根坐标 = (x>>d, y>>d, z>>d)；局部坐标 = (x & (2^d−1), ...)
可用瓦片的元数据行号 = 该瓦片之前（按位流顺序）置 1 的位数，即前缀和 cumsum(tileBits)[idx] − 1
```

#### 3.5.3 `info(L,x,y,z)` 与 `children()`（与 `sse_select.py` 中的 `ImplicitOctree` 一致）

```ts
function info(L, x, y, z) {
  const sL = Math.floor(L / SL) * SL, d = L - sL;
  const st = subtreeCache.get(sL, x >> d, y >> d, z >> d);   // 未加载时返回 'pending' 并排入请求队列
  if (!st) return 'pending';
  const m = (1 << d) - 1, idx = levelOffset(d) + morton3(x & m, y & m, z & m);
  if (!st.tile(idx)) return null;
  return { hasContent: st.content(idx), pointCount: st.pointCount[st.rank(idx)],
           ge: GE0 / 2 ** L, box: boxAt(L, x, y, z) };
}
function children(L, x, y, z) {
  if (L + 1 >= availableLevels) return [];
  const out = [];
  for (let c = 0; c < 8; c++) {                                // c 的位序：x | y<<1 | z<<2
    const k = [L + 1, 2*x + (c & 1), 2*y + ((c >> 1) & 1), 2*z + ((c >> 2) & 1)];
    if ((L + 1) % SL === 0) {                                   // 子节点是下一个 subtree 的根
      const parent = subtreeCache.get(L + 1 - SL, k[1] >> SL, k[2] >> SL, k[3] >> SL);
      const mm = (1 << SL) - 1;
      if (!parent.child(morton3(k[1] & mm, k[2] & mm, k[3] & mm))) continue;
    }
    const inf = info(...k); if (inf !== null) out.push(k);     // 'pending' 也返回，由遍历负责触发加载
  }
  return out;
}
```

#### 3.5.4 包围盒推导

按规范建议，**每一层直接从根盒计算，不要逐级二分**，以保证数值稳定。3d-tiles-tools 的 `BoundingVolumeDerivation.deriveBoundingBox` 做法相同：

```text
tileScale = 2^−L
center = rootCenter + rootHalfAxes · ( −1 + (2x+1)·tileScale, −1 + (2y+1)·tileScale, −1 + (2z+1)·tileScale )
halfAxes = rootHalfAxes · diag(tileScale, tileScale, tileScale)        // quadtree 的 z 分量不缩放
轴对齐时简化为：min = rootMin + (x,y,z)·size_L，size_L = rootSize / 2^L
```

### 3.6 离线切片器：PLY → 3D Tiles 1.1 implicit octree

原型 `.cache/research/r10/wp_tiler.py` 约 250 行 numpy，**已通过 validator**。

#### 3.6.1 算法

```text
输入：xyz(float64, ENU 米, Z-up，已完成 §3.8 的单位与轴向归一化)，可选属性（法线、类别、强度、first_seen）
1. 立方根盒：origin = min(xyz)，size = max(extent)·1.0001；center = origin + size/2
2. s0 = size/128（PotreeConverter 约定）；数据间距 s_data ≈ sqrt(A/N)，A 由细体素占据数 × c² 估计
   maxLevel = ceil(log2(s0/s_data))                             // SF：s0=5.78 m，s_data≈0.30 m，maxLevel=5
3. 分层无放回体素采样（ADD 语义，与 Potree random sampler 同构）：
     order = 随机置换(N)
     for L in 0..maxLevel-1:
        s_L = s0/2^L；key = cell(xyz[remaining], s_L)
        free = key ∉ cell(xyz[accepted], s_L)                  // 已被更粗层占据的格子不再收点
        每个空闲格子取第一个点（随机点）→ level=L，并移出 remaining
     剩余点全部归入 maxLevel
     叶子合并（建议加入，原型未实现）：某节点子树总点数 < MIN_NODE(≈8k) 时，子孙点全部并入该节点，
       并把它的子节点标为不可用。目的是减少小文件（SF 原型 L5 有 1061 个节点，中位数只有 1.5k 点）
4. 节点坐标：(L, floor((p−origin)/(size/2^L)))，clamp 到 [0, 2^L−1]
5. 按 (L<<60 | morton) 排序，逐节点写 glb（§3.6.2）
6. tile 可用性 = 有内容的节点 ∪ 它们的全部祖先；content 可用性 = 有内容的节点
7. 按 SL 切分 subtree：写三类位流（全 0 或全 1 时写 constant），外加 propertyTable "tile.pointCount"（UINT32）
8. tileset.json：asset 1.1、schema、tileset GE = 4·s0、root{box, GE=s0, refine=ADD, content 模板, implicitTiling}
```

#### 3.6.2 glb 字节布局（每点 12 B，无颜色时）

| 部分 | 内容 | 说明 |
|---|---|---|
| 场景 | node0 `matrix = Z-up→Y-up` → node1 `{translation: nodeMin−center, scale: [size_L]*3, mesh}` | 与规范"z-up 数据"的推荐做法一致，运行时抵消 |
| POSITION | `UNSIGNED_SHORT normalized VEC3`，bufferView `byteStride = 8`（第 4 分量是填充，可以放 intensity 或 first_seen） | 需要 `KHR_mesh_quantization`，并写入 `extensionsRequired`；**`min/max` 必须是整数（未归一化值）**，否则 validator 报错 |
| NORMAL | `BYTE normalized VEC3`，`byteStride = 4` | 可选；无颜色数据靠法线和 EDL 提供可读性 |
| COLOR_0（有颜色时） | `UNSIGNED_BYTE normalized VEC3`（stride 4）或 VEC4 | **必须 normalized，必须 4 字节对齐**（py3dtiles 在这里违规），而且是 linear 值（sRGB 要先转换） |
| 语义类别（V0.2） | `_FEATURE_ID_0 UNSIGNED_BYTE` + `EXT_mesh_features{featureIds:[{featureCount, attribute:0, propertyTable:0}]}` + `EXT_structural_metadata` 类名表 | 与 3d-tiles-tools `assignFeatureIdAttributes` 一致 |

**实测结果（San Francisco，5,000,091 点）**

| 指标 | 数值 |
|---|---|
| 立方根盒 | 740.18 m |
| s0 / 估计数据间距 | 5.78 m / 0.30 m |
| maxLevel / availableLevels | 5 / 6 |
| subtreeLevels / subtree 数 | 3 / 65（根子树覆盖 L0–L2，另 64 个覆盖 L3–L5） |
| 各层节点数与点数 | L0 1/20,399；L1 4/62,572；L2 16/267,631；L3 64/1,034,049；L4 257/1,991,289；L5 1061/1,624,151 |
| 内容节点 | 1403；每节点点数中位数 1525，最大 41,702 |
| 体积 | 61.3 MB 内容；68 MB 共 1469 个文件；12.27 B/点 |
| 耗时 | 60 s，其中分层采样 45 s，瓶颈是 `np.isin`，可以换成排序合并或 numba |
| **3d-tiles-validator 0.6.1** | **0 errors / 0 warnings / 0 infos** |

**原型调通过程中踩到的 3 个坑（validator 实报，实现者直接照此避免）**
1. subtree `bufferView.byteLength` 写成了填充后的长度，报 `SUBTREE_AVAILABILITY_INCONSISTENT`（"73 bits (10 bytes) … 16"）。
2. `extensionsUsed: []` 空数组，报 `ARRAY_LENGTH_MISMATCH`。没有扩展时不要写这个字段。
3. normalized 整数 accessor 的 `min/max` 写成了 [0,1] 浮点，报 "is not a 'integer'"。

### 3.7 点尺寸：Web 端"疏密可读性"

**方案 A（MVP，Cesium 衰减式，逐瓦片 uniform）：**

```glsl
// uniform: uGE = 瓦片 GE（= 该层 spacing）× geometricErrorScale；uDepthMul = H_px / (2·tan(fovy/2))
float px = min(uGE / (-mvPosition.z) * uDepthMul * uCoverage, uMaxPx);   // uCoverage ≈ 1.7（Potree SPACING_COVERAGE_FACTOR）
gl_PointSize = max(px, uMinPx);                                          // uMinPx = 1–2，uMaxPx = errorTarget·4 或 ≤ 硬件上限
```

缺点：ADD 下同一区域里父层点用 `s_L`、子层点用 `s_{L+1}`，粗点会更大、更显眼。

**方案 B（V0.2，Potree VNT / 3DTilesRendererJS `LOD_SIZING`，逐点）：**
- 每帧把 active 节点编码进 RGBA8UI 纹理，四个字节依次为：`childMask`、首子节点偏移的高字节和低字节、`lodOffset`，按层级和 key 排序，使兄弟节点连续。
- 顶点着色器里按点位置自顶向下查找最深的 active 节点，`worldSize = size0 / 2^(depth+lodOffset)`。
- 然后 `gl_PointSize = worldSize·(H/2)·P[1][1] / −z`（three 的 `scale = H/2`）。
- 移植源：`PotreePlugin._updateActiveNodesTexture`、`PointCloudMaterial.getActiveDepth`。**WebGPU 路径需要改写为 TSL**：WebGPU 的 point-list 恒为 1 px，要用实例化 quad 或 sprite，在 vertex 里算尺寸。
- **注意八分体顺序**：它的 `octant = 4·ix + 2·iy + iz` 是 Potree 顺序。用 3D Tiles 数据时要换成 `ix + 2·iy + 4·iz`，或者在编码纹理时统一换算。

### 3.8 坐标系、单位与精度

| 层 | 约定 | 说明 |
|---|---|---|
| World 内部 | **ENU，米，Z-up，右手系**；原点为 RTK 或指定点 | 与 3D Tiles 1.1 本地系、ROS REP-103 一致；PX4 NED 在仿真边界转换 |
| 3D Tiles 1.1 本地 tileset | Z-up；不写 `root.transform` 时就是纯局部世界（规范允许，例如"电厂"） | V0.1 |
| 地理配准（V0.5） | `root.transform = ENU→ECEF`（`eastNorthUpToFixedFrame`），元数据 `TILESET_CRS_GEOCENTRIC = "EPSG:4978"` | 只含旋转和平移，**不含缩放** |
| glTF 内容 | Y-up：根节点写 Z-up→Y-up 矩阵，与运行时 Y→Z 抵消 | 与规范、cdb-to-3dtiles、3d-tiles-tools 一致 |
| three.js 场景 | Y-up：`WorldLayer.rotation.x = −π/2`，`WorldLayer.position = −center`（使用 extras 中的 `enuOriginOffset`） | DroneLayer、SensorLayer 都挂在 WorldLayer 下，共用 ENU 坐标 |
| 3D Tiles 2.0（草案） | 本地系改为 Y-up，推荐用 `EXT_georeference` | 只需要改写出器的一处矩阵 |

**量化精度**（uint16，每节点局部）：

| 节点尺寸 | 量化步长 = size/65535 |
|---|---|
| 7,736 m（Shanghai 根） | 0.118 m（远小于该层 GE 60 m） |
| 740 m（SF 根） | 11.3 mm |
| 23 m（SF L5） | 0.35 mm |

- float32 世界坐标在 8 km 处的 ULP ≈ 0.98 mm，本地 ENU 世界（<10 km）可以直接用；**不要用 float16**（r01 已实测 1–4 m 误差）。
- 法线 int8 的角度误差约 0.5°。
- 地球尺度（ECEF）下必须用 RTC：节点 translation 是 double，顶点保持局部坐标。

**UrbanScene3D 导入归一化（本机实测，`ply_stats.py`、`ply_axis2.py`）**

| 城市 | 点数 | 包围盒（文件单位） | 估计间距 | 判断 |
|---|---|---|---|---|
| New York | 5,000,065 | 2928×3167×292 | 1.40–1.65 m | Z-up，单位米 |
| San Francisco | 5,000,091 | 740×717×55 | 0.30–0.38 m | Z-up，单位米 |
| Shenzhen | 5,000,141 | 1848×1999×391 | 1.11 m | Z-up，单位米 |
| Shanghai | 5,000,202 | 7736×6211×646 | 1.88 m | Z-up，单位米；立方根盒 7.7 km |
| Chicago | 5,000,391 | **4.17×8.04×0.62** | 0.0029 | Z-up（法线 z 主轴占 67%），**单位疑似 km，需要 ×1000** |
| Suzhou | 4,999,857 | 4407×**155**×686 | 0.76 m | **轴向存疑**：y 向只有 155 m，但法线 z 主轴仅占 48%；需人工确认后决定是否交换 y/z |

以上 6 个 PLY 都**只有 xyz 和法线，没有 RGB**。所以渲染配色要靠高度色带、法线着色和 EDL，并在构建期烘焙一个 `_HEIGHT` 或类别属性。

### 3.9 Potree 2.0 <-> 3D Tiles implicit 映射

使用场景有两个：用 PotreeConverter 当切片器；或者复用 potree-core、three-loader 生态。

```text
Potree key: "r" d1 d2 … dL，每位 d_i ∈ [0,7]，d = (x<<2)|(y<<1)|z   （Potree OctreeLoader.createChildAABB）
3D Tiles:   (L, X, Y, Z)，子索引 m = x | (y<<1) | (z<<2)                （AVAILABILITY.adoc）
X = Σ_i ((d_i>>2)&1) << (L−i)，  Y = Σ_i ((d_i>>1)&1) << (L−i)，  Z = Σ_i (d_i&1) << (L−i)
m = ((d&1)<<2) | (d&2) | ((d>>2)&1)          // 即交换 bit0 与 bit2
GE_L = metadata.spacing / 2^L；refine = ADD；根盒 = metadata.boundingBox（PotreeConverter 输出为立方）
numPoints 取自 hierarchy.bin（22 B/节点），写入 subtree 的 tile.pointCount
octree.bin 节点数据（int32·scale + offset）重新量化为 uint16 节点局部坐标后写入 glb
```

运行时也可以不转换：3DTilesRendererJS `PotreePlugin` 把 Potree 数据集当作 ADD tileset 读取（`fetchData` 把 `r012.potree` 映射成 HTTP Range 请求）。对我们来说，更干净的做法是抽象出 `NodeSource` 接口（§3.2.2），同时支持 `ImplicitTilesSource` 和 `Potree2Source`，与 r01、r06 建议的 Potree 2 布局兼容。

### 3.10 REPLACE 网格层要点（V0.5–V0.6）

1. **完整子覆盖**：细化时子节点必须铺满父节点区域。缺失象限用父网格裁出的子区域补上，参考 cdb-to-3dtiles `fillMissingPositiveLODElevation` 与 `createNorthWestSubRegion` 等函数。
2. **纹理回退**：子瓦片没有影像时，沿父链找最近的影像，重算 UV（`indexUVRelativeToParent`），并缓存已处理的父影像。
3. **GE 要有度量含义**：`GE = 简化误差（米）`，对应 meshopt `targetError × 网格尺寸`。**不要**用 cdb 的 `300000/2^L`。
4. **简化参数起点**：`meshopt_simplify(targetIndexCount = 0.3·N, targetError = 0.01)`（cdb 默认）。更好的做法是用 gltfpack，3d-tiles-tools 已经集成，另有 meshoptimizer 8.4k stars（2026-09 活跃）。
5. **RTC**：顶点写成相对 AABB 中心的坐标，中心放进节点 translation。

### 3.11 3DGS 与语义扩展速查

- **`KHR_gaussian_splatting`（已批准）**：`mode = 0 (POINTS)`。属性有 `POSITION`、`KHR_gaussian_splatting:ROTATION`（VEC4 四元数）、`:SCALE`（VEC3，线性，非负）、`:OPACITY`（SCALAR，范围 0–1）、`:SH_DEGREE_0_COEF_0`（必需）以及可选的 `:SH_DEGREE_l_COEF_n`（最高 3 阶，共 45 个系数）。扩展对象字段为 `{kernel:"ellipse", colorSpace:"srgb_rec709_display"|"lin_rec709_display", projection:"perspective", sortingMethod:"cameraDistance"}`。回退时 `COLOR_0 = clamp(SH0·0.282095 + 0.5)` 并转为 linear，不支持该扩展的查看器会把它当普通点云渲染。节点变换必须能分解为 TRS，缩放为正。
- 压缩：`KHR_gaussian_splatting_compression_spz_2`（Khronos PR #2531）。CesiumJS 已支持。
- 语义：逐点类别用 `EXT_mesh_features`（feature ID 放在属性里），类名和属性表用 `EXT_structural_metadata`（`propertyTables`，或逐点 `propertyAttributes`）。瓦片级和内容级元数据放在 subtree 的 property table 里（implicit），显式 tileset 则写在 `tile.metadata` 或 `content.metadata`。

---

## 4. 在本项目中的落点与复用方式

### 4.1 复用清单

| # | 项 | 来源（仓库 / 文件 / 函数） | 落点模块 | 版本 | 复用方式 | 理由 |
|---|---|---|---|---|---|---|
| 1 | SSE 公式与 errorTarget 语义 | cesium `Cesium3DTile.getScreenSpaceError`；3DTilesRendererJS `TilesRenderer.calculateTileViewError` | `apps/web/src/world/pointcloud/lod/sse.ts` | V0.1 | port | 事实标准，只有十几行 |
| 2 | ADD early cull | cesium `Cesium3DTilesetTraversal.meetsScreenSpaceErrorEarly`；3DTilesRendererJS `markUsedTiles` | `lod/select.ts` | V0.1 | port | 省下 30%+ 的无效节点 |
| 3 | 最佳优先 + 硬点预算 | potree `Potree_update_visibility.js`（BinaryHeap、pointBudget） | `lod/select.ts` | V0.1 | port | 硬性保证流畅 |
| 4 | 帧时和内存自适应 | cesium `Cesium3DTileset.increaseScreenSpaceError/decrease...`（×1.02） | `lod/controller.ts` | V0.1 | port | "疏密自动调节"的主体 |
| 5 | 动态 SSE（贴地平线） | cesium `_dynamicScreenSpaceErrorComputedDensity` + `CesiumMath.fog`；3DTilesRendererJS `errorFalloff` | `lod/controller.ts` | V0.2 | port | FPV 和低空视角省点 |
| 6 | 移动中暂缓请求、注视点延迟 | cesium `loadTile/isOnScreenLongEnough/isPriorityDeferred` | `pointcloud/loader/queue.ts` | V0.1 | port | 快速飞行时不浪费带宽 |
| 7 | implicit 客户端（subtree 解析、位流、Morton、模板 URI、child subtree） | 3d-tiles-tools `BinarySubtreeDataResolver`、`BufferAvailabilityInfo`、`MortonOrder`、`TemplateUris`、`ImplicitTraversedTile`；3DTilesRendererJS `SUBTREELoader` | `pointcloud/source/implicit.ts` | V0.1 | port | 参考实现 `sse_select.py` 已验证 |
| 8 | 包围盒推导 | 3d-tiles-tools `BoundingVolumeDerivation.deriveBoundingBox` | `source/implicit.ts`、`world/tiler` | V0.1 | port | 数值稳定 |
| 9 | 点云切片器（implicit octree + glb + pointCount） | 本单元 `wp_tiler.py`（已验证）+ 3d-tiles-tools `GltfTransformPointClouds`（Z-up→Y-up 节点） | `world/pointcloud/tiler.py` | V0.1 | port | 自有数据（法线、类别、first_seen）必须自研 |
| 10 | 格式校验 | 3d-tiles-validator 0.6.1 | `tools/ci/validate-world.sh` | V0.1 | adopt | 结构性错误进不了主干 |
| 11 | LRU 与队列 | 3DTilesRendererJS `LRUCache`（min/max bytes、unloadPercent、先卸深层）、`PriorityQueue` | `pointcloud/loader/cache.ts` | V0.1 | port | 参数成熟 |
| 12 | 点尺寸衰减（逐瓦片） | cesium `PointCloudStylingStageVS.glsl` `getPointSizeFromAttenuation` | `pointcloud/material/points.tsl.ts` | V0.1 | port | 最简单、足够 MVP |
| 13 | VNT 逐点自适应尺寸 | 3DTilesRendererJS `PotreePlugin._updateActiveNodesTexture` + `PointCloudMaterial.getActiveDepth` | `material/vnt.tsl.ts` | V0.2 | port（GLSL→TSL） | 消除 ADD 下粗点偏大 |
| 14 | Potree2 数据源 | 3DTilesRendererJS `PotreeLoader` / `PotreePlugin`；potree23dtiles `potreebin.py` | `source/potree2.ts`、`tiler/import_potree.py` | V0.2 | reference / port | 兼容 PotreeConverter 产物 |
| 15 | 打包、合并、服务 | 3d-tiles-tools CLI `convert`（.3tz / .3dtiles）、`mergeJson`、`combine`、`serve`、`upgrade` | `world/packaging`、`tools/dev-serve` | V0.5 | adopt | 多架次、多区域组合与分发 |
| 16 | 3TZ 索引（MD5 排序、HTTP Range） | 3d-tiles-tools `IndexBuilder.ts`、`ArchiveFunctions3tz.ts` | FastAPI `world_service` 单文件服务 | V0.5 | reference | 一个 World 一个文件，免解压服务 |
| 17 | ENU<->ECEF 根变换 | 3d-tiles-tools `TilesetJsonCreator.computeTransformFromCartographicPositionDegrees`；potree23dtiles `proj.wgs84_trans_matrix` | `world/georef` | V0.5 | port | RTK 配准后进入 Cesium |
| 18 | 大区域瓦片集组织、贴地查询 | cdb-to-3dtiles `combineTilesetJson`、`CDBTileset.getFitTile`、`CDB::queryElevationTiles` | `world/package`、`sim/ground_query` | V0.5 | reference | 山区加街区的多区域 World |
| 19 | REPLACE 补洞、纹理回退、meshopt 简化 | cdb-to-3dtiles `fillMissingPositiveLODElevation`、`addElevationToTilesetCollection`、`CDBElevation::createSimplifiedMesh` | `world/mesh/tiler` | V0.6 | reference | 网格和地形层 |
| 20 | pnts 兼容读取 | 3d-tiles-tools `TileFormatsMigrationPnts.convertPntsToGlb`、`AttributeCompression.octDecode`、`Colors` | `world/import` | V0.5 | adopt（CLI `upgrade --targetVersion 1.1`） | 导入外部旧数据 |
| 21 | 3DGS 内容 | Khronos `KHR_gaussian_splatting`；cesium `GaussianSplat3DTileContent` | `visual/gaussian` | V0.6+ | reference | 标准化的 Visual World |
| 22 | 外部 3D Tiles 图层（城市模型、Google 3D Tiles、PLATEAU） | 3DTilesRendererJS `TilesRenderer`（adopt） | `apps/web/src/world/layers/tiles3d.ts` | V0.5 | adopt | WebGL2 路径开箱即用，WebGPU 需验证 |

### 4.2 World Package <-> 3D Tiles 兼容策略

**原则：3D Tiles 负责可视层（Visual World）的流式容器，Geometry World（碰撞、ESDF、占据栅格）不进 3D Tiles。** 物理查询在服务器端完成，数据格式是 VDB、npz 或 octomap；3D Tiles 2.0 voxels 只作为将来的可视化和交换格式。

```text
worlds/<world_id>/
  world.json                      # 清单（带 $schema 版本号）：id、version、crs{epsg, enu_origin{lat,lon,h}, T_enu_ecef},
                                  #   up_axis:"+Z", units:"m", layers[{id,type,href,group}], stats, lod{rootSpacing, errorTarget}
  tileset.json                    # 根 World tileset（ADD）：children 通过 external tileset 引用各图层；
                                  #   V0.5 起根 transform = ENU→ECEF；可直接在 CesiumJS 或 3DTilesRendererJS 打开
  layers/
    pointcloud/                   # 3D Tiles 1.1 implicit OCTREE（本单元方案）
      tileset.json  subtrees/{level}/{x}/{y}/{z}.subtree  content/{level}/{x}/{y}/{z}.glb
    pointcloud-<sortie>/          # 多架次各自一个子集，由根 tileset 合并（3d-tiles-tools mergeJson 的模式）
    mesh/                         # V0.6：3D Tiles 1.1（REPLACE，glb + EXT_meshopt_compression + KHR_texture_basisu）
    gaussian/                     # V0.6+：3D Tiles 1.1 + KHR_gaussian_splatting（+ spz_2）
  semantic/                       # 区域和线状要素：GeoJSON（限飞区、电力线），后续可选 3D Tiles 2.0 vectors
  geometry/                       # Geometry World（非 3D Tiles）：collision.vdb / esdf.npz / occupancy.bt
  environment/                    # E(x,y,z,t)：Zarr/NetCDF/VDB；远期可导出 3D Tiles 2.0 voxels + visibility conditions(t)
  reconstruction/                 # 相机和轨迹（沿用 r01 格式）
  dist/world.3tz                  # 可选：单文件分发（3d-tiles-tools convert），服务器用 HTTP Range 读取
```

**兼容分级**

| 级别 | 版本 | 内容 | 验收 |
|---|---|---|---|
| L0 本地 | V0.1 | 3D Tiles 1.1 子集：本地 ENU Z-up，box 包围体，implicit OCTREE，glb POINTS + `KHR_mesh_quantization`，ADD，GE=spacing，subtree `pointCount` | validator 0 错误；自研流式器、CesiumJS、3DTilesRendererJS 三者都能打开 |
| L1 地理 | V0.5 | `root.transform = ENU→ECEF`（刚体）；`TILESET_CRS_GEOCENTRIC`；`asset.tilesetVersion` 用于缓存失效；多架次、多区域以 external tileset 组合；可打包为 .3tz | Cesium 地球上位置正确，与 RTK 标定点的误差 <0.1 m |
| L2 标准扩展 | V0.6 | 语义（`EXT_mesh_features` / `EXT_structural_metadata`）；网格层 REPLACE；3DGS 层 `KHR_gaussian_splatting` | 同上，外加语义拾取 |
| L3 前瞻 | V1.0 | 3D Tiles 2.0 写出器：glTF 2.1 tileset、`EXT_georeference`、`EXT_node_visibility_conditions`（世界版本和时间）、`EXT_voxels`（环境场可视化） | 等 2.0 正式发布后再做 |

**设计不变量**（保证 1.1 → 2.0 迁移成本最低）：
1. transform 只含旋转和平移。
2. GE 以米为单位，严格逐级递减。
3. 上轴和单位写进 `world.json`。
4. 内容一律用 glb，不写 pnts、b3dm、i3dm。
5. 瓦片或内容级元数据尽量放进 glb 或 subtree，少依赖 tileset JSON 的 `metadata` 字段（2.0 已从核心移除）。

### 4.3 Web 端模块划分（Three.js + React）

```text
apps/web/src/world/pointcloud/
  source/implicit.ts      ImplicitTilesSource：tileset.json + subtree 缓存 + info()/children()
  source/potree2.ts       Potree2Source（V0.2，可选）
  lod/sse.ts              sse()、distToBox()、sseDenom 取值
  lod/select.ts           selectNodes()：最佳优先、预算、early cull
  lod/controller.ts       LodController：帧时、内存、交互态
  loader/queue.ts         请求优先队列、并发、abort、移动中暂缓、注视点延迟
  loader/worker.ts        最小 GLB 解析 → {positions: Uint16Array(stride 4), normals: Int8Array, ...} transferable
  loader/cache.ts         LRU（字节预算，先卸深层）
  material/points.tsl.ts  WebGPU：instanced quad + TSL 点尺寸（Cesium 衰减）；WebGL2：PointsMaterial 或 GLSL
  material/edl.ts         EDL 后处理（参数见 r01 §3.12）
  PointCloudLayer.tsx     R3F 组件：每帧 useFrame → select → queue → upload（≤4 MB/帧）
  stats.ts                points / tiles / pending / errorTarget / budget / fps → lieflat 风格面板
```

后端 `world_service`（FastAPI）只做**静态分发**：`StaticFiles` 或 nginx，glb 用 `Cache-Control: immutable`，URL 带 `?v=tilesetVersion`。切片在离线 Job 里完成（`world/pointcloud/tiler.py`），完成后跑 validator。

### 4.4 离线流水线（MVP）

```text
PLY/LAS/LAZ ──> normalize（单位、轴向 → ENU Z-up 米；Chicago ×1000，Suzhou 轴向人工确认）
           ──> clean（离群点等，交给 r06 Open3D）
           ──> tiler.py（分层采样 → implicit octree → glb + subtree + tileset.json）
           ──> 3d-tiles-validator（CI 必过）
           ──> world.json 与根 tileset.json
           ──>（V0.5）3d-tiles-tools convert → world.3tz
```

---

## 5. 对比与推荐

| 维度 | 3d-tiles | 3d-tiles-tools | cdb-to-3dtiles | potree23dtiles | （补充）3DTilesRendererJS | （补充）py3dtiles |
|---|---|---|---|---|---|---|
| Star / 2026 活跃度 | 2612 / 2026-08（2.0 草案推进中） | 539 / 2026-07（全年 3 个版本） | 92 / 2024-05（停滞） | 58 / 2021（废弃） | 2475 / 2026-09-28（周更） | 234（GitHub 镜像）、GitLab 62 / 2026-09 |
| 与本项目契合度 | 极高：格式与语义基础 | 高：implicit 客户端移植源，打包合并 | 中低：CDB 输入无关，只借鉴组织和 REPLACE 经验 | 低：只借鉴映射思路 | 极高：Three.js 流式、Potree、LRU、EDL | 中：Python 切片可作对照，但输出不合规 |
| 工程可用性 | 规范 | npm 直接可用，Node 22 OK | 构建门槛高（GDAL、OSG、submodule） | 只能在 Windows 跑，有规范违规 | npm 可用；**点云材质是 GLSL，与 WebGPU 不兼容** | pip 可用 |
| 复用方式 | adopt（标准） | port + adopt（CLI） | reference | reference | port（点云）+ adopt（网格、外部图层） | reference |

**推荐排序**：
**3d-tiles 规范 > 3d-tiles-tools > cdb-to-3dtiles > potree23dtiles。**
加入补充对象后：3d-tiles ≈ 3DTilesRendererJS > 3d-tiles-tools ≈ 3d-tiles-validator > py3dtiles > cdb-to-3dtiles > potree23dtiles。

理由：
- 规范决定格式是否"对"；3DTilesRendererJS 和 Cesium 决定运行时算法是否"对"。
- 3d-tiles-tools 是 2026 年仍在维护的官方工具，适合做 implicit 数据结构的移植源和打包链。
- cdb-to-3dtiles 的价值在"网格 REPLACE 地形"这一类经验，放在 V0.5 以后。
- potree23dtiles 只剩下"Potree 与 3D Tiles 可以互转"这个思路的参考价值。

---

## 6. 风险与注意事项

### 6.1 3D Tiles 2.0 草案的破坏性变更（`next/2.0/CHANGES.md`，2026-08-18）

| 变更 | 对我们的影响 | 规避 |
|---|---|---|
| tileset 改为 glTF 2.1 资产（`3DTILES_tileset`），入口推荐命名 `root.tileset.glb` | 1.1 的 JSON 写出器将来需要一个 2.0 版本 | 写出器与内部模型解耦（World 清单 → writer） |
| implicit tiling、多内容、metadata、region、viewerRequestVolume、group 都移出核心，改为扩展 | 不能过度依赖 1.1 核心的 metadata 和 group | 元数据放 subtree 或 glb；图层用 external tileset 表达 |
| 坐标名改为 `{right}{forward}{up}`（box） | 模板 URI 变量要换 | 文件路径保持 `{level}/{x}/{y}/{z}`，只改映射 |
| **GE 不再随 transform 缩放**（回到 1.0 行为） | 带缩放的 transform 在 1.1 与 2.0 下 LOD 相差 scale 倍（py3dtiles 的 ×10 transform 就是实例） | **transform 只含刚体变换** |
| GE ≥ 父级的瓦片无条件细化 | 空父瓦片、GE 设置不当会导致该瓦片永远不渲染 | GE 严格递减；空瓦片 GE 取父 GE |
| 本地坐标系改为 Y-up，全球坐标系须声明 `EXT_geospatial_crs` | 上轴约定会变 | 上轴写进清单，写出器集中处理一个矩阵 |
| `.3tz` / `.3dtiles` 被 glTF 2.1 打包机制取代 | 分发格式有迁移成本 | .3tz 只作可选的分发产物，不作存储主格式 |

### 6.2 其他风险

1. **WebGPU 兼容性。** 3DTilesRendererJS 与 Potree 的点云材质都用 `onBeforeCompile` 注入 GLSL，在 `WebGPURenderer`（包括它的 WebGL2 后端）下无效。WebGPU 的 point-list 恒为 1 px。必须自写 TSL 材质，用实例化 quad；保留一条纯 `WebGLRenderer` 回退路径。本机 headless Chromium 大概率只有 WebGL2（软件渲染），验收时要两条路径都测。
2. **小文件过多。** SF 切出 1403 个 glb（中位数 1.5k 点）。大城市或多架次时会达到上万文件。对策：叶子合并（MIN_NODE≈8k）、HTTP/2、`.3tz` 配合 Range；开发期用 Vite 或 FastAPI StaticFiles，生产用 nginx 或 Caddy。
3. **gzip 收益低。** uint16 位置和 int8 法线压缩率只有约 10–30%。需要进一步压缩时用 `EXT_meshopt_compression`（three 自带 MeshoptDecoder）。**不要用 Draco**：`KHR_draco_mesh_compression` 只支持三角网格，规范迁移指南已经说明。
4. **implicit 的 GE 只能逐层减半。** 某些节点实际误差不同（例如合并后的叶子）时，要用 `TILE_GEOMETRIC_ERROR` 元数据覆盖，否则只是略微多细化一点，不影响正确性。
5. **JS 位运算只有 32 位。** `MortonOrder.encode3D` 每轴 10 bit。Morton 只用在 subtree 内部（`subtreeLevels ≤ 10`）；全局坐标用普通整数，若超过 2^31 再改用 BigInt。
6. **数据异构。** Chicago 疑似以 km 为单位，Suzhou 轴向存疑，全部城市都没有颜色。导入时不做归一化会导致 GE、SSE 和飞行尺度全部失真。**导入必须输出 QA 报告**：包围盒、间距、上轴判定和法线直方图。
7. **py3dtiles 1.1 输出不合规**（COLOR_0 格式、对齐），而且依赖带缩放的 transform，不能直接进 World Package。
8. **cdb-to-3dtiles 无法直接构建。** shallow clone 里 submodule 为空，还依赖 GDAL、OSG、OpenGL，只支持 Linux，并且 2024 年后停更。输入是 CDB，与本项目无关，**不建议投入构建**。
9. **potree23dtiles 只能在 Windows 跑。** 它违反 pnts 的 8 字节对齐要求，节点点数 <4 时会丢整棵子树，GE 取值是启发式。**不要使用**。
10. **REPLACE 网格层的空洞与闪烁。** 构建期不保证完整子覆盖，或运行时不等兄弟节点到齐，都会出现空洞。3DTilesRendererJS 的 `loadSiblings` 默认开启，Cesium 采用"子节点都加载完才细化"。
11. **规范版本漂移。** 3DTilesRendererJS 仍在高频变化（0.5.x，traversal 语义 2026-09 还在调整）。直接 adopt 时要锁定小版本，并把回归截图测试放进 CI。
12. **Validator 版本。** 0.6.1 已能校验 KHR_mesh_quantization 和 subtree 元数据。它对 `EXT_structural_metadata` 或 3DGS 的支持程度要在 V0.6 时重新确认。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§15 改写"后期兼容 3D Tiles"。** 改为 **V0.1 起点云层原生采用 3D Tiles 1.1 implicit octree**。3D Tiles 本身就是"空间分块 + 八叉树 + LOD + 流式"的标准化表达，与 §14 的需求一一对应。先自定义 `Binary Tile + Octree Index`、以后再转，等于做两遍。同时写明 **ADD 细化、GE = 层点间距、subtree 元数据 `pointCount`、glb + `KHR_mesh_quantization`** 这四个规格。
2. **§14 把定性描述落成公式和参数。** 原文是"浏览器根据相机位置、FOV、距离、屏幕尺寸自动确定节点"。建议改为：`SSE = GE·H/(d·2tan(fovy/2))`；τ 点云 1.5 px、网格 16 px；硬点预算默认 1M（软件渲染 300k）；帧时 EMA 反馈（×1.05 / ÷1.02，滞回区间 [0.8, 1.15]）；ADD early cull；移动中暂缓请求（60·‖Δ‖/D）；注视点延迟 0.2 s；每帧上传 ≤4 MB；LRU 字节预算 512 MB。另外，示例"远处 10K、近处 1M 点"应改为以屏幕像素间隙为准。
3. **§51 分层有误。** "WORLD MODEL → 3D Tiles"把一种**容器和流式格式**与 Point Cloud、Mesh、Voxel、SDF 这些**几何表达**并列了。建议把 3D Tiles 挪到 "WORLD RUNTIME / Visual World 分发格式" 一层，并明确 **Geometry World（碰撞、ESDF、占据栅格）不进 3D Tiles**，在服务器端用 VDB、npz 或 octomap。
4. **§41 World Package 补齐清单和图层化结构。** 补充以下内容（详见 §4.2）：`world.json`（带 schema 版本、CRS、`up_axis`、单位、图层列表、统计信息）；根 `tileset.json`，用 external tileset 组合点云、网格、3DGS 图层，这样一个 URL 就能在 Cesium 或 3DTilesRendererJS 打开；`asset.tilesetVersion` 用于缓存失效；可选 `.3tz` 单文件分发；多架次各成一个子集再合并（CDB GeoCell 或 mergeJson 模式）。另外，原文 `── visual/` 缺少树枝符号，应为 `└── visual/`。
5. **§7 和 §16 的坐标约定要写明。** World 内部统一用 **ENU、米、Z-up**（与 3D Tiles 1.1 和 ROS 一致）。three.js 场景是 Y-up，在 `WorldLayer` 根上做一次 `−π/2` 旋转。PX4 NED 在 Simulation Gateway 处转换。地理配准只允许刚体 transform（ENU→ECEF），**禁止带缩放**，这样才能兼容 3D Tiles 1.1 与 2.0 在 GE 缩放上的差异。
6. **§8 Visual World 的 3DGS 走标准路线。** `KHR_gaussian_splatting` 已被 Khronos 批准，加上 3D Tiles HLOD 就能流式传输 3DGS，CesiumJS 已支持。建议写成"Visual World = 点云（V0.1）→ 网格（V0.6，REPLACE）→ 3DGS（V0.6+，`KHR_gaussian_splatting` + SPZ）"，三者共用一套 SSE、LRU 和队列框架。
7. **§11 的 Cesium 定位。** 由于数据是 3D Tiles，**CesiumJS 可以直接消费，不需要"后续增加"**，它适合做 GIS 大场景和地球视图（V0.5 配准后）。Three.js 侧补充 **3DTilesRendererJS**（2.5k stars，2026-09 活跃，支持 Potree 和 R3F），用于外部 3D Tiles 图层；自研点云流式器只负责核心点云，原因是 WebGPU、硬点预算和回放揭示这几项需求。
8. **§18–20 环境场的交换格式。** `E(x,y,z,t)` 的物理查询留在服务器（VDB、Zarr）。可视化和交换可以在 V1.0 评估 3D Tiles 2.0 的 `EXT_voxels` / `3DTILES_tileset_voxels` 与 `EXT_node_visibility_conditions`（时间键）。这与 §20 Level 3 的 CFD 离线结果按风向风速组织（例如 `000_05.vdb`）是自然对应的。
9. **§43 MVP 链路补两步。** 改为 `…Point Cloud → 归一化（单位/轴向） → Tiler → Validator → Octree/3D Tiles → Three.js…`。归一化和校验是 UrbanScene3D 实测暴露出来的必需步骤（Chicago km、Suzhou 轴向、全体无颜色）。
10. **§37 更新频率补充"流式预算"指标。** 例如：首帧出图 ≤1.5 s（根瓦片约 250 KB）；桌面 GPU 在 1–2M 点下保持 60 FPS；软件 WebGL2 在 300k 点下 ≥30 FPS；最大并发请求 6 或 16；每帧上传 ≤4 MB；GPU 缓存 ≤512 MB。这些指标同时作为流畅性测试的验收线。
11. **§7 Semantic 的落地方式。** 逐点语义写入 glb，用 `EXT_mesh_features` 加 `EXT_structural_metadata`，拾取时可以直接查类别。限飞区、电力线这类要素用 GeoJSON 挤出体（将来可选 3D Tiles 2.0 vectors），不要硬塞进点云。
12. **§3 和 §36 服务端职责。** World Service 在 MVP 阶段只需要静态分发 3D Tiles（带 Range 和 immutable 缓存）。切片放在离线 Job 里（与 r01 的 Job 框架一致），不要做成在线 API。
13. **§44 V0.1 功能清单补充。** 在 "Point Cloud / Web Viewer" 之外，补上 "Tiler + LOD Streamer（SSE / 预算 / 自适应）+ Stats 面板（点数、瓦片、待加载、τ、预算、FPS）"。这是"系统非常流畅"这个目标的可测实现。
14. **02-refs.md 修正。** 3d-tiles-tools 的描述应改为"3D Tiles 处理、打包、合并、升级工具（不含切片器）"。同时补充 **NASA-AMMOS/3DTilesRendererJS**（P0）、**CesiumGS/3d-tiles-validator**（P1），以及可选的 **py3dtiles**（P2，仅对照）。

---

**附：本单元可复现的命令**

```bash
# 解析 subtree 并检查规范 Morton 示例
.venv/bin/python .cache/research/r10/parse_subtree.py refs/world/3d-tiles-tools/specs/data/tilesetProcessing/implicitProcessing
# 统计 UrbanScene3D（包围盒、间距、上轴）
.venv/bin/python .cache/research/r10/ply_stats.py && .venv/bin/python .cache/research/r10/ply_axis2.py
# 切片原型 + 官方校验（0 错误）
.venv/bin/python .cache/research/r10/wp_tiler.py "data/raw/urbanscene3d/San Francisco_sampled_5m.ply" .cache/research/r10/wp_sf
.cache/research/r10/node/node_modules/.bin/3d-tiles-validator --tilesetFile .cache/research/r10/wp_sf/tileset.json
# SSE 与预算选择模拟
.venv/bin/python .cache/research/r10/sse_select.py .cache/research/r10/wp_sf
```
