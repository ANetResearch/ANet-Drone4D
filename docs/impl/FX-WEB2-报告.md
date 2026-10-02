# FX-WEB2 前端 UI 面板与产品完成度：验收与加固报告

| 项 | 内容 |
|---|---|
| 工作包 | FX-WEB2（区域：前端 UI 面板与产品完成度，M15 / M12 / M01 前端） |
| 日期 | 2026-09-29（第 1 轮）；2026-10-01（续作：复核、补齐遗留与第二遍 UI 精修，见 §1.6） |
| 依据 | INT-1 报告 §3、§6、§7.5、§7.8；AWR-03 §4.3、§8.4、ADR-032；AWR-14 §2、§4、§5.4–§5.7、§6.10、§6.17、§7、§11；AWR-15 §4.5；M12 PRD §7.1、§8；M01 PRD §7.1、§7.2、§8.2；M16 PRD M16-FR-006、FR-010、FR-011；请求 M12-to-M15、M01-to-M15、M01-to-M03 第 8 条、M16-to-M15 第 1–3 条 |
| 环境 | 8 核 CPU、无 GPU；Chromium 151（SwiftShader，Tier S）；Node 22；`.venv`。私有后端：`python -m awr.runtime.supervisor --profile demo`（端口 18766、总线 17766、私有 runs 目录、`AWR_WEB_DIST` 指向私有测试构建），深圳 + S1 |
| 约束执行 | 未安装任何依赖；未运行性能基准与 `@perf` 用例；未执行 git 写操作（两轮各有一次误输入的只读 `git status`，因目录不是仓库直接返回，无任何影响）；颜色只用 token；无 emoji 与禁用字形；未引入 lucide-react 与 backdrop-filter；只用 shadcn 组件；续作期间其他工作包同时在跑全量 pytest 与剧本用例（负载均值 8–22），私有构建、私有后端与 Playwright 输出目录均放在 scratchpad 的 `fxweb2/` 下，不与他人共享 |
| 结论 | 五项任务全部完成。Timeline 已接 M12 store 与轨道模型（实时与回放、seek、倍速 0.1–20×、书签、快捷键、Runs 覆盖页与深链，回放悬停按 R68 取事件正文）；重建任务页 `/jobs` 与 `stores/jobs.ts` 已对接 M03 R39–R42、R64 与 M01 R38（进度 ≤ 4 Hz、`scale_status` 徽标），并用真实 job-worker 跑通 Mock 重建到"在沙盘中打开"；诚实标识与报告保真度、页脚槽位已交付；产品显示名统一为"ANet Drone4D"（ADR-056），关于区块含版本、许可摘要、仓库链接、UrbanScene3D 引用与版权行；两种分辨率的截图逐面板复核，第 1 轮修正 17 处、续作再修正 14 处（§1.5、§1.6）。本区域功能测试全部通过（vitest 947/947；Playwright M15/M12 功能 26/26、skeleton 门禁 4/4、e2e 13 例全部通过，其中 timeline 实时用例须在新起的后端上运行，原因是该用例对共享后端席位的顺序依赖；pytest 子集 370/370；负载敏感用例的说明见 §5）；`make lint` 全部通过（第 1 轮阻断的 8 条 ruff 已由其所有者修正） |

## 1 实现清单

### 1.1 Timeline 接 M12 store（任务 1；M12-to-M15 第 1–9 条）

