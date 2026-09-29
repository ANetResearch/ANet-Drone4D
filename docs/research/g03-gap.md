# G3 补充深挖：World Package、ANET_Q16 v1、`coordinate.json` 缺少统一 schema——冻结 v1 契约（JSON Schema + 二进制规范 + 校验器 + 六城实例）

> 缺口编号：G3（见 `00-index.md` §9）｜ 日期：2026-09-28 ｜ 相关单元：r09、x01、r15、r02、r07、r10、n01、n02（旁及 r01、r06、r08、r11、r12、r13、r17、g06）
> 对应设计：`docs/01-design.md` §7–8（World Model 双表达）、§14–16（点云数据格式与 WorldLayer）、§41（World Package）
>
> 输入：上述单元笔记；源码 `refs/world/PotreeConverter/Converter/src/indexer.cpp`（`createMetadata`、`computeScaleOffset`）、`refs/world/PotreeConverter/Converter/include/Attributes.h`、`refs/web3d/potree/src/modules/loader/2.0/OctreeLoader.js`、`refs/web3d/potree-core/source/loading2/OctreeLoader.ts`、`refs/web3d/three-loader/src/loading2/octree-loader.ts`；原型 `.cache/research/r09_octree_fast.py`、`r09_potree2_writer.py`、`.cache/research/x01/analyze.py`、`x01/analysis.json`。
>
> 本次产物都在 `/data/projs/anet-drone/.cache/research/g03/`，可以直接搬到 `packages/contracts/`：
> - `schemas/{common,world,coordinate,pointcloud-metadata,class-table,grid}.schema.json`：JSON Schema draft 2020-12。Python `jsonschema 4.26` 与 **Ajv 8.20 strict 模式**都能编译，六城实例 0 错误。
> - `classes/anet-classes-v1.json`：规范类别码表，紧凑索引 0–15 与 ASPRS LAS 1.4 码一一对应。
> - `worldpkg_validate.py`：校验器，结构校验加 40 余条跨文件语义不变量，`--deep` 逐节点解码检查。
> - `g03_build.py`：参考实现，覆盖 ingest 规范化、中心优先网格竞选八叉树、ANET_Q16 v1 写出、多根森林、coordinate/world 清单。**六城全部重新生成**，每城约 27 s。
> - `instances/<city>/`：六城 World Package 实例，包括 `world.json`、`coordinate.json`、`visual/pointcloud/**`（每城约 60 MB）、`geometry/terrain/dtm_10m.*`、`semantic/anet-classes@1.json`。
> - `ts/world-package.d.ts`：由 json-schema-to-typescript 16 生成，`tsc --strict` 通过；`ts/check.mjs` 是 Ajv 校验脚本，`ts/gen.mjs` 是代码生成脚本。

---

## 0. 结论速览（10 个分歧逐条裁决）

| # | 分歧 | 裁决（v1 冻结） | 依据 |
|---|---|---|---|
| 1 | `pos.w` 放 oct16 法线（r09、x01），还是强度或类别（r11） | **放 oct16 法线**：`(octU<<8)\|octV`，**0x0000 保留为"无法线"**。编码器若恰好算出 0x0000，改写为 0xFFFF；四个角都解码为 −Z，结果不变。强度放在可选的第三流 `ext.x`，或在无 RGB 时烘进 `col.rgb` | UrbanScene3D 没有颜色也没有强度，画面质量几乎全靠法线光照（x01 §3.7）。r11 的写法只是占位，没有实测依据。实测 oct16 在 1M 随机方向上的误差：平均 0.34°，p99 0.75°，最大 0.95° |
| 2 | `col.w` 放 class（r09、x01），还是 LOD 秩（r11） | **放 class 紧凑索引**。LOD 秩**不需要存**：节点内点序已经用固定种子 Fisher–Yates 打散，秩就是点在节点内的下标（`gl_VertexID − first` 或 `vertex_index − prefix`） | 打散后"任意前缀都是均匀子采样"（r09、r12），单独存秩是重复信息 |
| 3 | 类别用 LAS 码（x01：2/64/65/1/9），还是紧凑索引 0–15（n02） | **两层**：运行时字节存**紧凑索引**，u32 `classMask` 一次位运算即可完成图层开关；归档（LAS/COPC）和 3D Tiles 导出存 **LAS 码**；由 `anet-classes@1` 码表双向映射。**屋顶采用 LAS 6**（n02 与 ASPRS 标准），**x01 的"65=屋顶"作废**；65 保留给 n02 的低矮物 | x01 与 n02 的冲突点是 65：x01 用它表示屋顶，n02 用它表示低矮物。标准码优先。16 项以内时 mask 就是一个 u32 uniform |
| 4 | `coordinate.json` 有 r15、x01、r02、r07 四套字段（外加 r01、r06、r08、r09(e)） | 合并为 **coordinate v1**：`frame`（常量）+ `anchor`（kind、datum、经纬、椭球高、MSL、geoid）+ `T_ecef_world`（派生，可校验）+ `trueNorth` + `scaleStatus` + `source`（单位、上轴、手性、调平、yaw、`T_world_source`、证据）+ `registration`（Sim3 `T_world_map`）+ `ground` + `extent` + `precision` + `conventions` + `qa`。**键名用 camelCase**，矩阵键保留 `T_<to>_<from>` 形式 | 字段逐一对照见 §2.2；r15 的 snake_case 与 x01 的 camelCase 并存，这次统一为 Potree/TS 一侧的 camelCase |
| 5 | 帧名混用："world" 在 r01、r07 中指重建世界，在 r15 中指 ENU | **`world` 只指锚点处的 ENU**。重建与 LIO 的帧分别叫 `map` 和 `engine`。更名：r07/r01 的 `T_enu_world` → `T_world_map`；r08 的 `T_enu_map` → `T_world_map`；r02 的 `T_enu_engine` → `T_world_engine` | 否则 `T_enu_world` 和 `T_ecef_world` 里的 "world" 会指两个不同的东西 |
| 6 | `metadata.anet` 扩展项（`levelsByteEnd`、`nnMedian`、`z_p1/p99`、tight bounds、`roots[]`） | 冻结 `anet` v1，共 19 个必填键（§4.3）。`levelsByteEnd`、`levelsPoints`、`levelsNodes` 三个平铺数组；`tightBounds`；`hierarchyExt`；`stats{zP1,zP99,hagP1,hagP99,nnMedianM,classHistogram}`。**多根森林不放进 metadata**，而是在 `world.json` 的点云图层下列 `roots[]`，每个根是一个独立的标准 Potree 2.0 容器 | 每个根都能单独被 Potree 工具读取；选择器已经是"多个点云共享一个预算"（Potree `updateVisibility` 本来就这样），不需要扩展容器语义 |
| 7 | `world.json` manifest 未定义 | 定义 **world v1**：id、`contentVersion`（全部文件哈希汇总后取 12 位）、dataset/许可、coordinate 引用与哈希、`scaleStatus`、bounds、`layers[]`（type/role/format/href/status/`T_world_layer`（只允许刚体）/roots）、lod（首屏与预算）、render（默认着色、z/hag 区间、合成地面）、camera、qa、`files[]` | 前端启动只需一个入口文件。G6 的风场 manifest 用 `coordinate.sha256` 绑定坐标（即 g06 的 `coordinate_hash`） |
| 8 | "Potree 2.0 兼容，可以用三个现成查看器对照"（n01、r12） | **不成立，需要修正**：potree 1.8 `OctreeLoader.js:60` 与 potree-core `OctreeLoader.ts:87` 只区分 `BROTLI`，three-loader `octree-loader.ts:28` 只区分 `GLTF`；除此之外的 encoding 一律交给 DEFAULT 解码器，按 AoS int32 解析。ANET_Q16 容器交给它们只会出乱码。v1 的做法：attribute 名用 `anet:pos` / `anet:col`（没有 `position`，旧 loader 会直接失败，不会静默出错）；需要对照时，tiler 加 `--twin-default` 额外写一份节点集完全相同的 DEFAULT 孪生容器，路径由 `anet.twin` 指向 | 源码核实 |
| 9 | `hierarchy_ext.bin` 的索引方式 | **与 `hierarchy.bin` 逐条镜像**：第 i 条 22 B 记录对应第 i 条 12 B 扩展记录（字节 22i ↔ 12i，PROXY 重复记录也算）。内容为**子树**的紧包围盒（本节点加全部子孙），u16 相对节点立方体，min 向下取整、max 向上取整，保证保守 | 分页加载时，每个 hierarchy chunk `[o, o+s)` 对应扩展文件的 `[o/22·12, s/22·12)`，可以同样用 Range 取回。如果按"全局 BFS 序号"索引，懒加载时拿不到序号 |
| 10 | SF 单位 10.1（`x01/analysis.json`）还是 10.15（x01 §3.3 CFG） | **取 10.15**，重新生成。新矩阵 `T_world_source = [[0,−10.15,0,0.370465],[10.15,0,0,0.425911],[0,0,10.15,164.727487]]`。**依赖 10.1 的旧产物要重建**：x01 的 `enu_sanfrancisco.npy`，以及 g06 已经指出的 r17 SF 风场 | x01 地标证据（Oracle 基线 ×10.15），x01 CFG 是权威配置 |

