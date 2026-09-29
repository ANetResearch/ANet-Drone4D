# X01 研究笔记：UrbanScene3D 数据集（内置演示世界 + 无人机航拍轨迹）

> 研究单元：x01 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §7–8（World Model）、§14–16（点云与场景结构）、§26–30（无人机与控制模式）、§38–41（UI、World Package）、§43–44（MVP）
>
> 仓库快照：`refs/data/UrbanScene3D` @ `a5ce19b`（2023-12-12，★156，C++17，ECCV 2022 数据集官方仓库，仅含评测代码）
>
> 本地数据：
> - `data/raw/urbanscene3d/*_sampled_5m.ply`：6 个虚拟城市采样点云，均已完整解压，逐个校验了 `header + N×24 B == 文件大小`
> - `polytech1k.ply` / `artsci1k.ply`：真实场景稀疏配准点
> - `data/raw/urbanscene3d/paths/`：本次用 gdown 下载的 **99 个航线文件**（96 个 txt + 3 个 Oblique.log），共 43,654 个视点。Town/Oblique.log 因 Google Drive 配额限制未下载成功
>
> 实测脚本与中间结果：`/data/projs/anet-drone/.cache/research/x01/`
>
> | 脚本 | 用途 |
> |---|---|
> | `plyio.py` | memmap 读 PLY、写 PNG |
> | `stats.py` | 原始统计 |
> | `topdown.py`、`sfzoom.py`、`sfscale.py` | 俯视高度图、地标量测 |
> | `tilt.py`、`tilt2.py` | 地面倾斜检测 |
> | `analyze.py` | 规范化 → DTM/HAG → 密度 → 体素 → 建筑 → 地标，输出 `analysis.json` |
> | `paths.py`、`paths2.py` | 航线统计与 TSP |
> | `missions.py` | 任务生成器与安全检查 |
> | `retarget.py` | 航线重定向实验 |
>
> 八叉树统计复用 r09 的 `r09_octree_fast.py::build_fast`，结果在 `octree_g64.json`。规范化后的 ENU 数组 `enu_/nrm_/hag_/hmap_<city>.npy` 可直接供后续实现者复用。
>
> 本文路径均相对仓库根或上述目录。结论来自源码精读与本机实测；凡属推断均标注"推断"。

---

## 0. 结论速览

| 资产 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **6 个虚拟城市采样点云**<br>`*_sampled_5m.ply`，各约 5M 点，xyz + normal | 系统内置演示世界，同时是性能与回归基准 | **adopt**：经过 ingest 规范化（单位 / 轴向 / 调平 / 北向 / 原点 / 法线 / DTM / 分类）后转成 World Package | V0.1 起 | ★★★★★ |
| **航线文件**<br>`Path/<Scene>/<Method>/<overlap>/<Scene>_<proxy>.txt` + `Oblique.log` | 真实论文算法产出的航拍视点集（4 个合成场景 × 3 种方法 × 2 种重叠度 × 4 种代理） | **port**：用作航线模板库，提供视点分布、云台角、重叠度参数；轨迹回放测试集<br>**reference**：参考其 5 向倾斜网格参数 | V0.2（回放 / Timeline）<br>V0.6（多机任务模板） | ★★★★ |
| `src/model_tools.h::Height_map` | 2.5D 最大高度栅格 + 膨胀 + 安全距离，UrbanScene3D 团队用它判定无人机位置是否安全 | **port**（numpy 约 40 行）：mock 碰撞、最小安全高度、安全转场 | V0.2 | ★★★★ |
| `src/evaluate_model.cpp::EvaluateModel::evaluate` | 重建精度评测（Accuracy / Completeness / Chamfer / Hausdorff / 阈值占比） | **port**（Python + KDTree，并修复 4 处 bug）：World Fusion QA | V0.5 | ★★★ |
| `src/model_tools.cpp::split_obj`、`sample_points` | 鸟瞰栅格 + 8 邻域连通 → 单体化；面积加权三角形采样 | **reference**：用于建筑单体化和 mesh 转点云 | V0.5 / V1.0 | ★★ |
| `src/intersection_tools.cpp`（Embree / CGAL 射线） | 深度图、可见性判定 | **reference**：服务器侧传感器仿真已有 r04/r05 的 numpy 方案 | — | ★ |
| `polytech1k.ply` / `artsci1k.ply` | 1000 点稀疏配准点（法线全 0），仅供官方评测程序预配准 | **skip** | — | ☆ |
| `Evaluation.zip`（Windows exe）、`Simulator.zip`（UE4 工程） | 官方评测二进制，以及 UE + AirSim 采图工程 | **skip**（V1.0 以后可参考 UE 高保真路线） | — | ☆ |

**关键结论（实现者先读）：**

1. **数据完整，但坐标体系高度不统一。** 六个城市都是 CloudCompare v2.10.2 导出的 binary PLY：
   - 布局为 `x y z nx ny nz`，float32，24 B/点，头部 293 B，无颜色，无重复点，法线单位长度。
   - 各城市的单位、上方向、倾斜和北向各不相同，必须先规范化再切片。

2. **旧金山的单位不是米，而是约 10.15 m/单位（本次新发现）。** 先前 r01/r05/r07/r08 按米处理，是错的。三组证据：
   - Transamerica→Sutro Tower 基线：模型 619 单位，实地 6.25 km；
   - Transamerica→Oracle Park 基线：216 单位，实地 2.19 km；
   - Transamerica 高度：26.7 单位，实地 260 m。

   另外，旧金山模型的 +x 指向真北，需要绕 Z 轴旋转 +90°。两条基线给出的旋转角分别为 +90.6° 和 +91.5°，彼此只差 0.9°；按镜像假设，两条基线的结果相差超过 150°，互相矛盾，因此排除镜像。

3. **芝加哥的单位是千米（×1000），且整体倾斜 2.0–2.1°。**
   - 倾斜证据：上向法线均值为 (0.032, 0.0146, 0.9974)，地面平面拟合 dz/dx=−0.0329、dz/dy=−0.0149，残差 MAD 仅 2.25 m，对角线方向落差 327 m。必须做调平旋转。
   - 调平后校验：最高点 443.1 m，对应 Willis Tower 屋顶 442 m；Willis→Hancock 向量为 (+1086, +2220) m，实地为 (+1066, +2219) m。

4. **苏州是 Y-up，而且没有地面。**
   - 仅 0.4% 的点属于地面，建筑点占 95%。
   - 走廊式条带，尺寸 4407 × 686 m。
   - 需要合成 z=0 地面，供碰撞、阴影和目标投放使用。

5. **手性：六个城市都是右手系，不存在 UE 左手镜像。**
   - 纽约：Battery 在西南、布鲁克林在东南。
   - 芝加哥：密歇根湖在东、Navy Pier 向东伸出、Soldier Field 在南。
   - 上海：陆家嘴在西北，Shanghai Tower→东方明珠为 (−504, +738) m，实地为 (−554, +681) m。
   - 以上三城和旧金山都经地标定量确认。
   - 因此 **PLY 坐标系 ≠ 航线文件的 UE 坐标系**，二者不能用同一个变换。

6. **法线方向不一致。**
   - 朝下法线占比：深圳 17%，上海 24%。上海另有 0.15% 为零法线。
   - 处理办法：着色器必须做 faceforward，ingest 阶段修正水平面法线。
   - 苏州的法线是面片法线（每 1M 点仅 9186 种），天然呈现"低多边形"平面着色质感。

7. **规范化后的"米级密度"。**
   - 最近邻中位距离：苏州 0.35 m、深圳 0.53 m、纽约 1.02 m、芝加哥 1.59 m、上海 1.85 m、旧金山 1.88 m，只差 5.4 倍。r07 所说的"差 1000 倍"是单位未修正导致的假象。
   - 表面点密度为 0.064–1.84 点/m²。
   - 结论：点大小和 LOD 阈值必须按数据集和节点的 spacing 自动推导。

8. **八叉树参数沿用 r09 的 G=64、LEAF=20000，在规范化数据上实测：**
   - 深度 5–7，节点 658–852 个，单节点最多 31,745 点。
   - 首屏预取层级：深圳、纽约、旧金山取 L≤2（10–18 万点，1.3–2.2 MB）；上海、芝加哥取 L≤3；苏州取 L≤4。
   - 苏州这种条带场景建议改用"多根森林"。

9. **航线文件格式：**
   - 每行 `image_name,x,y,z,pitch,roll,yaw`，坐标为 UE 厘米。
   - **pitch 为俯角，正值向下，90 表示正下视。** 证据：Oblique.log 固定为 90 + 4×45。
   - roll 恒为 0。
   - yaw 为罗盘航向；Smith 的 yaw 100% 是 2.8125°（360/128）的整数倍。
   - 三种方法语义不同：
     - Smith：近似有序，低空近景，高度 5–74 m；
     - Zhang：连续轨迹，每 5 m 一个采样，高度 30–63 m；
     - Zhou：无序视点集，按文件顺序飞行长 23 km，TSP 排序后仅 4.1 km。
   - 合成场景只有约 300 m 见方，与六个城市不对应。

10. **航线直接相似变换到城市上不可行（实测）。**
    - 视点落入建筑安全包络的比例为 1.5%–48%。
    - 即使把视点抬升，Zhou 按原顺序连线仍有 62%–87% 的航段穿楼。
    - 对策：模板只复用"视点分布与云台参数"，连接段由 Height_map 上的安全转场或规划器重新生成。内置城市的任务主要由参数化生成器产生：螺旋立面扫描、割草机覆盖、5 向倾斜网格、扩展方形搜索、走廊跟随、编队。

11. **六个城市恰好覆盖了六类世界形态**：紧凑平地、立面峡谷、超大稀疏、倾斜加千米单位、十米单位加丘陵、Y-up 加无地面。建议固化为 **World Ingest 回归测试集和性能基准**，默认演示城市选深圳。

---

## 1. 仓库与数据概览

### 1.1 仓库事实

