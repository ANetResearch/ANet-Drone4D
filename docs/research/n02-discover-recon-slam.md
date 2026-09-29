# N02 新项目发现：2025–2026 前馈三维重建 / SLAM / LiDAR-视觉融合 / 城市点云语义

> 研究单元：n02（Discovery）｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §5–§8、§14–§15、§33、§41、§43–§50
> 关联笔记：[r01 LingBot-Map + viser](./r01-lingbot-map-viser.md)、[r02 VGGT + COLMAP + Recon IR](./r02-vggt-colmap.md)、[r05 FAST-LIO2 / LIVO2 / R3LIVE](./r05-fastlio-livo-r3live.md)、[r07 GICP / 图优化](./r07-gicp-graph-slam.md)、[r09 PotreeConverter / PDAL](./r09-potreeconverter-pdal-lastools.md)。本文只补充这几篇没有覆盖的内容，坐标约定、Recon IR 目录沿用 r01/r02。
>
> **方法**
> - 检索：WebSearch 中英文共 20 余轮，覆盖 VGGT 后继、streaming 重建、前馈 SLAM、2026 LIO/LIVO、无人机航拍重建、城市点云语义。
> - 实测 GitHub 数据：star 取仓库页 `repo-stars-counter-star` 的 `title`；最近提交取 `https://github.com/<owner>/<repo>/commits.atom` 中第一条 entry 的 `<updated>`（默认分支）。数据保存在 `.cache/research/n02/stars.txt`，采集时间 2026-09-28。
> - 精读源码：7 个仓库 shallow clone 到 `refs/discovery/`，分别是 `map-anything`、`Depth-Anything-3`、`vggt-omega`、`rko_lio`、`Super-LIO`、`CSF`、`Utonia`。其中官方 `HengyiWang/amb3r-slam` clone 下来只有一个 `README.md`（占位仓库），已删除。
> - 本机实验（8 核 CPU、无 GPU，脚本和输出都在 `.cache/research/n02/`）：
>   1. **DA3-SMALL / DA3-BASE 纯 CPU 推理计时**（`n02_da3_cpu.py`）；
>   2. **RKO-LIO（PyPI wheel）+ MID-360 仿真扫描 + 200 Hz IMU 的无人机 mock 飞行**（`n02_rko_mock.py`）；
>   3. **CSF + 规则的城市语义分割**，在 UrbanScene3D 的 New York、San Francisco、Shenzhen 上跑了全量 5M 点（`n02_semantic.py`）。
>
> 路径一律相对各仓库根目录。估算值会标注"估算"，未验证的结论会标注"待验证"。

---

## 0. 结论速览

| 仓库 | ★ / 最后提交（实测） | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|---|
| **facebookresearch/map-anything** | 3771 / 2026-08-07 | 通用**度量**前馈重建，可选输入内参、位姿、深度（含稀疏深度）做条件。统一模型工厂封装了 VGGT、VGGT-Ω、π3X、DA3、MoGe、MUSt3R 等 | **adopt**：GPU Worker 中的"度量条件引擎"。**port**：统一输出字典、多视深度一致性置信度、IQR 自适应体素、位姿平移 log-scale 条件化 | V0.5（RTK/LiDAR 融合），V0.1 定接口 | ★★★★★ |
| **ByteDance-Seed/Depth-Anything-3** | 6402 / 2026-07-27 | any-view 几何基础模型，单一 depth-ray 表达。DA3-Streaming 支持长视频（chunk + Sim3 + 回环，显存 <12 GB）。Nested 系列输出米制。SMALL/BASE 为 Apache 许可，**可在 CPU 上跑** | **adopt**：离线长序列引擎（DA3-Streaming）和 CPU 冒烟引擎（DA3-SMALL）。**port**：置信度加权 IRLS-Sim3、chunk 调度、水库采样、参考帧选择 | V0.1（CPU 冒烟、离线引擎），V0.5 | ★★★★★ |
| **PRBonn/rko_lio** | 660 / 2026-09-24 | 不依赖传感器建模的 LIO。C++ 核心 + pybind，**`pip install` 即可在 CPU 上跑，不需要 ROS**，能直接读 ROS1/ROS2 bag | **adopt**：Python 后端离线 LIO Job，以及 mock-LIO 测试床。**port**：int8 体素块地图、重力方向正则 | V0.2（mock 测试床），V0.5（离线处理 P600 bag） | ★★★★★（已实测） |
| **jianboqi/CSF** | 649 / 2026-09-11 | 布料模拟地面滤波，可 `pip install cloth-simulation-filter`，纯 CPU | **adopt**：World ingest 的地面/DTM/HAG 与规则语义 | **V0.1 MVP** | ★★★★★（已实测） |
| **Liansheng-Wang/Super-LIO** | 602 / 2026-07-13 | RA-L 2026 LIO。ROS2 Humble/Jazzy（另有 ROS1 分支），支持 x86 和 ARM64，自带 MID-360 配置，OctVoxMap 紧凑地图 | **adopt**：机载（Orin NX）LIO，作为 FAST-LIO2 的候选替代。**port**：OctVoxMap（实时地图去重和有界内存） | V0.5 真机 | ★★★★ |
| **facebookresearch/vggt-omega** | 4588 / 2026-09-22 | VGGT 官方后继（CVPR 2026 Oral）。register attention，1B 参数，权重需申请 | **reference**：通过 MapAnything 的 `vggt_omega` wrapper 接入，做对照 | V0.5 评测 | ★★★ |
| **yyfz/Pi3（Pi3X）** | 2181 / 2026-05-18 | 置换等变、无参考帧；Pi3X 支持位姿/内参/深度条件和近似度量 | **reference**：由 MapAnything 的 `pi3x` wrapper 统一调用。OpenFlyScan 的无人机工作站也用它 | V0.5 评测 | ★★★★ |
| **MIT-SPARK/VGGT-SLAM（2.0）** | 1133 / 2026-06-29 | 实时前馈稠密 SLAM（RSS 2026），在 Jetson Thor 上验证。集成 SAM 3 + Perception Encoder 做开放词汇 3D 检测 | **reference**：V1.0 的目标发现和开放词汇查询 | V1.0 | ★★★ |
| zju3dv/Scal3R、HengyiWang/amb3r（+amb3r-slam）、DengKaiCQ/VGGT-Long | 538 / 501（41）/ 903 | 公里级前馈重建与 SLAM | **reference**：amb3r-slam 官方代码未发布；VGGT-Long 的工程改进已并入 DA3-Streaming | V0.5+ 观察 | ★★ |
| CUT3R / TTT3R / StreamVGGT / STream3R / InfiniteVGGT | 1497 / 734 / 975 / 409 / 391 | 状态式、因果流式重建 | **skip**：LingBot-Map（★17141）在同一定位上更强、更活跃 | — | ★ |
| OpenDroneMap/ODM（+WebODM） | 6494 / 2026-09-15 | 成熟的无人机摄影测量（正射、DSM、点云、网格） | **reference/adopt**：离线基准，以及 DJI SRT 遥测解析 | V0.5 QA | ★★★ |
| Pointcept/Pointcept + Utonia | 3237 / 751 | PTv3 系列点云编码器；Utonia 可以不用颜色和法线（ICML 2026） | **reference**：V0.5 以后在 GPU 上做线性探针语义（需 STPLS3D/SensatUrban 标注） | V0.5+ | ★★★ |
| IGNF/myria3d、meidachen/STPLS3D、facebookresearch/sam3 | 294 / 290 / 11818 | 航空 LiDAR 语义产线、合成航测语义数据集、2D 开放词汇分割 | **reference** | V0.5–V1.0 | ★★★ |
| hku-mars：Point-LIO / Voxel-SLAM / Swarm-LIO2 / MARSIM / UMI-3D；APRIL-ZJU/Gaussian-LIC；rpng/MINS；mistletoe235/OpenFlyScan | 见 §1 | LIO 变体、集群 LIO、LiDAR 无人机仿真、MID-360+相机标定、LIC-3DGS、GNSS 多传感器、质量引导补拍 | **reference** | V0.5–V1.0 | ★★–★★★ |

**关键结论（实现者先读这 10 条）：**

1. **2026 年前馈重建形成"四强一基线"。** 四强是 LingBot-Map（★17141，流式，r01 已选）、Depth-Anything-3（★6402，any-view 且有小模型）、VGGT-Ω（★4588，VGGT 官方后继）和 MapAnything（★3771，度量加多模态条件）；基线是 COLMAP 4.x。**不应再只绑定单一引擎。** 建议把 r02 的 Recon IR 升级为 **Engine Adapter v2**（§3.1）：LingBot-Map 负责在线/流式，DA3-Streaming 负责离线长序列和回环，MapAnything 负责"带 RTK 位姿和 LiDAR 稀疏深度的度量重建"，DA3-SMALL 负责 CPU 冒烟和 CI。
2. **MapAnything 在模型层面解决了 r01/r02 指出的"前馈重建本来就没有尺度"。** 视图字典里放入 `camera_poses`（OpenCV c2w）、`is_metric_scale=True`，以及可选的 `depth_z`（稀疏深度用 0 表示无效），模型就直接输出米制几何和 `metric_scaling_factor`（`mapanything/models/mapanything/model.py::_encode_and_fuse_cam_quats_and_trans / _encode_and_fuse_depths`）。训练配置 `configs/model/task/depth_completion.yaml` 中 `sparse_depth_prob: 1`、`sparsification_removal_percent: 0.9`，说明它见过只保留 10% 像素的稀疏深度。MID-360 投影深度能否直接使用：**待验证**。在度量输出前提下，chunk 之间应改用 **SE(3) 对齐而不是 Sim(3)**（VGGT-Long 2025-10-08 更新说明和 Map-Long 的做法）。
3. **长序列离线重建建议用 DA3-Streaming 作第二引擎。** 它是 VGGT-Long 的工程升级：`chunk_size=120`、`overlap=60`，置信度加权 IRLS-Sim3（Huber δ=0.1），SALAD 回环（相似度 ≥0.85），pypose Sim3 LM 位姿图。官方 KITTI ATE 为 **16.83 m**（chunk 60），VGGT-Long 25.60 m，Pi-Long 21.17 m；r01 记录的 LingBot-Map KITTI ATE 为 24.0 m（各自评测协议不同，只作量级参考）。该流程在 GPU 上接近 10 FPS。
4. **无 GPU 也能跑"真实"重建冒烟测试（本机实测）。** DA3-SMALL（Apache，实测 34.3M 参数）在 8 核 CPU 上：8 帧 504×280 耗时 **37.5 s**，16 帧 88 s，峰值 RSS 1.9 GB；336 分辨率下 16 帧 51.5 s。DA3-BASE（135.4M）16 帧 504 分辨率 173 s。这足够做 CI 和演示用的"8–16 张关键帧 → 点云 → World Package"真实链路，不必全靠 mock。
5. **DA3 的规范系有两个坑。**
   - 视图数 ≥3 时（`utils/constants.py::THRESH_FOR_REF_SELECTION = 3`），默认 `ref_view_strategy="saddle_balanced"` 会按特征挑一个参考视图作为世界系，输出顺序虽然会恢复，**世界系却不是第 0 帧**。本机实测第 0 帧外参不是单位阵（旋转约 3.5°，平移 0.03）。
   - 输出 `extrinsics` 是 **W2C**，VGGT-Ω 同样是 W2C（`vggt_omega/utils/pose_enc.py` 的注释写的是 "camera-from-world"）；MapAnything、π3、LingBot-Map 则是 **C2W**。
   
   Adapter 必须统一做"方向归一 + 规范到第 0 帧"（§3.1）。
