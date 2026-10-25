# M34 研究者 SDK 与命令行 PRD（Researcher SDK & Command Line）

## 文档信息

| 项 | 内容 |
|---|---|
| 文档编号 | AWR-M34 |
| 标题 | 研究者 SDK 与命令行（Researcher SDK & Command Line）PRD：10 分钟上手路径、公开 Python API 与最小策略、实验配置 `drone4d.coord.experiment.v1` 全字段、命令行 `drone4d bench run/report/replay/verify` 与 `drone4d sdk pack`、PettingZoo Parallel/AEC、TorchRL、BenchMARL 适配、`drone4d` wheel 打包与命名空间重写（ADR-D4-28）、依赖附加包 `ref`/`hifi`/`train`、教程与文档站结构、兼容与弃用策略 |
| 模块与包 | M34（AWR-06 §3.3 暂定编号，表中"SDK"行）；包路径 `sdk:coord/`、`sdk:experiment/`、`sdk:adapters/`、`sdk:cli.py`；分发物 `drone4d` wheel |
| 版本 | v0.2.1（2026-10-18 文档集集成同步：按 AWR-06 v0.3 §0.5 统一裁定 XR-02 关闭 Python 下限待决项，按 XR-05 修订 R-M34-44、§7 一致性比较与 NS-08 的摘要字段名，按 M33 §13 第 2 条更正 §7.10.2 的 MCAP Sink 路径；按 XR-11 把 AWR-06 调度规则 R-S1 至 R-S8 的引用改称 SR-1 至 SR-8；按 XR-12 把进程内模块引用的方案名由 `py:` 改为 `module:`（R-M34-17、§7.4.2 `policies[].ref`、AC-M34-08）；按 XR-09 在 R-M34-05 增补 `op_seq` 的哨兵填写） |
| 日期 | 2026-10-18（UTC） |
| 状态 | D4 设计评审稿。D4 处于深入调研与概念设计阶段，D3 完整收尾并封板（G0）后才启动实现；本文全部阶段与周次以"D3 封板后第 N 周"表述，不含日历日期承诺；本文不构成实现承诺 |
| 上游文档 | [AWR-06 D4 协同实验平台概念设计](../06-D4-协同实验平台概念设计.md)（v0.2）：§0.1 第 3 条、§1.2（R-D4-10、R-D4-11、R-D4-16、R-D4-17）、§1.5 DP-11、§3.2、§3.3（SDK 行与依赖闭包表）、§3.4 B-02、B-10、§3.5（`sdk:agent.py` 行）、§3.7、§4.2、§4.3、§4.4、§4.7、§5.6、§5.9、§5.10、§8.10、§10.2–§10.6、ADR-D4-13、28、29、31、32、33、35；调研 [r01 平台能力矩阵](../research/d4/r01-平台能力矩阵.md) §3.1、§7、§8；[r03 NS-2 经验与排行榜规则](../research/d4/r03-NS-2经验与排行榜规则.md) R1、R2、R4、R10、R11、R20、R24、R29、R30；工作笔记 [n01 现有代码映射](../research/d4/notes/n01-现有代码映射.md) §2.4（G-CP-2）、§1；[n03 外部模型讨论纪要](../research/d4/notes/n03-外部模型讨论纪要.md) §2.10、§3.6；负责人前期稿 design-r2 §七、§九、§十（`/data/projs/anet-drone-data/design-r2.md`） |
| 并列文档 | [M28 实验内核](M28-实验内核PRD.md)、[M29 交互网络](M29-交互网络PRD.md)、[M30 协同接口与基线](M30-协同接口与基线PRD.md)、[M31 基准套件](M31-基准套件PRD.md)、[M32 评测与统计](M32-评测与统计PRD.md)、[M33 Trace 与复现](M33-Trace与复现PRD.md)、[S1](../d4/suites/S1-TaskAlloc规格.md)、[S2](../d4/suites/S2-CoopSearch-Handoff规格.md)、[S3](../d4/suites/S3-InfoShare规格.md)、[X1](../d4/suites/X1-RobustCoord协议.md)、[协作智能排行榜规格](../d4/协作智能排行榜规格.md)（下称 LB）、[契约草案](../d4/契约草案.md)（下称 CT）、[r04 首篇论文实验设计](../research/d4/r04-首篇论文实验设计.md)。上述文档并行撰写，本文对其引用以章节职责为准，符号与编号以各文档定稿为准 |
| 编号约定 | 需求 `R-M34-yy`；验收 `AC-M34-yy`；命名空间重写规则 `NS-yy`；YAML 载入规则 `YL-yy`；风险 `RK-M34-yy`；待决 `Q-M34-yy`；跨模块变更请求 `CR-M34-yy`；工作包、决策、验收引用 AWR-06 的 `WP-D4-xx`、`ADR-D4-xx`、`AC-D4-xx`、`DL-D4-xx`；并列模块需求以 `R-M28-yy` 等原样引用 |
| 引用约定 | 沿用 AWR-06：`py:` = `python/awr/`；`sdk:` = `tools/sdk/drone4d/src/drone4d/`；`ct:` = `packages/contracts/`；`web:` = `apps/web/src/`。本文对现有代码的陈述均已于 2026-10-18 打开文件核实，以"文件 + 符号"为准，不引用行号。§1.1 F-02 的 wheel 内容核对在会话临时目录中以源码副本构建完成，未改动仓库。`sdk:coord/`、`sdk:experiment/`、`sdk:adapters/`、`sdk:cli.py`、`tests/perf/northstar/` 目前均不存在，本文给出的类、函数、文件与命令均为新建设计 |
| 数值约定 | 标注"目标""初值""缺省"的数值为设计默认值或待实测校准的目标，不是对现有代码性能的断言；北极星相关数值以 WP-D4-43（CP-2）与 WP-D4-44（第 10 周门禁实测）为准 |
| 写作规范 | 中文、技术名词保留英文；企业级行文；表格与编号清晰；不使用 emoji 与状态字形；命令行与文档站的用户可见文案遵循项目企业级文案规范 |
| 修订记录 | v0.1（2026-10-18）：首版。v0.2（2026-10-18）：按评审意见 29 条逐条核实后修订（处理结果见 §14）。主要变更：vendoring 映射表增补 `awr.swarm.allocation`、`awr.swarm.coverage`，`py:sim/events/tape.py` 移出 wheel，NS-01 覆盖函数体内导入；实验级摘要改名 `experiment_config_sha256`，与逐 Episode 的 `config_sha256`（M27/M28 口径）分离，`episode_id` 不再依赖 `experiment_id`，`--policies` 改为执行计划过滤；哨兵 epoch 改由 SDK 侧决策定稿函数解析（与 CT §4.3.2 一致）；公开 API 区分 M30 协议与 SDK 便利基类，`claim`/`release`/`dist` 改为自由函数，`send` 对齐 CT `SendRequest`；受限 YAML 改为替换隐式解析器的自定义 Loader（YL-01 至 YL-12）；新增 `protocol` 块、`instances.block`，`evaluation.ci.*` 与 M32 对齐；新增策略预检、策略失败告警与退出码 24；`sdk pack`/`sdk check` 对齐 LB 扫描项、CT `submission.v2` 与 `containers.py` 参数；新增 §7.7.1 一致性比较集、§8.5 跨模块变更请求、§9.4 工作量估算；`cli_result` 契约按 CT §9 第 18 项更名为 `drone4d.coord.cli_result.v1`；表格单元格内的竖线全部转义 |

---

## 0. 摘要

1. **定位**。M34 是 D4 第 L7 层"研究者接口"（AWR-06 §3.2）的交付模块：它不实现实验语义，而是把 M28–M33 的能力以一个可 `pip install` 的 `drone4d` wheel、一组公开 Python API 与一个命令行交付给研究者。M34 的成功标准是 AWR-06 的北极星门禁（ADR-D4-29）：在参考机 RM-8 上，自 `pip install drone4d` 完成起 10 分钟内跑通"20 个 Agent × 100 个外生种子单元 × 4 个算法"的对比实验，得到带 95% 置信区间的指标表、相对 `π_ind` 的配对差与可回放 trace，全程不克隆仓库、不安装 Gazebo、不打开三维页面。
2. **10 分钟上手路径**。路径固定为 6 步：`drone4d env`（环境自检）→ `drone4d bench init --template northstar`（生成实验配置与 12 行的示例策略）→ 阅读策略 → `drone4d bench run --dry-run`（预检与墙钟估算）→ `drone4d bench run`（≤ 6 min，8 worker）→ 打开自包含的 `report/index.html`。模板与 `tests/perf/northstar/` 的门禁配置逐字节一致，使"文档里的路径"与"门禁实测的路径"是同一条路径（§4）。
3. **公开 API**。`drone4d.coord` 按 M30 §7.2 的模块划分再导出协议（`PolicyProtocol`、`CentralPolicy`、`Solver`、`CoordinationPolicy`、`Replannable`）与 `ct:coord/` 数据类（含 `CoordContext`、`WakeDecision`），另提供带缺省实现的便利基类 `Policy`，以及 `goto`、`hold`、`send`、`claim`、`release`、`dist` 等自由函数，不在冻结数据类上挂方法（M30 §12 第 15 条）；`drone4d.experiment` 提供 `Experiment.load/resolve/run` 的编程入口。最小可运行策略为 AWR-06 §4.2 的 `NearestTask` 按自由函数改写后的版本，非空非注释行 12 行，满足 DP-11 的"不超过 20 行"。AWR-06 示例中 `goto(ent, pos)` 缺少 `assignment_epoch`：构造函数写入 SDK 内部哨兵 `EPOCH_CURRENT`，由 SDK 侧的决策定稿函数在决策离开策略边界之前以同一次观测中的租约 epoch 替换，哨兵不出现在线上帧与 trace 中（R-M34-05，与 CT §4.3.2 一致）。
4. **实验配置**。`drone4d.coord.experiment.v1` 以 YAML 编写、以 JCS 规范化 JSON 为规范形式；本文给出全部字段的类型、缺省、约束、是否计入实验级摘要 `experiment_config_sha256` 与排名约束（§7.4；逐 Episode 运行清单中的 `config_sha256` 沿用 M27-FR-007 与 M28 口径，二者不同名），规定受限 YAML 载入规则 YL-01 至 YL-12（基于 PyYAML 的自定义 Loader，以 JSON 数值文法替换隐式解析器；拒绝 YAML 1.1 隐式布尔、六十进制、八进制、二进制、数字分隔符、重复键、别名与合并键、`NaN`/`Inf`），以及"解析 → 校验 → Helper 展开 → 兼容规则 → 引用解析 → 属性解析 → Episode 展开"的固定流水线。本地运行只允许 `split = dev` 且 `ranked = false`。
5. **命令行**。单一程序 `drone4d`，按优先级分批：P0 必须交付 `bench run`、`bench report`；P0 应交付 `bench init`、`bench run --dry-run`、`env`、`version` 与 M31 请求的 `bench suite list/show/check`、`bench instance gen/check`（薄封装）；P1 交付 `bench replay`、`bench verify`、`sdk pack`、`sdk check`、`sdk serve`、`bench baseline list/show`。平台侧命令（`drone4d suite verify/audit/freeze/...`）不进入 wheel，由完整平台发行版 `awr` 经入口点组 `drone4d.cli.v1` 注册为插件。退出码沿用 19 号 §16.2 的编号空间并新增 21–24；`--json` 输出统一为 `drone4d.coord.cli_result.v1`（CT §9 第 18 项）。
6. **训练框架适配**。PettingZoo `ParallelEnv` 是同步适配器：一次 `step()` 对应一个密封决策周期，经 M28 的外部步进接口 `advance_to_decision()`/`submit()` 驱动；AEC 由 PettingZoo 官方转换得到，是轮转外观而非异步网络；TorchRL 适配张量 spec，BenchMARL 经 TorchRL 接入，训练库不进入内核。空间映射、`message_vec` 的 int8 量化、固定 Agent 集合加 mask、奖励与 `state()` 的特权限制均在 §7.8 给出精确规则。训练产物经 `TensorPolicyAdapter` 与 `drone4d sdk pack` 打包为 `wire.v2` 评测容器，评测一律走 `drone4d bench run` 的同一协议，不使用训练库自带的评测循环。
7. **wheel 与命名空间重写**。wheel 为纯 Python 的 `py3-none-any`，由 `tools/sdk/build_wheel.py` 按映射表把 `awr.kernel` 等核心包、B-10 白名单中的 D3 纯函数模块与核心基线所需的 `awr.swarm.allocation`、`awr.swarm.coverage` 子集（M30 §12 第 12 条）以词法级 import 重写方式 vendoring 为 `drone4d._core.*`，不含顶层 `awr` 包；`py:sim/events/tape.py` 因函数体内导入 `awr.environment.*`、`awr.sim.fleet.*` 且读取依赖 zstandard，不进入 wheel（F-20、§13 第 22 条）。本文规定重写规则 NS-01 至 NS-08，导入闭包检查覆盖函数体内的导入，其中三条来自核实到的具体风险：`py:sim/sensors/__init__.py` 依据环境变量 `AWR_PLUGINS` 条件导入插件，中间包 `__init__` 必须生成而非复制；`py:sim/sensors/cbrng.py` 的 `_KC_NAME = __package__ + ".kernels_cbrng"` 依赖包名；凡进入摘要、trace 或检查点的模块路径字符串都会破坏"仓库路径与 wheel 路径事件哈希逐字节一致"的 CI 判据。构建产出 `SOURCES.json` 溯源清单，同一提交两次构建的 wheel 字节一致。
8. **依赖与附加包**。附加包只声明依赖、不携带代码（pip extras 的固有语义），全部模块随基础 wheel 分发并延迟导入，缺依赖时报 `extra_missing` 与安装提示。核实发现 numpy 2.5.3 的 `Requires-Python` 为 `>=3.12`，与 ADR-D4-28 的 CPython 3.11 支持冲突；YAML 解析与 `wire.v2` 分别需要 PyYAML 与 msgpack，与"运行时依赖只有 numpy"冲突。本文建议 wheel 下限为 CPython 3.12、运行时依赖为 numpy、PyYAML、msgpack（内核导入闭包仍只有 numpy，B-02 不变），并列入对 AWR-06 的反馈。
9. **文档站与兼容**。D4 文档进入现有 `docs-site`（Sphinx + sphinx-rtd-theme + MyST，中英双语强制同构），以 `0.4` 版本路径与现有内容并存；教程以 `steps.sh` 单一来源被两种语言页面引用，并在干净环境 CI 中逐步执行（AC-D4-46、AC-D4-48）。版本方案为 wheel 版本与产品版本同线（`0.4.Z`），P1 门（第一赛季开启）发布首个稳定版 `0.4.0`，之前为预发布；弃用须保留至少 2 个后续发布且不少于 6 个月，赛季进行中不得破坏评测容器所依赖的 SDK 面。
10. **规模**。需求 R-M34-01 至 R-M34-107，验收 AC-M34-01 至 AC-M34-37，风险 8 条，待决 10 条，跨模块变更请求 17 条（§8.5），对 AWR-06 的反馈 28 条（§13），评审处理记录 29 条（§14）；其中须在 WP-D4-01 实验契约 RFC 中裁决的有：Python 下限、运行时依赖集合、`hifi` 的分发形态、包名预留与索引配置、版本号方案。

---

## 1. 背景与目标

### 1.1 约束本模块的事实

以下事实均已打开对应文件核实。

| 编号 | 事实 | 对设计的含义 | 依据 |
|---|---|---|---|
| F-01 | 现有 SDK 的 `pyproject.toml`：`name = "drone4d"`、`version = "0.2.0.dev0"`、`requires-python = ">=3.11"`、`license = { text = "Apache-2.0" }`、`dependencies = ["httpx>=0.27", "msgpack>=1.0"]`、附加包 `images = ["numpy>=1.26", "pillow>=10"]`；无 `[project.scripts]`，无包数据声明，`build-system` 为 `setuptools>=69` | 包名 `drone4d` 已被 D3 SDK 占用并保留（ADR-D4-28）；D4 的变更是在同一发行版上增加模块与入口点，而非新建发行版；现有硬依赖 httpx、msgpack 与"运行时依赖只有 numpy"直接冲突 | `tools/sdk/drone4d/pyproject.toml` |
| F-02 | 以现有 SDK 源码副本构建 wheel（setuptools 84.0.0、`--no-build-isolation`）得到 `drone4d-0.2.0.dev0-py3-none-any.whl`，包内除 `.py` 外只有 `dist-info` 的 4 个文件：`perception/ref_det_v1.npz`（76 KB）与 `perception/model_card.json` 未被打包，`dist-info` 中无许可文件 | 包数据必须显式声明并以内容清单校验（R-M34-73）；D3 从未经过"安装 wheel"路径，因为 D3 Agent 基础镜像直接复制源码目录（F-05）；许可文件须随 wheel 分发（ADR-D4-32） | 会话临时目录中的构建结果；`tools/sdk/drone4d/src/drone4d/perception/` |
| F-03 | `sdk:bench/__init__.py` 在包导入时执行 `from drone4d.bench.client import BenchClient, ...`，而 `sdk:bench/client.py` 在模块顶层 `import httpx`；`sdk:local.py` `make()` 在函数内导入 `awr.agentsdk.env.core.BenchEnv`，失败时抛 `RuntimeError("drone4d.bench.make needs the Drone4D repository environment (awr package)")` | httpx 改为附加包后 `import drone4d.bench` 必须延迟导入客户端；`make()` 是 D3 仓库内开发入口，不属于 wheel 的独立能力，文档须明确标注 | `sdk:bench/__init__.py`、`sdk:bench/client.py`、`sdk:local.py` `make` |
| F-04 | `sdk:wire.py`：`PROTOCOL = "drone4d.bench.wire.v1"`，帧头 `struct.Struct("<IBBH")`，编码时帧头版本字段写 1，模块顶层 `import msgpack`；`sdk:serve.py` 提供 `python -m drone4d.serve pkg.module:Factory` 与 `--baseline NAME`、`--sock PATH`，缺省套接字 `/run/drone4d/agent.sock`（亦可经 `DRONE4D_AGENT_SOCK`） | `wire.v1` Agent 侧原样保留（B-06）；`wire.v2` 的 Agent 侧以新增参数 `--protocol v2` 接入同一入口，容器入口保持 `python -m drone4d.serve` 不依赖控制台脚本；`wire.v2` 同样需要 msgpack | `sdk:wire.py` `PROTOCOL`、`_HDR`、`encode_frame`；`sdk:serve.py` `main` |
| F-05 | D3 容器 Agent：`py:agentsdk/env/containers.py` `AgentContainers.argv` 以 `--network none`、`--read-only`、`--tmpfs /tmp:rw,size=256m`、`--cap-drop ALL`、`--security-opt no-new-privileges`、`--pids-limit 256`、`--cpus`、`--memory`、`--user 65534:65534` 启动并挂载 `/run/drone4d`，`AgentContainers.__init__` 拒绝不符合 `^[A-Z][A-Z0-9_]{0,63}$` 或以 `DRONE4D_` 开头的 `entry.env` 键；`py:bench/runner.py` `AGENT_IMAGE = "d3-agent-py:dev"`，策略分 `reference`（`--baseline <name>`）与 `image`（按摘要引用，入口取 `entry.cmd`、`entry.env`）两类；基础镜像 `tools/deploy/dmax/images/agent/Dockerfile` 以 `python:3.12-slim` 为底、离线安装 msgpack/numpy/pillow 的本地 wheel、复制源码目录、设单线程 BLAS 环境变量与 `USER 65534:65534`；示例提交 `tools/bench/d3/examples/close_track/Dockerfile` 以 `FROM d3-agent-py:dev` 加 `CMD ["python", "-m", "drone4d.serve", "close_track.agent:Agent"]` | `drone4d sdk pack` 沿用该镜像约定（非 root、无网络、只读根、单线程数值库、离线依赖），并改为安装 wheel 而非复制源码；本地 `sdk check` 的容器参数与 `entry.env` 键规则以 `containers.py` 为规格自含实现（wheel 不能导入 `awr`） | 所列文件 |
| F-06 | 仓库根 `pyproject.toml`：`name = "awr"`、`version = "0.2.0"`、`requires-python = ">=3.12,<3.13"`；依赖精确锁定，含 `numpy==2.5.3`、`pyyaml==6.0.3`、`msgpack==1.2.2`、`scipy==1.18.1`、`jsonschema==4.26.0`；`[project.scripts]` 为 `worldpkg` 与 `awr`；文件头注明 scripts 属 AWR-03 §4.1 冻结片段、依赖增删须向 M00 提交请求 | 平台侧命令以入口点插件注册须经 M00（R-M34-29）；wheel 的核心依赖与仓库锁同主版本 | `pyproject.toml` |
| F-07 | 仓库 `.venv` 中 numpy 2.5.3 的 `METADATA` 为 `Requires-Python: >=3.12`；msgpack 1.2.2 为 `>=3.10`；PyYAML 6.0.3 为 `>=3.8` | 在 CPython 3.11 上无法安装与仓库锁定版本相同的 numpy；ADR-D4-28 的"3.11 与 3.12 支持矩阵"和"与仓库锁定版本同主版本"在 3.11 上只能以更旧的次版本成立，跨解释器的数值一致性无法由同一 numpy 构建保证（§13 第 1 条） | `.venv/lib/python3.12/site-packages/{numpy,msgpack,PyYAML}-*.dist-info/METADATA` |
| F-08 | 仓库 ruff 配置 `target-version = "py312"`，启用规则集含 `UP`（pyupgrade），作用于 `python`、`tools`、`tests` | 若保留 3.11 支持，D4 包与被 vendoring 的模块须单独以 3.11 为目标检查，否则自动修复可能引入 3.12 专有语法；选择 3.12 下限则无此问题 | `pyproject.toml` `[tool.ruff]`、`[tool.ruff.lint]` |
| F-09 | 拟 vendoring 的模块 `py:sim/sensors/cbrng.py`、`py:sim/events/{jcs,tape,errors}.py`、`py:phys/{__init__,adapter,pose_history}.py` 均可按 3.11 语法解析（`ast.parse(feature_version=(3, 11))`）；`py:phys/adapter.py` 使用 `enum.StrEnum`（3.11 起提供），`cbrng.py` 使用 `itertools.pairwise`（3.10 起）；该核对只覆盖语法，不覆盖导入闭包（闭包见 F-20、F-21） | 语法层面不阻碍 3.11；阻碍来自依赖（F-07）；进入 wheel 的判据是导入闭包而非语法 | 所列文件 |
| F-10 | `py:sim/sensors/__init__.py` 在环境变量 `AWR_PLUGINS` 列出 `awr.sim.sensors` 时转而导入 `.plugin`；`py:sim/sensors/cbrng.py` 以 `_KC_NAME = __package__ + ".kernels_cbrng"` 在 `sys.modules` 中查找 numba 外积核，未导入时走 numpy 路径（结果逐位相同）；同文件 `warmup()` 以 `importlib.import_module(_KC_NAME)`（非字面量参数）装载该核，只由栈 A 插件安装时调用 | vendoring 时 `awr/sim/sensors/__init__.py` 必须生成为无副作用的空包（NS-05）；vendored `cbrng` 的包名变为 `drone4d._core.sim.sensors`，numba 路径永不启用，这一事实须登记（NS-04）；`warmup()` 在 wheel 中调用会因目标模块不存在而失败，登记于 NS-03/NS-04 白名单并由 lint 保证 wheel 内无调用点 | `py:sim/sensors/__init__.py`；`py:sim/sensors/cbrng.py` `_KC_NAME`、`warmup` |
| F-11 | `py:phys/__init__.py` 只定义 `PhysError` 与模块说明；`py:sim/__init__.py` 只有模块说明；`py:sim/events/__init__.py` 为 1 行 | `awr.phys` 的 `__init__` 可按原样复制（白名单），其余中间包生成空包 | 所列文件 |
| F-12 | 高保真构件的导入链：`py:agentsdk/env/l1.py` 导入 `awr.agentsdk.env.camera`、`awr.agentsdk.env.geom`、`awr.agentsdk.rng`；`py:phys/kinematic.py` 导入 `awr.phys.adapter`、`awr.phys.pose_history`、`awr.phys.tracker`；`py:world/mesh/query.py` 导入 `awr.world.geometry.query` 与 `awr.world.geometry.types` | ADR-D4-28 的 vendoring 清单（`cbrng`、`jcs`、`tape` 及其错误类型、`phys.adapter`、`phys.pose_history`）不覆盖 `hosts/hifi` 所需模块；`drone4d[hifi]` 仅靠声明附加依赖无法在不克隆仓库的环境中工作（§13 第 4 条） | 所列文件 |
| F-13 | `python/awr/__init__.py` 规定 `awr` 为唯一 Python 命名空间、入口一律 `python -m awr.<pkg>`（ADR-050） | wheel 不得包含顶层 `awr`；平台侧命令的规范入口为 `python -m awr.<pkg>`，`drone4d suite ...` 只是插件别名 | `python/awr/__init__.py` |
| F-14 | 文档站 `docs-site/`：Sphinx、sphinx-rtd-theme、MyST；`_shared/conf_common.py` 中 `VERSIONS = ["latest"]`，`release` 读自仓库根 `pyproject.toml`；扩展为 `myst_parser`、`sphinx_copybutton`、`drone4d_api`、`drone4d_site`，未启用 autodoc；`docs-site/README.md` 规定两种语言文档名一一对应、新增页面须两种语言同时增加否则构建失败、快速开始中的每段输出都来自真实运行；`drone4d_api` 扩展以 `api_summaries.json` 承载说明译文 | D4 文档须同时产出中英文页面并引入版本路径；API 参考自动生成需要在文档构建环境中安装 wheel；英文说明沿用 `api_summaries.json` 的译文登记方式（R-M34-86、R-M34-87） | `docs-site/README.md`、`docs-site/_shared/conf_common.py`、`docs-site/requirements.txt` |
| F-15 | 现有 `docs-site/zh_CN/api/sdk.md` 的标题为"Python SDK"，内容是 D2 沙盒接口的单文件 SDK `awr_sandbox` | D4 文档中 `drone4d` SDK 的页面标题与路径须与之区分，避免同名 | `docs-site/zh_CN/api/sdk.md` |
| F-16 | `tools/lint/check-version.mjs` 只核对 `pyproject.toml`、`apps/web/package.json`、`package.json` 三处产品版本，不覆盖 `tools/sdk/drone4d/pyproject.toml` | wheel 版本与产品版本的一致性须新增检查（R-M34-92） | `tools/lint/check-version.mjs` `SOURCES` |
| F-17 | 仓库测试以 `sys.path.insert(0, str(ROOT / "tools/sdk/drone4d/src"))` 导入 SDK（`tests/d3/sdk/test_sdk_frames.py`、`tests/d3/env/test_wire.py`）；`.venv` 中 `awr` 以可编辑方式安装，`drone4d` 未安装 | 仓库内开发态与安装态是两条路径；`drone4d._core` 在开发态须由同一重写脚本生成（R-M34-75），不得以"开发态回退到 `awr.*`"的方式实现 | 所列文件；`.venv/lib/python3.12/site-packages/__editable__.awr-0.1.0.pth` |
| F-18 | `py:agentsdk/env/core.py` `BenchEnv` 以 `self.possible_agents` 提供 PettingZoo 风格的 Agent 列表，`step` 返回 `dict.fromkeys(obs, 0.0)` 作为奖励；仓库内无 pettingzoo、gymnasium、torchrl、benchmarl 依赖 | D3 只有"PettingZoo 风格"，没有可被训练框架直接使用的环境（n01 §2.4 G-CP-2）；训练适配器与其依赖是 D4 的新增，奖励定义须由 Suite 提供（§13 第 12 条） | `py:agentsdk/env/core.py` `BenchEnv.step`；仓库依赖清单 |
| F-19 | `tests/perf/northstar/`、`py:kernel/`、`py:coordination/`、`sdk:coord/`、`sdk:adapters/`、`sdk:cli.py` 均不存在 | 本模块的全部 API 与命令为新建设计 | 目录列表 |
| F-20 | `py:sim/events/tape.py` 模块顶层只导入 numpy、`awr.sim.events.errors` 与 `awr.sim.events.jcs`，但函数体内另有 5 处 `awr.*` 导入：`TapeReader._env_track` 导入 `awr.environment.keyframe`（其顶层导入 msgpack 与 `awr.environment` 的 `anchors`、`sky`、`weather`、`wind` 子模块）与 `awr.environment.weather.presets`（导入 `awr.contracts`），`env_at` 导入 `awr.environment.weather` 与 `awr.environment.weather.transitions`，`actor_states` 导入 `awr.sim.fleet.stages.kinematic`（经 `..kernels_tap` 导入 `awr.world.georef.frames` 与 numba 可选的 `kernels_l1`）；`write_tape` 与 `TapeReader._sub` 在函数内导入 zstandard | AWR-06 §3.3 闭包表把 tape 记为"numpy、进入核心"不成立；若进入 wheel，NS-01（覆盖函数体内导入）必然使构建失败，若放行则 `env_at`、`actor_states` 在 wheel 中运行时 ImportError；tape 只服务 S2 网格族与磁带通信区，二者依赖网格世界，属仓库环境与 `hosts/hifi`；本文将 tape 移出 wheel（§7.10.2、§13 第 22 条） | `py:sim/events/tape.py` `_env_track`、`env_at`、`actor_states`、`_sub`、`write_tape`；`py:environment/keyframe.py`；`py:environment/weather/presets.py`；`py:sim/fleet/stages/kinematic.py`；`py:sim/fleet/kernels_tap.py` |
| F-21 | `py:swarm/coverage/__init__.py` 在包导入时导入 `lanes`、`partition`、`plan`、`sensor`，`partition.py` 导入 `..allocation.hungarian`；`py:swarm/allocation/__init__.py` 导入 `hungarian`，后者在首次调用时才装载 scipy；`score.py` 只依赖标准库；`gsd.py`、`levels.py` 不被 `__init__` 导入；`py:swarm/__init__.py` 只有模块说明 | M30 的核心基线（`ssi`、`cbba` 依赖 `score.py`；S2 `π_ind` 与 `partition_lawnmower` 依赖 `lanes`、`partition`，M30 §8.2、R-M30-47）要求这两个包进入映射表；两个包的 `__init__` 带子模块导入，必须原样复制而非生成空包（NS-05 例外），且其导入的子模块须全部在映射内 | 所列文件；M30 §12 第 12 条 |
| F-22 | 仓库 `.venv` 中 PyYAML 6.0.3 的 `yaml.safe_load` 实测：`0o17`、`1e3`、`1.0e3`、`1e-3`、`-1E+3`、`1.5e3` 解析为字符串；`1.0e+3`、`1.0E-3` 解析为浮点；`1_000` 为 1000、`0b11` 为 3、`010` 为 8、`0x1F` 为 31、`1:30` 为 90；`.inf`、`.nan` 为非有限浮点；`2020-01-01` 为 `date`；`yes`、`on`、`Yes`、`TRUE` 为 `True`，`y` 为字符串 | `SafeLoader` 的隐式解析器同时存在"过度解析"（八进制、二进制、六十进制、数字分隔符）与"解析不足"（无小数点或无符号指数的科学计数法），受限 YAML 不能在其输出上做事后检查，须替换隐式解析器并在 compose 阶段检查标量文本（R-M34-12、§7.4.3） | 会话内以 `.venv/bin/python` 执行 `yaml.safe_load` 的实测 |

### 1.2 目标

