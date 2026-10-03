# DEMO-W 合成演示城市 synthcity：零下载试用与可再分发截图

| 项 | 内容 |
|---|---|
| 工作包 | DEMO-W（M03 世界生成与默认世界回退；M16 剧本与演示资产；M15 世界中心与世界面板；M11 runtime 配置的回退钩子） |
| 日期 | 2026-10-03 |
| 依据 | 任务书 DEMO-W；AWR-03 §1.3、§4.3、ADR-033、ADR-034、ADR-053、附录 E（ADR-054 至 ADR-076）；D1 验收报告第 3 轮（§3–§5）；AWR-16 §3.5、§5、§10、§12；AWR-18；AWR-19 §4.3、§8；M03、M16 PRD |
| 环境 | VMware 虚拟机 8 vCPU（E5-2603 v4 1.7 GHz）、无 GPU；Node 22.12；Python 3.12（`.venv`，numpy 2.5.3）；Chrome for Testing 151（SwiftShader）；六城 `worlds/` 已生成 |
| 约束执行 | 没有安装依赖；没有执行 git 写操作（只用了只读的 `git status`）；重计算（构建、端到端、浏览器）都持共享性能锁 `runs/.perf.lock`；排他锁只持有一次，约 25 s（perf 用例 `test_full_build_budget`）；共享的 `apps/web/dist` 没有改写，浏览器验证用私有测试构建（`M05_DIST`、`AWR_WEB_DIST` 指向暂存目录）；`make lint` 通过 |
| 规格变更 | 新增 AWR-03 ADR-077（附录 E 正文与 §7.0 索引）；M03 新增 FR-066、FR-067、§6.18、AC-033 至 AC-036，修订 FR-044、FR-053、§6.12（3）状态映射、§7.1、§9.1；AWR-16 修订 §3.5、§12.1、§12.3、§12.5、V-SC-08，新增 §10.5 与 DATA-FR-066；M16 新增 FR-075、§6.2.5、§6.4.9、AC-043、AC-044，修订 §6.4.1、§7.3.1、§9.1；AWR-19 修订 §4.3、§6.2、§7.2，新增 §8.6 与 OPS-FR-042；README（中英）增加"不下载数据也能试用"。**没有冻结或放宽任何阈值** |

## 1 结论摘要

| 任务 | 结果 | 证据 |
|---|---|---|
| 1 合成城市生成器与 World Package | 完成。`python/awr/world/ingest/synthetic.py` 确定性生成 1.2 km × 1.2 km、3,924,500 点的城市（道路网格与路口、桥、六类建筑、两座 300 m 级地标、立面窗格密度纹理、屋顶设备、行道树、公园、河道），带法线与 `anet-classes@1` 九类类别；经现有 ingest → grid → tile → derive → package → validate → publish 生成 `worlds/synthcity`，`anchor.kind = synthetic`，显示名 ANet Synthetic City | 生成 5.0 s；构建 23.4–25.0 s；`validate --deep` 0 错误 0 警告；两次独立构建逐字节一致（§2、§3） |
| 2 synthetic 来源与默认世界回退 | 完成。`configs/worldpkg.yaml` 新增 `synthetic` 段，`configs/runtime.yaml` 新增 `run.fallback_world`；`make demo-world`；无原始数据时 `make worlds` 自动生成 synthcity，supervisor、`awr doctor` 与 `make worlds` 的默认世界切到 synthcity 与 S0；有原始数据时仍默认深圳 | 空目录、无数据的 `make worlds` 27 s 退出码 0；supervisor `READY ... world=synthcity`；`/` 落到 `/world/synthcity`（§4） |
| 3 演示剧本 S0 | 完成。`scenarios/s0-synthcity-showcase.json`：7 架（地标立面两段螺旋、两机街区覆盖、三机 V 形编队沿河巡航），天气晴 → 小雨 → 雾；285.6 s【仿真】；ci profile ×5 | 静态校验与加载器（已构建世界）通过；端到端 ×5 `SUCCEEDED`、7 个谓词全真、墙钟 65.1 s（§5） |
| 4 前端 | 完成。World Hub 与世界切换显示 synthcity（可用世界排前，未构建城市给出 `make fetch-data` 提示）；世界面板写"程序生成，可自由再分发"；`/` 的临时默认跳转按运行世界修正。Tier S 渐进加载与 CAS 正常；Tier B 在本机 SwiftShader 上 CAS 停在最低档（与深圳完全相同），锁定预算时 Tier B 流式加载正常 | `perf/m03/synthcity.spec.ts` 3 例通过；Hub 顺序与重定向实测（§6） |
| 5 文档 | 完成。16、M03、M16、19 相应小节与 AWR-03 ADR-077 | §8 |
| 测试 | `tests/world/test_synthcity.py`（确定性、字节一致、`validate --deep`、生成耗时、回退规则对拍）17 例 + perf 1 例；`tests/e2e/test_synthcity_showcase.py` 3 例；`make lint` 通过 | §7 |