6. **LIO：RKO-LIO 是后端离线 LIO 的首选。** 它 `pip install` 即可在 CPU 上运行，不需要 ROS，直接读 bag。本机 mock 飞行结果（Shenzhen，20k 点/扫描，10 Hz，200 Hz IMU 带噪声和偏置，最大倾角 25°，最高速 11.6 m/s）：配准耗时 **p50 8.8–11.3 ms / p95 18.5–25 ms**；轨迹 **424 m 时 ATE RMSE 0.12 m，1410 m 时 0.39 m**（SE3 对齐后）。机载端用 FAST-LIO2（Prometheus 已内置）或 **Super-LIO**（ROS2、ARM64、MID-360 配置齐全）。FAST_LIO 主仓最后提交在 2024-07，已经停更。
7. **MVP 语义层只能走"几何规则"路线，本机实测可行。** 流程是 CSF 地面 → DTM → HAG → 网格法线 + 法线一致性 → 类别，再平滑。New York 5M 点全流程 **112 s**（CSF 在 3 m 布料下 50 s，2 m 布料要 159 s，地面比例几乎一样）。结果：地面 35.6%、屋顶 14.2%、立面 47.6%、低矮物 2.2%。学习型模型（PTv3/Utonia/Myria3D）依赖 spconv、flash-attn 和 GPU，并且需要航测标注做线性探针，放到 V0.5 以后。
8. **UrbanScene3D 内置数据的三个新发现（补充 r03/r09）：**
   - ① 三座虚拟城市**几乎没有植被**（v2 规则判为植被的比例：NY 0.03%、SF 0.12%、Shenzhen 0.01%），MVP 的 "Tree" 图层没有真实数据可展示；
   - ② San Francisco 建筑 HAG 最大只有 26.2 m、p99 14.0 m，而原型城市最高楼约 326 m，**疑似按 1:10–1:12 缩放**（r03 把它当作米制，**待核实**）；
   - ③ Shenzhen 城区街道低于外围大平面约 8 m，景观坡地被 CSF 判为非地面（§3.10 给出 v3 规则修正）。
9. **对 MVP 流畅性的直接贡献：**
   - (a) 每点 1 B 的 `classification`（ASPRS 编码）加上 GPU 端 class-mask uniform，图层开关不用重新加载；
   - (b) DTM 栅格同时服务 AGL 高度显示、mock 无人机贴地飞行和 WindNinja 地形风（§3.10）；
   - (c) "实时建图"演示用 OctVoxMap 去重，WebSocket 按 int8 体素块下发增量（约 3 B/点，§3.7–3.8）；
   - (d) 流式预览用水库采样维持固定点预算（§3.4）。
10. **风险。** VGGT-Ω 权重需人工申请；MapAnything、DA3-Giant、π3 权重是 CC-BY-NC（按要求忽略许可，但 `engine.json` 要记下来）；flash-attn、spconv、xformers、gsplat 都依赖 CUDA；AMB3R-SLAM 官方代码尚未发布，GitHub 上唯一可跑的是 0★ 的第三方复现。

---

## 1. 仓库概览

### 1.1 已有 refs 与本单元候选对照

`refs/` 已有的同类仓库：`recon/{vggt, lingbot-map, colmap, gaussian-splatting, gsplat, nerfstudio, viser}`、`lidar/{FAST_LIO, FAST-LIVO2, FASTLIO2_ROS2, glim, small_gicp, fast_gicp, hdl_graph_slam, faster_lio_localization, LiDAR_IMU_Init, r3live, Open3D, Livox-SDK2, livox_ros_driver2}`、`world/{PotreeConverter, PDAL, LAStools, 3d-tiles, ...}`。其中**没有**度量条件前馈模型、长序列回环前馈系统、ROS-free 的 Python LIO，也没有任何点云语义相关的仓库。

| 类别 | 已有 | 本单元新增/建议 | 关系 |
|---|---|---|---|
| 前馈重建 | vggt（★14436，2026-05-19）、lingbot-map（★17141，2026-09-08） | map-anything、Depth-Anything-3、vggt-omega | **补充**：LingBot-Map 仍是流式主引擎；vggt 保留，因为它是 LingBot 的祖先，接口要对照 |
| 长序列 / SLAM | —（r01 提到 LingBot windowed 模式） | DA3-Streaming（在 DA3 仓库内）、VGGT-SLAM 2.0、Scal3R | **补充** |
| LIO | FAST_LIO（★5226，**2024-07-23** 停更）、FASTLIO2_ROS2、glim（★1848，2026-09-06） | rko_lio、Super-LIO | **补充**：Python 后端用 rko_lio；机载侧 Super-LIO 可替代 FAST_LIO 主仓 |
| 航拍摄影测量 | colmap | ODM/WebODM（reference） | 参考 |
| 地面 / DTM / 语义 | PDAL（有 SMRF/PMF 地面滤波） | CSF、Pointcept/Utonia、myria3d、STPLS3D | **新增**：MVP 用 CSF；PDAL 的 `filters.csf` 可在 ingest 管道里二选一 |

### 1.2 评估过的全部候选（实测 star / 最近提交）

| 仓库 | ★ | 最近提交 | 年份 / 会议 | 一句话 |
|---|---|---|---|---|
| Robbyant/lingbot-map | 17141 | 2026-09-08 | 2026 | 流式 GCT + paged KV（r01 已研究） |
| facebookresearch/vggt | 14436 | 2026-05-19 | CVPR25 Best | 前馈多视几何（r02 已研究） |
| facebookresearch/sam3 | 11818 | 2026-09-18 | 2025-11 | 概念提示的 2D/视频分割，VGGT-SLAM 2.0 用它做开放词汇 3D 检测 |
| OpenDroneMap/ODM | 6494 | 2026-09-15 | 3.6.2（2026-08） | 无人机摄影测量全流程 |
| ByteDance-Seed/Depth-Anything-3 | 6402 | 2026-07-27 | 2025-11 | any-view 深度 + 射线 + 位姿；DA3-Streaming |
| hku-mars/FAST-LIVO2 | 4689 | 2026-03-08 | T-RO | r05 已研究 |
| facebookresearch/vggt-omega | 4588 | 2026-09-22 | CVPR26 Oral | VGGT 后继，训练代码 2026-09-08 开源 |
| OpenDroneMap/WebODM | 4189 | 2026-09-25 | — | ODM 的 Web UI |
| facebookresearch/map-anything | 3771 | 2026-08-07 | 3DV26，v1.1.4 | 度量前馈重建框架，含外部模型工厂 |
| Pointcept/Pointcept | 3237 | 2026-09-11 | v1.7 | PTv3/Sonata/Concerto/Utonia 训练框架 |
| rmurai0610/MASt3R-SLAM | 3193 | 2025-11-09 | CVPR25 | 以 MASt3R 为先验的稠密 SLAM，需要 CUDA |
| microsoft/MoGe | 2984 | 2026-08-19 | — | 单目几何（MoGe-2 度量），MapAnything 可调用 |
| Tencent-Hunyuan/HY-World-2.0 | 2676 | 2026-08-12 | 2026-04 | 内含 WorldMirror-2.0 |
| PRBonn/kiss-icp | 2330 | 2026-05-04 | — | rko_lio 的 ICP 思想来源 |
| yyfz/Pi3 | 2181 | 2026-05-18 | ICLR26 | π3 / Pi3X（2025-12-28） |
| Pointcept/PointTransformerV3 | 1959 | 2025-10-24 | CVPR24 | PTv3 主干 |
| koide3/glim | 1848 | 2026-09-06 | — | 已有 |
| CUT3R/CUT3R | 1497 | 2025-08-27 | CVPR25 | 持续状态的流式重建 |
| PKU-VCL-3DV/SLAM3R | 1369 | 2025-10-18 | CVPR25 | 前馈稠密 SLAM |
| hku-mars/Point-LIO | 1338 | 2026-06-13 | — | 逐点 LIO，适合高动态 |
| DekuLiuTesla/CityGaussian | 1263 | 2026-08-16 | — | 城市级 3DGS |
| Tencent-Hunyuan/HunyuanWorld-Mirror | 1210 | 2026-05-27 | ICML26 | 带先验条件的前馈重建 |
| MIT-SPARK/VGGT-SLAM | 1133 | 2026-06-29 | RSS26 | 2.0 版实时版本 |
| hku-mars/SUPER | 1048 | 2025-06-04 | Sci. Robotics | MID-360 高速安全导航 |
| wzzheng/StreamVGGT | 975 | 2025-10-22 | ICLR26 | 因果流式 VGGT |
| DengKaiCQ/VGGT-Long | 903 | 2026-02-09 | ICRA26 | chunk + loop + align |
| mystorm16/FastVGGT | 816 | 2026-01-28 | — | token merging 加速 |
| rpng/MINS | 784 | 2026-09-26 | — | IMU/相机/LiDAR/GNSS/轮速滤波融合 |
| Pointcept/Utonia | 751 | 2026-07-01 | ICML26 | 跨域统一点云编码器 |
| Inception3D/TTT3R | 734 | 2026-05-11 | ICLR26 | CUT3R 的长度泛化 |
| hku-mars/Voxel-SLAM | 687 | 2025-04-07 | — | 多会话 LiDAR-惯性 SLAM |
| PRBonn/rko_lio | 660 | 2026-09-24 | RA-L26 | 0.4.1（PyPI 最新为 0.4.0） |
| jianboqi/CSF | 649 | 2026-09-11 | — | 布料模拟地面滤波 |
| APRIL-ZJU/Gaussian-LIC | 648 | 2026-08-08 | ICRA25/IJRR26 | LiDAR-惯性-相机 3DGS SLAM |
| Liansheng-Wang/Super-LIO | 602 | 2026-07-13 | RA-L26 | OctVoxMap LIO |
| hku-mars/MARSIM | 583 | 2025-10-25 | — | LiDAR 无人机点云仿真器（含 MID-360） |
| zju3dv/Scal3R | 538 | 2026-09-15 | CVPR26 Highlight | 大规模 test-time training |
| HengyiWang/amb3r | 501 | 2026-06-05 | CVPR26 Highlight | 度量前馈 + 后端；AMB3R-VO/SfM |
| hku-mars/Swarm-LIO2 | 455 | 2026-01-13 | T-RO25 | 去中心化集群 LIO |
| AutoLab-SAI-SJTU/InfiniteVGGT | 391 | 2026-04-19 | 2026 | 滚动记忆的无限流式 |
| IGNF/myria3d | 294 | 2026-09-17 | — | 法国 Lidar HD 航空点云语义产线 |
| meidachen/STPLS3D | 290 | 2025-12-31 | BMVC22 | 合成 + 真实航测点云语义数据集 |
| hku-mars/UMI-3D | 278 | 2026-07-21 | — | MID-360 + 鱼眼相机 SLAM 与外参 |
| HengyiWang/amb3r-slam | 41 | 2026-09-17 | arXiv 2609.19518 | **官方仓库只有 README（占位）** |
| mistletoe235/OpenFlyScan | 2 | 2026-09-27 | arXiv 2609.24253 | 质量引导补拍；工作站用 Pi3X |
| johnhenning/amb3r-slam | 0 | 2026-09-25 | — | AMB3R-SLAM 的第三方复现（基于 DA3），非官方 |