| 路径 | 内容 |
|---|---|
| `ui/lf/LfTimelineTrack.tsx`（重写） | 一张 CPU 画布，由 LfScheduler 按 Tier S 4 Hz 重绘，只在模型版本、视窗、宽度或播放头变化时重绘。按 M12 §8.3 分层：L0 轨道底；L1 日历地板刻度（主、次、微三级，主刻度下方标签间距 ≥ 64 px，`m:ss`、`h:mm:ss`、不足 1 s 带十分位）；L2 选中机高度发丝（每 3 px 一桶，0.55 px）；L3 区间（回滚重跑 45° 斜纹、抽取点、缺口与停滞虚线）与 L6 已预读 2 px 底条；L4 标记按列聚合（生命周期实心点 5 px、航线空心点 5 px、警告红描边三角 7 px、严重红描边八边形 7 px、`heroIdx` 胜出者为全图唯一的 HERO 实心点 7 px、系统 FLOOR 竖线，SUPERSEDED 置灰）；L5 书签以注册表 `tl.bookmark` 的路径绘制；播放头。另有缩略轨道变体：整段范围的密度条码（1–2、3–9、≥ 10 三级灰，含严重事件的列顶端 2 px 红描边短线）、视窗框与播放头短线 |
| `ui/layout/TimelineTrackArea.tsx`（新增） | 缩略轨道 + 详细轨道与 M12 §8.4 的交互：悬停线与提示（该列事件数、类别、相关机体、实时模式下取事件日志中的正文、SIM 时刻）；点选标记（±6 px）选中相关机体，回放时 seek；实时拖动平移视窗；滚轮平移、Ctrl+滚轮以指针为中心缩放（最小跨度 2 s）；双击适配全段；右键菜单（在此处添加书签、适配全段；回放另有"从这里播放""复制该时刻链接"）；覆盖一层透明 shadcn Slider 承担键盘与读屏，回放可拖动（拖动中只显示预览标签，松开 seek），实时 `aria-disabled` 并说明"实时模式不支持回看"；BUFFERING 时播放头处显示 Spinner |
| `ui/layout/TimelineBar.tsx`（重写） | 播放/暂停（Play 与 Pause morph；LIVE 显示锁；ENDED 显示"从头播放"）、回放后退 1 s、单步（STEPPING 时 Spinner）；倍速：实时标准档 ToggleGroup ×0.25–×10（按 `caps.clock.max_speed` 截断），紧凑档与回放用 Select（回放 ×0.1–×20，超过本段 `speed_max` 的档置灰并说明原因）；SIM 读数取 M12 的所见时刻 `tDisplayS`（≤ 4 Hz），STALE 时虚线下划线，HoverCard 给出 `clockFacts()`（SIM、所见时刻、D 全局与焦点、请求与实际倍率、纪元、墙钟）；状态区：受 RTF 限制徽标、索引加载中、录制中（`tl.record`，前景色）、时间状态、LIVE/REPLAY；待确认的动作带描边环 |
| `ui/layout/timelineGuards.ts`（新增） | 传输控件的禁用原因：先取会话守卫（离线、非会话世界；实时另含只读与回放），再取 M12 `timelineGuards()`；`rateLabel` |
| `ui/panels/timeline/TimelinePanel.tsx`（重写） | Dock"时间轴"页：LIVE/REPLAY、运行与段、录制中；缩小、放大、适配全段；回放的上一个与下一个标记；添加书签；"录制列表"或"回到实时"；64 px 详细轨道与缩略轨道；形状图例（CSS 形状与图标，不用字形）；所见时刻、实际/请求倍率、渲染延迟 D、纪元、上次 seek 时延；本段书签表（定位、编辑、删除；空态"按 M 在所见时刻添加"） |
| `ui/panels/timeline/BookmarkEditor.tsx`（新增） | 书签标签编辑 Popover（锚定时间读数，≤ 64 字符，Enter 保存，可删除）；M 键 = 在所见时刻添加并打开编辑（席位持有者写共享书签，viewer 写本地书签，均为 M12 动作） |
| `ui/views/RunsPage.tsx`、`app/routes/runs.tsx`（新增） | `/runs` 录制列表覆盖页（M12 §8.8）：运行、世界、剧本、开始墙钟、时长、段数、大小、最大倍速、兼容性、状态与保留标记；不兼容、已损坏、正在录制的当前运行禁用"回放"并在 Tooltip 说明原因；表内一处红给最新的已损坏行；"回放"先弹确认（AWR-14 §5.4 文案） |
| `ui/views/replayFlow.ts`、`app/routes/replay.tsx`（新增） | 进入回放：先暂停实时（等到 PAUSED，≤ 2.5 s），再 `openReplay`；退出回放；深链 `/world/:id/replay/:run?seg=&t=` 重定向为一次性参数 `?replay=&seg=&t=`，Sandbox 在连接就绪后打开并删除参数 |
| `ui/notify/Banners.tsx` | 回放横幅（运行、段、世界、REPLAY、"只读（命令已禁用）"、"回到实时"）；回放进程停止时显示原因码文案 |
| `ui/actions/builtin.ts` | 传输快捷键改用统一守卫并支持回放：Space、`[`/`]`（按实时或回放档位表跳档）、→/Shift+→/Shift+.（回放映射为 +1 s、+10 s、+1 块）、←/Shift+←/Shift+,（仅回放，实时闪现"实时模式不支持回看"）、PageUp/PageDown（仅回放）、M（书签）、Timeline 获得焦点时 Home/End（回放 seek 段首/段尾，实时适配全段/视窗到末端；Home 仍为一个绑定，焦点不在 Timeline 时重置视角）；命令面板新增"录制列表""回到实时""重建任务" |
| `ui/layout/AppHeader.tsx` | 仿真菜单"进入回放…"（回放中为"回到实时"）与"添加书签"；工具菜单"时间轴""重建任务""录制列表"；帮助菜单"项目仓库"；顶栏时钟改读所见时刻（与 Timeline 读数一致）；覆盖页上面包屑仍显示会话世界 |
| `ui/notify/alarms.ts`、`ui/shell/RtContext.tsx` | `timelineTrack.isAcked` 接告警中心：已确认的严重告警退出 Timeline 的 HERO 仲裁（M12-to-M15 第 9 条）；回放打开、seek、关闭引起的纪元变化不再弹"仿真已从剧本起点重开" |
| `stores/timeline.ts`（M12） | ① 录制状态兜底：recorder 随剧本自动开录时 `rec.started` 早于网关订阅、事件环里没有，改以 `GET /api/runs/{run}` 的 OPEN 段判断"录制中"；② 由其他客户端打开或本页加载前已打开的回放，收到带 `run` 的 playbackState 时载入一次上下文（段与缺口、`.evx` 标记、`.ovw` 序列、书签、适配视窗），此前 viewer 看不到回放标记；③ 页面加载时服务器已在回放而无 playbackState：席位持有者以当前倍率发一次幂等 speed 取回状态；进入回放时清掉实时标记 |

### 1.2 重建任务页（任务 2；D1-AC-22 UI 部分；M01-to-M15、M01-to-M03 第 8 条）

| 路径 | 内容 |
|---|---|
| `stores/jobs.ts`（M03，重写） | `JobState` 取 `rt/enums.json` 的 `JobState.recon` 与 `JobState.world_build`；R39 行映射（snake_case、纳秒字符串、`recon{}`、`error{}`、`resumable`、`attempt`）；`job.state` 与 `job.progress` 事件按任务合批，**每 250 ms 至多一次 store 写入（≤ 4 Hz）**，同一任务的进度只保留最新；同一 attempt 内进度单调不减，重试从头计；终态 SUCCEEDED 置 100% 并带产物世界；`perf_lock` 暂停原因；动作：R38 提交（Idempotency-Key）、R41 取消、R42 重试、R64 日志尾部、R63 引擎列表；R39 为 404 或 503/213 时报告"接口未接线"或"job-worker 未运行"而不是空列表；测试构建暴露 `window.__jobs` |
| `ui/views/JobsPage.tsx`、`app/routes/jobs.tsx`（新增） | `/jobs` 覆盖页（AWR-14 §5.7、M01 §8.2）：表格（任务、引擎、来源与产物世界、状态图标与中文、进度条与百分比、帧数与帧率、约剩余时间、尺度徽标、开始墙钟、耗时、取消/重试/打开世界）；尺度徽标必显：`relative` 为"相对尺度"描边并提示"尺度未知，距离与高度不可用于物理"，GNSS、RTK、LiDAR 为次级徽标；表内一处红给失败行；详情 Sheet：阶段条（已完成灰、当前为该图主角、未开始为地板线）、配准摘要（方法、内点率、RMSE、门禁结论，"需要复核"用文字）、产物世界卡（"模拟数据"与"在沙盘中打开"）、日志尾部（JSON 记录格式化为"时刻 级别 消息 · 键=值"）；新建 Dialog：来源世界、合成航线、帧数、产物世界 id（格式校验）、GNSS 配准开关，124/332 显示为字段错误；只读会话"新建任务"置灰并说明原因 |
| `ui/shell/RtContext.tsx` | `bindJobs(client)`：任务事件随页面连接进入 store |
| `ui/panels/world/WorldPanel.tsx`、`app/query/options.ts` | 读取 `world.json` 的 `dataset`、`tags` 与 `generator.params.recon.engine`；Mock 重建产物世界显示"模拟数据"（M01-to-M15 第 2 条） |