| 项 | 内容 |
|---|---|
| 仓库 | https://github.com/Linxius/UrbanScene3D ，项目页 https://vcc.tech/UrbanScene3D |
| 论文 | Lin et al., "Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset", ECCV 2022 |
| Star / 活跃度 | ★156；最后提交 2023-12-12（仅更新 README）；无 2026 年活动 |
| 代码量 | `src/` 3,897 行 C++17，只是一个评测库 `evaluate_model` 加一个测试程序，不含采集、规划或重建代码 |
| 依赖 | Boost（filesystem、serialization）、Eigen3、OpenCV、CGAL、tinyxml2、tinyply、tinyobjloader、Embree 3、glog、TBB、glm、OpenMP。另外代码中 `#include` 了 jsoncpp（`json/reader.h`）和 `argparse`，但 `CMakeLists.txt` 里没有这两个依赖 |
| 许可 | 数据仅限非商业使用，禁止再分发（按任务约定忽略 license；公开演示时见 §6） |
| 数据下载 | Dropbox、Google Drive、百度云；采样点云和评测包在 GitHub Release v0.0.1 |

### 1.2 数据集组成

来源：对 Google Drive 目录 `13-B6IINPFZ4a-Iz_5CcRP0n9ez3E9cTb` 做实测清单，共 352 个文件。

| 目录 | 文件数 | 内容 | 与本项目关系 |
|---|---|---|---|
| `Path/` | 100 | 4 个合成场景（Bridge、Castle、School、Town）<br>× 3 种规划方法（Smith et al. 2018、Zhou et al. 2020、Zhang et al. 2021）<br>× 2 种重叠度（high、low）<br>× 4 种代理（box、coarse、inter、fine）<br>另加每个场景 1 个 `Oblique.log` | **本次下载 99 个**，作为航线模板库 |
| `Image/` | 120 | 各航线对应的渲染图像（zip） | 暂不需要，体积大 |
| `Reconstructed/` | 120 | 各航线图像的重建网格 | V0.5 评测参考 |
| `Proxy/` | 6 | 场景代理几何（ArtSci、Bridge、Castle、PolyTech、School、Town） | 可让轨迹与建筑对齐。本次下载 `School_proxy.zip` 时被 Drive 配额拒绝，待重试 |
| `Capture/` | 4 | 合成场景采图程序 | skip |
| `Evaluation.zip`、`Simulator.zip` | 2 | Windows 评测 exe；UE4 工程 `UrbanScene.uproject` | skip / 远期参考 |

- 六个虚拟城市（New York、Chicago、San Francisco、Shenzhen、Suzhou、Shanghai）的网格因版权原因**不提供**，只发布了采样点云（Release 中的 `UrbanScene3D-virtual_cities-sampled.7z`，252.9 MB）。
- **Path 只覆盖 4 个合成场景，与六个城市完全不对应**，对策见 §3.9 至 §3.11。

### 1.3 本地数据清单

| 文件 | 字节数 | 点数 | 生成信息（PLY comment） |
|---|---|---|---|
| `Chicago_sampled_5m.ply` | 120,009,677 | 5,000,391 | CloudCompare v2.10.2，2023/3/7 12:15 |
| `New York_sampled_5m.ply` | 120,001,853 | 5,000,065 | 2023/3/7 12:14 |
| `San Francisco_sampled_5m.ply` | 120,002,477 | 5,000,091 | 2023/3/7 12:17 |
| `Shenzhen_sampled_5m.ply` | 120,003,677 | 5,000,141 | 2023/3/7 12:14 |
| `Suzhou_sampled_5m.ply` | 119,996,861 | 4,999,857 | 2023/3/7 12:12 |
| `shanghai_sampled_5m.ply` | 120,005,141 | 5,000,202 | 2023/3/7 11:02 |
| `polytech1k.ply` / `artsci1k.ply` | 24,292 | 1,000 | 2023/12/12；法线全 0 |
| `UrbanScene3D-virtual_cities-sampled.7z` | 252,857,193 | — | 原始压缩包，可删除或归档 |
| `paths/**`（99 个文件，3.2 MB） | — | 43,654 视点 | Drive 下载 |

- 点数都不是整 5,000,000。这符合 CloudCompare "Sample points on a mesh"按面积加权随机采样的特征（推断）。
- 注意：用来采样的并不是仓库里的 `sample_points`，因为 PLY 注释写明由 CloudCompare 生成。

### 1.4 与其他研究单元的关系与修正

r01、r02、r05、r06、r07、r08、r09、r12 都用过这批数据。本单元独立复核后，对它们的结论做如下修正：

| 先前结论 | 修正 |
|---|---|
| r01："六个场景法线主轴都是 Z" | **错误**：苏州为 +Y-up（与 r06、r07、r09 一致） |
| r01、r05、r08：旧金山为米制，范围 740×717 m | **错误**：1 单位 ≈ 10.15 m，实际范围约 7.24×7.48 km，高差 555 m（含 Twin Peaks 丘陵），并需旋转 +90° |
| r07："六个城市点距相差 1000 倍" | 规范化后只差 **5.4 倍**（0.35–1.88 m） |
| r06、r09："Chicago 1 单位 ≈ 1 km" | 正确，另需 **2.03° 调平**（前文均未发现） |
| r06：可能存在"UE 左手系镜像" | 经地标验证，**六个 PLY 都不镜像**；左手系只出现在航线文件 |

---

## 2. 源码结构与关键模块

### 2.1 目录与构建

```
UrbanScene3D/
├── CMakeLists.txt          # find_package 13 个依赖；定义 link_private()；add_subdirectory(src, test)
├── src/
│   ├── CMakeLists.txt      # add_library(evaluate_model ${SOURCE_FILE})
│   ├── evaluate_model.{h,cpp}      # class EvaluateModel：精度 / 完整度评测
│   ├── model_tools.{h,cpp}         # 模型 IO、OBJ 拆分、采样、Height_map（1630 行，核心）
│   ├── intersection_tools.{h,cpp}  # CGAL AABB / Embree 射线：深度图、可见性
│   ├── cgal_tools.{h,cpp}          # Rotated_box、包围盒、OBJ→Surface_mesh
│   └── common_util.{h,cpp}         # JSON 配置 + argparse 覆盖、计时、颜色表、全局 RNG
└── test/test_evaluate.cpp  # main(gt_points, recon_points) → evaluate()
```

**构建难度高，不建议编译：**
- 需要 CGAL、Embree 3、OpenCV 等重依赖。
- jsoncpp 和 argparse 未在 CMake 中声明。
- `common_util.cpp::override_sleep` 用到 Windows 的 `_sleep`。
- 官方只提供 Windows 版二进制。

本项目只移植算法（§3.8、§3.12）。

### 2.2 关键类与函数

| 文件 · 符号 | 作用 | 算法要点 | 对本项目的价值 |
|---|---|---|---|
| `evaluate_model.h` · `class EvaluateModel` | 重建评测主类 | 成员：`gt_point_set` / `recon_point_set`（CGAL `Point_set_3`）、`kdtree_gt_points` / `kdtree_recon_points`（`Orthogonal_k_neighbor_search`）、`bvhtree_*`（AABB 树）、`mesh_sampling_density=1000`、`filter_z=-9` | 评测接口设计 |
| `evaluate_model.cpp` · `read_gt_model` / `read_recon_model` | 读网格后按密度采样成点 | `sample_points_according_density(faces, 1000 /m²)` | "网格转点云评测"的规范 |
| `evaluate_model.cpp` · `evaluate()` | 核心评测 | ① Accuracy：每个重建点到 GT 的 1-NN 距离（OpenMP 并行）<br>② Completeness：每个 GT 点到重建的 1-NN 距离<br>③ Chamfer = mean(acc) + mean(comp)（**不除以 2**）<br>④ Hausdorff = max(max acc, max comp)<br>⑤ 50/60/70/80/90/95% 分位<br>⑥ 误差低于 {0.005, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.5, 1} m 的点占比<br>⑦ 可选输出带 `error` 属性的 PLY 和离群点 PLY | **V0.5 World Fusion QA**，也可给 LingBot-Map 与 LiDAR 做对比 |
| `model_tools.h` · `class Height_map` | 2.5D 安全高度图 | 构造：按包围盒建 `CV_32FC1` 栅格，逐点取 `max(z)`，可 `+ safe_distance`，再用 3×3 矩形核 `cv::dilate` 迭代 `v_dilate` 次<br>`is_safe(p)` 判断 `get_height(x,y) < p.z`<br>`get_height` 支持严格多边形边界，界外返回 +∞<br>`update(Rotated_box)` 增量写入障碍<br>`save_height_map_png/tiff/mesh` | **mock 碰撞与安全高度的最低成本实现**（§3.8） |
| `model_tools.cpp` · `get_bounds(path, v_bounds)` | 从 OBJ 取 xmin/xmax/ymin/ymax/zmax+bounds | 注释写明 "to determine drone position" | 起飞点和作业范围的初值 |
| `model_tools.cpp` · `split_obj(dir, name, resolution, filter_height, max_num, out, split_axis)` | 城市网格单体化 | ① 所有三角形投影到 `split_axis` 以外的两个轴，光栅化成鸟瞰图，并记录每格三角形列表<br>② 8 邻域 BFS 求连通域<br>③ 每栋建筑单独输出 OBJ，并以中心归零，偏移写进 `<id>.txt`<br>④ `Z_THRESHOLD` 过滤低矮物体<br>⑤ `split_axis` 参数说明作者也遇到过 Y-up 数据 | 建筑实例、语义层（V1.0），思路已用于 §3.4 的连通域统计 |
| `model_tools.cpp` · `sample_points(mesh, n)` | 面积加权三角形均匀采样 | `u = 1-√r1, v = r2·√r1, w = 1-u-v`；每面样本数 = 面积 × 密度，小数部分按概率补 1；法线取面法线 | mesh 转点云，用于 Geometry World 采样 |
| `model_tools.cpp` · `load_footprint` / `get_polygons` | 读 footprint（`height` + 顶点序列）和多段多边形（`x y index`） | 文本格式解析 | 禁飞区、建筑轮廓格式参考 |
| `model_tools.cpp` · `read_model(path, pts, nrm, idx, faces)` | tinyply 快速读 PLY | 小于 1 GB 的文件整块读入内存流，否则走文件流；支持 f32/f64 顶点与法线、u32/i32 面 | 与 `plyio.py` 的 memmap 思路一致 |
| `intersection_tools.cpp` · `get_depth_map_through_meshes` | 逐像素射线求交，输出实例 / 平面深度 / 透视深度三张图 | CGAL AABB `first_intersection` | 深度相机 mock 参考（服务器侧） |
| `intersection_tools.cpp` · `is_visible(K, T, view, p, RTCScene, max_d)` | 视点对点的可见性 | 先投影判断视锥（`0<u<2cx, 0<v<2cy`），再用 Embree `rtcIntersect1` 取首个交点，比较距离 | 目标搜索的 LOS 判定参考（§3.11 用高度图近似） |
| `intersection_tools.cpp` · `remove_points_inside` | 剔除网格内部点 | 包围盒外 26 个远相机，任一射线交点数 ≤1 即判为可见 | 清洗采样点 |
| `cgal_tools.h` · `struct Rotated_box` | 旋转包围盒，`inside_2d` | `cv::minAreaRect` | 建筑 OBB、任务区域 |

