# 世界与数据

## World Package v1

World 是 ANet Drone4D 的核心资产：一个带版本、锚定在地点上的目录，同时服务于物理与查询（几何）和显示（点云）。`worldpkg` 把原始点云
规范化（单位、上方向、调平、北向、原点、法线、地面模型、离地高度、分类）后写成 World Package v1：

```text
worlds/<id>/
├── world.json             # 清单：名称、数据集与许可、内容版本、图层、相机初始视角、统计与质检摘要
├── coordinate.json        # 唯一度量帧 world（锚点处 ENU）的定义：锚点、北向、尺度来源、源数据变换、地面模型
├── visual/pointcloud/     # 显示用八叉树：Potree 2.0 兼容容器 + 12 字节 ANET_Q16 点编码
├── geometry/
│   ├── pointcloud/        # 规范化后的源点（供查询与派生）
│   └── terrain/           # DSM（2 m）与 DTM（10 m）栅格
├── semantic/              # 语义类别表与区域（zones.geojson，World ENU 米坐标）
├── environment/env.json   # 环境缺省：地表类别、风场参数等
└── qa/report.json         # 构建质检报告
```

几何查询服务在 World 上提供高度、离地高度、净空、视线、射线求交与路径校验，安全、规划与"点击地面即飞往"都使用它。每个世界的
`contentVersion`（构建输出中的 `cv`）由内容哈希得到，用于缓存与录制兼容判定。`make validate` 对全部世界执行深度校验与 JSON Schema 严格校验，
错误为 0 才通过。

## 七个世界

| id | 世界 | 点数 | 范围（东西 × 南北 × 高，m） | 面积 | 最高点（离地） | 来源 |
|---|---|---|---|---|---|---|
| `synthcity` | ANet Synthetic City | 3,924,500 | 1,200 × 1,200 × 355 | 1.44 km² | 352 m | 程序生成，不含第三方数据 |
| `shenzhen` | 深圳 | 5,000,141 | 1,848 × 1,999 × 391 | 3.69 km² | 374 m | UrbanScene3D |
| `shanghai` | 上海 | 5,000,202 | 7,736 × 6,211 × 646 | 48.05 km² | 637 m | UrbanScene3D |
| `newyork` | 纽约 | 5,000,065 | 2,928 × 3,167 × 293 | 9.27 km² | 287 m | UrbanScene3D |
| `sanfrancisco` | 旧金山 | 5,000,091 | 7,278 × 7,512 × 558 | 54.67 km² | 443 m | UrbanScene3D |
| `suzhou` | 苏州 | 4,999,857 | 4,407 × 687 × 156 | 3.03 km² | 152 m | UrbanScene3D |
| `chicago` | 芝加哥 | 5,000,391 | 4,176 × 8,038 × 451 | 33.56 km² | 445 m | UrbanScene3D |

六城的原始数据单位、上方向与倾斜各不相同（例如旧金山为 10.15 m/单位且需绕 Z 轴旋转 +90°，芝加哥需放大 1000 倍并调平 2.03°，苏州为 Y 轴向上），
由 ingest 统一规范化为 World ENU。六城在 CPU 上单城构建约 27 至 35 s。全部七个世界的锚点均为示意锚点（`anchor.kind = "synthetic"`）：
经纬度只用于太阳位置、天空与界面显示，不用于导航（见 {doc}`architecture`）。

## ANet Synthetic City

合成演示城市由 `python/awr/world/ingest/synthetic.py` 按固定种子生成：1.2 km × 1.2 km、约 390 万点，包含道路、河道、公园与两座 300 m 级塔楼。
它不含任何第三方数据，按本仓库的许可证发布，世界包、截图与录屏都可以自由分享。`make demo-world` 约 25 s 完成构建；本机没有六城数据时，
它是缺省世界，剧本为 S0；公开实例的共享展示在它上面运行剧本 S7。

## UrbanScene3D

内置的六座城市由 [UrbanScene3D](https://vcc.tech/UrbanScene3D)（Lin 等，ECCV 2022）的虚拟城市采样点云构建。

**数据集条款。** UrbanScene3D 的作者只允许非商业使用，禁止再分发数据或其任何改动版本，并要求使用它的工作引用原论文。因此本仓库不包含任何
UrbanScene3D 点数据，也不包含由其生成的世界包、栅格或录制；`worlds/`、`runs/` 与 `data/raw/` 永不入库。少量测试夹具只保留已构建世界的元数据
（八叉树节点计数与包围盒、清单字段、一条测试相机路径），不含点数据。`make fetch-data` 从作者的
[发布页](https://github.com/Linxius/UrbanScene3D/releases/tag/v0.0.1) 下载 `UrbanScene3D-virtual_cities-sampled.7z`，并按 `configs/data.yaml`
逐个校验 sha256；使用前请阅读并接受其条款。本项目许可证中的商用许可不延及该数据集，作者条款原文见
[THIRD_PARTY_NOTICES.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/THIRD_PARTY_NOTICES.md) 第 4 节。

**经数据集权利方授权使用。** ANet Drone4D 项目已获得 UrbanScene3D 数据集权利方的授权，在本项目自己的部署中使用由六城构建的世界包：
V0.2-demo 起，公开实例与合成演示城市一并开放六城，部署包附带六城的世界包，项目的截图也可以使用六城画面。该授权不经由本仓库或其许可证转授给使用者：
仓库仍不包含任何 UrbanScene3D 数据，自行下载时仍须遵守上述作者条款。

使用六城时请引用：

```bibtex
@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}
```

## 后续的数据来源

| 环节 | 当前 | 后续 |
|---|---|---|
| 采集 | UrbanScene3D 六城采样点云与程序生成的合成城市 | 带 RTK 的 Livox MID-360 采集会话（V0.5） |
| 重建 | Engine Adapter v2、Recon IR 与任务状态机，Mock 引擎端到端打通 | GPU 重建引擎（V0.5） |
| 世界格式 | World Package v1 与几何查询，浏览器渐进式流式加载 | COPC 与 3D Tiles 导出（V0.5）；三角网格世界资产导入与米制标定（D3） |
