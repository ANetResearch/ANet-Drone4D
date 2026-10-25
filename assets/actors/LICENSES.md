# 动态实体资产许可登记

本文件登记 ANet4D 事件演员（人员、车辆、狗、机械、物体）使用的全部外观与几何资产的来源与许可（M25-FR-180；AWR-05 §5.2.8；
docs/05 第三方资产表"外观库人物与车辆资产"行；D3-AC-62"外观库与车辆资产许可登记齐全"）。未在本文件登记的资产不得用于事件、渲染或数据集发布。

截至 D3 封板，事件与传感器渲染只使用下列项目自制的程序化资产，不含任何第三方模型、纹理、扫描或动作数据，无第三方署名义务；
随项目许可（仓库根目录 `LICENSE`，ANet Open Source License，SPDX 标识 `LicenseRef-ANet-OSL`）发布。

## 1. 外观库 appearance_lib@1

| 项目 | 内容 |
|---|---|
| 资产 id | `appearance_lib@1`（条目引用形式 `appearance_lib@1/<id>`，事件 DSL 中为 `appearance_ref: <id>`） |
| 文件 | `configs/appearance.d3/appearance_lib.v1.json`（由 `configs/appearance.d3/spec.yaml` 经 `python -m awr.sim.events.appearance build` 确定性生成） |
| 生成规格摘要 | 库文件 `generator.spec_sha256` = `2540221fd058e6f946b093f61a3f3f34a292b1947f26f20cb2a871c27828408a`，种子 20261018 |
| 条目 | 人员 24 条（`person-01` 至 `person-24`），车辆 16 条（`vehicle-01` 至 `vehicle-16`） |
| 来源 | 项目自制（kind `original`）：属性取值、调色板、几何参数与文字描述均由规格文件定义并程序生成 |
| 许可 | `LicenseRef-ANet-OSL`，权利人 Agent Network Research；库级与逐条目 `license` 字段一致，`third_party` 为空 |
| 署名 | ANet-Drone4D appearance_lib@1 (procedurally generated, project-original) |
| 校验 | `python -m awr.sim.events.appearance check`；`tests/d3/events/test_appearance.py`、`tests/d3/events/test_event_set.py` |

## 2. 程序化代理几何与骨架

| 资产 id | 类别 | 生成位置 | 来源 | 许可 |
|---|---|---|---|---|
| `person-proxy@1`（骨架 `awr-proxy-rig-person@1`） | 人员 | `python/awr/sensorsvc/camera/actors.py`（M25） | 项目自制，程序生成 | `LicenseRef-ANet-OSL` |
| `vehicle-proxy@1`（骨架 `awr-proxy-rig-vehicle@1`） | 车辆 | 同上 | 项目自制，程序生成 | `LicenseRef-ANet-OSL` |
| `dog-proxy@1` | 狗 | 同上 | 项目自制，程序生成 | `LicenseRef-ANet-OSL` |
| `machine-proxy@1` | 机械 | 同上 | 项目自制，程序生成 | `LicenseRef-ANet-OSL` |
| `object-proxy@1` | 物体（交接道具） | 同上 | 项目自制，程序生成 | `LicenseRef-ANet-OSL` |

## 3. 未使用的第三方资产

docs/05 第三方资产表中的 Khronos glTF 样例动态资产（CesiumMan、CesiumMilkTruck、Fox 等）未进入仓库，D3 封板范围内的事件、渲染与数据集
均不使用。日后引入任何第三方动态资产时，须先在本文件按资产逐项登记来源、许可与署名，并在 `assets/actors/manifest.json` 中登记类别与
归一化参数（M25-FR-180），开源资产限 CC0、CC-BY。