### 2.3 源码缺陷清单（移植时必须修正）

| # | 位置 | 问题 | 影响与修正 |
|---|---|---|---|
| 1 | `EvaluateModel::filter_points_by_z(Point_set point_set)` | 参数按**值传递**，过滤结果被丢弃 | `test()` 里的 z 过滤实际不生效。移植时改为返回新数组 |
| 2 | `evaluate()` 中的 `sorted.size()/100*50` | 整数除法先截断，N<100 时下标恒为 0 | 改用 `np.percentile` |
| 3 | `std::accumulate(..., 0.f)` | 用 float 累加上亿个 double，精度损失明显 | 改用 double 或 `np.mean` |
| 4 | `read_recon_model` | 实际按 1 点/m² 采样，但日志写的是 1000 | 采样密度要显式配置 |
| 5 | `sample_points` | 多个 OpenMP 线程共享同一个 `std::mt19937 gen`（数据竞争），且写入加 `omp critical`（性能差） | 结果不可复现。numpy 版改为按面向量化 |
| 6 | `Height_map` 构造函数 | `(int)((y-start)/res)` 在点恰好落在 max 边界时，索引可能与 `delta+1` 行数不一致；默认高度 `-610610.61` 是魔数 | 移植时统一用 `floor` 加 clip，默认高度改为 NaN 或 0 |
| 7 | 航线的 pitch 语义（README 未说明） | 与 UE `FRotator.Pitch` 符号相反：90 为正下视 | 见 §3.5 |

### 2.4 数据格式

**PLY（6 个城市一致）：**
```
ply
format binary_little_endian 1.0
comment Created by CloudCompare v2.10.2 (Zephyrus)
comment Created 2023/3/7 12:15
obj_info Generated by CloudCompare!
element vertex 5000391
property float x / y / z / nx / ny / nz
end_header                       ← 头部共 293 B，随后是 N×24 B 的 AoS
```

**航线文件（`Path/**/<Scene>_<proxy>.txt`）：**`image_name,x,y,z,pitch,roll,yaw`

- 坐标：UE 世界坐标，**左手系，Z-up，单位 cm**，原点为 PlayerStart（AirSim 同样以 PlayerStart 为原点）。
- `image_name`：Smith 和 Oblique 用 `0000.png` 形式，Zhang 和 Zhou 用 `0000` 形式（不带扩展名）。
- `pitch`：**俯角，正值向下**，90 为正下视。例如 `Oblique.log` 每个网格点固定 5 行：`90,0,0` 加 `45,0,{0,90,180,270}`。存在负值：Zhou 的 Bridge 最低 −48°，属于仰拍桥底；Smith 的两个文件中有 −90°，属于无效或朝天视点，应截断。
- `roll`：43,654 行中全部为 0。
- `yaw`：航向角，单位度，从 UE +X 顺时针（俯视）。Smith 的值 100% 是 2.8125° 的整数倍，pitch 为整数度，说明该方法在离散角度格上规划（推断）。
- 相机：README 写明合成场景 `fov=60`（水平），分辨率 6000×4000。因此垂直 FOV = 2·atan(tan30°·4000/6000) = **42.1°**，fx = 3000/tan30° = 5196 px。

---

## 3. 可复用算法与实现

### 3.1 原始数据统计（未规范化）

| 城市 | 原始范围 x / y / z | 原始尺寸 | 上方向判定 | 单位（到米） | 朝下法线 | 零法线 | 不同法线数 / 1M 样本 |
|---|---|---|---|---|---|---|---|
| Shenzhen | [−605.6, 1242.5] / [−748.5, 1250.5] / [−48.8, 342.1] | 1848×1999×391 | +Z（z 向上占 55.6%） | 1 | **17.0%** | 0 | 33,378 |
| Shanghai | [−684.1, 7052.3] / [−4414.1, 1796.6] / [−15.1, 630.4] | 7736×6211×645 | +Z | 1 | **24.3%** | 0.149% | 12,236 |
| New York | [−1447.0, 1481.1] / [−1607.7, 1559.0] / [−22.0, 270.5] | 2928×3167×292 | +Z | 1 | 0.003% | 0 | 98,146 |
| San Francisco | [−370.1, 370.0] / [−358.5, 358.5] / [−27.5, 27.4] | 740×717×55 | +Z | **≈10.15** | 0.13% | 0 | 78,238 |
| Suzhou | [−1887.5, 2519.4] / [−2.8, 152.7] / [−1032.4, −346.0] | 4407×155×686 | **+Y**（y 正向 16.3%、负向 2.6%；z 正负对称，均约 23.5%，说明 z 是水平轴） | 1 | 2.6% | 0 | **9,186**（面法线，低模） |
| Chicago | [−11.10, −6.93] / [−0.53, 7.51] / [−0.098, 0.518] | 4.17×8.04×0.62 | +Z（**倾斜 2.03°**） | **1000** | 0.001% | 0 | 45,696 |

- **上方向判定**沿用 r07 的打分：`score_a = f_a · |m_a|`，其中 `f_a` 是 `|n_a|>0.9` 的点占比，`m_a` 是这些点的法线符号均值。苏州的 y 得分为 0.137，z 约为 0，据此判为 Y-up。
- **单位的自动初判（新增启发式，六个城市全部正确）：** `s = 10^round(log10(120 m / HAG_p99_raw))`。
  - 各城原始 HAG p99：旧金山 8.25 → s=10；芝加哥 0.166 → s=1000；其余在 116–167 之间 → s=1。
  - 这只是初值。最终以地标量测为准，写进 `coordinate.json`（旧金山取 10.15）。

### 3.2 单位、手性与北向的地标证据

实地坐标取自公开 WGS84 值，按局部平面换算：`ΔE = Δlon·111.32·cos(lat)`，`ΔN = Δlat·111.0`（km）。模型坐标是 §3.3 规范化后的 ENU 值（m），地标位置取高度图局部极大值（5 m 栅格，误差约 ±10–20 m）。

| 城市 | 地标 / 基线 | 模型（规范化后） | 实地 | 结论 |
|---|---|---|---|---|
| Chicago | Willis Tower 高度 | 443.1 m | 442 m（屋顶） | ×1000 正确 |
| Chicago | Willis → John Hancock | (+1086, +2220) m | (+1066, +2219) m | 北向正确，未镜像，误差约 1% |
| Chicago | Aon Center 相对 Willis | (+1230, +702) m，高 356.8 | (+1190, +700) m，高 346 | 一致 |
| Shanghai | Shanghai Tower / 环球金融中心 / 东方明珠高度 | 636.7 / 499.1 / 479.0 m | 632 / 492 / 468 m | 米制 |
| Shanghai | ST → 东方明珠 | (−504, +738) m | (−554, +681) m | 北向正确，未镜像（角度差约 5°，属于地标定位误差） |
| New York | 70 Pine / 40 Wall Street 高度 | 287.4 / 282.7 m | 290 / 283 m | 米制 |
| New York | 70 Pine → 40 Wall | (−172, +64) m | (−152, +67) m | 北向正确（另有定性证据：Battery 在西南、布鲁克林在东南、下东城十字形公屋位于东河北岸） |
| San Francisco | Transamerica → Sutro Tower | 原始 (−435, +440) 单位，长 619 | 实地 (−4.41, −4.44) km，长 6.25 km | 比例 **10.10**；原始角 134.7°，实地角 −134.8°，**旋转 +90.6°** |
| San Francisco | Transamerica → Oracle Park | 原始 (−184.6, −113) 单位，长 216 | (+1.19, −1.84) km，长 2.19 km | 比例 **10.15**；旋转 +91.5°（与上一行一致）；按镜像假设两条基线残差大于 150° |
| San Francisco | Transamerica 高度 | 26.7 单位 → 269 m | 260 m | 支持约 10 m/单位 |
| Shenzhen | 最高 381.3 m、次高 303.9 m、300.5 m | — | 推断为南山后海（华润大厦 393 m） | 米制可信，北向**未验证** |
| Suzhou | 最高 152.4 m | — | — | 米制可信，北向与手性**未验证**；按其余五城一致的右手系假设，采用 (x, −z, y) 旋转（det=+1） |

**结论：**
- 这批采样点云都经过右手系的 DCC 或 CloudCompare 流程，**没有**残留 UE 的左手镜像。
- 旧金山的 +x 指向真北（+y 指西），相当于把 AirSim"X=北"的约定套在一个右手系上。
- 由此可知 **PLY 坐标与航线 UE 坐标是两套不同的坐标系**。

### 3.3 Ingest 规范化流水线

目标模块：`world/ingest/urbanscene3d.py`，V0.1。

