# README-V2 README 真实截图、零下载快速开始与 D1 最终验收结果

| 项 | 内容 |
|---|---|
| 工作包 | README-V2：更新 `README.md` 与 `README.zh-CN.md`（两版内容逐节对应）：头图下方加入真实产品截图与演示动图并注明合成演示城市 ANet Synthetic City，各特性补真实截图；快速开始改为零下载试用优先、UrbanScene3D 六城作为可选的真实数据路径；以 D1 验收报告第 5 轮替换旧的"Measured so far / Where D1 stands"；CI 徽章；链接、图片、emoji 与中文硬换行校验 |
| 日期 | 2026-10-03 |
| 依据 | D1 验收报告第 5 轮（§1、§3 逐条结果、§4 诊断、§5 冻结建议、§7 未测项）；AWR-03 §8.4（验收表、真 GPU 阈值段、D1-AC-20 注 4 的 emoji 与禁用字形规则）、§7.0 ADR 索引（ADR-001 至 ADR-078）、ADR-033、ADR-061、ADR-074、ADR-077、ADR-078；SHOW-M、SHOW-CI、DEMO-W 报告；`.cache/publish/screens.md`；AWR-19 §8.6 |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4）、无 GPU；Node 22.12；Python 3.12（`.venv`）；六城与 `worlds/synthcity` 已生成 |
| 约束执行 | 只改两个 README 与新增本报告；没有安装依赖；没有执行 git 写操作（只用只读的 `git ls-files`、`git check-ignore`）；没有占用性能锁（本包不跑性能用例）；`make lint` 通过 |
| 规格变更 | 无。README 只引用已登记的结果与规格，不新增 ADR，不冻结也不放宽任何阈值 |

## 1 结论摘要

| 任务 | 结果 |
|---|---|
| 头图下方的真实截图 | 完成。`screenshot-hero.jpg`（完整沙盘 UI）与 `demo-flight.webp`（S0 动图）放在徽章与导航之后、"Why"之前，图注写明"当前版本的真实截图，不是概念图"、城市是合成演示城市 ANet Synthetic City（程序生成、不对应真实地点、无第三方数据）、拍摄机器无 GPU（SwiftShader）、点预算由测试开关锁定为 150 万点（HUD 徽标）、HUD 上约 1 s 的帧间隔是该机器读数；动图注明逐帧步进渲染、两倍实时播放 |
| 特性各节补真实截图 | 完成。流式加载下方 `screenshot-streaming.jpg`，环境下方 `screenshot-weather.jpg`，多机同钟下方 `screenshot-swarm.jpg` 与 `screenshot-follow.jpg` 两栏，设计体系下方 `screenshot-perf.jpg`（lieflat 性能面板）。特性开头的 NOTE 改为"开头插图是概念图，图注标有 Screenshot / 截图的是真实截图"。Reality → World 与无人机即智能体没有对应的真实画面，只保留概念图 |
| 快速开始 | 完成。第一段即零下载：`make setup` → `make demo-world` → `make run` → `http://localhost:8000/world/synthcity`；说明 S0 内容、synthcity 的生成方式与可自由再分发、已有六城时的启动命令 `AWR_WORLD=synthcity AWR_SCENARIO=s0-synthcity-showcase make run`。第二段"加入六座真实城市（可选）"：`make fetch-data` → `make worlds` → `make run` → `/world/shenzhen`，紧接 UrbanScene3D 非商业与禁止再分发的 IMPORTANT 提示 |
| 最终验收结果 | 完成。删去 Features 下的"Measured so far"与 Roadmap 下的"Where D1 stands"，新增一级节"D1 acceptance / D1 验收"（导航与 Status 徽章指向它）：通过计数表（含 P0 的 25 项通过 23，只含 P1 的 13 项通过 12，合计 38 项通过 35）、各轮趋势、测试口径 NOTE、16 行关键实测表、"Open items / 未通过项"三条与沿用项说明，链接第 5 轮报告 |
| CI 徽章 | 已存在（SHOW-CI 加入），指向 `ANetResearch/ANet-Drone4D` 的 `actions/workflows/ci.yml`，`.github/workflows/ci.yml` 存在；保留。验收节末尾另链接工作流文件 |
| 校验 | 相对链接与图片各 70 处全部存在，页内锚点各 16 处全部可解析，`no-emoji` 规则 0 违规，中文 README 无段内硬换行，`make lint` 通过（§4） |

