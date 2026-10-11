# r2 研究笔记：GLB → World Package 工作流（D3 事件沙盘的世界资产）

> 研究单元：r2-glb-world ｜ 日期：2026-10-11 ｜ 对应设计：用户设计讨论 `/data/projs/anet-drone-data/design-r1.md` 第四节（GLB 派生多表达）、第十节（M22 World Asset Import）；现有规范 [16 World 数据规范](../../16-World数据规范.md) §3、§6、§7、§16；M03、M04、M05、M06 PRD
>
> 输入数据（均为 Sketchfab 导出的带纹理摄影测量三角网格，不是 3DGS，CC-BY-4.0）：
> - `01-small-town/an_entire_small_town_scanned_by_a_drone.glb`（下称 **town**，作者 Mitko / bg3d）
> - `01-small-town/oryahovo_-_crop_1_-_metashape_texture_problems.glb`（下称 **crop**，同一作者、同一 Metashape 工程的另一次导出）
> - `02-hilltop-village/anciolina_3d_hilltop_village_scan_snowy.glb`（下称 **hill**，作者 dok11）
>
> 实测环境：本机 Intel Xeon E5-2603 v4（8 核 1.7 GHz，62 GB，无 GPU）；dmax 容器 `d3-r2-glb-world`（Xeon Gold 6326，`--cpus 16`，GPU 2、3 两张 A40，驱动 580.95）。本机 Python 环境是单元自带的 venv（open3d-cpu 0.20.0、warp-lang 1.18.0、trimesh 5.1.1、meshoptimizer 0.2.30a0、fast-simplification 0.2.0、pymeshlab 2025.7、xatlas 0.0.11），Node 工具为 @recast-navigation 0.43.1、@gltf-transform 4.5.1、gltfpack 1.3、KTX-Software 4.4.2，浏览器测试用项目自带的 three 0.186.1 与 Playwright Chromium（SwiftShader）。
>
> 脚本与原始结果：`/data/projs/anet-drone/.cache/research/d3/r2-glb-world/`（`scripts/`、`results/`、`results/dmax/`、`out/<world>/` 中的图片与小文件、`setup.sh` 可重建环境）。dmax 工作目录 `/data/anet-drone4d-d3/r2-glb-world/`；实验结束容器已停止并删除，保留镜像 `d3-r2-glb-world:0.1`（2.59 GB）。
>
> 本文没有修改项目代码，也没有修改 `docs/research/d3/` 以外的文档。

---

## 0. 结论速览

| 主题 | 结论 | 主要实测依据 | 落点 |
|---|---|---|---|
| 米制尺度 | **town 每个 glTF 单位 = 8.374 m（±0.5%）**，crop = 20.935 m/单位，hill = 2.49 m/单位（±2%）。Sketchfab 节点矩阵里的 ×2.5 只是上传者为了好看设的，与米制无关 | OSM 道路网 FFT 相关 + 倒角精配（E03）；0.25 m/像素正射图上 OSM 建筑轮廓逐栋吻合；SRTM 高程回归竖向 k/s = 0.96 | M22 必做步骤，写入 `coordinate.source.unitsToMeters` 与 `scaleStatus = landmark` |
| 与兄弟单元的分歧 | r3 估计 town 需"再 ×4"（2140 m 宽），**低估约 2.09 倍**；r4、r5 中凡按 glTF 单位或 ×4 换算的米数（航高、量程、点密度、碰撞误差）需要按本文尺度重算；r6 对 crop 用的 20.9 m/单位与本文一致 | §2.3 的三条独立证据 | 跨单元修正 |
| 轴向与镜像 | 三份 GLB 经节点矩阵后都是标准 glTF Y-up、右手，无镜像（det > 0）；World ENU = (x, −z, y)·s，与项目的 three 约定 `(x,y,z) = (E,U,−N)` 互逆 | 面积加权法线：+Y 朝向占 48–62%，−Y 只有 0.5–1.6% | `T_world_source` |
| 真北与调平 | town/crop 模型 +Y 需绕 Z 转 **168.04°** 才是真北；模型**倾斜约 1.25°**（2.5 km 尺度上 55 m 高差误差），hill 倾斜约 1.1°。用 DEM 回归或"墙面竖直度"两种方法都能测出 | SRTM 拟合 RMSE 从 9.9 m 降到 2.9 m；墙面法则给 0.88°（town）与 1.12°（hill） | coordinate 增加调平证据，`trueNorth = verified` |
| 三份数据的定位 | crop 与 town 共用同一 Metashape 局部坐标（crop 顶点到 town 网格距离中位数 0.26 m），是同一地区更细的重建。town：4.6 × 2.8 km、边长 3.3 m、纹素 28 cm；crop：2.5 × 1.7 km、边长 1.9 m、纹素 19 cm；hill：122 × 145 m、边长 13 cm、纹素 2.5 cm | E01、E04、E09b | 建议 crop 作城镇级事件主场景，hill 作街巷级（人物可辨）场景，town 作大范围 LiDAR 探索与远景 |
| 纹理与材质 | town/crop 为 `KHR_materials_unlit`（双面），hill 为 `KHR_materials_specular`（specularFactor = 0），本质上都是烘焙了光照的反照率。不能重新打光，阴影方向固定在拍摄时刻；hill 的 8192² 纹理超过移动 GPU 的 4096 上限 | E01 | 渲染一律按 unlit 处理；时间/天气只能叠加，不能重算阴影（r3） |
| 精细 BVH | Open3D `RaycastingScene`（Embree 4）是 CPU 首选：3M 三角形建树 2.5–4 s（本机）、1.1–1.4 s（dmax），常驻约 +250 MB；本机 5–12 M 射线/s，dmax 16 核持续 18–33 M/s（短批次 30–100 M/s）；Warp CUDA（A40）LBVH 建树 17–38 ms，45–119 M 射线/s，与 CPU 结果命中 100% 一致、深度差 < 1 cm 占 99.9999%。trimesh + embreex 只有 0.6–0.9 M/s，不用 | E05、E10、E15 | `mesh.raycast` 资产 + 每世界共享的几何/传感器服务 |
| DSM/DTM 与 M04 | 网格竖向射线可直接生成 M04 兼容的 `dsm_2m`/`dtm_10m`/`dsm_2m_n`（M04 代码写死 2 m、10 m 格），现有 WorldQuery、Grid25、规划不用改；另出 0.5 m 精细 DSM 作新层 | town 2 m DSM 本机 32 s、dmax 8.7 s；0.5 m 本机 284 s、dmax 34 s（本机要分块流式，否则 RSS 23.5 GB） | M22 输出 + M04 原样消费 |
| 碰撞网格 | 小尺度精细网格（hill）减到 10% 后 p95 误差 3–4 cm，减到 2% 为 9–11 cm；城镇级粗网格（crop/town）本身边长 2–3 m，减到 10% 后 p95 1.3–2.1 m。meshoptimizer `simplify` 速度/质量比最好（3M 三角形 9 s）；Open3D、pymeshlab 质量略好但慢 10–15 倍 | E06 | FleetSim 用全分辨率 raycast 网格；Gazebo 用 meshopt 减面网格 |
| 占据体素 / ESDF | hill 0.25 m 稠密 3 态体素 52.7M 格，u8 52.7 MB，zlib 后 0.82 MB；EDT 生成 ESDF 35 s（单线程）。crop 1 m 稠密就有 10.5 亿格，必须用稀疏分块。约 7–9% 的竖向柱子有多层表面（屋檐、树冠），2.5D DSM 表达不了 | E07 | `awr-occ@1` 稀疏分块；未观测区域标 unknown，不当空地 |
| NavMesh | Recast/Detour 的 WASM 版（@recast-navigation）可在 Node 与浏览器共用同一份二进制。crop 行人网格烘焙 237 s（单线程），最大连通岛 1.13 km²，就是镇上的街道网；用种子洪泛裁剪后随机路径 100% 可达、平均 4.6 ms；OSM 路口 92% 落在网格 3 m 内。车辆网格（坡度 15°、可跨 0.25 m）碎成 2221 块，不可用 | E12、E13 | 行人用 NavMesh，车辆用配准后的 OSM 道路图 |
| 摄影测量的坑 | 树冠被 Recast 当成可走的地面（"走在树顶上"）；屋顶也是可走岛。直接删除树冠/屋顶三角形会把街道网打碎（最大岛从 1.13 km² 掉到 0.23 km²）；改用"语义代价 + OSM 道路先验"的加权寻路后，路径 74% 落在 OSM 道路走廊、只有 2% 经过植被 | E13b、E13c | 区域代价而非删除；OSM 先验；编辑器人工校正 |
| 锚点 | 用配准后的 OSM 自动播种 anchors：crop 得到 151 个路口、70 栋建筑、30 个 POI（渡口、博物馆、汽车站等），吸附到 NavMesh 的中位距离 0.84 m；渡口→博物馆→汽车站的事件路径分别 1570 m、369 m，计算 20 ms、0.8 ms | E12 | `semantic/anchors.json` 草稿 + 沙盘编辑器确认 |
| 浏览器 LOD | 原始 GLB（144–290 MB、16 张 4K JPEG，RGBA8 上 GPU 约 1.4 GB）不能直接进浏览器。gltfpack `-cc -tc`（meshopt + KTX2）把 hill 压到 22–32 MB、town 69 MB、crop 74 MB；SwiftShader（Tier S）每秒只能光栅约 0.6–0.7 M 三角形，3M 三角形一帧 4 s | E08 | Tier S 用"网格采样彩色点云"走现有 M05；Tier B/A 用 3D Tiles 网格（REPLACE，KTX2 + meshopt） |
| 瓦片纹理 | 城镇级网格的每个空间瓦片会引用全部 16 张图集（UV 碎片 1.5–1.8 万个/瓦片），必须按瓦片"图集重排"（Obj2Tiles 式：拷贝用到的 UV 碎片矩形）；每瓦片所需纹素约 6.8–7.9 M，可放进一张 4096²。xatlas 重新展开 18 万三角形的瓦片 24 CPU 分钟未完成，不可行 | E11b | M22 的 tile 步骤 |
| 几何不完整 | 扫描覆盖只占包围盒的 61%（crop）、74%（hill）；crop 覆盖区内另有 3% 的无数据洞、147 个开放边界环、1.7% 面积的拉伸三角形（插值出来的未观测面）。全部输出为 `quality` 位掩码栅格 + GeoJSON，border 改为覆盖多边形内缩 | E09 | `semantic.quality` 层；传感器与事件都要读 |
| 进程内存与准入 | 一个 3M 三角形世界的网格 + BVH 约 300 MB 常驻，而 D2 沙盒每会话匿名页预算只有 140–155 MB | E15 | BVH 放每世界一个的共享服务，不进每会话 sim-core |
| world.json 扩展 | 以 `schemaVersion 1.1.0` + `profile = "d3-glb"` 做**加法式**扩展：保留 D1 必需图层（点云、DSM/DTM、zones、env），D2 运行时不改就能加载 GLB 世界；新增 `navmesh`、`navgraph`、`anchors`、`quality` 图层类型与 6 种格式注册名 | §5 | M00 合入 schema，M03 生成 |
| CLI | `worldpkg glb inspect / register / build / export-gazebo / anchors`，构建分 13 个阶段，可选 `--gpu`。单世界全流程本机约 15–20 分钟、dmax 约 4–6 分钟（NavMesh 单线程是长板） | §6、§7 | M22 + job-worker |