没有公开代码或未找到仓库：GeoFF3D（arXiv 2608.28288，坐标锚定的 UAV 前馈建图，9 个航测块上 F@5 从 0.829 提到 0.877）、FAST-LIVGO（IROS 2026，LIVO+GNSS）、FFVO。这几项只作论文参考。

---

## 2. 源码结构与关键模块

### 2.1 MapAnything（`refs/discovery/map-anything` @3d10cf7，v1.1.4）

```text
mapanything/models/__init__.py            model_factory(model_str) / init_model_from_config / init_model  ← 统一模型工厂
mapanything/models/mapanything/model.py   MapAnything（2189 行）
  ├─ _encode_n_views                      DINOv2 图像编码
  ├─ _encode_and_fuse_ray_dirs            内参 → 射线方向编码
  ├─ _encode_and_fuse_depths              深度条件（先按非零像素归一化，得到 log 尺度因子）
  ├─ _compute_pose_quats_and_trans_for_across_views_in_ref_view  把所有位姿变换到 view0 坐标系
  ├─ _encode_and_fuse_cam_quats_and_trans 旋转 / 平移 / 平移尺度（log）三个编码器相加
  ├─ forward / infer                      多视 transformer + scale_token → scale_head → metric_scaling_factor
  ├─ downstream_head                      DPT 稠密头 + 位姿头；memory_efficient_inference 按 minibatch 跑稠密头
  └─ _compute_adaptive_minibatch_size     按剩余显存自适应 minibatch
mapanything/models/external/{vggt, vggt_omega, pi3, pi3x, da3, moge, must3r, mast3r, dust3r, pow3r, anycalib}
                                          ← 外部模型 wrapper，全部输出统一字典
mapanything/utils/multiview_confidence.py compute_multiview_depth_confidence（几何一致性置信度）
mapanything/utils/colmap_export.py        voxel_downsample_point_cloud（IQR 自适应体素）/ export_predictions_to_colmap
mapanything/utils/geometry.py             normalize_pose_translations / depthmap_to_camera_frame / closed_form_pose_inverse
configs/model/task/*.yaml                 各任务的几何条件采样概率（posed_sfm / depth_completion / registration ...）
scripts/demo_inference_on_colmap_outputs.py  以 COLMAP 结果为条件做推理
```

**统一输出字典**（每个 view 一份）：`pts3d`（世界系）、`pts3d_cam`、`depth_z`、`depth_along_ray`、`ray_directions`、`intrinsics`、`camera_poses`（**OpenCV c2w**，4×4）、`cam_trans`、`cam_quats`（**xyzw**；代码中单位四元数写作 `[0,0,0,1]  # (q_x, q_y, q_z, q_w)`）、`conf`、`mask`、`non_ambiguous_mask`、`metric_scaling_factor`、`img_no_norm`。

**条件输入的约束**（见 `infer` docstring）：
- 有 `depth_z` 就必须同时给 `intrinsics` 或 `ray_directions`；
- 只要有一个 view 带 `camera_poses`，view0 也必须带；
- `intrinsics` 与 `ray_directions` 只能给一个；
- 可以用 `ignore_{calibration,depth,pose,depth_scale,pose_scale}_inputs` 做消融。

**位姿条件的实现细节**（`_encode_and_fuse_cam_quats_and_trans`）：
1. 所有位姿先变换到 view0 坐标系；
2. 平移除以"非零平移的平均模长" `normalize_pose_translations` 得到单位尺度；
3. `log(norm_factor)` 单独经过 `cam_trans_scale_encoder`，**只有 `is_metric_scale=True` 的样本才会注入**；
4. 三组特征直接加到每个 view 的 patch 特征上。

也就是说，模型是在"知道米制尺度数值"的前提下预测几何，而不是事后缩放。这正是它能直接吃 RTK 位姿的原因。

**深度条件**（`_encode_and_fuse_depths`）：`depth>0` 视为有效，用 `normalize_depth_using_non_zero_pixels` 归一化。训练时 `depth_completion` 任务把稠密深度随机删掉 90%（`sparsification_removal_percent: 0.9`），因此能接受**随机稀疏**深度。MID-360 的玫瑰花瓣式扫描投影后的分布与此不同，适配效果**待验证**。

**CHANGELOG 1.1.0（2026-01-18）** 引入了模型工厂和外部模型、AerialMegaDepth 数据集接入（对航拍视角有利）、`memory_efficient_inference`（140 GB 显存可处理 2000 视图）。权重有两份：`facebook/map-anything`（CC-BY-NC）和 `facebook/map-anything-apache`。

### 2.2 Depth-Anything-3（`refs/discovery/Depth-Anything-3` @3d835ec）

```text
src/depth_anything_3/api.py              DepthAnything3(PyTorchModelHubMixin).inference(...)
  ├─ _normalize_extrinsics               输入外参以第 0 帧为原点，平移除以中位距离（clamp ≥0.1）
  ├─ _align_to_input_extrinsics_intrinsics  align_poses_umeyama（≥10 视图启用 RANSAC），把预测对齐回输入位姿尺度
  └─ export(...)                         mini_npz / npz / glb / ply / colmap / gs_ply / gs_video / depth_vis / feat_vis
src/depth_anything_3/model/da3.py        DepthAnything3Net：DinoV2 主干 + DualDPT（depth + ray）+ CameraDec / CameraEnc + GS 头
src/depth_anything_3/model/cam_enc.py    CameraEnc：c2w + 内参 → 9 维 pose encoding → MLP + 4 层 Block → 相机 token（位姿条件）
src/depth_anything_3/model/dinov2/vision_transformer.py   视图数 ≥3 时 select_reference_view → reorder → … → restore_original_order
src/depth_anything_3/model/reference_view_selector.py     first / middle / saddle_balanced / saddle_sim_range
src/depth_anything_3/utils/alignment.py  least_squares_scale_scalar / compute_sky_mask / apply_metric_scaling
src/depth_anything_3/configs/*.yaml      da3-{small,base,large,giant} / da3metric-large / da3mono-large / da3nested-giant-large
da3_streaming/da3_streaming.py           DA3_Streaming：get_chunk_indices / process_single_chunk / align_2pcds / process_long_sequence
da3_streaming/loop_utils/sim3utils.py    weighted_estimate_sim3 / robust_weighted_estimate_sim3（IRLS-Huber）/ accumulate_sim3_transforms /
                                         optimized_vectorized_reservoir_sampling / compute_scale_ransac|weighted
da3_streaming/loop_utils/loop_detector.py  LoopDetector：SALAD（DINOv2）全局描述子，336×336，top-k + NMS
da3_streaming/loop_utils/sim3loop.py     Sim3LoopOptimizer：pypose Sim3，顺序边 + 回环边，LM（max_iter 30，λ0 1e-6）
da3_streaming/configs/base_config.yaml   chunk 120 / overlap 60 / align_lib triton|torch|numba|numpy / IRLS δ=0.1 iters=5
```

- **模型谱系**：Main（any-view）、Metric（`metric_depth = focal * out / 300`）、Mono、Nested（any-view 加度量，直接输出米）。README 建议优先使用带 `-1.1` 后缀的新权重，街景效果明显更好。SMALL、BASE、METRIC-LARGE、MONO-LARGE 是 Apache 2.0；LARGE、GIANT、NESTED 是 CC-BY-NC。
- **API 输出**：`depth [N,H,W]`、`conf`、`extrinsics [N,3,4]`（**opencv w2c**，README 原文 "opencv w2c or colmap format"）、`intrinsics`、`processed_images`。`use_ray_pose=True` 时从射线头推位姿，更准但更慢（ETH3D AUC3 从 48.4 提到 52.6）。
- **xformers 可选**：`model/dinov2/layers/swiglu_ffn.py` 在 import 失败时回退到纯 PyTorch 的 SwiGLU。本机正是用这条路径在 CPU 上跑通（§附录 A）。

### 2.3 VGGT-Ω（`refs/discovery/vggt-omega` @48b23c8）

- `vggt_omega/models/aggregator.py::Aggregator`：
  - depth=24，embed 1024，patch 16，每帧 1 个 camera token 加 16 个 register token；
  - `register_attention_block_indices=[2,6,9,14,20]` 这 5 层的跨帧注意力**只在 camera 和 register token 之间做**（每帧 17 个 token），其余 19 层是全 token 全局注意力；
  - 与 VGGT 相比省掉了约 1/5 的二次项开销。
- `vggt_omega/utils/pose_enc.py`：9 维编码（T、四元数、fov_h、fov_w）。外参是 **camera-from-world（W2C）**，主点固定在图像中心。
- `heads/text_alignment_head.py`：256 分辨率的 text-aligned checkpoint 可以输出 `text_alignment_embedding`，register 能和语言对齐。V1.0 可以用它按语言检索视图。
- 显存（README 实测，A100，624×416）：1 帧 6.02 GB，50 帧 9.66 GB，100 帧 13.37 GB，300 帧 28.26 GB，500 帧 43.15 GB。**约 0.074 GB/帧 + 6 GB 常数**。
- 许可和获取：代码是 FAIR Noncommercial Research License；权重在 HF 上需申请，由自动流程审批。2026-09-08 发布了训练代码和复现用 checkpoint（416 分辨率）。

### 2.4 RKO-LIO（`refs/discovery/rko_lio` @b2eec8d，0.4.1）

```text
rko_lio/core/lio.hpp / lio.cpp        LIO::Config{deskew, max_iterations=100, voxel_size=1.0, max_range=100, min_range=1.0,
                                       convergence_criterion=1e-5, max_correspondence_distance=0.5, max_expected_jerk=3,
                                       double_downsample=true, min_beta=200, initialization_phase}
  ├─ add_imu_measurement              IMU 转到 base 系（带杆臂角加速度补偿），积分 imu_state，累计 IntervalStats（Welford）
  ├─ register_scan                    时间戳处理 → deskew → preprocess（0.5×/1.5× 体素双降采样）→ icp → map.update
  ├─ step_body_accel_filter           机体加速度 KF：过程噪声 = (jerk·dt)²/3，观测噪声 = 加速度模方差/3
  ├─ build_orientation_linear_system  残差 = R⁻¹(−g) − 局部重力估计；J = [0 | hat(pred_g)]
  └─ icp                              H = H_icp + H_ori/β，β = min_beta·(1 + accel_var)；LDLT 求解；Ceres 风格收敛判据
rko_lio/core/voxel_hash_map.hpp/.cpp  VoxelBlock：每体素最多 8 点，存为相对体素中心的 **int8 偏移**（量化 = voxel/254 左右）
                                      remove_points_far_from_location（按 max_range 裁剪局部地图）
rko_lio/core/deskew.cpp               不做逐点 SE3 exp 的去畸变（#172）
rko_lio/python/rko_lio/lio.py         LIO.add_imu_measurement(acc, gyro, t_ns, T_imu2base) / register_scan(pts, ts_ns, T_lidar2base)
                                      / poses_with_timestamps() → (ns[], [N,7] = xyz + qxyzw) / map_point_cloud()
rko_lio/python/rko_lio/dataloaders/   rosbag.py（rosbags，ROS1/ROS2 通吃）/ raw.py（点云文件夹 + imu.csv）
rko_lio/ros/                          online_node / offline_node / threaded_node（Humble、Jazzy、Kilted、Lyrical、Rolling）
```