新发现（会影响实现）：
- **首屏规则必须按世界计算，不能按根计算。** 苏州改成 6 根森林后，如果每根都按"≥1e5 点"选首屏层级，首屏会变成 106 万点、12.8 MB。v1 规则：每根的阈值取 `1e5 / 根数`，上限取 `4.5e5 / 根数`。苏州因此变为 **16.7 万点、2.0 MB、6 次 Range**；原来的单立方体方案是 L≤4，需要 28.8 万点、3.46 MB，而 L0 只有 500 点。
- **六个城市的数据中心都落在各自的城市中心附近。** 由最高地标反推出的示意锚点位置：SF 为 (37.7791, −122.4212)，在市政厅附近（约 0.2 km）；NY 为 (40.7131, −74.0023)，在市政厅公园以东约 0.3 km；Chicago 为 (41.8841, −87.6225)，在千禧公园附近。三者都落在数据集应有的市中心位置，从侧面再次印证了 x01 的单位与北向结论。
- **tight 包围盒对"俯视加无预算"的 LOD 选择几乎没有影响**。NY 与 Chicago 在 120–300 m 高度、τ=1.35 px 下，选中点数只减少 0–2%；节点包围球半径只缩小约 20%（tight/cube 比为 0.79，Shanghai 0.74）。r12 所说的"过度细分"主要出现在视锥裁剪和按预算排序这两步，这次**没有量化**。`hierarchy_ext.bin` 每节点 12 B，成本可以忽略，v1 照常写出，loader 可以先不用（§8 风险 R3）。

---

## 1. 分歧来源（核实记录）

| 来源 | 原文要点 | 问题 |
|---|---|---|
| r09 §3.3 | `pos=u16×4`（xyz 节点量化 + w=oct16），`col=u8×4`（rgb + class 或 intensity）；`anet{formatVersion, frame, units, upAxis, origin, sampling, pointOrder, compression, buffers[], levelsByteEnd, tightBoundsFile}` | 把 origin 和 upAxis 放在点云 metadata 里，与 coordinate.json 重复；`buffers[].comp` 可以任意声明，前端只能写成解释器；"没有法线时填 0"与合法的 −Z 编码冲突 |
| r11 §3.2.1 | `qpos.w = intensity 或 classification（可选）`；`rgba.a` 可以存 LOD 随机秩 | 与 r09 冲突；秩是冗余信息 |
| r13 §3.3 | 页池每点 12 B："unorm16×3 + oct16 \| RGBA8"；WebGL2 RGBA32UI 的第 4 个 word 放"分类/强度/时间戳" | 与本裁决兼容：页池直接搬运 ANET_Q16 字节，第 4 个 word 属于页池的私有扩展 |
| n05 | trial 用 `u16 x,y,z,pad \| u8 r,g,b,a` | 属于 v1 的特例（w=0 表示无法线，a 为类别）；trial 数据要重新生成 |
| x01 §3.3、§3.6、§3.7 | coordinate 字段 `frame:"LOCAL_ENU", upAxis, handedness, georef:null, approxTrueNorthYawDeg, northConfidence, source{…T_enu_src…}, ground, qa`；类别为 LAS 2/64/65(屋顶)/1/9；`metadata.anet.nnMedian`、z_p1/p99；多根森林 `roots:[{name,min,size}]` | 屋顶用 65 与 n02 冲突；`roots` 放进 metadata 会破坏"一个容器一个根"的约定 |
| n02 §3.10 | 屋顶用 LAS 6，立面 64，低矮物 65，水 9，路面 11，电力线 14；GPU `aClass` 为紧凑索引 0..15，配 `classMask:u32`；追加 `classification u8[n]` 与 `hag_q u8[n]` | 追加单字节流破坏了 4 字节对齐（WebGPU 顶点格式没有 u8x1）。v1 改为把 class 放进 `col.a`，hag 放进可选的 `ext`（u8x4） |
| r15 §3.1.3 | `{version, world_frame, anchor{kind,datum,lon_deg,lat_deg,h_ellipsoid_m,epoch}, georeferenced, vertical{geoid,N_at_anchor_m}, enu_to_ecef[16 列主序], projection_hint, import_transform{unit_scale,axis,rotation_quat_xyzw,translation_m}, extent_world_m}` | snake_case；`import_transform` 表达不了调平与手性，也放不下证据；矩阵是列主序平铺，与其他单元的嵌套行主序不同 |
| r02 §3.2 | coordinate 增加 `units_to_m, source_crs, vertical_datum, geoid_undulation_m, alignment_ref`；Alignment 为 `T_enu_engine: Sim3` | 帧名为 "enu"，其余单元为 "world" |
| r07 §3.x | `T_enu_world`（Sim3）、`scale_status`、`confidence`；Sim3 约定 `x_b = s·R·x_a + t`（gtsam 的约定不同） | "world" 指重建世界 |
| r01 §4.x | `{crs:"EPSG:4326", enu_origin{lat,lon,h}, T_enu_world{s,R,t}, up_axis, units, scale_status}` | 同上 |
| r06 §4.3 | `origin_enu(float64), up_axis, scale, mirror_y, crs` | `mirror_y` 应由 `T_world_source` 的行列式与 `handedness` 表达 |
| r08 §4.3 | `frames: map→enu→wgs84; T_enu_map; method: rtk_4dof; rmse` | 与 r07 同义不同名 |
| r09 §3.5(e) | `{frame, units, up, origin{lat,lon,h,datum,epoch}, source{crs,up_axis,unit_scale}, T_source_to_world, extent_m, curvature_drop_at_corner_m}` | `T_source_to_world` 与 x01 的 `T_enu_src` 方向记法相反（语义相同） |
| r10 §4.2 | `world.json{id, version, crs{epsg,enu_origin,T_enu_ecef}, up_axis, units, layers[{id,type,href,group}], stats, lod{rootSpacing,errorTarget}}`；运行时格式用 3D Tiles | 运行时格式已由 00-index C3 裁决为 Potree+ANET_Q16；CRS 信息移到 coordinate.json |
| r17 | coordinate 需要 `ground_z`，风向为气象来向 | 放进 `ground.zM`（恒为 0）与 `conventions.windDirection` |
| r23 | Mock 需要起飞点海拔来算空气密度 | `anchor.hMslM` |