## 2 改动明细（两版逐节对应）

| 位置 | 改动 |
|---|---|
| 头部描述 | "built for up to 1,000 drones / 按至多 1,000 架设计"改为"up to 1,000 drones / 至多 1,000 架"（D1-AC-07 已实测 1,000 架 RTF 1.000） |
| 徽章 | Status 由 `in progress`（指向 Roadmap）改为 `in acceptance`（指向 D1 验收节）；Specs 由 `26 + 53 ADRs` 改为 `26 + 78 ADRs`（AWR-03 §7.0 当前为 ADR-001 至 ADR-078；26 = AWR-03、10–19 共 10 份加 16 份模块 PRD）；CI 徽章不变 |
| 导航 | 增加 Acceptance / 验收结果 |
| 头图下方 | 新增 hero 截图与动图及图注（§1） |
| Features NOTE | 概念图与真实截图的区分 |
| Reality → World | 增加一句：第七个世界 ANet Synthetic City（固定种子、1.2 km × 1.2 km、约 390 万点、无第三方数据，约 25 s 构建），本页所有截图都来自它 |
| 流式、环境、多机、设计体系 | 各加真实截图与图注（数字取自 SHOW-M §2 与截图 HUD） |
| 多机同钟正文 | 删去"1,000 架机群阶梯在性能阶段运行"，改为"1,000 架时用约半个 CPU 核保持实时"，链接验收节 |
| 无人机即智能体 | "S3 在锁步测试环境中通过、多进程等待 agent 租约"改为"S3 已在真实多进程（sim-core、api、agent-runtime）下端到端通过，×1 与 ×10 决策一致"（D1-AC-16 第 5 轮通过；`tests/e2e/test_scenarios.py` 的 `S3_PROCS`） |
| 还有这些 · 剧本 | 增加 S0 ANet Synthetic City 全景展示 |
| D1 验收（新节） | 见 §3 |
| 快速开始 | 见 §1；"更多命令"表、SSH 转发与局域网段保留 |
| 链路表 | Reality 增加"以及程序生成的 ANet Synthetic City"；Simulation 由"机群阶梯待测"改为"1,000 架用约半个核保持实时" |
| 文档表 | "53 条 ADR"改为"78 条 ADR"；实现报告的"当前状态"由 INT-1 集成报告改为 D1 验收报告第 5 轮 |
| 版本路线 | V0.1 行主要内容加"合成演示城市"，状态改为"验收中：38 项中 35 项通过，2 项 P0 未通过"；删去"Where D1 stands / D1 当前进展"小节 |

英文版正文沿用原有约 120 列的折行；中文版每个段落、列表项、表格行与图注都是一行（头部居中块里以 `<br/>` 结尾的三行是原有的显式换行，每行是完整的句子）。

## 3 验收节的数据来源

验收节的全部数字取自 D1 验收报告第 5 轮，只有两处来自 ADR 与设计基线，逐项如下：