## 2 合成城市生成器

### 2.1 设计

生成帧即 World ENU（x 东、y 北、z 上，米），城市范围 [−600, 600]²。四角各放一个地面点、桅杆顶放一个显式顶点，使包围盒中心、DTM 中位数与最高点都与点数无关；规范化后 `T_world_source` 为单位阵，剧本直接使用生成坐标。

| 子系统 | 内容 |
|---|---|
| 用地栅格（0.5 m，2400 × 2400） | 9 种用地码：铺装、路面、标线、草坪、河水、池塘、河岸、桥面、建筑。南北向道路 x = −480…480 每 120 m、东西向 y = −300…540 每 120 m 与南路 y = −540；大道 26 m、街道 18 m、人行道 5 m；车道虚线（6 m 实 6 m 空，路口内不画）与斑马线（0.5 m 间隔）；河道中心线 y = −415 + 14·sin(2π(x + 600)/640)，水面半宽 22 m，岸坡到 31 m；南北向道路跨河处为桥面（z 0.6 m）；中央公园 2 × 2 街区（环形与对角园路、池塘 −1.2 m、10 m 小丘）；滨河绿带与步道 |
| 建筑（255 栋、372 个体块；树 2,837 棵、灌木 151 丛） | 按街区中心到 CBD (0, 60) 的距离：d < 330 m 为裙楼加 1–2 座塔（矩形、阶梯退台、圆柱，70–210 m）；d < 560 m 为地块（矩形、L 形）、围合院落或双板楼（15–60 m），浅街区只用地块；其余低层（7–16 m，45% 为 30° 坡屋顶）；南岸一排低层。地标：圆柱 ANet Tower（中心 (62, 122)，底半径 25 m、300 m 处 19 m，开口塔冠到 318 m，桅杆到 352 m）；阶梯退台塔（中心 (−62, −2)，裙楼 24 m，四级 44/36/28/18 m 到 286 m） |
| 立面窗格纹理 | 五种风格（幕墙、方窗、带窗、住宅、办公），窗距 1.6–7.5 m、层高 3.0–4.0 m、底层商铺另计；窗框与窗间墙全保留，玻璃按 0.10–0.22 的接受率保留，点密度差即纹理（0.5 m 竖向条带计数的变异系数 > 0.15，测试断言）；首版玻璃后退 0.2 m，被 M03 法线修正的 2 m 外取规则误翻（2 m 顶面格边界落在玻璃与窗框之间），改为不后退 |
| 屋顶 | 屋面、女儿墙（0.8–1.5 m）、机组箱体与水箱（落在其他体块内的剔除） |
| 植被 | 行道树（离路口中心 ≥ 18 m、离立面 ≥ 4.8 m）、公园树丛（抖动网格 13 m）与灌木、滨河散植；树冠为椭球壳（外壳加 28% 厚度的内部点）加树干 |
| 体块合成 | 外立面判定：外移 0.3 m 落入同建筑其他实心体块的点是内墙；与更早体块共线的边界只保留一份；同高体块的女儿墙按体块顶高判定；被更高体块覆盖的屋面与同高重叠的屋面去重 |
| 确定性 | 每个子系统独立的 `default_rng([seed, k, i])`；坐标舍入到 1 mm；规范字节流（`AWRSYN1\n` + xyz `<f8` + normal `<f4` + class `u1`）的 sha256 作为输入指纹 |