---

## 2. `coordinate.json` v1

### 2.1 规则（写进系统架构说明书"跨模块契约"）

1. **唯一度量帧 `world`**：锚点处的 ENU 切平面，单位米，右手系，+Z 向上。几何、物理、规划、环境场、DroneState、WS 负载一律使用 world（r15 R1）。
2. **帧名**：`earth`（ECEF）、`world`（ENU@anchor）、`map`（LIO 或融合地图帧，由 `registration.T_world_map` 给出）、`engine`（视觉引擎规范帧，每个会话的 `alignment.json` 给出 `T_world_engine`）、`uavNN/base_link`（FLU）、`tiles/<id>`（只允许刚体平移）。
3. **矩阵记法**：`T_<to>_<from>` 满足 `p_to = T · [p_from; 1]`。JSON 中一律写**嵌套行主序 4×4**，最后一行必须是 `[0,0,0,1]`。三方库接口在边界处换算：three.js 用 `Matrix4.set(...m.flat())`，Cesium 用 `Matrix4.fromRowMajorArray`，3D Tiles 的 `transform` 用列主序平铺。
4. **Sim3**：`{s, q:[x,y,z,w], t}`，含义为 `x_to = s·R(q)·x_from + t`（与 r07、COLMAP Sim3d 相同；gtsam `Similarity3` 需做 `t_gtsam = t/s` 换算）。
5. **合成数据**（UrbanScene3D）：`anchor.kind = "synthetic"`，`georeferenced = false`。原点按 x01 规则取：XY 为包围盒中心，Z 为 DTM 中位数。锚点经纬度由"最高 HAG 点对应的已知地标"反推，只作示意，用于太阳、天空和 UI 经纬度显示，**禁止用于导航**。此时 `geoid = null`，`hEllipsoidM := hMslM`（误差不超过当地 |N|，最多约 35 m，对太阳和天空没有影响）。
6. **真实数据**：kind 取 `rtk`、`survey` 或 `gnss`，`georeferenced = true`，`trueNorth.yawOffsetDeg = 0` 且 `confidence = "exact"`（schema 用 if/then 强制）。原点是 RTK 或测量锚点，**不能**是"首条 GPS"（r02 已指出 COLMAP 默认取首条，接入时要绕开）。
7. **精度门禁**：`precision.float32UlpMm < 1` 且 `maxRadiusM ≤ 10 km`，否则告警，要求分区或移动原点（r09、r15）。六城实测最大 0.244 mm 和 5.23 km（SF）。

### 2.2 字段表（来源 → v1 键名）

| v1 键 | 类型 / 取值 | 来源单元原名 |
|---|---|---|
| `schemaVersion` | `"1.x.y"` | r15 `version` |
| `worldId` | `^[a-z0-9-]{1,63}$` | — |
| `frame` | 常量对象：`{id:"world", type:"ENU", units:"m", handedness:"right", upAxis:"+Z"}` | r15 `world_frame`；x01 `frame/units/upAxis/handedness`；r09 `frame/units/up` |
| `anchor.kind` | `rtk｜survey｜gnss｜synthetic` | r15 `anchor.kind` |
| `anchor.georeferenced` | bool；synthetic 时必须为 false，rtk/survey 时必须为 true | r15 `georeferenced`；x01 `georef:null` |
| `anchor.datum` | `WGS84｜CGCS2000` | r15、r09 |
| `anchor.lonDeg/latDeg/hEllipsoidM` | float64 | r15 `lon_deg…`；r01 `enu_origin`；r09 `origin` |
| `anchor.hMslM`、`anchor.geoid{model,undulationM}` | rtk/survey 时必填 | r15 `vertical`；r02 `vertical_datum/geoid_undulation_m`；r23 需要 |
| `anchor.epoch`、`anchor.uncertaintyM`、`anchor.label` | | r15 `epoch` |
| `T_ecef_world` | mat4（派生） | r15 `enu_to_ecef`（列主序平铺 → 改为嵌套行主序） |
| `trueNorth{yawOffsetDeg, confidence: exact｜verified｜assumed｜unknown, evidence[]}` | | x01 `approxTrueNorthYawDeg/northConfidence` |
| `scaleStatus` | `relative｜assumed｜landmark｜gnss｜lidar｜rtk｜survey` | r01/r02/r07 `scale_status`（新增 assumed、landmark、survey 三档） |
| `source{kind, dataset, files[], crs, projPipeline, handedness, upAxis, unitsToMeters, leveledDeg, yawDeg, T_world_source, evidence[]}` | | x01 `source{…T_enu_src…}`；r15 `import_transform`；r02 `units_to_m/source_crs`；r06 `scale/mirror_y/crs`；r09 `source{…}/T_source_to_world` |
| `registration{method, T_world_map: Sim3, rmseM, inliers, alignmentRef}` | source.kind 为 reconstruction 或 lio-map 时必填 | r01/r07 `T_enu_world`；r08 `T_enu_map/method/rmse`；r02 `alignment_ref` |
| `ground{type: dtm｜flat｜synthetic, zM, dtm{href,cellM}, reliefP1P99M}` | | x01 `ground`；r17 `ground_z` |
| `extent{min,max}` | 紧包围盒，world 坐标 | r15 `extent_world_m`；r09 `extent_m` |
| `precision{maxRadiusM, curvatureDropM, float32UlpMm}` | 校验器会重新计算 | r09 `curvature_drop_at_corner_m` |
| `conventions` | 常量对象（自描述） | r17（风向）、r15 R7/R8 |
| `qa{status, normalsFlippedFrac, zeroNormalsFrac, groundFrac, nnMedianM, maxHeightM, tiltRawDeg, gates[]}` | | x01 `qa`；r08 `qa` |

`projection_hint`（r15）不再单独设字段。需要 GK 或 UTM 交换时，写进 `source.crs` 与 `source.projPipeline`，或者在导出任务的参数里给出。

### 2.3 实例（San Francisco，节选，出自 `instances/sanfrancisco/coordinate.json`）

