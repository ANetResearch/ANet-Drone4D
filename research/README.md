# 研究资料

本目录收录基于 Drone4D 平台开展的研究工作：论文稿件、实验驱动脚本、实测数据与图表脚本。研究目录只引用平台代码（`python/awr`
等），不被平台代码引用；平台发布、测试与部署不依赖本目录。

| 目录 | 内容 | 来源 |
|---|---|---|
| [`ccf-tpci/`](ccf-tpci/README.md) | 耦合决策（返航几何、风场可行性、感知缺口、委托）实验与 CCF TPCI 投稿稿件；实验驱动在 `ccf-tpci/experiments/` | 原 `paper/ccf-tpci` 分支（发布标签 `drone4d-tpci-v1`） |

约定：

- 每项研究一个子目录，含 `README.md`（复现步骤、数据集与图表对应关系）、`data/`（每次实验运行一个 Parquet 及其 `.meta.json`）、
  `experiments/`（实验驱动）与稿件源文件；
- 实验在仓库根目录运行，`PYTHONPATH=python:research/<项目>/experiments`；原始运行输出写入 `runs/`（不入库）；
- 稿件正文中引用的代码路径以对应发布标签为准。