```python
# 输出：points ENU(m, Z-up, 右手), normals(单位, 已翻正), hag, class, coordinate.json, qa.json
CFG = {  # 由本研究实测确定；s 为到米的比例；up: 源上轴；level: 是否调平；yaw: 绕 Z 旋转到近似真北
  "shenzhen":     dict(s=1.0,    up="+z", level=False, yaw=0.0,  north="unknown"),
  "shanghai":     dict(s=1.0,    up="+z", level=False, yaw=0.0,  north="verified"),
  "newyork":      dict(s=1.0,    up="+z", level=False, yaw=0.0,  north="verified"),
  "sanfrancisco": dict(s=10.15,  up="+z", level=False, yaw=+90.0, north="verified"),  # 地形真实起伏，禁止调平
  "suzhou":       dict(s=1.0,    up="+y", level=False, yaw=0.0,  north="unknown", synth_ground=True),
  "chicago":      dict(s=1000.0, up="+z", level=True,  yaw=0.0,  north="verified"),
}

def ingest(ply, c):
    P, N = read_ply_memmap(ply)                        # float32 → float64；检查 hdr + n*24 == size
    R = R_up(c.up)                                     # +y: [[1,0,0],[0,0,-1],[0,1,0]]  (x,y,z)->(x,-z,y)
    if c.level:                                        # 调平：上向法线均值 → +Z
        m  = (N @ R.T)[:, 2] > 0.95
        nu = (N @ R.T)[m].mean(0)                      # Chicago: (0.0321, 0.0146, 0.9974) → 2.03°
        R  = rodrigues(nu, [0,0,1]) @ R                # 仅当 ground-plane 残差 MAD < 5 m 且倾角 > 0.5° 时启用
    R  = Rz(c.yaw) @ R
    Q  = c.s * (P @ R.T);  Nn = N @ R.T                # 法线不乘尺度
    dtm = dtm_opening(Q, cell=10, k=4)                 # 每格 min z → 9×9 最小滤波 → 9×9 最大滤波 → NaN 邻域填充
    if ground_frac(Q, Nn, dtm) < 0.10: dtm[:] = pct(Q.z, 0.5)   # Suzhou：无地面 → 平地面
    o  = [(Q.x.min()+Q.x.max())/2, (Q.y.min()+Q.y.max())/2, median(dtm)]
    E  = Q - o                                         # 局部 ENU：XY 居中，Z=0 为地面中位数
    Nn = fix_normals(E, Nn, hmap)                      # §3.7
    hag = E.z - dtm_at(E.x, E.y)
    cls = classify(Nn, hag)                            # §3.7：2 地面 / 64 立面 / 65 屋顶 / 1 其他
    T  = [[s*R, -o], [0, 1]]                           # 4×4，写入 coordinate.json
    return E.astype(f32), Nn, hag, cls, T
```

**实测变换矩阵** `p_enu = T · [p_raw; 1]`，写入 `coordinate.json.source.T_enu_src`：

```
Shenzhen  : [[1,0,0,-318.432],[0,1,0,-250.994],[0,0,1, 31.908]]
Shanghai  : [[1,0,0,-3184.088],[0,1,0,1308.725],[0,0,1, 6.317]]
NewYork   : [[1,0,0,-17.047],[0,1,0,24.387],[0,0,1,16.502]]
SanFran   : [[0,-s,0,tx],[s,0,0,ty],[0,0,s,tz]]      # s=10.15；本次统计用 s=10.1 时 t=(0.369, 0.424, 163.908)
Suzhou    : [[1,0,0,-315.931],[0,0,-1,-689.212],[0,1,0,-0.280]]
Chicago   : [[ 999.4832, -0.2358, -32.1450,  9009.184],
             [  -0.2358, 999.8924, -14.6687, -3489.145],
             [  32.1450,  14.6687, 999.3756,   209.317]]   # = 1000·R_level + t
```

规范化后，所有坐标满足 |ENU| ≤ 4.02 km。float32 在 4096 m 处的 ULP 为 0.49 mm，所以 GPU 端可以直接使用世界坐标，并且与 r09 的 ANET_Q16 节点局部量化兼容。

**`coordinate.json` 建议字段**（在 r09 的 `anet` 扩展基础上补充）：

```json
{ "frame": "LOCAL_ENU", "units": "m", "upAxis": "Z", "handedness": "right",
  "georef": null,
  "approxTrueNorthYawDeg": 0.0, "northConfidence": "verified|unknown",
  "source": { "dataset": "UrbanScene3D", "file": "San Francisco_sampled_5m.ply",
              "unitsToMeters": 10.15, "upAxis": "+z", "leveledDeg": 0.0, "yawDeg": 90.0,
              "T_enu_src": [[...4x4...]],
              "evidence": ["Transamerica→Sutro 6.25 km / 619 u", "Transamerica→Oracle 2.19 km / 216 u"] },
  "ground": { "type": "dtm|flat|synthetic", "z": 0.0, "dtm": "geometry/terrain/dtm_10m.f32", "cell": 10.0 },
  "qa": { "normalsFlipped": 0.17, "zeroNormals": 0.0, "nnMedian": 0.526 } }
```

### 3.4 规范化后的统计表（米制 ENU）

| 城市 | 尺寸 E×N×U (m) | 平面包围盒 (km²) | 地面起伏 DTM p1–p99 | HAG p50 / p90 / p99 (m) | 最高 (m) | 类别占比<br>地面 / 立面 / 屋顶 / 其他 | 最近邻距离<br>中位 / 均值 / p90 (m) | 表面密度 (点/m²) | 估计表面积 (km²) | 每个已占用 1 m 平面格的点数 | 1 m 体素内平均点数 | 建筑数（≥50 m²） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Shenzhen** | 1848×1999×391 | 3.69 | 约 9 m（有小丘） | 2.8 / 23.2 / 130.0 | 381.3 | 45 / 25 / 26 / 4 % | **0.53** / 0.59 / 1.05 | 0.72 | 6.9 | 2.48 | 1.38 | 372（CBD 裙房连成一片） |
| **Shanghai** | 7736×6211×645 | 48.05 | 近平（平面残差 MAD 0.00 m；DTM p1–p99 跨 8.6 m，含河道） | 4.7 / 28.1 / 116.2 | 636.7 | 49 / 21 / 29 / 1 % | 1.85 / 1.98 / 3.42 | 0.064 | 78.6 | 1.20 | 1.03 | 3957 |
| **New York** | 2928×3167×292 | 9.27 | 约 23 m | 11.3 / 68.1 / 167.3 | 287.4 | 15 / **52** / 31 / 3 % | 1.02 / 1.08 / 1.82 | 0.216 | 23.1 | 2.10 | 1.10 | 3220 |
| **San Francisco** | 7242×7475×555 | 54.13 | **约 268 m（丘陵）** | 3.4 / 18.6 / 83.3 | 268.4 | 35 / 23 / 31 / 10 % | 1.88 / 1.98 / 3.33 | 0.064 | 78.5 | 1.16 | 1.03 | 2964 |
| **Suzhou** | 4407×686×155 | 3.03 | **无地面** | 17.2 / 53.9 / 118.5 | 152.4 | **0.4** / **77** / 19 / 5 % | **0.35** / 0.37 / 0.63 | 1.84 | 2.7 | 12.1 | 2.18 | 306 |
| **Chicago** | 4176×8038×450 | 33.56 | 调平后平坦 | 1.1 / 59.4 / 166.3 | 443.1 | 47 / 32 / 8 / 13 % | 1.59 / 1.67 / 2.81 | 0.090 | 55.7 | 1.37 | 1.04 | 2559 |

**口径说明：**
- 最近邻距离：用 Open3D `compute_nearest_neighbor_distance` 计算全量 5M 点，每城约 5–7 s。本机需要按 r06 的方法设置 `LD_LIBRARY_PATH` 指向本地 libEGL。
- 表面密度：按二维泊松过程估计，ρ = 1/(4·d̄²)；表面积 = N/ρ。
- 建筑数：在 2–3 m 高度栅格上取 HAG > 5 m 的区域，腐蚀 1 格后求 4 连通域。
- 分类规则见 §3.7。

**对 Geometry World 的直接含义：**
- 上海、旧金山、芝加哥的 1 m 体素里平均只有约 1 个点。1 m 占据栅格会出现"漏洞"，碰撞体素宜取 **2 m 并膨胀 1 格**。
- 深圳、苏州、纽约可以用 1 m。

### 3.5 坐标变换全集（UE ⇄ ENU ⇄ Three.js ⇄ PX4）

约定：
- 世界系：ENU（E=x，N=y，U=z，右手，米）。
- 机体系：FLU（REP-103）。
- 航向 `heading`：从北顺时针。
- ENU yaw `ψ`：从东逆时针。

**(a) UrbanScene3D 航线（UE 左手 Z-up cm）→ ENU**

沿用 AirSim 的约定：UE +X 为北，+Y 为东。

```
E = Y_ue/100 + tE      N = X_ue/100 + tN      U = Z_ue/100 + tU      # 交换 X/Y：det=-1，左手 → 右手
heading = yaw_file                         # UE yaw 即罗盘航向（+X=北，俯视顺时针）
ψ_enu   = 90° − yaw_file
θ_flu   = +pitch_file                      # FLU 下正 pitch 为低头；90 = 正下视（与文件语义恰好一致）
φ       = roll_file (= 0)
q_enu_flu = Rz(ψ) · Ry(θ) · Rx(φ)          # ZYX 内旋；相机光轴 = 机体 +x
forward_enu = (cosθ·cosψ, cosθ·sinψ, −sinθ)
```

验证：
- `yaw=0, pitch=0` 时 forward=(0,1,0)，指向北。
- `yaw=90` 时 forward=(1,0,0)，指向东。
- `pitch=90` 时 forward=(0,0,−1)，正下视。

与 AirSim 的一致性：`NED = (X, Y, −Z)/100`，即 N=X、E=Y、D=−U，与上式一致。

**(b) 点云 PLY → ENU：** `p_enu = T_enu_src · p_raw`（§3.3）。法线 `n_enu = R · n_raw`，不乘尺度。两者**不能**套用 (a) 的 X/Y 交换，因为 PLY 已经是右手系。

**(c) ENU → Three.js（Y-up）**

```
x_three =  E      y_three = U      z_three = −N           # = R_x(−90°) · p_enu
q_three = q_x(−90°) ⊗ q_enu ⊗ q_x(−90°)^{-1}
```

实现上，把 WorldLayer 整体放进 `group.rotation.x = −π/2` 的容器里，内部全部使用 ENU。