**关键结论（实现者先读这几条）**

1. **Sketchfab 的 GLB 不是米制，节点矩阵里的缩放也不可信。** town 根节点有 ×2.5，crop 是 ×1，而两者共用同一个局部坐标系，说明缩放是上传者随手设的。尺度必须外部标定。本文用 OSM 道路网做 FFT 相似变换搜索，一次得到尺度、真北和平移；town 得 8.374 m/单位，比 r3 的估计（×4）大 2.09 倍。三条独立证据都支持本文结果：OSM 道路中心线与正射图道路吻合，OSM 建筑轮廓在 0.25 m/像素下逐栋吻合，SRTM 高程回归竖向尺度比 0.96。
2. **摄影测量模型会倾斜。** town 在 SRTM 回归中有 1.25° 的系统倾斜，墙面竖直度法独立给出 0.88°，方向一致。2.5 km 尺度下这会造成几十米的高度误差，对风场、AGL、航高和 LiDAR 都有影响。导入时必须调平，并把证据写进 coordinate。
3. **城镇级网格很粗**（crop 三角形边长中位数 1.9 m、纹素 19 cm）：适合全局观测和 LiDAR 建图；相机在 165 m（720p）到 500 m（4K）以内时背景清晰度受纹理限制，比真实航拍模糊。动态人物、车辆由动态网格渲染，不受影响。hill 是 2.5 cm 纹素、13 cm 边长的街巷级场景，但只有 122 × 145 m。两份 GLB 应承担不同的实验目标。
4. **Geometry World 的真值层要换成三角网格 BVH，但 DSM 栅格不能丢。** 现有 M04/M10/Grid25 全部依赖 2 m DSM 与 10 m DTM，且代码写死格距；网格射线生成的 DSM 让 D2 能力原样可用。精确视线、LiDAR、深度相机走新的 `mesh.raycast`。
5. **NavMesh 是可行的，但不能全自动。** Recast 能自动找出街道网，但会把树冠、屋顶、平静水面当作可走面。正确做法是：Recast 烘焙 → 种子洪泛裁剪 → 语义区域代价 → OSM 道路先验 → 编辑器人工补"楼梯/通道"连接。车辆直接用 OSM 道路图。
6. **"几何不完整"必须进入世界包。** 包围盒里有 26–39% 是扫描范围外，覆盖范围内仍有洞、拉伸面、多层结构。约定：不完整区域是 unknown，不是 free；传感器射线打到无数据区要打标记；border 用覆盖多边形内缩，不再用包围盒。
7. **浏览器端分层**：Tier S 无法渲染百万级三角形，必须继续用点云（由网格采样出带真实颜色的点，进现有 ANET_Q16 管线）；有硬件 GPU 的 Tier B/A 走 3D Tiles 网格瓦片。原始 `appearance.glb` 只供服务端渲染（r3），不下发浏览器。

---

## 1. 输入数据实测（E01、E04）

脚本：`scripts/glb_inspect.py`（零依赖解析 GLB JSON 块与 JPEG/PNG 尺寸）、`scripts/glbworld.py`（numpy 直接解析二进制块、展平节点矩阵）、`scripts/e01_load.py`、`scripts/e04_crop_frame.py`。结果：`results/glb_inspect.json`、`results/e01_load.json`、`results/e04_crop_frame.json`。

| 项 | town | crop | hill |
|---|---|---|---|
| 文件 | 230.6 MB | 289.7 MB | 143.8 MB |
| 三角形 | 1,996,759 | 2,879,110 | 2,999,995 |
| glTF 顶点 / 去重后位置 | 3,087,397 / 1,002,786 | 4,432,293 / 1,442,582 | 2,444,257 / 1,502,358 |
| 图元（primitive） | 49 | 80 | 38 |
| 索引类型 | uint32 | uint32 | uint32 |
| 纹理 | 16 × 4096² JPEG，107.8 MB | 16 × 4096² JPEG，113.3 MB | 1 × 8192² JPEG，29.6 MB |
| 材质扩展 | `KHR_materials_unlit`，doubleSided | `KHR_materials_unlit`，doubleSided | `KHR_materials_specular`（specularFactor 0），单面 |
| 根节点矩阵 | 缩放 2.5 + 近似 180° 旋转（det = +15.6，无镜像） | 缩放 1 + 同类旋转 | 两个 ±90° 节点互相抵消，净为单位阵 |
| glTF 场景包围盒（单位） | 535.0 × 28.1 × 320.3 | 119.5 × 10.3 × 75.6 | 57.5 × 17.6 × 47.8 |
| 朝 +Y 面积占比 / 朝 −Y | 62.3% / 0.5% | 48.4% / 0.8% | 51.2% / 1.6% |
| 本机解析 + 展平耗时 | 1.76 s | 1.96 s | 0.81 s |
| 解析后峰值 RSS | 1.0 GB | 1.6 GB | 1.6 GB |

说明：

1. 顶点数约为去重位置数的 1.6–3.1 倍，原因是 UV 接缝处顶点按纹理坐标拆分。碰撞、射线、体素都应该先按位置焊接（§4.1）。
2. "朝向"统计确认了经过节点矩阵后的场景是 Y-up：地面与屋顶贡献了约一半面积，而朝下的面几乎没有。
3. **crop 与 town 共用同一个局部坐标系**：用两份文件根节点矩阵求出 crop → town 的相似变换（缩放 2.5、旋转差 0.24°），把 crop 的 5 万个顶点映射到 town 网格上，距离中位数 0.26 m、p90 0.80 m（按 §2 的米制换算）。所以 crop 不是"一小块"，而是同一 Metashape 工程对镇中心 2.5 × 1.7 km 范围的更细重建（其标题"texture problems"指纹理拼接问题，§4.9 有量化）。
4. 两份 Oryahovo 数据和 hill 都是 CC-BY-4.0：可以再分发，但 UI 与 NOTICE 必须署名（作者、来源 URL、许可）。

---

## 2. 坐标：glTF Y-up → World ENU、米制尺度、真北、调平（E03）

### 2.1 轴向

glTF 规定 Y-up、右手、米。World 帧为 ENU、Z-up、右手（[16 §3.3](../../16-World数据规范.md)）。换算：

```text
p_enu = R_level · Rz(yaw) · s · R_enu_gltf · p_gltf + t
R_enu_gltf = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]      # E = x, N = -z, U = y（det = +1）
```

它恰好是 coordinate.json 约定 `render = "three Y-up: (x,y,z)=(E,U,-N)"` 的逆。`p_gltf` 指**经过全部节点矩阵之后**的场景坐标（本文称 glTF 场景帧）。Gazebo 直接读取原始 GLB 时需要 roll = +π/2 才不会"竖起来"（r5 已复现），这与上式一致。

### 2.2 尺度、真北与平移：OSM 道路网相似变换搜索

方法（`scripts/e03_roadmask.py`、`e03_register.py`、`e03b_translate.py`）：

1. 用 Open3D 竖向射线生成 0.25 单位/像素的纹理正射图与 DSM（§4.3 同一套代码）。
2. 道路掩码：低饱和度（max − min < 28）、亮度 110–235、局部高差（DSM 减去 6 单位窗口最小值）< 0.6 单位，再做一次开运算。town 掩码占 1.8% 像素，主干道与街区网格清晰。
3. OSM 数据：OSM API 0.6 `map` 调用（Oryahovo 7823 个节点、562 条 way；Anciolina 1902 节点、115 way）。Overpass 三个公共端点当天都超时或 500，**生产中应改用 Geofabrik 离线抽取**。
4. 搜索：对每个（yaw，尺度）把 OSM 道路中心线变换到模型单位并栅格化，与模型掩码做 FFT 互相关取峰值。粗搜 yaw 0–360° 步长 4°、尺度 4–14 m/单位步长 0.5（2 单位/像素）；细搜 ±4° 步长 0.5°、±0.6 步长 0.05（1 单位/像素）。
5. 精配：以道路掩码的距离变换为代价（截断 3 单位），Nelder–Mead 同时优化 yaw、尺度、平移。

| 世界 | 粗搜峰值 | 细搜峰值 | 精配结果 | 耗时（本机 8 核） |
|---|---|---|---|---|
| town（道路网） | 0.357（168°，8.5）；第二名 0.309 | 0.328（168.0°，8.40） | **yaw 168.04°，s = 8.374 m/单位** | 粗搜 65 s + 细搜 48 s + 精配约 30 s |
| hill（建筑轮廓，因道路太少改用 46 个 OSM 建筑） | 0.595（316°，2.6） | 0.621（316.0°，2.49；2.51、2.53 并列） | **yaw 316°，s = 2.49–2.53 m/单位** | 223 s |

crop 不单独搜索：它通过 §1 的 crop → town 变换继承 town 的结果，即 **20.935 m/crop 单位**。

### 2.3 三条独立验证（以及与 r3 的分歧）