| 编号 | 目标 | 度量 |
|---|---|---|
| G1 | 北极星门禁：干净环境中自 `pip install drone4d` 完成起 10 分钟内完成 §4 的路径并得到报告、配对差与可回放 trace | AC-D4-11、AC-D4-12；AC-M34-01、02 |
| G2 | 研究者只需一种原生语义（`Policy.step(obs, inbox)`）与一个命令（`drone4d bench run`）即可完成对比实验 | AC-M34-03、04 |
| G3 | 实验配置完整、可校验、可复现：同一配置在任意 worker 数下产出逐字节一致的结果文件 | AC-M34-06 至 09 |
| G4 | MARL 研究者可在 PettingZoo、TorchRL、BenchMARL 中训练，并以同一评测协议评测训练产物 | AC-D4-28、DL-D4-01；AC-M34-20 至 24 |
| G5 | 仓库路径与 wheel 路径语义一致：同一实验在两条路径上的事件摘要、真值摘要、评测分量与统计结果逐字节一致 | AC-M34-26、27 |
| G6 | 外部团队按文档独立接入，文档中的每一步在干净环境 CI 中执行 | AC-D4-29、AC-D4-46、AC-D4-48；AC-M34-31、32 |
| G7 | 公开 API、命令行、实验配置与线协议有成文的兼容承诺与弃用流程，赛季进行中不破坏提交者 | AC-M34-33 至 35 |

### 1.3 非目标

1. 不实现时间推进、调度、网络、观测、评测、统计与 trace 格式（M28–M33）；M34 只调用其公开接口。
2. 不提供 REST/gRPC 研究接口。AWR-06 R-D4-10 的 REST 入口由容器线协议与 P2 只读接口承载（ADR-D4-13），不属于本模块。
3. 不在 wheel 中提供平台侧能力：留出集冻结、秘密种子、榜单重算与审计、提交受理（M31 平台工具、M32、LB）。
4. 不提供提交上传客户端。P1 的提交由平台既有渠道受理，`drone4d sdk pack` 只产出镜像与 `submission.v2` 草稿（§13 第 18 条记录该边界）。
5. 不承诺跨硬件等级的位级一致；本地运行与平台运行在同一硬件等级且依赖构建相同时才可期望位级一致（§5.6 R-2a）。
6. 不修改 D3 SDK 的既有行为：`drone4d.agent`、`drone4d.wire`（v1）、`drone4d.baselines`、`drone4d.perception` 的接口与输出不变；对 `drone4d.bench` 的改动限于延迟导入，其中 httpx 改为附加包是唯一的依赖集合例外（R-M34-82）。

### 1.4 术语（本文新增，其余见 AWR-06 术语表）

| 术语 | 定义 |
|---|---|
| 实验目录 | 一次 `drone4d bench run` 的输出根目录，含解析结果、Episode 索引、各运行目录、逐 Episode 分量、分析结果与报告（§7.7） |
| 开发态 / 安装态 | 开发态指在仓库中以 `awr.*` 源码运行；安装态指在干净环境中以已安装的 `drone4d` wheel 运行，核心代码位于 `drone4d._core.*` |
| vendoring | 构建 wheel 时把仓库源码复制并重写导入路径后放入 `drone4d._core` 的过程；源码权威位置不变 |
| 映射表 | `tools/sdk/vendor.toml` 中"源模块前缀 → 目标模块前缀"的登记与排除清单 |
| 附加包 | pip 的 extras（`drone4d[ref]` 等）；只增加依赖，不改变 wheel 内容 |
| 张量画像（tensor profile） | 训练适配器的固定形状与截断规则（`Kmax`、`Tmax`、`Mmax`、`D`、`F`、`G`、`D_msg` 与导航动作形式）；Suite 声明者优先，未声明者取 §7.8.2 的缺省规则 |
| 打包上下文 | `drone4d sdk pack` 生成的构建目录（Dockerfile、本地 wheel、源码副本、锁文件） |
| 哨兵 epoch | `EPOCH_CURRENT = -1`；SDK 内部值，由构造函数写入 `EntityCommand.assignment_epoch`，由 SDK 侧决策定稿函数在决策离开策略边界前以同一次观测中的租约 epoch 替换，不出现在线上与 trace 中（R-M34-05） |
| 一致性比较集 | 判定"逐字节一致"时的比较对象与豁免项（§7.7.1） |
| 执行计划过滤 | 只决定本次运行哪些 Episode、不改变实验内容与标识的选择（`--policies`、续跑跳过已完成项，R-M34-32） |
| 平台侧插件 | 由完整平台发行版 `awr` 经入口点组 `drone4d.cli.v1` 注册、只在仓库环境可用的 `drone4d` 子命令组 |

---

## 2. 范围

### 2.1 分阶段范围

| 阶段 | 范围 | 工作包 | 门禁关联 |
|---|---|---|---|
| P0（第 1–11 周） | `drone4d.coord` 公开 API 与构造函数；`drone4d.experiment` 载入、校验、解析与批量运行；命令 `bench run`、`bench report`（必须），`bench init`、`bench run --dry-run`、`env`、`version`、`bench suite list/show/check`、`bench instance gen/check`（应交付）；北极星模板与 `tests/perf/northstar/`；wheel 构建、命名空间重写、依赖闭包校验、支持矩阵 CI、许可扫描与 SBOM、受邀私有包索引发布（预发布 `0.4.0aN`）；文档站快速入门、教程一与 API 参考 | WP-D4-08（第 5–8 周）、WP-D4-44（第 9–10 周）、WP-D4-45（第 8–10 周）；WP-D4-01 中的包名、许可与依赖裁决 | AC-D4-11、12、43、46、47；P0 门 |
| P1（第 12–21 周） | `bench replay`、`bench verify`；`sdk pack`、`sdk check`、`sdk serve --protocol v2`；PettingZoo Parallel/AEC、TorchRL、BenchMARL 适配与 `TensorPolicyAdapter`；`bench baseline list/show`；`bench suite check --gate`；教程二至五、迁移指南、FAQ、贡献与版本策略页面；外部复现支持；P1 门发布首个稳定版 `0.4.0` | WP-D4-24（第 15–17 周）、WP-D4-47（第 18–20 周）、WP-D4-49（第 14 周）、WP-D4-30（第 19–21 周） | AC-D4-28、29、48；DL-D4-01；P1 门 |
| P2（第 22–31 周） | 公共 PyPI 发布（须负责人批准）；`hifi` 的分发形态按 Q-M34-03 裁决结果实施；`fast` 物理档与异构实体在实验配置中的开放 | WP-D4-40；WP-D4-34、35 的配置面 | P2 门 |

### 2.2 与其他模块的边界

| 模块 | M34 依赖其提供 | M34 向其提供 | 边界规则 |
|---|---|---|---|
| M28 实验内核 | `EpisodeSpec`、`InprocHost`、`BatchHost`、外部步进接口 `Kernel.advance_to_decision()`/`submit()`（R-M28-86）、`privileged_state()`（R-M28-87）、`AttrRegistry.export_schema()`（R-M28-35）、运行清单与确定性等级、`hw_class` 探测、`wire.v2` 编解码（WP-D4-04） | `EpisodeSpec` 列表的构造（实验解析结果）、输出目录、命令行 `--set`/`--attr` 到属性层 `cli`（R-M28-32 已设该层）的映射 | 命令行不绕过内核直接构造世界或实体；`hosts.eval` 不进入 wheel；M34 对宿主 API 与运行清单的增补需求见 §8.5 CR-M34-01 至 05 |
| M29 交互网络 | 网络档 id、参数集 `params_ref` 与参数的 `AttrSpec` 登记 | `network.overrides` 的校验调用 | 未登记参数一律拒绝 |
| M30 协同接口与基线 | `py:coordination/api.py`、`policy.py`（`sdk:coord/` 的同源定义）、决策定稿函数 `finalize_decision`（§8.5 CR-M34-06）、观测构造函数（两种宿主共用）、`msgschema`、登记表 `resolve(policy_ref)` 与索引导出（R-M30-50、R-M30-57）、MARL 训练配方（IPPO、MAPPO、QMIX） | `drone4d.coord` 公开 API 的发布与稳定性承诺、训练框架适配器、`drone4d sdk pack`、`bench baseline` 命令 | `sdk:coord/` 再导出的内容以 M30 为准，所有权在 M34（M30 §2.3）；对 M30 的增补需求见 §8.5 CR-M34-06 至 08 |
| M31 基准套件 | Suite 与实例的本地发现、生成、校验 API；`dev` 公开种子派生；Suite 清单中张量画像、训练奖励与特权状态的取值（字段名已由 CT §5.1.1 登记，取值由各 Suite 声明，§8.5 CR-M34-09、10）；各自包数据清单（CR-M34-11） | `bench suite`、`bench instance` 命令（R-M31-77）；`dev` 场景集的本地展开 | 平台侧命令（R-M31-78）不进入 wheel |
| M32 评测与统计 | `analyze_experiment(rows, ...)`（R-M32-78）、`Row` 结构、本地报告的数据结构 | `rows.jsonl` 的汇编与 md/html/csv 渲染 | 本地报告标注"本地分析，非正式成绩"（M32 §2.2） |
| M33 Trace 与复现 | `TraceReader`、`render_report`、`InputTape`、`diverge`、`verify_run`、`build_replay_bundle`（M33 §7.1）、trace 级别取值域 | `bench report`、`bench replay`、`bench verify` 的参数解析、退出码与输出格式 | M33 不读 `sys.argv`（M33 §2.2） |
| CT 契约草案 | `ct:coord/experiment.v1` 的最终 schema；`init.v1`/`observation.v1`/`decision.v1`/`wire.v2` | 本文 §7.4 的字段需求与 §8 的新增契约提议（`cli_result.v1`、`pack_lock.v1`） | 契约文件由 CT 起草、M00 合入 |
| LB 排行榜规格 | `submission.v2` 字段与提交约束 | `submission.v2` 草稿生成 | M34 不提交、不访问排行榜服务 |
| M00 | 仓库根 `pyproject.toml` 的入口点登记、依赖锁 | 平台侧插件入口点请求（R-M34-29） | 跨所有权变更请求，经 M00 合入 |
| M26（D3 SDK 原所有者） | `sdk:agent.py`、`sdk:wire.py`、`sdk:serve.py`、`sdk:baselines/`、`sdk:perception/`、`sdk:bench/` | `sdk:serve.py` 的 `--protocol` 增补、`sdk:bench/__init__.py` 的延迟导入 | 加法式改动；D3 回归门 B-07 |

### 2.3 路径所有权（M34）

| 路径 | 内容 |
|---|---|
| `tools/sdk/drone4d/src/drone4d/coord/` | 公开 API 再导出、构造函数、观测视图（`sdk:coord/`） |
| `tools/sdk/drone4d/src/drone4d/experiment/` | 实验载入、受限 YAML、校验、解析、展开、运行与结果汇编（`sdk:experiment/`） |
| `tools/sdk/drone4d/src/drone4d/adapters/` | `pettingzoo_parallel.py`、`pettingzoo_aec.py`、`torchrl_env.py`、`benchmarl_task.py`、`tensor_policy.py`、`legacy_obs_v1.py`（后者为 M30 投影器的 SDK 面再导出） |
| `tools/sdk/drone4d/src/drone4d/cli.py`、`tools/sdk/drone4d/src/drone4d/_cli/` | 命令行入口与子命令实现 |
| `tools/sdk/drone4d/src/drone4d/templates/` | `bench init` 模板（`northstar/`、`policy/`、`solver/`、`central/`） |
| `tools/sdk/drone4d/pyproject.toml` | D4 增补部分（入口点、附加包、包数据、构建后端版本） |
| `tools/sdk/build_wheel.py`、`tools/sdk/vendor.toml` | wheel 构建与命名空间重写 |
| `tools/sdk/images/d4-agent-py/` | `wire.v2` 评测容器基础镜像的构建定义 |
| `tests/d4/sdk/**` | 单元、命令行、wheel、适配器测试 |
| `tests/perf/northstar/**` | 北极星门禁配置、示例策略与执行脚本（WP-D4-08；内核分项探针属 M28） |
| `docs-site/{zh_CN,en}/research/**`、`docs-site/_shared/tutorials/**` | D4 研究平台文档页与可执行教程源（与文档站维护者共有，构建配置改动经其评审） |

---

## 3. 用户与用例

| 编号 | 用户 | 用例 | 涉及命令或 API | 验收 |
|---|---|---|---|---|
| UC-01 | 新接触平台的算法研究者 | 按快速入门在 10 分钟内跑通北极星实验并读懂报告 | `env`、`bench init`、`bench run`、`report/index.html` | AC-M34-01、02 |
| UC-02 | 经典协同算法研究者 | 修改示例策略或编写 `Policy`，与平台基线在 S1 上做配对对比 | `drone4d.coord`、`bench run` | AC-M34-03、04 |
| UC-03 | 运筹研究者 | 只提供 `Solver`，在算法隔离模式比较求解器 | `bench init --template solver`、`bench run` | AC-M34-04 |
| UC-04 | 实验设计者 | 扫描网络参数与决策时延，复用同一实例与种子做配对比较 | `--attr`、`--set`、`network.overrides` | AC-M34-08 |
| UC-05 | 复现者 | 拿到他人的实验目录，离线重算指标并逐字节比对；动作重放定位发散 | `bench verify`、`bench replay` | AC-M34-14、15 |
| UC-06 | MARL 研究者 | 用 BenchMARL 训练 MAPPO，在 PettingZoo/TorchRL 中自定义训练循环 | `drone4d.adapters.*` | AC-M34-20 至 23 |
| UC-07 | 提交者 | 把训练产物或自研策略打包为 `wire.v2` 容器，本地冒烟后生成提交草稿 | `sdk pack`、`sdk check` | AC-M34-17 至 19 |
| UC-08 | D3 迁移用户 | 继续运行 D3 `DroneAgent`；按迁移指南改写为 `Policy` | `python -m drone4d.serve`、迁移指南 | AC-M34-33 |
| UC-09 | Suite 作者 | 在本地生成与校验实例包、检查 Suite 包门禁 | `bench suite check`、`bench instance gen/check` | AC-M34-16 |
| UC-10 | 平台维护者 | 发布 wheel，确认仓库路径与安装路径一致、依赖闭包与许可合规 | `tools/sdk/build_wheel.py`、发布流水线 | AC-M34-25 至 30 |

---

## 4. 十分钟上手路径

### 4.1 路径定义

北极星的计时自 `pip install drone4d` 完成起算（AWR-06 §5.10），总预算 10 min，其中实验墙钟 ≤ 6 min，其余 4 min 用于自检、生成、阅读、预检与报告。下表为门禁路径，文档站快速入门与 `tests/perf/northstar/run.sh` 执行同一序列。

| 步 | 操作 | 命令 | 时间预算（RM-8） | 产物 | 判据 |
|---|---|---|---|---|---|
| 0 | 安装（不计入） | `python3.12 -m venv .venv && . .venv/bin/activate && pip install --pre "drone4d>=0.4.0a1,<0.5"`（预发布期间的写法；`run.sh` 以 `drone4d==<被测版本>` 精确固定，R-M34-91） | 记录不计入；目标 ≤ 60 s | 只含 `drone4d` 与其运行时依赖的环境 | 不克隆仓库，不安装附加包 |
| 1 | 环境自检 | `drone4d env` | ≤ 5 s | 版本、解释器、numpy 构建、主进程 SIMD 派发集合、worker 等级 `hw_class`（经 worker 启动路径探测，R-M34-51）、附加包状态、容器运行时 | worker 等级 `hw_class = hwc-x86-64-v3`；为 `unclassified` 时提示"结果只可与平台做统计比较" |
| 2 | 生成工程 | `drone4d bench init --template northstar ta-quickstart && cd ta-quickstart` | ≤ 5 s | `experiment.yaml`、`my_policy.py`、`README.md` | 两个文件与 `tests/perf/northstar/` 中同名文件逐字节一致 |
| 3 | 阅读策略 | 打开 `my_policy.py` | ≤ 2 min | — | 12 个非空非注释行（§7.3） |
| 4 | 预检 | `drone4d bench run experiment.yaml --dry-run` | ≤ 10 s | 解析摘要：400 个 Episode、`experiment_id`、预计墙钟（逐项标注来源）、`synthetic-stress` 标签、策略预检结果 | 预计墙钟 ≤ 6 min 且逐项标注估算来源（R-M34-22）；`mine` 通过策略预检（R-M34-105）；不启动完整 Episode |
| 5 | 运行 | `drone4d bench run experiment.yaml`（门禁脚本另加 `--fail-on-policy-error`） | ≤ 6 min（8 worker） | `runs/ta-quickstart/`：各运行目录、`rows.jsonl`、`analysis.json`、`report/` | 退出码 0；`manifest.experiment.json` 中 `failed_infra = 0` 且 `failed_policy = 0` |
| 6 | 查看结果 | 打开 `runs/ta-quickstart/report/index.html`（或 `drone4d bench report runs/ta-quickstart` 重渲染） | ≤ 30 s | 指标表、配对差、判定词、回放链接 | 报告自包含，不访问网络 |

合计预算约 8 min 50 s，余量用于阅读报告。步骤 5 的 `outputs.report` 已含 `md` 与 `html`，步骤 6 的重渲染为可选。

### 4.2 后续路径

| 路径 | 起点 | 命令或页面 | 阶段 |
|---|---|---|---|
| 改写策略 | 教程一"编写一个协同策略" | 修改 `my_policy.py`，`drone4d bench run experiment.yaml --policies mine,cbba` | P0 |
| 求解器 | `drone4d bench init --template solver` | 算法隔离模式的 `Solver` 示例 | P0 |
| 复现与核验 | 指南"复现与核验" | `drone4d bench verify`、`drone4d bench replay` | P1 |
| MARL 训练 | 教程四"在 BenchMARL 中训练" | `pip install "drone4d[train]"` | P1 |
| 打包与提交 | 教程五"打包评测容器" | `drone4d sdk pack`、`drone4d sdk check` | P1 |
| 编写 Suite 与网络档 | 教程二、三 | `drone4d bench suite check`、`bench instance gen` | P1 |

### 4.3 输出格式（示意）

`drone4d bench run` 在标准错误输出进度，在标准输出打印结束摘要。下列为格式定义，尖括号内为占位；文档站中的输出块一律取自 RM-8 上的真实运行（F-14）。

```
drone4d <版本> · kernel <kernel_version> · hw_class <hw_class>
experiment ta-quickstart · id <experiment_id> · 400 episodes · 8 workers
[ <完成数>/400 ] <已用时>  预计剩余 <时长>
完成：400 / 平台失败 0 / 策略失败 0 / 平台重跑 <n> · 墙钟 <时长>
本地分析，非正式成绩 · 网络参数标签 synthetic-stress
场景数 10 低于正式成榜下限 12，区间与判定仅供参考 · 资源指标为进程计量（近似计量）
  policy            U (95% CI)              Δ vs static_partition (95% CI)    判定
  static_partition  <值> [<下>, <上>]        —                                 参考
  ...
报告：runs/ta-quickstart/report/index.html
```

---

## 5. 功能需求

优先级 P0 为阻塞门禁的必须项，P1 为应交付项；"阶段"指交付所在的 P0/P1/P2。

### 5.1 公开 Python API 与最小策略

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-01 | `drone4d.coord` 是研究者唯一需要导入的协同 API，按 M30 §7.2 的模块划分再导出：(1) 协议（M30 `py:coordination/policy.py`）：`PolicyProtocol`（即 M30 的 `runtime_checkable` 协议 `Policy`）、`WakeOnRx`（P1）、`CentralPolicy`、`Solver`、`CoordinationPolicy`、`Replannable`；(2) 数据类（M30 `py:coordination/api.py`）：`AgentContext`、`TeamContext`、`CoordContext`、`Observation`、`Decision`、`EntityCommand`、`SendRequest`、`DeliveredMessage`、`TxReceipt`、`InteractionOp`、`InteractionEvent`、`ReportRequest`、`BeliefSnapshot`、`Assignment`、`FrameRef`、`ProblemView`、`SolveBudget`、`Plan`、`WakeDecision`。上述符号以 M30 为单一来源，经 vendoring 产出，不在 SDK 中另行维护副本；`CallbackAdapter` 属 M30 `py:coordination/hosting.py` 的宿主内部接口，SDK 不再导出（§8.5 CR-M34-07） | P0 | P0 | AC-M34-03 | AWR-06 §3.3 SDK 行、§4.2；ADR-D4-13；M30 §2.3、§6.2、§7.2 |
| R-M34-02 | 便利基类：`drone4d.coord.Policy` 由 SDK 定义（`sdk:coord/base.py`），是 `PolicyProtocol` 的显式子类：`reset` 与 `close` 缺省为空操作，`step` 未覆盖时在 `reset` 阶段报 `policy_step_missing`；未继承基类的鸭子类型对象以 `isinstance(obj, PolicyProtocol)` 的结构检查接受；其余协议不提供便利基类 | P0 | P0 | AC-M34-03 | AWR-06 §4.2 示例 `class NearestTask(Policy)`；M30 §6.2 |
| R-M34-03 | 构造函数（自由函数）：`goto(entity_id, pos, *, speed_mps=None, valid_for_ns=None, epoch=EPOCH_CURRENT)`、`vel(entity_id, v_w, *, ...)`、`hold(entity_id, ...)`、`follow_path(entity_id, waypoints, ...)`、`sensor_point(entity_id, sensor_id, target, ...)`、`drive(entity_id, ...)`（P2）、`send(dst, payload, *, schema="opaque", channel="default", max_age_ns=0, priority=4, flow_id=None, source_sample_time_ns=None)`、`broadcast(payload, **kw)`。`send` 的字段名与单位与 CT §4.1.1 `SendRequest` 一致：时间一律为 int64 纳秒（R-M30-05），`max_age_ns = 0` 取网络档缺省 TTL；`info_class` 不作为参数，由 `schema` 推导（`opaque` 对应 `opaque`，登记 schema 取其 `x-awr-info-class`，CT §4.1.1）；`payload` 为 `bytes` 时原样发送，为映射时须同时给出非 `opaque` 的登记 schema，以 M30 `msgschema.encode`（JCS）编码并经 `msgschema.validate` 校验，未给 schema 时抛 `SdkUsageError`（`payload_schema_missing`） | P0 | P0 | AC-M34-03 | AWR-06 §4.2 `EntityCommand`、`SendRequest`；CT §4.1.1；R-M30-05 |
| R-M34-04 | 观测便捷面一律为自由函数：`claim(task_id)`、`release(task_id)` 返回 `InteractionOp`，`dist(a, b)` 返回二维或三维欧氏距离；`self_state` 与 `tasks` 的元素为 Suite 声明的只读视图数据类（由 M31 定义、经 `drone4d.suites.<suite>` 发布），只含字段、不含方法；进程内宿主与容器宿主交给策略的 `Observation` 由同一个观测构造函数（M30 `py:coordination/` 所有，经 vendoring 同时服务 `drone4d.serve`）构造，规范化后逐字节相同，策略不得依赖对象身份（R-M30-04）；便捷面只做投影与构造，不引入观测之外的信息 | P0 | P0 | AC-M34-03 | AWR-06 §4.2 最小策略；I-01；M30 §7.2 表末行、§12 第 15 条；R-M30-04 |
| R-M34-05 | 哨兵 epoch：构造函数缺省写入 SDK 内部哨兵 `assignment_epoch = EPOCH_CURRENT (-1)`；决策离开策略边界之前，由 M30 提供的决策定稿函数 `finalize_decision(obs, decision)`（§8.5 CR-M34-06）以同一次 `Observation.assignments` 中本 Agent 对该实体的 `ACTIVE` 租约 epoch 替换；本 Agent 在该次观测中对该实体无有效租约时，以观测中该实体可见的最大 epoch（不可见时取 0）填写，由内核按 R-M28-62 以 `no_assignment` 或 `stale_epoch` 回执，SDK 不另设原因码；进程内路径（`make_policy` 包装）与容器路径（`drone4d.serve` 编码前）调用同一函数，哨兵不出现在线上帧、trace 与运行清单中（CT §4.3.2：`assignment_epoch ≥ 0`）；显式给出 `epoch ≥ 0` 时原样保留。`claim`、`release` 等构造的 `InteractionOp` 的 `op_seq`（CT §4.3.4，AWR-06 v0.3 XR-09）同样以哨兵写入，由 `finalize_decision` 按本 Agent 在本 Episode 内的单调计数器依决策内次序填写 | P0 | P0 | AC-M34-05 | AWR-06 §4.2（`EntityCommand.assignment_epoch` 为必填，示例 `goto(ent, t.pos)` 未给出）；CT §4.3.2；R-M28-44、62；§13 第 11 条 |
| R-M34-06 | 最小可运行策略：AWR-06 §4.2 的 `NearestTask` 以模板 `templates/northstar/my_policy.py` 发布，非空非注释物理行 ≤ 20（现为 12 行，计数规则见 §7.3），无需 `drone4d.coord` 之外的导入即可运行 | P0 | P0 | AC-M34-03 | DP-11；AC-D4-12 |
| R-M34-07 | `drone4d.experiment` 编程入口：`Experiment.load(path, *, sets=(), attrs=())`、`Experiment.from_mapping(doc, *, base_dir)`、`resolve() -> ResolvedExperiment`、`ResolvedExperiment.run(*, out_dir=None, workers="auto", resume=False, progress=None) -> ExperimentResult`、`ExperimentResult.report(formats)`；命令行 `bench run` 是该入口的薄封装，二者对同一输入产出逐字节一致的实验目录（墙钟命名空间除外） | P0 | P0 | AC-M34-06 | r03 R1（配置为主、Python 为辅） |
| R-M34-08 | 进程内调试入口：`drone4d.experiment.debug_episode(resolved, episode_key, *, policies=None) -> EpisodeResult` 以 `hosts.inproc` 运行单个 Episode，支持断点调试；该入口只用于非排名运行，结果与批量运行同一 Episode 逐字节一致 | P1 | P0 | AC-M34-06 | AWR-06 §3.7 进程内宿主 |
| R-M34-09 | 版本常量：`drone4d.__version__`（wheel 版本）、`drone4d.KERNEL_VERSION`、`drone4d.coord.API_LEVEL`（公开 API 面的整数级别，每次增加公开符号或参数加 1）；三者写入每个运行清单的 `versions`（键 `drone4d`、`kernel`、`drone4d_api_level`，后者与 `distribution` 键待 §8.5 CR-M34-02 确认） | P0 | P0 | AC-M34-33 | ADR-D4-35 |
| R-M34-10 | 公开面清单：`drone4d.coord`、`drone4d.experiment`、`drone4d.adapters.*`、`drone4d.suites.*`（只读视图类型）、`drone4d.kernel`（再导出 M28 的公开类型）为稳定面；`drone4d.coord.experimental` 为预览面；`drone4d._core` 与 `drone4d._cli` 为内部面；每个公开模块定义 `__all__`，API 参考只覆盖稳定面与预览面 | P0 | P0 | AC-M34-33 | ADR-D4-28（2）、ADR-D4-35 |

### 5.2 实验配置与解析

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-11 | 实验配置以 `drone4d.coord.experiment.v1` 为唯一 schema，接受 `.yaml`/`.yml`（受限 YAML，§7.4.3）与 `.json`；规范形式为文档的 JCS 字节（`py:sim/events/jcs.py` `jcs`），`spec_sha256 = sha256(JCS(文档))` | P0 | P0 | AC-M34-07 | AWR-06 §4.4；r03 R1 |
| R-M34-12 | 受限 YAML 载入规则 YL-01 至 YL-12（§7.4.3）以基于 PyYAML 的自定义 Loader 实现：以 `SafeLoader` 为基类，清空并替换其隐式解析器（只保留 JSON 数值、布尔与 `null` 三类），在 compose 阶段逐个检查未加引号的标量文本，命中歧义模式即报错；不在 `safe_load` 的输出上做事后检查（F-22）；违反任一规则报 `yaml_restricted` 或其细分码并给出行列号；载入器不执行任何构造器标签 | P0 | P0 | AC-M34-07 | 本文 §7.4.3；F-22 |
| R-M34-13 | schema 校验由自含校验器执行，不依赖 jsonschema（B-02）；`ct:coord/experiment.v1.schema.json` 只允许使用自含校验器支持的关键字子集（§7.4.4），CI 在仓库环境中以 jsonschema 对同一组正反例交叉验证，二者判定一致 | P0 | P0 | AC-M34-07 | B-02；ADR-D4-33 |
| R-M34-14 | 全部字段按 §7.4.2 实现；未知字段一律报错 `field_unknown`（含"该字段可能来自更新版本，请核对 `requires.drone4d`"的提示）；字段级错误一次报出全部，不在第一个错误处停止 | P0 | P0 | AC-M34-07 | AWR-06 §4.6 Experiment |
| R-M34-15 | 解析流水线固定为：载入 → schema 校验 → Helper 展开（`track` 简写、`agents.template`、`clock_profile` 等）→ 兼容规则（`spec_rules` 语义的自含求值器，规则由 Suite 清单与 `experiment.v1` 的 `x-rules` 提供）→ `policy_ref` 解析（M30 登记表）→ 属性解析（M28 `AttrRegistry.resolve`，层次见 R-M28-32）→ Episode 展开；任一阶段失败即停止并报告阶段名 | P0 | P0 | AC-M34-07 | AWR-06 §4.2 属性系统规则、§4.4 组合校验 |
| R-M34-16 | 本地运行约束：`ranked` 必须为 `false` 或缺省；`split` 只允许 `dev`（`validation` 的外生种子为冻结时生成的秘密种子且真值不分发，`holdout`、`stress` 只在平台侧），违反时报 `split_platform_only`；`policies[].ref` 允许 `baseline:`、`contrib:`、`community:`（含 M30 §7.5 以 `+` 连接的重规划包装器复合引用，如 `baseline:robust/event@1+taskalloc/cbba@1`）与 `py:`（若 WP-D4-01 采纳 M30 §12 第 14 条的 `module:` 写法，二者在 `0.4.x` 内同义），`image:sha256:` 仅在 P1 且检测到容器运行时时允许 | P0 | P0 | AC-M34-08 | M31 §4.4、§4.5；R-M30-54；§13 第 10 条 |
| R-M34-17 | `module:` 引用的模块搜索路径为实验配置文件所在目录加 `sys.path` 原值，不隐式包含当前工作目录；该路径经批量宿主的 worker 初始化参数传入（§8.5 CR-M34-01）。来源记录写入解析结果的 `provenance` 节：顶层单文件模块只记录该文件的 sha256；包内模块记录包根目录的有序文件摘要，排除实验输出目录（`outputs.dir` 与缺省的 `runs/`）、隐藏目录、`__pycache__/` 与 `*.pyc`，文件数上限 1,000（超出时报警告并只记录模块文件）；该摘要即 `module:` 策略的 `policy_impl_sha256`，进入 `episode_id`（R-M34-21） | P0 | P0 | AC-M34-08 | r03 R24 |
| R-M34-18 | 命令行覆盖：`--set KEY=VALUE`（以点路径覆盖规格字段，如 `seeds.repeats=2`）与 `--attr PATH=VALUE`（以属性路径覆盖，如 `/net/abstract-disk/range_m=300`）；覆盖以属性层 `cli`（R-M28-32）记录在 `experiment.resolved.json`，计入 `experiment_config_sha256` 与受影响 Episode 的 `config_sha256`；`VALUE` 以 JSON 标量语法解析，非法时报 `override_invalid` | P0 | P0 | AC-M34-08 | R-M28-32；r03 R11 |
| R-M34-19 | 摘要分三项，名称不复用：(1) 实验级 `experiment_config_sha256` 覆盖影响 Episode 结果的实验内容，即 §7.4.2 中"计入摘要 = 是"的字段的解析值与 `attrs_digest`（R-M28-33）；`policies[]` 按 `name` 排序后计入（JCS 保留数组顺序，故先排序再规范化），书写顺序不计入；`name`、`metadata`、`requires`、`evaluation`、`outputs`、`execution` 不计入；(2) 逐 Episode 的 `config_sha256` 沿用 M27-FR-007 与 M28 §7.2.1 的口径（规格去掉种子与标识后的摘要，用于同配置多种子聚合），由 M28 写入运行清单，M34 不另行定义；(3) `analysis_config_sha256` 覆盖 `evaluation` 的解析值、`policies[]` 的书写顺序与本次分析所含的策略集合。三项均写入实验级清单；`experiment.v1`、实验级清单与运行清单中不出现同名不同义的字段 | P0 | P0 | AC-M34-09 | M27-FR-007、M27 §0 第 2 条；M28 §7.2.1；R-M28-33 |
| R-M34-20 | Episode 展开：`(policy, scenario, seed_index, repeat, intervention)` 的笛卡尔积，再按 M32 `InterventionPlan` 增删干预对照 Episode；参考策略（`reference.independent`、`reference.central`）的 Episode 在多个被测策略之间共享，不重复运行；展开结果按键的码点序排列，与输入中列表的书写顺序无关（`policies[]` 的书写顺序只决定报告的列顺序） | P0 | P0 | AC-M34-09 | AWR-06 §4.6 Episode；M32 `InterventionPlan` |
| R-M34-21 | 本地标识：`experiment_id = "x" + experiment_config_sha256[:12]`；`episode_id = sha256(JCS({config_sha256, scenario_id, seed_index, exogenous_seed, repeat, intervention, policy_impl_sha256}))[:16]`，其中 `config_sha256` 为该 Episode 的 M28 口径摘要，`exogenous_seed` 为 `dev` 公开种子值，`policy_impl_sha256` 对 `py:` 策略取 R-M34-17 的来源摘要、对登记引用取基线卡登记的实现版本摘要（与源码路径无关，仓库路径与 wheel 路径相同）；`episode_id` 不含 `experiment_id`，只依赖 Episode 自身内容；索引 `episodes.jsonl` 记录 `EpisodeKey` 到 `episode_id` 的映射；排名运行的标识由平台签发，以 CT §3.1 为准 | P0 | P0 | AC-M34-09 | R-M28-89；CT §3.1 |
| R-M34-22 | 预检 `--dry-run`：完成解析流水线与策略预检（R-M34-105），输出 Episode 数、各策略的 Episode 数、`experiment_id`、`synthetic-stress` 等标签、所需附加包与预计墙钟；不创建运行目录，只可选写出 `plan.json`。预计墙钟 `T̂ = Σ_e n_dec(e) · (c_plat(e) + c_pol(e)) / W_eff + T_fixed`：`n_dec` 为该 Episode 的决策次数（时域 / Δd）；`c_plat` 按（Agent 数、网络档、物理档、时钟档）查 M28、M29 登记的平台分项预算表（AWR-06 §5.10）并按 `hw_class` 换算；`c_pol` 对平台基线取 M30 基线卡登记的每决策目标值，对 `py:` 策略取预检实测（首个场景前 20 次决策耗时的中位数），给出 `--assume-ms MS` 时以该值代替实测；`W_eff = min(workers, 物理核数)`；`T_fixed` 取 R-M34-99 的命令行开销目标。输出逐项标注来源（登记预算、基线卡、预检实测、用户假设）与"估算"字样 | P0 | P0 | AC-M34-10 | AWR-06 §5.10 分项预算 |