端到端验证（真实后端，非 mock）：在 `/jobs` 新建深圳 Mock 任务（600 帧，GNSS 配准），界面依次显示排队 → 准备 → 分割 → 推理（帧数与约剩余时间）→ 融合 → 地理配准 → 切片 → 打包发布 → 成功，尺度徽标 GNSS，配准摘要 gnss-sim3、内点率 93.2%、RMSE 3.77 m、门禁通过；"在沙盘中打开"加载产物世界 `shenzhen-recon-01`（1.35M 点，静态浏览）。测试产生的产物世界与锁文件已从共享 `worlds/` 删除（见 §7 第 9 条）。

### 1.3 诚实标识与报告槽位（任务 3；M16-FR-006、FR-010、FR-011；M16-to-M15 第 1–3 条）

| 路径 | 内容 |
|---|---|
| `ui/views/Report.tsx` | 报告页新增保真度声明区块 `[data-report-fidelity]`（后端、机型与参数状态、仿真值、设备能力档、强制档位）与数据来源页脚 `[data-report-footer]`（数据名、引用、版本、"科研用途"、示意锚点、仿真后端与机型、识别行）；数据取自与 `report.json` 同目录的 `report.meta.json`，`/reports/:rid` 或缺少 meta 时取报告首个世界的 `world.json.dataset`，再缺失时用文档规定的缺省文字；render.mjs 检测到这两个区块后不再注入 |
| `perf/report/render.mjs`（M16） | 静态渲染时同时路由 `/report.meta.json`，报告视图可以读到 meta |
| `ui/panels/drone-detail/DroneDetailPanel.tsx` | 遥测下方写明"以上为仿真值（simulated），不是实测数据"（`[data-honesty="simulated"]`） |
| `ui/layout/ViewportOverlay.tsx` | `?source=fake` 时常驻"合成数据（FakeSource，非仿真后端）"徽标；连接进入 DEGRADED 时显示"信号延迟"徽标 `[data-testid="signal-delay-badge"]`（M16-to-M15 第 3 条） |
| `ui/panels/world/WorldPanel.tsx` | 世界卡片增加数据来源行"数据 UrbanScene3D v0.0.1 · 科研用途"（`world.json.dataset`，悬停显示完整引用） |
| `ui/views/AboutDialog.tsx` | 关于中写出数据来源、引用、科研用途与保真度声明（见 1.4） |

### 1.4 产品显示名 ANet Drone4D 与关于区块（任务 4；ADR-056）

- 页面标题：`index.html` 为"ANet Drone4D"，运行时为"<视图> · ANet Drone4D"（Sandbox 为世界 id，覆盖页为其名称）；启动遮罩文字"ANet Drone4D · World Runtime"。
- 顶栏锁定组合：头像 24 px + "ANet Drone4D" + 分隔线 + "World Runtime"（紧凑档省略副名）；头像与徽章资产未动，check-brand 通过。
- 关于区块（帮助 › 关于、设置 › 关于共用 `AboutContent`）：完整徽章 320 px（原样）、产品名与副名、版本、许可摘要（ANet Open Source License，Apache 2.0 修改版；多租户托管需书面授权；界面标志与版权不得移除；第三方见 THIRD_PARTY_NOTICES.md）、仓库链接 `github.com/ANetResearch/ANet-Drone4D`、数据（UrbanScene3D，仅限非商业科研用途，不随仓库分发）与引用（Lin et al., ECCV 2022）、保真度、应用与 contracts 版本、会话（世界、运行、contentVersion）、版权行"Copyright (c) 2026 Agent Network Research（anet0.com）"。帮助菜单新增"项目仓库"。版权行写"(c)"：U+00A9 属 Extended_Pictographic，会被 D1-AC-20 拦截。

### 1.5 UI 精修（任务 5）

截图条件：私有生产式测试构建，深圳 + S1 运行中（T+1:03 至 T+4:36），Chromium SwiftShader；打开左右栏、Dock 各页签、单机详情、关于、快捷键、命令面板、重建任务、录制列表、回放、只读与断线状态。复核后修正：