1. **OSM 建筑轮廓逐栋吻合**：把 OSM 道路与建筑画到 crop 的世界帧正射图上（0.25 m/像素，400 × 300 m 窗口，`out/crop/osm_overlay_0.25.jpg`），公寓楼轮廓的长宽与位置都对齐，残差在 1–2 m 内；hill 的 46 个建筑轮廓同样吻合（`out/hill/osm_overlay_0.05.jpg`）。尺度若偏 2 倍，轮廓会差一倍大小，一眼可见。
2. **SRTM 竖向回归**（`e03c_vertical.py`）：在道路像素上随机取 400 点，按配准换算经纬度查询 SRTM 30 m 与 ASTER 30 m（opentopodata）。只拟合 `h = k·z + c` 时 k/s = 1.15、RMSE 9.9 m；加入水平梯度项 `h = k·z + gE·E + gN·N + c` 后 **k/s = 0.962、RMSE 2.9 m**，倾斜 1.25°。竖向尺度与水平尺度一致（差异在 SRTM 精度内），倾斜是真实存在的系统误差。
3. **地物尺寸**：0.008 单位/像素的局部正射图上，路边停放的小汽车长约 0.56–0.68 单位，即 4.7–5.7 m；独栋民居 1.2–1.5 单位，即 10–13 m；镇中心主路（含路肩）约 1.5 单位，即 12.6 m。都在常识范围内。

**与 r3 的分歧**：r3（`r3-camera-render.md` §2.2）按道路宽"1–1.5 单位"、车辆"约 1 单位"推断 town 还需 ×4，得到 2140 m 宽、112 m 高差。按 ×4，小汽车只有 2.2–2.7 m，主路 4–6 m，镇区高差 112 m，而 SRTM 实测为 26–224 m（约 200 m）。**正确值是 ×8.374，约为 r3 的 2.09 倍**。r4 的"town ×2.5 看起来合理"、r5 用 r6 帧单位计的米数也都需要按本文换算。r6 对 crop 用 20.9 m/单位，与本文一致。

### 2.4 调平：DEM 回归与墙面竖直度

| 方法 | 原理 | town | hill | 适用 |
|---|---|---|---|---|
| DEM 回归（`e03c_vertical.py`） | 地面点高程对 (z, E, N) 线性回归，取水平梯度 | gE = −0.0007，gN = −0.0218，**1.25°** | DEM 30 m 对 145 m 小场景太粗，回归无意义（倾斜 12.7°、k/s 0.29，不可信） | 公里级场景 |
| 墙面竖直度（`e03f_wall_tilt.py`） | 近竖直面（\|n_z\| < 0.35）在模型倾斜时法线 z 分量与水平朝向相关：`n_z ≈ −gE·n_x − gN·n_y`，面积加权 + Huber IRLS | gE = −0.0031，gN = −0.0151，**0.88°**（与 DEM 法方向一致，量级差 0.4°） | gE = 0.0156，gN = 0.0117，**1.12°** | 有建筑的任意尺度场景，不需要外部数据 |

建议：公里级场景以 DEM 回归为准、墙面法交叉校验；小场景用墙面法；两者都没有时保持不调平，`qa` 记录"未调平"。调平写成绕水平轴的小旋转（保持 `T_world_source` 为相似变换，不用剪切）。

### 2.5 原点、锚点与精度

| 世界 | 原点经纬度（世界帧 XY 原点） | 原点地面 hMsl（DEM） | 世界帧包围盒（m，z 已按 DTM 中位数归零） | `unitsToMeters` |
|---|---|---|---|---|
| town | 43.731199° N，23.962713° E | 约 165 m（SRTM，9 点离散 4 m） | [−2287, −1393, −166] – [2287, 1393, 85] | 8.37399 |
| crop | 43.735247° N，23.961284° E | 约 115 m | [−1256, −850, −103] – [1256, 850, 138] | 20.93497 |
| hill | 43.594844° N，11.683000° E | 约 896 m（EU-DEM 25 m） | [−61, −73, −19] – [61, 73, 27] | 2.49 |

- XY 原点取包围盒中心，Z 原点取 DTM 中位数（沿用 16 §3.3 synthetic 锚点规则）。完整的 4×4 `T_world_source` 见 `results/e17_coordinate_examples.json`。
- 最大半径 2.7 km，float32 ULP 0.24 mm，满足 `float32UlpMm < 1` 与 `maxRadiusM ≤ 10000`。
- 这三个世界**真实存在且已配准到地图**，水平精度约 2–5 m、竖向约 5 m。现有 `anchor.kind` 只有 rtk/survey/gnss/synthetic，而 synthetic 要求 `georeferenced = false`。建议新增 `anchor.kind = "map"`（地图/DEM 配准，georeferenced = true，不可用于真实导航，可用于太阳、天气、真实地名与开放数据叠加），并在 `registration.method` 枚举中增加 `osm-road-correlation`、`osm-footprint-correlation`。

### 2.6 没有 OSM 覆盖时的退路

1. 人工控制点：在沙盘编辑器里点 2 个以上已知距离的点（或 3 个带经纬度的点），按最小二乘求相似变换，`scaleStatus = landmark`。
2. 地物统计：车道宽 3.0–3.75 m、小汽车 4.2–4.8 m、标准门高约 2.0–2.1 m、层高 2.8–3.2 m，联合回归给出 ±10–20% 的尺度，`scaleStatus = assumed`，UI 必须显示"尺度未标定"。
3. 只有局部场景时，用 DEM 高差与 SRTM 交叉检查（公里级以上才有效）。

---

## 3. 纹理与材质

| 项 | 结论 | 依据 |
|---|---|---|
| 材质语义 | 三份都是烘焙了光照的反照率贴图。unlit 是正确的显示方式；hill 的 specular 扩展 specularFactor = 0，等价于漫反射，建议统一按 unlit 渲染，避免场景灯光二次照明 | E01 |
| 阴影与时间 | 阴影方向固定在拍摄时刻（Oryahovo 为夏季正午附近，hill 为冬季低太阳角）。仿真日历与太阳位置（ADR-102）只能影响动态实体的光照与天空，**不能重算静态场景阴影**；`world.json.sensorHints` 应记录拍摄季节与大致太阳方位（r3 已建议估计） | 目视 |
| 纹素尺寸（面积加权中位数） | town 28 cm，crop 19 cm，hill 2.5 cm；墙面与屋顶几乎相同（town 29 / 28 cm，crop 19.1 / 18.4 cm，hill 2.45 / 2.41 cm），说明图集按面积均匀分配，不能用纹素密度识别"立面质量差" | E09b |
| GPU 纹理内存 | 16 × 4096² RGBA8 加 mip 约 1.43 GB；KTX2（BC7/ASTC 1 B/纹素）约 357 MB；BC1/ETC1S 转码为 0.5 B/纹素约 180 MB | E08 |
| 纹理尺寸上限 | hill 的 8192² 超过 WebGL2 移动设备常见的 4096；gltfpack `-tl 4096` 处理 | E08 |
| 纹理受限距离 | 相机像素地面尺寸 = 斜距 × 单像素视角（IFOV）。斜距小于 `纹素 / IFOV` 时，图像清晰度受纹理而不是相机限制。84° 水平视场：4K（IFOV 0.38 mrad）对应 crop 约 500 m、town 约 730 m、hill 约 65 m；720p（1.15 mrad）对应 crop 约 165 m、town 约 245 m、hill 约 22 m。即在城镇级 GLB 上，常规航高（50–120 m）的相机图像背景一定比真实航拍模糊。写入 `sensorHints.texelM`，由传感器配置换算 | 计算 |

---

## 4. 派生资产与实测

### 4.1 Raycast 网格（焊接、清理、拓扑体检）

`scripts/e05_pipeline.py` 第 1–3 步：按 1 mm 网格焊接位置 → 去掉退化面与零面积面 → 统计重复面、边界边、非流形边、连通分量。输出 `raycast_xyz.f32`（float32 world ENU）、`raycast_tri.u32`、`raycast_prim.u16`（来源图元编号，用于回查纹理与语义）。

| 项 | town | crop | hill |
|---|---|---|---|
| 焊接后顶点 / 三角形 | 1,002,786 / 1,996,759 | 1,442,582 / 2,879,110 | 1,502,358 / 2,999,995 |
| 重复面 / 退化面 | 302 / 0 | 256 / 0 | 0 / 0 |
| 边界边（总长） | 7,355（23.0 km） | 6,040（11.1 km） | 3,341（669 m） |
| 非流形边 | 208 | 182 | 3 |
| 连通分量（最大占比；< 100 面的碎块） | 1,655（99.0%；1,636） | 1,421（98.9%；1,360） | 363（95.5%；251） |
| 边长中位数 / p95 | 3.30 / 7.83 m | 1.88 / 4.10 m | 0.13 / 0.26 m |
| 表面积 | 11.72 km² | 5.20 km² | 25,281 m² |
| 文件大小（xyz + tri） | 36.0 MB | 51.9 MB | 54.0 MB |

焊接是整条流水线里最慢的 numpy 步骤（`np.unique(axis=0)`：本机 17–25 s，dmax 6.6–9.3 s），实现时应换成 meshoptimizer `generate_vertex_remap` 或哈希焊接。

### 4.2 精细 BVH 与光线查询

脚本：`e05_pipeline.py` 第 4 步（Open3D）、`e10_gpu_raycast.py`（Warp CUDA 对比 Open3D）、`e15_bvh_mem.py`（建树时间与内存）。

**Open3D RaycastingScene（Embree 4，CPU）**

| 负载 | 本机 8 核（M 射线/s） | dmax 16 核，1M 射线（M/s） | dmax 16 核，10M 射线持续（M/s） |
|---|---|---|---|
| 竖向 1M（DSM） | 5.2–5.9 | 30–62 | 18–27 |
| 全向/类 LiDAR 1M | 6.0–12.3 | 62–101 | 20–33 |
| 针孔 640 × 480（30.7 万射线） | 6.2–7.1（每帧 43–50 ms） | 41–47（每帧 6.6–7.4 ms） | — |
| 针孔 1920 × 1080 | 5.6–8.6（每帧 0.24–0.37 s） | 20–28（每帧 75–103 ms） | — |
| MID-360 单帧 2 万射线 | 3.5–8.6 ms | — | — |
| 最近点距离 20 万 | 1.5–2.7 s | 0.47–1.1 s | 0.31–0.93 s |

**Warp 1.18 CUDA（A40）对比**