### 5.3 命令行总则

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-23 | 单一程序 `drone4d`，由 `[project.scripts] drone4d = "drone4d.cli:main"` 提供，亦可 `python -m drone4d` 调用；命令树、参数、退出码与阶段见 §7.6 | P0 | P0 | AC-M34-11 | AWR-06 §4.4；R-D4-11 |
| R-M34-24 | 解析库为标准库 `argparse`；`drone4d --help` 与各子命令的 `--help` 不导入 numpy 与 `drone4d._core`；子命令模块在调度时才导入 | P0 | P0 | AC-M34-12 | 本文 §6 R-M34-98 |
| R-M34-25 | 输出分离：机器可读结果写标准输出，进度与日志写标准错误；`--json` 时标准输出只有一个 `drone4d.coord.cli_result.v1` 文档（§8.2）；`--quiet` 只抑制进度；日志格式 `--log-format text\|json` | P0 | P0 | AC-M34-11 | — |
| R-M34-26 | 退出码沿用 19 号 §16.2 的编号空间，本模块新增 21 `VERIFY_MISMATCH`、22 `RUN_INCOMPLETE`、23 `PLATFORM_ONLY`、24 `POLICY_FAILED`，中断为 130；完整表见 §7.6.3；任何非零退出码伴随一个原因码（snake_case）与一条可执行的修复提示 | P0 | P0 | AC-M34-11 | 19 号 §16.2 |
| R-M34-27 | 原因码以 snake_case 名称为权威，数值码段由 CT 与 17 号登记（M34 候选 880–899，§7.6.4）；登记前只在 `--json` 输出与日志中使用名称 | P0 | P0 | AC-M34-11 | M28 §7.3.1 的码段惯例（700–729） |
| R-M34-28 | 人类可读消息提供中文与英文两套，按 `DRONE4D_LANG`、其次 `LANG` 选择，缺省英文；原因码、JSON 字段与文件内容与语言无关；文案遵循项目企业级文案规范，不使用感叹号与 emoji | P1 | P0 | AC-M34-11 | 项目文案规范；`docs-site/README.md` 编写约定 |
| R-M34-29 | 插件子命令组：入口点组 `drone4d.cli.v1`，值为 `register(subparsers)` 可调用对象；完整平台发行版 `awr` 以该组注册 `suite`（`verify`、`audit`、`freeze`、`calibrate`、`retire`、`reveal-verify`），其规范入口仍为 `python -m awr.benchmarks.platform`（ADR-050）；未安装 `awr` 时执行 `drone4d suite ...` 返回 23 `PLATFORM_ONLY`；插件装载失败只影响该组；第三方插件不得注册 `bench`、`sdk`、`suite`、`env`、`version` 组名 | P1 | P1 | AC-M34-13 | AWR-06 §4.4（平台侧命令不进入 wheel）；R-M31-78；F-06、F-13 |
| R-M34-30 | 环境变量只允许影响不改变结果的行为（缓存目录 `DRONE4D_CACHE_DIR`、语言 `DRONE4D_LANG`、颜色 `NO_COLOR`、容器运行时 `DRONE4D_CONTAINER_RUNTIME`）；任何影响结果的设置必须经实验配置或 `--set`/`--attr` 进入属性层；`NPY_DISABLE_CPU_FEATURES` 等硬件等级相关变量由批量宿主按 `hw_class` 设置（R-M28-88），命令行不读取用户自设值作为结果输入 | P0 | P0 | AC-M34-08 | DP-03；r03 R2 |

### 5.4 `drone4d bench run`

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-31 | `drone4d bench run <experiment> [--out DIR] [--workers N\|auto] [--policies a,b] [--set ...] [--attr ...] [--dry-run] [--assume-ms MS] [--resume] [--force] [--fail-on-policy-error] [--json]` 依 §5.2 解析后经 `hosts.batch`（缺省）或 `hosts.inproc`（`execution.host = inproc`，仅 `workers = 1`）运行全部 Episode，完成后汇编 `rows.jsonl`、调用 M32 `analyze_experiment`、渲染报告 | P0 | P0 | AC-M34-01、06 | AWR-06 §4.4；R-D4-11 |
| R-M34-32 | `--policies` 是执行计划过滤，不是配置覆盖：只运行所列策略及其所需参考策略的 Episode，不进入属性层，不改变 `experiment_config_sha256`、`experiment_id` 与任何 `episode_id`；所选子集写入 `plan.json` 与实验级清单的 `plan.selected_policies`；子集运行可写入同一实验目录（须 `--resume`，R-M34-33），已完成的其他策略 Episode 保留并在后续运行中复用，未运行者在 `episodes.jsonl` 中记为 `pending`；`rows.jsonl` 与 `analysis.json` 只覆盖已完成 Episode，`analysis.json` 记录所含策略集合，该集合计入 `analysis_config_sha256` | P1 | P0 | AC-M34-06、36 | M28 R-M28-32（§8.5 CR-M34-04） |
| R-M34-33 | 输出目录：缺省 `outputs.dir`，再缺省 `runs/<name>`（相对实验配置文件目录）。目录非空且不含本工具的标记文件 `manifest.experiment.json`，或标记文件中的 `experiment_config_sha256` 与本次不同时拒绝（`out_dir_not_empty`，退出码 2）；标记文件与本次相同而未给 `--resume` 时拒绝并提示 `--resume` 或 `--force`（`out_dir_has_results`，退出码 2）；`--force` 只在目录含本工具标记文件时清空后重建，且拒绝清空实验配置文件所在目录本身、其任一祖先目录、`$HOME` 与文件系统根（`out_dir_unsafe`，退出码 2）；运行期间持有 `.drone4d.lock`（`fcntl.flock`），被占用时退出码 11 | P0 | P0 | AC-M34-06 | 19 号 §16.2（11 BUSY） |
| R-M34-34 | 续跑 `--resume`：实验级清单的 `experiment_config_sha256` 与本次解析结果不一致时拒绝（`resume_config_mismatch`，退出码 10）；逐 Episode 判定以 `episodes.jsonl` 与该 Episode 的运行清单为准：`trace.complete = true`、`episode_id` 一致且运行清单 `config_sha256` 与本次为该 Episode 构造的 `EpisodeSpec` 的摘要一致者视为已完成，其余（含中断时在途的 Episode）删除其运行目录后以初始尝试序号重建；续跑后的实验目录与一次跑完的结果在 §7.7.1 一致性比较集上逐字节一致 | P0 | P0 | AC-M34-06 | R-M28-89 |
| R-M34-35 | 失败处置：平台失败按 M28 R-M28-90 重跑一次并计数；重跑仍失败的 Episode 记 `FAILED_INFRA`，实验整体完成后以退出码 22 返回并在报告中列出；策略失败（`failure.policy`）按 Suite `invalid_policy` 计分，缺省不改变退出码，但按 R-M34-106 告警；给出 `--fail-on-policy-error` 时任一策略失败使退出码为 24（平台失败与策略失败并存时取 22） | P0 | P0 | AC-M34-06 | AWR-06 §8.6 失败计分；R-M28-90 |
| R-M34-36 | 结果次序无关：`rows.jsonl` 按 Episode 键排序；§7.7.1 一致性比较集在 worker 数 1、4、8 下逐字节一致 | P0 | P0 | AC-M34-09 | R-M28-89；SR-7 |
| R-M34-37 | 进度：标准错误输出完成数、已用时与预计剩余时间；`--json` 时进度以 JSON Lines 写标准错误；进度刷新间隔 ≥ 1 s；进度不写入实验目录的摘要文件；完成数取自批量宿主的迭代式结果流（§8.5 CR-M34-01） | P1 | P0 | AC-M34-11 | B-05 |
| R-M34-38 | 中断：首次 SIGINT 停止派发新 Episode、等待在途 Episode 结束（上限 30 s）后写出部分索引并以 130 退出；第二次 SIGINT 立即终止 worker；中断后的目录可 `--resume`；取消经批量宿主的取消接口实现（§8.5 CR-M34-01） | P1 | P0 | AC-M34-06 | — |
| R-M34-105 | 策略预检：派发任何 Episode 之前，对每个 `py:` 策略在首个场景、首个种子上以 `hosts.inproc` 执行导入、实例化、`reset` 与前 20 次决策（不足 20 次时至终止），任一步抛出异常、返回值不通过 `DecisionValidator` 的结构检查或单次决策超过 10 s 即以 `policy_preflight_failed`（退出码 2）中止，并输出异常摘要与源位置；预检结果不写入实验目录的摘要文件，其每决策耗时供 R-M34-22 估算；`--dry-run` 同样执行预检；平台基线与 `image:` 引用不预检 | P0 | P0 | AC-M34-06、10 | 本文；北极星步骤 4、5 |
| R-M34-106 | 策略失败告警：运行中任一策略出现策略失败的 Episode 时，在标准错误输出该策略的失败数与首个失败的原因码；运行结束时在结束摘要、报告首屏与 `--json` 的 `warnings[]` 中列出（`policy_failures`），并给出失败率；实验级清单分别记录 `failed_infra` 与 `failed_policy` | P0 | P0 | AC-M34-06 | 本文 |

### 5.5 `drone4d bench report`

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-39 | `drone4d bench report <experiment_dir> [--format md,html,csv,json] [--runs] [--out DIR]` 由 `rows.jsonl` 调用 M32 `analyze_experiment` 重新生成 `analysis.json` 与报告；不重跑任何 Episode；`--runs` 另为每个运行调用 M33 `render_report` 生成运行级报告 | P0 | P0 | AC-M34-02 | AWR-06 §4.4；R-M32-78；R-M33-70 |
| R-M34-40 | 实验级报告章节：概况（Suite 与摘要、赛道、时钟档、物理档、网络档与标签、确定性等级分布、trace 级别、Episode 计数与失败）；主表（各策略 `U` 与资源指标的点估计与 95% 区间、名次区间）；配对差表（相对 `reference.independent` 与"组首"的差值、95% 区间、Holm 校正后的判定词"显著且实际更优""等价""未判定"）；干预对照（若有 `ind`、`cut`）；失败与无效明细；环境块（版本、`hw_class`、numpy 构建、依赖锁）；回放与运行级报告链接 | P0 | P0 | AC-M34-02 | AWR-06 §5.10 产物、§8.6 判定词 |
| R-M34-41 | 标注：报告首屏固定显示"本地分析，非正式成绩"；任一网络参数集带 `synthetic-stress` 时显示该标签与"不得外推为真实无线环境的结论"；场景数 `S < 12` 时显示 M32 的样本量提示；`clock_profile = d4_quick` 时显示"快速档结论未经 FC-7 检验不得声称可迁移"；资源类指标（如 S1 `cpu_ratio`）在本地运行中来自 `process_time`，列名旁固定标注"近似计量"（R-M32-58） | P0 | P0 | AC-M34-02 | DP-07；R-M32-78；AWR-06 §5.1 |
| R-M34-42 | 静态 HTML 自包含：脚本、样式与图形内联，不加载外部资源；配色与图表遵循 [15 视觉设计规范与色卡](../15-视觉设计规范与色卡.md)；同一 `analysis.json` 渲染的 HTML 逐字节一致（不含生成时刻，生成时刻只写入 `report/meta.json`） | P0 | P0 | AC-M34-02 | R-M33-72 |

### 5.6 `drone4d bench replay` 与 `drone4d bench verify`

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-43 | `drone4d bench replay <run_dir> [--out DIR] [--until T_NS] [--diff]` 执行动作重放：以 M33 `InputTape.from_trace` 构建输入磁带，经 M28 以重放策略替换 Agent 运行，不启动 Agent；`--diff` 调用 M33 `diverge` 报告首个发散记录（层、序号、`t_ns`、`kind`、字段级差异）；一致时退出码 0，发散时 21 | P1 | P1 | AC-M34-14 | AWR-06 §5.9 动作重放；R-M33-64 至 66 |
| R-M34-44 | 全量重仿真：`drone4d bench replay <run_dir> --mode full [--allow-impl-change]` 依运行清单与 `experiment.resolved.json` 重新运行该 Episode（策略须可解析且实现摘要一致，否则报 `policy_impl_changed` 并列出差异），比对 `trace_events_digest` 与 `truth_digest`（XR-05）；给出 `--allow-impl-change` 时以当前实现重新运行，并对新旧 trace 调用 M33 `diverge` 报告首个发散记录（一致退出码 0，发散 21），服务"修改算法后定位首个发散"的用例（M33 UC-02，§8.5 CR-M34-13）；AWR-06 §8.10 的 `drone4d bench run --replay` 统一为本命令（§13 第 9 条） | P1 | P1 | AC-M34-14 | AWR-06 §5.9 全量重仿真、§8.10；M33 UC-02 |
| R-M34-45 | `drone4d bench verify <dir> [--resim] [--json]`：`<dir>` 为运行目录或实验目录；依次执行 M33 `verify_run`（完整性、链头复算与清单比对、离线泄露审计、经 `TraceRunView` 离线重算分量并与 `evaluation.json` 逐字节比对）；实验目录另由 `rows.jsonl` 重算 `analysis.json` 并逐字节比对；`--resim` 对每个运行追加全量重仿真；任一不一致以 21 退出并输出结构化差异（项、期望、实际） | P1 | P1 | AC-M34-15 | AWR-06 §4.4、§8.10 R-4；R-M33-68 |
| R-M34-46 | 复核下载的公开划分条目：`bench verify` 与 `bench replay` 接受平台导出的条目包（运行清单与完整 trace，P1 由 LB 定义格式），不要求原实验配置文件存在 | P1 | P1 | AC-M34-15 | AWR-06 §8.10 末条 |
| R-M34-47 | 跨硬件等级提示：被核验运行的 `hw_class` 与本机不同时，`verify` 的位级比对项标注为"跨等级，按统计一致判定（R-2b）"，位级不一致不视为失败，但逐项列出 | P1 | P1 | AC-M34-15 | AWR-06 §5.6、§8.10 R-2b |

### 5.7 Suite、实例、基线与环境命令

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-48 | `drone4d bench suite list\|show <id>`、`drone4d bench suite check <dir> [--gate <stage>]`、`drone4d bench instance gen ...`、`drone4d bench instance check <pkg>`：参数与输出由 M31 定义，M34 负责命令注册、退出码与 `--json` 封装；`--gate` 为 P1 | P1 | P0（`--gate` P1） | AC-M34-16 | R-M31-77 |
| R-M34-49 | `drone4d bench baseline list\|show <ref>`：读取 M30 登记表导出的索引（R-M30-57），列出基线卡、兼容矩阵、确定性等级、所属附加包与 `stale` 状态 | P1 | P1 | AC-M34-16 | R-M30-57 |
| R-M34-50 | `drone4d bench init [--template northstar\|policy\|solver\|central] <dir>`：生成实验配置、策略源文件与说明；`northstar` 模板的两个文件与 `tests/perf/northstar/` 中同名文件逐字节一致；目标目录非空时拒绝 | P1 | P0 | AC-M34-01 | AWR-06 §5.10；RK-10 |
| R-M34-51 | `drone4d env [--json]` 报告：wheel 版本、`KERNEL_VERSION`、`API_LEVEL`、契约版本、Python 实现与版本、numpy 版本与构建信息、主进程运行时 SIMD 派发集合、worker 等级 `hw_class`、已安装附加包与缺失项、容器运行时（docker、podman）与版本、缓存目录。`hw_class` 经 M28 的探测接口在子进程中求得，该子进程与 `hosts.batch` 的 worker 使用相同的 spawn 与环境固定路径（父进程先写入 `NPY_DISABLE_CPU_FEATURES` 等变量再创建子进程，R-M28-88；§8.5 CR-M34-05），不满足任一登记等级时报 `unclassified`（与 R-M28-76 同名）；输出同时给出"主进程派发集合"与"worker 等级"两项，二者不同不视为错误；`drone4d version` 只打印版本行；二者不访问网络 | P1 | P0 | AC-M34-01 | AWR-06 §5.6 硬件等级；R-M28-76、88 |

### 5.8 `drone4d sdk pack`、`sdk check` 与 `sdk serve`

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-52 | `drone4d sdk pack --policy <ref> --src <dir> --wheelhouse <dir> [--requirements <lock>] [--weights <path>...] [--suite <id@ver>...] [--topology per_agent\|single_container] [--tag <name>] [--base-image <ref@digest>] [--download] [--context-only] [--out DIR]` 生成打包上下文（§7.9）。`wheels/` 中的 wheel 只从用户给出的本地 wheelhouse 复制，并按 `--requirements` 锁文件的哈希逐个核对，缺失时报 `wheel_missing` 并列出；`--download` 为显式联网开关，以 `pip download --only-binary=:all: --platform manylinux_2_35_x86_64 --python-version 3.12 --implementation cp --require-hashes` 补齐缺失 wheel（R-M34-101 的豁免）；镜像一律以 `--platform linux/amd64` 构建，arm64 主机上经仿真构建并提示耗时；检测到容器运行时时构建 OCI 镜像并输出镜像摘要，`--context-only` 只生成上下文 | P1 | P1 | AC-M34-17 | AWR-06 §4.3 末条；LB F6；RK-10 |
| R-M34-53 | 镜像约定：基础镜像以摘要固定（`d4-agent-py@sha256:...`，由 `tools/sdk/images/d4-agent-py/` 构建并随 wheel 版本发布）；依赖只从打包上下文中的本地 wheel 以 `--no-index --require-hashes` 安装；`USER 65534:65534`；入口 `["python", "-m", "drone4d.serve", "--protocol", "v2", "--policy", "<ref>"]`；环境变量 `PYTHONHASHSEED=0`、`PYTHONDONTWRITEBYTECODE=1`、`OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=MKL_NUM_THREADS=1`；不声明 `EXPOSE`、`VOLUME` | P1 | P1 | AC-M34-17 | F-05；R-M30-62 |
| R-M34-54 | 拓扑：`per_agent`（每个 Agent 一个容器，分布式赛道缺省）与 `single_container`（一个容器承载 `CentralPolicy`，集中式拓扑）；与 `policy_ref` 的类型不一致时拒绝（如 `CentralPolicy` 选 `per_agent`，报 `topology_mismatch`） | P1 | P1 | AC-M34-17 | AWR-06 §4.7 `submission.v2.entry.topology`；I-04 |
| R-M34-55 | 源码收集：只收集 `--src` 目录内的文件；排除 `.git/`、`__pycache__/`、`*.pyc`、构建目录；命中敏感文件模式（`.env`、`*.pem`、`*.key`、`id_rsa*`、`*.p12`）时拒绝并列出，需逐个以 `--allow-file` 显式放行；单文件 > 100 MiB 时报警告 | P1 | P1 | AC-M34-18 | 安全基线 |
| R-M34-56 | 产物：`pack.lock.json`（`drone4d.coord.pack_lock.v1`，§8.3）；`submission.v2.draft.json`，字段名与 CT §5.5（采纳 LB §13.4）逐字对齐：`suite{suite_id, major}`、`track`、`split`、`scenario_sets[]`、`algorithm{name, version}`、`entry{image_digest, topology, protocol = wire.v2, cmd[], env{}}` 由打包结果与参数填写，`declared{resources, external_data[], pretraining, human_tuning, scenario_specific_logic, training{seeds, artifacts[]}, method_family_tags[], llm?}` 与 `license` 以显式占位 `"TO_DECLARE"` 填写，`sdk pack` 不替提交者声明（`training.seeds` 可由训练日志的种子范围预填并标注"待提交者确认"）；草稿不含 `signature`，P1 起必填的签名不在本命令范围（§8.5 CR-M34-15）；`sbom.spdx.json`（SPDX 格式，工具在 WP-D4-44 中选定） | P1 | P1 | AC-M34-17 | AWR-06 §8.10 提交；CT §5.5；LB §13.4；ADR-D4-32（6） |
| R-M34-57 | `drone4d sdk check <image\|pack_dir> [--suite <id@ver>]`：(1) 复现平台扫描项：镜像平台为 `linux/amd64`、镜像 ≤ 8 GiB（LB F6）、SBOM 存在且与 `pack.lock.json` 一致、许可扫描无禁用许可、`entry.env` 键满足 `^[A-Z][A-Z0-9_]{0,63}$` 且不以 `DRONE4D_` 开头（F-05）；(2) 以本地容器宿主（非排名）在 `dev` 冒烟集（每 Suite 2 个场景 × 3 个公开种子 = 6 个 Episode，AWR-06 §8.10）上运行，容器参数与 `py:agentsdk/env/containers.py` `AgentContainers.argv` 逐项一致：`--network none`、`--read-only`、`--tmpfs /tmp:rw,size=256m`、`--cap-drop ALL`、`--security-opt no-new-privileges`、`--pids-limit 256`、`--user 65534:65534`、`--cpus` 与 `--memory` 按赛道 `budget_profile`、挂载 `/run/drone4d`；(3) 检查握手（`HELLO.protocol = drone4d.coord.wire.v2`、`features[]`）、`CONFIG` 应答、每次决策的墙钟、崩溃率与相对 `π_ind` 的效用。崩溃率定义为"出现策略失败的 Episode 数 / 6"，> 20% 或效用低于 `π_ind` 一半时报告"平台 VALIDATING 阶段将终止"；输出每 Agent 每仿真秒 CPU 时间与 B1 额度的比值（进程计量，标注"近似计量"） | P1 | P1 | AC-M34-19 | AWR-06 §8.10 VALIDATING；LB F6；R-M32-58；F-05 |
| R-M34-58 | `drone4d sdk serve` 与 `python -m drone4d.serve` 等价；新增 `--protocol v1\|v2`（缺省 `v1`，保持 D3 行为）与 `--policy <ref>`；`v2` 会话按 AWR-06 §4.7：帧头 `version = 2`、`Hello{protocol = drone4d.coord.wire.v2, sdk_version, features[], prefs}`、帧体为 `init.v1`/`observation.v1`/`decision.v1`、支持 `TRACE` 帧；`v1` 会话代码路径不变 | P1 | P1 | AC-M34-19 | AWR-06 §4.7 线协议；B-06 |
| R-M34-59 | `wire.v2` 编解码只有一份源码（M28 宿主侧所有，WP-D4-04），经 vendoring 同时服务评测宿主与 Agent 侧；以黄金帧向量对拍（与 D3 `wire.v1` 两份实现加黄金向量的做法不同，避免再出现双实现） | P1 | P1 | AC-M34-19 | AWR-06 §4.7；F-04 |

### 5.9 训练框架适配

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-60 | `drone4d.adapters.pettingzoo_parallel.parallel_env(suite, *, scenario_set=None, experiment=None, run_mode="closed_loop", track=None, clock_profile="d4_quick", tensor_profile="default", reward="suite_default", seed_schedule="dev_round_robin", privileged_state=False, trace_level="off")` 返回 `pettingzoo.ParallelEnv` 子类；一次 `step(actions)` 校验同一密封 tick 的动作、经 M28 `submit()` 提交、`advance_to_decision()` 推进至下一密封决策时刻，返回各 Agent 的私有观测、奖励、终止、截断与 `info`；Suite 终止谓词触发时 `terminations` 为真，到达时域 `T` 时 `truncations` 为真，二者同刻发生时取 `terminations`，以便训练框架按截断自举；不把网络事件映射为 `step()` | P1 | P1 | AC-M34-20 | AWR-06 §4.3 第 1 条；ADR-D4-13 |
| R-M34-61 | 空间映射按 Suite 声明的张量画像（§7.8.2；Suite 未声明的字段取 §7.8.2 的缺省规则）：observation 为 `Dict{self_state, detections + mask, tasks + mask, inbox + mask + age, assignment_mask}`，action 为 `Dict{nav, claim, message_emit, message_vec}`；`algorithm_isolated` 模式不含 `nav`；截断、排序与最大项数由 Suite 固定，适配器不提供改变形状的参数 | P1 | P1 | AC-M34-20 | AWR-06 §4.3 第 3 条 |
| R-M34-62 | 学习型通信：`message_emit = 1` 时把 `message_vec ∈ [−1, 1]^D_msg` 量化为 `q = clip(rint(127·x), −127, 127)`（`numpy.rint`，向偶数舍入；黄金向量见 `tests/d4/sdk/adapters/golden/vec_i8.json`）的 int8 字节串装入 `SendRequest`（`schema = drone4d.coord.msg.vec_i8.v1`，待 M30 登记，§8.5 CR-M34-08；`info_class` 由 schema 推导为 `opaque`，目的地按 Suite 张量画像的 `msg_dest`，缺省广播并由内核展开为计费单播）；收件箱张量以 `q/127` 反量化；非该 schema 的消息不进入张量视图，计数写入 `info` | P1 | P1 | AC-M34-21 | AWR-06 §4.3 第 3 条；R-M30-33；n03 §2.10 |
| R-M34-63 | 固定 Agent 集合：`possible_agents` 为实例 Agent 模板的 `agent_id` 码点序列表，Episode 内不增减；Assignment 变化以 `assignment_mask` 表达；任意 Bytes 协议与真正异步的研究使用原生 `Policy`，适配器文档明示该边界 | P1 | P1 | AC-M34-20 | AWR-06 §4.3 第 4、5 条 |
| R-M34-64 | 奖励：参数 `reward` 取 `"suite_default"`（Suite 声明的 `training_reward`；未声明时为 `team_terminal_U`）、`"team_terminal_U"`（各步为 0，终止或截断步为 Suite 主指标 `U`，全队相同；由评测器离线可得，任何 Suite 均可用）、`"team_delta_U"`（每决策周期 `U` 的在线增量，只在 Suite 声明其在线形式后可用，否则构造时报 `training_reward_unavailable`；S1 尚未声明，§8.5 CR-M34-10）或可调用对象 `RewardFn(step_view) -> Mapping[agent_id, float]`（`step_view` 只含 R-M34-65 的 `info` 字段与终止步的 `U`，对应 S1 §9.3"奖励为训练接口自定义"）；奖励不经任何渠道泄露其他真值 | P1 | P1 | AC-M34-21 | S1 §9.3；§13 第 12 条；F-18 |
| R-M34-65 | `info` 不含 Agent 无权获得的真值，只含回执计数、账本余量、`tick_id`、`invalid_action`、被丢弃的非张量消息数；`state()` 只在 `privileged_state=True`、`hosts.inproc` 且 `ranked = false` 时返回 Suite 的 `state_spec` 向量（经 M28 `privileged_state()`），否则抛 `PrivilegedAccessDenied`（726）；Suite 未声明 `state_spec` 时同样拒绝（`why = state_spec_undeclared`） | P1 | P1 | AC-M34-21 | AWR-06 §4.3 第 2 条；R-M28-87 |
| R-M34-66 | `drone4d.adapters.pettingzoo_aec.env(...)` 由 Parallel 环境经 PettingZoo 官方 Parallel→AEC 转换得到：轮转次序为 `agent_id` 码点序，全部 Agent 行动后推进一个密封决策周期；文档注明 AEC 是轮转外观而非异步网络；通过 PettingZoo 的 AEC API 测试 | P1 | P1 | AC-M34-20 | AWR-06 §4.3 第 5 条；n03 §3.6 |
| R-M34-67 | `drone4d.adapters.torchrl_env.torchrl_env(..., device="cpu", group="agents")` 返回 TorchRL 环境：P1 首版以 TorchRL 的 PettingZoo 封装实现，吞吐实测（DL-D4-01）不达标时改为原生 `EnvBase` 子类；两种实现对同一动作序列的观测、奖励与终止张量逐元素一致；文档注明 TorchRL 的多进程批量环境类名与 PettingZoo 的 `ParallelEnv` 同名而语义不同 | P1 | P1 | AC-M34-22 | AWR-06 §4.3 第 6 条 |
| R-M34-68 | `drone4d.adapters.benchmarl_task`：以 BenchMARL 的自定义任务扩展机制登记 `drone4d_<suite>` 任务族（扩展基类与登记方式以 `train` 附加包锁定的 BenchMARL 版本核实），提供 M30 的 IPPO、MAPPO、QMIX 训练配方（QMIX 只用于离散动作画像）；训练期 trace 缺省关闭，可按 Episode 抽样；评测一律以 `TensorPolicyAdapter` 经 `drone4d bench run` 执行，不以 BenchMARL 的评测循环产出可比成绩 | P1 | P1 | AC-M34-23 | AWR-06 §4.3 第 6 条；AC-D4-28 |
| R-M34-69 | `drone4d.adapters.tensor_policy.TensorPolicyAdapter(model, *, tensor_profile, device="cpu")` 实现 `Policy`：以与训练相同的编码把 `Observation` 转为张量、调用模型、把动作映射为 `Decision`；推理在单线程、确定性算法开关下执行；提供 `export_numpy_mlp(model, path)` 与 `NumpyMLPPolicy`，使多层感知机策略可在不含 torch 的镜像中推理 | P1 | P1 | AC-M34-24 | AWR-06 §4.3 末条（`sdk pack`） |