点数由全局密度系数 k = target_points / 期望点数控制（窗格接受率已计入期望），目标 400 万、实际 3,924,500（−1.9%）。

### 2.2 实测（`worlds/synthcity`，contentVersion `9e05c2364f58`）

| 项 | 值 |
|---|---|
| 生成 | 3,924,500 点，5.0 s（规划 0.75 s） |
| 类别直方图 | 地面 510,151；低矮植被 257,740；中等植被 4,458；高植被 569,742；屋顶 408,242；立面 1,730,515；水面 29,151；路面 399,044；桥面 15,457 |
| 构建阶段 | read 5.41 s（含生成）、ingest 4.71、dtm 1.16、normals 1.77、classify 0.15、dsm 3.12、tile 5.21、zones 0.02、hash 0.66、validate 1.77；合计 24.4 s，峰值 RSS 1.27 GB；perf 用例（排他锁）25.0 s ≤ 60 s |
| 门禁 | G-01 352.0 m（最高点即桅杆顶）、G-02 0.061 mm、G-03 848.5 m、G-05 0.02°、G-06 0、G-07 地面占比 29.3%、G-08 证据 2 条、G-09 守恒；qa `pass` |
| 切片 | 1 根、深度 5、439 节点；首屏规则 G 190,507 点 / 2,286,084 B；Tier S L1 40,855 点；`nnMedianM` 0.382 |
| 法线翻正 | 2.4%（植被 12.9%、立面 1.2%，都来自 M03 §6.4 的 2 m 外取规则在曲面与树冠处的误判；六城为 2.7–21.1%） |
| 体积 | 107.9 MB（octree 47.1 MB、源点云 58.9 MB、DSM 1.8 MB） |
| 确定性 | 同一配置两次生成 sha256 一致；`worlds/` 与无数据暂存目录两次独立构建的 contentVersion 都是 `cbe62eb0de2b`（同一 zones 文件时），逐文件字节一致（测试对缩小点数的城市做同样断言） |

## 3 流水线与 World Package 改动（M03）

1. **`semantic = provided`**：`IngestConfig.semantic` 新增取值，`RawCloud.class_index` 携带 `anet-classes@1` 紧凑索引；ingest 第 8 步直接采用（与 `IngestFromArrays.class_index` 同一语义），其余九步不变。规则分类不产生植被、水面与路面，故不用。
2. **锚点标签覆盖**：`IngestConfig.anchor_label`，`solve_anchor(label=...)`（必须以 `illustrative:` 开头）。synthcity：`illustrative: ANet Synthetic City, procedurally generated; no real location`，示意锚点 (30.0, 120.0, 10)，不确定度 5000 m；`trueNorth = exact`（+Y 即北由构造给出）；`scaleStatus = assumed`；`source.kind = simulation`。
3. **清单**：`tags = [synthetic, builtin, generated, redistributable]`；`dataset` 的 name、version、url、citation、license 齐全，`redistribution = true`，`sourceFiles` 为规范字节流（V-W-12 通过）；`camera.home` 为能看到两座地标的西南俯视位；`generator.params.synthetic = {generator, version, seed, sizeM, targetPoints, sourceSha256}`（`awr.world.package.params.SynthParams`）。
4. **`--missing`**：`missing_reason(..., synthetic=...)` 以 `generator.params.synthetic` 与当前配置比较，不同即 `raw_changed`。开发中发现只比较 `GENERATOR_VERSION` 时改动算法不会触发重建，于是加入生成器源文件 sha256 前 16 位。`build_missing` 新增 `_synthetic_todo`：显式点名或 `AWR_WORLD` 指向合成世界时构建；`--missing` 时已发布的保持新鲜，未发布的只在回退需要时生成；`worlds-force` 只重建已有的合成世界。
5. **CLI**：`worldpkg ingest synthetic [--city synthcity]`、`worldpkg build synthcity`、`worldpkg status`（增加合成世界行与默认世界）、`worldpkg default-world [--json] [--field world|scenario]`。
6. **目录服务**：`.status` 原因为 `raw_missing` 的失败记录报 `missing`（AWR-16 §3.5 状态表：缺原始数据时世界仍为 ABSENT）。此前无数据的机器上六城会显示"构建失败"。

