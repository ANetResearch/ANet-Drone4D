# ANet4D Python SDK 文档

本目录是 `anet4d` SDK（`tools/sdk/anet4d/`，M26-FR-080）的研究者文档，中英文两篇内容与命令一致：

| 文件 | 内容 |
|---|---|
| [quickstart.zh-CN.md](quickstart.zh-CN.md) | 快速上手：安装、编写示例 Agent、在高保真仿真档（Gazebo Sim 锁步）与快速仿真档上运行、基线策略与参考检测器、打包镜像、从源码构建、排错 |
| [quickstart.en.md](quickstart.en.md) | Quickstart, English version of the same guide |
| [examples/plaza_watch.py](examples/plaza_watch.py) | 快速上手的示例 Agent（文档中的代码即此文件全文） |
| [examples/run_quickstart.py](examples/run_quickstart.py) | 平台侧运行脚本：构造本地开发 Env，以进程内或套接字方式接入 Agent，输出摘要 |

SDK 源码、三个基线策略（`static_guard`、`coverage_scan`、`track_share`）与参考检测器 `ref_det_v1` 位于 `tools/sdk/anet4d/src/anet4d/`，随 sdist 原样发布。验证用例在 `tests/d3/agentsdk_pkg/`：构建 wheel 与 sdist，在临时 venv 中安装，以本目录的示例经线协议运行，并对本目录文档执行 copy-lint。

SDK 在 V0.3.1 及以前名为 `drone4d`；V0.3.2 起发行包与导入包均为 `anet4d`，`drone4d` 作为已弃用的兼容别名保留一个版本（导入时发出 `DeprecationWarning`）。线协议 `drone4d.bench.wire.v1` 等协议标识符为遗留标识符，保持原名。