### 5.10 wheel 打包与命名空间重写

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-70 | 分发物为纯 Python wheel `drone4d-<版本>-py3-none-any.whl` 与对应 sdist；不含编译扩展；不含顶层 `awr` 包；与完整平台安装的 `awr` 发行版可共存 | P0 | P0 | AC-M34-25 | ADR-D4-28（2） |
| R-M34-71 | 命名空间重写：`tools/sdk/build_wheel.py` 依映射表 `tools/sdk/vendor.toml`（§7.10.2）把源模块复制到 `drone4d/_core/` 并重写导入；重写为词法级（`tokenize`），只改写 `import awr.<映射前缀>...` 与 `from awr.<映射前缀>... import ...` 语句的模块路径，保持行号与其余字节不变；规则 NS-01 至 NS-08 见 §7.10.3，任一违反使构建失败 | P0 | P0 | AC-M34-26 | ADR-D4-28（2）；F-10、F-11 |
| R-M34-72 | 溯源清单：wheel 内 `drone4d/_core/SOURCES.json` 记录仓库提交、工作树是否干净、映射表摘要，以及每个文件的源路径、源 sha256、输出路径、输出 sha256 与所应用的规则；发布构建要求工作树干净 | P0 | P0 | AC-M34-26 | r03 R24 |
| R-M34-73 | 包数据：`ct:coord/`、`ct:suite/` 的 schema 与 golden、`ct:rt/rng_streams.json`、M29 网络参数集 `py:interaction/params/*.json`（M29 路径所有权表：随 wheel 打包；北极星的 `net/abstract-disk-default@1` 依赖之）、Suite 清单与 `dev` 参考缓存、模板、`perception/ref_det_v1.npz` 与 `model_card.json` 以显式 `package-data` 声明。各所有者模块在其 PRD 声明随 wheel 分发的包数据路径与预期内容清单，M34 构建脚本汇总为 `tools/sdk/package_data.toml` 并逐项核对（缺失或多余即失败）；映射包内未被任何所有者声明的非 `.py` 文件逐个列出并使构建失败，防止清单本身遗漏（§8.5 CR-M34-11）；运行时一律以 `importlib.resources.files(__package__)` 读取 | P0 | P0 | AC-M34-25 | F-02；AC-D4-43；M29 路径所有权表 |
| R-M34-74 | 可重复构建：以 `SOURCE_DATE_EPOCH` 取提交时刻、文件按路径排序写入；同一提交在两次独立构建中产出 sha256 相同的 wheel；发布记录 wheel 与 sdist 的 sha256 | P0 | P0 | AC-M34-28 | — |
| R-M34-75 | 开发态等价：仓库内开发以 `make sdk-dev`（命名以 M00 登记为准）用同一重写脚本生成 `tools/sdk/drone4d/src/drone4d/_core/`（加入忽略清单），不以"导入失败时回退到 `awr.*`"的方式实现 | P0 | P0 | AC-M34-27 | F-17 |
| R-M34-76 | 路径一致性：CI 对北极星配置的 20 个 Episode 子集与各 Suite 黄金集，分别在仓库路径（`awr.*`）与干净环境中已安装的 wheel 上运行，§7.7.1 一致性比较集逐字节一致（运行清单的 `versions.*`、`run_hash` 与由运行清单派生的摘要列入该集的路径豁免）；北极星子集须包含 `cbba` 与 `nearest_greedy`，S2 黄金集须包含 `independent_lawnmower`（`π_ind`）与 `partition_lawnmower`，以覆盖 vendored `awr.swarm.*`；vendored `cbrng` 以黄金向量对拍 | P0 | P0 | AC-M34-27 | ADR-D4-28（2）；M30 R-M30-47 |
| R-M34-77 | 依赖闭包：只装运行时依赖的干净环境中导入全部核心模块后，`sys.modules` 不含 open3d、scipy、jsonschema、numba、torch、pettingzoo；导入 `drone4d.coord` 与 `drone4d.experiment` 不导入 msgpack（`wire.v2` 只在 `serve` 与容器宿主路径导入） | P0 | P0 | AC-M34-29 | AC-D4-43；B-02 |
| R-M34-78 | 体积与支持矩阵：wheel ≤ 50 MB；支持矩阵为 CPython 3.12 × {Linux x86-64（glibc ≥ 2.35）、macOS arm64}（Python 下限已由 AWR-06 v0.3 XR-02 裁定为 CPython 3.12，Q-M34-01 关闭）；矩阵中每个组合在干净环境中安装并执行冒烟（`env`、北极星配置的 4 个 Episode、`bench report`） | P0 | P0 | AC-M34-30 | ADR-D4-28（4）；AC-D4-47 |
| R-M34-79 | 许可与 SBOM：wheel 元数据的许可字段按 ADR-D4-32 裁决结果填写（SPDX 表达式或 `LicenseRef-`），`LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES.md` 随 wheel 分发（`license-files`）；构建 `build-system` 提升为 `setuptools>=80`；发布流水线生成 SPDX SBOM 并执行许可扫描门禁；裁决前不发布（RK-17） | P0 | P0 | AC-M34-30 | ADR-D4-32；AC-D4-47；F-01、F-02 |
| R-M34-107 | wheel 内的组件可用性：wheel 只包含可在 numpy-only 核心中运行的网络档、物理档、时钟档与 Suite；解析阶段对 wheel 中不可用的取值（`network_profile ∈ {fspl-nlos@1, ns3@1}`、`clock_profile = d3_compat`、`suite = drone4d-event@1`，以及未安装 `hifi` 时的 `physics ∈ {kinematic, realistic}`）以 `x-rules` 规则组 `wheel_unavailable` 拒绝（原因码 `component_unavailable_in_wheel`，退出码 2），消息说明该取值只在仓库环境可用；该规则组只在安装态生效 | P0 | P0 | AC-M34-37 | M29 §0 第 3 条、§4.8（`fspl-nlos@1` 仅 `drone4d-event@1` 与对拍）；F-12、F-20 |

### 5.11 依赖与附加包

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-80 | 运行时依赖（建议方案，待 Q-M34-02 裁决）：`numpy>=2.0,<3`、`PyYAML>=6.0,<7`、`msgpack>=1.0,<2`；内核与 D4 核心包的导入闭包仍只有 numpy（B-02 不变）；裁决为"只有 numpy"时，YAML 改由内置受限子集解析器承担（YL 规则不变）、`wire.v2` 的 Agent 侧移入附加包 `serve` | P0 | P0 | AC-M34-29 | ADR-D4-28（4）；F-01、F-06；§13 第 2 条 |
| R-M34-81 | 附加包只声明依赖，不改变 wheel 内容；依赖附加包的模块随基础 wheel 分发、在使用时延迟导入，缺依赖时抛 `ExtraMissing`（原因码 `extra_missing`，退出码 8），消息给出 `pip install "drone4d[<名>]"`；附加包与模块的对应见 §7.11 | P0 | P0 | AC-M34-29 | ADR-D4-33；§13 第 3 条 |
| R-M34-82 | D3 兼容面：httpx 移入附加包 `client`；`sdk:bench/__init__.py` 改为延迟导入 `BenchClient` 等符号（模块级 `__getattr__`），安装 `client` 时行为不变，未安装时 `import drone4d.bench` 成功、访问 `BenchClient` 报 `extra_missing` 并给出 `pip install "drone4d[client]"`。这是对 D3 面的依赖集合变更，是 R-M34-97 冻结规则的唯一例外，须登记在 AWR-06 附录 B（D3 封板裁决清单）并经 M26 同意后实施（§13 第 25 条）；变更列入迁移指南与发布说明 | P0 | P0 | AC-M34-33 | F-01、F-03；B-07；R-D4-17 |
| R-M34-83 | `train` 附加包的精确版本在 WP-D4-24 中锁定（PettingZoo、Gymnasium、PyTorch CPU 版、TorchRL、TensorDict、BenchMARL）；许可扫描确认传递闭包不含 GPL 系许可（VMAS 为 GPL-3.0，只做对照，r01 §3.1）；torch 的 CPU 与 CUDA 选择由用户的索引配置决定，`drone4d` 不固定 CUDA 变体 | P1 | P1 | AC-M34-30 | RK-14；ADR-D4-32（3） |
| R-M34-84 | 每个发布版本附约束文件 `drone4d-<版本>-constraints.txt`（运行时依赖与各附加包的精确版本与哈希），用于在本地复现与平台评测镜像相同的依赖构建；`drone4d env --json` 输出当前环境与该约束的偏差 | P1 | P0 | AC-M34-30 | AWR-06 §5.6 硬件等级（数学库与数值库构建固定） |

### 5.12 教程与文档站

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-85 | D4 文档纳入现有文档站，结构见 §7.12；以版本路径 `/drone4d/<lang>/0.4/` 发布，`latest` 在 D4 公开前继续指向已发布的产品版本；`VERSIONS` 与版本切换浮层按 `docs-site/_shared/conf_common.py` 现有机制扩展 | P0 | P0 | AC-M34-31 | ADR-D4-35；F-14 |
| R-M34-86 | 中英双语同构：每个新页面两种语言同时提交；教程代码、命令与输出以 `docs-site/_shared/tutorials/<id>/` 中的单一来源文件经 `literalinclude` 引用，两种语言不各自维护代码块 | P0 | P0 | AC-M34-31 | F-14 |
| R-M34-87 | API 参考自动生成：文档构建环境安装同一提交构建的 wheel（`--no-deps` 加运行时依赖），以 `sphinx.ext.autodoc` 生成稳定面与预览面；英文页的说明文字以 `api_summaries.json` 同类的译文登记文件提供，缺译文时构建失败；命令行参考由 `argparse` 树生成，实验配置参考由 `experiment.v1.schema.json` 生成，属性参考由 `AttrRegistry.export_schema()` 生成，基线页由 M30 索引生成 | P0 | P0 | AC-M34-31 | ADR-D4-35；R-M28-35；R-M30-57 |
| R-M34-88 | 可执行教程：每个教程有 `steps.sh`，CI 在干净容器（`python:3.12-slim`，不挂载仓库，只提供构建出的 wheel 与私有索引镜像）中逐步执行；页面中的输出块由 RM-8 上的真实运行捕获并入库，CI 核对其结构（列名、行数、标签）与本次执行一致，不核对随运行变化的数值 | P0 | P0 | AC-M34-32 | AC-D4-46、AC-D4-48；F-14 |
| R-M34-89 | 教程清单：P0 快速入门（§4）与教程一"编写一个协同策略"；P1 教程二"编写一个 Suite"、教程三"编写一个网络档"、教程四"在 BenchMARL 中训练"、教程五"打包评测容器"、迁移指南（D3 SDK → D4）、指南"复现与核验"、FAQ；D4 的 SDK 页标题为"研究者 SDK（drone4d）"，与现有"Python SDK（awr_sandbox）"页区分 | P0 | P0–P1 | AC-M34-31、32 | WP-D4-45、WP-D4-47；F-15 |
| R-M34-90 | 项目页：版本与弃用策略、兼容矩阵、变更记录、贡献流程（DCO）、评审流程、行为准则、插件与模块注册表规格（Core/Contrib/Community）、许可说明、引用方式；P1 发布 | P1 | P1 | AC-M34-31 | ADR-D4-35；r03 R29、R30 |

### 5.13 兼容与弃用

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-91 | 版本号：wheel 版本与产品版本同线，D4 为 `0.4.Z`（PEP 440）；P1 门之前的发布为预发布（`0.4.0aN`、`0.4.0bN`），不承诺兼容，但每个破坏性变更须在变更记录中给出迁移说明；P1 门（第一赛季开启，ADR-D4-34）发布首个稳定版 `0.4.0`，此后适用 R-M34-93 至 97。预发布期间 pip 缺省不选择预发布版本，文档一律写 `pip install --pre "drone4d>=0.4.0a1,<0.5"`，`tests/perf/northstar/run.sh` 以 `pip install "drone4d==<被测版本>"` 精确固定；私有索引在 P1 门之前不发布任何 `drone4d` 稳定版本 | P0 | P0 | AC-M34-33 | ADR-D4-35；§13 第 13 条 |
| R-M34-92 | 版本一致性检查：新增 lint 核对 `tools/sdk/drone4d/pyproject.toml` 的版本与产品版本同线（主次版本相同），并核对 `drone4d.__version__` 与元数据一致 | P0 | P0 | AC-M34-33 | F-16 |
| R-M34-93 | 兼容面与规则（§7.13.1）：稳定面在 `0.4.x` 内只做加法；新增可选参数须有缺省值且缺省行为不变；命令行的命令、参数、退出码与 `--json` 字段只增不改；`experiment.v1` 只增可选字段（§7.13.3）；实验目录布局只增文件 | P0 | P1 | AC-M34-34 | ADR-D4-31、35 |
| R-M34-94 | 弃用流程：以 `drone4d.deprecated(since, removal, alternative)` 标记，运行时发出 `Drone4DDeprecationWarning`（`FutureWarning` 的子类，缺省可见），每个调用位置只警告一次；命令行参数弃用时在标准错误提示并在 `--json` 的 `warnings[]` 中列出；移除须同时满足：自首次发出警告起至少 2 个后续发布、且不少于 6 个月 | P0 | P1 | AC-M34-34 | ADR-D4-35 |
| R-M34-95 | 赛季保护：赛季进行中，评测宿主须接受以该赛季开启时最低 SDK 版本及其后任一版本打包的容器；影响容器内运行面（`drone4d.coord`、`wire.v2`、`TensorPolicyAdapter`）的移除只在赛季之间生效 | P0 | P1 | AC-M34-35 | AWR-06 §8.9 赛季；ADR-D4-35 |
| R-M34-96 | 内核版本与赛季：`KERNEL_VERSION` 独立于 wheel 版本按语义化版本演进，主版本变化即新赛季；兼容矩阵（§7.13.2）随每次发布更新，列出 wheel 版本、`KERNEL_VERSION`、契约版本、线协议、所支持的 Suite 版本、Python 与 numpy 范围；每个赛季的内核主版本在赛季结束后再保留一个赛季的只读复现支持（`bench verify`、`bench replay` 可用） | P0 | P1 | AC-M34-35 | ADR-D4-35；AWR-06 §8.9 |
| R-M34-97 | D3 面冻结：`drone4d.agent`、`drone4d.wire`（v1）、`drone4d.serve` 的 v1 缺省、`drone4d.baselines`、`drone4d.perception`、`drone4d.bench.make` 在 `drone4d-event@1` 受支持期间不弃用、不改行为（依赖集合的唯一例外见 R-M34-82）；`drone4d.bench.make` 的文档注明其需要仓库环境（F-03） | P0 | P0 | AC-M34-33 | B-06、B-07；R-D4-17 |

---

## 6. 非功能需求

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M34-98 | 启动：`drone4d --help` 与 `drone4d version` 在 RM-8 上墙钟 ≤ 0.3 s（目标值，温缓存） | P1 | P0 | AC-M34-12 | — |
| R-M34-99 | 命令行开销：北极星实验中，解析、展开、worker 启动、`rows.jsonl` 汇编、分析与报告渲染合计 ≤ 30 s（目标值），其中 `analyze_experiment` ≤ 5 s（R-M32-83）；该开销计入 6 min 实验墙钟预算 | P0 | P0 | AC-M34-01 | AWR-06 §5.10 |
| R-M34-100 | 资源：北极星配置下每个 worker 常驻内存 ≤ 1 GiB（目标值）；实验目录（trace 级别 `minimal`）≤ 2 GiB（目标值，以 CP-2 实测校准） | P1 | P0 | AC-M34-01 | — |
| R-M34-101 | 离线：除 `sdk pack` 拉取缺失的基础镜像（须显式 `--pull`）与补齐缺失 wheel（须显式 `--download`）外，任何命令不访问网络；不收集遥测 | P0 | P0 | AC-M34-11 | — |
| R-M34-102 | 安全：日志与 `--json` 输出不写入种子明文以外的秘密（本地 `dev` 种子为公开值）；命令行不读取、不存储任何令牌；私有包索引凭据只经 pip 自身配置提供，文档不出现凭据示例值 | P0 | P0 | AC-M34-18 | R-M28-78 |
| R-M34-103 | 可诊断：任何失败输出原因码、所在阶段与修复提示；`--log-format json` 的每条日志含 `episode_id`（若有）与 `code`；`drone4d env --json` 的输出可直接附在问题报告中且不含主机名与用户名以外的个人信息 | P1 | P0 | AC-M34-11 | — |
| R-M34-104 | 训练吞吐：8 实体、`L1`、Δd = 100 ms、32 个环境、trace 关闭时 ≥ 200 team-steps/s/服务器（目标值，未达标如实报告），实测入 DL-D4-01 | P1 | P1 | AC-M34-22 | DL-D4-01 |

---

## 7. 设计方案

### 7.1 包结构

**源码位置**（仓库）：

```
tools/sdk/
├── build_wheel.py              # wheel 构建与命名空间重写（§7.10）
├── vendor.toml                 # 映射表与排除清单
├── images/d4-agent-py/         # wire.v2 评测容器基础镜像（Dockerfile、build.sh）
└── drone4d/
    ├── pyproject.toml
    └── src/drone4d/
        ├── __init__.py         # __version__、KERNEL_VERSION
        ├── __main__.py         # python -m drone4d → cli.main
        ├── cli.py              # 入口与命令树
        ├── _cli/               # 子命令实现：bench_run.py、bench_report.py、bench_replay.py、bench_verify.py、
        │                       #   bench_init.py、bench_suite.py、bench_instance.py、bench_baseline.py、
        │                       #   sdk_pack.py、sdk_check.py、env.py、plugins.py、result.py、exitcodes.py、i18n/
        ├── coord/              # __init__.py（再导出）、base.py（便利基类）、helpers.py（自由函数）、experimental.py
        ├── experiment/         # yaml_restricted.py、validate.py、resolve.py、expand.py、run.py、result.py、
        │                       #   localhost_container.py（sdk check 的本地容器宿主）
        ├── adapters/           # pettingzoo_parallel.py、pettingzoo_aec.py、torchrl_env.py、benchmarl_task.py、
        │                       #   tensor_policy.py、encoding.py、legacy_obs_v1.py
        ├── kernel/             # __init__.py：再导出 drone4d._core.kernel 的公开类型
        ├── suites/             # 各 Suite 的只读视图类型再导出
        ├── templates/          # northstar/、policy/、solver/、central/
        ├── _core/              # 生成目录（不入库）：vendored 源码与 SOURCES.json
        │
        ├── agent.py  wire.py  serve.py  runner.py  local.py      # D3 面（冻结；serve.py 增 --protocol）
        ├── baselines/  bench/  perception/                        # D3 面（bench/__init__.py 改为延迟导入）
        └── py.typed
```

**wheel 内容**与源码布局相同，`_core/` 为构建时生成的 vendored 树：

```
drone4d/_core/
├── kernel/  interaction/  coordination/  benchmarks/  evaluation/  trace/     # D4 核心包（按 vendor.toml 排除平台侧子树）
├── sim/__init__.py  sim/sensors/__init__.py  sim/sensors/cbrng.py           # 中间包为生成的空包（NS-05）
├── sim/events/__init__.py  sim/events/{jcs,errors}.py                       # tape.py 不进入（F-20）
├── phys/__init__.py（原样复制，只含 PhysError）  phys/{adapter,pose_history}.py
├── swarm/__init__.py（生成）  swarm/allocation/{__init__,hungarian,score}.py
├── swarm/coverage/{__init__,lanes,partition,plan,sensor}.py                  # 两个子包的 __init__ 原样复制（NS-05 例外，F-21）
├── interaction/params/*.json（M29 参数集）  contracts/coord/*.json  contracts/suite/*.json  contracts/rt/rng_streams.json
└── SOURCES.json
```

### 7.2 公开 API 草案（Python 3.12）

```python
# sdk:coord/__init__.py —— 稳定面；协议与数据类的定义在 M30（py:coordination/policy.py、api.py），此处只再导出
from drone4d._core.coordination.policy import (
    Policy as PolicyProtocol, WakeOnRx, CentralPolicy, Solver, CoordinationPolicy, Replannable,
)
from drone4d._core.coordination.api import (
    AgentContext, TeamContext, CoordContext, Observation, Decision, EntityCommand, SendRequest,
    DeliveredMessage, TxReceipt, InteractionOp, InteractionEvent, ReportRequest, BeliefSnapshot,
    Assignment, FrameRef, ProblemView, SolveBudget, Plan, WakeDecision,
)
from drone4d.coord.base import Policy                       # 便利基类，PolicyProtocol 的显式子类（R-M34-02）
from drone4d.coord.helpers import (EPOCH_CURRENT, SdkUsageError, goto, vel, hold, follow_path,
                                   sensor_point, send, broadcast, claim, release, dist)
API_LEVEL: int = 1

# sdk:coord/base.py
class Policy(PolicyProtocol):
    def reset(self, ctx: AgentContext) -> None: ...          # 缺省空操作
    def step(self, obs: Observation, inbox: Sequence[DeliveredMessage]) -> Decision: ...   # 未覆盖时 reset 阶段报 policy_step_missing
    def close(self) -> None: ...                             # 缺省空操作

# sdk:coord/helpers.py
EPOCH_CURRENT = -1                                           # SDK 内部哨兵，由 finalize_decision 替换（R-M34-05）
def goto(entity_id: str, pos: "Vec3", *, speed_mps: float | None = None,
         valid_for_ns: int | None = None, epoch: int = EPOCH_CURRENT) -> EntityCommand: ...
def hold(entity_id: str, *, valid_for_ns: int | None = None, epoch: int = EPOCH_CURRENT) -> EntityCommand: ...
def send(dst: str | Sequence[str], payload: bytes | Mapping[str, object], *, schema: str = "opaque",
         channel: str = "default", max_age_ns: int = 0, priority: int = 4, flow_id: str | None = None,
         source_sample_time_ns: int | None = None) -> SendRequest: ...   # info_class 由 schema 推导（CT §4.1.1）
def broadcast(payload: bytes | Mapping[str, object], **kw) -> SendRequest: ...   # destinations = ()
def claim(task_id: str) -> InteractionOp: ...                # task.claim
def release(task_id: str) -> InteractionOp: ...              # task.release
def dist(a: Sequence[float], b: Sequence[float]) -> float: ...
class SdkUsageError(ValueError):                             # 如 payload_schema_missing
    code: str

# sdk:experiment/__init__.py —— 稳定面
class Experiment:
    @classmethod
    def load(cls, path: str | os.PathLike, *, sets: Sequence[str] = (), attrs: Sequence[str] = ()) -> "Experiment": ...
    @classmethod
    def from_mapping(cls, doc: Mapping[str, object], *, base_dir: os.PathLike) -> "Experiment": ...
    def resolve(self) -> "ResolvedExperiment": ...          # 抛 SpecError（含全部字段级错误）

@dataclass(frozen=True)
class EpisodeKey:
    policy: str; scenario_id: str; seed_index: int; repeat: int; intervention: str

class ResolvedExperiment:
    experiment_id: str; spec_sha256: str; experiment_config_sha256: str; analysis_config_sha256: str
    episodes: tuple[EpisodeKey, ...]
    labels: frozenset[str]                                   # 如 {"synthetic-stress", "quick-clock"}
    required_extras: frozenset[str]
    def document(self) -> Mapping[str, object]: ...          # experiment.resolved.json
    def plan(self, *, workers: int | Literal["auto"] = "auto") -> "RunPlan": ...   # 含估算墙钟
    def run(self, *, out_dir: os.PathLike | None = None, workers: int | Literal["auto"] = "auto",
            resume: bool = False, progress: "ProgressSink | None" = None) -> "ExperimentResult": ...

class ExperimentResult:
    out_dir: Path; completed: int; failed_infra: int; failed_policy: int; pending: int
    analysis: Mapping[str, object]
    def report(self, formats: Sequence[str] = ("md", "html")) -> list[Path]: ...

def debug_episode(resolved: ResolvedExperiment, key: EpisodeKey, *,
                  policies: Mapping[str, object] | None = None) -> "EpisodeResult": ...   # hosts.inproc

class SpecError(Exception):
    issues: tuple["SpecIssue", ...]                          # (stage, pointer, code, message)
class ExtraMissing(ImportError):
    extra: str
```

### 7.3 最小策略示例与行数判据

模板 `templates/northstar/my_policy.py` 的内容为 AWR-06 §4.2 示例（S1 TaskAlloc，独立最近任务贪心）按自由函数改写后的版本：`obs.claim(...)` 改为 `claim(...)`，`st.dist_to(p)` 改为 `dist(st.pos, p)`（M30 §12 第 15 条；§13 第 11 条）：

```python
from drone4d.coord import Policy, Decision, goto, claim, dist

class NearestTask(Policy):
    def reset(self, ctx):
        self.me = ctx.agent_id
    def step(self, obs, inbox):
        open_tasks = [t for t in obs.tasks if t.status == "open"]
        if not open_tasks:
            return Decision(tick_id=obs.tick_id)
        ent, st = next(iter(obs.self_state.items()))
        t = min(open_tasks, key=lambda t: dist(st.pos, t.pos))
        return Decision(tick_id=obs.tick_id, commands=(goto(ent, t.pos),),
                        interactions=(claim(t.task_id),))
```

| 判据 | 规则 |
|---|---|
| 计数口径 | 以 `tokenize` 统计含至少一个非注释、非换行记号的物理行；导入行计入；空行与只含注释的行不计；续行按物理行计 |
| 当前计数 | 12 行（≤ 20，DP-11） |
| 可运行 | 只导入 `drone4d.coord`；`goto` 的 epoch 由 R-M34-05 的决策定稿函数填充；`obs.tasks` 的元素为 S1 的任务视图（`task_id`、`status`、`pos`），`self_state` 的值为 S1 的实体状态视图（含 `pos`），二者只含字段；`claim` 返回 `task.claim` 交互 |
| 检查位置 | `tests/d4/sdk/test_min_policy.py`：对模板文件执行计数并在 S1 `d1/n20` 的 1 个 Episode 上运行；与 `tests/perf/northstar/my_policy.py` 逐字节比较 |

### 7.4 实验配置

#### 7.4.1 北极星模板

`templates/northstar/experiment.yaml` 与 `tests/perf/northstar/config.yaml` 逐字节一致（AC-D4-11 的配置摘要以该文件的 JCS 摘要为准）：

```yaml
schema: drone4d.coord.experiment.v1
name: ta-quickstart
suite: {id: taskalloc, version: 1.0.0}
split: dev
instances: {difficulty: d1, scale: n20, count: 10}
track:
  execution_topology: decentralized
  observation_profile: L1
  budget_profile: B1
  network_profile: abstract-disk@1
run_mode: closed_loop
mode: exogenous
seeds: {exogenous: {range: [0, 10]}, repeats: 1}   # 10 实例 × 10 种子 = 100 个配对单元
clock_profile: d4_quick
physics: planar@1
network: {params_ref: net/abstract-disk-default@1}
policies:
  - {name: static_partition, ref: "baseline:taskalloc/static_partition@1"}
  - {name: nearest_greedy,   ref: "baseline:taskalloc/nearest_greedy@1"}
  - {name: cbba,             ref: "baseline:taskalloc/cbba@1"}
  - {name: mine,             ref: "py:my_policy:NearestTask"}
reference: {independent: static_partition}
interventions: []
outputs: {trace_level: minimal, report: [md, html]}
execution: {host: batch, workers: auto}
```

与 AWR-06 §4.4 样例的差异：北极星不含 `interventions`（400 个 Episode 只来自 4 个策略 × 100 个配对单元）、不含 `reference.central`（滚动 MILP 依赖 scipy 且不在北极星内，AWR-06 §5.10），`trace_level` 为 `minimal`（AWR-06 §5.10 表）。`seeds.exogenous.range` 为左闭右开区间，须落在 S1 `dev` 为（`d1`, `n20`）声明的公开种子范围之内（§7.4.2 `seeds.exogenous`）。

#### 7.4.2 全字段

"计入摘要"指计入实验级摘要 `experiment_config_sha256`（R-M34-19）；"排名"列为平台排名运行的附加约束（本地运行不可设 `ranked = true`）。