| # | 问题（修正前） | 修正 |
|---|---|---|
| 1 | 机体 3D 标签显示 i18n 键"fs.short.69"（状态字节含子模式位） | 标签格式化器按 `fs & 0x1f` 取 FlightState（`RtContext.tsx`），并请 M06 传入解码值 |
| 2 | 点击 DroneRail 行后整个应用壳上移 6 px（`overflow: hidden` 的根容器被 scrollIntoView 滚动） | `.app-root` 改为 `overflow: clip`（`styles/layout.css`） |
| 3 | 视口右下角常驻一个只有"—"的坐标卡 | 未拾取地面点时不渲染坐标卡 |
| 4 | 世界卡片"示意坐标"徽标被截断，名称与 id 重复 | 标题行名称截断、徽标不收缩；id 与规模并入第二行；新增数据来源行 |
| 5 | 图层"点 25.0K / 预算 25.0K · soft-min"折成两行 | 拆为"点 / 预算 + 刻度条"与"档位 · 受限原因"两行 |
| 6 | 环境"背景能见度（不含降水）MOR 30.0 km 30.0 km"标签行折行 | 附加事实移到滑块下方；文案改为"背景能见度"与"含降水的总能见度 MOR" |
| 7 | 事件表消息显示原始类型（coverage.progress、sim.rtf_limited 等） | 为契约 known_kinds 中其余约 80 种事件补中文文案；时间列改为"SIM 时刻" |
| 8 | 任务表"机体"列只显示数量 1，"预计 (s)"显示原始秒数 | 显示机体 id（超过 2 架为"a, b 等 N 架"）；ETA 用 `fmt.dur`（11:02）；列名"预计剩余" |
| 9 | 只读会话的任务面板仍显示三个置灰按钮 | 按 AWR-14 §7.8 改为一处说明（"只读模式"或断线原因） |
| 10 | 单机详情命令区为 5 个无文字图标 | 起飞、悬停、降落、返航一行带文字的按钮组，GoTo 与"更多"一行 |
| 11 | 选择被清空（Esc、纪元变化、机体移除）后右栏停在空的详情页 | 自动回到列表页 |
| 12 | Timeline 条只有一条空轨道与一个像"°"的空心点，Slider 拇指常驻右端 | 见 1.1：缩略与详细轨道、刻度标签、形状编码标记、悬停提示；实时模式不显示 Slider 拇指 |
| 13 | 回放打开与 seek 引起的纪元变化弹"仿真已从剧本起点重开" | 回放相关的纪元变化不提示 |
| 14 | 覆盖页上面包屑显示"未选择世界" | 显示会话世界 |
| 15 | 性能降级 Toast 与 HUD 显示"降级第 5 步"（环境旋钮的文案键 `env.degraded` 缺失） | 补文案"环境视觉降级（雾保留，物理不受影响）" |
| 16 | 录制中没有任何指示（`rec.started` 丢失） | 见 1.1 `stores/timeline.ts` ①，Timeline 条与时间轴页显示"录制中" |
| 17 | 深圳内置世界曾误显示"模拟数据"（`tags` 含 synthetic 即判定） | 只在 `generator.params.recon.engine = mock` 或 tags 同时含 recon 与 synthetic 时显示 |

逐项核对（两种分辨率）：告警一处红（HUD KPI 为红色文字不占实心名额；Timeline 只有 HERO 一个实心红；顶栏无未确认 critical 时无红）；颜色全部来自 token；图标来自 morphicons 注册表；空状态（任务、录制、书签、重建任务）、加载（Skeleton）、断线横幅与只读说明均已出现并截图；页面扫描 emoji 与禁用字形为 0（Playwright 用例断言）。

### 1.6 续作（2026-10-01）：遗留补齐与第二遍精修

续作开始时先复核第 1 轮交付在其他工作包改动之后的状态：`tsc` 0 错误、vitest 全部通过；`make lint` 只剩其他工作包的 8 条 ruff（续作结束时已由其所有者修正，见 §5）。随后用私有生产式测试构建与私有 demo 后端（深圳 + S1）重新截图，逐面板复核，修正下表各项。

| # | 问题（修正前） | 修正 | 文件（相对 `apps/web/`） |
|---|---|---|---|
| 18 | 回放中悬停 Timeline 标记只显示类别，没有事件正文（第 1 轮 §3 的偏差） | 指针在标记上停留 150 ms 后按 R68 请求该标记所在像素列的时间窗（`limit=20`），正文按 `mseq` 缓存（同一运行与段内有效，换运行或段即清空；同一窗口失败不重试）；取回后提示框就地改为"类别 · 正文"。实测：`航线变更 · 任务状态变化`、`警告 · p600-02 进入返航中 · 爬升` | `src/ui/layout/replayEventText.ts`（新增）、`src/ui/layout/TimelineTrackArea.tsx` |
| 19 | 悬停文字重复机体名（"类别 · p600-01 · p600-01 …"） | 有正文时不再另写机体（正文已含） | `src/ui/layout/TimelineTrackArea.tsx` |
| 20 | 实时模式播放头钉在右端（M12-FR-017），绘制在画布外第 `width` 列，详细轨道与缩略轨道上都看不见 | 播放头列夹在 `[0, width − 1]` 内（`pxCol`） | `src/ui/lf/LfTimelineTrack.tsx` |
| 21 | lieflat 表格的数值列：单元格右对齐、表头仍左对齐（`th` 的 `text-left` 工具类压过 `lf.css` 的 `[data-num]`），任务、录制列表、重建任务、性能等表的表头与数值错位约 100–300 px | 数值列表头加 `text-right` | `src/ui/lf/LfTable.tsx` |
| 22 | KPI 写成"92 %"；缺失值写成"— ms" | `%` 与 `°` 紧贴数字（AWR-14 §13.3 第 1 条）；值缺失时不显示单位 | `src/ui/lf/LfStat.tsx` |
| 23 | 面包屑"运行"不可点击，回放中仍显示实时会话的运行 id，与回放横幅不一致；"世界""运行""焦点机"三段字号不一 | "运行"显示正在查看的运行（回放中为被回放的录制），点击打开录制列表；焦点机点击聚焦相机（AWR-14 §2.3）；三段统一为 xs 等宽 | `src/ui/layout/AppHeader.tsx` |
| 24 | 服务器已在回放时刷新回放深链：前端再发 `open`，网关回 105，横幅误报"回放进程已停止" | 深链指向正在回放的同一录制与段时只 seek，指向另一录制时先 `close` 再 `open`；网关拒绝本页命令（应答带本页 `request_id`、原因码不是 213）时不改回放状态，只 Toast 原因 | `src/ui/views/replayFlow.ts`、`src/stores/timeline.ts`（M12） |
| 25 | 回放中单机详情的命令区是一排置灰按钮；只读 viewer 断线后由"只读说明"变成一排置灰按钮 | 回放中替换为一处说明"回放中不可下发命令"（席位持有者附"回到实时"，AWR-14 §5.4"右栏命令区替换为只读说明"）；只读会话断线时保留只读说明（隐藏"申请控制"）；持席操作员断线仍为置灰按钮组，避免短暂重连时面板跳动 | `src/ui/panels/drone-detail/DroneDetailPanel.tsx` |
| 26 | 事件与 Toast 中子状态为英文（"p600-02 进入返航中 · CRUISE"） | 为契约 `FlightSub` 的 38 个子状态补中文（巡航、爬升、下降、进近、触地、安全停止等），未知名称仍净化后原样显示 | `src/ui/notify/severity.ts`（`subStateText`）、`src/app/i18n/{zh-CN,en}.json` |
| 27 | World Hub 卡片标签为硬编码英文"POINTS、NODES、MAX H、TTFP"与"LEVELS · 1 RUNG = 200K 点" | 改为 i18n："点数、节点、最高、上次首屏"与"各层级点数 · 每格 200K 点"；未打开过的世界首屏显示"—"（不带 ms） | `src/ui/views/WorldHub.tsx`、`src/app/i18n/{zh-CN,en}.json` |
| 28 | INT-1 §7.4 建议为模态探针补单元用例 | 导出默认探针 `modalOpenInDom`，新增浏览器用例：打开的对话框、警告对话框、菜单计为模态；带 `data-closed`、`data-ending-style` 的退出过渡与非模态弹层不计 | `src/ui/hotkeys/registry.ts`、`tests/m15/modal-probe.browser.test.ts`（新增） |
| 29 | `motion-dom.browser.test.tsx` 的 SwapText 用固定 600 ms 等待，全量并行时偶发失败（第 1 轮 §8 第 9 条、INT-1 §7.11） | 改为 `vi.waitFor` 轮询（≤ 5 s） | `tests/m15/motion-dom.browser.test.tsx` |
| 30 | `tests/time/store.test.ts`"1 s 未确认即回退"在负载均值 ≈ 20 时失败（`t0` 取在 `play()` 之前，二者间隔超过 1 ms 时 `t0 + 1001` 仍未到期） | `t0` 改在 `play()` 之后读取 | `tests/time/store.test.ts`（M12 前端用例） |
| 31 | `perf/m15/fxweb2.spec.ts` 注释把产品名 ADR 写成 ADR-054（该号已被 FX-WEB1 用于点径上限） | 改为 ADR-056 | `perf/m15/fxweb2.spec.ts` |