特点是**不做传感器专用建模**：不区分 Livox 或机械式雷达，只要每点有时间戳。IMU 只用于去畸变、运动先验和重力方向正则。这对"真机 P600 与仿真 LiDAR 共用一套算法"非常友好。CHANGELOG 0.4.0（2026-09-01）的改动包括：致命配准错误后自动 reset 并发布计数；LiDAR 间隙时外推运动先验；IMU 负 dt 丢弃。

### 2.5 Super-LIO（`refs/discovery/Super-LIO` @f89f48d，ROS2 分支）

```text
src/super_lio/include/OctVoxMap/OctVoxMap.hpp   OctVox<Point>：8 个子体素（八分体），每个只存 1 个滑动均值点
                                                 （≤20 次累加，与现值距离 >0.1 m 的新点直接丢弃）
                                                 OctVoxMap::insert：fine_key = floor(p / (res/2))；key = fine_key >> 1；
                                                 local_idx = (dz<<2)|(dy<<1)|dx
                                                 用 robin_map + std::list 实现 LRU，超过 capacity 淘汰最久未访问的体素
                                                 getTopK：60 个邻域体素按最小距离分 6 组（HKNN_list60_gem.h 的 orders_min_dis2），
                                                 5-NN 满且 max_d² < 下一组最小距离时提前退出
src/super_lio/src/lio/super_lio.cpp             calc_plane_coeff（4–5 点 QR 拟合，平面残差 ≤0.1 m）/ compute_error（length > 81·e²）
                                                 Observe：TBB 并行点到平面，J = [p_b × n_b, n]，权重 1000
src/super_lio/src/lio/ESKF.cpp                  迭代 ESKF（kf_max_iterations 4，重力对齐）
src/super_lio/src/lio/super_lio_reloc.cpp + apps/relocation_node.cpp  基于已存地图的重定位
src/basic/include/basic/buffer/{LatestOnlyBuffer, RingBuffer, MultiSourceLatestBuffer}.hpp  多源最新值缓冲
src/super_lio/config/livox_360.yaml             blind 2.0 / maxrange 60 / voxel_fliter_size 0.5 / vox_resolution 0.5 / hash_capacity 2e6
```

构建环境：C++20、ROS2 Humble/Jazzy、Eigen、PCL、glog、TBB；x86 与 ARM64 都支持（适配 Orin NX）。2026-06-07 的提交修复了已知错误并提升了精度。

### 2.6 CSF（`refs/discovery/CSF` @d5952da）

- 布料模拟：`src/CSF.cpp::CSF::do_cloth`。
  - 点云先内部翻转为 `(x, −z, y)`，即"倒过来的地形"。
  - 在包围盒上方 0.05 处铺一张分辨率为 `cloth_resolution` 的布，四周各扩 2 格。
  - `Rasterization::RasterTerrian` 把每个布点下方最近点的高度作为碰撞高度。
  - 施加重力 0.2·dt²（dt = `time_step` 0.65），迭代 `timeStep`（Verlet 加 `satisfyConstraintSelf(rigidness)`）和 `terrCollision`，直到 `maxDiff < 0.005` 或达到 500 次。
  - 可选 `movableFilter` 做坡度后处理（`bSloopSmooth`）。
- 分类：`src/c2cdist.cpp::calCloud2CloudDist` 对布高做**双线性插值**，点到布的距离 < `class_threshold` 判为地面。
- Python 接口（SWIG，PyPI 包名 `cloth-simulation-filter`）：`CSF.CSF()`、`params.*`、`setPointCloud(np)`、`do_filtering(VecInt ground, VecInt non_ground, exportCloth)`、`do_cloth_export()`。
- PDAL 也内置了 `filters.csf`，与 r09 的 ingest 管道天然兼容。

### 2.7 Utonia / Pointcept（`refs/discovery/Utonia` @da776a0）

- `utonia/model.py::PointTransformerV3`：只有编码器。`enc_depths=(3,3,3,12,3)`、`enc_channels=(48,96,192,384,512)`、`enc_patch_size=1024`，序列化顺序为 z-order 和 z-trans（`utonia/serialization/`）。硬依赖 `spconv`（SubMConv3d）；`flash_attn` 可选，`enable_flash=False` 时走普通注意力，demo 里 CPU 设备也能跑（很慢）。
- `utonia/transform.py::default(scale, apply_z_positive, normalize_coord)`：先 `RandomScale(scale)`，再 `GridSample(grid_size=0.01)`，**等效网格 = 0.01/scale 米**。户外 demo 用 `scale=0.2`，即 5 cm。
- `demo/2_sem_seg.py`：`SegHead = nn.Linear(C, num_classes)` 线性探针。**官方只提供 ScanNet 室内探针**（`utonia_linear_prob_head_sc`），航测城市类别需要自己拿 STPLS3D 或 SensatUrban 训练。
- 支持无颜色、无法线输入（`--wo_color --wo_normal` 置零），与 UrbanScene3D 只有 xyz+normal 的情况吻合。

---

## 3. 可复用算法与实现（含伪代码与参数）

### 3.1 Reconstruction Engine Adapter v2（规范系归一）

各引擎的约定（本单元实测和读码结果，加上 r01、r02）：

| 引擎 | 位姿方向 | 四元数 | 世界系（gauge） | 尺度 | 置信度 |
|---|---|---|---|---|---|
| LingBot-Map | C2W（pose_enc 解码）；`demo.py` 存 W2C | xyzw | scale 帧附近的相机系 | 相对 | `1+exp` |
| VGGT | W2C | xyzw | 第 0 帧 | 平均距离归一 | `1+exp` |
| VGGT-Ω | **W2C** | 从 R 转换 | 第 0 帧（camera token 分 ref/src 两组） | 相对 | `depth_conf` |
| DA3 any-view | **W2C** | — | **N≥3 时为 saddle 选出的参考视图**；给了位姿条件时对齐到输入 | 相对（Nested 为米） | `conf` |
| MapAnything | **C2W** | **xyzw** | view0 | **米**（`metric_scaling_factor`） | `conf` + `mask` |
| π3 / Pi3X | C2W | — | 置换等变，无固定参考帧 | 相对（Pi3X 近似米） | `sigmoid(logit)` |

```python
# recon/adapters/base.py —— 所有引擎输出都先经过它，再写入 Recon IR（r02 §3.2）
def normalize_engine_output(eng, poses, pts=None, depth=None, conf=None):
    """返回 T_world_cam（C2W，frame0 = 单位阵）、q_xyzw、conf01 和 scale_status"""
    T = np.asarray(poses)                          # [N,4,4] 或 [N,3,4]
    if T.shape[-2] == 3:
        T = np.concatenate([T, np.tile([0,0,0,1.], (len(T),1,1))], 1)
    if ENGINE[eng].pose_dir == "w2c":              # VGGT / VGGT-Ω / DA3
        T = np.linalg.inv(T)
    # 规范到 frame0：DA3（saddle 参考）和 π3（无参考）必须做，其余做了也无害
    T0inv = np.linalg.inv(T[0])
    T = T0inv @ T
    if pts is not None:
        pts = apply_T(T0inv, pts)
    # 方向自检：相机前向（+Z）应大致指向该帧点云的质心
    assert frac_points_in_front(T, pts) > 0.8, "pose direction flipped?"
    conf01 = ENGINE[eng].conf_to_unit(conf)        # 1+exp → 1−1/c；logit → sigmoid
    scale_status = ENGINE[eng].scale               # relative | metric-predicted | metric-conditioned
    return T, rot_to_quat_xyzw(T[:, :3, :3]), conf01, scale_status
```

写入 `reconstruction/<session>/engine.json`（在 r02 的 session.json 旁边）：

```json
{ "engine": "mapanything", "version": "1.1.4", "weights": {"repo": "facebook/map-anything", "sha256": "…", "license": "CC-BY-NC-4.0"},
  "gauge": {"pose_dir_raw": "c2w", "ref_view_raw": 0, "renormalized_to_frame0": true},
  "scale_status": "metric-conditioned",
  "conditioning": {"intrinsics": true, "poses": "rtk+imu", "depth": "sparse-mid360"},
  "process_res": 518, "chunking": {"size": 64, "overlap": 16, "align": "se3"} }
```

### 3.2 用 RTK 位姿和 MID-360 深度做度量条件重建（MapAnything）

```python
# recon/engines/mapanything_engine.py（GPU Worker，V0.5）
views = []
for f in keyframes:                                  # 关键帧：按 r01 的 flow / 距离规则选
    T_w_b  = rtk_imu_pose(f.t_ns)                    # ENU 世界系下的机体位姿（杆臂、时间偏移已补偿，r02 §3.5）
    T_w_c  = T_w_b @ T_b_cam                         # 相机外参（UMI-3D / FAST-Calib 标定）
    d = None
    if lidar_available:
        P_l   = lio_map.query_frustum(T_w_c, K, max_range=60)   # 用 LIO 局部地图，而不是单帧扫描
        d     = zbuffer_project(P_l, T_w_c, K, H, W)            # 无效像素置 0，前景取最近
    views.append(dict(img=f.rgb, intrinsics=K,
                      camera_poses=torch.tensor(T_w_c - center_offset),   # 先减去 chunk 中心，避免 float32 精度问题
                      depth_z=d, is_metric_scale=torch.tensor([True])))
views = preprocess_inputs(views)
pred  = model.infer(views, memory_efficient_inference=True, use_amp=True, amp_dtype="bf16",
                    apply_mask=True, mask_edges=True, use_multiview_confidence=True)
```

规则和参数：
- **分块**：每块 32–64 视图，重叠 25%，块间用 **SE(3)**（度量输出）加 IRLS 对齐（§3.3，`align_method: se3`）。
- **平移数值**：块内先减去中心。`normalize_pose_translations` 本身会归一化，但 fp32 的 ENU 坐标超过 8 km 时精度只有毫米级，见 r02 §7。
- **质量门禁**：`metric_scaling_factor` 与 RTK 基线之比应在 [0.98, 1.02] 内，否则标记 `scale_suspect`。多视一致性置信度（§3.5）的中位数 <0.5 时整块降级。
- **对照实验（待验证）**：同一段 P600 视频分别跑 ① 纯图像、② +RTK 位姿、③ +RTK+LiDAR 稀疏深度，与 LIO 地图做 C2C 距离比较。这组实验决定 V0.5 是"前馈融合"还是"后配准融合"。

### 3.3 长序列 chunk 流式对齐（DA3-Streaming / VGGT-Long 可移植核心）

**chunk 划分**（`DA3_Streaming.get_chunk_indices`）：

```
step = S − O；n_chunks = ceil((N − O) / step)；chunk_i = [i·step, min(i·step + S, N))
默认 S = 120，O = 60（每帧推理约 2 次）；Map-Long 这类度量模型可改用 S = 64，O = 16
```

**置信度加权鲁棒 Sim3/SE3**（`sim3utils.weighted_align_point_maps` + `robust_weighted_estimate_sim3`）：