| 字段 | 类型 | 必填 | 缺省 | 取值与约束 | 计入摘要 | 排名 | 说明 |
|---|---|---|---|---|---|---|---|
| `schema` | string | 是 | — | 常量 `drone4d.coord.experiment.v1` | 否（决定解析） | — | 契约标识 |
| `requires.drone4d` | string | 否 | — | PEP 440 版本说明符，如 `">=0.4.3"` | 否 | — | 不满足时报 `sdk_version_unsatisfied`，先于字段校验执行 |
| `name` | string | 是 | — | `^[a-z0-9][a-z0-9._-]{0,63}$` | 否 | — | 缺省输出目录名 |
| `metadata.description` | string | 否 | — | ≤ 2,000 字符 | 否 | — | 写入报告概况 |
| `metadata.tags[]`、`metadata.authors[]` | string[] | 否 | `[]` | 各 ≤ 16 项 | 否 | — | — |
| `suite.id` | string | 是 | — | 已安装 Suite 的 id | 是 | 平台填写 | — |
| `suite.version` | string | 是 | — | 精确语义化版本 | 是 | 平台填写 | 不接受范围 |
| `suite.digest` | string | 否 | — | `sha256:<64 hex>` | 是（作为断言） | — | 与已安装 Suite 的 `suite_digest` 不符时报 `suite_digest_mismatch` |
| `split` | string | 否 | `dev` | 本地只允许 `dev` | 是 | 平台填写 | R-M34-16 |
| `scenario_set` | string | 二选一 | — | Suite 登记的场景集 id | 是 | 平台填写 | 与 `instances` 恰选其一（`exactly_one`） |
| `instances.family` | string | 否 | Suite 的首个族 | Suite 登记的族 id | 是 | — | — |
| `instances.block` | string | 否 | 该族的首个公开区块 | M31 登记的公开区块 id（如 S1 的 `pc`） | 是 | — | 实例坐标的一部分（M31 §7.7.2） |
| `instances.difficulty` | string | 二选一内必填 | — | Suite 开放的难度档（如 `d0`–`d3`） | 是 | — | 档位状态须为 `frozen` 或 `preview`（M31 §4.1） |
| `instances.scale` | string | 同上 | — | Suite 开放的规模档或 `n20` 北极星档 | 是 | — | — |
| `instances.count` / `instances.ids[]` | int / string[] | 二选一 | — | `count` 1–1,000；`ids` 为实例 id 列表 | 是 | — | `count` 按 M31 §7.7.2 的公开派生规则，以（`family`, `block`, `index`）字典序（字符串按码点序，`index` 按整数序）取前 `count` 个实例 |
| `track` | string 或 object | 是 | — | 字符串为 Suite 赛道 id 简写（Helper，展开为四元组）；对象为四元组 | 是 | 平台填写 | — |
| `track.execution_topology` | string | 是 | — | `decentralized`、`centralized`（以 Suite 登记为准） | 是 | — | — |
| `track.observation_profile` | string | 是 | — | `L0`、`L1`、`L2` | 是 | — | `L0` 要求 `centralized` 或非排名 |
| `track.budget_profile` | string | 是 | — | Suite 登记的预算档（如 `B1`） | 是 | — | — |
| `track.network_profile` | string | 是 | — | `none@1`、`ideal@1`、`abstract-disk@1`、`abstract-radio@1`、`ns3@1`、`fspl-nlos@1` | 是 | `ns3@1` 禁止排名 | 网络档须被 Suite 允许；wheel 中 `fspl-nlos@1`（仅 `drone4d-event@1` 与对拍，M29 §4.8）与 `ns3@1`（P2）由 R-M34-107 拒绝 |
| `run_mode` | string | 否 | Suite 首个模式 | `algorithm_isolated`、`closed_loop` | 是 | — | 两种模式为不同场景集 |
| `mode` | string | 否 | `exogenous` | `exogenous`、`interactive` | 是 | — | `interactive` 的目标反应使用独立随机域 |
| `seeds.exogenous` | object | 是 | — | `{range: [a, b]}`（0 ≤ a < b，左闭右开）或 `{list: [...]}`（去重、≤ 10,000 项）；`b` 与 `list` 各元素须落在所选场景集声明的 `exogenous_seeds.public = k` 即 `[0, k)` 之内（`instances` 写法取该族 `dev` 场景集的声明），否则报 `seed_out_of_public_range` | 是 | 平台填写（只存承诺） | `dev` 中为公开种子空间的序号（M31 §7.7.1、§7.7.2） |
| `seeds.repeats` | int | 否 | 1 | 1–100 | 是 | — | 同一种子的重复次数，用于 `repeat_delta` |
| `clock_profile` | string | 否 | `d4_std` | `d4_quick`、`d4_std`、`d3_compat` | 是 | 只允许 `d4_std` | `d3_compat` 仅 `drone4d-event@1`（不在 wheel） |
| `physics` | string | 否 | Suite `ranked_physics[0]` | `planar@1`、`kinematic`、`realistic`、`fast` | 是 | Suite `ranked_physics[]` | 非 `planar@1` 需 `hifi` 或 P2 的 `fast` |
| `network.params_ref` | string | 否 | 网络档的缺省参数集 | 登记参数集 id（如 `net/abstract-disk-default@1`） | 是 | — | 未校准参数集自动带 `synthetic-stress` |
| `network.overrides` | object | 否 | `{}` | 键为该网络档登记参数名，值经 `AttrSpec` 校验 | 是 | Suite 约束 | 展开为 `/net/<profile>/<param>` 属性 |
| `decision.modeled_latency_ms` | int | 否 | 时钟档缺省 | `d4_std` 下为 50、100、250、500 | 是 | — | — |
| `decision.wake_on_rx` | bool | 否 | `false` | Suite 显式允许时才可为 `true` | 是 | — | P1 |
| `agents.template` | string | 否 | 实例的 `agents_template` | `one_per_entity`、`single_central`、`custom` | 是 | `custom` 禁止 | Helper |
| `agents.custom[]` / `entities[]` | object[] | `custom` 时必填 | — | `AgentSpec` / `EntitySpec`（AWR-06 §4.2） | 是 | 禁止 | 研发用途 |
| `policies[]` | object[] | 是 | — | 1–16 项；`name` 唯一，`^[a-z0-9_-]{1,32}$` | 是（按 `name` 排序后计入；书写顺序计入分析摘要） | 平台填写 | 书写顺序决定报告列顺序 |
| `policies[].ref` | string | 是 | — | `baseline:<suite>/<id>@<major>`、`contrib:...`、`community:...`（均可带 `+` 连接的包装器复合引用，M30 §7.5）、`module:<module>:<attr>`、`image:sha256:<hex>`（P1） | 是 | 只允许 `image:sha256:` | R-M30-50、R-M30-54 |
| `policies[].params` | object | 否 | `{}` | 基线：登记的 `/policy/<suite>/<baseline>/<param>`；`py:`：以 `cls(**params)` 传入，须可 JCS 规范化 | 是 | 禁止 | R-M30-63 |
| `reference.independent` | string | 否 | Suite `reference_policies.ind` | `policies[].name`、引用串，或 Suite 参考策略的登记名（如 `rolling_milp`，解析为 `baseline:<suite>/<name>@<major>`） | 是 | 平台填写 | 配对差的基准 |
| `reference.central` | string | 否 | 不运行 | 同上 | 是 | 平台填写 | 依赖 scipy 者需 `drone4d[ref]`，或使用 wheel 内的 `dev` 参考缓存 |
| `interventions[]` | string[] | 否 | `[]` | `ind`、`cut`、`perm`（P1）、`central` | 是 | 平台填写 | 展开由 M32 `InterventionPlan` 决定 |
| `protocol` | object | 否 | — | `{id, families[], q_levels[]}`；P1；以 `x-since` 标注引入版本 | 是 | 平台填写 | 扰动协议（X1 §12.6 第 15 条）；存在时 Episode 展开增加（族, 档）维度，展开与聚合规则由 X1 与 M31、M32 定义 |
| `protocol.id` | string | 块内必填 | — | 已安装协议包的 id，如 `robustcoord@1` | 是 | — | 须与 Suite 兼容 |
| `protocol.families[]` | string[] | 否 | 协议的全部族 | 协议登记的族 id 子集（X1：`F`、`C`、`P`、`L`、`J`、`H`、`S`、`M`），去重 | 是 | — | — |
| `protocol.q_levels[]` | int[] | 否 | 协议的全部档 | 协议登记的档位子集（X1：0–4），去重升序 | 是 | — | `q = 0` 跨族共享（X1） |
| `attrs` | object | 否 | `{}` | 键为属性路径，值为标量 | 是 | Suite 约束 | 未登记路径与越界值报错（R-M28-32） |
| `evaluation.compare` | string | 否 | `paired` | 常量 `paired` | 否（计入分析摘要） | — | 同实例同种子配对 |
| `evaluation.ci.method` | string | 否 | `auto` | `auto`、`percentile`、`cluster_bootstrap_t`；为断言字段：非 `auto` 时须等于 M32 R-M32-16 对主统计量在所选 `estimand` 下推导的方法（`fixed_set` 对应 `percentile`，`family` 对应场景层 `cluster_bootstrap_t`），否则 `rule_violation`（`ci_method_inconsistent`） | 否（计入分析摘要） | 平台固定 | 方法由估计目标决定，本字段不提供选择（M32 R-M32-07、16） |
| `evaluation.ci.estimand` | string | 否 | Suite `scoring.estimand` | `family`、`fixed_set` | 否（计入分析摘要） | 平台固定 | — |
| `evaluation.ci.n_boot` | int 或 `auto` | 否 | `auto` | `auto`（R-M32-15 公式）或 10,000–200,000 的整数；分析时小于 R-M32-15 对该分析组给出的下限 `max(10000, ⌈20·m/α⌉)` 则报 `n_boot_below_bound` | 否（计入分析摘要） | 平台固定 | — |
| `evaluation.ci.level` | number | 否 | 0.95 | 枚举 0.90、0.95、0.99；映射为 M32 `Scoring.alpha = 1 − level`，同时作用于区间与 Holm 判定（§8.5 CR-M34-16） | 否（计入分析摘要） | 平台固定 | — |
| `evaluation.multiple_comparison` | string | 否 | `holm` | 常量 `holm` | 否（计入分析摘要） | — | — |
| `evaluation.metrics[]` | string[] | 否 | Suite 全部指标 | Suite `metrics[].id` 子集 | 否 | — | 只影响报告列 |
| `outputs.dir` | string | 否 | `runs/<name>` | 相对路径以配置文件目录为基准 | 否 | — | — |
| `outputs.trace_level` | string | 否 | `standard` | `off`、`minimal`、`standard`、`full` | 否 | 下限 `minimal` 且完整 | trace 不得影响结果（R-M33-46） |
| `outputs.trace_sample.episode_rate` | number | 否 | — | (0, 1]；Episode 级确定性抽样 | 否 | 禁止 | R-M33-46 |
| `outputs.report[]` | string[] | 否 | `[md, html]` | `md`、`html`、`csv`、`json` | 否 | — | — |
| `outputs.per_run_reports` | bool | 否 | `false` | — | 否 | — | 等价于 `bench report --runs` |
| `execution.host` | string | 否 | `batch` | `batch`、`inproc` | 否 | 平台固定 | `inproc` 只允许 `workers = 1` |
| `execution.workers` | int 或 `auto` | 否 | `auto` | `auto` 或 1–256 | 否 | — | R-M28-88 |
| `execution.max_tasks_per_child` | int | 否 | 50 | 1–10,000 | 否 | — | — |
| `ranked` | bool | 否 | `false` | 本地只允许 `false` | 是 | 平台强制 `true` | 规则组 `ranked` 由平台注入 |

兼容规则（`x-rules`，示例，完整集合由 CT 与各 Suite 清单给出）：规则组 `wheel_unavailable` 在安装态拒绝 wheel 中不可用的组件取值（R-M34-107）；`protocol` 存在时要求 P1 以上内核、协议包已安装且 `protocol.id` 与 Suite 兼容（具体规则由 X1 与 M31 给出）；`evaluation.ci.method` 与 `evaluation.ci.estimand` 一致；`observation_profile = L0` 要求 `execution_topology = centralized` 或 `ranked = false`；`network_profile = ns3@1` 禁止 `ranked = true`；`clock_profile = d4_quick` 禁止 `ranked = true`；`physics ∈ {kinematic, realistic}` 要求 `hifi` 可用；`interventions` 含 `perm` 要求 P1 以上内核；`decision.wake_on_rx = true` 要求 Suite 允许；`policies[].ref` 为 `image:` 时要求容器运行时可用。

#### 7.4.3 受限 YAML 载入规则

| 编号 | 规则 | 处置 |
|---|---|---|
| YL-01 | 只接受 UTF-8，无 BOM；文件 ≤ 1 MiB；嵌套深度 ≤ 32 | 超限报 `yaml_restricted` |
| YL-02 | 布尔只认 `true`、`True`、`TRUE`、`false`、`False`、`FALSE`；未加引号的 `yes`、`no`、`on`、`off`、`y`、`n` 及其大小写变体报 `yaml_ambiguous_scalar`，须加引号表示字符串 | 防止 YAML 1.1 隐式布尔；反例 `wake_on_rx: yes` |
| YL-03 | 整数只接受十进制 JSON 整数文法 `-?(0\|[1-9][0-9]*)`；`0x`、`0o`、`0b` 前缀、前导零（`010`）、数字分隔符（`1_000`）、六十进制（`1:30`）与前导 `+` 报 `yaml_ambiguous_scalar` | 防止 YAML 1.1 隐式整数（F-22）；反例 `repeats: 010`、`count: 1_000`、`n_boot: 0x2710` |
| YL-04 | 浮点只接受 JSON 数值文法 `-?(0\|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?` 中含小数部分或指数部分者，因此 `1e3`、`1.5e3`、`1e-3`、`-1E+3` 均解析为浮点；`.5`、`1.`、`.inf`、`.nan` 及其大小写变体报 `yaml_ambiguous_scalar` | JCS 无法表示非有限数；PyYAML 把多数指数写法解析为字符串（F-22）；反例 `level: .95`，正例 `level: 9.5e-1` |
| YL-05 | 空值只认 `null`；`~`、`Null`、`NULL` 与省略的值报 `yaml_ambiguous_scalar` | 空映射值须写 `null` 或 `{}` |
| YL-06 | 未匹配 YL-02 至 YL-05 文法、也不命中其歧义模式的未加引号标量按字符串保留（如 `2020-01-01`、`1.0.0`） | 不解析时间戳 |
| YL-07 | 映射键只允许字符串；重复键报 `yaml_duplicate_key` | — |
| YL-08 | 禁止锚点、别名与合并键 `<<` | 防止展开放大与隐式合并 |
| YL-09 | 禁止任何显式标签（`!`、`!!`） | 只构造 JSON 数据模型 |
| YL-10 | 多文档流报错；空文档报错 | — |
| YL-11 | 整数须在 int64 范围内，浮点须有限且可由 `py:sim/events/jcs.py` `jcs_number` 往返 | 与规范形式一致 |
| YL-12 | YL-02 至 YL-06 只作用于未加引号的标量；加引号的标量一律为字符串 | 字符串字段以引号消歧，如 `version: "1.0"` |

实现：`RestrictedLoader(yaml.SafeLoader)` 在类上以空表替换 `yaml_implicit_resolvers`，只登记 YL-02 至 YL-05 的三类解析器；在 `compose_node` 阶段对每个 `style is None`（未加引号）的 `ScalarNode` 按 YL-02 至 YL-05 的歧义模式检查标量文本，遇 `AliasNode` 或锚点报错；`construct_mapping` 检查重复键；读到第二个文档起始事件即报错。每条规则的反例与正例逐条列于 `tests/d4/sdk/test_yaml_restricted.py`（AC-M34-07）。

#### 7.4.4 自含校验器支持的 schema 关键字

`type`、`enum`、`const`、`required`、`properties`、`additionalProperties`（布尔或 schema）、`items`、`minItems`、`maxItems`、`uniqueItems`、`minimum`、`maximum`、`exclusiveMinimum`、`exclusiveMaximum`、`minLength`、`maxLength`、`pattern`（Python `re` 语法子集，禁用回溯引用）、`oneOf`（只用于互斥分支，按 `const` 判别字段选择）、`$ref`（仅文档内 `#/$defs/...`）、`x-rules`（规则求值器）、`x-since`（字段引入版本，供错误提示）。schema 使用上述集合之外的关键字时 CI 失败。

### 7.5 解析流水线与 Episode 展开

```
experiment.yaml
  │  YL-01..12（受限 YAML）                         → yaml_restricted / yaml_ambiguous_scalar / ...
  ↓
文档（JSON 数据模型） ── spec_sha256 = sha256(JCS(文档))
  │  requires.drone4d 检查                          → sdk_version_unsatisfied
  │  schema 校验（自含校验器，§7.4.4）               → field_unknown / type_mismatch / ...
  │  Helper 展开（track 简写、agents.template、clock_profile、--set）
  │  兼容规则（x-rules + Suite 规则）                → rule_violation
  │  本地约束（ranked、split、ref 种类、种子公开范围、wheel 可用性）
  │                                                 → split_platform_only / seed_out_of_public_range / component_unavailable_in_wheel
  │  policy_ref 解析（M30 登记表）                    → policy_ref_unknown / extra_missing
  │  属性解析（M28 AttrRegistry，层：登记缺省 < Suite < Helper < 实验 < cli）
  ↓
ResolvedExperiment ── experiment_id、experiment_config_sha256、analysis_config_sha256、labels、required_extras
  │  Episode 展开：policies × scenarios × seed_index × repeats，经 InterventionPlan 增删
  │  参考策略 Episode 去重共享；按 EpisodeKey 码点序排序；逐 Episode config_sha256 与 episode_id
  │  执行计划过滤（--policies 子集、续跑已完成项）       → plan.json
  │  策略预检（py: 策略，R-M34-105）                  → policy_preflight_failed
  ↓
EpisodeSpec[]（M28）→ BatchHost 迭代式结果流（CR-M34-01）→ 各 runs/<episode_id>/ → rows.jsonl → analyze_experiment → 报告
```

### 7.6 命令行参考

#### 7.6.1 命令树

| 命令 | 阶段 | 优先级 | 作用 | 主要参数 | 库实现 |
|---|---|---|---|---|---|
| `drone4d bench run <experiment>` | P0 | P0 | 运行实验 | `--out`、`--workers`、`--policies`、`--set`、`--attr`、`--dry-run`、`--assume-ms`、`--resume`、`--force`、`--fail-on-policy-error` | `drone4d.experiment`；M28 `BatchHost` |
| `drone4d bench report <dir>` | P0 | P0 | 生成实验级报告 | `--format`、`--runs`、`--out` | M32 `analyze_experiment`；M33 `render_report` |
| `drone4d bench init <dir>` | P0 | P1 | 生成工程模板 | `--template` | 本模块 |
| `drone4d env`、`drone4d version` | P0 | P1 | 环境与版本 | `--json` | 本模块 |
| `drone4d bench suite list` / `show <id>` / `check <dir>` | P0（`--gate` P1） | P1 | Suite 发现与校验 | `--gate` | M31 |
| `drone4d bench instance gen` / `check <pkg>` | P0 | P1 | 实例生成与校验 | 由 M31 定义 | M31 |
| `drone4d bench replay <run_dir>` | P1 | P1 | 动作重放或全量重仿真 | `--mode actions\|full`、`--allow-impl-change`、`--until`、`--diff`、`--out` | M33 `InputTape`、`diverge`；M28 |
| `drone4d bench verify <dir>` | P1 | P1 | 离线复核 | `--resim` | M33 `verify_run`；M32 |
| `drone4d bench baseline list` / `show <ref>` | P1 | P1 | 基线卡查询 | `--suite` | M30 索引 |
| `drone4d sdk pack` | P1 | P1 | 打包评测容器 | 见 R-M34-52 | 本模块 |
| `drone4d sdk check <image\|dir>` | P1 | P1 | 扫描项复现与本地冒烟 | `--suite` | 本模块；M28 `wire.v2` |
| `drone4d sdk serve` | P1 | P1 | 容器内 Agent 服务 | `--protocol`、`--policy`、`--baseline`、`--sock` | `sdk:serve.py` |
| `drone4d suite verify`、`audit`、`freeze`、`calibrate`、`retire`、`reveal-verify` | P1 | P1 | 平台侧（插件，wheel 中不存在） | 由 M31、M32 定义 | `awr` 发行版 |

全局参数：`--json`、`--quiet`、`--log-format text|json`（取值 `text` 或 `json`）、`--no-color`、`--cache-dir`、`--version`、`--help`。

#### 7.6.2 参数约定

| 约定 | 规则 |
|---|---|
| 路径 | 实验配置中的相对路径以配置文件目录为基准；命令行参数中的相对路径以当前工作目录为基准 |
| 列表参数 | 逗号分隔（`--policies a,b`）或重复参数（`--set a=1 --set b=2`），二者等价 |
| 布尔开关 | 只用肯定式长参数（`--resume`、`--context-only`、`--allow-impl-change`、`--fail-on-policy-error`、`--download`），不提供 `--no-` 前缀的反向开关，`--no-color` 除外 |
| 短参数 | 只提供 `-q`（`--quiet`）、`-o`（`--out`）、`-j`（`--workers`） |
| 覆盖记录 | `--set`、`--attr` 进入属性层 `cli` 并计入摘要（R-M34-18）；`--policies` 是执行计划过滤，只写入 `plan.json` 与 `analysis_config_sha256`，不进入属性层（R-M34-32） |

#### 7.6.3 退出码

| 退出码 | 名称 | 含义 | 典型原因码 |
|---|---|---|---|
| 0 | OK | 成功（可能带警告） | — |
| 1 | GENERIC | 未分类内部错误 | `internal_error` |
| 2 | CONFIG_INVALID | 参数、实验配置、规则、引用或策略预检错误 | `yaml_restricted`、`field_unknown`、`rule_violation`、`split_platform_only`、`seed_out_of_public_range`、`component_unavailable_in_wheel`、`policy_ref_unknown`、`policy_preflight_failed`、`out_dir_not_empty`、`out_dir_has_results`、`out_dir_unsafe` |
| 4 | DATA_MISSING | 所需 Suite、实例或参考缓存缺失 | `suite_not_installed`、`reference_cache_missing` |
| 5 | DATA_CORRUPT | 包数据或下载物摘要不符 | `package_data_digest_mismatch` |
| 8 | DEP_MISSING | 附加包、容器运行时、基础镜像或打包所需 wheel 缺失 | `extra_missing`、`container_runtime_missing`、`wheel_missing` |
| 10 | STATE_INCOMPATIBLE | 运行目录、trace 或检查点与当前版本不兼容 | `kernel_major_mismatch`、`resume_config_mismatch` |
| 11 | BUSY | 输出目录被另一进程锁定 | `out_dir_locked` |
| 21 | VERIFY_MISMATCH | 复核或重放发现不一致 | `verify_mismatch`、`replay_diverged` |
| 22 | RUN_INCOMPLETE | 实验完成但有 Episode 平台失败或 trace 不完整 | `failed_infra`、`trace_incomplete` |
| 23 | PLATFORM_ONLY | 命令或划分只在平台侧可用 | `platform_command`、`split_platform_only`（`bench run` 中优先报 2） |
| 24 | POLICY_FAILED | 给出 `--fail-on-policy-error` 时实验完成但有策略失败的 Episode（与 22 并存时取 22） | `policy_failures` |
| 130 | INTERRUPTED | 用户中断 | `interrupted` |

退出码 3、6、7、9、12–15、20 在本程序中不使用，保留其在 19 号中的含义。

#### 7.6.4 候选原因码段

M34 候选码段 880–899（待 CT 与 17 号 §18.12 登记）。登记前只以名称出现在 `--json` 与日志中。首批名称：`yaml_restricted`、`yaml_ambiguous_scalar`、`yaml_duplicate_key`、`field_unknown`、`sdk_version_unsatisfied`、`suite_digest_mismatch`、`split_platform_only`、`seed_out_of_public_range`、`component_unavailable_in_wheel`、`ci_method_inconsistent`、`n_boot_below_bound`、`override_invalid`、`out_dir_not_empty`、`out_dir_has_results`、`out_dir_unsafe`、`out_dir_locked`、`resume_config_mismatch`、`extra_missing`、`container_runtime_missing`、`wheel_missing`、`policy_step_missing`、`policy_preflight_failed`、`policy_failures`、`policy_impl_changed`、`payload_schema_missing`、`topology_mismatch`、`pack_sensitive_file`、`training_reward_unavailable`、`platform_command`。v0.1 的 `epoch_unresolved` 已取消（R-M34-05 改由内核按 R-M28-62 回执），`training_reward_undeclared` 更名为 `training_reward_unavailable`。

### 7.7 实验目录布局

```
runs/<name>/
  experiment.yaml               # 输入文件的逐字节副本
  experiment.resolved.json      # 规范化解析结果（JCS）：字段解析值、属性值与来源层、provenance、标签、摘要
  plan.json                     # 展开计划、所选策略、策略预检结果与墙钟估算（不入摘要，可删除后再生）
  episodes.jsonl                # Episode 索引：EpisodeKey → episode_id、状态（done、pending、failed_infra、failed_policy）、清单摘要
  runs/<episode_id>/            # 单次运行目录（以 episode_id 命名，不含尝试序号），布局见 M33 §6.4
  rows.jsonl                    # 逐 Episode 评测分量（M32 Row 视图，不含墙钟字段），按 EpisodeKey 排序
  analysis.json                 # analyze_experiment 输出（M32），含所含策略集合
  manifest.experiment.json      # 实验级清单，兼作本工具的目录标记：experiment_id、spec_sha256、experiment_config_sha256、
                                #   analysis_config_sha256、plan.selected_policies、versions、hw_class、
                                #   Episode 计数（completed、failed_infra、failed_policy、pending）、rows_sha256、analysis_sha256、wall{}
  report/ summary.md  index.html  table.csv  meta.json
  logs/   cli.log  workers/<n>.log      # 不入摘要
  .drone4d.lock                 # 运行期间存在
```

`manifest.experiment.json` 的 `wall` 命名空间记录开始与结束时刻、各阶段墙钟、worker 数与平台重跑计数。

#### 7.7.1 一致性比较集

本文凡称"逐字节一致"的判据，比较对象与豁免项以下表为准（R-M34-07、34、36、76）。

| 比较场景 | 比较对象 | 豁免项 |
|---|---|---|
| worker 数 1、4、8；续跑与一次跑完；`bench run` 与 `Experiment.run` | `experiment.resolved.json`；`episodes.jsonl`；`manifest.experiment.json`；`rows.jsonl`；`analysis.json`；各运行目录的 `evaluation.json` 与 trace 文件；各运行清单 | 实验级清单的 `wall`；运行清单的 `wall`、`attempt`、`run_id`、`infra_retries`（平台失败重跑属非确定事件，M28 R-M28-90）；`logs/`；`report/meta.json`；`plan.json` |
| 仓库路径与 wheel 路径（R-M34-76） | 同上；运行清单另比较 `trace.trace_events_digest`、`trace.truth_digest`、`digests.*`、`config_sha256`、`spec_sha256`、`attrs_digest` | 上一行全部豁免项；另豁免运行清单的 `versions.*` 与 `run_hash`（`run_hash = sha256(spec_sha256 ‖ JCS(versions))` 含 wheel 版本与发行版，M28 R-M28-73），实验级清单的 `versions`，以及由运行清单字节派生的摘要（如 `board_row.v1.run_manifest_sha256`，§8.5 CR-M34-14） |

`rows.jsonl` 使用 M32 `Row` 视图，不含 `recorded_unix_ns` 等墙钟字段，也不含 `run_manifest_sha256`；平台榜单使用的 `board_row.v1` 不在本地生成。

### 7.8 训练框架适配设计

#### 7.8.1 驱动方式

适配器在 `hosts.inproc` 上以 M28 外部步进接口驱动单个 Episode：`reset()` 依种子计划构造 `EpisodeSpec` 并执行 `Kernel.configure/prepare`，随后 `advance_to_decision()` 返回首个 `DecisionPoint`；`step(actions)` 把张量动作解码为各 Agent 的 `Decision`（经 R-M34-05 填充 epoch），调用 `submit()`，再 `advance_to_decision()`；返回 `None` 即终止。两种驱动方式对同一决策序列结果逐字节一致（R-M28-86），因此训练期动作序列可在评测期经 `bench replay` 复核。

#### 7.8.2 张量画像（缺省规则，Suite 可覆盖）

下表为适配器的缺省编码规则。Suite 在清单 `tensor_profiles`（CT §5.1.1 已登记为 `suite.v1` 的可选字段，字段由本文定义，取值由各 Suite 声明）中给出的取值与排序截断规则优先于缺省规则。S1 的 `tensor_profiles.default` 以 S1 PB-5（`Tmax = 128`，按 `task_id` 升序截断，P0 档位不触发截断；`inbox_max = 256`）与 S1 §9.3 的任务特征 `G = (x, y, s_j, r_j 位, m_j, w_j, l_j − now, v_j, 前驱已满足)` 为原文（§8.5 CR-M34-09）。符号：`N` 为 `possible_agents` 的长度；`sender_slot` 为发送方 `agent_id` 在 `possible_agents` 中的序号（自 0 起）；`E_max` 为每 Agent 受控实体数上限（Suite 声明，缺省 1）；`Δd` 为决策周期。

| 字段 | 含义 | 编码规则 |
|---|---|---|
| `D` | `self_state` 维数 | 受控实体按 `entity_id` 码点序取前 `E_max` 个，位置以 Suite `pos_scale_m` 归一化 |
| `Kmax`、`F` | 检测条数上限与每条特征维数 | 按（距离升序、`det_id`）排序截断，`mask` 为 1 表示有效 |
| `Tmax`、`G` | 任务条数上限与每条特征维数 | 缺省按（`status = open` 优先、距离升序、`task_id`）排序截断；S1 覆盖为按 `task_id` 升序截断（PB-5） |
| `Mmax`、`D_msg` | 收件箱条数上限与消息向量维数 | 只取 `vec_i8.v1` 消息，按（`delivered_at_ns` 降序、`message_id`）保留最新 `Mmax` 条；每条为 `[q/127 (D_msg), age/Δd, sender_slot/N]` |
| `nav` | 导航动作形式 | `box`：`Box(−1, 1, (2,))` 或 `(3,)`，按最大速度比例映射为 `vel_w`；`discrete`：Suite 登记的方向集合；`algorithm_isolated` 下不存在 |
| `msg_dest` | 学习型消息目的地 | `broadcast`（缺省）或 Suite 登记的组 `group:<id>` |
| `claim` | 认领动作 | `Discrete(Tmax + 1)`，索引 `Tmax` 为不动作；指向被 mask 的位置视为不动作并在 `info.invalid_action` 计数 |

#### 7.8.3 种子计划

`reset(seed=s, options=None)`：`options` 可显式给出 `{"scenario": <id>, "seed_index": k}`；否则按 `seed_schedule = dev_round_robin`，第 e 次 reset 取场景 `scen[(s + e) mod S]` 与种子序号 `⌊(s + e) / S⌋ mod K_dev`，只取 `dev` 场景集；其中 `scen[0..S−1]` 为该 `dev` 场景集的场景按 id 码点序排列，`S` 为场景数，`K_dev` 为该场景集声明的公开种子数 `exogenous_seeds.public`。适配器内部的随机性只经 `RandomOracle` 的 `train.env` 域取得，不使用 numpy 全局随机状态。使用过的训练种子范围写入训练日志，供 `submission.v2.declared.training_seeds` 填写。

#### 7.8.4 训练奖励与特权状态（Suite 声明）

| 字段 | 取值 | 说明 |
|---|---|---|
| `training_reward` | `team_terminal_U`（缺省，Suite 未声明时亦取此值）、`team_delta_U` | 前者终止或截断步给出 `U`，其余步为 0；后者每决策周期给出 `U` 的在线增量，只在 Suite 声明在线形式后可用；全队同值；适配器另接受用户提供的 `RewardFn`（R-M34-64） |
| `state_spec` | 维数与字段列表 | `state()` 的全局向量定义；只在特权模式可得（R-M34-65）；未声明时 `state()` 不可用 |

#### 7.8.5 适配器与框架的对应

| 适配器 | 依赖（`train` 附加包） | 验收 |
|---|---|---|
| `pettingzoo_parallel` | PettingZoo、Gymnasium | `parallel_api_test` 通过（AC-D4-28） |
| `pettingzoo_aec` | 同上 | AEC API 测试通过 |
| `torchrl_env` | TorchRL、TensorDict、PyTorch（CPU） | 与 PettingZoo 路径逐元素一致；吞吐入 DL-D4-01 |
| `benchmarl_task` | BenchMARL（经 TorchRL） | MAPPO 在 S1 算法隔离模式以固定种子完成训练与评测（AC-D4-28） |
| `tensor_policy` | PyTorch（CPU）或无（`NumpyMLPPolicy`） | 训练产物经 `bench run` 评测，结果与训练期同种子的动作序列一致 |

### 7.9 `sdk pack` 设计

打包上下文：

```
pack/
  Dockerfile              # 生成，文件头注明"由 drone4d sdk pack 生成"
  wheels/                 # drone4d 本身与 requirements.lock 中的全部 wheel：来自 --wheelhouse，缺失项仅在 --download 时补齐；均按哈希核对
  wheels/requirements.lock
  src/                    # --src 的过滤副本（R-M34-55）
  weights/                # --weights 的副本
  pack.lock.json          # drone4d.coord.pack_lock.v1
  submission.v2.draft.json
  sbom.spdx.json
```