另：网关已在 hello 之后补发 playbackState（FX-GW 实现 FX-WEB2-to-M11 第 1 条），`stores/timeline.ts` 的 `syncReplayState` 只作丢包兜底，注释同步更新；FastAPI 标题已改为"ANet Drone4D World Runtime API"（同一请求第 3 条）。

新增与修改的单元用例（`tests/m15/fxweb2.test.ts`，共 24 例）：R68 请求窗口与 `limit`、按运行与段缓存与失效、回放悬停先类别后正文且机体只出现一次、实时模式不请求 R68、播放头列夹取、子状态中文。

## 2 验收对照（本区域）

| 编号 | 结论 | 证据 |
|---|---|---|
| D1-AC-22（UI 部分：进度 ≤ 4 Hz、`scale_status`） | 通过 | `perf/m15/fxweb2.spec.ts`：60 条 `job.progress` 在 1.5 s 内只引起 2–8 次 store 写入，行进度到 83%，`relative` 徽标文案与提示；`tests/m15/fxweb2.test.ts`：40 条事件 1 s 内 ≤ 5 次写入、进度单调；真实 job-worker 端到端（1.2 节，截图 `fx-web2-ui-jobs*.png` 与过程截图） |
| D1-AC-18（录制与回放，UI 功能部分） | 通过（功能） | `tests/e2e/timeline.spec.ts` 回放用例（深链 `?seg=0&t=420`、25 × Shift+. = 1.00 s、→/←、书签与 PageDown、M、kill replay-worker 后 1 s 内"回放已停止"）在本构建下通过；续作另用真实录制（S1 实时录制 8–14 min）验证深链、深链重入、R68 悬停正文、回放中命令区说明与面包屑（截图 `fx-web2-replay-*`）；seek ≤ 500 ms 属性能子项，未测 |
| D1-AC-20（设计体系） | 通过（前端部分） | `make lint` 的 oxlint 与全部 tools/lint 检查通过；`brand.spec.ts`、`sanitize.spec.ts`、`motion.spec.ts` 通过；新增页面 glyphScan 为 0 |
| D1-AC-21（可用性） | 通过 | `a11y.spec.ts` 通过；`tests/m15/hotkeys.test.ts` 通过（每个 D1-core 键只登记一次，Home 合并为一个绑定） |
| D1-AC-32（交互） | 通过 | `tests/e2e/interaction.spec.ts`（free-shenzhen 独立后端）1/1 |
| D1-AC-34（集成门禁） | 通过 | `perf/skeleton.spec.ts` 4/4（`AWR_PERF_DIST` 指向本构建；续作复跑 4/4） |
| M16-FR-006（诚实标识） | 通过 | `honesty.spec.ts` 4/4（新增"关于写明数据来源、科研用途、许可与版权"与仿真值标注的断言） |
| M16-FR-010、FR-011（报告页脚、保真度） | 通过 | `perf/m15/fxweb2.spec.ts` 报告用例；`tests/m15/fxweb2.test.ts` 的 `fidelityRows`、`footerLines` |
| M12-AC-008、014、017、019、021–028（DOM 部分） | 功能通过 | Timeline 条、轨道悬停与右键、时间轴页（`fxweb2.spec.ts`）；回放控件与快捷键（上面的 timeline 回放用例）；回放悬停正文（M12 §8.4，R68）见 §1.6 第 18 条与 `tests/m15/fxweb2.test.ts` |