| 项 | town（2.0M 面） | crop（2.9M 面） | hill（3.0M 面） |
|---|---|---|---|
| 建树 LBVH / median / SAH | 17 ms / 0.97 s / 1.36 s | 38 ms / 1.53 s / 1.85 s | 25 ms / 1.67 s / 1.97 s |
| 竖向 10M 射线 | 50 M/s（CPU 27 M/s） | 45 M/s（CPU 20 M/s） | 62 M/s（CPU 18 M/s） |
| 全向 10M 射线 | 119 M/s（CPU 33 M/s） | 92 M/s（CPU 20 M/s） | 115 M/s（CPU 20 M/s） |
| 最近点 2M | 0.25 s | 0.30 s | 0.54 s |
| 与 Open3D 一致性 | 命中 100% 一致；深度差 < 1 cm 99.99997% | 99.99984% | 99.99994% |

**进程内存（本机，`e15_bvh_mem.py`）**：Open3D 建树 2.5–4.1 s，RSS 增加 178–253 MB（约 85 B/三角形），再加网格本身 24–54 MB。trimesh + embreex 建树 6–10 s、只有 0.6–0.9 M 射线/s（Python 包装开销），不推荐。

结论：

1. 单机 CPU 已足够支撑"少量无人机 + 低频相机 + MID-360"的 Fast 档；批量数据集生成用 Warp GPU，3–6 倍于 16 核 CPU。
2. LBVH 毫秒级建树意味着动态实体（人、车）可以每帧重建一个小 BVH，静态场景 BVH 一次建好常驻，两者分别求交后取最近（与 r4 的做法一致）。
3. **BVH 不能放进每个沙盒会话**：D2 准入模型里每会话匿名页预算 140–155 MB（[04 §4.4.2](../../04-D2-设计增补与决策记录.md)），一个 3M 面世界的 BVH 就要约 300 MB。应按"每个活跃世界一个几何/传感器服务进程"共享，会话通过 IPC 批量提交射线。

### 4.3 DSM / DTM / 覆盖栅格（与 M03/M04 对接）

`e05_pipeline.py` 第 5 步：每个 2 m 格内 4 × 4 = 16 条竖向射线，取最高命中为 DSM、最低命中参与 DTM；DTM 沿用 M03 算法（10 m 最小值 → 9 × 9 开运算 → 空格 4 邻域迭代填补）；`dsm_2m_n` 写每格命中数（0 表示无观测，DSM 用 DTM 填补，正好对应 M04 区分"观测到的低处"与"空格"的需求）。

| 项 | town | crop | hill |
|---|---|---|---|
| DSM 2 m 尺寸 | 2288 × 1393 | 1257 × 851 | 61 × 73 |
| 2 m 覆盖率（有命中的格） | 59.7% | 61.9% | 74.8% |
| DSM 2 m（16 采样/格）耗时：本机 / dmax | 32.0 / 8.7 s（纯射线 7.0 / 2.5 s） | 7.7 / 2.6 s | 0.03 / 0.02 s |
| DSM 0.5 m（4 采样/格）耗时：本机 / dmax | 284 / 34 s（本机峰值 RSS 23.5 GB） | 28.9 / 12.5 s | 0.15 / 0.04 s |
| DTM 10 m | 0.8 / 0.25 s | 0.19 / 0.07 s | < 0.01 s |

要点：

1. **M04 代码写死 DSM 2 m、DTM 10 m**（`geometry/query.py` 第 548–549 行 `expect_cell=2.0 / 10.0`），所以 GLB 世界必须照常输出 `dsm_2m`/`dtm_10m`/`dsm_2m_n`，D2 的 WorldQuery、Grid25、规划、围栏无需改动即可工作。
2. hill 只有 61 × 73 个 2 m 格，街巷尺度的视线必须走 `mesh.raycast`；另出 `terrain.dsm-fine`（0.5 m 或 0.25 m）作为新消费者的快速 2.5D 查询。
3. 本机 0.5 m 一次性生成 2 亿条射线占 23.5 GB，实现时按 1024 × 1024 格分块流式写盘。

### 4.4 碰撞网格（减面与误差）

`scripts/e06_decimate.py`：误差为"原网格表面 30 万采样点到简化网格的距离"（o2s）与反向（s2o），单位 m。

**hill（3.0M 面，边长 13 cm）**

| 方法 | 10%：耗时 | 10%：o2s p50 / p95 / p99 | 10%：s2o p95 | 2%：耗时 | 2%：o2s p50 / p95 |
|---|---|---|---|---|---|
| fast-simplification 0.2.0 | 16.6 s | 1.0 / 3.6 / 5.6 cm | 3.5 cm | 16.0 s | 2.4 / 11.2 cm |
| **meshoptimizer `simplify`** | **9.0 s** | 1.2 / 4.1 / 5.8 cm | 4.1 cm | 9.5 s | 2.9 / 10.4 cm |
| meshoptimizer `simplify_sloppy` | 0.88 s | 1.3 / 6.5 / 11.2 cm | 6.0 cm | 0.77 s | 3.1 / 19.0 cm |
| pyfqmr 0.5.0 | 21.7 s | 1.0 / 3.6 / 5.4 cm | 3.5 cm | 19.0 s | 2.4 / 11.2 cm |
| Open3D 二次误差 | 92.5 s | 0.8 / 3.1 / 4.7 cm | 2.9 cm | 106 s | 2.0 / 8.7 cm |
| pymeshlab 二次误差 | 150.5 s | 0.8 / 3.3 / 5.1 cm | 3.0 cm | 143.8 s | 2.2 / 10.0 cm |

**城镇级粗网格（meshoptimizer `simplify`）**：crop 2.88M → 288k 用 10.3 s，o2s p95 1.28 m；→ 57k（2%）p95 3.3 m。town 2.0M → 199k 用 6.9 s，p95 2.05 m；2% p95 4.5 m。各方法的 o2s 最大值 4–80 m，来自被整体删掉的漂浮碎块。

建议：

1. **FleetSim（Fast 档）直接用全分辨率 raycast 网格做碰撞与净空**：Embree 查询 3M 面毫无压力，不需要额外的碰撞代理。
2. **Gazebo（Realistic 档）用 meshopt 减面网格**：hill 类 10%（30 万面、p95 4 cm），城镇类 10%（20–30 万面，误差约等于原网格自身分辨率）。r5 实测物理步长几乎不随碰撞网格规模变化，瓶颈在加载时间。导出时直接写 world ENU 米制的 STL/OBJ（Z-up，无需 roll），视觉网格用 `appearance.glb` 加 `pose roll = π/2`。
3. 减面前先删掉 < 100 面的漂浮碎块，并记录到 quality（§4.9），否则 o2s 最大误差被碎块主导。

### 4.5 占据体素、ESDF 与 TSDF

`scripts/e07_voxel.py`：表面体素由面积比例采样（间距 voxel/3）得到；对每个竖向柱做多次射线穿透，统计表面层数；稠密 3 态栅格为 unknown（柱子无命中）/ free（第一层表面之上）/ occupied（表面）/ below-surface（表面之下，未知或实体）。

| 项 | hill，0.25 m | crop，1 m |
|---|---|---|
| 网格尺寸 | 489 × 582 × 185 = 5265 万 | 2515 × 1703 × 244 = 10.45 亿（稠密不可行） |
| 表面体素 | 52.2 万 | 634 万（稀疏 u64 键 50.7 MB） |
| 状态计数 | unknown 1439 万，free 1711 万，occupied 52 万，below 2062 万 | — |
| 存储 | 稠密 u8 52.7 MB，zlib 后 0.82 MB | 需稀疏分块 |
| 表面体素化 / 多层射线 | 15.2 s / 4.9 s | 117 s / 10.2 s |
| ESDF（scipy EDT，单线程） | 35.1 s（f16 105 MB） | GPU 最近点估算：A40 约 400–800 万点/s，2 m 稀疏块内可行 |
| 有多层表面的柱子 | 6.8%（屋檐、拱、树冠） | 9.4% |

结论：

1. 世界包里的占据体素是**真值资产**（评测、规划器、Frontier 真值），建议格式 `awr-occ@1`：16³ 砖块、u8 四态、砖块索引为排序的 Morton 键、每砖块 zstd；小世界允许整块稠密 + zlib。ESDF 存 f16、截断 20 m，只在 free 区计算。
2. "表面之下"必须与 unknown 同样处理为不可通行、不可视为空地；摄影测量里建筑内部、树冠下方都是没有观测的。
3. **TSDF 不放进世界包**：TSDF 是 Agent 用自己的 LiDAR/深度帧在线融合出来的私有地图（设计讨论 P1"每机私有认知地图"，r4 的建图实验）。世界包只提供用于评分的真值占据与 ESDF。

### 4.6 可行走 NavMesh（Recast/Detour）

Python 侧没有可用的 Recast 绑定：PyPI 上 `pynavmesh` 1.2.5 的烘焙器是纯 Python 移植（`pathfinder/navmesh_baker/rc_*.py`），百万面级不可用；`recastnavigation`、`pyrecast` 等名字在 PyPI 不存在。采用 **@recast-navigation/core + generators 0.43.1（Recast/Detour 的 WASM 构建）**：Node 里离线烘焙，导出的 Detour 二进制可以在 Node 和浏览器里直接加载查询（`scripts/navmesh_bake.mjs`、`nav_prune_query.mjs`、`path_demo.mjs`）。

坐标约定：Recast 为 Y-up，输入写成 `(x, y, z) = (E, U, −N)`，与 three 约定相同，导出路径再换回 ENU。

| 输入 | 参数（cs / ch，代理） | 烘焙 | 瓦片 / 多边形 | Detour 大小 | 可走面积 | 连通岛（最大岛） |
|---|---|---|---|---|---|---|
| hill，10% 碰撞网格（30 万面） | 0.15 / 0.10，行人 r 0.3 h 1.8 climb 0.4 slope 35° | 12.1 s | 46 / 4,160 | 1.49 MB | 6,982 m² | 191（3,419 m²，49%） |
| hill，全分辨率（300 万面） | 同上 | 69.0 s | 46 / 3,394 | 1.31 MB | 6,714 m² | — |
| crop，10%（28.8 万面） | 0.30 / 0.20，行人 | 237 s | 1,899 / 111,079 | 38.3 MB | 1.755 km² | 11,261（**1.127 km²，64%**） |
| crop，10% | 0.50 / 0.20，车辆 r 1.2 climb 0.25 slope 15° | 22.6 s | 676 / 18,007 | 4.6 MB | 0.588 km² | 2,221（0.084 km²，14%） |
| town，10%（19.9 万面） | 0.50 / 0.25，行人 | 225 s | 1,967 / 132,832 | 27.5 MB | 4.38 km² | — |