| README 中的内容 | 来源 |
|---|---|
| 计数：38 项通过 35；含 P0 的 25 项通过 23；只含 P1 的 13 项通过 12；各轮 13、22、24、31、35；本轮复测 23 项、沿用 15 项；两项 P0 不通过都是本轮回归 | 第 5 轮 §1 |
| 测试口径：机器、SwiftShader、Tier S（1280 × 720、0.5 渲染比例、30 fps 目标）、ADR-033 协议 | 第 5 轮 §2.1、§2.3；AWR-03 §8.4 环境列说明 |
| 真 GPU 阈值（集显 p50 等于刷新周期、掉帧 ≤ 5%、TTFP ≤ 700 ms；独显掉帧 ≤ 2%、TTFP ≤ 500 ms）为设计值、不阻塞 D1 | AWR-03 §8.4 表后"真 GPU 的绝对阈值"段 |
| 01 World 构建 | 第 5 轮 §3 D1-AC-01（沿用第 1 轮，单城 27.3–35.1 s） |
| 02 首屏：TTFP 四城 1.8–8.1 ms、旧金山与芝加哥沿用第 4 轮 4.4、3.7 ms（合写为"六城 1.8 到 8.1 ms"）；切换 489 ms；`scene=pc` 冷启动 3.52–3.76 s；整景冷启动 17.56 s 为记录项 | 第 5 轮 §3 D1-AC-02 |
| 03a、03b、09a 帧节奏 | 第 5 轮 §3 对应行（03a 纽约最大 383 ms，深圳、上海、苏州 9 次最大 67–117 ms 取自 §4.2a） |
| 04、06、25、19、26 | 第 5 轮 §3 对应行；26 的渲染延迟 D_global 约 206 ms 取自该行"减 D_global（约 206 ms）为 −62 ms" |
| 07、08、10 | 第 5 轮 §3 对应行；10 的丢弃注入 591 次（207 + 189 + 195）全部 69 ms 内补齐取自 §4.4 |
| 11a、11b 崩溃恢复 | 第 5 轮 §3 D1-AC-11a（chaos-core 2/2）与 D1-AC-11b（沿用第 3 轮，阈值断言）；"功能剖析中 2.1 s"取自 ADR-061 实测段（kill -9 到 RUNNING 2.10–2.11 s，机器空闲时） |
| 29 长稳 | 第 5 轮 §3 D1-AC-29（保留堆 +3.00%、RSS api +0.53%、sim-core +0.24%、重连 0） |
| 15、16、17 剧本 | 第 5 轮 §3 对应行 |
| 未通过项三条（03a、27、28）的数值与原因 | 第 5 轮 §1、§3、§4.1、§4.2a、§4.3；28 的"ADR-074 列为已知问题、豁免未登记"取自 §3 与 §7 |
| 应复测的 5 项（18、11b、23、30、09b）与有子项未完成的 22、33；Tier A 产品后端未交付、WebGPU 功能矩阵在回归页通过 | 第 5 轮 §7；第 3 轮 §3 D1-AC-14、22、33 |
| 托管 CI 的步骤 | SHOW-CI §5 与 `.github/workflows/ci.yml` |

图注中的截图数字（150 万点预算、72.4 万 / 104 万 / 150 万点、能见度 6.0 km / 690 m / 150 m、动图每帧约 85 万点与每帧 0.1 s、性能面板 Tier S、1 万点、p50 133 ms）取自 SHOW-M §2、§4.3 与 `.cache/publish/screens.md`，并逐张看图核对过（hero 的 HUD p95 1033.3 ms 与"测试开关强制档位"徽标、性能面板的 p50 133.3 / p95 200.0 ms 与 soft-min）。性能面板截图是 1920 × 1080、不在性能协议下拍摄的，图注写明"其中帧时间不是验收结果"，并放在设计体系一节（展示 lieflat 图表），不与验收表并列，避免与验收口径的 p50 33.3 ms 混淆。

## 4 校验