相机姿态：three 相机看向自身 −Z、+Y 为上。它相对机体 FLU 的固定旋转为：相机 −Z = 机体 +X，相机 +Y = 机体 +Z，相机 +X = 机体 −Y。写成矩阵，列依次为相机 x、y、z 轴在 FLU 中的表示：`R_flu_cam = [[0,0,−1],[−1,0,0],[0,1,0]]`。

**(d) ENU/FLU ⇄ PX4 NED/FRD**（与 MAVROS `ftf` 相同）

```
p_ned = (N, E, −U)
q_ned_frd = q_ENU→NED ⊗ q_enu_flu ⊗ q_FLU→FRD
q_ENU→NED = (w=0, x=√½, y=√½, z=0)        q_FLU→FRD = (w=0, x=1, y=0, z=0)
yaw_ned = 90° − ψ_enu = heading
```

TypeScript 参考实现（前端 `lib/frames.ts`）：

```ts
export const enuToThree = (e: number, n: number, u: number) => new THREE.Vector3(e, u, -n);
export function us3dViewToEnuPose(r: {x:number;y:number;z:number;pitch:number;roll:number;yaw:number},
                                   off = {e:0, n:0, u:0}) {
  const pos = { e: r.y / 100 + off.e, n: r.x / 100 + off.n, u: r.z / 100 + off.u };
  const psi = THREE.MathUtils.degToRad(90 - r.yaw), th = THREE.MathUtils.degToRad(r.pitch);
  const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, th, psi, 'ZYX')); // q_enu_flu
  return { pos, q, headingDeg: ((r.yaw % 360) + 360) % 360, gimbalPitchDeg: -r.pitch };  // UI 显示用仰角
}
```

### 3.6 World Package 八叉树推荐参数（在规范化数据上实测）

算法用 r09 的 `build_fast`：加法式、Potree 2.0 节点命名、网格竞选。参数 G=64，LEAF=20000。

| 城市 | 立方体边长 L (m) | spacing_root = L/64 | 深度 | 节点数 | 单节点点数（最大 / 中位） | 累计点数 L≤1 / L≤2 / L≤3 / L≤4 | 首屏层级 | 首屏字节（ANET_Q16，12 B/点） |
|---|---|---|---|---|---|---|---|---|
| Shenzhen | 1999.0 | 31.24 | 5 | 696 | 24,755 / 6,533 | 26,785 / **124,701** / 646,510 / 2,687,182 | L≤2 | 1.50 MB |
| Shanghai | 7736.3 | 120.88 | 5 | 676 | 18,884 / 7,518 | 12,920 / 75,198 / **419,200** / 1,780,053 | L≤3 | 5.03 MB |
| New York | 3166.7 | 49.48 | 5 | 852 | 31,745 / 3,860 | 35,351 / **181,872** / 851,069 / 3,551,028 | L≤2 | 2.18 MB |
| San Francisco | 7475.1 | 116.80 | 5 | 658 | 19,979 / 6,022 | 25,572 / **108,334** / 457,088 / 3,453,743 | L≤2 | 1.30 MB |
| Suzhou | 4406.9 | 68.86 | 7 | 720 | 19,874 / 6,076 | 2,529 / 11,139 / 84,139 / **288,459** | L≤4 | 3.46 MB |
| Chicago | 8037.6 | 125.59 | 6 | 787 | 23,089 / 4,514 | 12,516 / 76,546 / **352,989** / 1,234,206 | L≤3 | 4.24 MB |

纽约的结果与 r09 在原始数据上的实测完全一致（852 节点，L≤2 为 181,872 点），交叉验证了流水线。每城 `octree.bin` 约 60 MB（ANET_Q16 未压缩），`hierarchy.bin` 约 15–19 KB（节点数 × 22 B）。本机单线程建树 5.0–5.6 s。

**推荐参数与公式：**

- **G=64，LEAF=20000，spacing_root = L/64，第 d 层 spacing_d = spacing_root/2^d。** 叶节点保留剩余全部点，有效点距约等于 nn_median。
- **首屏层级**：取最小的 d，使 `cum(d) ≥ 1e5`，且不超过首屏预算（桌面 20 万点；无头 SwiftShader 4 万点，见 r12，此时回退到 L≤1 或 L≤2）。把 `levelsByteEnd[d]` 写入 metadata，发一次 HTTP Range 请求取回。
- **点尺寸（世界单位）**：
  - 内部节点：`size_d = k·spacing_d`，k=1.0–1.4。
  - 叶节点：`size_leaf = max(k·spacing_D, 1.6·nn_median)`。
  - 屏幕像素限定在 [1, 6] px。nn_median 取自 §3.4，写入 `metadata.anet.nnMedian`。
- **LOD 细分判据**（与 r12 一致）：`px = spacing_d · (H_px / (2·tan(fov/2))) / dist`。`px > τ` 时细分，τ 在 1.0–1.5 px 之间由 FPS 闭环调节。节点内点序已洗牌，前缀渲染可以平滑调整疏密。
- **苏州这类细长场景**：立方体根节点让前两层几乎为空（L0 只有 501 点，L≤2 仅 1.1 万点）。建议 World Package 支持**多根森林**：`roots: [{ name, min, size }, ...]`，苏州沿长轴切成 6 个 735 m 的立方体，各自深度约 5。这对未来大于 10 km 的真实场景同样有用，便于分区设原点，参见 r09 关于 ENU 平面误差的讨论。
- **Geometry World 占据体素**：深圳、苏州、纽约用 1 m；上海、旧金山、芝加哥用 2 m 并膨胀 1 格（依据 §3.4 的"1 m 体素内平均点数"）。

### 3.7 无颜色着色方案

依据：
- UrbanScene3D 没有 RGB 和强度，r12 已指出 potree-core 对 2.0 数据会整片发黑。
- 色板沿用 d01 的 **ANet Graphite**：灰阶 g950 `#0A0B0D` 到 g50 `#F2F3F5`，品牌红 r500 `#E93024`。

**(1) ingest 阶段的法线修正 `fix_normals`：**
```
for p with |n_z| > 0.9:                                # 水平面（地面 / 屋顶）
    if n_z < 0 and z_p ≥ hmap_top(p.xy) − 1.0 m:  n = −n   # 位于该格最高表面却朝下 → 绕序错误
for p with |n_z| < 0.3 and hag ≥ 1:                    # 立面
    q = p.xy + 2 m · n_xy
    if hmap_top(q) > z_p + 1:  n = −n                  # 法线指向建筑内部 → 翻转
zero-length n → n = (0,0,1)                            # 上海 0.15%
```

**(2) 分类字节**，写入 ANET_Q16 的 `col.w`，编码遵循 LAS 1.4，64 以上为用户自定义：
- 地面（2）：`hag<1 ∧ |n_z|>0.9`
- 立面（64）：`hag≥1 ∧ |n_z|<0.3`
- 屋顶（65）：`hag≥2.5 ∧ |n_z|>0.9`
- 其他（1）
- 水面（9）为可选项：地面格中离最近建筑超过 150 m、且 z 低于地面 p5 的大面积平区，对应芝加哥湖面、纽约港。

**(3) 着色模式**（前端 `PointColorMode`，默认 Height + Normal + EDL）：

| 模式 | 公式 | 适用 |
|---|---|---|
| Height | `t = clamp((z − z_p1)/(z_p99 − z_p1), 0, 1)^0.6` → Graphite 渐变 g800 `#1D1F23` → g600 `#3E4249` → g400 `#81868F` → g200 `#CACDD3` → g50 `#F2F3F5`。γ=0.6 用来拉开低层建筑 | 默认。p1/p99 写入 metadata（深圳 −7.3/124.7；纽约 −0.6/167.3） |
| HAG | 同上，改用 `hag` 的 p1–p99 | **旧金山必须用**：地形起伏 268 m 会吞掉建筑的层次 |
| Normal | `n' = faceforward(n, viewDir)`；`c = base·(0.45 + 0.55·max(0, n'·L))`，外加半球环境光 `0.15·(0.5+0.5·n'_z)` | 与 Height 相乘。苏州的面法线会呈现平面着色质感 |
| EDL | r01/r12 的参数：strength 0.3–0.5，半径 1.5–2 px | 桌面默认开；SwiftShader 无头档关闭（r12） |
| Class | 地面 g800，立面按高度 g500→g300，屋顶 g200→g50，水面 g900 | 语义检查、LiDAR 仿真调试 |
| Intensity（伪） | 反射率 `ρ_cls`：地面 0.25、立面 0.45、屋顶 0.60、水面 0.05<br>加每点固定种子 hash 噪声 ±0.08，量化为 u8<br>LiDAR mock 返回强度 `I = ρ·|cos θ_inc|·(r0/r)²` | 与 r04/r05 虚拟雷达共用 |
| 高亮 | 只用于选中、告警、禁飞区、任务目标：`#E93024`。暗色主题下的文字用 `#FF5242` | 红色只给"主角"，不参与数据渐变 |

**着色器伪代码**（TSL 和 GLSL 同构）：
```glsl
vec3 n  = decodeOct16(a_pos.w);  n = dot(n, V) > 0.0 ? -n : n;       // faceforward（V = 相机→点）
float t = pow(clamp((wz - uZ1) / (uZ99 - uZ1), 0.0, 1.0), 0.6);
vec3 base = graphiteRamp(t);                                          // 5 档分段线性，uniform 数组
float lam = 0.45 + 0.55 * max(dot(n, uSunDir), 0.0) + 0.15 * (0.5 + 0.5 * n.z);
vec3 c = base * lam;
if (uClassMask != 0u && isHighlighted(a_col.w)) c = mix(c, vec3(0.914, 0.188, 0.141), 0.85);
```

本机渲染验证（`preview_newyork.png`，俯视，Graphite 渐变 × 翻正法线光照）：楼群层次清晰；1 m/px 下屋顶仍有稀疏空洞，浏览器端需要按上面的点尺寸规则补齐。

### 3.8 Height_map 移植与安全转场

目标模块：`sim/world/heightmap.py`，V0.2。