## 3 与规格的偏差及理由

| 偏差 | 理由与影响 |
|---|---|
| 播放头画在轨道画布内，随 store 频率（Tier S 4 Hz）移动，没有做成每帧只写 transform 的 DOM 元素（M12 §8.3 DOM 行） | 48 px 条上 1 s 对应数个像素，4 Hz 的移动肉眼连续；避免为此新增常驻 rAF 任务（D1-AC-23 的 UI 开销）。回放拖动时预览标签 1:1 跟随 |
| 缩略轨道拖动视窗框实现为"点击或拖动时视窗居中到指针处" | 行为等价（平移），实现更简单 |
| 持席操作员断线时，单机详情命令区仍为置灰按钮组（只读与回放已改为一处说明） | AWR-14 §7.1 的断线列对 DroneRail 只要求 STALE 样式；断线常为数秒的重连，换成说明会让面板来回跳动 |

## 4 规格变更（已同步设计文档）

| 变更 | 文档 |
|---|---|
| 产品显示名统一为 ANet Drone4D，关于区块内容与版权行、页面标题规则、帮助菜单"项目仓库" | AWR-03 附录 E **ADR-056**（§7.0 索引登记；ADR-032 锁定组合文字加注以 ADR-056 为准）；13 §2.6、产品名表与定位陈述、功能树根节点；14 §2.2（页面标题）、§2.3（帮助菜单）、§3.2 线框、§4.1、§5.6 关于行、UX-FR-009；15 §4.5；M15-FR-110；docs/README 标题与首段 |
| 回放深链的一次性参数 `replay`、`seg`、`t`（`/world/:id/replay/:run` 重定向到 Sandbox） | 14 §2.2 URL 参数表 |
| 回放"中途加入"的客户端行为 | 14 §5.4 新增一行；续作改写：网关 hello 后补发 playbackState（M11 已实现），speed 只作兜底；深链指向正在回放的同一录制时只 seek，另一录制先 close 再 open；被拒命令（本页 `request_id`、原因码不是 213）不改回放状态、横幅不报"回放进程已停止" |
| 回放中面包屑"运行"显示被回放的录制并可点击打开 Runs；Timeline 回放悬停 150 ms 后经 R68 取正文 | 14 §5.4 新增"面包屑与悬停"一行（续作）；后者与 M12 §8.4 一致，不改 M12 |

续作的其余修正（表头对齐、`%` 紧贴、子状态中文、World Hub 标签、播放头可见）都是按既有规格修实现，不改变阈值、参数或协议，因此没有新增 ADR。

ADR 编号说明：第 1 轮时 ADR-054 已被另一工作包在代码中引用而尚未写入 03，FX-GW 已写入 ADR-055，本包取 **ADR-056**，写在附录 E 中 ADR-055 之后。续作核对：03 现有 ADR-054（FX-SIM1，能量 RTL 绕行返航）、055、056、057–059、061、062，本包的 ADR-056 未与他人撞号；另注意 FX-WEB1 的代码注释也以"ADR-054"指点径上限，与 03 中的 ADR-054 不是同一条，请 FX-WEB1 核对编号（本包测试注释中误写的 ADR-054 已改为 ADR-056）。

## 5 测试结果

续作（2026-10-01）的最终结果；第 1 轮结果见表后。全部在私有测试构建（`VITE_AWR_TEST_SWITCHES=1`，输出到 scratchpad）上运行，Playwright 输出目录用 `--output` 指向 scratchpad，未触碰共享的 `runs/playwright`。

| 类别 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `npx tsc -p tsconfig.json --noEmit` | 0 错误 |
| lint | `make lint` | 05:25 一次完整通过（rc 0）：ruff "All checks passed"（第 1 轮阻断的 8 条已由其所有者修正）、oxlint `--type-aware` 0、no-emoji、no-hex、lint-lf、motion-lint、check-icons、no-raw-controls、check-brand、check-deps、check-units、check_py_imports、check_py_callbacks、check-perf-flags、check-thresholds、m06-lint、各生成物 up to date、check-cn-keys、registry 全部通过。05:55 写报告前复跑：本包相关检查仍全部通过，唯一失败是 no-emoji 的 2 条 GLYPH-01（U+21D4 双向箭头，注释中改写为文字即可），位于 `python/awr/sim/core/command.py` 第 1248、1424 行，该文件 05:42 刚被其他工作包修改（本包未改动），留给其所有者修正（§8 第 10 条） |
| Vitest | `npx vitest run --project unit --project browser` | 122 个文件通过、1 跳过；947 通过、1 跳过、0 失败。其中 `tests/m15/fxweb2.test.ts` 24/24、`tests/m15/modal-probe.browser.test.ts` 3/3（新增）。负载均值约 20 时曾有 1 例失败（`tests/time/store.test.ts` 的 1 s 回退，见 §1.6 第 30 条），修正后单独 3/3、全量通过 |
| Playwright（M15 UI，FakeSource） | `M15_DIST=<私有构建> AWR_PORT_OFFSET=23 npx playwright test perf/m15/{fxweb2,interaction,layout-probe,report,responsive,smoke}.spec.ts --project perf` | 25/25 通过 |
| Playwright（M12 冒烟） | `M11_DIST=<私有构建> npx playwright test perf/m12/smoke.spec.ts --project perf` | 1/1 通过。负载均值约 20 时曾失败一次（1.5 s 内 SwiftShader 未出新帧，`frames` 未增加），负载降到约 8 后复跑通过；属负载敏感，非本包改动引起 |
| Playwright（门禁） | `AWR_PERF_DIST=<私有构建> npx playwright test perf/skeleton.spec.ts --project perf` | 4/4 通过 |
| Playwright（e2e，私有 demo 后端） | `AWR_PERF_BASE=<私有后端> npx playwright test a11y brand honesty motion sanitize timeline --project e2e` | 12 例中 11 例通过。`timeline.spec.ts` 的实时用例在同一批次中排在其他用例之后失败：前面的用例用其他 principal 持有过席位，席位宽限期内本用例的 principal 是只读（已用探针脚本确认页面显示"只读"），Space 被拒；在新起的后端上单独运行 `timeline.spec.ts` 3/3 通过（实时、viewer、回放）。属用例对共享后端的顺序依赖（该文件由其他工作包维护，见 §8） |
| Playwright（e2e，独立后端） | `AWR_WEB_DIST=<私有构建> npx playwright test interaction --project e2e`（free-shenzhen） | 1/1 通过。负载均值约 15 时曾失败一次（添加 P600 后 2.02 s 才出现，断言 ≤ 1.5 s），负载下降后复跑通过 |
| pytest | `pytest -m "not perf" tests/m15 tests/recorder tests/jobs tests/reconstruction tests/rt/test_rest_contract.py tests/e2e/test_builtin_worlds.py tests/e2e/test_fake_source.py` | 370 通过、2 未选（perf）。本包两轮均未修改 Python 代码；R68、runs、jobs、录制回放与世界服务均在该子集内 |