## 4 默认世界回退与零下载试用

规则（ADR-077 第 5 条）：显式 `AWR_WORLD` 或 `--set run.world=` 永远优先；否则用 `run.world`（深圳），已构建即用；否则若 `run.fallback_world`（synthcity）已构建，改用它，剧本未显式指定时取 catalog 中它的 `default`（S0）。

| 落点 | 实现 |
|---|---|
| M03 | `awr/world/package/defaults.py`：`resolve_default_world`（`worldpkg default-world`，`make worlds` 据此判定"默认世界不可用"）与 `fallback_needed`（`build --missing` 是否自动生成） |
| runtime（M11） | `awr/runtime/config.py`：`RunCfg.fallback_world`（缺省 synthcity，null 关闭）、`apply_world_fallback`、`load_runtime_config(world_fallback=True)`；supervisor 与 `awr` CLI 打开，打印一行提示。runtime 不得 import `awr.world`（AWR-10 §3.3 规则 2），因此规则在两处各实现一次，`test_default_world_rule_matches_runtime` 用 6 组状态对拍 |
| Make（M03） | `mk/m03.mk`：`worlds-build` 改用 `worldpkg default-world` 判定默认世界；新增 `make demo-world` |
| 前端（M15） | `ui/shell/defaultWorld.ts`：`/` 在没有上次世界时仍先跳 `/world/shenzhen`，标记为临时跳转；首个 `serverInfo` 的运行世界不同则以 replace 改为运行世界（重定向在 router 的 resolve 内，不同步导航） |

实测（无数据，空 `worlds/` 暂存目录）：`AWR_DATA_DIR=<空> AWR_WORLDS_DIR=<空> make worlds` 27.0 s、退出码 0：六城各报 RAW_MISSING，synthcity 构建 23.5 s，提示"默认世界为合成演示城市 synthcity"，随后 flight60 与 geo-warm 正常处理 synthcity。不设 `AWR_WORLD` 启动 supervisor：打印"默认世界 shenzhen 未构建，回退到合成演示城市 synthcity（剧本 s0-synthcity-showcase；ADR-077）"并以 `world=synthcity` READY；新浏览器配置打开 `/`，最终 URL 为 `/world/synthcity`，无页面错误。本机（有数据）`worldpkg default-world` 仍为深圳。

## 5 演示剧本 S0

定稿参数在 `python/awr/datasets/synthcity.py` 的 `SHOWCASE`，文件由 `authoring.s0_scenario()` 生成（剧本只能经 authoring 修改，`test_authoring_deterministic_and_committed`），坐标钉经 `make scenarios-pin`。