种子洪泛裁剪（`floodFillPruneNavMesh`，保留最大岛）后的查询：

| 世界 | 裁剪耗时 | 随机点对可达 | 平均查询（maxNodes 65535） | 路径长 / 直线 |
|---|---|---|---|---|
| crop 行人 | 0.49 s | 201/201（另 99 次随机取点落在已裁空的瓦片，属取点失败） | 4.6 ms | 1.56 |
| hill 行人 | 0.03 s | 112/112 | 0.23 ms | 1.92 |

观察与处理：

1. **未裁剪前随机路径只有 7–22% 可达**，因为屋顶、树冠、平台各自成岛。默认 `maxNodes = 2048` 时长路径会被截断成"部分路径"，需调到 65535。
2. **最大岛就是街道网**（`out/crop/nav_person_col10_overlay.jpg`）：crop 的 1.13 km² 主岛清晰覆盖镇上的街道与广场，151 个 OSM 路口中 92% 在其 3 m 以内（§4.7）。
3. **车辆不能靠 Recast**：坡度 15°、可跨 0.25 m 的约束在摄影测量的路面噪声、路缘和植被遮挡下碎成 2221 块。车辆移动改用配准后的 OSM 道路中心线图（`nav.graph`），高度从 DSM/网格吸附。
4. **"走在树顶上"**：渡口→博物馆的 Detour 路径（`out/crop/path_demo.jpg`）有一段直接穿过长满树的山坡，因为摄影测量的树冠是连续曲面、坡度小于 35°。尝试一：按颜色 + nDSM 做逐三角形粗语义（`e13_tri_semantic.py`：地面 16.7%、路面 3.7%、植被 62.4%、屋顶 10.8%、墙 6.4%，耗时 14 s），把树冠与屋顶三角形从 Recast 输入中删除（28.8 万 → 4.8 万面）后重新烘焙 145 s，最大岛只剩 0.233 km²，街道网被打碎，失败。尝试二（`e13b_cost_route.py`、`e13c_cost_route_osm.py`）：保留几何，按语义设区域代价（地面 3、路面 1.5、植被 8、墙 6、屋顶不可走），并把 OSM 道路中心线 5 m 内的三角形设为代价 1，在 103 万个导航三角形的邻接图上做 Dijkstra（每次 0.35–0.40 s）：

| 路线（渡口 → 博物馆） | 长度 | OSM 道路走廊 | 植被 | 屋顶 |
|---|---|---|---|---|
| 纯几何最短 | 1,984 m | 37% | 14% | 9% |
| 语义代价 + OSM 先验 | 2,622 m | **74%** | 2% | 1% |

   加权路线沿河岸公路走、再从镇西的道路上坡（`out/crop/path_cost_route_osm.jpg`），符合常识。Detour 原生支持区域代价（`dtQueryFilter.setAreaCost`），所以实现时应在 Recast 栅格化阶段把三角形语义写成 area id（用 recast-navigation 的低层 API `markWalkableTriangles`/`rasterizeTriangles` 传 areas 数组，generators 的高层函数不支持），而不是删三角形。
5. **楼梯与狭窄通道**：hill 的外环路与主岛不连通（台阶在摄影测量中是陡坡或竖墙）。需要在编辑器里人工加 off-mesh 连接（Recast 支持 `offMeshConnections`）。
6. **事件运行时不依赖 Detour**：事件路径在"事件编译"阶段离线算好写进 Canonical Event Tape（r6 的 `drone4d.tape.v1`），运行时只插值磁带；Python sim-core 不需要 Recast 绑定。浏览器编辑器可直接加载同一份 Detour 二进制做交互式路径预览。

### 4.7 锚点（anchors.json）

`scripts/e12_anchors.py`：把配准后的 OSM 路口（被 ≥ 2 条道路共享的节点）、建筑轮廓与 POI 变换到世界帧，裁到扫描覆盖多边形内，竖向射线取表面高度，并吸附到裁剪后的行人 NavMesh。

| 类别 | 数量 | 到 NavMesh 的吸附距离中位数 |
|---|---|---|
| 道路路口 | 151 | 0.84 m（92% 在 3 m 内） |
| 建筑（OSM 轮廓） | 70（含 civic 3、仓库 1、废墟 1） | 4.1 m（建筑质心到最近可走面） |
| POI | 30（渡口 2、博物馆、汽车站、邮局、银行 2、咖啡馆 2、加油站、超市、长椅、凉亭等） | 0.2–11.6 m；观景点 158 m（在覆盖边缘外的山顶，需人工处理） |

示例路径（`results/e12_path_demo.json`）：渡口 → 博物馆 1,570 m（直线 1,302 m，Detour 20 ms），博物馆 → 汽车站 369 m（0.8 ms）。这正是用户例子"A 经由建筑从 X 走到 Y 再到 Z"的数据基础。

锚点字段建议见 §5.3。注意 **OSM 是 ODbL**：由 OSM 派生的 anchors 与道路图属于派生数据库，再分发时须署名"© OpenStreetMap contributors"并以 ODbL 提供（世界包的几何与纹理仍是 CC-BY-4.0）。

### 4.8 浏览器展示 LOD（与 three.js 视口和 GPU 上限的兼容）

**压缩实测**（gltfpack 1.3；`results/e08_gltfpack_town.log`、`out/web/*.report.json`、`results/dmax/e08_dmax_gltfpack.txt`）

| 变体 | 大小 | 本机耗时（8 线程） | dmax（16 线程） | 几何（顶点 + 索引） | 图像 |
|---|---|---|---|---|---|
| hill 原始 | 143.8 MB | — | — | 约 114 MB | 29.6 MB JPEG |
| hill `-cc`（meshopt，保留 JPEG） | 47.8 MB | 7.8 s | — | 14.8 + 3.4 MB | 29.6 MB |
| hill `-cc -tc`（KTX2 ETC1S，8192²） | 32.1 MB | 157.5 s | 39.0 s | 同上 | 13.9 MB |
| hill `-cc -tc -tl 4096` | 21.9 MB | 68.9 s | — | 同上 | 3.7 MB |
| hill `-cc -tw -tl 4096`（WebP） | 22.3 MB | 20.9 s | — | 同上 | 4.1 MB |
| hill LOD1（`-si 0.25 -tl 2048`，75 万面） | 8.0 MB | 42.6 s | — | 6.1 + 0.9 MB | 1.0 MB |
| hill LOD2（`-si 0.05`，被默认 `-se 0.01` 限在 41.7 万面；要更少需 `-sa`） | 5.1 MB | 30.2 s | — | 4.3 + 0.5 MB | 0.3 MB |
| town `-cc` | 118.3 MB | 5.4 s | — | 8.4 + 2.1 MB | 107.8 MB |
| town `-cc -tc` | 68.8 MB | 422.6 s | 50.0 s | 同上 | 58.3 MB |
| town `-cc -tc -tl 2048` | 26.2 MB | 138.8 s | — | 同上 | 15.7 MB |
| crop `-cc -tc` | 74.2 MB | 429.9 s | 50.4 s | 11.3 + 3.1 MB | 59.8 MB |

meshopt 几何压缩约 6.1 B/三角形（hill 原始约 38 B/三角形）。three r186 的 GLTFLoader 同时支持 `EXT_meshopt_compression`、`KHR_meshopt_compression`、`KHR_texture_basisu`、`KHR_mesh_quantization`、`KHR_materials_unlit`，`node_modules/three/examples/jsm/libs/` 自带 meshopt 解码器与 basis 转码器，无需新增依赖。

**浏览器加载与渲染**（Playwright Chromium，SwiftShader，1280 × 720，`web/index.html`、`web/bench.mjs`；每帧后 `readPixels` 强制同步）

| 文件 | 解析 | 首帧（含纹理上传与着色器编译） | 稳态每帧 | 三角形 / draw |
|---|---|---|---|---|
| hill LOD2 | 0.52 s | 1.49 s | 746 ms | 41.7 万 / 1 |
| hill LOD1 | 0.76 s | 2.70 s | 1,347 ms | 75 万 / 1 |
| hill `-tc -tl 4096` | 1.65 s | 7.70 s | 4,090 ms | 300 万 / 1 |
| town `-tc -tl 2048` | 2.22 s | 12.6 s | 2,919 ms | 200 万 / 16 |
| crop `-tc` | 7.04 s | 32.3 s | 3,072 ms | 288 万 / 16 |

KTX2 在 SwiftShader 上转码到 BC7/DXT（`bptcSupported`）；`MAX_TEXTURE_SIZE = 8192`。SwiftShader 光栅约 0.56–0.73 M 三角形/s：Tier S 的 30 fps 预算里留给网格的 10 ms 只够约 6–7 千个三角形。

**分层建议**

| 渲染档 | 世界外观 | 预算 |
|---|---|---|
| Tier S（SwiftShader，本机验收） | **网格采样的彩色点云**：面积加权采样 5M 点并取纹理色（`e14_mesh2points.py`：crop 平均间距 1.02 m，耗时 15.7 s；hill 7.1 cm，13.4 s），写入现有 `potree2/anet-q16@1`，`col.rgb = srgb`（16 §4.4 已预留该取值，ingest 的 `RawCloud` 需增加 rgb 流）。M05/M06 不改即可显示 | 沿用 D2 点预算阶梯 |
| Tier B/A（硬件 GPU） | 3D Tiles 1.1 网格瓦片（glTF 内容，meshopt + KTX2，REPLACE 细化），可采用 r10 推荐的 3DTilesRendererJS（WebGL2 路径）；瓦片纹理 ≤ 2048–4096 | 独显 2–4M 三角形、集显 0.5–1M；纹理显存 ≤ 256–512 MB |
| 服务端（r3 相机渲染） | 原始 `appearance.glb`（或焊接后的世界帧米制副本），不下发浏览器 | — |

**瓦片纹理：图集重排而非重新展开**（`e11b_uv_charts.py`）