Dockerfile 模板：

```dockerfile
# 由 drone4d sdk pack 生成；修改请重新执行 pack
FROM --platform=linux/amd64 <registry>/d4-agent-py@sha256:<基础镜像摘要>
COPY wheels/ /tmp/wheels/
RUN pip install --no-index --no-cache-dir --require-hashes -r /tmp/wheels/requirements.lock && rm -rf /tmp/wheels
COPY src/ /opt/agent/
COPY weights/ /opt/agent/weights/
ENV PYTHONPATH=/opt/agent PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 DRONE4D_AGENT_SOCK=/run/drone4d/agent.sock
USER 65534:65534
WORKDIR /tmp
CMD ["python", "-m", "drone4d.serve", "--protocol", "v2", "--policy", "<policy_ref>"]
```

构建命令固定为 `docker build --platform linux/amd64`（podman 同义），与 LB F6 的扫描项一致。`d4-agent-py` 基础镜像以 `python:3.12-slim` 的 `linux/amd64` 摘要为底，离线安装与 wheel 版本对应的 `drone4d`、numpy、msgpack、PyYAML，镜像摘要随 wheel 发布并写入兼容矩阵。镜像的可重复构建（同输入同摘要）为 P1 评估目标，不作为门禁。

### 7.10 wheel 构建流程与命名空间重写

#### 7.10.1 构建步骤

| 步 | 动作 | 失败条件 |
|---|---|---|
| 1 | 读取 `vendor.toml`，核对源树干净（发布构建） | 工作树有未提交改动 |
| 2 | 复制 `sdk:` 源到暂存目录 | — |
| 3 | 以 AST 遍历每个源文件的全部 `Import`/`ImportFrom` 节点（含函数体、类体、条件分支、`try` 与 `TYPE_CHECKING` 块）做闭包检查；对映射表内每个源文件执行词法级导入重写，写入 `drone4d/_core/<目标路径>`；按 NS-05 生成或复制包 `__init__.py` | 违反 NS-01 至 NS-08 |
| 4 | 按 `tools/sdk/package_data.toml` 复制包数据（契约 schema、M29 网络参数集、Suite 清单、`dev` 参考缓存、模板、D3 感知模型） | 预期文件缺失或多余；映射包内存在未声明的非 `.py` 文件 |
| 5 | 写 `SOURCES.json` | — |
| 6 | 以 `SOURCE_DATE_EPOCH` 构建 wheel 与 sdist | 构建失败 |
| 7 | 后置核对：内容清单、元数据（`Requires-Dist` 与附加包集合精确匹配）、体积 ≤ 50 MB、许可文件存在 | 任一不符 |
| 8 | 干净环境安装与导入闭包测试（R-M34-77）、路径一致性测试（R-M34-76） | 任一不符 |

#### 7.10.2 映射表（`vendor.toml` 初版）

| 源前缀 | 目标前缀 | 排除 |
|---|---|---|
| `awr.kernel` | `drone4d._core.kernel` | `hosts/eval/`（平台评测宿主）；`hosts/hifi/`（按 Q-M34-03） |
| `awr.interaction` | `drone4d._core.interaction` | `ns3`（P2 桥，按其依赖另定） |
| `awr.coordination` | `drone4d._core.coordination` | — |
| `awr.benchmarks` | `drone4d._core.benchmarks` | `platform/`、`suites/drone4d_event/` |
| `awr.evaluation` | `drone4d._core.evaluation` | D3 兼容入口（委托 `py:bench/eval/aggregate.py` 的部分） |
| `awr.trace` | `drone4d._core.trace` | 无；MCAP Sink 位于 `py:trace/sinks/mcap.py`（P1，M33 §13 第 2 条、Q-M33-01），依赖 `mcap`，随基础 wheel 分发、使用时延迟导入，所属附加包随 WP-D4-24 登记 |
| `awr.sim.sensors.cbrng` | `drone4d._core.sim.sensors.cbrng` | — |
| `awr.sim.events.jcs`、`.errors` | `drone4d._core.sim.events.*` | `tape`：函数体内导入 `awr.environment.*`、`awr.sim.fleet.*`，读取依赖 zstandard，只服务网格族，不进入 wheel（F-20） |
| `awr.phys`（`__init__` 原样复制，仅 `PhysError`）、`awr.phys.adapter`、`awr.phys.pose_history` | `drone4d._core.phys.*` | — |
| `awr.swarm.allocation`（`__init__` 原样复制、`hungarian`、`score`） | `drone4d._core.swarm.allocation.*` | —；`hungarian` 的 scipy 只在调用时装载，只在 `ref` 基线路径被调用 |
| `awr.swarm.coverage`（`__init__` 原样复制、`lanes`、`partition`、`plan`、`sensor`） | `drone4d._core.swarm.coverage.*` | `gsd`、`levels`（不被 `__init__` 导入，核心基线不用）；`partition.assign_chunks` 调用 `hungarian`，只用于 `ref` 变体 |
| `awr.tasking.{allocator, greedy, model, check, energy, geom, keepout}`（`ref` 级，待定） | `drone4d._core.tasking.*` | 其余子模块；是否进入 wheel 与具体清单由 M30 与 M34 在 WP-D4-01 闭包审计中定案（M30 §12 第 12 条，Q-M34-10），定案前不登记，`ref` 基线 `d2_greedy` 等只在仓库环境可用 |
| `awr.agent.runtime.{allocator, clock, effect, network, scoring, tsir, types}`（`ref` 级，待定） | `drone4d._core.agent.runtime.*` | 同上；真实 `TaskManager` 不进入，以门面替代（M30 F5） |

`awr/swarm/__init__.py` 只有模块说明，按 NS-05 生成为空包。除标为 `ref` 级的两行外，其余条目均为核心级，构成 v0.2 的 `vendor.toml` 初版，`ref` 级条目以 `tier = "ref"`、`status = "pending"` 登记但不参与构建。

`awr.agentsdk.env.link`、`geom` 与 `awr.agentsdk.rng`（`fspl-nlos@1` 遗留档）不进入 wheel：M29 规定该档只服务 `drone4d-event@1` 与 D3 黄金运行对拍（M29 §0 第 3 条、§4.8），二者依赖网格世界与事件磁带，不在 wheel 范围内；wheel 中选用 `fspl-nlos@1` 由 R-M34-107 在解析阶段拒绝。这一结论与 AWR-06 §3.3 闭包表"进入 numpy-only 核心：是（仅 `fspl-nlos@1` 遗留档）"不一致，列入 §13 第 23 条。`abstract-radio@1` 在 wheel 中的通信区只来自实例 `comm.json`，磁带通信区只在仓库环境装配（§8.5 CR-M34-12）。

#### 7.10.3 重写规则

| 编号 | 规则 | 检查方式 |
|---|---|---|
| NS-01 | 只改写导入语句中映射前缀的模块路径；被 vendoring 的代码出现映射表之外的 `awr.*` 导入即构建失败（闭包不完整）；扫描范围为 AST 中全部 `Import`/`ImportFrom` 节点，包括函数体、类体、条件分支、`try` 块与 `TYPE_CHECKING` 块内的导入，后者只用于类型标注者须登记为类型专用例外 | AST 扫描 + 词法重写 |
| NS-02 | 非文档字符串的字符串字面量不得以 `awr.` 开头（如事件处理器点路径、logger 名、动态导入目标）；需要模块身份时经 `_ns.qualify(logical_name)` 取得 | AST 扫描；D4 核心包的 lint 同时启用 |
| NS-03 | 不得以非字面量参数调用 `importlib.import_module`/`__import__` 加载 `awr.*`；插件与组件以逻辑 id 登记（`ComponentRegistry`，M28），不以模块路径登记；已知的非字面量调用（`cbrng.warmup`，F-10）登记于 NS-04 白名单，并由 lint 保证 wheel 内无调用点 | AST 扫描 |
| NS-04 | 依赖 `__package__`、`__name__` 的行为须登记在白名单并附说明；首批登记 `cbrng._KC_NAME`：wheel 中 numba 外积核永不启用，结果与 numpy 路径逐位相同；`cbrng.warmup()` 在 wheel 中不得调用（F-10） | 白名单核对；调用点 lint |
| NS-05 | 中间包的 `__init__.py` 缺省生成为只含说明字符串的空包，不复制源文件。例外：映射表以整包登记的包原样复制其 `__init__.py`（首批 `awr.phys`、`awr.swarm.allocation`、`awr.swarm.coverage`，F-11、F-21），前提是其导入的子模块全部在映射内，且不含依环境变量或运行时条件的导入（AST 断言：无 `os.environ`/`os.getenv` 访问，无位于 `if`/`try` 内的导入）；导入子模块的 `__init__` 若被生成为空包会改变包的公开面，因此不得生成；`awr/sim/sensors/__init__.py`（依 `AWR_PLUGINS` 条件导入，F-10）不满足前提，一律生成 | 生成器断言；AST 断言 |
| NS-06 | 包数据只经 `importlib.resources.files(__package__)` 访问，不经仓库相对路径或 `AWR_CONTRACTS_DIR` | AST 扫描；AC-D4-43 |
| NS-07 | 检查点、trace、运行清单与评测输出不得含 pickle 或类的限定名；跨路径的持久化格式只用纯数据（msgpack/JSON） | 黄金文件比对 |
| NS-08 | 摘要（`trace_events_digest`、`truth_digest`、`rows_sha256`、`analysis_sha256`）的输入不得含模块路径或发行版名；运行清单以 `versions.distribution ∈ {awr, drone4d}` 显式标识路径，该字段不参与一致性比较 | 路径一致性测试（R-M34-76） |

### 7.11 依赖与附加包矩阵

| 附加包 | 依赖 | 启用的模块或能力 | 阶段 | 说明 |
|---|---|---|---|---|
| （核心） | numpy；建议另含 PyYAML、msgpack（Q-M34-02） | 内核、交互网络、numpy-only 基线、P0 Suite、评测、trace（gzip）、命令行、`wire.v2` Agent 侧 | P0 | 内核导入闭包只有 numpy |
| `ref` | scipy | 周期匈牙利基线、滚动 MILP 集中式参考的本地复算、`AllocatorSolver` 分组路径 | P0 | `dev` 的 `π_central` 缓存随 wheel 分发，无 `ref` 时也可作为参考行 |
| `zstd` | zstandard | trace 的 zstd 承载 | P0 | 未安装时 trace 用 gzip（AWR-06 §4.5）；事件磁带不在 wheel 中（F-20） |
| `hifi` | open3d、scipy、zstandard | `hosts.hifi`（网格世界、`KinematicTier`、`L1Model`、事件磁带） | P1 | 代码随 wheel 分发的前提是映射表扩展（Q-M34-03，F-12、F-20） |
| `train` | PettingZoo、Gymnasium、PyTorch（CPU）、TorchRL、TensorDict、BenchMARL | 训练框架适配器、`TensorPolicyAdapter` | P1 | 精确版本于 WP-D4-24 锁定；不得传递引入 GPL |
| `client` | httpx | D3 `drone4d.bench.BenchClient` | P0 | 由硬依赖改为附加包（R-M34-82） |
| `images` | numpy、pillow | D3 `drone4d.perception`、`Frame.image()` | 保留 | 原样 |

### 7.12 文档站结构

D4 页面位于两种语言树下的 `research/` 目录，构建为版本 `0.4`：

```
docs-site/{zh_CN,en}/research/
  index.md                          协同实验平台总览与导航
  intro/      overview.md  install.md  quickstart.md            （P0；quickstart 即 §4）
  tutorials/  t1-policy.md                                     （P0）
              t2-suite.md  t3-network.md  t4-marl.md  t5-pack.md（P1）
  guides/     experiment-config.md  reproduce-verify.md  migrate-d3.md（P1）  baselines.md（P1）
  concepts/   agent-entity-assignment.md  time-and-events.md  determinism.md  networks.md
              statistics.md  leaderboard.md
  reference/  cli.md（由 argparse 生成）  experiment-v1.md（由 schema 生成）  attrs.md（由 AttrRegistry 生成）
              api/coord.md  api/experiment.md  api/adapters.md（autodoc）  reason-codes.md  suites/（由清单生成）
  project/    versioning.md  compatibility.md  changelog.md  contributing.md  review.md  code-of-conduct.md
              registry.md  license.md  faq.md                   （P1）
docs-site/_shared/tutorials/<id>/   steps.sh、源文件、captured/*.txt（RM-8 真实输出）
```

| 门禁 | 内容 |
|---|---|
| 构建 | 两种语言文档名一致性检查（现有 `drone4d_site`）；API 译文完整性检查；告警即失败 |
| 执行 | `steps.sh` 在干净容器中逐步执行（快速入门与教程一为 P0，其余为 P1） |
| 一致 | 快速入门的 `experiment.yaml` 与 `my_policy.py` 经 `literalinclude` 引自 `templates/northstar/`，与北极星门禁文件同一来源 |

### 7.13 兼容矩阵与弃用

#### 7.13.1 兼容面

| 面 | 稳定级别 | 允许的变更（`0.4.x` 内） | 破坏性变更的处置 |
|---|---|---|---|
| `drone4d.coord`、`drone4d.experiment`、`drone4d.adapters.*` | 稳定 | 新增符号、带缺省值的可选参数 | 弃用流程（R-M34-94），下一产品系列移除 |
| `drone4d.coord.experimental` | 预览 | 任意，须记入变更记录 | 无保证 |
| `drone4d._core`、`drone4d._cli` | 内部 | 任意 | 无保证；文档不出现 |
| 命令、参数、退出码、`--json` 字段 | 稳定 | 新增命令、参数、字段与原因码 | 弃用流程 |
| `experiment.v1` | 稳定 | 新增可选字段（带 `x-since`） | 新主版本 `experiment.v2`，旧版本至少保留一个赛季 |
| 实验目录与运行目录布局 | 稳定 | 新增文件 | 读侧兼容视图（M33） |
| `wire.v2` | 稳定 | 不改帧表；新增特性只经 `HELLO.features[]` 协商 | 新主版本 `wire.v3`（ADR-D4-31） |
| D3 面（`agent`、`wire` v1、`baselines`、`perception`、`bench`） | 冻结 | 只接受 D3 封板裁决的修复与延迟导入 | 随 `drone4d-event@1` 退役评审 |

#### 7.13.2 兼容矩阵（每次发布更新，示例列）

| `drone4d` | `KERNEL_VERSION` | `ct:coord` | 线协议 | Suite | Python | numpy | `d4-agent-py` |
|---|---|---|---|---|---|---|---|
| `0.4.0aN`（P0） | `1.0.0-dev` | `v1` 草案 | `wire.v1`、`wire.v2` | `taskalloc`、`coopsearch-handoff`、`infoshare` 的 `dev` | 3.12 | `>=2.0,<3`（测试于 2.5.3） | 摘要随发布 |

#### 7.13.3 `experiment.v1` 的演进规则

`experiment.v1` 保持 `additionalProperties: false`，以便拼写错误立即报错。新增可选字段视为次版本变化并以 `x-since` 标注：旧版本 `drone4d` 读到新字段时报 `field_unknown` 并提示核对 `requires.drone4d`，这一拒绝是期望行为，因为配置的生产者是作者、消费者是本机安装的 `drone4d`，不存在"旧端须静默忽略新字段"的需要。该规则与 ADR-D4-31"`additionalProperties: false` 的对象新增字段一律视为主版本变化"不一致，列入 §13 第 16 条。

---

## 8. 契约

契约文件由 CT 起草、M00 合入；本节只列 M34 的字段需求。

### 8.1 `drone4d.coord.experiment.v1`

字段集合与约束以 §7.4.2 为准；`x-rules` 以 §7.4.2 末段为首批；`x-since` 为每个字段的引入版本。