| 组 | 机体与出生点 | 任务 |
|---|---|---|
| 立面螺旋 | `p600-h1`、`p600-h2`，路口 (0, 60) 的 (∓8, 60) | `helix_scan` 中心 (62, 122)，半径 58 m、立面距参数 30 m，上段 120 → 80 m、下段 80 → 40 m，`dz_per_rev` 16 m，9 m/s；h2 带 mid360 |
| 街区覆盖 | `p600-c1`、`p600-c2`，路口 (360, −60) | `lawnmower` x ∈ [260, 460]、y ∈ [−165, −75]（两个中层街区，≤ 42 m），fly_over 70 m AGL、旁向重叠 0.4、9 m/s |
| 编队巡航 | `p600-f1..f3`，路口 (−480, −300) | `formation` V 形（12 m、35°），沿河道中心线 x −430 → 140 再沿北岸 y = −335 返回，z 40 m、10 m/s |
| 天气 | — | 开局 clear；45 s `env.preset{name: lightRain, duration_s: 20}`；150 s `env.preset{name: fog, duration_s: 40}` |

迭代记录：

1. 首版螺旋半径 54 m：`facade_coverage` 0.767。原因：M10 立面覆盖的柱面代理半径 = radius − standoff = 24 m，小于塔底半径 25 m，代理格心外 0.5 m 的视线终点落进含塔体的 2 m DSM 格，M04 视线判为遮挡。改为 58 m（代理 28 m）后 1.0。
2. 首版各段时长使剧本 335.6 s【仿真】结束；缩短螺旋高度段、提高速度、缩短编队东端后为 285.6 s。
3. 发现 AWR-16 §12.3 写的 `env.preset{preset, duration_s}` 与 `env/preset` 命令契约 `{name, duration_s}`（`additionalProperties: false`）冲突：剧本导演把参数原样转给命令，旧写法会被 `op_from_msg` 以 300 拒绝，而 V-SC-08 却只检查 `args.preset`。现有剧本没有用到这个事件，所以一直没暴露。处理：S0 用 `name`；V-SC-08 两种写法都接受；剧本导演把旧写法 `preset` 改写为 `name`；16 §12.3 改为 `{name, duration_s?}`。

端到端（ci profile ×5，`pytest tests/e2e/test_synthcity_showcase.py::test_s0_showcase_x5`）：`SUCCEEDED`；`facade_coverage` 1.0、`area_coverage` 1.0、`formation_err_rms_m` 0.23 m、`min_separation_m` 11.84 m（编队槽位间距）、`guard_events` 0、`landed_all` 真、`missions_done` 真；下段螺旋 226.2 s、覆盖 244.4 s、上段螺旋 269.2 s、编队 285.6 s 完成（含返航降落）；两次天气事件 `code 0`；墙钟 65.1 s（含启动 7.2 s）。curated 区域（阶梯塔禁飞、池塘限制）与出生点、航线、返航线最近 27.4 m（≥ 10 m）。

## 6 前端与 Tier S / Tier B

改动：World Hub 按"运行中 → 可用 → 其余"稳定排序，`missing` 卡片显示"需要原始数据：make fetch-data，再 make worlds"；世界面板对 `dataset.redistribution = true` 的世界写"数据 ANet Synthetic City v1.0.0 · 程序生成，可自由再分发"（仍有"示意坐标"徽标）；关于区块的数据行增加合成城市一句（六城的 UrbanScene3D 与"科研用途"文字不变，honesty 与 fxweb2 规格的断言不受影响）；i18n 中英键各 3 个。

浏览器冒烟 `apps/web/perf/m03/synthcity.spec.ts`（M05 静态服务器，私有测试构建，每 250 ms 采样 12 s）：

| 用例 | 结果 |
|---|---|
| Tier S | 首帧 2.0 万点（≤ 1e5），驻留点随后增长到 2.2 万；CAS 在 soft-min 档、受预算限制，预算不低于下限；无失败请求、无页面错误 |
| Tier B（强制，SwiftShader） | 首帧 14,492 点后帧时余量不足，CAS 停在 soft-min、`limitedBy = headroom`；约 9 s 后 PerfGovernor 把预算降到 1 万（低于 CAS 下限 2 万）。**深圳在同一条件下的序列完全相同**（15,466 点、9 s 后降到 1 万），说明这是本机 SwiftShader 上强制 Tier B 的行为，不是 synthcity 的问题；ADR-044 规定软件渲染取 Tier S |
| Tier B 锁定预算（`fixedB=600000`） | 首帧 40,855 点，流式加载到 155,017 点，`limitedBy = error`（已达目标精度），无失败请求 |