```json
{ "schemaVersion": "1.0.0", "worldId": "sanfrancisco",
  "frame": { "id": "world", "type": "ENU", "units": "m", "handedness": "right", "upAxis": "+Z" },
  "anchor": { "kind": "synthetic", "georeferenced": false, "datum": "WGS84",
              "lonDeg": -122.4212116, "latDeg": 37.7791216, "hEllipsoidM": 39.5, "hMslM": 39.5, "geoid": null,
              "uncertaintyM": { "horizontal": 50, "vertical": 40 },
              "label": "illustrative: world origin placed by offset from Transamerica Pyramid (tallest HAG peak at E=1621.6, N=1784.7)" },
  "T_ecef_world": [[0.844129,0.328449,-0.423753,-2706172.418859],[-0.536139,0.51713,-0.667182,-4260757.978574],[0,0.790378,0.612619,3886120.042606],[0,0,0,1]],
  "trueNorth": { "yawOffsetDeg": 0.0, "confidence": "verified", "evidence": ["residual yaw about +1 deg after the applied +90"] },
  "scaleStatus": "landmark",
  "source": { "kind": "dataset", "dataset": "UrbanScene3D (virtual cities, sampled 5M)", "crs": "LOCAL", "projPipeline": null,
              "handedness": "right", "upAxis": "+z", "unitsToMeters": 10.15, "leveledDeg": 0.0, "yawDeg": 90.0,
              "T_world_source": [[0,-10.15,0,0.370465],[10.15,0,0,0.425911],[0,0,10.15,164.727487],[0,0,0,1]],
              "evidence": ["Transamerica->Sutro 6.25 km / 619 u (x10.10)", "Transamerica->Oracle Park 2.19 km / 216 u (x10.15)", "rotation +90.6/+91.5 deg measured, +90 applied"] },
  "registration": null,
  "ground": { "type": "dtm", "zM": 0.0, "dtm": { "href": "geometry/terrain/dtm_10m.json", "cellM": 10.0 }, "reliefP1P99M": [-103.5, 165.37] },
  "extent": { "min": [-3638.787,-3756.033,-114.549], "max": [3638.787,3756.033,443.2] },
  "precision": { "maxRadiusM": 5229.6, "curvatureDropM": 2.146, "float32UlpMm": 0.2441 },
  "conventions": { "matrix": "row-major T_to_from", "quaternion": "xyzw, WORLD<-BODY(FLU)", "heading": "deg, north=0, clockwise",
                   "windDirection": "meteorological-from", "render": "three Y-up: (x,y,z)=(E,U,-N)", "px4Boundary": "NED/FRD at gateway only", "time": "int64 t_sim_ns" },
  "qa": { "status": "pass", "normalsFlippedFrac": 0.0364, "groundFrac": 0.385, "nnMedianM": 1.887, "gates": [ … ] } }
```

真实数据（P600 + RTK，合肥）的差异只有这几处：`anchor.kind="rtk"`，`georeferenced=true`，`geoid={"model":"EGM2008","undulationM":…}`，`trueNorth={0,"exact"}`，`scaleStatus="rtk"`，`source.kind="lio-map"`，`registration={"method":"rtk-4dof","T_world_map":{…},"rmseM":…}`，`source.crs="EPSG:4979"`，`source.T_world_source=null`，`source.projPipeline="+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad +step +proj=cart +ellps=WGS84 +step +proj=topocentric +ellps=WGS84 +lon_0=117.2272 +lat_0=31.8206 +h_0=…"`（r09 §3.5(b)）。

### 2.4 上下游怎么用

- **前端 `WorldFrame`**（r15 §3.1.5）：由 `anchor` 构造。GEO↔world 走 ECEF 严格变换；`T_ecef_world` 只用于校验和导出。
- **Recon IR**（r02）：`reconstruction/<session>/alignment.json` 的 `T_enu_engine` 改名为 `T_world_engine`。`trajectory.bin` 头部的 `origin_offset` 在 World 内恒为 `[0,0,0]`，位置直接是 world 坐标。
- **融合**（r07 F8）：导出时写 `registration.T_world_map` 与 `scaleStatus`。如果它改变了 world 原点，必须升级 World 的 `contentVersion`，并重建所有依赖坐标的派生物，例如风场库、DTM、规划栅格。
- **G6 环境场**：各类 manifest 的 `coordinate_hash` 取 `world.json` 中的 `coordinate.sha256`。

---

## 3. 类别码表 `anet-classes@1`

| index | key | LAS 码 | 并入的 LAS 码 | 名称 | 色 token（Class 模式） | 默认可见 | 伪反射率 ρ |
|---|---|---|---|---|---|---|---|
| 0 | unclassified | 1 | 0、8、12 | 未分类/其他 | g600 | 是 | 0.35 |
| 1 | ground | 2 | | 地面 | g800 | 是 | 0.25 |
| 2 | low_vegetation | 3 | | 低矮植被 | g700 | 是 | 0.30 |
| 3 | medium_vegetation | 4 | | 中等植被 | g600 | 是 | 0.32 |
| 4 | high_vegetation | 5 | | 高植被 | g500 | 是 | 0.35 |
| 5 | building_roof | **6** | | 建筑屋顶 | g100 | 是 | 0.60 |
| 6 | building_facade | 64 | | 建筑立面 | g400 | 是 | 0.45 |
| 7 | low_object | **65** | | 低矮物 | g500 | 是 | 0.40 |
| 8 | water | 9 | | 水面 | g900 | 是 | 0.05 |
| 9 | road_surface | 11 | | 路面 | g700 | 是 | 0.20 |
| 10 | bridge_deck | 17 | | 桥面 | g300 | 是 | 0.40 |
| 11 | wire_conductor | 14 | 13、16 | 电力线 | r500（品牌红，只给"主角"） | 是 | 0.30 |
| 12 | transmission_tower | 15 | | 输电塔 | g200 | 是 | 0.50 |
| 13 | dynamic_object | 66 | | 动态物/伪影（r07 F6） | g500 | 否 | 0.40 |
| 14 | noise | 7 | 18 | 噪点 | g900 | 否 | 0.10 |
| 15 | reserved | — | | 保留 | — | 否 | — |

- **导入**（LAS/COPC 读入）：`index = lut[lasCode]`，表中没有的码映射到 `unmappedLasCodeIndex=0`，数量计入 QA。**导出**：`lasCode = table[index].lasCode`。noise 从 18 往返后会变成 7，这是有意的合并。
- **禁飞区、限制区不是点类别**（n02），放在 `semantic/zones.geojson`。
- x01 的规则分类（ingest 阶段）映射如下：地面 `hag<1 ∧ |nz|>0.9` → 1；立面 `hag≥1 ∧ |nz|<0.3` → 6；屋顶 `hag≥2.5 ∧ |nz|>0.9` → 5；`1≤hag<2.5 ∧ |nz|>0.9` → 7；其余 → 0。n02 的 CSF v2 规则落地后，按同一张表写出。
- NY 实例的直方图：0=91,064，1=726,199，5=1,528,938，6=2,603,626，7=50,238。

---

## 4. ANET_Q16 v1（点云运行时编码）

### 4.1 容器（沿用 Potree 2.0，只加约束）

```text
visual/pointcloud/            # 单根；多根时为 visual/pointcloud/r-<i>/，各自是完整容器
├── metadata.json             # Potree 2.0 字段 + "anet" 扩展（§4.3）
├── hierarchy.bin             # Potree 2.0 原样：22 B/记录 <BBIqq>(type, childMask, numPoints, byteOffset, byteSize)
├── octree.bin                # 节点负载，按层级优先 BFS 顺序连续写出：(level, name) 升序，从 0 开始，无空洞
└── hierarchy_ext.bin         # 可选（v1 默认写出）：与 hierarchy.bin 逐条镜像，12 B/记录
```

v1 在 Potree 2.0 之上**额外要求**以下几点，由校验器检查：
- `octree.bin` 严格按层级优先的 BFS 写出，`levelsByteEnd[L]` 是第 L 层最后一个节点的结束偏移（不含）。所以"取 L0..Lk"只需**一次** `Range: bytes=0-(levelsByteEnd[k]-1)`。
- `boundingBox` 是立方体；`offset == boundingBox.min`；`spacing == cubeSize / G`；`hierarchy.depth` 等于实际最大层级。
- `hierarchy.bin` 的节点总数 ≤ 约 40k 时只写一个 chunk，不写 PROXY，`stepSize = depth`。超出时按 step=4 分页（算法同 `Indexer::createHierarchyChunks`），校验器按 Potree `parseHierarchy` 的语义完整解析 PROXY。
- NORMAL 记录的 childMask 不能为 0；LEAF 记录的 childMask 必须为 0。
- 自身没有点、但有子节点的内部节点照常写记录（`numPoints=0, byteSize=0`）。
- `compression=none` 时，`byteSize == numPoints × bytesPerPoint`，且 `byteOffset % 4 == 0`（12n 与 16n 天然满足），首屏缓冲可以零拷贝切成 `Uint16Array` 和 `Uint8Array`。