| 项 | 方法 | 结果 |
|---|---|---|
| 相对链接与图片存在 | 脚本提取两版代码块之外的全部 `](…)`、`src="…"`、`href="…"`，URL 解码后检查仓库内文件 | 各 70 处，全部存在 |
| 页内锚点 | 按 GitHub 的标题锚点规则（小写、去标点、空格变连字符、保留 CJK）生成锚点集合并比对 | 各 16 处，全部可解析（含 `#d1-acceptance`、`#open-items`、`#d1-验收`、`#未通过项`） |
| emoji 与禁用字形 | `node tools/lint/no-emoji.mjs README.md README.zh-CN.md`（EMOJI-01、GLYPH-01，与 D1-AC-20 同一规则；根目录 README 不在 lint 的默认扫描范围，故显式传入） | 0 违规 |
| 中文段内硬换行 | 脚本检查代码块、表格、HTML 块之外相邻两行都是正文的情形（以 `<br/>` 结尾的显式换行与提示块的 `> [!NOTE]` 首行除外） | 0 处 |
| 检查脚本自身 | 在副本中故意加入一处段内换行、一个不存在的图片与一个不存在的锚点 | 3 处都被报出 |
| 两版结构对应 | 比对两版的标题序列与图片序列 | 一级与二级标题逐节对应；14 张图片顺序一致 |
| 零下载流程 | 只读核对：`scenarios/catalog.json` 中 synthcity 的默认剧本为 `s0-synthcity-showcase`；`AWR_WORLDS_DIR` 指向只含 synthcity 的目录时 `worldpkg default-world` 为 synthcity，`load_runtime_config(world_fallback=True)` 回退到 synthcity 与 S0；本机（有六城）默认世界为深圳；`AWR_WORLD=synthcity AWR_SCENARIO=s0-synthcity-showcase` 时运行配置为 synthcity 与 S0 | 与 README 描述一致 |
| `make lint` | 全部规则 | 通过 |

检查脚本在本会话的 scratchpad 中（不入库）。

## 5 遗留与建议

1. **必须与 README 一起入库**：README 引用的 `docs/media/screenshot-{hero,streaming,swarm,weather,follow,perf}.jpg`、`docs/media/demo-flight.webp` 与 CI 徽章指向的 `.github/workflows/ci.yml` 目前都未跟踪（`git ls-files --others --exclude-standard` 列出，`git check-ignore` 确认没有被忽略）。只提交 README 会在 GitHub 上显示 7 张破图，CI 徽章也会在工作流首次运行前显示无状态。SHOW-CI §7 第 1 条的 `python/awr/swarm/coverage/` 同样需要在同一次提交中加入。7 个媒体文件合计 8.1 MB。
2. **验收结果会过时**：验收节以第 5 轮为准。修复阶段若再做一轮验收（03a、27 的 Toast 修复，28 的豁免登记，18、11b、23、30、09b 的复测），应同步更新计数表、关键实测表与未通过项，以及 Status 徽章和版本路线的 V0.1 状态。
3. **`docs/README.md` 仍写"53 条 ADR"**（AWR-03 行），不在本包的两个文件范围内，建议文档地图的维护方改为 78。
4. **截图的界面语言是中文**（拍摄时 locale 为 zh-CN），英文 README 中截图的标签也是中文；若需要，可在 en 语言下按 `.cache/publish/screens.md` 的步骤补拍一组。
5. **展示图使用强制 Tier B 与锁定预算**（SHOW-M §4.2），图注已如实说明；有 GPU 的机器上默认配置的画面会好得多，建议以后在 GPU 机器上补拍一组默认配置截图（SHOW-M §8 第 4 条），届时可去掉图注中的测试开关说明。
6. **根目录 README 不在 `no-emoji` 的默认扫描范围**（`tools/lint/_common.mjs` 的 `DOC_GLOBS` 只含 docs/03、docs/1x、docs/modules 与 docs/README.md）。本包显式运行了该规则；若希望 README 也由 `make lint` 持续约束，需要按 AWR-03 §1.3 修订 D1-AC-20 注 1 的扫描范围（ADR）并同步 AWR-18 §13.2。

## 6 文件清单

修改：`README.md`、`README.zh-CN.md`。
新增：本报告。
不入库的过程文件：scratchpad 中的 `check_readme.py`（链接、锚点与硬换行检查）、两份 README 的改动前备份与 `make lint` 日志。