| 世界（瓦片边长） | 瓦片数 | 每瓦片三角形 | 每瓦片 UV 碎片 | 每瓦片引用纹理数 | 碎片包围盒纹素（等效方图） | 实际用到的纹素 | 全部瓦片包围盒之和 / 原图集 |
|---|---|---|---|---|---|---|---|
| hill（32 m） | 19 | 18.0 万 | 6,629 | 1 | 6.66 M（2581²） | 2.30 M | 1.67 |
| crop（256 m） | 53 | 6.1 万 | 18,332 | **16** | 6.79 M（2606²） | 3.10 M | 1.15 |
| town（512 m） | 41 | 5.3 万 | 15,337 | **16** | 7.93 M（2816²） | 3.64 M | 1.14 |

城镇级网格每个瓦片都会引用全部 16 张 4K 图集，不重排就无法按瓦片流式加载。按瓦片拷贝用到的 UV 碎片矩形重新装箱（Obj2Tiles 的做法），每瓦片约需一张 4096² 纹理，总纹素只增加 14–67%。用 xatlas 重新展开再烘焙的方案在 hill 的 18 万面瓦片上跑了 24 CPU 分钟仍未完成，已放弃。简化后的 LOD 层用最近点回查原纹理（Open3D `compute_closest_points` 给出原三角形与重心坐标）烘焙到新图集。

### 4.9 "几何不完整"标注

`scripts/e09_incomplete.py` 输出位掩码栅格（bit0 覆盖外或无数据、bit1 开放边界洞、bit2 拉伸三角形、bit3 低纹素密度）与 GeoJSON（扫描覆盖多边形 + 不完整区域多边形），并在正射图上叠加（`out/hill/incomplete_overlay.jpg`）。

| 项 | hill（0.5 m 格） | crop（2 m 格） |
|---|---|---|
| 扫描覆盖面积 / 包围盒 | 13,017 / 17,640 m²（73.8%） | 2.63 / 4.27 km²（61.4%） |
| 开放边界环 | 2（外边界 591 m；洞 1 个，周长 78 m，12.7 × 9.4 m，在山顶边缘） | 148（洞 147 个，周长 > 5 m 的 68 个，最大 40 m） |
| 覆盖内无数据格 | 0.1% | 3.0% |
| 拉伸三角形（边长 > max(p99, 6 × 中位数) 或细长） | 1,277 面，2.1% 面积（阈值 1.0 m） | 3,258 面，1.7% 面积（阈值 14.2 m） |
| 低纹素密度（< 中位数 1/8） | 0.02% 面积 | 1.0% 面积 |
| 多层表面柱（§4.5） | 6.8% | 9.4% |
| 不完整多边形 | 17 | 1,296 |
| 耗时 | 约 17 s | 约 16 s |

规则建议：

1. **border 用扫描覆盖多边形内缩**（小世界内缩 5 m、城镇级 20 m），而不是包围盒内缩（16 §7 现规则）。覆盖多边形 crop 有 735 个顶点，简化后满足 zones 每环 ≤ 1024 顶点的约束。
2. 无数据、洞、表面之下统一视为 **unknown**：规划与碰撞按障碍处理；事件实体不得穿越（除非编辑器人工确认）。
3. 传感器：射线穿出覆盖区或进入洞时，LiDAR 点标记"void"（tag 位），相机像素输出无效掩码，评测时这些像素和点不计入"可观测"。
4. 拉伸三角形与低纹素区在 UI 上可视化（"此处为插值面"），并影响事件设计：关键事件点不宜落在这些区域。
5. 水面（河面）在摄影测量里是平整网格，会被 NavMesh 当作可走面，需要语义（OSM 水体或颜色）标出 bit5。

---

## 5. world.json v2 扩展（D3 GLB profile）

### 5.1 版本策略

- `world.schema.json` 的 `layers[].type` 是闭合枚举、`additionalProperties = false`；`worldpkg --missing` 把主版本 ≠ 1 的包判为 `schema_major` 需要重建；规范 §16.1 规定读者接受任意 `1.*`。
- 因此 D3 扩展以 **`schemaVersion = "1.1.0"` + `profile = "d3-glb"`** 发布，只做加法：类型枚举增加 `navmesh`、`navgraph`、`anchors`、`quality`；格式注册表增加 6 个名字；layer 对象增加可选字段 `axes`、`stats`、`note`；顶层增加可选 `profile`、`sensorHints`。
- **D1 必需图层全部保留**（点云、源点云、DTM、DSM、类别表、zones、env），所以 D2 的 sim-core、WorldQuery、M05 点云引擎、沙盒准入不改代码就能加载 GLB 世界；新能力由新模块（M22–M25）按图层按需读取。
- `T_world_layer` 仍只允许刚体：米制尺度、调平、真北都在导入时烘焙进几何（所有派生资产都在 world 帧、米），原始 GLB 的相似变换只出现在 `coordinate.source.T_world_source`。

### 5.2 目录结构（新增部分）

```text
worlds/<id>/
├── world.json  coordinate.json                  # 1.1.0，profile = d3-glb
├── source/original.glb                          # 原始文件（可选，按体积决定是否随包分发；sha256 记录在 dataset.sourceFiles）
├── visual/
│   ├── pointcloud/                              # 网格采样的彩色点云，potree2/anet-q16@1，col.rgb = srgb（Tier S 与 D2 视口）
│   └── mesh/
│       ├── appearance.glb                       # 世界帧米制、glTF Y-up（x=E, y=U, z=-N），unlit；服务端渲染用
│       ├── tileset.json  tiles/*.glb            # 3D Tiles 1.1，REPLACE，meshopt + KTX2，图集按瓦片重排
│       └── gaussian/                            # 预留：3DGS 背景（r3），spz@2 / KHR_gaussian_splatting
├── geometry/
│   ├── terrain/dtm_10m  dsm_2m  dsm_2m_n        # 与 D1 相同格式与格距（M04 兼容）
│   ├── terrain/dsm_0p5m  quality_2m             # 新：精细 DSM、不完整位掩码（f32-grid@1 / u8）
│   ├── pointcloud/source/                       # awr-pts@1（由网格采样，供 D2 的 M04 例外读取与 ingest 统计）
│   ├── mesh/raycast.json  raycast.{xyz.f32,tri.u32,cls.u8,q.u8,src.u32}   # awr-trimesh@1
│   ├── collision/collision.json  *.f32/*.u32  collision.stl              # awr-trimesh@1 + Gazebo 导出
│   ├── voxel/occ_<cell>.json  *.bricks                                    # awr-occ@1
│   ├── sdf/esdf_<cell>.json  *.bricks
│   └── nav/person.detour.bin  vehicle.detour.bin(可选)  graph.geojson     # detour-navmesh@1、geojson/awr-navgraph@1
├── semantic/
│   ├── anet-classes@1.json  zones.geojson       # zones 的 border 改为覆盖多边形内缩
│   ├── anchors.json                             # awr-anchors@1
│   └── quality.geojson                          # geojson/awr-quality@1
└── qa/report.json                               # 增加：配准残差、调平证据、拓扑体检、NavMesh 连通性
```

### 5.3 新格式注册名

| 注册名 | 内容 | 关键字段 |
|---|---|---|
| `gltf-glb@2` | glTF 2.0 二进制 | `axes = "gltf-y-up-enu"`（x=E、y=U、z=−N，米） |
| `awr-trimesh@1` | 三角网格 SoA：sidecar JSON + `xyz.f32`（world ENU，float32）、`tri.u32`、可选 `cls.u8`（逐三角形语义：地面、路面、植被、屋顶、墙、水）、`q.u8`（质量位）、`src.u32`（原三角形编号） | `count_v`、`count_t`、`bounds`、`coordinate_sha256`、`stats`（边长分位数、误差 p95） |
| `awr-occ@1` | 稀疏 16³ 砖块：u8 四态（0 unknown、1 free、2 occupied、3 below-surface）或 f16 ESDF；砖块索引为排序 Morton 键 + 偏移，逐砖块 zstd；小世界允许 `layout = dense` | `voxelM`、`origin`、`dims`、`states`、`truncM` |
| `detour-navmesh@1` | recast-navigation `exportNavMesh` 字节（Detour tiled navmesh），Node 与浏览器共用 | `agent`（半径、高度、可跨、坡度）、`cs`、`ch`、`axes = "recast-y-up-enu"`、`prunedFromSeeds`、`walkableAreaM2` |
| `geojson/awr-navgraph@1` | world ENU 的 LineString 道路图 | properties：`kind`（road、footway、path、steps）、`agents`、`widthM`、`speedMps`、`osm_id`、`z`（吸附后的高程序列） |
| `awr-anchors@1` | 锚点表 | 见下 |
| `geojson/awr-quality@1` | 覆盖多边形与不完整区域 | properties：`kind`（scan-footprint、nodata、hole、stretched、low-texture、multilayer、water）、`reasons_bits` |

`anchors.json` 单项：

```json
{ "id": "poi-0213", "kind": "poi:museum", "name": "…", "label_zh": "博物馆",
  "xy": [-131.42, 209.93], "z": -179.43,
  "navSnap": { "person": [-131.42, 209.93, -179.43], "vehicle": null },
  "footprintXY": null, "entrances": [],
  "source": "osm:node/…", "confidence": "osm", "editable": true }
```

顶层带 `schema`、`world_id`、`frame = world`、`units = m`、`coordinate_sha256`、`source_attribution`（OSM 时为 ODbL 署名）。Event DSL（设计讨论 §5 的 `anchor: building_X_exit`）直接引用 `id`。

### 5.4 示例（crop，节选；完整生成物见 `results/world_v2_example_crop.json`、`results/coordinate_v2_example_crop.json`）