### 4.2 每节点负载（SoA，小端，n 个点）

```text
offset 0      pos  u16[4n]   xyz: q = clamp(round((p − nodeMin) / nodeSize · 65535), 0, 65535)
                             w  : oct16 法线 (octU << 8) | octV；0x0000 = 无法线
offset 8n     col  u8[4n]    rgb: sRGB 显示底色（总是颜色语义，见 col.rgb 取值）；a: 类别紧凑索引
offset 12n    ext  u8[4n]    仅当 streams 含 "ext"：intensity, hagQ(=HAG/0.5 m), returns((ret<<4)|nret), flags
```

- **节点立方体**：`nodeSize = cubeSize / 2^L`，`nodeMin = cubeMin + nodeSize · (x, y, z)`，其中 (x, y, z) 由节点名解析（r09 §3.1 的 `name_to_key`）。在 CPU 上用 float64 计算，再以 `object.position = nodeMin`、`object.scale = nodeSize` 下发（r11），或者作为 uniform 下发。
- **量化步长**为 `nodeSize/65535`：NY 根节点 4.8 cm，L5 为 1.5 mm；Shanghai 根节点 11.8 cm。都远小于该层的点间距（NY 根节点 49.5 m）。
- **`col.rgb` 的取值**（`anet.col.rgb`）：`srgb` 为实测颜色；`intensity-gray` 为 r=g=b=强度；`baked-height` 为 x01 Graphite 渐变（`t = clamp((z − zP1)/(zP99 − zP1))^0.6`，五档：`#1D1F23 → #3E4249 → #81868F → #CACDD3 → #F2F3F5`），**不含光照**；`constant` 为 0x80。着色器永远把它当作 sRGB 底色，不按语义分支。UrbanScene3D 用 `baked-height`，Height、HAG、Normal、Class 等模式由着色器实时计算（x01 §3.7）。
- **可选 `ext`**：MVP 的 UrbanScene3D 不写（保持 12 B/点）。HAG 模式通过采样 `geometry/terrain/dtm_10m.f32` 纹理实时计算 `z − dtm(x,y)`（SF 为 728×752 个 float，WebGL2 顶点纹理可用）。真实 LiDAR 数据写 `ext`（16 B/点），`intensity` 按数据集 p99 归一化到 255。
- **点序**：每个节点用固定种子（`shuffleSeed`）做 Fisher–Yates 打散，任意前缀都是均匀子采样，支持 `drawRange` 调密度、淡入、以及最后一个节点只画一部分。
- **gzip**（`compression=gzip`，面向公网）：每个节点一个 gzip member，`byteSize` 取压缩后的长度，解压后长度必须等于 `n × bpp`，浏览器用 `DecompressionStream('gzip')` 解压。**不能**让服务器在 Range 响应上再加 `Content-Encoding`（r12 风险 4）。局域网和本机用 `none`。

**oct16 编码**（与 `g03_build.py::oct16` 一致，TS 逐行移植）：

```python
def oct16(n):                                   # n: (N,3) 单位向量
    n = n / np.abs(n).sum(1, keepdims=True)
    sx = np.where(n[:,0] >= 0, 1.0, -1.0); sy = np.where(n[:,1] >= 0, 1.0, -1.0)   # signNotZero，不能用 np.sign
    neg = n[:,2] < 0
    ox = np.where(neg, (1 - np.abs(n[:,1])) * sx, n[:,0]); oy = np.where(neg, (1 - np.abs(n[:,0])) * sy, n[:,1])
    u = np.round((ox*0.5 + 0.5)*255).clip(0,255).astype(np.uint16); v = np.round((oy*0.5 + 0.5)*255).clip(0,255).astype(np.uint16)
    w = (u << 8) | v
    w[w == 0] = 0xFFFF                          # 0 保留为"无法线"；(0,0) 与 (255,255) 都解码为 −Z
    return w
```

**着色器解码**（WebGL2 GLSL；TSL 写法同构。两个 attribute 都设 `normalized=true`，itemSize=4）：

```glsl
uniform vec3  uNodeMin;  uniform float uNodeSize;      // 或者用 object.position/scale
uniform uint  uClassMask;                              // bit i = 紧凑类别 i 可见
in vec4 a_pos;  in vec4 a_col;                         // Uint16 x4 norm, Uint8 x4 norm
vec3 decodeOct16(float w, out bool has) {
  has = w > 0.0;
  vec2 o = vec2(floor(w / 256.0), mod(w, 256.0)) / 255.0 * 2.0 - 1.0;
  vec3 n = vec3(o, 1.0 - abs(o.x) - abs(o.y));
  if (n.z < 0.0) n.xy = (1.0 - abs(n.yx)) * vec2(n.x >= 0.0 ? 1.0 : -1.0, n.y >= 0.0 ? 1.0 : -1.0);
  return normalize(n);
}
void main() {
  uint cls = uint(a_col.a * 255.0 + 0.5);
  if (((uClassMask >> cls) & 1u) == 0u) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); gl_PointSize = 0.0; return; }
  vec3 p = uNodeMin + a_pos.xyz * uNodeSize;           // world ENU（WorldLayer 父节点 rotation.x = −π/2 → three Y-up）
  bool hasN; vec3 n = decodeOct16(floor(a_pos.w * 65535.0 + 0.5), hasN);
  vec3 albedo = sRGBToLinear(a_col.rgb);               // col.rgb 是 sRGB 编码
  float lam = hasN ? 0.45 + 0.55 * max(dot(faceforward(n, viewDir, n), uSunDir), 0.0) + 0.15 * (0.5 + 0.5 * n.z) : 1.0;
  …
}
```

TSL 要点：`const pos = attribute('pos','vec4'); const col = attribute('col','vec4'); const w = pos.w.mul(65535).round(); const cls = col.a.mul(255).round().toUint(); visible = classMask.shiftRight(cls).bitAnd(1)`。WebGPU 端必须用 x4 格式（`unorm16x4`、`unorm8x4`），不能用 x3（r11 已实测）。

### 4.3 `metadata.json`（Potree 2.0 + `anet` v1）

Potree 基础字段与 `Indexer::createMetadata` 完全相同：`version="2.0"`、`name`、`description`、`points`、`projection`、`hierarchy{firstChunkSize(22 的倍数), stepSize, depth}`、`offset`、`scale`、`spacing`、`boundingBox`、`encoding`、`attributes[]`。ANET_Q16 的约定：
- `attributes = [{name:"anet:pos", size:8, numElements:4, elementSize:2, type:"uint16"}, {name:"anet:col", size:4, numElements:4, elementSize:1, type:"uint8"}(, {name:"anet:ext", …})]`，由 schema 的 `prefixItems` 强制。
- `scale` 对 ANET_Q16 不起作用，固定写 `[0.001,0.001,0.001]`，只为保持容器兼容。

`anet` 扩展（19 个必填键，另有 `ext`、`root`、`twin`、`generator` 可选）：