```python
class HeightMap:                                        # 移植自 model_tools.h::Height_map
    def __init__(self, E, cell=2.0, dilate=2, safe=10.0):
        self.x0, self.y0 = E[:,0].min(), E[:,1].min()
        ix, iy = floor((E.x-x0)/cell), floor((E.y-y0)/cell)
        top = full((H, W), -inf); maximum.at(top, (iy, ix), E.z)   # 等价 cv::Mat CV_32FC1 逐点取 max
        top = where(isfinite(top), top, ground)                    # 空格 = 地面（苏州 = 0）
        self.h = maxfilter(top, 2*dilate+1) + safe                 # 等价 cv::dilate(3×3, dilate 次) + safe_distance
    def get(self, x, y): return self.h[clip(iy), clip(ix)]         # 界外 → 地面 + safe
    def is_safe(self, p): return p.z > self.get(p.x, p.y)

def safe_transit(A, B, hm, margin=5.0, ds=2.0):         # 爬升-巡航-下降，按构造保证无碰撞
    S  = sample_segment(A.xy, B.xy, ds)
    zc = max(hm.get(S).max() + margin, A.z, B.z)
    return [A, (A.x, A.y, zc), (B.x, B.y, zc), B]
```

**实测：**
- `hmap_<city>.npy` 为 2–3 m 栅格，内存 3–24 MB。
- 全图安全高度的 p99 与 p99.9（10 m 栅格，未加 safe）：

| 城市 | p99 | p99.9 |
|---|---|---|
| 深圳 | 56.9 m | 260.8 m |
| 上海 | 105.5 m | 219.0 m |
| 纽约 | 171.6 m | 234.4 m |
| 旧金山 | 49.1 m | 148.5 m |
| 苏州 | 131.6 m | 152.4 m |
| 芝加哥 | 131.3 m | 232.7 m |

- 结论：全局固定巡航高度 120 m 在纽约、苏州、芝加哥会撞楼。**任务高度必须按航线查询 Height_map**，这也应当写进 UI：航线编辑时实时显示"最小安全高度剖面"。

### 3.9 航线文件：解析、排序与重定向

**按方法汇总**（`paths2.py`，每种方法 32 个文件；TSP 为最近邻初解加 2-opt）：

| 方法 | 视点数（中位） | 高度范围（中位） | 云台俯角中位 | 下视（≥85°）/ 倾斜（15–85°）/ 平视（<15°） | 相邻视点步长中位 | 文件顺序长度 vs TSP 长度（中位） | 顺序语义 | 旋转半径 R95 |
|---|---|---|---|---|---|---|---|---|
| **Smith et al. 2018**（连续优化 + 基准） | 155–559（455） | 5–74 m（19） | 26° | 0.00 / 0.75 / 0.25 | 16.2 m | 7.1 km vs 3.4 km | 近似有序（分段扫掠） | 73 m |
| **Zhang et al. 2021**（连续航线规划） | 167–1043（404） | 30–63 m（60） | 47° | 0.07 / 0.79 / 0.14 | **5.0 m** | 3.6 km vs 3.0 km | **连续轨迹**；起点有多帧悬停 | 112 m |
| **Zhou et al. 2020**（离场规划） | 167–1468（379） | 5–105 m（29） | 37° | 0.05 / 0.60 / 0.35 | 59.8 m | **23.2 km vs 4.1 km** | **无序视点集** | 95 m |
| Oblique.log（五向倾斜基线） | 800–1080（每点 5 视） | 80 m 定高 | 90 / 45 | 0.2 / 0.8 / 0 | 网格 18.48 m | 3–4 km | 行扫描 | 约 150 m |

**Oblique.log 的参数反推**（可直接写成生成器）：
- 80 m 高度下，水平 FOV 60° 的地面宽度 W = 2·80·tan30° = 92.4 m；垂直 FOV 42.1° 的地面长度 L = 61.6 m。
- 网格间距 18.48 m，对应**旁向重叠 80%、航向重叠 70%**。
- 每个网格点拍 1 张正下视和 4 张 45° 斜视（yaw 为 0/90/180/270）。

**重定向实验**（`retarget.py`）：
- 做法：把 School 或 Town 的视点集按相似变换放到城市目标上，水平按 R95 缩放，垂直按目标高度加 30 m 缩放；然后用 HeightMap（膨胀 2 格，safe 10 m）检查。
- 深圳目标为 173 m 塔（R=220 m），纽约目标为 70 Pine（R=250 m）。

| 模板 → 城市 | 视点不安全率 | 抬升视点后，按文件顺序连线仍穿楼的航段 |
|---|---|---|
| Smith School_fine → 深圳 / 纽约 | 9.7% / 44.5% | 65/558 / 204/558 |
| Zhang School_fine → 深圳 / 纽约 | **1.5% / 1.8%** | **5/1042 / 12/1042** |
| Zhou Town_fine → 深圳 / 纽约 | 27.4% / 48.4% | 477/769 / 667/769 |

**结论与算法：**

```
load_template(file):
    rows → ENU（§3.5a）; pitch_file 截断到 [-30, 90]（P600 云台下限 −90°、上限约 +30°，越界视点标记 invalid）
    Zhang: 去掉起点的重复帧（位置相同的连续帧只保留一帧）；Zhou: 按 TSP 排序
retarget(template, target):
    视点 = 相似变换(template 视点)；在 HeightMap 中不安全的视点，沿"远离目标中心"的径向外推，直到 is_safe
    连接段 = safe_transit / A*（2.5D，代价 = 路长 + λ·爬升）；禁止直接复用文件顺序的直线段
time_param(path, v=5 m/s, a=2 m/s², ψ̇=45°/s, dwell=1.5 s)   # 航线没有时间戳，由此生成 Timeline
```

- **Zhang** 的模板最适合"真实规划轨迹回放"：高空、连续、平滑。
- **Smith** 适合近景立面，但需要重规划连接段。
- **Zhou** 只能当视点集使用。

另外，把原始航线放在"平地世界"（Blank World，z=0 平面加 100 m 网格）中 1:1 回放，是验证 Timeline、轨迹渲染、视锥显示和 WebSocket 吞吐的零风险测试集。例如 Zhou School_fine 有 1468 个视点，适合做单机 1468 帧回放。

### 3.10 任务生成器公式

目标模块：`sim/mission/generators.py`，V0.2 至 V0.6。相机默认使用 UrbanScene3D 的采图相机（HFOV 60°，VFOV 42.1°），P600 标定后再替换。

| 生成器 | 参数 → 几何 | 公式 |
|---|---|---|
| `lawnmower(poly, h, side_ov, fwd_ov)` | 正下视覆盖 | 地面宽 `W = 2h·tan(HFOV/2) = 1.155h`；航线间距 `s = W(1−side_ov)`；快门间距 `d = 2h·tan(VFOV/2)(1−fwd_ov) = 0.770h(1−fwd_ov)`。h=150 m、70%/80% 时 s=52.0 m，d=23.1 m |
| `helix_scan(c, r_fp, d, z0, z1, ov_v)` | 立面螺旋扫描，相机水平指向中轴 | 半径 `r = r_fp + d`；每圈上升 `Δz = 2d·tan(VFOV/2)(1−ov_v)`。d=30 m、60% 时 Δz=9.24 m；长度 ≈ `2πr·(z1−z0)/Δz` |
| `orbit(c, r, h, n_img)` | 环绕倾斜拍摄 | 云台俯角 `θ = atan2(h − h_target/2, r)` |
| `oblique5_grid(poly, h, 0.8, 0.7)` | 复现 Oblique.log | 网格 `g = min(W·(1−0.8), L·(1−0.7))`；每点拍 1 张下视和 4 张 45° 斜视 |
| `expanding_square(p0, h, leg0)` | 基于基准点的搜索（IAMSAR） | `leg0 = 0.8·W`；腿长序列 L, L, 2L, 2L, 3L, …，方向依次为 E、N、W、S |
| `sector_search(p0, R, n)` | 扇形搜索 | 3n 条腿，每次转 120° |
| `corridor(polyline, h, offset)` | 线状目标巡检（道路、河、管廊） | 两侧偏移 ±offset，斜视 45° 朝向中线 |
| `terrain_follow(path, agl)` | 地形跟随 | `z(s) = dtm(x(s), y(s)) + agl`，再做纵向限坡 `|dz/ds| ≤ tan 15°`（前视最大值滤波） |
| `formation(leader_path, shape, spacing)` | 编队 | 从机 i 的期望位置 `p_i* = p_L + R_z(ψ_L)·o_i`<br>控制量 `u_i = v_L + K_p(p_i* − p_i) + Σ_{j: d_ij<d_s} k_r·(d_s − d_ij)·(p_i − p_j)/d_ij`，再按 v_max 截断<br>参数 K_p=0.8 s⁻¹，d_s=8 m，k_r=1.5 s⁻¹，v_max=12 m/s<br>队形：V 形（35°）、横队、纵队、菱形；队形切换用 2 s 的 min-jerk 插值 |

`missions.py` 的实测（6 城，可复现）：

- **单机立面螺旋扫描的航程远超续航**：
  - 深圳 381 m 塔：11.7 km，4 m/s 下 48.6 min。
  - 上海 Shanghai Tower：29.0 km，120.8 min。
  - 芝加哥 Willis：19.0 km，79 min。
  - 结论：天然适合"多机按高度分段"的协同场景（按 25 min 续航计，分别需要 2、5、4 架）。
- **中心区割草机覆盖**（盒长 = 40% 城市短边，150 m AGL）：
  - 深圳 739 m 见方，14 条线，11.0 km，18.4 min。
  - 纽约 1171 m 见方，28.1 km，47 min。
  - 所有城市都有航点落入超高层的安全包络，例如芝加哥 770 个 5 m 采样点不安全。
  - 所以覆盖航线必须先查询高度图，逐段抬升或绕行。

### 3.11 基于六城的 mock 任务剧本

**剧本 JSON Schema**，文件放在 `scenarios/*.json`，由仿真服务加载，前端 Mission 面板展示：