第 1 轮（2026-09-29）：tsc 0；`make lint` 前端全部通过、ruff 8 条（其他工作包）；vitest 925 通过、1 失败（SwapText 固定等待，续作已修）；Playwright M15 18/18、e2e 13/13、skeleton 4/4；pytest 子集 103 通过。

## 6 截图

`.cache/impl/shots/`，续作重拍（1920 × 1080 与 1280 × 720 各一套，私有 demo 后端，深圳 + S1 运行中；拍摄时机器负载均值 8–22，所以 HUD 呈现间隔为红色数值、偶见"信号延迟 STALE"标签与 SIM 读数虚线下划线，均为规定的降级与过期表现）：

- 运行中：`fx-web2-main-*`、`fx-web2-dock-0-*`（事件）、`-dock-1-*`（图表）、`-dock-2-*`（性能）、`-dock-3-*`（任务）、`-dock-4-*`（时间轴）、`-dock-close-*`、`fx-web2-detail-*`；
- 覆盖页与对话框：`fx-web2-worlds-*`、`fx-web2-jobs-*`、`fx-web2-runs-*`、`fx-web2-about-*`、`fx-web2-help-*`、`fx-web2-palette-*`、`fx-web2-settings-general-*`、`fx-web2-settings-about-*`；
- 回放（真实录制）：`fx-web2-replay-*`、`fx-web2-replay-top-*`（横幅与面包屑）、`fx-web2-replay-bar-*`、`fx-web2-replay-dock-*`（时间轴页）、`fx-web2-replay-hover-*`（R68 正文）、`fx-web2-replay-hover-warning-1920x1080`、`fx-web2-replay-detail-1920x1080`（命令区说明）；
- 只读与断线：`fx-web2-viewer-readonly-*`、`fx-web2-viewer-detail-*`、`fx-web2-offline-*`（只读会话断线：重连横幅、离线徽标、只读说明保留）；
- `fx-web2-ui-*`（FakeSource 功能用例，续作重跑生成）：`about`、`timeline-hover`、`timeline-tab`、`jobs`、`jobs-sheet`、`runs-confirm`、`report-footer`。

第 1 轮的 `fx-web2-after-replay-1280x720` 已过时，已删除。

## 7 改动文件清单

路径相对 `apps/web/`，除非另注。