| 键 | 说明 | NY 实例 |
|---|---|---|
| `formatVersion` | 常量 1。二进制布局任何改动都要升级；loader 遇到未知版本直接拒绝 | 1 |
| `frame` | 常量 `"world"`（点坐标是 world ENU；图层级的 `T_world_layer` 叠加在其上） | world |
| `streams` / `bytesPerPoint` | `["pos","col"]` 对应 12；`["pos","col","ext"]` 对应 16 | 12 |
| `pos{format,xyz,w}` / `col{format,rgb,a}` / `ext` | 见 §4.2 | oct16 / baked-height / null |
| `compression` | `none｜gzip` | none |
| `pointOrder` / `shuffleSeed` | 常量 `"shuffled"` / int | 1 |
| `sampling{method,G,leaf,minChild,B,seed}` | `grid-center`（默认）、`grid-random`、`potree-poisson` | grid-center, 64, 20000, 0, 21, 1 |
| `classTable{id,href}` | `anet-classes@1` | semantic/anet-classes@1.json |
| `nodeCount` | 不含 PROXY 重复记录 | 852 |
| `levelsByteEnd` | 各层结束偏移（字节，单调不减） | [67764, 424212, 2181252, 10197756, 42483000, 60000780] |
| `levelsPoints` | 累计点数 | [5647, 35351, 181771, 849813, 3540250, 5000065] |
| `levelsNodes` | 每层节点数 | [1, 4, 16, 64, 250, 517] |
| `firstScreenLevel` | 最小的 L 使 `levelsPoints[L] ≥ 1e5/根数`，且不超过 `4.5e5/根数` | 2 |
| `tightBounds` | 真实包围盒（Potree 的 boundingBox 是立方体） | z ∈ [−5.5, 287.0] |
| `hierarchyExt` | `{href:"hierarchy_ext.bin", recordSize:12, content:"subtree-aabb-u16"}` 或 null | ✓ |
| `stats` | `zP1, zP99, hagP1, hagP99, nnMedianM, nnSource, classHistogram{index:count}` | −0.64 / 167.28 / 0 / 167.31 / 1.016 |
| `root` | `{forestIndex, forestSize}` | 0 / 1 |
| `twin` | DEFAULT 孪生容器的 relHref，或 null | null |

从 r09 与 x01 移除的键：`units`、`upAxis`、`origin`（统一由 coordinate.json 提供）；`buffers[]`（由固定布局 + `pos/col/ext` 描述代替，前端不用写解释器）；`tightBoundsFile`（改为 `hierarchyExt.href`）；`nnMedian`（改名 `stats.nnMedianM`）；`roots`（移到 world.json）。

### 4.4 多根森林（苏州）

- **切分规则**（`forest=auto`）：若 `Lmax/Lmid ≥ 3`，则 `k = floor(Lmax/Lmid)`，`size = max(Lmax/k, Lmid, Lz)`，沿长轴切成 k 个相邻立方体，点按长轴坐标划入对应的根。另外，任何立方体大于 8 km、或 `precision` 门禁不过时，也按 2×2 切分（V0.5，未实现）。
- 每个根是完整的 Potree 2.0 容器：`visual/pointcloud/r-<i>/`，`metadata.anet.root = {forestIndex:i, forestSize:k}`。
- world.json 的 `roots[]` 记录每个根的 `{name, href, cubeMin, cubeSize, points, depth, firstScreenBytes}`。**根之间不能重叠**（校验器检查），选择器把所有根的节点放进**同一个**优先队列、共享一个点预算。
- 苏州实测：6 根，每根 734.5 m、深度 4，首屏合计 16.7 万点、2.0 MB、6 次 Range。

---

## 5. `world.json` v1（World Package 入口）

### 5.1 目录（在 00-index §3.4 基础上收紧，★ 为 V0.1 必须有）

```text
worlds/<id>/
├── world.json ★                     # 本节；no-cache
├── coordinate.json ★                # §2
├── visual/pointcloud/ ★             # §4（单根）或 visual/pointcloud/r-<i>/（森林）
├── visual/pointcloud-default/       # 可选：--twin-default，Potree 查看器对照用
├── geometry/terrain/dtm_10m.{json,f32} ★  # grid.schema.json（行序由南向北，originXY 为 (0,0) 格的左下角）
├── geometry/pointcloud/source/      # 全分辨率点云（物理权威，Geometry World）
├── geometry/{collision,voxel,sdf}/  # r06/r13；不进入 3D Tiles
├── semantic/anet-classes@1.json ★   # 码表副本（与 packages/contracts 中的一致，校验器比对）
├── semantic/zones.geojson           # 禁飞区、限制区（矢量体）
├── environment/…                    # G6：AWRV/AWSL manifest 用 coordinate.sha256 绑定
├── reconstruction/<session>/        # r02 Recon IR（alignment.json 用 T_world_engine）
└── qa/report.json
```

### 5.2 字段

| 键 | 说明 |
|---|---|
| `schemaVersion`, `id`, `name`, `nameZh`, `description`, `tags` | 基本信息 |
| `contentVersion` | `sha256("".join(f"{path}:{sha256}\n" for f in sorted(files)))[:12]`。缓存键：除 world.json 外，所有请求都带 `?v=<contentVersion>`，服务端返回 `Cache-Control: public, max-age=31536000, immutable`（206 响应同样适用） |
| `createdAt`, `generator{name,version,commit,params}` | 可复现性 |
| `dataset{name,url,citation,redistribution,notice}` | UrbanScene3D 的 `redistribution:false` 表示公开版必须在首次启动时于本地生成（00-index Q3） |
| `coordinate{href,sha256}` | 恒为 coordinate.json |
| `scaleStatus` | 与 coordinate 相同（校验器检查），供 UI 角标直接读取 |
| `bounds` | 等于 `coordinate.extent` |
| `layers[]` | `{id, type, role, format, href, status: ready｜building｜missing｜stale, default, T_world_layer?(只允许刚体 q,t), bytes, points, sha256, roots[]?, stats}` |
| `lod{errorTargetPx, firstScreen{points,bytes,requests}, budgets{desktop,integrated,software}}` | 首屏由所有根汇总；预算的默认值取自 r09 §3.4 |
| `render{defaultColorMode, zRangeM, hagRangeM, nnMedianM, pointSizeK, edl, syntheticGroundZ}` | SF 默认用 `hag`；苏州 `syntheticGroundZ=0` |
| `camera.home{position,target,fovDeg}` | 首次进入时的视角 |
| `thumbnail`, `stats`, `qa{status,href,messages}`, `files[]` | |

`format` 注册表：`potree2/anet-q16@1`、`potree2/default`、`potree2/brotli`、`copc@1.0`、`3dtiles@1.1`、`f32-grid@1`、`npz@1`、`geojson`、`anet-classes@1`、`anet-wind@1`（G6 AWRV）、`recon-ir@1`、`spz@2`、`mcap`。

**`T_world_layer` 只允许刚体**（schema 中 `rigid` 不含 `s`）。这样能保证导出 3D Tiles 1.1 和 2.0 时几何误差不被缩放（r10 不变量 1）。带尺度的对齐只能出现在 `registration` 或 `alignment.json` 中，并且在写入点云之前就要应用。

### 5.3 前端启动协议（`engine/pointcloud/io`）