带后端的检查（supervisor 回退到 synthcity，`AWR_WEB_DIST` 指向私有构建）：`/` 落到 `/world/synthcity`；World Hub 顺序 `synthcity, shenzhen, …`；世界面板数据行如上。另以 Tier B、200 万锁定预算、high 画质截了带 S0 机群的全界面图（`runs/playwright/synthcity-hero.png`）：SwiftShader 下帧间隔 1.8 s，近景点云呈块状，不代表 GPU 上的画质，因此没有放进 README；README 只加文字与命令。

## 7 测试

| 文件 | 内容 | 结果 |
|---|---|---|
| `tests/world/test_synthcity.py`（新） | 生成器确定性与内容；配置解析；完整规格生成 ≤ 15 s（实测 5 s）；缩小点数城市两次构建 `validate --deep` 0 错误 0 警告、contentVersion 与全部文件逐字节一致、清单字段；`missing_reason` 的 raw_changed；`build_missing` 自动生成与不再重建；默认世界规则 M03 与 runtime 对拍 6 组；runtime 不带开关或显式指定时不回退；已发布 synthcity 与演示事实一致；perf：完整构建 ≤ 60 s | 17 + 1（perf）通过 |
| `tests/e2e/test_synthcity_showcase.py`（新） | S0 形状、catalog；S0 ×5 端到端 | 3 通过（端到端 65 s 墙钟） |
| `apps/web/perf/m03/synthcity.spec.ts`（新） | Tier S、Tier B、Tier B 锁定预算 | 3 通过 |
| `tests/e2e/test_scenarios_static.py`（改） | 内置集合、zones 文件集合、catalog 与 free 剧本加入 synthcity；V-SC-07 的 border 上限取 `authoring.facts()` | 通过（含已构建世界上的加载器规则） |
| `tests/world/test_catalog.py`（改） | `raw_missing` → missing，其他失败 → failed | 通过 |
| `tests/world/test_missing.py`（改） | `worldpkg status --json` 含合成世界行与 `default_world` | 通过 |

回归：`pytest -m "not perf" tests/world tests/scenarios tests/mission tests/contracts tests/runtime tests/jobs` 774 通过；Vitest unit 954 通过、browser 45 通过；`make lint` 通过（ruff、oxlint、no-emoji、no-hex 等全部规则；THR-04 为既有只报告项）。

## 8 文档变更

- **AWR-03**：ADR-077（附录 E）与 §7.0 索引行。
- **AWR-16**：§3.5 合成世界的条件 ⑤ 与自动生成；新增 §10.5 synthcity 规范值；§12.1 内置剧本加 S0；§12.3 `env.preset` 参数改为 `{name, duration_s?}`；§12.5 S0 行；V-SC-08；DATA-FR-066。
- **M03**：FR-066、FR-067；FR-044、FR-053 补充；§6.12（3）状态映射新增"原始数据缺失 → missing"；新增 §6.18（定位、配置、算法、适配器、`--missing`、实测）；§7.1 CLI；§9.1；AC-033 至 AC-036。
- **M16**：FR-075；§6.2.5 演示定位与事实；§6.4.1 主题表"全景展示"；新增 §6.4.9 S0 定稿与实测；§7.3.1 catalog；§9.1；AC-043、AC-044。
- **AWR-19**：§4.3 流程图与"默认世界回退"说明；§6.2 `run.fallback_world`；§7.2 `make demo-world`；新增 §8.6 合成演示城市与零下载试用；OPS-FR-042。
- **README.md、README.zh-CN.md**：快速开始后增加三条命令的零下载试用与 `make demo-world`。

## 9 发现与遗留