| 文件 | 所有者 | 说明 |
|---|---|---|
| `index.html` | M15 | 标题与启动遮罩文字 |
| `src/app/App.tsx` | M15 | 页面标题（另一工作包同时在此文件加了 `CanvasOnly`，本包只在 `RouterOutlet` 中加一个 effect） |
| `src/app/router/table.ts` | M15 | 已交付 ext 路由 runs、replay、jobs |
| `src/app/routes/{runs,replay,jobs}.tsx` | M15 | 新增路由 |
| `src/app/query/{options,keys}.ts` | M15 | `worldDatasetQuery` |
| `src/app/i18n/{zh-CN,en}.json` | M15 | 约 260 个新键（en 为空串回退），`brand.product`、`detail.alt`、`layers.budget`、`env.morBg`、`env.morTotal`、`events.col.time`、`mission.col.eta` 改文案 |
| `src/lib/format.ts` | M15 | `fmt.dur` |
| `src/styles/layout.css` | M15 | `.app-root` overflow clip |
| `src/stores/jobs.ts` | M03 | 重写（1.2） |
| `src/stores/timeline.ts` | M12 | 录制状态兜底、回放上下文载入、回放状态同步（1.1） |
| `src/ui/lf/LfTimelineTrack.tsx` | M15 | 重写 |
| `src/ui/layout/{TimelineBar,TimelineTrackArea,timelineGuards,AppHeader,ViewportOverlay}.tsx/ts` | M15 | 1.1、1.3、1.5 |
| `src/ui/panels/timeline/{TimelinePanel,BookmarkEditor}.tsx` | M15 | 1.1 |
| `src/ui/panels/{world/WorldPanel,drone-detail/DroneDetailPanel,mission/MissionPanel,layers/LayersPanel,env/EnvPanel,settings/SettingsPanel}.tsx` | M15 | 1.3、1.5 |
| `src/ui/notify/{Banners.tsx,alarms.ts}` | M15 | 回放横幅；`isSeqAcked` |
| `src/ui/shell/RtContext.tsx` | M15 | `bindJobs`、`isAcked`、纪元提示、标签状态位 |
| `src/ui/actions/builtin.ts` | M15 | 快捷键与命令面板条目 |
| `src/ui/views/{AboutDialog,JobsPage,RunsPage,Report,Sandbox}.tsx`、`src/ui/views/replayFlow.ts` | M15 | 1.1–1.4 |
| `src/ui/brand/BrandLockup.tsx` | M15 | 注释（产品名） |
| `tests/m15/fxweb2.test.ts`（新增）、`tests/m15/i18n.test.ts` | M15 | 单元用例；品牌名断言 |
| `perf/m15/fxweb2.spec.ts`（新增） | M15 | Playwright 功能用例 |
| `perf/report/render.mjs` | M16 | 路由 `/report.meta.json` |
| `tests/e2e/honesty.spec.ts`（仓库根） | M16 | 新增关于用例与仿真值断言 |
| **续作新增或修改** | | |
| `src/ui/layout/replayEventText.ts`（新增）、`src/ui/layout/TimelineTrackArea.tsx` | M15 | R68 回放悬停正文；悬停文字去重 |
| `src/ui/lf/LfTimelineTrack.tsx`、`src/ui/lf/LfTable.tsx`、`src/ui/lf/LfStat.tsx` | M15 | 播放头列夹取；数值列表头右对齐；`%`/`°` 紧贴与缺失值不带单位 |
| `src/ui/layout/AppHeader.tsx` | M15 | 面包屑：运行与焦点机可点击、回放中显示被回放录制、字号统一 |
| `src/ui/views/replayFlow.ts`、`src/stores/timeline.ts` | M15、M12 | 深链重入只 seek；被拒命令不改回放状态；`syncReplayState` 注释 |
| `src/ui/panels/drone-detail/DroneDetailPanel.tsx` | M15 | 回放与只读（含断线）命令区一处说明 |
| `src/ui/notify/severity.ts`、`src/app/i18n/{zh-CN,en}.json` | M15 | `subStateText` 与 38 个子状态文案；World Hub 4 个标签与来源行 |
| `src/ui/views/WorldHub.tsx` | M15 | 卡片标签 i18n |
| `src/ui/hotkeys/registry.ts` | M15 | 导出默认模态探针 `modalOpenInDom` |
| `tests/m15/fxweb2.test.ts`、`tests/m15/modal-probe.browser.test.ts`（新增）、`tests/m15/motion-dom.browser.test.tsx`、`tests/time/store.test.ts` | M15、M12 | 新用例；两处负载敏感等待改为条件或正确取时 |
| `perf/m15/fxweb2.spec.ts` | M15 | 注释 ADR 编号 |
| `docs/14-UI交互设计PRD.md`（仓库根） | 文档 | §5.4"中途加入"改写、新增"面包屑与悬停" |
| `docs/03-设计基线与决策记录.md`、`docs/13-产品设计PRD.md`、`docs/14-UI交互设计PRD.md`、`docs/15-视觉设计规范与色卡.md`、`docs/modules/M15-前端UI壳与设计体系组件PRD.md`、`docs/README.md`（仓库根） | 文档 | §4 |
| `.cache/impl/requests/FX-WEB2-to-{M11,M06-M12,M10}.md`（仓库根） | — | 跨模块请求 |

## 8 遗留问题与请求

1. **M11**（`FX-WEB2-to-M11.md`）：第 1 条（hello 后补发 playbackState）与第 3 条（FastAPI 标题）已由 FX-GW 完成；第 2 条（recorder 自动开录时 `rec.started` 早于网关订阅）续作未再核对，前端仍以 runs meta 的 OPEN 段兜底，"录制中"指示正常。
2. **M06**（`FX-WEB2-to-M06-M12.md`）：续作的暂停与回放暂停截图中机体标签不再显示 STALE；新观察到断线 17 s 后机体标签仍写"STALE 1.0 S"，而顶栏读数为"STALE 16.6 S"（标签年龄随渲染时钟冻结），建议标签与顶栏用同一年龄口径。Mock 重建世界下 ViewCube 被顶栏遮挡一项续作未复核。
3. **M10**（`FX-WEB2-to-M10.md`）：S1 任务进度已随扫描更新（续作截图 15%、77%、95%）；"预计剩余"仍停在开局值（11:02、8:03），请 M10 随进度更新 `eta_s`。
4. **M16（`tests/e2e/timeline.spec.ts`）**：实时用例依赖席位，与其他 e2e 用例共用同一后端并排在其后时，会落在前一个 principal 的席位宽限期内而只读，Space 被拒（§5）。建议用例先等 `[data-role]` 为操作员（或发"申请控制"）再按 Space。
5. **负载敏感用例**：`perf/m12/smoke.spec.ts`（1.5 s 内要求出新帧）、`tests/e2e/interaction.spec.ts`（添加 P600 ≤ 1.5 s）在负载均值 15–20 时各失败过一次，负载下降后通过；判定应放到验收阶段的排他锁下（AWR-18 运行协议）。
6. **M07、M11**：回放时出现状态横幅"环境预设与 api 加载的 presets.json 不一致"（第 1 轮观察，服务端 status 项），续作的回放截图中未再出现。
7. **M01**：`perf/m01/recon.spec.ts` 可按 M01-to-M15 第 4 条补"提交 → 进度 → GNSS 徽标 → 在沙盘中打开"的断言；UI 侧已由 `perf/m15/fxweb2.spec.ts` 与第 1 轮真实链路手工验证覆盖。
8. **测试环境**：UI 中提交的 Mock 重建会把产物世界写入 `AWR_WORLDS_DIR`（缺省为共享的 `worlds/`）；并行或测试环境请把 `AWR_WORLDS_DIR` 指向临时目录。续作没有提交重建任务。
9. **规格待议（不阻塞）**：AWR-14 §7.1 没有规定持席操作员断线时单机详情命令区的呈现，本包保留置灰按钮组（§3）；如需统一为说明，请在 14 §7.1 明确。
10. **lint（其他工作包）**：`python/awr/sim/core/command.py` 第 1248、1424 行注释含 U+21D4（GLYPH-01），使 `make lint` 的 no-emoji 失败；该文件在续作期间被其他工作包修改，本包未越区改动，请其所有者把该双向箭头改为"当且仅当"。

## 9 依赖与安装

没有安装或升级任何依赖，没有修改 `package.json`、`package-lock.json`、`pyproject.toml`、`requirements.lock`。