```ts
// packages/contracts/gen/ts/world-package.d.ts（由 schema 生成）
import type { ANetWorldPackageManifestWorldJsonV1 as WorldJson, AnetExt } from '@anet/contracts';

export async function openWorld(base: string, f = fetch): Promise<OpenedWorld> {
  const world: WorldJson = await (await f(`${base}/world.json`, { cache: 'no-cache' })).json();
  const v = `?v=${world.contentVersion}`;
  const coord = await (await f(`${base}/coordinate.json${v}`)).json();
  const pc = world.layers.find(l => l.type === 'pointcloud' && l.role === 'visual' && l.default)!;
  const roots = await Promise.all(pc.roots!.map(async r => {
    const md = await (await f(`${base}/${r.href}metadata.json${v}`)).json();
    if (md.encoding !== 'ANET_Q16' || md.anet?.formatVersion !== 1) throw new Error('unsupported encoding');
    const [hier, ext] = await Promise.all([                         // 整体 GET，不用 Range（200 可被 CDN 压缩，n01）
      f(`${base}/${r.href}hierarchy.bin${v}`).then(x => x.arrayBuffer()),
      md.anet.hierarchyExt ? f(`${base}/${r.href}hierarchy_ext.bin${v}`).then(x => x.arrayBuffer()) : null]);
    const end = md.anet.levelsByteEnd[md.anet.firstScreenLevel];
    const first = f(`${base}/${r.href}octree.bin${v}`, { headers: { Range: `bytes=0-${end - 1}` } }).then(x => x.arrayBuffer());
    return { r, md, nodes: parseHierarchy(hier, md, ext), first };
  }));
  return { world, coord, roots };
}
export function parseHierarchy(buf: ArrayBuffer, md: PotreeMeta, ext: ArrayBuffer | null): PCNode[];       // r09 §3.1 语义；ext 按 22i↔12i 对应
export function viewQ16(buf: ArrayBuffer, byteOffset: number, n: number, bpp: 12 | 16):
  { pos: Uint16Array; col: Uint8Array; ext?: Uint8Array };                                                   // 零拷贝：new Uint16Array(buf, off, 4n) …
```

Python 侧签名（落地时的模块）：

```python
# world/package/validate.py   ← g03/worldpkg_validate.py
def validate_world(world_dir: Path, deep: bool = False) -> Report            # CLI: worldpkg validate worlds/* [--deep] [--json]
# world/pointcloud/tiler.py   ← g03/g03_build.py::build_octree / write_container / forest_split
def tile(points_world: np.ndarray, normals: np.ndarray | None, cls_index: np.ndarray, out_dir: Path, *, G=64, leaf=20000,
         compression: Literal["none", "gzip"] = "none", forest: Literal["auto", "off"] = "auto", seed=1,
         z_range: tuple[float, float], hag_range: tuple[float, float] | None, nn_median_m: float, twin_default=False) -> list[RootEntry]
# world/ingest/urbanscene3d.py ← g03/g03_build.py::ingest（x01 §3.3 CFG，SF=10.15）
def ingest(city: str, raw_dir: Path) -> IngestResult     # E(float64 → float32), N, hag, cls_index, terrain, coordinate dict
# CLI
#   worldpkg ingest urbanscene3d --city newyork --raw data/raw/urbanscene3d --out worlds/newyork
#   worldpkg tile worlds/newyork [--gzip] [--twin-default]
#   worldpkg validate worlds/* --deep
```

---

## 6. 校验器

### 6.1 结构层（JSON Schema 2020-12）

- `$id` 统一为 `https://schemas.anet-drone.dev/world/1/<name>.schema.json`，文件之间用相对 `$ref`（例如 `common.schema.json#/$defs/vec3`）。
- Python：`jsonschema 4.26` 加 `referencing.Registry`（`load_registry()` 会载入目录下全部 schema）。
- TS：`Ajv2020({strict:true, allErrors:true, strictTuples:false, allowUnionTypes:true})`，加 `ajv-formats 3`，逐个 `addSchema`。**Ajv strictTypes 要求带 `properties`、`required` 的子 schema 写明 `type`**，本次已全部补齐。
- 类型生成：`json-schema-to-typescript 16`，用一个聚合 schema 一次性编译（分开编译会产生重复的 `Vec3`），并设 `maxItems:-1`（否则 `levelsByteEnd` 会被展开成 21 元组）。
- 运行时策略：浏览器**只在 dev 和 test 下**跑 Ajv。生产环境只做几项廉价检查：`formatVersion`、立方体、`levelsByteEnd` 单调、`hierarchy.bin` 长度是 22 的倍数。原因是 schema 用了 `additionalProperties:false`，1.1 新增字段会让 1.0 的严格校验失败。**演进规则**：1.x 只能新增可选字段，生成器写出的 `schemaVersion` 必须与 CI 使用的 schema 一致；二进制布局的任何变化都要升级 `anet.formatVersion`。

### 6.2 语义层（`worldpkg_validate.py`，40 余条）

- **coordinate**：用 anchor 重新计算 `T_ecef_world`（旋转误差 ≤1e-6，平移误差 ≤1 cm）；`hMsl = hEll − N`；`T_world_source` 最后一行为 [0,0,0,1]；行列式符号与 `handedness` 一致；`∛|det| == unitsToMeters`；`R/scale` 正交；`upAxis` 映射到 +Z（夹角 <30°）；`precision` 三项重新计算；ULP ≥1 mm 或半径 >10 km 时告警；DTM 文件存在；synthetic 不能搭配 rtk 或 survey 的 scaleStatus。
- **world**：`coordinate.worldId == id`；`scaleStatus` 与 bounds 两边一致；图层 id 唯一；`status=ready` 的 href 存在；码表索引为 0..n−1 且 LAS 码不重复；栅格字节数等于 w×h×itemsize；各根的立方体与 metadata 一致、根之间不重叠、`points` 与 `depth` 与 metadata 一致；`lod.firstScreen` 等于各根之和；`files[]` 的大小与 `contentVersion` 可以复算；`--deep` 时复算全部 sha256。
- **容器**：立方体检查；`offset == min`；`spacing == cube/G`；`hierarchy.bin` 长度是 22 的倍数且 chunk 全覆盖（没有孤儿记录）；PROXY 完整解析；Σ`numPoints == points`；`depth` 一致；NORMAL/LEAF 与 mask 一致；payload 按层级优先 BFS 连续排列、从 0 开始、结束于 `octree.bin` 的大小；none 时 `byteSize == n·bpp` 且 4 字节对齐；`levelsByteEnd/Points/Nodes` 复算；`nodeCount`；首屏规则；首屏超过 8 MiB 时告警；`tightBounds` 在立方体内；直方图之和等于 points；类别索引小于码表长度；`hierarchy_ext` 长度为 records×12，min ≤ max，根记录与 `tightBounds` 相差不超过一个量化步长。
- **`--deep`**：逐节点读取（gzip 时先解压）；每个点都落在对应的 `hierarchy_ext` 包围盒内；法线解码后是单位向量；w==0 的点超过 1% 时告警；类别字节直方图与 `stats.classHistogram` 完全相等。六城 `--deep` 全部通过，每城约 2–4 s。

### 6.3 变异测试（在 NY 副本上逐条注入各单元原来的写法，确认都能被拦下）

| 注入 | 结果 |
|---|---|
| r11：`pos.w = "intensity"` | ✗ schema `anet/pos/w` |
| r11：`col.a = "lod-rank"` | ✗ schema `anet/col/a` |
| x01：类别字节直接写 LAS 65（屋顶） | ✗ schema `classHistogram` 的键超出 0..31 |
| 随机竞选（r09）的 `levelsByteEnd` 套到中心竞选的数据上 | ✗ 与复算结果不一致（2182464 ≠ 2181252） |
| r15 的 snake_case 键 | ✗ `additionalProperties`（world_frame） |
| `handedness=left`，但行列式 >0 | ✗ 语义检查 |
| SF 旧单位 10.1 配新矩阵 | ✗ `∛|det| ≠ unitsToMeters` |
| synthetic 锚点写 `georeferenced=true` | ✗ schema if/then |
| 非立方体 boundingBox | ✗ 语义检查 |
| 截断 `hierarchy_ext.bin` | ✗ 长度不符 |
| 图层 transform 带尺度 | ✗ schema `rigid` |