```python
def align_chunks(Pa, Ca, Pb, Cb, method="sim3", delta=0.1, iters=5):
    """Pa/Pb：相邻 chunk 在重叠帧上的点图 [O,H,W,3]；返回把 b 变到 a 的 (s, R, t)"""
    thr = 0.1 * min(np.median(Ca), np.median(Cb))           # 置信度阈值
    m   = (Ca > thr) & (Cb > thr)
    src, dst = Pb[m], Pa[m]
    w0  = np.sqrt(Ca[m] * Cb[m])                            # 联合置信度作为初始权重
    s, R, t = weighted_umeyama(src, dst, w0, scale=(method == "sim3"))
    prev = np.inf
    for _ in range(iters):                                  # IRLS-Huber
        r   = np.linalg.norm(dst - (s * src @ R.T + t), axis=1)
        wh  = np.where(r > delta, delta / r, 1.0)
        w   = w0 * wh
        w  /= w.sum()
        s2, R2, t2 = weighted_umeyama(src, dst, w, scale=(method == "sim3"))
        cost = (huber(r, delta) * w0).sum()
        if abs(prev - cost) < 1e-9 * prev:
            break
        s, R, t, prev = s2, R2, t2, cost
    return s, R, t

def weighted_umeyama(X, Y, w, scale=True):
    w  = w / w.sum()
    mx, my = w @ X, w @ Y
    Xc, Yc = X - mx, Y - my
    s  = np.sqrt((w * (Yc**2).sum(1)).sum() / (w * (Xc**2).sum(1)).sum()) if scale else 1.0
    H  = (s * Xc * np.sqrt(w)[:, None]).T @ (Yc * np.sqrt(w)[:, None])
    U, _, Vt = np.linalg.svd(H)
    R  = Vt.T @ U.T
    if np.linalg.det(R) < 0:
        Vt[2] *= -1
        R = Vt.T @ U.T
    return s, R, my - s * R @ mx
```

**累积**（`accumulate_sim3_transforms`）：

```
s_k = s_{k−1}·s'_k
R_k = R_{k−1}·R'_k
t_k = s_{k−1}·R_{k−1}·t'_k + t_{k−1}
```

**回环**：`LoopDetector` 用 SALAD（DINOv2，336²）计算全局描述子，取余弦相似度 ≥0.85 的 top-5，NMS 窗口 25 帧。对每个回环对，截取两端各 `loop_chunk_size=20` 帧拼成一个 chunk 重新推理，得到回环 Sim3。`Sim3LoopOptimizer` 用 pypose 做 LM（顺序边 + 回环边，30 次迭代，λ0=1e-6）。

**本项目用法**：
- V0.5 的"离线长视频 Job"直接调用 `da3_streaming.py`，产物 `camera_poses.txt` / `intrinsic.txt` / `pcd/combined_pcd.ply` 通过 Adapter 写入 IR；
- V0.1 的 mock 管线可以用 numpy 版 `align_chunks` 拼接 CPU 上 DA3-SMALL 的多段结果（`align_lib: numpy` 或 `numba`）。

### 3.4 固定点预算的流式预览：向量化水库采样（Algorithm R）

来源：`sim3utils.optimized_vectorized_reservoir_sampling`。这是修正版，旧版 `vectorized_reservoir_sampling` 会造成密度不均，见 VGGT-Long issue #28。

```python
def reservoir_update(res_pts, res_attr, seen, new_pts, new_attr, rng):
    """res_*: [K,…] 固定预算缓冲；seen：已见点数。每个点最终被保留的概率都是 K / n。"""
    K, M = len(res_pts), len(new_pts)
    if seen < K:                                  # 先填满
        k = min(K - seen, M)
        res_pts[seen:seen+k] = new_pts[:k]
        res_attr[seen:seen+k] = new_attr[:k]
        new_pts, new_attr, seen = new_pts[k:], new_attr[k:], seen + k
        M -= k
    idx = np.arange(seen + 1, seen + M + 1)       # 第 i 个新点的序号（从 1 开始）
    j   = rng.integers(0, idx)                    # j ~ U[0, i)
    rep = j < K
    res_pts[j[rep]] = new_pts[rep]
    res_attr[j[rep]] = new_attr[rep]              # 同一批次内后写覆盖先写，与串行语义一致
    return seen + M
```

**落点**：ReconstructionLayer 的"重建进行中"预览（V0.1 Job 进度推送）、实时建图演示（§3.7）。参数取 K = 300k 点（桌面端）或 100k 点（移动端），与 r12 的全局 point budget 分开计算。缓冲区是**固定大小的 GPU buffer**，每次更新只 `bufferSubData` 被替换的下标区间，不重建几何。

### 3.5 多视深度一致性置信度（QA 与入库过滤）

来源：`multiview_confidence.compute_multiview_depth_confidence`。

```
对每个源视图 i：
  取与 i 视锥相交的视图 J(i)（frustum_intersection_check，near/far 取有效深度的 min/max）
  对每个像素 p（深度 d_i(p) > 0）：
    X = T_i · backproject(p, d_i(p))
    对每个 j ∈ J(i)：
      (u, v, z) = project(K_j, T_j⁻¹ X)
      若 (u, v) 在图内且 z > 0.04：
        d̂ = d_j(round(u, v))                          # grid_sample nearest
        若 |z − d̂| < 0.02 + 0.02·z，则 inlier += 1，否则 outlier += 1
  conf_i(p) = inlier / (inlier + outlier)             # 没有重叠视图时为 1
```

用途：
- ① 入库前过滤，丢弃 conf<0.3 的像素点；
- ② 重建 QA 指标（写入 r02 的 `qa.json`，并在 UI 的重建质量面板里用 lieflat 风格直方图展示）；
- ③ 为 OpenFlyScan 式"补拍区域"提供低质量区域掩码（V1.0）。

纯 PyTorch 实现，CPU 上可以运行（O(N·|J|·H·W)），适合在 Job 末尾执行。

### 3.6 入库密度归一：IQR 自适应体素

来源：`colmap_export.voxel_downsample_point_cloud`。

```
extent = 2 · max_axis(Q75(P) − Q25(P))       # 对离群点和远景天空点鲁棒
voxel  = extent · f                          # MapAnything 默认 f = 0.01
```

与 r09 的八叉树 spacing 规则配合：**入库阶段先按 IQR 体素去重**（前馈重建会在同一表面产生大量冗余点，16 帧 × 504×280 就有 2.26M 像素），再交给 PotreeConverter 式建树。DA3 的 GLB 导出另有两个默认值值得沿用：`conf_thresh_percentile=40`（丢掉置信度最低的 40%）和 `num_max_points=1_000_000`。

### 3.7 OctVoxMap：实时建图去重与有界内存（Super-LIO → 服务端 Python/TS）

```python
class OctVoxMap:
    """体素 res（0.5 m），每个体素 8 个子体素（res/2），每个子体素只存 1 个滑动均值点。
    LRU 容量上限 capacity（2e6 体素），超出时淘汰最久未访问的。"""
    def __init__(self, res=0.5, capacity=2_000_000, max_cnt=20, gate=0.1):
        self.inv_sub = 2.0 / res
        self.cap, self.max_cnt, self.gate2 = capacity, max_cnt, gate * gate
        self.vox = OrderedDict()                 # key → [pts(8,3) f32, cnt(8) u8]

    def insert(self, P):                         # P：世界系 [M,3]
        fine = np.floor(P * self.inv_sub).astype(np.int64)
        key  = fine >> 1                         # 体素坐标
        li   = ((fine[:, 2] & 1) << 2) | ((fine[:, 1] & 1) << 1) | (fine[:, 0] & 1)
        dirty = set()
        for k, l, p in zip(map(tuple, key), li, P):
            v = self.vox.get(k)
            if v is None:
                v = self.vox[k] = [np.zeros((8, 3), np.float32), np.zeros(8, np.uint8)]
                if len(self.vox) > self.cap:
                    self.vox.popitem(last=False)
            else:
                self.vox.move_to_end(k)          # LRU 刷新
            pts, cnt = v
            if cnt[l] == 0:
                pts[l], cnt[l] = p, 1
                dirty.add(k)
            elif cnt[l] < self.max_cnt and ((p - pts[l])**2).sum() <= self.gate2:
                pts[l] = (pts[l] * cnt[l] + p) / (cnt[l] + 1)
                cnt[l] += 1                      # 位置精化，不算新点
        return dirty                             # 本帧新增了点的体素，用于 WS 增量下发
```

- 生产实现应把逐点 Python 循环换成 numpy 分组（`np.unique(key)` 加 `np.add.at`），或者用 C++ 扩展。上面只表达语义。
- 查询（LIO 用）：60 个邻域体素按最小距离分 6 组，5-NN 满且最大距离平方小于下一组的最小距离平方时提前退出。`HKNN_list60_gem.h::orders_min_dis2` 里写死的常量是 0.0625、0.125、0.25、0.3125、0.375（m²），**只对应 res = 0.5 m**。换分辨率时要乘以 (res/0.5)²，原代码没有做这一步，是个潜在坑。Web 端不需要这一步。
- **本项目用法**：
  - V0.2+ 的 "Live Mapping 演示"：Sim Service 用 §3.11 的 mock MID-360 扫描喂 OctVoxMap，每 100 ms 把 `dirty` 体素打包成增量下发；
  - V0.5 真机时，同一个类接 LIO 的 `deskewed_scan`。
  - 浏览器端只维护"体素 key → GPU buffer 槽位"的映射，槽位用完后按 LRU 回收，与 r12 的节点缓存策略一致。

### 3.8 实时地图增量线协议：int8 体素块（RKO-LIO 的 VoxelBlock 思路）

```
LiveMapDelta（WS 二进制帧，msg_type = 0x21，小端）
  u8  type=0x21 | u8 flags(bit0 = keyframe 全量) | u16 n_blocks | u32 seq | i64 t_ns
  f32 voxel_size (m) | f64 origin_enu[3]（瓦片原点，与 r03/r09 的瓦片局部 ENU 一致）
  blocks[n_blocks]：
     i16 kx, ky, kz          # 相对原点的体素坐标（±32767 × 0.5 m = ±16 km）
     u8  n_pts (≤8) | u8 cls（体素主类别，§3.10；未知为 1）
     i8  off[n_pts][3]        # 相对体素中心，quantum = voxel_size / 254
点坐标：p = origin + (k + 0.5)·voxel + off·quantum
```

- 体积：每块 8 B，每点 3 B。20k 点/扫描去重后通常新增 2–5k 点（估算），即 **8–20 KB/帧，10 Hz 下约 80–200 KB/s**。
- 与 r01 的 uint16 节点量化对比：静态八叉树节点尺度大，用 u16；实时体素尺度小（0.5 m），int8 就够了。0.5 m / 254 ≈ **2 mm**，低于 MID-360 的测距噪声（约 2 cm）。
- 解码在 Worker 里完成（Transferable），写入固定大小的 GPU ring buffer。

### 3.9 LIO 核心公式速查（mock-LIO、离线 Job 与 QA 用）

- **RKO-LIO 重力正则**：
  - `β = min_beta·(1 + σ²_|a|)`，`H = H_icp + H_ori/β`；
  - 残差 `r = R⁻¹(−g) − ĝ_local`，`J_ori = [0₃ | [R⁻¹(−g)]×]`。加速度越抖（σ² 大），重力约束越弱。
  - 机体加速度 KF：`Q = (j_max·dt)²/3·I`（`j_max=3 m/s³`），`R = σ²_|a|/3·I`。
- **Super-LIO 点到平面门限**：平面拟合残差 ≤0.1 m，且 `|e| < √range / 9`（代码写作 `length > 81·e²`）。远处的点允许更大残差，观测权重固定为 1000。
- **双降采样**（RKO-LIO `preprocess_scan`）：先按 0.5·voxel 降采样入图，再按 1.5·voxel 取关键点做 ICP。