1. **强制 Tier B 在 SwiftShader 上停在最低档**：与深圳相同（§6），属设备行为；Tier B 的渐进加载用锁定预算验证。PerfGovernor 会把点云预算降到 CAS 下限以下（1 万 < 2 万），这是现有的预算仲裁行为（ADR-041），不在本包范围，记录供 web-engine 参考。
2. **性能报告页脚**：`Report.tsx` 的数据行固定写"虚拟城市采样点云 … 科研用途"，`check-report.mjs` 也断言页脚含"科研用途"。若以后在 synthcity 上出性能报告，页脚需要按 `dataset.redistribution` 改写，校验规则同步调整（M16）。本包没有改，因为现有用例都在深圳等六城上。
3. **生成器版本纪律**：`generator.params.synthetic.sourceSha256` 让任何 `synthetic.py` 改动（包括注释）都会使 synthcity 在下次 `make worlds` 时重建（约 25 s），重建后需要 `make scenarios-pin` 与再一次 `make worlds`（zones 文件钉住坐标哈希后其 sha256 变化）。只要几何不变，`coordinate.sha256` 就不变，钉值不变。
4. **跨平台字节一致**：同一平台与 numpy 版本下逐字节一致；不同 CPU 或 numpy 版本下三角函数末位可能不同，坐标虽舍入到 mm，仍不保证跨平台的 sha256 相同（与六城的 R-1 同口径，`generator.params.numpy` 记录版本）。
5. **分步命令**：`worldpkg ingest synthetic` 之后的 `package` 分步命令重新构造的参数不含 `synthetic`，分步产物只用于诊断（不发布），与 recon 的分步命令一致。
6. **立面窗格纹理的可见度**：窗格表现为点密度差（测试断言变异系数 > 0.15）；在 400 万点预算与当前点径下近景不形成清晰的窗格线，这是点预算决定的，不影响作为演示与截图的世界。
7. **`make demo` 仍固定深圳与 S1**（门禁演示，需要数据）；零下载用户用 `make run`。

## 10 文件清单

新增：`python/awr/world/ingest/synthetic.py`、`python/awr/world/package/defaults.py`、`python/awr/datasets/synthcity.py`、`scenarios/s0-synthcity-showcase.json`、`scenarios/free-synthcity.json`、`scenarios/zones/synthcity.zones.geojson`、`tests/world/test_synthcity.py`、`tests/e2e/test_synthcity_showcase.py`、`apps/web/perf/m03/synthcity.spec.ts`、`apps/web/src/ui/shell/defaultWorld.ts`、本报告。

修改：`python/awr/world/ingest/{types,anchor,pipeline}.py`、`python/awr/world/package/{build,cli,params,derivers,catalog}.py`、`python/awr/runtime/{config,supervisor,cli}.py`、`python/awr/sim/mission/{scenario_loader,director}.py`、`python/awr/datasets/scenarios/authoring.py`、`configs/{worldpkg,runtime}.yaml`、`mk/m03.mk`、`scenarios/catalog.json`、`apps/web/src/app/routes/index.tsx`、`apps/web/src/ui/views/{WorldHub,AboutDialog}.tsx`、`apps/web/src/ui/panels/world/WorldPanel.tsx`、`apps/web/src/app/i18n/{zh-CN,en}.json`、`tests/e2e/test_scenarios_static.py`、`tests/world/{test_catalog,test_missing}.py`、`docs/03-设计基线与决策记录.md`、`docs/16-World数据规范.md`、`docs/19-部署与运维说明书.md`、`docs/modules/M03-World模型与Ingest切片PRD.md`、`docs/modules/M16-演示数据剧本与流畅性测试PRD.md`、`README.md`、`README.zh-CN.md`。

生成物（不入库）：`worlds/synthcity/`（约 104 MB）、`apps/web/public/bench/flight60/synthcity.{bin,json}`（`make worlds` 的 flight60 目标）。