```json
{ "id": "shenzhen-facade-duo", "world": "urbanscene3d/shenzhen", "frame": "LOCAL_ENU", "seed": 7,
  "env": { "wind": { "speed": 6.0, "fromDeg": 135, "gust": 2.5, "profile": "power:0.25" },
           "fog": 0.0, "rainMmH": 0 },
  "agents": [
    { "id": "P600-01", "model": "p600", "home": [-230, 20, 0], "caps": ["rgb.zoom"],          "sensors": ["cam.survey"] },
    { "id": "P600-02", "model": "p600", "home": [-230, 40, 0], "caps": ["rgb.zoom", "lidar.mapping"], "sensors": ["cam.survey", "lidar.mid360"] } ],
  "tasks": [
    { "agent": "P600-01", "type": "helix_scan", "center": [-162.0, 98.5], "radius": 45, "z": [10, 195], "dzPerRev": 9.24, "speed": 4, "gimbal": "look_at_axis" },
    { "agent": "P600-02", "type": "helix_scan", "center": [-162.0, 98.5], "radius": 45, "z": [190, 391], "dzPerRev": 9.24, "speed": 4, "gimbal": "look_at_axis" } ],
  "transit": { "planner": "safe_transit", "margin": 5 },
  "events": [ { "t": 420, "type": "gust", "dv": 6.0, "duration": 20 },
              { "when": "battery < 0.25", "action": "RTL" } ],
  "success": { "facadeCoverage": 0.9, "minClearanceM": 10, "maxDurationS": 1800 } }
```

**六个剧本**（坐标为规范化后的 ENU 米值，来自 `analysis.json`、`missions.json`、`open_areas.json`）：

| # | 城市 · 剧本 | 目标与几何 | 智能体与流程 | 验证点 |
|---|---|---|---|---|
| S1 | **深圳 · 超高层双机立面巡检**（默认 Demo） | 塔 (−162, 98.5)，高 381 m，螺旋半径 45 m，Δz 9.24 m/圈，共 41 圈、11.7 km；按高度分成 0–195 m 和 190–391 m 两段 | 2 架 P600 同时起飞，安全转场后分段扫描；遇阵风时机体姿态偏移可视化；低电量 RTL | 立面覆盖率、最小间距 ≥10 m、Timeline 回放 |
| S2 | **上海 · 陆家嘴三塔编队环绕 + 世纪公园区域覆盖** | 三塔：Shanghai Tower (−2923, 810) 637 m、环球金融中心 (−2749, 954) 499 m、金茂 (−2887, 1038) 426 m，质心 (−2853, 934)<br>开阔区 (1158, −1094)，300 m 盒，开阔占比 0.89 | 5 机 V 形编队，绕三塔质心转一圈（半径 350 m，距三塔水平间距均大于 200 m），高度 250 m（环线上最高建筑 202 m，按 Height_map 查询后再加裕度），然后解散；3 机分条带覆盖开阔区（120 m AGL，s=41.6 m） | 大场景 LOD 流式加载（7.7 km）、编队切换动效、编队误差 |
| S3 | **纽约 · 港口搜救（ANet 能力协同）** | 搜索区：Upper Bay 开阔水域 (−64, −1283)，500 m 盒，全开阔<br>目标：3 个随机落水点 | A（RGB 广角）做扩展方形搜索（60 m 高，leg0 55 m）<br>检测到置信度 0.42 的疑似目标 → 通过 ANet 发布 "need thermal verification"<br>B（thermal）接单并改航，确认后置信度升到 0.9<br>C（relay）在 150 m 悬停做中继 | 01-design §32 的工作流、事件时间线、概率栅格热图 |
| S4 | **芝加哥 · 湖岸编队巡航 + Loop 覆盖** | 湖上走廊 x=+600，y 从 −3000 到 +3000（6 km；沿线最高障碍 101 m）<br>Loop 覆盖以 Willis (−1101, −584) 为中心，1.2 km 盒 | 5 机横队，150 m AGL 巡航，队形变为 V 形；随后 4 机按条带覆盖 Loop，逐段抬升越过 443 m 塔 | 调平是否生效（湖面高度应恒定）、覆盖航线的最小安全高度剖面 |
| S5 | **旧金山 · 丘陵地形跟随测绘** | 丘陵区 (−2000, −2500)，1 km 盒，地面起伏 8–200 m；避开 Sutro Tower (−2796, −2613)，高 258 m | 单机割草机航线，80 m AGL 地形跟随，限坡 15°；对照组为固定 MSL 高度，展示 AGL 从 −40 m 到 +150 m 的剖面 | DTM、HAG 着色、AGL 显示 |
| S6 | **苏州 · 带状走廊巡检 + 中继** | 走廊 y=0，x 从 −2100 到 +2100（4.2 km；沿线最高障碍 58.6 m），两侧 y=±200 | 2 机沿两侧 45° 斜视飞 80 m AGL；通信距离按 2 km mock，第 3 机在中点做中继；合成地面 z=0 | 无地面世界的处理、链路质量曲线 |

**搜索剧本的检测与贝叶斯更新（S3，可移植）：**
```
P_d(r) = P0 · exp(−(r/R_fp)^2) · LOS(p_uav, p_tgt; HeightMap) · vis(fog, rain)     # P0: RGB 0.8 / thermal 0.95
LOS: 沿射线每 2 m 采样，z_ray > hm.top(x, y) 才算可见（remove_points_inside 与 is_visible 的 2.5D 近似）
未检出时：p_c ← p_c(1 − P_d)/(1 − p_c·P_d)；检出时：conf ~ U(0.35, 0.6)，thermal 确认后 conf = 0.9
先验：开阔格（hag<1）均匀分布，水岸线 50 m 内 ×2
```

### 3.12 重建精度评测（移植 `EvaluateModel::evaluate`）

目标模块：`world/qa/recon_eval.py`，V0.5。

```python
def evaluate(recon, gt, thresholds=(0.005,0.01,0.02,0.03,0.05,0.1,0.2,0.5,1.0)):
    acc  = knn1(gt_tree, recon)          # 重建 → GT（Accuracy）
    comp = knn1(recon_tree, gt)          # GT → 重建（Completeness）
    return dict(chamfer=acc.mean()+comp.mean(),   # 与原实现一致（不除以 2），报表中另给 /2 版本
                hausdorff=max(acc.max(), comp.max()),
                acc_q=np.percentile(acc,[50,60,70,80,90,95]), comp_q=np.percentile(comp,[50,60,70,80,90,95]),
                acc_below={t:(acc<t).mean() for t in thresholds}, comp_below={t:(comp<t).mean() for t in thresholds})
```

- 用途：LingBot-Map 或 VGGT 的视觉点云与 MID-360 地图做对比；World Fusion 前后做回归。
- 本地可做无 GPU 的闭环：从内置城市合成"带噪声、带 1% 尺度误差的视觉点云"作为 recon，原点云作为 GT，验证 r07 的 Sim(3)-GICP 修正效果。
- 输出带误差着色的点云（原实现的 `if_write_error_file`），前端用 Height 渐变叠加红色阈值高亮展示。

---

## 4. 在本项目中的落点与复用方式

| 条目 | 用途 | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|
| 6 城采样点云 | 内置世界、性能基准、回归测试 | `data/worlds/urbanscene3d-*`（World Package） | V0.1 | adopt | 无 GPU 也能端到端演示 Reality→World→Web |
| Ingest 规范化（单位 / 上轴 / 调平 / 北向 / 原点 / 法线 / DTM / 分类） | 数据修复与 QA | `world/ingest/urbanscene3d.py`，通用部分放 `world/ingest/normalize.py` | V0.1 | 自研（本文 §3.3 实测） | 六城问题各不相同，不处理则 LOD、物理、风场全部出错 |
| 单位启发式 + 地标证据 | 自动初判 + 人工确认 | `world/ingest/units.py` + `qa.json` | V0.1 | 自研 | 旧金山是反例：只凭"看起来像米"会误判 |
| G=64 / LEAF=20000 八叉树、首屏层级 | Web 渐进加载 | `worldpkg tile`（r09） | V0.1 | port（r09） | 实测节点数 658–852，单节点 ≤32k |
| 多根森林 | 条带或超大场景 | `metadata.anet.roots` | V0.3 | 自研扩展 | 苏州实测前两层浪费 |
| 着色模式与 Graphite 渐变 | 无色点云的可读性 | `web/src/world/pointcloud/colorModes.ts` | V0.1 | 自研（参考 potree、r12） | 品牌色约束，红色只做高亮 |
| 分类字节（2 / 64 / 65 / 1 / 9） | 语义层、LiDAR 强度、搜索先验 | `col.w`（ANET_Q16） | V0.1 | 自研 | 零成本复用已有字节 |
| Height_map | mock 碰撞、安全高度、LOS | `sim/world/heightmap.py`，前端 `web/src/world/heightProfile.ts` | V0.2 | port（`model_tools.h`） | 约 40 行 numpy；2 m 栅格下各城 3–24 MB |
| safe_transit、2.5D A* | 任务连接段 | `sim/planning/transit.py` | V0.2 → V0.6 | 自研 | 重定向实验证明必须重规划 |
| 航线解析（UE → ENU）、TSP、time_param | 回放与模板 | `datasets/urbanscene3d/paths.py` | V0.2 | port | 43,654 个视点可直接用于回放测试 |
| 任务生成器（割草机 / 螺旋 / 环绕 / 5 向倾斜 / 扩展方形 / 走廊 / 地形跟随 / 编队） | Mission 模块 | `sim/mission/generators.py` | V0.2（单机）<br>V0.6（编队） | 自研（公式见 §3.10） | 城市上的主要任务来源 |
| 剧本 S1–S6 | Demo 与验收 | `scenarios/*.json` | V0.2 – V1.0 | 自研 | 覆盖单机、多机、ANet 协同 |
| 重建评测 | Fusion QA | `world/qa/recon_eval.py` | V0.5 | port（修 bug） | 业界通用指标，与官方口径对齐 |
| `split_obj` 单体化 | 建筑实例、语义 | `world/semantic/instances.py` | V1.0 | reference | 当前用高度栅格连通域已够用 |
| Embree 可见性、深度图 | 传感器仿真 | — | — | reference | 服务器侧已有 r04/r05 的 numpy 方案 |
| Simulator（UE4 + AirSim） | 高保真采图 | — | V1.0+ | skip | Windows / UE，体积大，与 Web 主栈无关 |

---

## 5. 对比与推荐

本单元只有一个仓库，因此对比集中在"资产之间"。