```json
{
  "schemaVersion": "1.1.0", "profile": "d3-glb", "id": "oryahovo",
  "name": "Oryahovo centre (Sketchfab photogrammetry)", "nameZh": "奥里亚霍沃镇中心（无人机摄影测量）",
  "dataset": { "name": "Sketchfab: oryahovo - crop #1", "license": "CC-BY-4.0", "redistribution": true,
               "citation": "Mitko (https://sketchfab.com/bg3d), CC-BY-4.0",
               "derivedFrom": [ { "name": "OpenStreetMap", "license": "ODbL-1.0", "use": "registration + anchors seed" },
                                { "name": "SRTM 30 m", "use": "levelling + hMsl" } ] },
  "scaleStatus": "landmark",
  "bounds": { "min": [-1256.03, -850.44, -103.29], "max": [1256.03, 850.44, 138.41] },
  "layers": [
    { "id": "pointcloud.visual", "type": "pointcloud", "role": "visual", "format": "potree2/anet-q16@1", "href": "visual/pointcloud/", "status": "ready", "default": true },
    { "id": "terrain.dsm", "type": "terrain", "role": "geometry", "format": "f32-grid@1", "href": "geometry/terrain/dsm_2m.json", "status": "ready" },
    { "id": "mesh.appearance", "type": "mesh", "role": "visual", "format": "gltf-glb@2", "href": "visual/mesh/appearance.glb", "status": "ready", "axes": "gltf-y-up-enu" },
    { "id": "mesh.tiles", "type": "tiles3d", "role": "visual", "format": "3dtiles@1.1", "href": "visual/mesh/tileset.json", "status": "ready" },
    { "id": "mesh.raycast", "type": "mesh", "role": "geometry", "format": "awr-trimesh@1", "href": "geometry/mesh/raycast.json", "status": "ready",
      "stats": { "triangles": 2879110, "edgeP50M": 1.88 } },
    { "id": "mesh.collision", "type": "collision", "role": "geometry", "format": "awr-trimesh@1", "href": "geometry/collision/collision.json", "status": "ready",
      "stats": { "triangles": 287849, "errP95M": 1.28 } },
    { "id": "occupancy.voxels", "type": "voxel", "role": "geometry", "format": "awr-occ@1", "href": "geometry/voxel/occ_1m.json", "status": "ready" },
    { "id": "nav.person", "type": "navmesh", "role": "geometry", "format": "detour-navmesh@1", "href": "geometry/nav/person.detour.bin", "status": "ready",
      "stats": { "walkableAreaM2": 1116461, "bytes": 38336396 } },
    { "id": "nav.graph", "type": "navgraph", "role": "geometry", "format": "geojson/awr-navgraph@1", "href": "geometry/nav/graph.geojson", "status": "ready" },
    { "id": "semantic.anchors", "type": "anchors", "role": "semantic", "format": "awr-anchors@1", "href": "semantic/anchors.json", "status": "ready", "default": true },
    { "id": "semantic.quality", "type": "quality", "role": "semantic", "format": "geojson/awr-quality@1", "href": "semantic/quality.geojson", "status": "ready" }
  ],
  "sensorHints": { "texelM": 0.19, "meshEdgeP50M": 1.88, "textureLimitedRangeM": { "4k_hfov84": 500, "720p_hfov84": 165 }, "multiLayerColumnFrac": 0.094, "captureSeason": "summer" }
}
```

`coordinate.json` 的变化：`source.upAxis = "+y"`、`source.unitsToMeters = 20.93497`、`source.yawDeg = 168.04`、`source.leveledDeg = 1.25`、`source.T_world_source` 为含尺度的 4×4（`results/e17_coordinate_examples.json`）、`trueNorth = {0, "verified", [OSM 证据]}`、`registration.method = "osm-road-correlation"`、`anchor.kind = "map"`（新增，§2.5）。

### 5.5 内容哈希与缓存

沿用 16 §3.4：所有新文件进入 `files[]` 与 `contentVersion`；`source/original.glb` 只在随包分发时列入。派生缓存（M04 的 dsm_eff、Grid25 等）以 `contentVersion` 与 `coordinate.sha256` 绑定，不需要改。NavMesh、anchors 依赖 OSM 快照，应把 OSM 抽取文件的 sha256 写进 `dataset.derivedFrom[].sha256`，OSM 更新时触发重建。

---

## 6. CLI 设计（`worldpkg glb …`）

```text
worldpkg glb inspect  <file.glb> [--json]
    # 节点矩阵、轴向判定（朝向统计）、镜像检查、三角形/顶点/图元、纹理尺寸与总纹素、材质扩展、
    # 估算内存与 GPU 纹理显存、Sketchfab 署名与许可（asset.extras）

worldpkg glb register <id> --glb <file> [--osm <file.osm|bbox>] [--dem srtm30m|eudem25m|none]
                     [--method roads|footprints|manual] [--control-points cp.json]
                     [--scale-range 0.5:20] [--frame-of <other-id>]
    # 输出 registration.json：unitsToMeters、yaw、调平（DEM 回归 / 墙面竖直度）、锚点经纬度、残差与证据、叠加图
    # --frame-of：与已配准世界共用局部坐标（crop 复用 town）

worldpkg glb build <id> --glb <file> --registration registration.json
                  [--profile fast|full] [--steps <list>] [--gpu] [--jobs N]
                  [--nav person,vehicle] [--nav-seeds seeds.json] [--osm <file>] [--voxel 0.25|1|2]
    # 阶段（可单独重跑，产物写 worlds/.staging/<id>-<nonce>/，沿用 M03 原子发布）：
    #  1 normalize   展平节点 → T_world_source → 世界帧米制（float64 计算，float32 落盘）
    #  2 raycast     焊接、去退化、拓扑体检 → awr-trimesh@1
    #  3 grids       dsm_2m(16 采样/格)、dtm_10m、dsm_2m_n、dsm_0p5m（分块流式）
    #  4 quality     覆盖多边形、洞、拉伸面、低纹素、多层柱 → quality 栅格与 GeoJSON；zones.border = 覆盖多边形内缩
    #  5 semantic    逐三角形粗语义（颜色 + 法线 + nDSM + OSM 水体/道路）→ cls.u8
    #  6 collision   meshopt 减面（误差预算）→ awr-trimesh@1 + collision.stl
    #  7 occupancy   稀疏砖块四态体素 + ESDF（--gpu 时用 Warp 最近点）
    #  8 navmesh     node navmesh_bake.mjs（语义 area 代价）→ 种子洪泛裁剪 → detour bin；navgraph 来自 OSM
    #  9 anchors     OSM 播种 + NavMesh 吸附 → anchors.json（草稿，editable）
    # 10 appearance  appearance.glb（世界帧米制 Y-up）；gltfpack -cc -tc；3D Tiles（瓦片 + 图集重排 + LOD）
    # 11 pointcloud  网格采样彩色点云 → 现有 tile 阶段（ANET_Q16，col.rgb = srgb）+ awr-pts@1
    # 12 package     files[]、contentVersion、world.json 1.1.0、qa/report.json
    # 13 validate    结构 + 语义 + deep（新增 V-GLB 规则组，见下）→ 原子发布

worldpkg glb export-gazebo <id> [--collision-ratio 0.1] [--out dir]   # SDF + collision.stl（ENU 米）+ visual(appearance.glb, roll=π/2)
worldpkg anchors {list|add|rm|import|export} <id> …                    # 编辑器之外的命令行维护
```

新增校验规则（V-GLB 组）：`unitsToMeters` 与 `T_world_source` 行列式一致；`scaleStatus ∈ {landmark, assumed}` 时必须有证据；`mesh.raycast` 顶点全部落在 `bounds` 内；`dsm_2m` 与 raycast 网格竖向射线复算的随机抽样差 < 1 cm；NavMesh 种子所在岛面积 > 0；anchors 的 `navSnap` 到 NavMesh 距离 < 3 m（否则 qa 告警）；quality 覆盖多边形与 `dsm_2m_n > 0` 的格一致；ODbL 派生层必须有署名字段。

与 job-worker 的关系：`worldpkg glb build` 作为 `world_build` 作业的新 adapter（`GlbAdapter`，与 `ArraysAdapter` 同级），阶段进度沿用 `StageContext.progress`；公开部署（`allow_derive = false`）仍只消费预构建的世界包。

---

## 7. 实测耗时汇总（本机 8 核 / dmax 16 核 + A40）

| 阶段 | town（2.0M 面，4.6 × 2.8 km） | crop（2.9M 面，2.5 × 1.7 km） | hill（3.0M 面，122 × 145 m） |
|---|---|---|---|
| inspect（JSON 块） | 0.02 s | 0.02 s | 0.01 s |
| register（OSM FFT 搜索 + 精配，本机） | 约 143 s（精配约 30 s 为估计） | 继承 town | 223 s |
| 调平（DEM 400 点 / 墙面法） | 网络查询约 10 s / 6 s | 继承 | — / 5 s |
| 1–2 normalize + raycast（解析、焊接、拓扑） | 35.6 / 8.5 s | 29.5 / 12.1 s | 21.6 / 9.2 s |
| BVH 建树（Open3D） | 2.6 / 1.1 s（GPU LBVH 17 ms） | 3.5 / 1.3 s（38 ms） | 3.3 / 1.4 s（25 ms） |
| 3 grids：2 m + DTM | 32.8 / 8.9 s | 7.9 / 2.7 s | < 0.1 s |
| 3 grids：0.5 m 精细 DSM | 284 / 34 s | 28.9 / 12.5 s | 0.15 / 0.04 s |
| 4 quality | — | 16 s | 17 s |
| 5 semantic | — | 14 s（+4 s 转移到碰撞网格） | — |
| 6 collision（meshopt 10%） | 6.9 s | 10.3 s | 9.0 s |
| 7 occupancy | — | 1 m：表面 117 s + 多层 10 s | 0.25 m：表面 15 s + 多层 5 s + ESDF 35 s |
| 8 navmesh 行人（单线程 WASM） | 225 s | 237 s + 裁剪 0.5 s | 12 s（10% 网格）/ 69 s（全分辨率） |
| 9 anchors | — | 约 5 s | — |
| 10 appearance（gltfpack `-cc -tc`） | 423 / 50 s | 430 / 50 s | 158 / 39 s |
| 11 pointcloud（采样 5M 点） | — | 35 s（含加载） | 20 s |
| **合计（不含配准）** | 本机约 17 分钟 / dmax 约 6 分钟 | 本机约 16 分钟 / dmax 约 6 分钟 | 本机约 5 分钟 / dmax 约 3 分钟 |

注：NavMesh、occupancy、quality、semantic、anchors、点云采样没有在 dmax 上单独计时，dmax 合计按本机单线程耗时计入（dmax 单核主频更高，实际会更短）。

dmax 的瓶颈是 NavMesh（单线程 WASM，约 4 分钟）。Recast 是按瓦片独立烘焙的，可用 Node worker_threads 按瓦片并行（预计 16 线程下降到 20–30 s），这是实现时最值得做的一项优化。

---

## 8. 对 D3 架构的建议