### 8.2 `drone4d.coord.cli_result.v1`（CT §2.1 已登记，`ct:coord/cli_result.v1.schema.json`；v0.1 的名称 `drone4d.cli.result.v1` 按 CT §9 第 18 项更名）

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema` | string | 常量 |
| `command` | string | 如 `bench run` |
| `ok` | bool | 退出码为 0 时为 `true` |
| `exit_code` | int | §7.6.3 |
| `reason` | string \| null | 原因码名称 |
| `stage` | string \| null | 失败所在阶段（§7.5） |
| `issues[]` | object[] | `{stage, pointer, code, message}`，`pointer` 为 JSON 指针 |
| `data` | object | 命令特定结果（如 `bench run` 的 `out_dir`、`experiment_id`、`experiment_config_sha256`、`episodes`、`failed_infra`、`failed_policy`） |
| `warnings[]` | object[] | `{code, message}`，含弃用警告与 `policy_failures` |
| `versions` | object | `drone4d`、`kernel`、`api_level` |

### 8.3 `drone4d.coord.pack_lock.v1`（CT §2.1 已登记路径，字段由本文定义）

`drone4d_version`、`base_image{ref, digest}`、`platform`（常量 `linux/amd64`）、`policy_ref`、`topology`、`src_tree_sha256`（相对路径与内容摘要的有序清单的摘要）、`requirements_sha256`、`wheels[]{name, sha256}`、`weights[]{path, sha256}`、`image_digest`（未构建时为 `null`）、`builder{name, version}`、`wall{built_at}`（不参与比较）。

### 8.4 Suite 清单增补（CT §5.1.1 已登记字段名，取值向 M31 与各 Suite 提出）

`tensor_profiles{<id>: {D, E_max, Kmax, F, Tmax, G, Mmax, D_msg, nav, msg_dest, pos_scale_m, sort_rules?}}`、`training_reward`（`team_terminal_U` 缺省，`team_delta_U` 须给出在线形式）、`state_spec`（§7.8.2、§7.8.4）。字段由本文定义，取值由各 Suite 声明，未声明的字段按 §7.8.2 缺省规则；AC-M34-20、21 以 S1 声明上述取值为前置条件（§8.5 CR-M34-09、10）。

### 8.5 跨模块变更请求

本节列出 M34 依赖、但并列文档目前未提供或口径不一致的能力。对方文档确认前，表中"受影响验收"列的验收判为待决，不作为门禁判据；确认后在对方文档中引用本表编号。

| 编号 | 对方 | 现状（已核实） | 请求 | 关联需求 | 受影响验收 |
|---|---|---|---|---|---|
| CR-M34-01 | M28 | §6.3 `BatchHost.run(specs, policy_refs) -> list` 同步返回完整列表，无进度、增量结果、取消或 worker 初始化参数 | 增加迭代式接口 `BatchHost.iter_run(specs, policy_refs, *, worker_init, cancel) -> Iterator[EpisodeResult]`：按完成次序产出（结果含 `episode_id`）；`worker_init` 携带 `py:` 搜索路径与父进程固定的环境（R-M28-88）；`cancel.request(graceful=True)` 停止派发并等待在途 Episode，`graceful=False` 立即终止 worker；`run()` 保留为其薄封装 | R-M34-17、34、37、38 | AC-M34-06、11 |
| CR-M34-02 | M28、CT | 运行清单 `versions` 为内核、wheel、Python、numpy、组件版本与镜像摘要（M28 §7.2.1、CT §4.5.2），无 API 级别与发行版标识 | `versions` 增键 `drone4d_api_level` 与 `distribution ∈ {awr, drone4d}`；二者计入 `run_hash`，仓库路径与 wheel 路径的 `run_hash` 因此不同，列入 §7.7.1 路径豁免 | R-M34-09、76；NS-08 | AC-M34-27、33 |
| CR-M34-03 | M28 | 逐 Episode `config_sha256` 以 M27-FR-007 的排除集合计算，而 `EpisodeSpec` 含 `trace_level`、`out_dir` | 二者不影响结果（R-M33-46），从 `config_sha256` 的输入中排除，否则改变 trace 级别会改变 `episode_id` | R-M34-19、21 | AC-M34-09 |
| CR-M34-04 | M28 | R-M28-32 把 `--policies` 列入 `cli` 层 | `--policies` 已改为执行计划过滤（R-M34-32），不进入属性层；R-M28-32 的括注删去 `--policies` | R-M34-32 | AC-M34-36 |
| CR-M34-05 | M28 | R-M28-76 的硬件等级探测接口待 WP-D4-01 冻结 | 提供 `probe_hw_class() -> HwClass`，以与 `hosts.batch` 相同的 spawn 与环境固定路径在子进程中执行，供 `drone4d env` 调用 | R-M34-51 | AC-M34-01 |
| CR-M34-06 | M30 | `EPOCH_CURRENT` 与决策定稿步骤在 M28、M30、M33 中无登记；CT §4.3.2 只规定哨兵"在发送前解析为具体值，不出现在线上" | `py:coordination/hosting.py` 增 `finalize_decision(obs, decision) -> Decision`（规则见 R-M34-05）；`make_policy` 的进程内包装与 `drone4d.serve` 的容器路径调用同一函数；M30 §6.2 写明 `EntityCommand` 由 SDK 构造时的 epoch 约定 | R-M34-05 | AC-M34-05 |
| CR-M34-07 | M30 | M30 §7.2 把 `CallbackAdapter` 列为宿主内部接口 | 确认 `drone4d.coord` 的再导出范围（§7.2）；`CallbackAdapter` 维持内部接口，研究者以 `CoordinationPolicy` 直接登记、由宿主适配；若 M30 决定对外公开，则加入预览面 | R-M34-01 | AC-M34-03 |
| CR-M34-08 | M30 | `drone4d.coord.msg.vec_i8.v1` 不在 M30 的消息 schema 登记中 | 在 `ct:coord/msg/` 登记 `vec_i8.v1`：`x-awr-info-class = opaque`，载荷为长度等于 Suite 张量画像 `D_msg` 的 int8 字节串，`$id` 按 CT §4.2.3 | R-M34-62 | AC-M34-21 |
| CR-M34-09 | M31、S1、S2、S3 | CT §5.1.1 只登记了 `tensor_profiles` 等字段名；M31 与各 Suite 无取值 | 各 Suite 清单给出 `tensor_profiles` 取值；S1 以 PB-5 与 §9.3 的特征 `G` 为 `tensor_profiles.default` 的原文 | R-M34-61 | AC-M34-20 |
| CR-M34-10 | M31、S1 | S1 §9.3"奖励为训练接口自定义，评测恒用 `U_TA`"；`U_TA` 的在线形式未定义 | S1 声明 `training_reward = team_terminal_U`；`team_delta_U` 在 S1 给出 `U_TA` 的在线增量定义后开放；`state_spec` 由 Suite 声明 | R-M34-64、65 | AC-M34-21 |
| CR-M34-11 | M28 至 M33、各 Suite | 包数据清单只在 M34 中列举，M29 的参数集曾被遗漏 | 各所有者模块在其 PRD 中声明随 wheel 分发的包数据路径与预期内容清单；M34 汇总为 `tools/sdk/package_data.toml` 并核对 | R-M34-73 | AC-M34-25 |
| CR-M34-12 | M29 | R-M29-57 由 `TapeReader.comm_zones_at` 读取通信区；tape 不进入 wheel（F-20） | 通信区读取经来源接口抽象：平面族来自实例 `comm.json`，磁带通信区适配只在仓库环境或 `hosts/hifi` 装配；核心网络包不导入 `awr.sim.events.tape` | R-M34-71、107 | AC-M34-27、37 |
| CR-M34-13 | M33 | UC-02"修改算法后以 `bench replay` 动作重放旧运行"与动作重放"不启动 Agent"的定义矛盾 | UC-02 改为全量重仿真加 `--allow-impl-change`（R-M34-44）；动作重放用于同一实现的复核 | R-M34-43、44 | AC-M34-14 |
| CR-M34-14 | CT | `board_row.v1` 必填 `recorded_unix_ns` 与 `run_manifest_sha256`（运行清单"规范化后的摘要"，未排除 `wall`） | `run_manifest_sha256` 的输入排除 `wall` 节；本地 `rows.jsonl` 使用 M32 `Row` 视图，不生成 `board_row.v1` | R-M34-36、76 | AC-M34-09、27 |
| CR-M34-15 | CT、LB | `submission.v2.signature` 自 P1 起必填，签名主体与工具未定义 | `sdk pack` 只生成不含签名的草稿；签名由独立命令或平台受理渠道承担，在 LB 中指定 | R-M34-56 | AC-M34-17 |
| CR-M34-16 | M32 | `Scoring` 只有 `alpha`，无 `method`、`level`；区间方法由估计目标决定（R-M32-16） | 确认本地可调范围：`ci.level ∈ {0.90, 0.95, 0.99}` 映射为 `Scoring.alpha = 1 − level`；`ci.method` 只作一致性断言；`n_boot` 下限按 R-M32-15 | §7.4.2 `evaluation.*` | AC-M34-07 |
| CR-M34-17 | X1 | X1 §12.6 第 15 条请求 `protocol` 块与复合引用校验；§12.7 样例已改用 `method: percentile` | v0.2 已增 `protocol` 块与复合引用（§7.4.2），请 X1 将该条标为已响应，并在 X1 中给出 `protocol` 的展开规则引用 | §7.4.2 | AC-M34-07 |

---

## 9. 实现指引

### 9.1 复用

| 现有代码 | 复用方式 | 约束 |
|---|---|---|
| `sdk:wire.py`、`sdk:serve.py` | `v1` 路径原样；`serve.py` 增 `--protocol`、`--policy` | B-06；D3 SDK 测试全部通过 |
| `sdk:agent.py` `DroneAgent` | 经 M30 `DroneAgentPolicy` 与 legacy-v1 投影运行 | 不改代码 |
| `py:agentsdk/env/containers.py` 的容器参数 | 作为 `sdk check` 本地容器宿主的规格，自含实现 | wheel 不导入 `awr` |
| `tools/deploy/dmax/images/agent/Dockerfile` 的镜像约定 | 作为 `d4-agent-py` 与 `sdk pack` 模板的规格 | 改为安装 wheel |
| `py:sim/events/jcs.py` `jcs`、`jcs_number` | 规范形式与 YL-10 | 经 vendoring |
| `py:bench/spec_rules.py` 的规则语义 | 规格来源；求值器由 M31 自含实现（M31 F-09），本模块调用 | 不导入 `awr.bench` |
| M28 `BatchHost`、`InprocHost`、外部步进接口 | `bench run`、适配器的执行层 | — |
| M32 `analyze_experiment`；M33 `render_report`、`verify_run`、`InputTape`、`diverge` | 报告、复核与重放的库实现 | 命令行不复制其逻辑 |
| `docs-site/_shared/` 的扩展与模板 | 版本浮层、文档名一致性检查、译文登记模式 | 构建配置改动经文档站维护者评审 |

### 9.2 实施顺序

| 周次 | 内容 | 前置 | 工作包 |
|---|---|---|---|
| 第 5–6 周 | 受限 YAML、自含校验器、解析流水线、Episode 展开（以 WP-D4-43 竖切的规格为首个用例） | WP-D4-43 | WP-D4-08 |
| 第 6–7 周 | `drone4d.coord` 再导出与构造函数、哨兵 epoch；`bench run` 接 `BatchHost`；北极星模板与 `tests/perf/northstar/` | WP-D4-03（实体与租约）、WP-D4-14（S1 基线） | WP-D4-08 |
| 第 7–8 周 | `bench report`、`init`、`env`、`--dry-run`、续跑与中断；`bench suite`、`bench instance` 封装 | M32 `analyze_experiment`、M31 命令库 | WP-D4-08 |
| 第 8–10 周 | 文档站：快速入门、教程一、API 参考与可执行教程 CI | WP-D4-08 | WP-D4-45 |
| 第 9–10 周 | `build_wheel.py`、映射表、NS 规则、包数据、可重复构建、支持矩阵、许可与 SBOM、私有索引发布；第 10 周在 wheel 上于 RM-8 实测北极星 | WP-D4-08；ADR-D4-32 裁决 | WP-D4-44 |
| 第 15–17 周 | 适配器四件与 `TensorPolicyAdapter`；`sdk pack/check/serve`；`bench replay/verify`；吞吐实测 | WP-D4-04（`wire.v2`）、M33 P1 库 | WP-D4-24 |
| 第 18–20 周 | 教程二至五、迁移指南、复现指南、项目页 | WP-D4-24 | WP-D4-47 |

### 9.3 降级

| 触发 | 降级动作 | 不降级项 |
|---|---|---|
| WP-D4-08 在第 8 周末未完成 | `bench init`、`env`、`bench suite/instance` 封装、`--dry-run` 的墙钟估算（保留 Episode 计数与预检）、`--policies` 子集复用、报告的 csv 格式与中文消息目录移入 P1 第 1 周；快速入门暂以手写 `experiment.yaml` 替代 `init` | `bench run`（含续跑、中断、策略预检）、`bench report`（md、html）、北极星模板 |
| WP-D4-44 在第 10 周末未完成 | macOS arm64 移出 P0 支持矩阵（北极星门禁只在 Linux x86-64 上执行）；可重复构建（AC-M34-28）改为 P1 门判据；私有索引发布改为向受邀用户分发 wheel 文件与约束文件 | 命名空间重写与 NS 规则、导入闭包、路径一致性、许可扫描与 SBOM（RK-17：裁决与扫描通过前不发布） |
| WP-D4-24 人力不足 | 顺序保留 PettingZoo Parallel → BenchMARL → TorchRL 原生实现 → AEC；`sdk check` 简化为握手检查 | AC-D4-28 的两项 |
| 文档英文页滞后 | 不降级：现有构建在缺页时失败（F-14），须在估算中计入双语工作量（§13 第 14 条） | — |

### 9.4 工作量估算（自下而上）

以下为 M34 自己的估算（人周），用于 WP-D4-01 中复核 AWR-06 §10.2 的工作包分配；含单元与命令行测试，不含评审与返工。

| 工作包（AWR-06 分配人周） | 内容分组与估算（人周） | 合计 |
|---|---|---|
| WP-D4-08（2.0） | 受限 YAML 载入器与反例集 0.3；自含 schema 校验器与 `x-rules` 调用 0.4；解析流水线、Helper 展开、Episode 展开与三项摘要 0.6；`drone4d.coord` 再导出、自由函数与决策定稿接入 0.3；`bench run` 接批量宿主（续跑、中断、锁、策略预检、失败处置）0.8；`bench report`（md、html、csv）0.5；`init`、`env`、`--dry-run` 估算、`version` 0.4；命令框架、退出码、`--json`、中英文消息目录 0.4；`suite`、`instance` 封装 0.2；北极星模板与 `tests/perf/northstar/` 0.2；测试 0.6 | 4.7 |
| WP-D4-44（1.0） | `build_wheel.py` 词法重写与 NS-01 至 NS-08 的 AST 检查 0.6；`vendor.toml` 与闭包审计（含 `awr.swarm`、tape 处置）0.3；包数据汇总与核对 0.2；可重复构建 0.2；支持矩阵 CI（Linux、macOS）0.4；路径一致性测试 0.3；SBOM 与许可扫描门禁 0.3；私有索引发布与约束文件 0.2；版本一致性 lint 0.1 | 2.6 |
| WP-D4-45（1.0） | 双语快速入门与教程一 0.6；`autodoc` 接入与 API 译文登记 0.4；`0.4` 版本路径机制 0.3；可执行教程 CI 与输出捕获 0.4；命令行、实验配置与属性参考的生成 0.3 | 2.0 |
| WP-D4-24（2.5，M34 部分） | PettingZoo Parallel 与 AEC 0.6；TorchRL 0.5；BenchMARL 任务族与配方接入 0.6；`TensorPolicyAdapter` 与 `NumpyMLPPolicy` 0.4；`sdk pack`、`sdk check`、`sdk serve --protocol v2` 1.0；`bench replay`、`bench verify` 命令封装 0.4；吞吐实测 0.3 | 3.8 |
| WP-D4-47（2.0） | 教程二至五（双语）1.2；迁移指南、复现指南与 FAQ 0.6；项目页 0.4；可执行教程 CI 扩展 0.3 | 2.5 |

P0 部分（WP-D4-08、44、45）合计 9.3 人周，AWR-06 分配 4.0 人周；P1 部分（WP-D4-24 的 M34 部分、WP-D4-47）合计 6.3 人周，分配 4.5 人周。差额的处置见 §9.3 降级表与 §13 第 7、14、18、28 条。

---

## 10. 测试与验收

### 10.1 测试布局

| 路径 | 内容 | 环境 |
|---|---|---|
| `tests/d4/sdk/test_yaml_restricted.py`、`test_validate.py`、`test_resolve.py`、`test_expand.py` | 载入、校验、解析、展开的正反例；与 jsonschema 的交叉验证 | 仓库 |
| `tests/d4/sdk/test_min_policy.py` | 行数判据与模板一致性 | 仓库 |
| `tests/d4/sdk/cli/` | 命令行黄金输出（`--json`）、退出码表、帮助文本不导入 numpy | 仓库 |
| `tests/d4/sdk/wheel/` | 构建、内容清单、可重复构建、导入闭包、路径一致性 | 干净容器 |
| `tests/d4/sdk/adapters/` | PettingZoo API 测试、TorchRL 一致性、BenchMARL 冒烟（未装 `train` 时跳过并在 CI 的 `train` 作业中执行） | 仓库 + `train` |
| `tests/d4/sdk/pack/` | 打包上下文与锁文件、敏感文件拦截、`sdk check` 冒烟（需容器运行时） | 容器节点 |
| `tests/perf/northstar/` | `config.yaml`、`my_policy.py`、`run.sh`；门禁计时 | RM-8 |
| `docs-site/_shared/tutorials/*/steps.sh` | 可执行教程 | 干净容器 |

### 10.2 验收

内容中标注"前置：CR-M34-xx"的验收依赖 §8.5 的跨模块变更请求，在对方文档确认前判为待决，不作为门禁判据。

| 编号 | 内容 | 对应需求 | 对应 AWR-06 | 环境 |
|---|---|---|---|---|
| AC-M34-01 | 干净环境中自 `pip install drone4d` 完成起执行 §4.1 的步骤 1–6，墙钟 ≤ 10 min，实验墙钟 ≤ 6 min，退出码 0；`bench init --template northstar` 的产物与 `tests/perf/northstar/` 逐字节一致；`drone4d env` 报告的 worker 等级与 `hosts.batch` worker 写入运行清单的 `hw_class` 一致（前置：CR-M34-05） | R-M34-31、50、51、99、100 | AC-D4-11、AC-D4-12 | RM-8 |
| AC-M34-02 | 报告含 §5.5 规定的全部章节与标注，首屏含"本地分析，非正式成绩"、`S < 12` 的样本量提示与资源指标的"近似计量"标注（北极星配置 S = 10 时三项均出现）；同一 `analysis.json` 两次渲染的 HTML 逐字节一致；HTML 不含外部资源引用 | R-M34-39 至 42 | §5.10 产物 | 仓库 |
| AC-M34-03 | `NearestTask` 非空非注释行 ≤ 20 且在 S1 `d1/n20` 上运行；`drone4d.coord` 再导出的协议与数据类与 M30 定义为同一对象（`is` 比较），`issubclass(drone4d.coord.Policy, drone4d.coord.PolicyProtocol)` 为真；继承基类与鸭子类型两种策略均被接受；以映射载荷且未给 schema 调用 `send` 时抛 `payload_schema_missing`；同一观测经进程内路径与 `drone4d.serve` 解码路径交给策略的对象规范化后逐字节相同（前置：CR-M34-07） | R-M34-01 至 04、06 | AC-D4-12；DP-11 | 仓库 |
| AC-M34-04 | `Policy`、`CentralPolicy`、`Solver`、`CoordinationPolicy` 各一个模板策略经 `bench run` 完成，结果写入同一实验目录格式 | R-M34-01、07、31 | ADR-D4-13 | 仓库 |
| AC-M34-05 | 哨兵 epoch：正常替换、本 Agent 无租约（内核回执 `no_assignment`）、租约在密封与生效之间移交（内核回执 `stale_epoch`）、显式 epoch 原样保留四个用例符合 R-M34-05；线上帧、trace 与运行清单中不出现负的 `assignment_epoch`；进程内与容器两条路径的定稿结果逐字节一致（前置：CR-M34-06） | R-M34-05 | AC-D4-10 | 仓库 |
| AC-M34-06 | `bench run` 与 `Experiment.run` 产出的实验目录在 §7.7.1 比较集上逐字节一致；续跑（含中断时在途 Episode 的重建）与一次跑完一致；中断后可续跑；`--policies` 子集不改变 `experiment_config_sha256`、`experiment_id` 与 `episode_id`，子集运行后以 `--resume` 补跑其余策略的结果与一次全量运行一致；故意写错的 `py:` 策略（导入错误、`step` 抛异常、返回非法决策各一例）在预检阶段以退出码 2 中止，预检通过而运行中失败的策略在结束摘要与报告首屏告警，给出 `--fail-on-policy-error` 时退出码 24；`--force` 在无标记文件的非空目录、配置文件所在目录与 `$HOME` 上被拒绝（前置：CR-M34-01） | R-M34-07、31 至 35、38、105、106 | — | 仓库 |
| AC-M34-07 | YL-01 至 YL-12 各至少一个反例被拒且给出行列号，反例至少包括 `yes`、`on`、`010`、`0o17`、`0b11`、`0x1F`、`1_000`、`1:30`、`.inf`、`.nan`、`.5`、`~`、重复键、锚点与别名、显式标签、多文档；`1e3`、`1.5e3`、`-1E+3`、`9.5e-1` 解析为浮点；schema 正反例在自含校验器与 jsonschema 上判定一致；字段级错误一次报全；北极星模板、S1 §11.4、S3 §9.3 与 X1 §12.7 的实验配置样例（后者含 `protocol` 块与包装器复合引用）通过解析；`ci.method` 与 `estimand` 不一致、`ci.level` 不在枚举内时被拒（前置：CR-M34-16） | R-M34-11 至 15 | AC-D4-43 | 仓库 |
| AC-M34-08 | 本地约束：`ranked: true`、`split: validation`、`image:` 引用（无容器运行时）、环境变量注入结果参数均被拒绝或无效；`module:` 引用记录 `provenance`，顶层单文件模块只含该文件摘要，输出目录位于配置目录下时第二次运行的 `provenance` 不变；`seeds.exogenous` 超出公开范围时报 `seed_out_of_public_range`；`--set`/`--attr` 进入 `cli` 层 | R-M34-16 至 18、30 | — | 仓库 |
| AC-M34-09 | worker 数 1、4、8 下 §7.7.1 比较集逐字节一致；修改 `outputs`、`execution`、`metadata` 不改变 `experiment_config_sha256`，修改任一计入摘要的字段改变之；仅调整 `policies[]` 书写顺序时 `experiment_config_sha256` 与全部 `episode_id` 不变、`analysis_config_sha256` 改变；仅把种子范围由 `[0, 10)` 改为 `[5, 15)` 时 `experiment_config_sha256` 改变，而共有种子 5–9 的 Episode 的 `config_sha256` 与 `episode_id` 不变；运行清单的 `config_sha256` 与 M28 口径的独立复算一致（前置：CR-M34-03、14） | R-M34-19 至 21、36 | AC-D4-05 | 仓库 |
| AC-M34-10 | `--dry-run` 不创建运行目录；北极星配置的 Episode 数为 400；估算逐项标注来源（登记预算、基线卡、预检实测或 `--assume-ms`）；估算墙钟与第 10 周实测之比落在 [0.8, 1.25]（目标值） | R-M34-22、105 | §5.10 | RM-8 |
| AC-M34-11 | 退出码表逐项触发；`--json` 输出通过 `drone4d.coord.cli_result.v1` 校验；进度只写标准错误且完成数随 Episode 完成递增；中英文消息目录完整（前置：CR-M34-01） | R-M34-23、25 至 28、37、101、103 | — | 仓库 |
| AC-M34-12 | `drone4d --help` 执行后 `sys.modules` 不含 numpy；RM-8 上墙钟 ≤ 0.3 s | R-M34-24、98 | — | RM-8 |
| AC-M34-13 | 未安装 `awr` 时 `drone4d suite verify` 返回 23；仓库环境中插件被发现；插件装载失败只影响该组；第三方插件注册保留组名被拒绝 | R-M34-29 | §4.4 | 仓库 + 干净环境 |
| AC-M34-14 | `bench replay` 对黄金运行判"重放一致"；注入一处决策改动后 `--diff` 报告的首个发散记录与注入点一致；`--mode full` 在策略实现变化时报 `policy_impl_changed`；修改策略后以 `--mode full --allow-impl-change` 重放，报告的首个发散记录位于修改生效的首个决策 tick 且退出码为 21（前置：CR-M34-13） | R-M34-43、44 | §5.9；AC-M33-20、21 | 仓库 |
| AC-M34-15 | `bench verify` 对完整实验目录返回 0；篡改一条 trace 记录、一个 `evaluation.json` 分量或 `rows.jsonl` 一行时分别返回 21 并指出项；跨 `hw_class` 的位级差异按 R-M34-47 标注 | R-M34-45 至 47 | §8.10 R-4；AC-D4-29 前置 | 仓库 |
| AC-M34-16 | `bench suite`、`bench instance`、`bench baseline` 命令的 `--json` 输出与 M31、M30 库函数结果一致 | R-M34-48、49 | R-M31-77 | 仓库 |
| AC-M34-17 | `sdk pack` 对示例策略生成上下文、锁文件、提交草稿与 SBOM；`wheels/` 只来自 `--wheelhouse` 或显式 `--download`，缺失时报 `wheel_missing`；镜像平台为 `linux/amd64`，以 `--network none`、只读根、非 root 运行；草稿字段名与 CT §5.5 一致，`declared` 与 `license` 为占位且未被填写，不含 `signature`；拓扑不一致被拒绝（前置：CR-M34-15） | R-M34-52 至 54、56 | §4.3 末条；§8.10 | 容器节点 |
| AC-M34-18 | 敏感文件模式命中时打包被拒，`--allow-file` 放行单个文件；日志与输出不含凭据 | R-M34-55、102 | — | 仓库 |
| AC-M34-19 | `sdk check` 对合格镜像通过扫描项、握手与冒烟；扫描项（平台、大小、SBOM、许可、`entry.env` 键）各有一个违例用例；容器参数与 `AgentContainers.argv` 逐项一致；对 6 个冒烟 Episode 中 2 个出现策略失败（崩溃率 2/6）的镜像报告"VALIDATING 将终止"；`serve --protocol v1` 的 D3 线协议黄金向量不变；`wire.v2` 帧黄金向量在宿主侧与 Agent 侧一致 | R-M34-57 至 59 | AC-D4-45 | 容器节点 |
| AC-M34-20 | PettingZoo `parallel_api_test` 与 AEC API 测试通过；`possible_agents` 在 Episode 内不变；一次 `step` 推进一个密封决策周期；终止谓词与到达时域分别产生 `terminations` 与 `truncations`；S1 的张量形状与截断次序符合 S1 PB-5（前置：CR-M34-09） | R-M34-60、61、63、66 | AC-D4-28 | 仓库 + `train` |
| AC-M34-21 | `message_vec` 量化（`rint`，含 ±0.5/127 的舍入边界）与反量化的黄金用例通过；Suite 未声明在线形式时 `team_delta_U` 报 `training_reward_unavailable`，`suite_default` 退为 `team_terminal_U`；`state()` 在非特权模式抛 726；`info` 经泄露审计不含真值字段（前置：CR-M34-08、10） | R-M34-62、64、65 | §4.3 第 2、3 条 | 仓库 + `train` |
| AC-M34-22 | TorchRL 适配与 PettingZoo 路径对同一动作序列逐元素一致；吞吐实测报告入档（未达标如实报告） | R-M34-67、104 | DL-D4-01 | 训练服务器 |
| AC-M34-23 | BenchMARL MAPPO 在 S1 算法隔离模式以固定种子完成训练；训练产物经 `TensorPolicyAdapter` 与 `bench run` 完成评测 | R-M34-68、69 | AC-D4-28 | 训练服务器 |
| AC-M34-24 | `TensorPolicyAdapter` 对训练期记录的同种子观测序列给出相同动作；`NumpyMLPPolicy` 与 torch 推理在容差 1e-6 内一致且在不含 torch 的镜像中运行 | R-M34-69 | — | 仓库 + `train` |
| AC-M34-25 | wheel 为 `py3-none-any`，不含顶层 `awr`；内容清单核对覆盖 `package_data.toml` 全部条目（含 `ref_det_v1.npz`、契约 schema、M29 参数集、模板），映射包内未声明的非 `.py` 文件使构建失败；在 wheel 路径解析 `network.params_ref: net/abstract-disk-default@1` 并运行 1 个 Episode；与 `awr` 可编辑安装共存时两者均可导入（前置：CR-M34-11） | R-M34-70、73 | ADR-D4-28 | 干净容器 |
| AC-M34-26 | NS-01 至 NS-08 的自测：每条规则至少一个违例使构建失败，其中函数体内的映射外 `awr.*` 导入（以 tape 的 `_env_track` 为样本）使构建失败；`awr.swarm.coverage` 与 `awr.swarm.allocation` 的 `__init__` 原样复制、`awr.sim.sensors` 的 `__init__` 生成为空包；`SOURCES.json` 中每个输出文件可回溯到源文件与提交 | R-M34-71、72 | ADR-D4-28 | 仓库 |
| AC-M34-27 | 路径一致性：北极星 20 个 Episode 子集（含 `cbba`、`nearest_greedy`）与各 Suite 黄金集（S2 含 `independent_lawnmower`、`partition_lawnmower`）在仓库路径与 wheel 路径上于 §7.7.1 比较集逐字节一致；`cbba` 在只装运行时依赖的 wheel 环境中运行通过；开发态 `_core` 由同一脚本生成（前置：CR-M34-02、12、14） | R-M34-75、76 | ADR-D4-28（2） | 干净容器 |
| AC-M34-28 | 同一提交两次独立构建的 wheel sha256 相同 | R-M34-74 | — | CI |
| AC-M34-29 | 只装运行时依赖的环境中导入全部核心模块后 `sys.modules` 不含 open3d、scipy、jsonschema、numba、torch、pettingzoo；导入 `drone4d.coord` 不导入 msgpack；缺附加包时报 `extra_missing` 与安装提示 | R-M34-77、80、81 | AC-D4-43 | 干净容器 |
| AC-M34-30 | 支持矩阵全部组合安装与冒烟通过；许可扫描与 SBOM 门禁通过；`train` 闭包无 GPL 系许可；约束文件可复现发布环境 | R-M34-78、79、83、84 | AC-D4-47 | CI |
| AC-M34-31 | 文档站两种语言构建无告警；`research/` 树与 §7.12 一致；API 参考覆盖稳定面与预览面且译文完整；版本路径 `0.4` 可访问 | R-M34-85 至 87、89、90 | AC-D4-46、AC-D4-48 | CI |
| AC-M34-32 | 快速入门与教程一（P0）、教程二至五与迁移指南（P1）的 `steps.sh` 在干净容器中逐步执行通过；页面输出块结构与执行结果一致 | R-M34-88、89 | AC-D4-46、AC-D4-48 | CI |
| AC-M34-33 | 版本常量写入运行清单；版本 lint 检出 SDK 版本与产品版本不同线；D3 SDK 测试（`tests/d3/sdk/`、`tests/d3/env/test_wire.py`）全部通过；未装 `client` 时 `import drone4d.bench` 成功且访问 `BenchClient` 报 `extra_missing`（前置：CR-M34-02；R-M34-82 另以 AWR-06 附录 B 登记为前提） | R-M34-09、10、82、91、92、97 | B-07 | 仓库 |
| AC-M34-34 | API 兼容检查：以上一稳定版的公开面快照比对当前版本，删除或改签名未走弃用流程时 CI 失败；弃用警告每调用位置只发出一次 | R-M34-93、94 | ADR-D4-35 | CI |
| AC-M34-35 | 赛季保护：以赛季开启时最低 SDK 版本打包的容器在当前评测宿主上握手与冒烟通过；兼容矩阵随发布更新 | R-M34-95、96 | §8.9 | 容器节点 |
| AC-M34-36 | `--set`、`--attr` 的组合在 `experiment.resolved.json` 中以 `cli` 层完整记录，`bench verify` 依该文件可重建同一 `experiment_config_sha256`；`--policies` 不出现在属性层，只出现在 `plan.json` 与实验级清单的 `plan.selected_policies`（前置：CR-M34-04） | R-M34-18、32 | r03 R11 | 仓库 |
| AC-M34-37 | 安装态中 `network_profile: fspl-nlos@1`、`clock_profile: d3_compat`、`suite: drone4d-event@1` 与（未装 `hifi` 时）`physics: kinematic` 在解析阶段以 `component_unavailable_in_wheel` 拒绝，开发态中不触发该规则组；wheel 内不含 `awr.sim.events.tape` 的 vendored 副本，导入全部核心模块后 `sys.modules` 不含 `drone4d._core.environment`、`drone4d._core.sim.fleet` 与 `zstandard`（前置：CR-M34-12） | R-M34-71、107 | ADR-D4-28；AC-D4-43 | 干净容器 |

---

## 11. 风险与待决

### 11.1 风险

| 编号 | 风险 | 触发指标 | 缓解 |
|---|---|---|---|
| RK-M34-01 | 命令行与 worker 启动开销侵蚀北极星预算 | CP-2 实测中非 Episode 开销 > 30 s；spawn worker 首次导入 > 2 s | 延迟导入；`max_tasks_per_child` 复用；`rows.jsonl` 增量汇编；分析与报告并行于末批 Episode |
| RK-M34-02 | vendoring 漂移：仓库路径与 wheel 路径结果不一致 | AC-M34-27 失败 | NS 规则在 D4 核心包 lint 中常开；路径一致性测试每次合入执行 |
| RK-M34-03 | 训练框架 API 变化 | 锁定版本升级后适配器测试失败 | `train` 精确锁版本；每个产品发布只升级一次；适配器只依赖各框架的公开 API |
| RK-M34-04 | 双语文档工作量超出估算 | WP-D4-45、WP-D4-47 的英文页滞后 > 1 周 | 代码块单一来源；术语表先行；估算计入双语（§13 第 14 条） |
| RK-M34-05 | 依赖混淆：公共 PyPI 上出现同名包 | 私有索引用户以 `--extra-index-url` 安装 | 在 WP-D4-01 预留公共包名；文档只给出单一 `--index-url`（私有索引代理公共依赖）的配置方式 |
| RK-M34-06 | 研究者本机缺少容器运行时，无法使用 `sdk pack`/`sdk check` | 外部复现团队反馈 | `--context-only` 产出上下文；平台侧构建通道作为后续选项（不在本期） |
| RK-M34-07 | torch 推理镜像体积与确定性问题 | 镜像超出扫描上限；同种子重复评测 `repeat_delta` 异常 | `NumpyMLPPolicy` 导出路径；单线程与确定性算法开关；在基线卡与提交声明中登记推理运行时 |
| RK-M34-08 | P0 工作量超出工作包分配（§9.4：估算 9.3 人周，分配 4.0 人周） | WP-D4-08 在第 8 周末或 WP-D4-44 在第 10 周末未完成 | WP-D4-01 中按 §9.4 复核分配；按 §9.3 降级；北极星门禁必需项（`bench run`、`bench report`、wheel 构建、闭包与路径一致性、许可）不降级 |

### 11.2 待决问题（未答复时按建议执行）

| 编号 | 问题 | 建议 |
|---|---|---|
| Q-M34-01 | wheel 的 Python 下限 | CPython 3.12（与仓库、numpy 2.5.3 的 `Requires-Python` 一致）；3.13 在 WP-D4-44 中核实依赖可用性后加入矩阵 |
| Q-M34-02 | 运行时依赖集合 | numpy、PyYAML、msgpack；内核导入闭包仍只有 numpy |
| Q-M34-03 | `hifi` 的分发形态 | P1 期间 `hosts.hifi` 只在仓库环境可用（不进入 wheel）；P2 评估把 F-12 所列模块加入映射表的成本与网格世界数据的分发方式后再定 |
| Q-M34-04 | 包名预留与索引配置 | WP-D4-01 中在公共 PyPI 预留 `drone4d`；私有索引代理公共依赖，文档只给 `--index-url` |
| Q-M34-05 | 平台侧命令的承载 | 入口点组 `drone4d.cli.v1` 插件；规范入口 `python -m awr.benchmarks.platform`；根 `pyproject.toml` 改动经 M00 |
| Q-M34-06 | 版本号方案 | 与产品版本同线 `0.4.Z`；P1 门前为预发布；P1 门发布 `0.4.0` |
| Q-M34-07 | 支持的容器运行时 | docker 与 podman；其他构建工具以 `--context-only` 加用户自建 |
| Q-M34-08 | 命令行消息缺省语言 | 英文缺省，`DRONE4D_LANG=zh_CN` 或 `LANG` 为中文环境时用中文 |
| Q-M34-09 | `py:sim/events/tape.py` 是否进入 wheel | 不进入（F-20）；`abstract-radio@1` 在 wheel 中只读实例 `comm.json` 的通信区；S2 网格族与磁带通信区只在仓库环境与 `hosts/hifi` 可用（§13 第 22 条） |
| Q-M34-10 | `ref` 级 vendoring 清单（`awr.tasking`、`awr.agent.runtime` 子集） | WP-D4-01 闭包审计后由 M30 与 M34 定案；定案前 `ref` 基线中依赖这两个包者只在仓库环境可用，`hungarian_periodic` 等只依赖 `awr.swarm` 与 scipy 者经 `drone4d[ref]` 可用 |

---

## 12. 追溯

| AWR-06 条目 | 内容 | 本文落点 |
|---|---|---|
| R-D4-10、ADR-D4-13 | 单一原生语义与多入口；PettingZoo 为同步适配器 | §5.1、§5.9、§7.8 |
| R-D4-11 | `drone4d bench run` 一条命令运行实验 | §5.4、§7.6 |
| R-D4-16、ADR-D4-29、AC-D4-11、AC-D4-12 | 北极星门禁；最小策略 ≤ 20 行；不克隆仓库运行 `dev` 评测 | §4、§7.3、§7.4.1；AC-M34-01、03 |
| R-D4-17、B-06、B-07 | D3 不回归；已发布契约原位不改 | §5.8 R-M34-58、§5.13 R-M34-97；AC-M34-19、33 |
| DP-11 | 接口极简、最小策略 ≤ 20 行 | R-M34-06；AC-M34-03 |
| §3.3 SDK 行、ADR-D4-28 | `sdk:coord/`、`sdk:adapters/`、`sdk:cli.py`；vendoring 与 import 重写；分发渠道与支持矩阵 | §5.10、§7.1、§7.10；AC-M34-25 至 30 |
| B-02、ADR-D4-33、AC-D4-43 | 依赖闭包；可选依赖延迟导入 | R-M34-77、80、81；AC-M34-29 |
| §4.3 | PettingZoo/TorchRL/BenchMARL 适配；`sdk pack` | §5.8、§5.9、§7.8、§7.9 |
| §4.4、§4.6、§4.7 | 实验配置样例与字段；契约 | §5.2、§7.4、§8 |
| §5.6、§5.9 | 确定性等级与硬件等级；重放与复现 | R-M34-43 至 47、51、84 |
| §8.10 | 提交、VALIDATING 冒烟、本地重放 | R-M34-44、46、56、57 |
| ADR-D4-31 | 契约命名空间与演进 | §7.13.3；§13 第 16 条 |
| ADR-D4-32、AC-D4-47 | 许可与 SBOM | R-M34-79、83；AC-M34-30 |
| ADR-D4-35、AC-D4-46、AC-D4-48 | 文档、教程、版本与弃用 | §5.12、§5.13、§7.12、§7.13 |
| WP-D4-08 | 研究者 SDK、CLI 首批、北极星基准 | §2.1、§9.2 |
| WP-D4-24、AC-D4-28、DL-D4-01 | 训练框架适配、`sdk pack`、CLI 第二批、吞吐实测 | §5.6、§5.8、§5.9 |
| WP-D4-44、WP-D4-45、WP-D4-47 | 发布流水线；文档站 P0、P1 | §5.10–§5.12 |
| WP-D4-30、WP-D4-49、AC-D4-29 | 外部复现与接入渠道 | §4、§5.12；AC-M34-01、32 |
| WP-D4-40 | 公共 PyPI 发布（须批准） | §2.1 P2 |
| r03 R1、R2、R11 | 声明式配置为主；属性系统；Helper 展开写入运行记录 | §5.2、§7.5 |
| r03 R10、R29、R30 | 接口版本化；贡献分级；注册表与过期 | §5.13、R-M34-29、49、90 |
| r03 R20、R24 | trace 开销；Run Manifest 为唯一凭据 | R-M34-17、72；§7.7 |
| n01 §2.4 G-CP-2 | D3 只有 PettingZoo 风格 | F-18；§5.9 |
| n03 §2.10、§3.6 | 可学习通信 int8 量化；固定 Agent 集合加 mask；AEC 非真实异步 | R-M34-62、63、66 |
| M30 §12 第 12、15 条；X1 §12.6 第 15 条；CT §4.3.2、§9 第 18 项 | 并列文档向本文提出的输入 | §7.10.2、R-M34-04、05、§7.4.2、§8.2 |
| 评审意见（v0.1） | 29 条 | §14 |

---

## 13. 对 AWR-06 的反馈

| 编号 | AWR-06 位置 | 问题 | 建议 | 理由 |
|---|---|---|---|---|
| 1 | ADR-D4-28（4）；§4.2 各代码块标题"Python 3.11" | 支持矩阵含 CPython 3.11，并要求运行时 numpy"与仓库锁定版本（`numpy==2.5.3`）同主版本"；已核实 numpy 2.5.3 的 `Requires-Python` 为 `>=3.12`（F-07），3.11 上只能安装更旧的 numpy 次版本 | wheel 下限定为 CPython 3.12，删除 3.11；接口草案标题改为"Python 3.12"；3.13 在 WP-D4-44 核实后加入 | 同一 numpy 构建是等级 1b 硬件等级的组成部分（§5.6）；3.11 用户的数值库与平台评测镜像必然不同，且仓库 ruff 以 3.12 为目标（F-08），保留 3.11 需额外的语法守护 |
| 2 | ADR-D4-28（4）"运行时依赖只有 numpy"；§0.1 第 3 条"只安装运行时依赖 numpy" | `drone4d bench run experiment.yaml` 需要 YAML 解析（标准库无），`wire.v2` 与 `serve` 需要 msgpack；现有 SDK 另有硬依赖 httpx（F-01） | 运行时依赖改为 numpy、PyYAML、msgpack；httpx 移入附加包 `client`；B-02 与 AC-D4-43 保持"内核导入闭包只有 numpy"的表述不变 | 自写 YAML 解析器的维护与安全成本高于引入一个成熟依赖；PyYAML 与 msgpack 均为仓库已锁定的依赖（F-06），不新增许可风险 |
| 3 | B-02、ADR-D4-33"只能出现在可选附加包的模块中" | pip 的附加包只增加依赖，不能携带或排除模块；"附加包的模块"在 wheel 中无对应实体 | 改写为"依赖这些包的模块随基础 wheel 分发，在使用时延迟导入，缺依赖时报明确错误" | 避免实现者按字面理解去拆分发行版，或误以为附加包能控制 wheel 内容 |
| 4 | ADR-D4-28（2）vendoring 清单；§10.3 WP-D4-25（`drone4d[hifi]`） | `hosts/hifi` 需要 `L1Model`、`camera`、`geom`、`rng`、`KinematicTier`、`tracker`、`MeshWorld`、`world.geometry.*`（F-12），均不在 vendoring 清单内；另需网格世界数据 | 二选一并写入 ADR：P1 中 `hifi` 只在仓库环境可用（建议）；或扩展映射表并给出世界数据的分发方式与体积预算 | 否则"`pip install drone4d[hifi]` 即可使用高保真后端"在不克隆仓库的环境中不成立 |
| 5 | ADR-D4-28（2） | 未规定 vendoring 对包级副作用与包名依赖的处理：`py:sim/sensors/__init__.py` 依 `AWR_PLUGINS` 条件导入插件，`cbrng._KC_NAME` 依 `__package__` 查找 numba 核（F-10） | 在 ADR 中写明：中间包 `__init__` 生成为空包；依赖包名的行为登记白名单（本文 NS-04、NS-05） | 复制原样 `__init__` 会把栈 A 的插件机制带入 wheel；包名变化使 numba 路径在 wheel 中永不启用，需作为已知事实登记 |
| 6 | §4.2 `EventTypeSpec.handler: str  # 点路径，如 "awr.interaction.radio:on_service"`；ADR-D4-28"trace 事件哈希逐字节一致" | 以模块点路径登记处理器，一旦该字符串进入事件类型登记的摘要、trace 或检查点，仓库路径与 wheel 路径的哈希必然不同 | 处理器与组件一律以逻辑 id 登记（如 `net.radio.on_service`），模块路径只在登记处出现；增加"摘要与持久化格式不得含模块路径"的规则（本文 NS-02、NS-07、NS-08） | 使 ADR-D4-28 的一致性判据可以成立 |
| 7 | §4.4 末段"P0 为 `bench run` 与 `bench report`"；WP-D4-08 估算 2.0 人周 | M31 R-M31-77 请求 P0 交付 `bench suite list/show/check` 与 `bench instance gen/check`；北极星路径与外部冷启动另需 `bench init`、`--dry-run`、`env` | 在 §4.4 登记 P0 应交付命令集合；WP-D4-08 复核估算（M34 自下而上估算为 4.7 人周，§9.4），可拆分为"解析与校验""运行与报告"两部分，或明确 `suite`/`instance` 封装计入 Suite 框架工作包 WP-D4-10；降级顺序采纳本文 §9.3 | 命令集合与工作包估算需一致，避免第 8 周的隐性溢出 |
| 8 | §4.4"`drone4d suite verify` 与 `drone4d suite audit` 为平台侧命令，随仓库提供，不进入 wheel" | 平台侧命令与 wheel 共用程序名 `drone4d`，但 wheel 不含其代码，未说明在仓库环境中如何出现在同一程序下；ADR-050 规定入口为 `python -m awr.<pkg>` | 规定入口点组 `drone4d.cli.v1`：`awr` 发行版注册 `suite` 组，规范入口为 `python -m awr.benchmarks.platform`；未装 `awr` 时返回专用退出码 | 避免两个发行版同时提供 `drone4d` 控制台脚本而相互覆盖 |
| 9 | §8.10 末条"`drone4d bench run --replay` 重放" | 与 §4.4 的 `drone4d bench replay <run_dir>` 不一致 | 统一为 `drone4d bench replay`（动作重放缺省，`--mode full` 为全量重仿真） | 命令行一致性（M33 §13 第 12 条同此意见） |
| 10 | §4.4 样例注释"`split: dev  # dev \| validation（holdout 只在平台侧）`" | `validation` 的外生种子为冻结时生成的秘密种子、`truth` 不分发（§8.7；M31 §4.4、§4.5），本地无法运行与评测 | 注释改为"本地只允许 `dev`；`validation`、`holdout`、`stress` 只在平台侧" | 避免研究者以为可以在本地对 `validation` 自评 |
| 11 | §4.2 最小策略 `goto(ent, t.pos)`、`obs.claim(...)`、`st.dist_to(...)` | `EntityCommand.assignment_epoch` 为必填，示例未给出，构造函数无从得知 epoch；示例在冻结数据类上调用方法，与 M30 §12 第 15 条及 R-M30-04 的同构要求冲突 | 在 §4.2 规定哨兵 epoch：构造函数写入 SDK 内部哨兵，由 SDK 侧决策定稿函数以同一次观测中的租约 epoch 替换，本 Agent 无租约时由内核按 R-M28-62 回执，哨兵不上线（与 CT §4.3.2 一致，本文 R-M34-05）；示例改用自由函数 `claim(t.task_id)`、`dist(st.pos, t.pos)`（本文 §7.3，仍为 12 行） | 保持示例 ≤ 20 行，不削弱 epoch 对过期指令的防护，并使示例可按声明类型运行 |
| 12 | §4.3 第 1 条"返回各 Agent 私有 observation、reward、..." | 训练奖励未定义；D3 `BenchEnv` 奖励恒为 0（F-18）；S1 §9.3 写"奖励为训练接口自定义"，与 Suite 版本化的要求未衔接 | 张量画像、`training_reward`、`state_spec` 作为 `suite.v1` 可选字段（CT §5.1.1 已登记字段名）由各 Suite 声明取值；缺省 `team_terminal_U`，`team_delta_U` 须 Suite 给出在线形式；适配器另允许研究者提供只读 `info` 的奖励函数（本文 R-M34-64） | 奖励定义影响训练结果的可比性，缺省值应与 Suite 一起版本化，同时保留研究者自定义的空间 |
| 13 | ADR-D4-35"`drone4d` wheel 采用语义化版本……保留至少 2 个次版本" | 产品版本 D4 = `0.4.0`，在 `0.y.z` 下"次版本"即产品系列，"保留 2 个次版本"意味着保留到 D6；wheel 版本与产品版本的关系未定义；`check-version.mjs` 不覆盖 SDK 版本（F-16） | wheel 版本与产品版本同线 `0.4.Z`；弃用以"至少 2 个后续发布且不少于 6 个月"计；P1 门前为预发布、P1 门发布 `0.4.0`；新增版本一致性检查 | 消除歧义；第一赛季开启时提交者需要稳定的 SDK 面 |
| 14 | WP-D4-45（1.0 人周）、WP-D4-47（2.0 人周）；ADR-D4-35 | 现有文档站强制中英文同构、缺页即构建失败，版本列表只有 `latest`，`release` 读自 `awr` 的根 `pyproject.toml`（F-14）；API 参考自动生成需在文档构建环境中安装 wheel 并提供英文说明 | 估算计入双语页面、版本路径机制与 API 译文登记（M34 估算 WP-D4-45 为 2.0 人周、WP-D4-47 为 2.5 人周，§9.4）；版本路径以 `0.4` 并存；文档构建环境安装同一提交的 wheel | 现有估算按单语计，P0、P1 文档门禁有溢出风险 |
| 15 | ADR-D4-28（3）"P0、P1 发布到受邀私有包索引"；Q-D4-15 | 私有索引与公共 PyPI 并用且公共包名未预留时，以 `--extra-index-url` 安装存在依赖混淆风险 | WP-D4-01 中预留公共包名；文档只给出单一 `--index-url` 的配置（私有索引代理公共依赖） | 供应链安全 |
| 16 | ADR-D4-31"`additionalProperties: false` 的对象新增字段一律视为主版本变化" | 用于 `experiment.v1` 时，每增加一个可选字段都要升主版本；而实验配置的读取者是本机安装的工具，旧版本拒绝新字段正是期望行为 | 为人工编写、由本机工具消费的规格（`experiment.v1`）设例外：保持严格校验，新增可选字段为次版本并以 `x-since` 标注 | 兼顾拼写错误即时报错与契约演进成本 |
| 17 | ADR-D4-28（4）"`dev` 场景集超出时改为首次运行下载并校验摘要" | `dev` 的实例由公开生成器与公开种子确定性生成（M31 §4.5），无需打包实例本身 | 改为"`dev` 实例在本地按公开派生规则生成，wheel 只携带 Suite 清单与参考缓存（如 `π_central`）"；下载机制只在参考缓存超出体积上限时启用 | 降低 wheel 体积与分发面，同时与 M31 的派生规则一致 |
| 18 | §4.3 末条、§8.10 | `sdk pack` 之后的提交上传渠道未定义；本地 `sdk check` 需要宿主侧 `wire.v2` 与容器运行器，未分配到工作包 | 明确提交渠道（平台既有受理入口或 SDK 客户端）与归属；`wire.v2` 编解码单一来源由 WP-D4-04 交付并 vendoring，本地容器宿主计入 WP-D4-24 | WP-D4-24（2.5 人周）同时承担三个框架适配、`sdk pack`、`bench replay/verify` 与吞吐实测，范围偏大；M34 估算其中 M34 部分为 3.8 人周（§9.4），建议复核或拆分 |
| 19 | §0.1 第 3 条"自 `pip install drone4d` 完成起 10 分钟"；§5.10 北极星表"其余 4 min 留给安装、示例阅读与报告生成"与 RM-8 安装方式"`pip install drone4d`"；AC-D4-29 | 三处计时起点不一致：§0.1 与 ADR-D4-29 不含安装，§5.10 表把安装计入 4 min，AC-D4-29 未说明；P0 期间发布的是预发布版本 `0.4.0aN`，pip 缺省不选择预发布，索引中出现任何稳定版本时 `pip install drone4d` 会装到其他版本 | §5.10 表改为"其余 4 min 留给自检、生成、阅读、预检与报告（安装耗时单独记录）"；AC-D4-29 明确"自安装完成起计时，安装耗时单独记录"；预发布期间的安装命令写为 `pip install --pre "drone4d>=0.4.0a1,<0.5"`，门禁脚本以 `drone4d==<被测版本>` 精确固定（本文 §4.1、R-M34-91） | 外部复现与北极星门禁使用同一判据，且门禁实测的确为被测版本 |
| 20 | §4.3（训练产物评测） | 未规定评测容器中的推理运行时；torch 推理镜像体积大，确定性与 CPU 预算需额外约束 | 增加"评测期推理运行时须在提交中声明；平台提供无 torch 的多层感知机导出路径"（本文 R-M34-69） | 学习型条目与经典条目在 B1 预算下的可比性 |
| 21 | ADR-D4-28（2）vendoring 清单；§3.3 依赖闭包表 | 清单缺少核心基线所需的 `awr.swarm.allocation.score` 与 `awr.swarm.coverage.{lanes, partition, plan, sensor}`（`cbba`、`ssi`、S2 `π_ind` 依赖之，M30 §12 第 12 条、R-M30-47）；两个包的 `__init__` 导入子模块，与"中间包生成空包"的处理冲突（F-21） | 清单分核心级与 `ref` 级登记（核心级见本文 §7.10.2 中 `ref` 级以外的各行，`ref` 级在 WP-D4-01 闭包审计后定案）；写明"以整包登记的包原样复制 `__init__`，其导入的子模块须全部在映射内"（本文 NS-05） | 否则北极星的 `cbba` 无法在 pip 安装环境中运行（AC-D4-11、12） |
| 22 | §3.3 闭包表 `py:sim/events/tape.py` 行（"numpy；进入核心：是"）；ADR-D4-28（2）清单中的 `tape` | tape 的读取依赖 zstandard，且 `_env_track`、`env_at`、`actor_states` 在函数体内导入 `awr.environment.*` 与 `awr.sim.fleet.stages.kinematic`（后者经 `kernels_tap` 引入 `awr.world.georef` 与可选 numba），闭包远超 numpy（F-20）；tape 只服务 S2 网格族与磁带通信区，二者依赖网格世界 | 闭包表改为"numpy、zstandard 与 `awr.environment`、`awr.sim.fleet` 子集；进入核心：否"；从 ADR-D4-28 的 vendoring 清单删去 `tape`；B-10 白名单保留（仓库环境与 `hosts/hifi` 使用）；核心中 `abstract-radio@1` 的通信区只来自实例 `comm.json` | D3 冻结（B-08）不允许为 wheel 拆分 `tape.py`；把 tape 留在核心会使 vendoring 闭包引入环境引擎与机队仿真 |
| 23 | §3.3 闭包表 `py:agentsdk/env/link.py` 行（"进入 numpy-only 核心：是（仅 `fspl-nlos@1` 遗留档）"） | M29 规定 `fspl-nlos@1` 只服务 `drone4d-event@1` 与 D3 黄金运行对拍（M29 §0 第 3 条、§4.8），二者依赖网格世界，不在 wheel 范围内 | 改为"进入核心：否（仓库环境，经 B-10 白名单 import）"；wheel 中选用该档在解析阶段拒绝（本文 R-M34-107） | 闭包表、M29 与 wheel 内容三者一致 |
| 24 | §4.4 实验配置样例 `ci: {method: cluster_bootstrap_t, estimand: family, n_boot: auto, level: 0.95}` | M32 中区间方法由估计目标决定（R-M32-07、16），`Scoring` 只有 `alpha`，`n_boot` 下限为 `max(10000, ⌈20·m/α⌉)`（R-M32-15）；样例使人以为方法与置信水平可自由选择 | 注明 `ci.method` 为与估计目标一致的断言字段（缺省 `auto`），`level` 映射为 `alpha`（本文 §7.4.2）；S1 §11.4、S3 §9.3 的样例沿用同一写法 | 字段语义与统计协议一致，避免本地分析与正式成绩采用不同方法 |
| 25 | R-D4-17、B-07；ADR-D4-28（4）；附录 B | 把 httpx 从 D3 SDK 的硬依赖移入附加包 `client` 后，只执行 `pip install drone4d` 的 D3 用户访问 `drone4d.bench.BenchClient` 将报 `extra_missing`，属 D3 面的行为变化；pip 不支持缺省附加包，无法以"缺省安装 `client`"规避 | 在附录 B（D3 封板裁决清单）登记该依赖集合变更，作为 D3 面冻结规则的唯一例外，并经 M26 同意（本文 R-M34-82） | 使 R-D4-17"D3 不造成回归"与"运行时依赖最小化"的取舍成文 |
| 26 | §3.3 闭包表 `py:sim/sensors/cbrng.py` 行 | 只记录了 numba 核依 `_KC_NAME` 查找；同文件 `warmup()` 以非字面量参数 `importlib.import_module(_KC_NAME)` 装载该核，vendoring 后目标模块不存在（F-10） | 闭包表注明"`warmup()` 只供栈 A 插件安装调用，wheel 中不得调用"；ADR-D4-28 的一致性规则引用本文 NS-03、NS-04 | 动态导入是命名空间重写最容易遗漏的路径 |
| 27 | §5.10 北极星判据；AC-D4-11 | 判据只要求得到指标表与配对差，未要求自研示例策略无失败；策略失败按 `invalid_policy` 计分且不影响退出码，示例策略全部失败时门禁仍可通过 | AC-D4-11 增加"4 个算法均无策略失败"；门禁脚本以 `--fail-on-policy-error` 运行（本文 R-M34-105、106） | 门禁应证明示例路径真实可用 |
| 28 | §10.2 WP-D4-08、44、45 | M34 自下而上估算 P0 部分合计 9.3 人周（WP-D4-08 为 4.7、WP-D4-44 为 2.6、WP-D4-45 为 2.0），分配 4.0 人周；WP-D4-44 须交付 tokenize 级重写与 NS 规则的 AST 检查、可重复构建、Linux 与 macOS 支持矩阵、SBOM 与许可扫描、私有索引发布与约束文件 | 在 WP-D4-01 中按本文 §9.4 复核三个工作包的分配；WP-D4-44 的起始周可前移，与 WP-D4-08 后半段并行（建议第 7–10 周）；降级顺序采纳本文 §9.3 | 北极星门禁位于关键路径末端，第 10 周的门禁实测依赖 WP-D4-44 按期完成 |

---

## 14. 评审处理记录

v0.1 评审意见共 29 条，逐条打开所引文件与代码核实后处理。"属实"者按评审建议或等效方案修改；"部分属实"者只修改属实部分并说明其余部分的核实结果；"不属实"者不改。

| 序 | 评审位置 | 严重度 | 核实结论 | 处理 | 修改位置 |
|---|---|---|---|---|---|
| 1 | §7.10.2 映射表；NS-01；R-M34-71；AC-M34-27 | 高 | 属实：`cbba`、`ssi` 依赖 `py:swarm/allocation/score.py`，S2 `π_ind` 依赖 `py:swarm/coverage/lanes.py`、`partition.py`（M30 §8.2、§12 第 12 条，R-M30-47）；`swarm/coverage/__init__.py` 导入 `lanes`、`partition`、`plan`、`sensor`，`swarm/allocation/__init__.py` 导入 `hungarian`（F-21） | 采纳：核心级条目写入映射表初版；`ref` 级（`awr.tasking`、`awr.agent.runtime` 子集）登记为待定，由 WP-D4-01 闭包审计定案；NS-05 增"整包登记者原样复制 `__init__`"的例外；路径一致性子集覆盖 `cbba` 与 S2 两个割草机基线；向 AWR-06 提反馈 | F-21；§7.1；§7.10.2；NS-05；R-M34-76；AC-M34-26、27；Q-M34-10；§13 第 21 条 |
| 2 | §1.1 F-09；NS-01；§7.10.2 `tape` | 高 | 属实：`tape.py` 函数体内有 5 处 `awr.*` 导入（`_env_track` 2 处、`env_at` 2 处、`actor_states` 1 处），其闭包另引入 msgpack、`awr.contracts`、`awr.world.georef` 与可选 numba；另核实 `_sub`、`write_tape` 在函数内导入 zstandard | 采纳评审选项 (c)：tape 移出 wheel（D3 冻结不允许拆分源文件，选项 (a) 不可行；选项 (b) 会把环境引擎与机队仿真带入核心）；NS-01 的扫描范围明确包含函数体内导入；核心网络的通信区来源改由 M29 抽象（CR-M34-12）；向 AWR-06 提反馈 | 摘要第 7 条；F-09；F-20；§7.1；§7.10.1；§7.10.2；NS-01；§7.11；AC-M34-26、37；CR-M34-12；Q-M34-09；§13 第 22 条 |
| 3 | §7.10.2 末段；§7.4.2 `track.network_profile` | 中 | 部分属实：M29 实际规定 `fspl-nlos@1` 只服务 `drone4d-event@1` 与对拍（M29 §0 第 3 条、§4.8），M34 的排除与 M29 一致，评审称其为 wheel 内可用的 D4 档不成立；但 §7.4.2 未规定 wheel 中选用该档的处置、未向 AWR-06 指出 §3.3 闭包表的不一致，属实 | 采纳评审选项 (b)：保持排除，新增 R-M34-107 与规则组 `wheel_unavailable`，在解析阶段拒绝；向 AWR-06 §3.3 提反馈 | R-M34-107；§7.4.2 `track.network_profile` 行与 `x-rules` 段；§7.10.2 末段；AC-M34-37；§13 第 23 条 |
| 4 | R-M34-19、21、34；AC-M34-36（`config_sha256` 口径） | 高 | 属实：M27-FR-007 与 M28 §7.2.1 的 `config_sha256` 不含种子，M34 v0.1 的同名字段含种子并用于派生 `experiment_id` | 采纳：实验级摘要更名 `experiment_config_sha256`，逐 Episode 沿用 M27/M28 口径；`policies[]` 先按 `name` 排序再计入，书写顺序计入 `analysis_config_sha256`；`episode_id` 改由逐 Episode 内容派生；续跑分两级比对；AC-M34-09 增"仅调整策略顺序""仅调整种子范围"两个用例；另发现 `EpisodeSpec` 的 `trace_level`、`out_dir` 会进入 M28 的 `config_sha256`，提 CR-M34-03 | 摘要第 4 条；R-M34-18、19、21、34；§7.2；§7.4.2；§7.5；§7.7；§8.2；AC-M34-09；CR-M34-03 |
| 5 | §2.2；R-M34-17、18、34、37、38；§7.5（对 M28 的接口假设） | 高 | 部分属实：(1) `BatchHost.run` 为同步列表接口，属实；(2) 不属实：M28 R-M28-32 与 `AttrRegistry.resolve` 注释已含 `cli` 层，并引用 M34 R-M34-18；(3) `EPOCH_CURRENT` 在 M28、M30、M33 中无登记，属实，但 CT §4.3.2 已规定哨兵"在发送前解析为具体值，不出现在线上"，内核不会收到 −1；(4) `versions` 无 `api_level`、`distribution`，属实 | 新增 §8.5 跨模块变更请求：(1) CR-M34-01 迭代式结果流、取消与 worker 初始化参数；(2) 不提请求，另因 `--policies` 改为计划过滤提 CR-M34-04；(3) 按 CT 采用 SDK 侧决策定稿函数（CR-M34-06，归 M30），而非 `DecisionValidator` 前置步骤，无租约时由内核按 R-M28-62 回执，取消 `epoch_unresolved`；(4) CR-M34-02；相关验收标注前置条件 | §2.2；R-M34-05、09、17、37、38；§7.6.4；§8.5；§10.2 说明与 AC-M34-05、06、11、33、36 |
| 6 | §8.4；§7.8.2；R-M34-61、64；AC-M34-20、21（张量画像） | 中 | 部分属实：M31 与 S1–S3 无取值、S1 PB-5 与 M34 截断规则冲突，属实；但 CT §5.1.1 已把 `tensor_profiles`、`training_reward`、`state_spec` 登记为 `suite.v1` 可选字段（字段由 M34 定义），"无对应字段"一说已不成立 | 采纳：§7.8.2 改为"缺省规则，Suite 可覆盖"，以 S1 PB-5 与 §9.3 特征 `G` 为 S1 原文；奖励缺省 `team_terminal_U` 并支持研究者自定义奖励函数，与 S1"训练接口自定义"衔接；向 M31、S1 提 CR-M34-09、10；AC-M34-20、21 标注前置条件 | R-M34-61、64、65；§7.8.2；§7.8.4；§8.4；CR-M34-09、10；AC-M34-20、21；§13 第 12 条 |
| 7 | §7.4.2（缺 `protocol`）；X1 §12.7 | 高 | 部分属实：`experiment.v1` 无 `protocol` 块属实；X1 §12.7 当前样例为 `method: percentile`，`seed_bootstrap_percentile` 一说不属实。另核实 S1 §11.4 样例使用 `instances.block`，§7.4.2 亦缺该字段 | 采纳：增 `protocol`、`protocol.id`、`families[]`、`q_levels[]`（P1，`x-since`）与相应 `x-rules`；`policies[].ref` 接受 M30 §7.5 的复合引用；增 `instances.block`；`ci.method` 改为枚举；AC-M34-07 增北极星、S1 §11.4、S3 §9.3、X1 §12.7 样例的解析正例；请 X1 将其 §12.6 第 15 条标为已响应 | R-M34-16；§7.4.2；AC-M34-07；CR-M34-17 |
| 8 | §7.4.2 `evaluation.*` | 中 | 属实：R-M32-07、15、16 与 `Scoring` 字段核对一致 | 采纳并调整：`ci.method` 保留为断言字段（`auto`、`percentile`、`cluster_bootstrap_t`），只接受与 R-M32-16 推导结果一致的取值，使 06、S1、S3、X1 的现有样例仍可解析；`ci.level` 限定为 0.90、0.95、0.99 并映射为 `Scoring.alpha`；`n_boot` 下限 10,000 并按 R-M32-15 复核；提 CR-M34-16；向 AWR-06 §4.4 样例提反馈 | §7.4.2；CR-M34-16；AC-M34-07；§13 第 24 条 |
| 9 | R-M34-01、02；§7.2；AC-M34-03（与 M30 的一致性） | 中 | 属实：M30 `Policy` 为 `runtime_checkable` 的 `Protocol`，协议位于 `awr.coordination.policy`，`CallbackAdapter` 位于 `awr.coordination.hosting`（宿主内部接口），`CoordContext`、`WakeDecision` 为 P0 冻结符号 | 采纳：区分协议 `PolicyProtocol`（`is` 检查）与便利基类 `Policy`（`issubclass` 检查）；导入路径按 M30 §7.2；补齐 `CoordContext`、`WakeDecision`、`Replannable`、`WakeOnRx`；`CallbackAdapter` 不再导出，提 CR-M34-07 | 摘要第 3 条；R-M34-01、02；§7.1；§7.2；AC-M34-03；CR-M34-07 |
| 10 | R-M34-04（`Observation.claim`、`EntityStateView`） | 中 | 属实：M30 §7.2 表末行与 §12 第 15 条建议以自由函数提供，R-M30-04 要求两种宿主的输入对象规范化后逐字节相同 | 采纳：提供自由函数 `claim`、`release`、`dist`，删除 `EntityStateView` 与 `views.py`；视图类型只含字段；两种宿主共用 M30 的观测构造函数；示例改写后仍为 12 行 | 摘要第 3 条；R-M34-04；§7.1；§7.2；§7.3；AC-M34-03；§13 第 11 条 |
| 11 | §7.2 `send()`；R-M34-03 | 中 | 属实：CT §4.1.1 与 AWR-06 §4.2 为 `max_age_ns` 与 `source_sample_time_ns`；另核实 CT 规定 `info_class` 须与 schema 登记一致、`opaque` 对应 `opaque` | 采纳：参数改为 `max_age_ns`（不提供毫秒便捷参数，避免单位换算歧义），增 `source_sample_time_ns`；`info_class` 由 schema 推导，不再作为参数；映射载荷未给登记 schema 时抛 `payload_schema_missing` | R-M34-03；§7.2；§7.6.4；AC-M34-03 |
| 12 | R-M34-12；YL-02、03、04、10 | 中 | 属实：在仓库 `.venv`（PyYAML 6.0.3）复测，结果与评审一致；另测得 `1.0e+3`、`1.0E-3` 解析为浮点、`0x1F` 为 31、`y` 为字符串 | 采纳：R-M34-12 改为替换隐式解析器的自定义 Loader；YL 规则扩为 YL-01 至 YL-12：接受全部 JSON 指数写法，拒绝 `0x`/`0o`/`0b`、前导零、`_` 分隔、`.5`、`~` 等；AC-M34-07 列出具体反例 | 摘要第 4 条；F-22；R-M34-12；§7.4.3；§7.5；AC-M34-07 |
| 13 | R-M34-35；§4.1 步骤 5 判据 | 中 | 属实 | 采纳：新增策略预检 R-M34-105（只跑首个场景前 20 次决策，不跑完整 Episode，以免侵占北极星预算）、策略失败告警 R-M34-106、`--fail-on-policy-error` 与退出码 24；步骤 5 判据改为平台失败与策略失败均为 0；未另设失败率阈值，任一策略失败即告警；向 AWR-06 提反馈 | §4.1；§4.3；R-M34-26、31、35、105、106；§7.6；AC-M34-06；§13 第 27 条 |
| 14 | R-M34-31 至 34（`--policies` 子集） | 中 | 属实 | 采纳评审第一方案：`--policies` 改为执行计划过滤，不进入属性层，不改变 `experiment_id` 与 `episode_id`；子集运行以 `--resume` 写入同一目录，未运行者记 `pending`；所含策略集合计入 `analysis_config_sha256`；提 CR-M34-04 | R-M34-32；§7.6.2；§7.7；AC-M34-06、36；CR-M34-04 |
| 15 | R-M34-17；§7.7；R-M34-33 | 低 | 属实 | 采纳：顶层单文件模块只记录文件摘要，包模块排除输出目录、隐藏目录与字节码；`--force` 只清空含标记文件的目录，拒绝清空配置目录及其祖先、`$HOME` 与根目录（`out_dir_unsafe`）；另规定同一实验未给 `--resume` 时报 `out_dir_has_results` | R-M34-17、33；§7.6.3；§7.6.4；AC-M34-06、08 |
| 16 | R-M34-34、36、76；M28 §7.2.1（逐字节一致的口径） | 中 | 属实；另核实 CT `board_row.v1` 必填 `recorded_unix_ns` 与 `run_manifest_sha256` | 采纳两项：新增 §7.7.1 一致性比较集，列明各场景的比较对象与豁免项（`attempt`、`run_id`、`infra_retries`、`wall`，路径场景另豁免 `versions.*`、`run_hash` 与派生摘要）；续跑时删除在途 Episode 目录、以初始尝试序号重建；运行目录以 `episode_id` 命名；本地 `rows.jsonl` 不含墙钟字段，提 CR-M34-14 | R-M34-34、36、76；§7.7；§7.7.1；CR-M34-14；AC-M34-06、09、27 |
| 17 | R-M34-73（包数据清单） | 中 | 属实：M29 路径所有权表规定 `py:interaction/params/*.json` 随 wheel 打包 | 采纳：增 M29 参数集；各所有者在其 PRD 声明包数据，M34 汇总为 `package_data.toml` 核对；映射包内未声明的非 `.py` 文件使构建失败，以发现清单本身的遗漏；AC-M34-25 增 wheel 路径解析 `params_ref` 并运行 1 个 Episode；提 CR-M34-11 | R-M34-73；§7.1；§7.10.1；AC-M34-25；CR-M34-11 |
| 18 | R-M34-51；§4.1 步骤 1；R-M34-30 | 中 | 属实：R-M28-76 写 `unclassified`，R-M28-88 规定父进程在创建 worker 前设置环境变量 | 采纳：名称统一为 `unclassified`；`drone4d env` 经与 `hosts.batch` 相同的子进程路径探测，并同时输出主进程派发集合与 worker 等级；步骤 1 判据改为 worker 等级；提 CR-M34-05 | §4.1；R-M34-51；AC-M34-01；CR-M34-05 |
| 19 | R-M34-43、44；M33 UC-02 | 中 | 属实 | 采纳：`--mode full` 增 `--allow-impl-change`，以当前实现重跑并调用 `diverge` 报告首个发散记录（退出码 21）；AC-M34-14 增对应用例；请 M33 修订 UC-02 的表述（CR-M34-13） | R-M34-44；§7.6.1；AC-M34-14；CR-M34-13 |
| 20 | R-M34-52、56、57；§7.9；R-M34-101（`sdk pack` 与提交草稿） | 中 | 属实：LB F6 扫描 `linux/amd64` 与 ≤ 8 GiB；CT §5.5（采纳 LB §13.4）的 `declared` 字段与必填项与 v0.1 不一致 | 采纳：wheel 来源为 `--wheelhouse`，`--download` 为显式联网开关并列入 R-M34-101 的豁免；构建固定 `--platform linux/amd64`；`sdk check` 复现扫描项；草稿字段名与 CT §5.5 逐字对齐（含结构化 `suite{suite_id, major}` 与 `entry.protocol`），签名不在本命令范围（CR-M34-15）；`--no-build` 更名为 `--context-only`，符合 §7.6.2"不用 `--no-` 前缀"的约定 | R-M34-52、56、57、101；§7.6.2；§7.9；§8.3；AC-M34-17；CR-M34-15 |
| 21 | R-M34-57（容器参数与冒烟判据） | 低 | 属实：参数位于 `AgentContainers.argv`（评审所称 `start` 为调用方，结论不变），`__init__` 拒绝以 `DRONE4D_` 开头的键；冒烟集为 6 个 Episode | 采纳：按 `containers.py` 原样列出参数，`sdk check` 校验 `entry.env` 键规则；崩溃率定义为"出现策略失败的 Episode 数 / 6"；AC-M34-19 改为 6 个中 2 个崩溃；同步修正 F-05 | F-05；R-M34-57；AC-M34-19 |
| 22 | R-M34-22；AC-M34-10；§4.1 步骤 4 | 中 | 属实 | 采纳：给出估算公式（平台分项查表、基线卡目标值、`py:` 策略取预检实测或 `--assume-ms`）并逐项标注来源；步骤 4 判据改为"估算 ≤ 6 min 且标注来源"；AC-M34-10 的比值区间收窄为 [0.8, 1.25] | §4.1；R-M34-22、105；AC-M34-10 |
| 23 | §7.8；R-M34-60、62、64 | 低 | 属实：`vec_i8` 在 M30、M31、S1 中均无登记 | 全部采纳：终止与截断的映射；定义 `N`、`S`、`K_dev`、`sender_slot`、`E_max`；量化改为 `numpy.rint` 并给黄金向量；向 M30 登记 `vec_i8.v1`（CR-M34-08）；`team_delta_U` 在 Suite 声明在线形式前报 `training_reward_unavailable` | R-M34-60、62、64；§7.8.2、§7.8.3、§7.8.4；AC-M34-20、21；CR-M34-08 |
| 24 | R-M34-41、40（本地报告的标注） | 低 | 属实：R-M34-41 已含 `S < 12` 提示，但缺"近似计量"标注，§4.3 示例未体现二者 | 采纳 | R-M34-41；§4.3；AC-M34-02 |
| 25 | §7.4.2 `seeds.exogenous`、`instances.count` | 低 | 属实：M31 §7.7.2 的实例坐标含 `block` 与 `index`，公开种子范围由 `exogenous_seeds.public` 声明 | 采纳：增 `seed_out_of_public_range` 校验；`instances.count` 按（`family`, `block`, `index`）字典序取前 `count` 个；同时增 `instances.block` 字段 | §7.4.1；§7.4.2；§7.5；AC-M34-08 |
| 26 | R-M34-82；§1.3 第 6 条；R-M34-97（D3 面冻结） | 低 | 属实 | 采纳评审第二方案（pip 不支持缺省附加包，第一方案不可行）：R-M34-82 与 R-M34-97 明示该依赖集合变更是唯一例外，须在 AWR-06 附录 B 登记并经 M26 同意 | §1.3；R-M34-82、97；AC-M34-33；§13 第 25 条 |
| 27 | §4.1 步骤 0；R-M34-91；AWR-06 §5.10 | 低 | 属实 | 采纳：§13 第 19 条扩为同时修订 AWR-06 §5.10 表；步骤 0 与 `run.sh` 使用显式版本，R-M34-91 写明预发布期间的安装约定 | §4.1；R-M34-91；§13 第 19 条 |
| 28 | §9.2；§13 第 7、14、18 条（工作量） | 中 | 属实 | 采纳：新增 §9.4 自下而上估算（P0 合计 9.3 人周，分配 4.0）；§9.3 增 WP-D4-44 的降级行，并扩充 WP-D4-08 的降级项；新增 RK-M34-08；§13 第 7、14、18 条补估算，新增第 28 条 | §9.3；§9.4；RK-M34-08；§13 第 7、14、18、28 条 |
| 29 | 全文表格版式与链接 | 低 | 部分属实：12 处表格行在单元格内含未转义竖线，属实；CT（`docs/d4/契约草案.md`）与 r04（`docs/research/d4/r04-首篇论文实验设计.md`）现已提交，链接有效，此部分不属实 | 竖线全部转义为 `\|` 或改写为顿号分隔，按列数脚本复核一致；链接不改；另按 CT §9 第 18 项将 `drone4d.cli.result.v1` 更名为 `drone4d.coord.cli_result.v1` | §5.3、§5.4、§5.7、§5.8、§7.4.3、§7.6.1、§13 第 10 条；§8.2 |