**5.1 六城作为内置世界的推荐排序：**

| 排名 | 城市 | 理由 | 主要用途 |
|---|---|---|---|
| 1 | **Shenzhen** | 紧凑（3.7 km²）；地面平（DTM 残差 0.03 m）；点距 0.53 m，平地城市中最密；381/304/300 m 高塔；米制，只需修正法线 | **默认 Demo**、首屏与帧率基线 |
| 2 | New York | 立面占 52%，城市峡谷；地标可辨识 | 低空穿行、LiDAR mock、搜救（港口开阔水面） |
| 3 | Shanghai | 最大（48 km²）、最稀疏（1.85 m）；陆家嘴三塔 | **LOD 流式加载压力测试**、编队 |
| 4 | Chicago | 需要 ×1000 加调平 2.03°；湖面开阔 | **Ingest 鲁棒性**回归、湖面编队与搜救 |
| 5 | San Francisco | 约 ×10.15 加旋转 90°；丘陵起伏 268 m | DTM、HAG、地形跟随 |
| 6 | Suzhou | Y-up、无地面、4.4 km 条带 | 走廊巡检、多根森林、合成地面 |

**5.2 航线方法的推荐排序（用作模板与回放）：**
1. **Zhang**：连续、5 m 采样、有序，重定向后不安全率仅 1.5–1.8%。适合轨迹回放和"真实规划轨迹"展示。
2. **Oblique.log**：参数化最简单，可直接写成生成器。
3. **Smith**：近景立面，俯角分布丰富（中位 26°），可作为近景视点分布参考；需要重规划。
4. **Zhou**：无序视点集，只当视点集使用，必须做 TSP 排序和安全连接。

**5.3 源码模块推荐排序：**
1. `Height_map`：立即移植。
2. `EvaluateModel::evaluate`：V0.5 移植并修正 bug。
3. `split_obj`、`sample_points`：参考。
4. 其余：跳过。

---

## 6. 风险与注意事项

1. **坐标体系陷阱，最高风险。**
   - 芝加哥不调平：同一"平地"上的高度误差达 ±160 m，风场、AGL、安全高度都会错。
   - 旧金山按米处理：尺度缩小 10 倍，Transamerica 只有 27 m 高，点大小和 LOD 全部失准。
   - 苏州不旋转：整城"侧躺"。
   - 对策：ingest 规范化后必须过 `qa.json` 检查，包括最高建筑高度在 [100, 700] m、地面平面残差、上向法线倾角、地标证据。任一项失败即拒绝发布。
2. **北向不确定**：深圳和苏州的北向未验证，`northConfidence: unknown`。UI 指南针和风向语义要标注"近似"，避免把"西风"误读为真实地理方向。
3. **法线问题**：深圳、上海约 17–24% 的法线朝下，另有零法线。不做 faceforward 或修正时，屋顶会成片发黑。苏州的面法线让 EDL 和光照呈现块面感，这属于风格而非错误。
4. **稀疏度**：上海、旧金山的点距约 1.9 m。近景（<50 m）观感稀疏，需要依靠点尺寸规则加 EDL 补足。虚拟 MID-360 在 40 m 量程内只能拿到很少的点（与 r08 一致）；近距离 LiDAR 仿真更适合深圳或苏州。
5. **无地面（苏州）**：开阔区检测、落点、阴影、碰撞都失效，必须合成地面，并在 `ground.type=synthetic` 中注明。
6. **航线文件陷阱**：
   - pitch 的符号语义（90 为下视）。
   - 存在 −90° 和 −48° 的异常视点。
   - 图像名格式不一（`0000.png` 与 `0000`）。
   - Zhang 起点有重复帧。
   - Zhou 无序。
   - 没有时间戳。
   - 位于 UE 左手系，**不能**与 PLY 共用变换。
   - 合成场景与城市不对应；只有 Proxy 几何才能让轨迹与建筑对齐，但目前被 Drive 配额挡住。
7. **下载可靠性**：本次 Google Drive 两次触发配额拒绝（Town/Oblique.log、School_proxy.zip）。应把数据镜像到本地或 NAS，记录 sha256，写入 `data/raw/urbanscene3d/MANIFEST.json`。
8. **C++ 代码不可直接用**：依赖重，CMake 缺依赖，偏 Windows，且有 4 处 bug（§2.3）。只移植算法。
9. **工具链**：本机 Open3D 0.20 需要 r06 提供的本地 libEGL（`LD_LIBRARY_PATH`）才能 import；单纯的 numpy 流水线不依赖它。规范化的中间数组共 880 MB，放在 `.cache`，不要提交进仓库。
10. **性能**：`analyze.py` 每城 90–160 s，主要耗在逐层体素统计和连通域迭代。生产版 ingest 只需规范化加 DTM 加分类，预计每城 15 s 以内，再加 r09 建树 5 s。连通域可改用 union-find 或 `scipy.ndimage.label`。
11. **分发**：数据条款禁止再分发。按任务约定忽略 license，但对外公开的 Demo URL 或公开仓库不应直接附带切片后的 World Package，改为首次启动时本地生成（r09 已建议）。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§41 World Package 缺少"来源坐标与质量"描述。** `coordinate.json` 应增加：
   - `source.unitsToMeters`、`source.upAxis`、`source.T_enu_src`（4×4，含调平和旋转）；
   - `approxTrueNorthYawDeg` + `northConfidence`；
   - `georef: null | {lat, lon, h}`；
   - `ground.type: dtm | flat | synthetic`。

   同时新增 `qa.json`，内容为地标证据、倾角、法线翻转率、nn_median、首屏层级。UrbanScene3D 的六城证明这些字段不可省略。

2. **§15 数据格式缺少强制的"Ingest 规范化"阶段。** 链路应改为：

   ```
   PLY / LAS / PCD
     → Ingest（单位 / 轴 / 手性 / 调平 / 原点 / 法线 / DTM / HAG / 分类）
     → Octree（ANET_Q16）
   ```

   还应明确"Web 端只接收局部 ENU 米制 Z-up 数据"。

3. **§8 Geometry World 应以"2.5D 高度图 + DTM/HAG"作为 V0.2 的第一碰撞与规划原语。** 它的成本远低于 Voxel/SDF，UrbanScene3D 团队也用它做无人机安全判定。Voxel/SDF 放到 V0.4 以后。

4. **§14 点云渲染**：点大小、LOD 阈值、首屏层级应由 metadata 中的 `spacing` 与 `nnMedian` 推导，不得硬编码（六城点距相差 5.4 倍，首屏层级为 L2–L4）。对细长或超大场景，应支持"多根森林"。

5. **§28 DroneState 缺少坐标约定。** 需要写明：
   - 世界系为 ENU、机体系为 FLU（REP-103）；`heading` 从北顺时针，`yaw_enu` 从东逆时针；
   - 增加 `agl` 与 `terrain_z` 字段；
   - 云台 pitch 的约定。UrbanScene3D 用"+ 向下，90 为下视"，UI 显示为仰角 −90°。
   - 与 PX4 NED/FRD 的换算公式（§3.5d）放进附录。

6. **§30 控制模式需要补充"任务原语"。** 包括 Lawnmower（重叠率驱动的间距公式）、HelixScan/FacadeScan、Orbit、Oblique5Grid、ExpandingSquare/SectorSearch、Corridor、TerrainFollow、Formation。另外，所有连接段必须经过 safe_transit 或规划器。实测表明，任何城市上固定 120–150 m 高度的覆盖航线都会撞到超高层。

7. **§43 MVP 链路过度依赖"P600 视频 → LingBot-Map"。** 本机无 GPU，这条链路无法验证。建议并行增加一条"内置世界链路"：

   ```
   UrbanScene3D → Ingest → Octree → Web → Mock Drone → WebSocket
   ```

   并把六城固化为**回归与性能基准**：紧凑平地、立面峡谷、超大稀疏、倾斜加千米、十米单位加丘陵、Y-up 加无地面。真实采集链路在 GPU 就绪后接入。

8. **§39 Timeline 没考虑无时间戳的数据源。** 需要写明 `time_param`（v、a、ψ̇ 上限，拍照驻留时间）和插值方式（min-jerk 或 Catmull-Rom，50 Hz 输出）。

9. **§38 UI 应补充以下元素：**
   - 世界信息卡：单位、坐标系、北向置信度、数据来源；
   - 近似指南针；
   - AGL 与 MSL 双读数；
   - 航线"最小安全高度剖面"图（用 lieflat 风格的面积图）；
   - 点云着色模式切换：Height / HAG / Normal / Class / Intensity，以及 EDL 开关。

10. **§42 仓库结构缺少以下目录：**
    - `datasets/`：各数据集适配器，如 `urbanscene3d/`；
    - `scenarios/`：任务剧本 JSON，对应 §3.11 的 S1–S6；
    - `world/qa/`：重建评测、ingest QA。

11. **§27 P600 数字孪生**：在 P600 相机标定前，默认"测绘相机配置"可直接采用 UrbanScene3D 的 HFOV 60°、6000×4000（VFOV 42.1°，fx=5196 px）。这样所有覆盖和重叠公式有统一输入，接入 P600 真机参数时只需替换配置文件。

12. **§32 Multi-Agent Workflow 可以落到具体剧本。** 纽约港口搜救（S3）就是"疑似目标 0.42 → thermal 确认"的最小可演示实现，检测、LOS、贝叶斯更新公式见 §3.11，建议列为 V1.0 验收用例。

---

### 附：复现命令

```bash
cd /data/projs/anet-drone
export LD_LIBRARY_PATH=$PWD/.cache/research/r06/root/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH  # 仅 Open3D 需要
.venv/bin/python .cache/research/x01/stats.py            # 原始统计 → stats.json
.venv/bin/python .cache/research/x01/analyze.py          # 规范化、DTM、密度、建筑、地标 → analysis.json + *.npy
.venv/bin/python .cache/research/x01/paths.py && .venv/bin/python .cache/research/x01/paths2.py   # 航线统计
.venv/bin/python .cache/research/x01/missions.py         # 任务生成器 + 安全检查 → missions.json
.venv/bin/python .cache/research/x01/retarget.py         # 航线重定向实验
```