### 3.10 城市语义 L0：CSF + DTM + HAG + 法线规则（MVP，本机已跑通）

**类别编码**：采用 ASPRS LAS 1.4 classification（u8），与 PDAL、LAS、3D Tiles 生态兼容。64–255 为用户自定义区间。

| code | 名称 | 01-design §7 映射 | MVP 来源 |
|---|---|---|---|
| 1 | Unclassified | — | 默认 |
| 2 | Ground | Road/Mountain（地面统称） | CSF |
| 3/4/5 | Low/Med/High Vegetation | Tree | 规则（UrbanScene3D 几乎没有） |
| 6 | Building（屋顶） | Building | 规则 |
| 64 | Building Facade（自定义） | Building | 规则 |
| 65 | Low Object（自定义，车、设施等） | — | 规则 |
| 9 | Water | — | V0.5（需要颜色或反射强度） |
| 11 | Road Surface | Road | V0.5（需要颜色、强度或矢量路网） |
| 14 | Wire-Conductor | Powerline | V1.0（学习型模型或专门算法） |

Restricted Area 属于**矢量或体积语义**，不作为点类别，放在 `semantic/zones.geojson` 里（带高度区间）。

**流程与参数**（`n02_semantic.py`）：

```python
# 0. 前置：r09 的单位 / up 轴归一（Suzhou Y→Z；Chicago km→m；SF 缩放待核实）
# 1. 地面：CSF
csf.params.cloth_resolution = clamp(3 * median_spacing, 1.0, 3.0)   # NY 3.0、SF 1.0、Shenzhen 2.0
csf.params.rigidness        = 3 if flat_city else 2                # 1 山地 / 2 复杂 / 3 平坦
csf.params.bSloopSmooth     = True
csf.params.class_threshold  = 0.5                                  # 米
csf.params.interations      = 500
csf.params.time_step        = 0.65
# 2. DTM：地面点按 cell = 2 m 取每格最小 z；空格（建筑占地）用最近有效格填补（KD 树）
# 3. HAG = z − DTM[cell(x, y)]
# 4. 0.5 m 体素代表点上算 kNN(K = 16) 特征；法线用数据自带的网格法线（比 PCA 法线可靠）
n_z   = |n·ẑ|
agree = mean_k( |n_i·n_k| > cos20° )                               # 法线一致性：平面接近 1，树冠低
# 5. 规则（v2）
ground   = csf_ground | HAG < 0.3
veg      = ¬ground ∧ 0.5 < HAG < 35 ∧ agree < 0.35 ∧ 0.15 < n_z < 0.95
low_obj  = ¬ground ∧ ¬veg ∧ HAG < 2.5
roof     = ¬ground ∧ ¬veg ∧ HAG ≥ 2.5 ∧ n_z > 0.9
facade   = ¬ground ∧ ¬veg ∧ HAG ≥ 2.5 ∧ n_z < 0.2
sloped   = 其余 HAG ≥ 2.5 的点 → 并入 Building（v2 里暂记为 high_other）
# 6. 在 kNN 上做一次多数投票平滑；体素代表点的类别回传给所有原始点（inverse 索引）
```

**实测**（5M 点/城，单进程 numpy + CSF C++；详见附录 C）：

| 城市 | 布料 / 刚度 | CSF 耗时 | 特征耗时 | 总耗时 | 地面 | 屋顶 | 立面 | 植被 | 低矮物 | 其他 |
|---|---|---|---|---|---|---|---|---|---|---|
| New York | 3 m / 3 | 50.1 s | 51.5 s | **112 s** | 35.6% | 14.2% | 47.6% | 0.03% | 2.2% | 0.4% |
| New York | 2 m / 3 | 158.6 s | 52.6 s | 222 s | 地面比例 0.270 vs 0.269（几乎相同） | | | | | |
| San Francisco | 1 m / 2 | 20.6 s | 24.9 s | 51.6 s | 80.3% | 1.4% | 4.2% | 0.12% | 14.0% | 0.04% |
| San Francisco | 3 m / 3 | 3.6 s | 25.2 s | 34.9 s | **失败**：西北山丘被判为屋顶和低矮物 | | | | | |
| Shenzhen | 2 m / 2 | 59.4 s | 43.9 s | 112 s | 64.3% | 10.1% | 22.1% | 0.01% | 2.2% | 1.3% |

**结论与坑：**
- ① v1 规则用 PCA 散度判植被时，稀疏立面在屋檐转角处 kNN 跨面，被误判为植被（NY 误报 21%）。改用"网格法线 + 法线一致性"（v2）后误报降到 0.03%。**对 UrbanScene3D 这类网格采样点云，要优先利用自带法线。**
- ② CSF 布料分辨率对耗时影响是平方级（3 m 比 2 m 快约 3 倍），地面比例基本不变。**城市用 2–3 m，丘陵用 1 m 加刚度 2。**
- ③ Shenzhen 的景观坡地、SF 的丘陵在布料过粗时会被判为屋顶。**v3 修正（未实现）**：对 HAG ≥2.5 的点做连通分量（3D 体素 6 邻域），立面点占比 <5% 的分量改判为地形（Ground）。真实建筑一定有立面，坡地没有。
- ④ 三座虚拟城市几乎没有植被。"Tree" 图层要么依据 ground 区域和道路边缘规则**程序化生成树实例**（mock），要么等真实数据（P600 实采或 STPLS3D real 子集）。

**Web 端落点**（直接服务流畅性）：
- 节点二进制在 r12/r09 布局后追加 `classification: u8[n]`（1 B/点）和可选的 `hag_q: u8[n]`（HAG/0.5 m，0–127.5 m）；
- 图层开关是一个 uniform `classMask: u32`（用紧凑索引 0–15 映射 ASPRS 码），**不重新加载，也不重建 buffer**；
- 按类别调整 LOD 预算：地面点在屏幕空间误差计算中乘以 1.5 的降权系数（估算值，需在 r12 基准中调参），从而把点预算让给建筑轮廓。

```glsl
// 点云顶点着色器片段（WebGL2；WebGPU 用 TSL 同构写法）
uniform uint classMask;          // bit i = 第 i 个紧凑类别可见
in uint aClass;                  // 紧凑类别索引 0..15
void main() {
  if (((classMask >> aClass) & 1u) == 0u) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); gl_PointSize = 0.0; return; }  // 裁到视锥外
  ...
}
```

**DTM 产物**（新增到 World Package：`geometry/terrain/dtm.f32` + `dtm.json{origin_enu, cell, W, H, nodata}`）的用途：
- ① UI 显示 AGL（离地高度），与 AMSL 并列；
- ② mock 无人机贴地飞行和最低安全高度校验；
- ③ WindNinja（r17）Level 2 地形风的 DEM 输入；
- ④ LiDAR 仿真时用作地面兜底。

### 3.11 Mock MID-360 + IMU 合成 + RKO-LIO（V0.2 测试床，本机已跑通）

`n02_rko_mock.py` 的完整链路，可直接移植成 `sim/sensors/lidar_mock.py` 和 `tests/test_lio_mock.py`：

```python
# 轨迹：先悬停 2 s（供 IMU 初始化），再用 4 s 余弦速度爬坡，然后沿 ∞ 字形飞行，平均速度 SPEED
w = 2π·SPEED / L_period
s(t) = w·warp(t)
p(t) = (cx + A_x·sin s, cy + A_y·sin 2s, z0 + 5·sin(s/2))
# 四旋翼姿态：机体 z 轴 = normalize(a + g·ẑ)，偏航 = 速度方向（悬停段冻结）
z_b = (a + g)/|a + g|;  x_c = (cos ψ, sin ψ, 0);  y_b = z_b × x_c / |·|;  x_b = y_b × z_b;  R_wb = [x_b y_b z_b]
# IMU（200 Hz）：比力 f_b = R_wbᵀ(a + g)，角速度 ω_b = Log(R_kᵀ R_{k+1})/dt
#   加噪声 σ_a = 0.05 m/s²、σ_g = 0.005 rad/s；常值偏置 b_a = (0.03, −0.02, 0.04)、b_g = (2, −1, 1.5)e-3
# LiDAR（10 Hz，≤20k 点/帧）：MID-360 倒装后绕 y 轴倾斜 20°：R_bl = diag(1, −1, −1)·R_y(20°)
#   FOV：方位 360°、俯仰 −7°…52°（传感器系），量程 70 m
#   可见性：720×118 的方位 / 俯仰格 z-buffer，取每格最近距离（+0.3 m 容差）
#   表面重采样：在切平面内加 N(0, 0.35·spacing) 的随机偏移，避免每帧命中同一批采样点导致 ICP 虚高
#   时间戳：点均匀分布在 [t0, t0+0.1)，按 10 个 10 ms 子片分别用对应时刻的真实位姿变换（制造运动畸变）
#   测距噪声：σ_r = 2 cm
lio = LIO(LIOConfig(voxel_size=1.0, max_range=70, min_range=0.5, initialization_phase=True))
for scan:
    lio.add_imu_measurement(acc, gyro, t_ns, T_imu2base)   # 逐条喂 IMU
    lio.register_scan(pts_lidar, ts_ns, T_lidar2base)
tsn, poses = lio.poses_with_timestamps()                    # poses [N,7] = xyz + qxyzw
```

**实测**（Shenzhen 低层片区，地面以上约 60 m 飞行；8 核 CPU）：

| 时长 | 路径 | 扫描数 | 配准 p50 / p95 | 扫描生成 p50 | ATE RMSE（SE3 对齐） | ATE max | 仅用前 10 s 对齐后的终点误差 |
|---|---|---|---|---|---|---|---|
| 60 s | 424 m | 600 | 8.8 / 18.5 ms | 30.7 ms | 0.117 m | 0.207 m | 2.03 m（0.48%） |
| 180 s | 1410 m | 1800 | 11.3 / 25.0 ms | 36.5 ms | 0.386 m | 0.753 m | 2.04 m（0.145%） |

说明：
- 这是**合成数据**（网格采样点云加切向抖动，不是真实的非重复扫描花样），只能证明链路可行和算力量级，**不代表真机精度**。
- "前 10 s 对齐"的终点误差主要来自悬停段基线太短导致的偏航外推，不代表 LIO 发散。
- **算力结论**：单机 Python 下 1 架无人机"仿真扫描 + LIO"约需 45 ms/100 ms。3 架以上要把扫描降到 5k 点、频率降到 5 Hz，或者把可见性计算改成 C++/numba。**浏览器端只画 LiDAR FOV 和稀疏化后的扫描**（2–5k 点/帧，int16 传感器系量化，1 cm），约 0.1–0.3 MB/s/架。

### 3.12 CPU 冒烟重建：DA3-SMALL（V0.1 CI 与离线演示）

```python
from depth_anything_3.api import DepthAnything3            # xformers 缺失时自动回退，CPU 可用
m = DepthAnything3.from_pretrained("depth-anything/DA3-SMALL").eval()
pred = m.inference(keyframes[:8], process_res=336,          # 8 帧 336 分辨率在本机约 27 s
                   ref_view_strategy="first")               # 明确让第 0 帧作为 gauge，省去 Adapter 的重新规范
# pred.depth [N,h,w]、pred.conf、pred.extrinsics（W2C 3×4）、pred.intrinsics
# 再走 Adapter → Recon IR → PotreeConverter 式建树 → World Package
```