复现：`python .cache/research/g03/worldpkg_validate.py .cache/research/g03/instances/* --deep`；`node .cache/research/g03/ts/check.mjs`（需先 `npm i ajv@8.20.0 ajv-formats@3.0.1`）。

---

## 7. 六城实例（`g03_build.py`，G=64，LEAF=20000，中心优先，none）

| 城市 | 示意锚点（lat, lon, hMsl） | 地标依据 | 北向 / 尺度 | T_world_source 平移（缩放、旋转） | 根数 / 深度 | 首屏（层级 → 点 / 字节） | 法线翻转 | 默认着色 |
|---|---|---|---|---|---|---|---|---|
| Shenzhen | 22.51606, 113.94325, 12 | 华润大厦（HAG 381.3 m 对应 392.5 m） | assumed / assumed | (−318.432, −250.994, 31.908) | 1 / 5 | L2 → 124,673 / 1.50 MB | 16.4% | height |
| Shanghai | 31.22819, 121.53169, 4 | 上海中心（636.7 m） | verified / landmark | (−3184.088, 1308.725, 6.317) | 1 / 5 | L3 → 418,818 / 5.03 MB | 20.9% | height |
| New York | 40.71306, −74.00234, 6 | 70 Pine（287.4 m） | verified / landmark | (−17.047, 24.387, 16.502) | 1 / 5 | L2 → 181,771 / 2.18 MB | 11.4% | height |
| San Francisco | 37.77912, −122.42121, 40 | Transamerica（269.7 m） | verified / landmark | (0.370, 0.426, 164.727)，×10.15，Rz(+90°) | 1 / 5 | L2 → 108,268 / 1.30 MB | 3.6% | **hag** |
| Suzhou | 31.30, 120.62, 3（城市示意） | 无 | unknown / assumed | (−315.931, −689.212, −0.280)，(x, −z, y) | **6 / 4** | 每根 L1–L2 → 166,529 / 2.00 MB（6 次 Range） | 21.1% | height，合成地面 z=0 |
| Chicago | 41.88413, −87.62253, 179 | Willis（443.1 m） | verified / landmark | (9009.184, −3489.145, 209.317)，×1000，调平 2.03° | 1 / 6 | L3 → 352,915 / 4.23 MB | 2.7% | height |

- 除 SF 外，矩阵与 `x01/analysis.json` 一致，逐位复现，确认流水线正确。SF 改用 10.15，平移随之变化。
- 中心优先竞选与随机竞选的首屏点数几乎相同（NY 181,771 对 181,872），但均匀性明显更好（r09：NN<0.25 格的比例 0.038 对 0.145）。
- 这里的"法线翻转"比例包括 x01 §3.7 的两类修正：水平面朝下、立面朝内。x01 表中"朝下法线"只统计了前一类，所以两边数字口径不同。
- 每城 `octree.bin` 约 60 MB，单根城市的 `hierarchy.bin` 为 14.5–18.7 KB，`hierarchy_ext.bin` 为它的 12/22。单城端到端约 21–35 s（本机有并发负载）。其中建树加写出加子树包围盒（`anet.generator.seconds`）在 NY 为 17.7 s，其余为读取 PLY、DTM、法线修正和 sha256。

---

## 8. 风险与未决

| # | 风险 | 处置 |
|---|---|---|
| R1 | **示意锚点**的水平误差约 ±50 m（地标定位误差加上 SF 约 1° 的残余 yaw），高程误差最多 ±40 m（没有 geoid） | UI 必须显示"示意坐标"。只用于太阳、天空和经纬度显示；Mock 的空气密度用 `hMslM`，误差 40 m 对应的密度误差小于 0.5% |
| R2 | 类别索引只有 16 个空位，mask 为 u32，最多 32 类 | 超过 32 类时，升级码表 `@2` 并改用 256 位 mask 纹理（`formatVersion` 不变，因为字节语义没有变） |
| R3 | `hierarchy_ext` 的收益没有量化：俯视且无预算时选中点数只少 0–2% | 实现 r12 选择器时，在"水平视线加视锥裁剪加预算"条件下对比 cube-sphere 与 tight-box 两种方式；如果收益小于 5%，loader 可以不读，但格式保留 |
| R4 | oct16 在 −Z 附近的量化：0x0000 被改写成 0xFFFF 后，恰好 −Z 的点会以 0xFFFF 形式出现 | 解码结果仍是 −Z，已实测 |
| R5 | 12 B/点放不下强度、HAG 和回波信息 | 用 `ext` 流（16 B/点），真实 LiDAR 数据才启用。页池（r13）按 bpp 分页；WebGL2 纹理方案需要第 4 个 word，正好容纳 ext |
| R6 | `contentVersion` 依赖全量 sha256 | 实测 60 MB 的 `octree.bin` 约 0.35 s，可以接受；增量构建时缓存各文件的 sha |
| R7 | 3D Tiles 导出时的子节点序：Potree 为 `x<<2\|y<<1\|z`，3D Tiles 隐式八叉树为 `x\|y<<1\|z<<2`（Morton，x 在最低位）；3D Tiles 的 glTF 是 Y-up | 导出器需要重映射子节点序，并加上 Y-up→Z-up 的节点变换。`root.transform` 只在 `georeferenced` 时取 `T_ecef_world`（按列主序平铺） |
| R8 | 旧产物使用了旧约定 | 需要重建或废弃的有：x01 的 `enu_*.npy`（SF 为 10.1）、r17 SF 风场库（原始单位，g06 已指出）、n05 trial 数据（`u16 x,y,z,pad`，需按 v1 重新生成）、x01 类别 npy（65 表示屋顶） |
| R9 | 苏州北向与手性仍未验证 | `trueNorth.confidence="unknown"`，UI 不显示指北针 |

---

## 9. 对后续文档与实现的直接修改

1. **`packages/contracts/`**：把 `g03/schemas/*.schema.json` 放到 `packages/contracts/schemas/world/`，把 `classes/anet-classes-v1.json` 放到 `packages/contracts/classes/`，把生成的类型放到 `packages/contracts/gen/ts/world-package.d.ts`（由 `tools/contracts/gen.mjs` 生成）。CI 执行 `worldpkg validate worlds/* --deep` 和 `node check.mjs`。
2. **系统架构说明书的"跨模块契约"**：加入 §2.1 的 7 条规则、帧名表和矩阵记法；声明"world 只指 ENU@anchor"。
3. **研究笔记的名词替换**（写 PRD 时照此执行）：`T_enu_world`（r01、r07）、`T_enu_map`（r08）改为 `T_world_map`；`T_enu_engine`（r02）改为 `T_world_engine`；`T_enu_src`（x01）、`T_source_to_world`（r09）改为 `T_world_source`；`levelsByteEnd` 保留；`nnMedian` 改为 `stats.nnMedianM`；x01 的类别 65（屋顶）改为索引 5 / LAS 6；r11 的 `qpos.w = intensity`、`rgba.a = LOD 秩` 作废；n01、r12 中"三个查看器可以直接对照"改为"通过 DEFAULT 孪生容器对照"。
4. **tiler 与 ingest 的实现顺序**：先搬 schema 和校验器（0.5 天），再移植 `g03_build.py` 为 `world/ingest/urbanscene3d.py` 加 `world/pointcloud/tiler.py`（1–1.5 天），最后写前端的 `openWorld`、`parseHierarchy`、`viewQ16` 与 GLSL/TSL 解码（1 天）。首次启动生成六城 World Package 约需 3 分钟单线程；可以用 `ProcessPool` 按城市并行。