1. **M22 World Asset Import = 本文的 `worldpkg glb`**，产物是 world.json 1.1.0 / d3-glb profile。复用 M03 的 staging、原子发布、contentVersion、validate；复用 M04 的 DSM/DTM 消费链；新增 `GlbAdapter` 与 V-GLB 校验。
2. **Geometry World 双后端**：M04 的 `WorldQuery` 增加 `MeshWorldQuery`（Open3D/Embree），实现 `ray_hit`、`segment_los`、`los_batch`、`clearance` 的精确版本；DSM 版保留为粗筛（DSM 判定无遮挡的射线直接返回，减少网格查询）和 D2 兼容路径。M19 的视线、M13/M25 的 LiDAR 与深度相机都走网格。
3. **每个活跃世界一个共享几何/传感器服务**（进程内持有 BVH，约 300 MB），sim-core 会话通过总线批量提交射线；批量离线数据集生成放 GPU Worker（Warp，45–119 M 射线/s）。这与设计讨论"高保真运行需要单独的 GPU Worker"一致，也避免冲击 D2 公开沙盒的内存准入。
4. **Event Runtime（M23）在编译阶段用 NavMesh/道路图把锚点序列变成轨迹**，写入 Canonical Event Tape（r6）；运行时只插值，确定性与跨后端（FleetSim、Gazebo）一致性由磁带保证。事件编辑器（浏览器）加载同一份 Detour 二进制做交互预览。
5. **anchors 是 Event DSL 与世界之间的唯一接口**：DSL 只写 `anchor: <id>`，世界包更新（重新配准、重新烘焙）时 anchors 的 id 保持稳定，坐标随之更新。
6. **"不完整即未知"贯穿全链路**：occupancy unknown、zones.border 用覆盖多边形、LiDAR void 标记、相机无效掩码、评测只在可观测区计分。
7. **浏览器分层**：Tier S 点云（复用 M05，零改动）；Tier B/A 网格瓦片（新图层，M06 注册，深度测试与点云共存）；锚点、质量、导航网格作为编辑叠加层（M15）。原始外观网格只在服务端。
8. **Gazebo（M24）**：从 `mesh.collision` 导出 world ENU 米制 STL/OBJ（不需要 roll），视觉用 `appearance.glb`（roll = π/2）；事件实体按 r5 的结论走磁带而非 actor 物理。
9. **默认沙盘的分工**：crop 作为城镇级事件主场景（2.5 km² 覆盖、街道网完整、OSM 锚点丰富）；hill 作为街巷级近距离场景（人物可辨、楼梯与台阶需人工连接）；town 作为大范围 LiDAR 探索与远景背景。

---

## 9. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 兄弟单元按错误尺度（r3 的 ×4、或直接用 glTF 单位）得出的米制数字 | 航高、LiDAR 量程、点密度、GSD、碰撞误差、风速相关结论偏差约 2 倍 | 以本文 `unitsToMeters` 为准统一重算；M22 输出前禁止下游自行假设尺度 |
| 无 OSM/DEM 覆盖的新场景 | 无法自动标定尺度与真北 | 编辑器控制点标定；`scaleStatus = assumed` 时 UI 明示并降低评测可信度 |
| Overpass 等公共服务不稳定（本次实测超时与 500） | 构建失败或不可复现 | 使用 Geofabrik 离线抽取并记录 sha256；API 只作开发便利 |
| 摄影测量语义缺失：树冠、屋顶、水面被当作可走面；楼梯不连通 | 事件实体"走在树上"或无法到达 | 语义区域代价 + OSM 先验 + 洪泛裁剪 + 人工 off-mesh 连接；事件编译时校验路径类别占比 |
| 粗网格（1.9–3.3 m 边长）与 19–28 cm 纹素 | 常规航高下图像背景比真实航拍模糊，建筑呈"融化"状 | `sensorHints.texelM` 与纹理受限距离；近距离任务用 hill 类精细场景或 3DGS（r3） |
| 烘焙光照与固定阴影 | 时间/天气变化下静态场景光照不一致 | 文档化为已知限制；相机渲染只对动态实体与天空做光照变化 |
| 许可：CC-BY 署名；OSM 派生数据为 ODbL | 再分发合规 | NOTICE 与 UI 署名；anchors/navgraph 单独标注 ODbL 并随包附许可 |
| 内存：每世界 BVH 约 300 MB | 公开沙盒无法每会话加载 | 共享几何服务；GLB 世界先只在高保真实验后端开放 |
| 存储：0.5 m DSM 一次性 2 亿射线占 23.5 GB；城镇级稠密体素 10 亿格 | 构建 OOM | 分块流式、稀疏砖块 |
| NavMesh 烘焙单线程 4 分钟 | 构建慢 | 按瓦片并行（worker_threads） |
| CPU/GPU 浮点差异 | 跨硬件结果不逐位一致 | 实测命中 100% 一致、深度差 < 1 cm 占 99.9999%；评测按容差比较（与设计讨论第九节一致） |
| schema 是闭合枚举 | 新图层被旧校验器拒绝 | 1.1.0 加法式扩展经 M00 合入，D2 运行时只读必需图层 |
| hill 的调平只有墙面法一个证据 | 1° 量级倾斜残留 | 编辑器提供"指定水平面"人工校正 |

---

## 10. 可复用实现清单

| 文件（`.cache/research/d3/r2-glb-world/scripts/`） | 内容 | 进入项目时的去向 |
|---|---|---|
| `glbworld.py` | 零依赖 GLB 解析（numpy 直接读二进制块，展平节点矩阵/TRS，法线变换，镜像检测，纹理解码） | `awr/world/glb/reader.py` |
| `worlds_cfg.py` | `linear_world_from_gltf(scale, yaw, gE, gN)`、调平旋转、跨文件共帧变换 | `awr/world/glb/normalize.py` |
| `e03_roadmask.py`、`e03_register.py`、`e03b_translate.py`、`e03d_hill_register.py`、`osm_parse.py` | 道路/建筑掩码、FFT 相似变换搜索、倒角精配、OSM XML 解析 | `awr/world/glb/register.py` |
| `e03c_vertical.py`、`e03f_wall_tilt.py`、`e16_anchor.py` | DEM 竖向/倾斜回归、墙面竖直度调平、锚点经纬度与 hMsl | 同上 |
| `rcast.py`、`world_ortho.py` | Open3D 纹理正射图、透视射线渲染（逐像素回查纹理） | 编辑器底图、QA 叠加 |
| `e05_pipeline.py` | 焊接、拓扑体检、BVH 基准、DSM/DTM/覆盖栅格 | `awr/world/glb/derive.py` |
| `e06_decimate.py` | 六种减面方法与双向误差评估 | `collision` 阶段（只保留 meshopt） |
| `e07_voxel.py` | 表面体素化、多层射线、四态体素、EDT | `occupancy` 阶段（改为稀疏砖块） |
| `e09_incomplete.py`、`e09b_facade.py` | 覆盖多边形、洞、拉伸面、纹素密度、质量位掩码与 GeoJSON | `quality` 阶段 |
| `navmesh_bake.mjs`、`nav_prune_query.mjs`、`path_demo.mjs`、`nav_seed.py`、`nav_overlay.py` | Recast 烘焙、洪泛裁剪、路径查询、岛统计与叠加图 | `navmesh` 阶段 + 浏览器编辑器预览 |
| `e12_anchors.py`、`e13_tri_semantic.py`、`e13c_cost_route_osm.py` | OSM 锚点播种与吸附、逐三角形粗语义、语义代价 + OSM 先验寻路 | `anchors`、`semantic` 阶段；事件编译 |
| `e10_gpu_raycast.py` | Warp CUDA 网格射线与最近点核函数 | GPU Worker 的传感器服务 |
| `e11b_uv_charts.py` | UV 碎片统计（图集重排可行性） | `appearance` 阶段的瓦片图集重排 |
| `e14_mesh2points.py` | 网格 → 彩色点云 | `pointcloud` 阶段 |
| `web/index.html`、`web/bench.mjs` | three r186 + meshopt + KTX2 的浏览器加载/渲染基准 | M06 性能回归 |

---

## 11. 复现命令

```bash
cd /data/projs/anet-drone/.cache/research/d3/r2-glb-world
./setup.sh                      # 首次：venv、本地 libEGL、Node 工具、gltfpack
source scripts/env.sh
$PY scripts/e01_load.py                                   # 轴向、规模、内存
$PY scripts/e02_ortho.py GLB_TOWN 0.25 town               # 正射图与 DSM（模型单位）
$PY scripts/e03_roadmask.py town 0.25 && $PY scripts/e03_register.py && $PY scripts/e03b_translate.py
$PY scripts/e03c_vertical.py && $PY scripts/e03f_wall_tilt.py
$PY scripts/e05_pipeline.py town GLB_TOWN results/params_town.json      # crop、hill 同理
$PY scripts/e06_decimate.py hill 0.1,0.02
$PY scripts/e07_voxel.py hill 0.25
$PY scripts/e09_incomplete.py crop GLB_CROP 2.0
(cd node && node navmesh_bake.mjs ../out/crop/col10_xyz.f32 ../out/crop/col10_tri.u32 ../out/crop/nav_person_col10 \
   '{"cs":0.3,"ch":0.2,"tileSize":128,"detailSampleDist":3,"detailSampleMaxError":0.4}')
$PY scripts/e12_anchors.py crop && $PY scripts/e13_tri_semantic.py crop GLB_CROP && $PY scripts/e13c_cost_route_osm.py
tools/gltfpack -i $GLB_HILL -o out/web/hill_cc_tc4k.glb -cc -tc -tl 4096 -tj 8
(cd web && node bench.mjs hill_cc_tc4k.glb)
# dmax（GPU 2、3，16 核）：
#   docker run -d --name d3-r2-glb-world --platform linux/amd64 --dns 223.5.5.5 --cpus 16 --memory 96g \
#     --gpus '"device=2,3"' -v /data/anet-drone4d-d3/r2-glb-world:/work -v /data/anet-drone4d-d3/data:/glb:ro d3-r2-glb-world:0.1 sleep infinity
#   docker exec -w /work d3-r2-glb-world /opt/venv/bin/python scripts/e10_gpu_raycast.py crop cuda:0
```

dmax 环境注意：本机 Docker 守护进程的默认 DNS 配置无效（容器内 `nameserver invalid IP`），需要显式 `--dns`；本地缓存的 `python:3.12-slim` 与 `debian:12-slim` 是 arm64 镜像，要用 amd64 的 `ubuntu:24.04` 或显式 `--platform`。