实测数据见附录 A。建议 CI 只跑 **8 帧 336 分辨率（约 27 s，RSS 1.5 GB）**。演示用 16 帧 504 分辨率（约 88 s）。尺度统一标记为 `relative`，之后按 r02 §3.5 用 GNSS 做 Sim3。

---

## 4. 在本项目中的落点与复用方式

| 功能 | 来源 | 目标模块 | 版本 | 复用方式 | 说明 |
|---|---|---|---|---|---|
| Engine Adapter v2（方向、gauge、尺度、置信度归一，engine.json） | MapAnything 统一字典 + 各引擎约定 | `reconstruction/adapters/` | V0.1 | port | 所有引擎的唯一出口；必须带方向自检 |
| CPU 冒烟重建 | DA3-SMALL（Apache） | `reconstruction/engines/da3_cpu.py` + CI | V0.1 | adopt | 让"视频 → World"在无 GPU 时也有真实（非 mock）路径 |
| 流式预览固定预算 | DA3-Streaming 水库采样 | `apps/api/jobs/recon_preview.py` + Web ReconLayer | V0.1 | port | K = 100k–300k |
| 城市语义 L0 + DTM + HAG | CSF + n02 规则 | `world/ingest/semantic_rules.py`、`world/geometry/terrain/` | **V0.1** | adopt + 自研 | UrbanScene3D 内置世界入库时运行 |
| 类别掩码图层开关 | 自研（ASPRS 编码） | Web `PointCloudMaterial` / TSL | **V0.1** | 自研 | 1 B/点 + uniform |
| AGL 显示、贴地、最低安全高度 | DTM | Sim Service + DroneState | V0.2 | 自研 | `DroneState.alt_agl` |
| Mock MID-360 + IMU 合成 | n02 mock（移植 r05 的 z-buffer） | `sim/sensors/lidar_mock.py`、`imu_mock.py` | V0.2 | port | 同时服务 SensorLayer 可视化 |
| mock-LIO 回归测试 | RKO-LIO（pip） | `tests/test_lio_mock.py` | V0.2 | adopt | ATE、耗时门禁 |
| 实时建图演示（去重 + 增量下发） | OctVoxMap + int8 VoxelBlock 协议 | `sim/mapping/octvox.py` + WS 0x21 | V0.2–V0.3（可选） | port | 展示 Reality→Reconstruction |
| 离线长视频重建 | DA3-Streaming | GPU Worker `jobs/recon_long.py` | V0.5 | adopt | chunk 120/60，回环 |
| RTK + LiDAR 条件度量重建 | MapAnything | GPU Worker `engines/mapanything_engine.py` | V0.5 | adopt | SE3 分块；与后配准方案对照 |
| 重建质量置信度 | MapAnything 多视一致性 | `reconstruction/qa/` | V0.5 | port | 写入 qa.json 与 UI 质量面板 |
| 离线 LIO（P600 bag） | RKO-LIO（rosbags） | `reconstruction/lidar/lio_job.py` | V0.5 | adopt | 不装 ROS 的 FastAPI Worker |
| 机载 LIO | FAST-LIO2（Prometheus）或 Super-LIO | P600 Orin NX | V0.5 | adopt | ROS2；Super-LIO 带重定位 |
| LiDAR-相机外参 | UMI-3D（MID-360 + 鱼眼） | 标定工具链 | V0.5 | reference | 与 r04/r05 的 FAST-Calib 并列 |
| 学习型语义 L1 | Utonia/PTv3 线性探针 + STPLS3D/SensatUrban | GPU Worker `semantic/learned.py` | V0.5+ | reference→adopt | spconv/flash-attn；需要标注 |
| 开放词汇 L2（"找人 / 车 / 电力线"） | SAM 3 + PE（VGGT-SLAM 2.0 的做法） | Agent Runtime / 搜救工作流 | V1.0 | reference | §32 的 "Drone A 发现疑似目标" |
| 质量引导补拍 | OpenFlyScan | Agent 任务 "reacquire" | V1.0 | reference | 低置信区域 → 生成补拍航带 |
| 集群 LIO | Swarm-LIO2 | 多机真机 | V1.0 | reference | 带宽高效的去中心化 LIO |

**MVP（V0.1–V0.3）实际需要本单元提供的只有 4 件**：CSF 语义与 DTM（入库）、class mask（渲染）、mock MID-360/IMU 加 RKO-LIO 测试床、DA3-SMALL CPU 冒烟重建。其余都是 V0.5 以后的 GPU 路线，不进 MVP。

```text
V0.1 ingest（UrbanScene3D 内置世界）
  raw.ply ──r09 归一化──► CSF(ground) ──► DTM(2 m) ──► HAG ──► 网格法线规则 + 平滑 ──► classification u8
                                  │                                          │
                                  └──► geometry/terrain/dtm.f32              └──► 八叉树节点属性块（r09/r12）
V0.2 仿真
  DroneState(p, R) ──► lidar_mock（z-buffer，≤20k 点）──► [可选] OctVoxMap ──► WS 0x21 增量
                  └──► imu_mock（200 Hz）─────────────┘             └──► RKO-LIO（测试 / QA）
V0.5 GPU Worker
  video + RTK + MID-360 ──► LIO(RKO / Super) ──► MapAnything(pose + sparse depth, SE3 chunks) ──► Adapter v2 ──► IR ──► Fusion(GICP)
  long video only        ──► DA3-Streaming(Sim3 + loop) ──► Adapter v2 ──► Sim3(GNSS) ──► IR
```

---

## 5. 对比与推荐

### 5.1 前馈重建引擎（排序依据：★、2026 活跃度、与"无人机长视频 + RTK + LiDAR"的契合度、本机可跑性）

| 排名 | 引擎 | ★ / 最近提交 | 优势 | 劣势 | 本项目角色 |
|---|---|---|---|---|---|
| 1 | LingBot-Map | 17141 / 09-08 | 真流式（paged KV），星数最高，r01 已完整研究 | 相对尺度；KV 池约 11 GB；ATE 在户外大场景偏大 | 在线 / 流式主引擎 |
| 2 | MapAnything | 3771 / 08-07 | **度量**；可接入位姿、内参、深度（含稀疏）条件；统一框架封装其余模型；AerialMegaDepth 训练数据 | 1B 级模型，需 GPU；默认权重 NC | 带 RTK/LiDAR 的度量引擎 + 评测框架 |
| 3 | Depth-Anything-3 | 6402 / 07-27 | 星数高；Apache 小模型 **CPU 可跑**；DA3-Streaming 长序列 + 回环；支持 COLMAP/GLB/3DGS 导出 | gauge 为 saddle 参考视图；大模型 NC | 离线长序列 + CPU 冒烟 |
| 4 | VGGT-Ω | 4588 / 09-22 | VGGT 官方后继；静态、动态场景都 SOTA；register 可对齐语言 | 权重需申请；NC；1B | 对照评测（经 MapAnything） |
| 5 | π3 / Pi3X | 2181 / 05-18 | 置换等变、长序列稳；Pi3X 支持条件输入；OpenFlyScan 的无人机工作站在用 | 权重 NC；无固定参考帧 | 对照评测 |
| — | CUT3R / TTT3R / StreamVGGT / STream3R / InfiniteVGGT | 391–1497 | 状态式流式、可无限长 | 精度和星数都不如上面几项；功能与 LingBot 重复 | skip |
| — | Scal3R / AMB3R(-SLAM) / VGG-T³ | 199–538 | 公里级、线性注意力 | AMB3R-SLAM 官方代码未发布 | 持续观察 |

### 5.2 SLAM / LIO

| 排名 | 项目 | ★ / 最近提交 | 本机可跑 | 本项目角色 |
|---|---|---|---|---|
| 1 | RKO-LIO | 660 / 09-24 | **pip wheel 可跑，已实测** | 后端离线 LIO、mock 测试床 |
| 2 | Super-LIO | 602 / 07-13 | 需要 ROS2 + colcon | 机载 LIO 候选（ARM64，带重定位） |
| 3 | FAST-LIO2（Prometheus 内置）/ FASTLIO2_ROS2 | 5226（2024 停更）/ — | ROS | 机载现状，r05 已研究 |
| 4 | GLIM（已有） | 1848 / 09-06 | GPU 可选 | 全局建图 + GNSS 因子（r07） |
| 5 | Point-LIO | 1338 / 06-13 | ROS | 高机动场景对照 |
| 参考 | VGGT-SLAM 2.0、MASt3R-SLAM、SLAM3R | 1133 / 3193 / 1369 | 需要 CUDA | 纯视觉 SLAM 对照；VGGT-SLAM 2.0 的开放词汇检测给 V1.0 参考 |
| 参考 | MINS、LIGO、FAST-LIVGO（论文） | 784 / 330 / — | ROS | RTK/GNSS 紧耦合思路 |

### 5.3 语义

| 排名 | 方案 | 本机可跑 | 适用 |
|---|---|---|---|
| 1 | CSF + 几何规则（n02 v2/v3） | **可跑，已实测** | MVP：Ground / Building(roof+facade) / LowObject |
| 2 | PDAL `filters.csf` / `filters.smrf` + `filters.hag_nn` | 可跑（r09） | 与 ingest 管道一体化；可替代 Python CSF |
| 3 | Utonia / PTv3 + 线性探针（STPLS3D、SensatUrban） | 需要 GPU | V0.5+，含植被、道路、车辆 |
| 4 | Myria3D | 需要 GPU；依赖 LiDAR HD 的强度和回波数 | 真实航空 LiDAR 的产线参考 |
| 5 | SAM 3 在 2D 上分割再按位姿投影到 3D | 需要 GPU | V1.0 开放词汇目标 |

---

## 6. 风险与注意事项

1. **许可与获取。** VGGT-Ω 权重需在 HF 上申请（自动审核，可能被拒）。MapAnything 默认权重、DA3 LARGE/GIANT/NESTED、π3/π3X 权重都是 CC-BY-NC。本项目是科研用途，按要求忽略许可，但**必须把许可写进 `engine.json`**，以免将来误用于其他场景。DA3-SMALL/BASE 和 `map-anything-apache` 是 Apache。
2. **GPU 依赖链。**
   - flash-attn 与 spconv（Utonia、PTv3）、xformers（DA3 可选）、triton（DA3-Streaming 的 `align_lib: triton`）、gsplat（DA3 的 GS 头）、FlashInfer（LingBot）都绑定 CUDA 版本。
   - GPU Worker 建议统一镜像为 CUDA 12.8 + torch 2.8，每个引擎一个 venv，互不污染。MapAnything 的 `[all]` extra 会拉十几个 git 依赖，**不要装在同一个环境里**。
3. **显存量级。** VGGT-Ω 约 6 GB + 0.074 GB/帧（300 帧 28 GB）；LingBot KV 池约 11 GB（r01）；DA3-Streaming <12 GB；MapAnything 在 memory-efficient 模式下每 100 视图约 7 GB（按 140 GB/2000 视图估算）。**24 GB 卡是 V0.5 的最低配置，48 GB 更从容。**
4. **规范系和方向错误是最常见的静默 bug。** DA3 的 saddle 参考视图、W2C 与 C2W 混用、四元数 xyzw 与 wxyz、π3 无参考帧。Adapter 必须带自检（点在相机前方的比例 >0.8），CI 用 DA3-SMALL 的真实输出做回归。
5. **稀疏深度条件的分布偏移。** MapAnything 训练时见过的是随机稀疏深度，MID-360 投影是结构化的扫描花样，**效果待验证**。验证之前 V0.5 保留"后配准"（Sim3 + GICP，r02/r07）作为兜底。
6. **CSF 失效模式。** 布料过粗或刚度过高时，丘陵和平台会被当作建筑；布料过细时，大屋顶的中心会下陷到地面（大平屋顶的经典问题）。需要 v3 的立面连通分量规则，加上 UI 质量叠加层（在 Ground 与 Building 之间切换高亮）供人工复核。
7. **Mock-LIO 的乐观偏差。** 网格采样点云加切向抖动，与真实 MID-360 非重复扫描、多回波、强度相关噪声差距很大。ATE 数值只作为回归门禁（相对变化），不能作为精度宣传。
8. **Python 端算力。** 扫描生成约 31–37 ms/帧、LIO 约 9–11 ms/帧，单进程最多支撑 2 架"带 LiDAR 仿真"的无人机。多机时要降采样、降频或做 C++ 化。**浏览器端绝不做 LiDAR 光线求交**，只画 FOV 和稀疏扫描。
9. **UrbanScene3D 数据陷阱（新增）。** 虚拟城市几乎没有植被；SF 疑似 1:10 缩放（待核实）；Shenzhen 地面分层。语义规则阈值（2.5 m、35 m）默认米制，**必须在单位归一之后运行**。
10. **ROS 依赖。** Super-LIO 和 FAST-LIO2 需要 ROS2/ROS1，RKO-LIO 的 ROS 部分是可选的。后端 FastAPI 只依赖 `rko_lio` 与 `rosbags`（纯 Python），不要把 ROS 引入 API 容器。
11. **amb3r-slam 的误导风险。** 官方 `HengyiWang/amb3r-slam` 目前是占位仓库，只有 README。搜索引擎会把 `johnhenning/amb3r-slam`（第三方复现，0★）当成代码地址，不能把它的结果当作论文复现。
12. **RKO-LIO 版本。** 仓库是 0.4.1（2026-09-25，改了 deskewed_scan 话题名和帧），PyPI 最新是 0.4.0。按 wheel 版本锁定 `rko_lio==0.4.0`，升级时注意 API 变化（0.4.0 把 `pointcloud()` 改名为 `points()`）。

---

## 7. 对设计文档 01-design.md 的优化建议

与 r01 §7、r02 §7 互补，已经提过的不再重复。

1. **§5 "LingBot-Map" 改为"多引擎 + 度量条件"。** 在 r02 提出的 Engine Adapter 基础上明确四个角色：
   - LingBot-Map：在线 / 流式；
   - DA3-Streaming：离线长序列 + 回环；
   - **MapAnything：带 RTK 位姿和 LiDAR 稀疏深度的度量重建**；
   - DA3-SMALL：CPU 冒烟和 CI。
   
   另外写明 Adapter 的方向、gauge、尺度约定表（§3.1），以及 `engine.json` 字段。
2. **§6 增加"前馈融合"路径。** 原文只有"视觉几何 + LiDAR → Registration"这种后配准路线。2026 年的模型可以把 RTK 位姿和 LiDAR 深度**作为网络输入**，直接输出米制几何（MapAnything、Pi3X、WorldMirror 都支持）。建议写成两条路线并行，由 V0.5 的对照实验（§3.2）决定主路径；度量输出下 chunk 对齐用 SE(3)。
3. **§6/§33 的 LIO 选型写清楚。**
   - 机载：FAST-LIO2（Prometheus 内置）或 Super-LIO（ROS2、ARM64、重定位）；
   - 后端离线：RKO-LIO（pip，不需要 ROS）；
   - 全局：GLIM + GNSS（r07）。
   
   同时注明 FAST_LIO 主仓 2024-07 以后没有提交。
4. **§7 Semantic 分成三层并给出点类别规范。**
   - L0：几何规则（CSF/HAG/法线），MVP；
   - L1：学习型（PTv3/Utonia 线性探针），V0.5+；
   - L2：开放词汇（SAM 3 投影到 3D），V1.0。
   
   点级类别用 ASPRS LAS 编码（u8，含 64+ 自定义），Restricted Area、禁飞区等用**矢量体积语义**（`semantic/zones.geojson` + 高度区间），不要混入点类别。原文把 "Restricted Area" 和 Road、Building 并列在 Semantic 下，粒度不一致。
5. **§7 Geometry 增加一等公民 "Terrain（DTM/DSM）"。** AGL 显示、贴地飞行、WindNinja 地形风（§20 Level 2）、LiDAR 仿真兜底都依赖 DTM，原文 Geometry 列表里缺这一项。World Package 增加 `geometry/terrain/{dtm.f32, dsm.f32, terrain.json}`。
6. **§14–§15 的点云节点格式增加属性块。** 每点 `classification u8` 加可选的 `hag_q u8`。图层开关改为 GPU uniform `classMask`，不重载；LOD 可按类别加权（地面降权）。原文 §38 的 "Layers ☑ Semantic" 应细化为按类别开关。
7. **§28 DroneState 增加 `alt_agl`**（基于 DTM），与 `alt_amsl` 和 `h_ell`（r02）并列。UI 的 "Altitude 82.3 m" 要标明基准。
8. **§36 WebSocket 增加 "LiveMapDelta（0x21）" 消息**（§3.8 的 int8 体素块）。Sensor 类数据（LiDAR 扫描）要稀疏化到 2–5k 点/帧，并写入频率和带宽预算：LiDAR 显示 ≤0.3 MB/s/架，实时地图 ≤0.2 MB/s。
9. **§43 MVP 补两件低成本交付物。**
   - ① UrbanScene3D 入库时生成语义和 DTM（本机 1–2 分钟/城）；
   - ② mock MID-360 + IMU + RKO-LIO 测试床（让 V0.5 的 LIO 链路在 V0.2 就有回归测试）。
   
   可选的第三件：DA3-SMALL CPU 冒烟重建，使 "Reality → Web World" 在无 GPU 时也有真实路径。
10. **§44–§50 版本表修正。**
    - V0.1 增加"内置世界语义与地形"；
    - V0.2 增加"mock LiDAR / IMU 与 LIO 回归"；
    - V0.5 改为"度量重建（前馈融合 vs 后配准对照）+ 离线 LIO + 学习型语义 L1"；
    - V1.0 增加"质量引导补拍（OpenFlyScan 式 reacquire 任务）"和"开放词汇目标发现（SAM 3 / PE）"。后者正好服务 §32 的搜救 workflow 中"发现疑似目标 → 发布验证任务"。
11. **§13 "WebGPU 不做什么" 给出量化依据。**
    - 前馈重建即便用最小的 DA3-SMALL，CPU 上也要 3–5 s/帧；
    - VGGT-Ω 需要 6–43 GB 显存；
    - LiDAR 扫描仿真在 Python 中约 30 ms/帧。
    
    这些都应留在服务端，浏览器只负责显示。
12. **§41 World Package 增加 `reconstruction/<session>/engine.json`**（§3.1）和 `semantic/{classification.meta.json, zones.geojson}`。类别码表、规则版本（`rules: n02-v2`）和参数（布料、刚度）都要写入 meta，便于复算。
13. **数据说明补充（§43 内置数据）。** UrbanScene3D 虚拟城市只有 xyz+normal，无颜色、无植被；各城单位和 up 轴不一，SF 疑似缩放。入库必须带 QA 报告（语义比例、HAG 分布、单位推断）。"Tree" 图层在 MVP 阶段用程序化实例 mock，并在 UI 中标注为模拟数据。

---

## 附录 A：DA3 纯 CPU 推理实测（`.cache/research/n02/n02_da3_cpu.py`）

环境：8 核 CPU、torch 2.14.0+cpu、无 xformers（自动回退）、numpy 1.26.4。输入是 `assets/examples/robot_unitree.mp4`（1024×576，174 帧）均匀抽取的 16 帧。每个模型测试的第一组（n=2）包含预热开销。

| 模型 | 参数（实测） | 帧数 | process_res | 输出尺寸 | 耗时 | 每帧 | 峰值 RSS |
|---|---|---|---|---|---|---|---|
| DA3-SMALL | 34.3M | 2 | 504 | 280×504 | 20.95 s | 10.5 s | 0.9 GB |
| DA3-SMALL | 34.3M | 8 | 504 | 280×504 | 37.49 s | 4.7 s | 1.47 GB |
| DA3-SMALL | 34.3M | 8 | 336 | 196×336 | 27.11 s | 3.4 s | 1.47 GB |
| DA3-SMALL | 34.3M | 16 | 504 | 280×504 | 88.08 s | 5.5 s | 1.86 GB |
| DA3-SMALL | 34.3M | 16 | 336 | 196×336 | 51.51 s | 3.2 s | 1.86 GB |
| DA3-BASE | 135.4M | 8 | 336 | 196×336 | 23.42 s | 2.9 s | 2.63 GB |
| DA3-BASE | 135.4M | 16 | 504 | 280×504 | 173.21 s | 10.8 s | 3.1 GB |
| DA3-BASE | 135.4M | 16 | 336 | 196×336 | 84.52 s | 5.3 s | 3.1 GB |

- README 称 DA3-SMALL 为 0.08B 参数，本机 `sum(p.numel())` 实测为 34.3M，BASE 为 135.4M（README 写 0.12B）。以实测为准，差异原因未查。
- 16 帧时第 0 帧外参旋转约 3.5°、平移约 0.03，**不是单位阵**，印证了 saddle 参考视图的规范系问题（§2.2、§3.1）。
- 模型加载：SMALL 7.3 s，BASE 17.6 s（含从 HF 读取）。

## 附录 B：RKO-LIO mock 飞行（`.cache/research/n02/n02_rko_mock.py`）

- 地图：Shenzhen 5M 点，自动选出"p99 相对高度约 40 m"的 400×280 m 窗口（中心 (674, −289)），裁剪后 726k 点，飞行高度为地面以上约 60 m。
- `rko_lio==0.4.0`（PyPI wheel，cp312），`LIOConfig(voxel_size=1.0, max_range=70, min_range=0.5, initialization_phase=True)`。
- 初始化日志：用 20 条 IMU 估计初始旋转，陀螺偏置估计为 (0.0031, −0.0021, 0.0003)，真值 (0.002, −0.001, 0.0015)。
- 结果见 §3.11 表格。原始 JSON：`rko_mock_result_60s.json`、`rko_mock_result_180s.json`；日志：`rko_180.log`。

## 附录 C：城市语义规则（`.cache/research/n02/n02_semantic.py`）

- 命令：`python n02_semantic.py <ply> <tag> <cloth_res> <rigidness>`。
- 产物：`semantic_<tag>.json`（比例、耗时、HAG 分位）、`semantic_<tag>_cls.npy`（每点类别）、`semantic_<tag>_hag.npy`、`semantic_<tag>_ground.npy`、`semantic_<tag>.png`（俯视类别图 + DTM）。局部放大图 `sem_zoom*.png` 由 `n02_sem_zoom.py` 生成。
- 抬升点（非地面）的 HAG 分布：New York p50 18.8 m、p90 80.2 m、p99 174.1 m、max 283.3 m；San Francisco p50 1.6 m、p90 4.7 m、p99 14.0 m、max 26.2 m。SF 这组数据是"疑似 1:10 缩放"判断的依据。
- v1 规则（PCA 散度判植被）在 NY 上植被误报 21.3%；v2（网格法线 + 法线一致性）降到 0.03%。
