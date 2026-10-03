# FINAL-REVIEW 发布前对抗式终审报告

| 项 | 内容 |
|---|---|
| 工作包 | FINAL-REVIEW：发布前对抗式终审与交付总结。①运行完整门禁并修复残留问题；②用户硬约束 R2、R3 逐条核对（附证据）并直接修复违规；③发布合规（入库范围、凭据、单文件大小、README 链接与命令）；④写 [D1 交付总结](D1-交付总结.md) |
| 日期 | 2026-10-03（20:00 至 21:20） |
| 依据 | AWR-03 §1.3、§2.4（R1–R4 追溯矩阵）、§8.4（验收表与 D1-AC-20 注）、ADR-028 至 ADR-034、ADR-077、ADR-078；AWR-15 §3、§4、§7–§9；AWR-18 §9、§12、§13；D1 验收报告第 3、5 轮（§3 逐条结果、§4 诊断、§5 冻结建议）；FX2-R3、FX2-R5 各报告；DEMO-W、SHOW-CI、SHOW-M、README-V2 报告 |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4 1.7 GHz）、62 GiB、无 GPU；Node 22.12.0；Python 3.12.3（`.venv`）；Chrome for Testing 151（SwiftShader）；六城与 synthcity 已生成；本阶段没有其他工作包在跑（开始时 load 0.2） |
| 约束执行 | 没有安装依赖（`package.json`、锁文件未动；演示动图重新编码用 `.cache/publish/venv` 中已有的 Pillow 12.3）；没有执行 git 写操作（只用 `ls-files`、`status`、`log`、`grep`、`rev-list`）；构建与测试持共享锁；Playwright 用例逐个经 harness 持排他锁，每次远小于 10 分钟（§2）；没有放宽任何 P0 阈值；`make lint` 通过 |
| 规格变更 | ADR-079（发布面合规）、ADR-080（D1-AC-09a 最大间隔与 D1-AC-26 四个子项按原值冻结）；同步 AWR-03 §7.0 索引、§8.4、AWR-18 §13.1、§13.2、PERF-AC-012、PERF-AC-040，AWR-15 §9.6，M15-FR-045，M06-AC-010，`thresholds.json`，文档地图与 README |

## 1 结论摘要

| 任务 | 结果 |
|---|---|
| 1 完整门禁 | 全部通过：`make lint`、`make test-contracts`（335 + 225）、`pytest -m "not perf"` 全量（2,711 通过、11 跳过、0 失败，47 min）、Vitest unit 与 browser（1,000 通过、1 跳过）及两个附加 node 配置（12 通过）、`npm run build`（`make build`，含生产包扫描）、9 个 Playwright 功能用例（§2.2）、`worldpkg validate worlds/* --deep`（7 个世界 0 错误 0 警告，82 个 JSON 通过 Ajv strict）。门禁之外发现并修复 1 处回归：生产包残留测试开关代码块（M06-AC-010，§4.4） |
| 2 用户硬约束 | R2 六项、R3 六项逐条核对（§3）。修复：数据表不在 table.log 皮肤作用域（3 张表）、5 处浏览器原生 `title` 提示改为 shadcn Tooltip 或去掉、浅色主题下 transitions.dev 配方的原色值、发布面文档不在 emoji 扫描范围。没有发现 lucide-react、backdrop-filter、token 外颜色、emoji、第三方组件库、非 morphicons 图标或自建浮层 |
| 3 发布合规 | 修复 1 处：`docs/media/demo-flight.webp` 6,037,146 字节超过 5 MB，按同一母版以 quality 72 重新编码为 4,630,188 字节。入库范围 2,301 个文件不含 UrbanScene3D 点数据、World Package 与其渲染画面；`~/.config/awr/gravitex.env` 的 3 个取值在发布集合与仓库全部 20 个提交中 0 次出现；README 两版 348 处相对链接与锚点全部有效，23 个外部链接中 22 个可达（CI 徽章 404，原因是工作流尚未提交），make 目标全部存在（§5） |
| 4 交付总结 | [D1-交付总结.md](D1-交付总结.md)：实现范围、架构要点、D1-AC 最终通过情况（38 条通过 35；含 P0 的 25 条通过 23；G4 未满足）、已知问题与路线图、运行与测试、文档索引、研究与设计过程 |
| 规格 | ADR-079（发布集合检查 REL-01 至 03 并入 `make lint`、发布面文档纳入 emoji 扫描、表格作用域、配方颜色、生产包扫描并入 `make build`）；ADR-080（D1-AC-09a 最大间隔、D1-AC-26 命令到可见、关注集切换、×10 HOLD、选中机通道频率按原值冻结）。没有放宽任何阈值 |

D1 的发布结论不变：第 5 轮的 2 条 P0 不通过项（D1-AC-03a 纽约最大间隔、D1-AC-27 全机 RTL 的我方 LoAF）与 1 条 P1（D1-AC-28）仍在，修复需要排他性能锁下多次复测，超出本包"每次不超过 10 分钟"的约束，处置建议见交付总结 §4.1。

## 2 门禁结果

### 2.1 功能门禁

| 门禁 | 命令 | 结果 | 耗时 | 日志 |
|---|---|---|---|---|
| lint | `make lint` | 通过（全部规则；第 2 次运行含新增的 `check-release`） | 26–36 s | `.cache/final/lint*.log` |
| 契约 | `make test-contracts` | 通过：pytest 335 通过、3 跳过；Vitest 225 通过 | 1 min | `.cache/final/test-contracts.log` |
| pytest 全量 | `pytest -m "not perf"` | **2,711 通过、11 跳过、47 取消选择（perf）、0 失败**；11 个跳过都是 `tests/e2e/test_scenarios.py` 的长剧本（需 `AWR_E2E_FULL=1`，由 harness 用例 `e2e.scenarios` 运行，第 5 轮已通过） | 47 min 9 s | `.cache/final/pytest.log`、`pytest-junit.xml` |
| Vitest | `vitest run --project unit --project browser` | 136 个文件（1 跳过）：1,000 通过、1 跳过；其中 browser 15 个文件 45 通过 | 45 s | `.cache/final/vitest*.log` |
| Vitest 附加配置 | `make test-safety-web test-agent-web` | 4 + 8 通过 | 10 s | `.cache/final/vitest-extra.log` |
| tsc | `tsc -p tsconfig.json --noEmit` | 通过 | 30 s | `.cache/final/tsc.log` |
| 生产构建 | `make build`（`npm run build -w apps/web`，之后 `scan-m06-bundle`） | 通过；生产包扫描 clean（修复前 10 处，§4.4）；只有 Vite 的"chunk > 500 kB"提示（three 与 ui 分组，属 AWR-18 §6.3 的分块设计） | 4 s | `.cache/final/build-prod*.log` |
| 测试构建 | `VITE_AWR_TEST_SWITCHES=1 vite build`（私有目录 `.cache/final/dist-test`） | 通过；含 `featMatrix*` 块 | 4 s | `.cache/final/build-test*.log` |
| World 校验 | `make validate`（`worldpkg validate worlds/* --deep` 与 Ajv strict） | 六城与 synthcity 0 错误 0 警告（单城 1.9–4.0 s）；82 个文档 0 无效 | 22 s | `.cache/final/validate.log` |

pytest 在前端改动之前开始（本包没有改动 Python 代码，pytest 结果不受影响）；Vitest、tsc、构建、lint 与 Playwright 都在全部前端改动之后运行。

### 2.2 Playwright 功能用例（M16 harness，`--runs 1`）

驱动 `.cache/final/pw.sh`：`node perf/harness/run.mjs --case <id> --runs 1`，构建取 `apps/web/dist`（生产）或 `.cache/final/dist-test`（`VITE_AWR_TEST_SWITCHES=1`），后端由 harness 按用例登记启动与停止；每个用例单独持排他锁（最长一次 226 s），run id `p20261003-final`，结果在 `runs/perf/p20261003-final/<case>/`。这些是功能复跑（单次、`local` 门禁），不替代第 5 轮按 ADR-033 的 3 次中位判定。

| 用例 | 规格 | 构建 | 结果 | 耗时 | 记录 |
|---|---|---|---|---|---|
| `skeleton` | `perf/skeleton.spec.ts`（D1-AC-34） | 测试 | PASS（2/2） | 159 s | 全链路与六城直达 |
| `e2e.interaction` | `tests/e2e/interaction.spec.ts`（D1-AC-32） | 测试 | PASS（1/1） | 116 s | `switch_ms` 4.53 |
| `e2e.mission-edit` | `perf/mission-edit.spec.ts`（D1-AC-17 任务编辑） | 生产 | PASS（1/1） | 226 s | 航点增删改拖、撤销重做、follow_path succeeded、框选区域生成覆盖任务完成 |
| `warmup` | `perf/warmup.spec.ts`（D1-AC-25） | 生产 | PASS | 107 s | programs 增量 0；操作后 1 s 内最大间隔 116.7 ms（天气 100、选中 116.7、跟随 83.3、近景 50、拾取 66.7 ms）；揭开后 11.3 s PerfGovernor 静止 |
| `layout` | `perf/layout.spec.ts`（D1-AC-24） | 生产 | PASS | 85 s | RT 重分配增量 0；> 50 ms 占比相对基线 −2.18 个百分点 |
| `e2e.a11y` | `tests/e2e/a11y.spec.ts`（D1-AC-21） | 测试 | PASS（1/1） | 24 s | — |
| `e2e.brand` | `tests/e2e/brand.spec.ts`（D1-AC-20 BRAND-04） | 测试 | PASS（1/1） | 24 s | — |
| `e2e.sanitize` | `tests/e2e/sanitize.spec.ts`（D1-AC-20 运行时净化） | 测试 | PASS（1/1） | 25 s | — |
| `e2e.motion` | `tests/e2e/motion.spec.ts`（D1-AC-20 reduced 档） | 测试 | PASS（2/2） | 37 s | — |

全部在本包的 UI 改动（§4.1–§4.4）之后运行：单机详情、任务面板、Timeline 倍速、世界面板与快捷键表的改动经 interaction、mission-edit、warmup（含"选中"打开单机详情）、a11y 与 brand 覆盖，没有退化。

## 3 用户硬约束逐条核对（R2、R3）

| 子项 | 核对方法 | 结论与证据 | 本包处理 |
|---|---|---|---|
| R2d UI 全量 shadcn | ①`no-raw-controls`（RAW-01，oxc AST）通过；②在 RAW-01 之外另扫 `apps/web/src`（`ui/components/ui` 除外）：原生元素只剩 `LfTable` 的 `tr`（shadcn Table 子元素）、`BookmarkEditor` 的 `form`、lieflat 图表的 `canvas`、品牌 `img`、关于对话框外链 `a` 与路由 `Link`（shadcn Button 渲染为 `a`）；带 `onClick` 的普通元素只有 DroneRail 行（虚拟列表行）与 JobsPage 的事件拦截 `span`；③第三方依赖：`src` 只 import `@base-ui/react`（只在 `ui/components/ui`）、`cmdk`、`react-resizable-panels`、`class-variance-authority`、`cn`（shadcn 官方）、`@tanstack/react-virtual`、`morphicons`、`lucide`（数据）与非 UI 库，没有其他组件库（`stats-gl`、`@react-three/drei`、`@tanstack/react-table` 已声明但未被 import）；④浏览器原生提示：5 处 `title` 属性 | 通过（修复后）。原生 `title` 提示不是 shadcn 组件：JobsPage 的阶段标签与截断的任务 ID、MissionPanel 两个禁用按钮的原因、WorldPanel 的数据集引用改为 shadcn Tooltip（禁用按钮用 `focusableWhenDisabled` 保持可悬停，与 WorldPanel"添加 P600"同一写法）；TimelineBar 倍速下拉的禁用项去掉 `title`，原因由触发器上已有的 Tooltip 说明。外链 `a` 是内容元素（不在 RAW-01 的控件清单内），保留 | 改 5 处（§4.2） |
| R2a 图表与表格 lieflat | ①`lint-lf`（LF-CHART-01 等）通过；②`src` 中 canvas 与 SVG 图形只出现在 `ui/lf/**` 与 `ui/icons/**`；③表格：凡直接使用 shadcn `Table` 的地方逐个核对是否在 `[data-lf-table]` 作用域内；④截图核对（`screenshot-perf.jpg`：细线标记、阶梯条、刻度仪表、日志式表格） | 通过（修复后）。单机详情的任务表与传感器表、快捷键表直接用 shadcn `Table` 而不在作用域内：`data-num` 单元格不右对齐、无 tabular-nums，行分隔为实线；加 `[data-lf-table]` 容器 | 改 2 个文件；AWR-15 §9.6 补作用域条（ADR-079） |
| R2b 动效 transitions.dev | ①`motion-lint`（MOT-01 至 04）通过；②`styles/motion/tokens.css` 由 transitions.dev `_root.css`（快照 e2d5551）生成，`--check` 一致；③`src` 中非 shadcn 文件的动效类只用 token 名（`duration-fast`、`ease-smooth-out` 等），`transition-all` 只在 shadcn 组件内 | 通过。另：配方颜色变量只在 `.dark` 中改指语义 token，浅色报告主题仍取 transitions.dev 原色值（`#7c7c7c`、`#f1f1f1`、`#f40051` 等），改为 `:root, .dark` | 生成器改 1 行选择器并重新生成（ADR-079） |
| R2c 图标 morphicons、禁 emoji | ①`check-icons`（ICON-01 至 04）通过：无 `lucide-react`；②图标全部经 `ui/icons/{Icon,StateIcon}` 以 `morphicons/dom` 渲染 lucide 数据；`src` 中没有图标库之外的内联 SVG；③emoji 与禁用字形：默认范围 2,129 个文件 0 违规；把发布集合全部文本文件显式传入，只有用户原稿 `docs/01-design.md`、`docs/02-refs.md` 报 18 处（D1-AC-20 注第 2 条排除、不改写）；④运行时净化 `e2e.sanitize` 通过 | 通过。根目录 README、CONTRIBUTING、SECURITY、通知文件、`.github/**` 与 `docs/impl/**` 不在默认扫描范围（README-V2 §5 第 6 条），纳入后 2,200 个文件 0 违规 | 扩大扫描范围（ADR-079） |
| R2e 色卡科技灰、黑、白、红 | ①`no-hex`（VIS-L-01、02）与 `lint-lf --palette`（LF-PAL-01 至 05）通过；②另扫 `rgb()`、`hsl()`、`oklch()`、`color-mix()` 等函数色值：只在 token 文件（`theme.css`、`lf.css`、生成的 `tokens.css`）、shadcn 组件与遮罩用的 `oklch(0 0 0)` 中出现；`index.html` 的启动遮罩关键 CSS 用 g950、g300、g50 的 OKLCH 等值（在 JS 与样式表加载前绘制，文件内有说明）；③three.js 颜色字面量只在开发页功能矩阵（`viewport/dev/featMatrix*.ts`，测试场景，生产包不含）与清屏黑色 | 通过（浅色主题配方颜色见 R2b） | — |
| R2f logo 与头像 | ①`check-brand`（BRAND-01 至 03）通过：`public/brand/*` 与 `brand.lock.json` 一致，`anet-logo.svg` 与 `docs/media/anet-logo.svg` 字节一致，`avatar-460.png` 与 `docs/media/anet-avatar.png` 一致，无 `avatars.githubusercontent.com`；②`e2e.brand`（BRAND-04）通过；③截图核对：顶栏头像 24 px 加 UI 字体排的"ANet Drone4D · World Runtime"，徽章不在视口内 | 通过。README 底部头像 72 px；README 头图（概念图）中徽章在 GitHub 页面上显示约 240–290 px，低于产品界面 320 px 下限，但不属 AWR-15 §4.2 的产品落点，记为建议（交付总结 §4.5） | — |
| R3a 内置 UrbanScene3D 六城 | `make validate` 六城 0 错误；`make fetch-data` 与 `make worlds` 在本机构建（数据与世界包不入库，ADR-034）；skeleton 第 2 例六城直达（第 5 轮 71.5 s） | 通过 | — |
| R3b 无人机与 Mock | P600 高模与低模 glb 入库（`apps/web/public/models/`，65 KB 与 9 KB）；Mock FleetSim 1–1000 架（D1-AC-07）；`fake_gw.py` 与 `FakeSource.ts`（D1-AC-35）；`e2e.interaction` 添加与移除 P600 | 通过 | — |
| R3c 流畅性测试 | M16 harness 108 个登记用例（`make perf-list`），ADR-033 运行协议，报告 lieflat 版式；本包 9 个功能用例经同一 harness 运行 | 通过 | — |
| R3d 渐进加载 | 首屏一次 Range、APH 选择器与 PointPool（D1-AC-02、06 第 5 轮通过）；Vitest `tests/pointcloud` 通过；`screenshot-streaming.jpg` 为粗层 → 细化 → 完成三联 | 通过 | — |
| R3e 疏密自动调节 | CAS 级联控制器与 PerfGovernor（D1-AC-04 第 5 轮整景 47.8 ms 进入目标带，D1-AC-05 第 2 轮） | 通过 | — |
| R3f 非常流畅 | 第 5 轮：03b、09a、24、25、26 通过；03a 纽约最大间隔与 27 全机 RTL 的我方 LoAF 不通过 | **未完全满足**（2 条 P0） | 见交付总结 §4.1 |

## 4 发现与修复

### 4.1 表格不在 table.log 皮肤作用域（R2a）

`styles/lf.css` 的 table.log 皮肤只作用于 `[data-lf-table]` 之内。`DroneDetailPanel` 的 MissionTab、SensorsTab 与 `ShortcutHelp` 的 `ShortcutTable` 直接使用 shadcn `Table`：前两者的进度与视场角单元格写了 `data-num` 却没有右对齐与 tabular-nums，三张表的行分隔都是 shadcn 默认实线而不是点线。修复：三张表外加 `<div data-lf-table="">`（不改单元格结构与选择器）。AWR-15 §9.6 增加"作用域"一行（ADR-079 第 4 条）。

### 4.2 浏览器原生 `title` 提示（R2d）

| 位置 | 原写法 | 修复 |
|---|---|---|
| `ui/views/JobsPage.tsx` 阶段标签 | `<span title={原始状态码}>` | shadcn Tooltip，内容为等宽的原始状态码 |
| `ui/views/JobsPage.tsx` 任务 ID 列 | 截断显示，`title` 给全称 | 截断时用 Tooltip 给全称；不截断时不加提示 |
| `ui/panels/mission/MissionPanel.tsx` 编辑航线、绘制区域 | 禁用时 `title="先选择一架无人机"` | Tooltip 包住 Button（`focusableWhenDisabled`、`aria-disabled:opacity-50`），禁用时说明原因，可用时给操作说明（`edit.openHint`、`edit.area.hint`）；Button 仍是 ButtonGroup 的直接子元素 |
| `ui/layout/TimelineBar.tsx` 倍速下拉的禁用项 | `SelectItem title={原因}` | 去掉；触发器上已有的 Tooltip 已说明回放上限或只读原因 |
| `ui/panels/world/WorldPanel.tsx` 数据集说明 | `ItemDescription title={引用}` | Tooltip（`render={<ItemDescription/>}`，引用为空时不显示内容） |

### 4.3 浅色主题下的 transitions.dev 原色值（R2b、R2e）

`gen-motion-tokens.py` 把 32 组配方的变量逐字写入 `:root`，再只在 `.dark` 中把颜色变量改指语义 token。浅色主题只用于报告与导出（M15-FR-080，去掉 `.dark`），此时 shimmer、tabs、tooltip、matrix 与 like 配方取 transitions.dev 原值（灰阶之外还有 `#f40051`）。修复：改指块的选择器改为 `:root, .dark`（位于逐字配方之后，两种主题都取语义 token；`.dark` 保留，使嵌套暗色容器按暗色 token 重新求值）；`tokens.css` 重新生成（只改一行），`--check` 一致。M15-FR-045 同步。

### 4.4 生产包残留测试开关代码块（M06-AC-010，门禁外发现）

`make scan-m06-bundle` 对当前生产构建报 10 处 `allowFallback`（测试开关 `?allowFallback=1` 的标识符）：`viewport/testHooks.ts` 在 `if (!TEST_SWITCHES …) return` 之后以动态 import 引用功能矩阵页，打包器先为动态 import 建块、再把这段代码当作死代码删除，生产包里留下两个无人引用的 `featMatrix-*.js`、`featMatrixWgpu-*.js`。FX-WEB1 时扫描是干净的，之后加入的入口造成回归；扫描不在 `make build`、`make ci`、G1h 中，所以没人发现。修复：

1. 入口改为内联条件 `import.meta.env.DEV || import.meta.env.VITE_AWR_TEST_SWITCHES === '1' ? async (o) => (await import('./dev/featMatrix')).runFeatMatrix(o) : undefined`（编译期折叠，生产构建不再为它分块）；生产包不再含 `featMatrix*` 块，测试构建仍含并由入口引用。
2. `mk/m06.mk` 把 `scan-m06-bundle` 登记进 `BUILD_TARGETS`（`VITE_AWR_TEST_SWITCHES=1` 时不登记）：`make build`、`make run` 的按需重建、`make ci` 与托管 CI 都会执行扫描，扫描不通过即构建失败（ADR-079 第 6 条；M06-AC-010 测试方法同步）。

### 4.5 演示动图超过 5 MB（发布合规）

`docs/media/demo-flight.webp`（README 头图下方）6,037,146 字节（SHOW-M 按 ≤ 8 MB 导出，quality 80）。用 SHOW-M 的同一组 200 帧 PNG 母版（`.cache/showm/raw/b/gif/`），帧时 50 ms、无限循环、960 × 540 不变，试编码 quality 72 与 66：

| quality | 字节 | 第 100 帧对母版 PSNR |
|---|---:|---:|
| 80（原文件） | 6,037,146 | 39.23 dB |
| 72（采用） | 4,630,188 | 38.10 dB |
| 66 | 4,233,700 | 37.65 dB |

采用 q72（最接近原画质且低于 5,000,000 字节，余量约 37 万字节）；回读 ANMF 块：200 帧、合计 10,000 ms、循环 0；抽查第 100 帧目视无可见退化。原文件备份在 `.cache/final/demo-flight.q80.webp.bak`；`.cache/showm/export.py` 的动图上限改为 4,900,000 字节，重新导出时自动选 q72。

### 4.6 发布集合检查与扫描范围（防回归）

1. 新增 `tools/lint/check-release.mjs`（REL-01 至 03，并入 `make lint`）：发布集合按 `git ls-files --cached --others --exclude-standard`（遵从 `GIT_DIR`、`GIT_WORK_TREE`）；REL-01 单文件 > 5,000,000 字节；REL-02 `worlds/`、`data/raw/`、`runs/`、`refs/`、`.cache/`、`apps/web/dist/` 下的文件与 `.ply`、`.las`、`.laz`、`.e57`、`.pcd`、`octree.bin`、`hierarchy.bin`；REL-03 私钥块、常见令牌形态，以及 `~/.config/awr/*.env` 中长度 ≥ 12 的取值逐字出现（只在内存中比较，报告只给文件与键名）。不在 git 仓库内时提示并通过（本机工作树没有 `.git`，`make lint` 中打印说明；托管 CI 在克隆内运行）。`tools/lint/selftest.mjs` 增加 8 个合成用例（令牌与本机取值在运行时拼出，自身不触发 REL-03），全部通过；以原 q80 动图做反例时报 REL-01。对发布集合运行（设置 `GIT_DIR`）：2,299 个文件、46,050,654 字节、0 违规、943 ms。
2. `no-emoji` 的扫描范围增加 `docs/impl/**`、根目录的 `README.md`、`README.zh-CN.md`、`CONTRIBUTING.md`、`SECURITY.md`、`THIRD_PARTY_NOTICES.md`、`NOTICE`、`CITATION.cff` 与 `.github/**`（`tools/lint/_common.mjs` 的 `DOC_GLOBS` 与 `RELEASE_DOCS`），扫描文件由 2,129 个增至 2,200 个，0 违规。

### 4.7 暂定阈值冻结（ADR-080）

按第 5 轮 §5 的建议与第 3 至 5 轮数据，以原值冻结（不收紧、不放宽）：D1-AC-09a 最大间隔 ≤ 250 ms（第 5 轮 3 次有效运行 117–167 ms）；D1-AC-26 命令到可见 ≤ D_global + 150 ms（三轮中位 −69、−26、−62 ms）、关注集切换 ≤ 0.5 m（0.005、0.003、0.003 m）、×10 HOLD < 1%（三轮 0%）、选中机通道 ≥ rAF（2.71、2.60、2.55 倍）。D1-AC-26 t_sim 到像素（统计窗口起点问题未修）与 D1-AC-09b（第 2 轮后未复测）仍为暂定。`thresholds.json` 五个键的 `status`/`source` 同步，`check-thresholds` 通过。

### 4.8 文档同步

`docs/README.md`（文档地图）的 ADR 数目与范围（53 → 80）、D1-AC-03b 冻结状态、零下载试用命令，并新增第 7 节"实现、验收与交付报告"索引；README 两版的 Specs 徽章与文档表（78 → 80 条 ADR）、实现报告行加交付总结链接。

## 5 发布合规

### 5.1 入库范围

发布集合 = `GIT_DIR=/data/projs/.anet-drone4d.git GIT_WORK_TREE=/data/projs/anet-drone git ls-files --cached --others --exclude-standard`，本包结束时 2,301 个文件、46,134,178 字节（含本包新增的 `check-release.mjs` 与两份报告；结束时再跑 `check-release` 0 违规）。

| 检查 | 方法 | 结果 |
|---|---|---|
| UrbanScene3D 点数据 | 扩展名统计；`.ply/.las/.laz/.e57/.pcd`、`octree.bin`、`hierarchy.bin` 与 `data/`、`worlds/` 路径 | 0。点云相关的入库文件只有 `apps/web/tests/pointcloud/fixtures/{newyork,shanghai,shenzhen}/`：`nodes.json` 为八叉树节点的点数与包围盒（无点坐标、无颜色），`flight60.bin` 为相机轨迹（AWR-18 §14 规定入库的夹具）；`packages/contracts/fixtures/world/{shenzhen,sanfrancisco}/` 为 4 个元数据 JSON（`dtm_10m.json` 303 字节，只有描述） |
| World Package | `worlds/` 路径、`world.json` 与容器文件 | 0（`worlds/` 与 `/data/raw/` 由 `.gitignore` 锚定排除） |
| UrbanScene3D 渲染截图 | 逐张查看 `docs/media/` 的 15 个图片与动图 | 6 张截图与动图都是 synthcity（图注与 HUD 写明，SHOW-M），7 张为概念图（AI 生成的示意画面与品牌素材），没有六城的渲染画面 |
| 凭据 | ①`~/.config/awr/gravitex.env` 读取后只在内存中逐字比对 3 个取值（URL、密钥、模型名；密钥另按任意 16 字符窗口比对）；②`git grep` 仓库 20 个提交的全部可达内容；③令牌形态与私钥块正则；④`gravitex`、`/home/ink`、`.config/awr` 字样 | 全部 0 次；私网 IP 只出现在部署文档与测试中的示例地址（10.0.0.x、192.168.1.x） |
| 单文件大小 | 遍历发布集合 | 修复前 1 个超过 5 MB（动图 6,037,146 字节）；修复后最大 4,630,188 字节，其次 `packages/contracts/env/golden/derive.json` 1,447,889 字节 |

### 5.2 README 链接与命令

| 检查 | 结果 |
|---|---|
| 相对链接、图片与锚点（README 两版、CONTRIBUTING、SECURITY、THIRD_PARTY_NOTICES、NOTICE、文档地图；代码块之外，按 GitHub 锚点规则） | 348 处全部存在且在发布集合内，0 处失效 |
| 外部链接（README 两版，去重 23 个，`curl -L`） | 22 个返回 200；CI 徽章 `actions/workflows/ci.yml/badge.svg` 返回 404：`.github/workflows/ci.yml` 尚未提交（README-V2 §5 第 1 条），首次推送并运行后恢复 |
| README 与 CONTRIBUTING、SECURITY、文档地图中的 `make <目标>` | 全部在 make 数据库中存在 |
| 零下载流程 | 本包重跑 `make validate`（synthcity 0 错误）与 `make build`；`make demo-world`、默认世界回退与 S0 由 DEMO-W、SHOW-CI 实测，SHOW-CI 的干净克隆 G1h 全部通过 |

### 5.3 提交时的注意事项

本地工作树相对 HEAD（`578960d`）有约 270 个未提交路径，包括 README 引用的 7 个媒体文件、`.github/workflows/ci.yml`、`python/awr/swarm/coverage/`（干净克隆的 pytest 依赖它）与本包的全部改动。它们必须在同一次提交中入库，否则 GitHub 页面出现破图、CI 徽章 404、干净克隆收集失败。本包没有执行任何 git 写操作。

## 6 规格变更

| ADR | 内容 | 同步的文档 |
|---|---|---|
| ADR-079 | 发布面合规：REL-01 至 03；emoji 与禁用字形扫描范围扩大到发布面文档；表格一律在 `[data-lf-table]` 作用域内；配方颜色两种主题都指向语义 token；只在测试构建存在的动态 import 用内联条件，生产包扫描并入 `make build` | AWR-03 §7.0、§8.4 D1-AC-20 注第 1 条、附录 E；AWR-18 §13.1（REL 行）、§13.2 第 1、5 条；AWR-15 §9.6；M15-FR-045；M06-AC-010 |
| ADR-080 | D1-AC-09a 最大间隔与 D1-AC-26 四个子项按原值冻结；t_sim 到像素与 09b 仍暂定 | AWR-03 §7.0、§8.4、附录 E；AWR-18 PERF-AC-012、PERF-AC-040；`apps/web/perf/thresholds.json` |

## 7 遗留与建议

1. **两条 P0 与一条 P1**（交付总结 §4.1）：D1-AC-03a 与 27 的共同根子是第一条 Toast 的插入（Base UI 高度测量的强制布局）与首次合成；修复后各需 3 次复测。D1-AC-28 需评审人在 `waivers.yaml` 登记豁免或在 GPU 客户端上复测。
2. **应复测项**：D1-AC-18、11b、23、30、09b；D1-AC-26 t_sim 到像素待 `focusSettled` 修正后冻结。
3. **原生 `title` 的防回归**：本包逐处改掉了 5 处，但 RAW-01 不检查 `title` 属性；如需持续约束，可在 `no-raw-controls` 中增加"`ui/components/ui` 之外的原生元素与 shadcn 组件不得带 `title` 属性"一条（需修订 AWR-18 §13.1）。
4. **未使用的依赖**：`stats-gl`、`@react-three/drei`、`@tanstack/react-table` 在 `apps/web/package.json` 中声明但 `src` 没有 import；依赖增删走 M00 流程（本阶段禁止改依赖），建议 V0.2 清理或说明用途。
5. **README 头图徽章显示宽度**与英文界面截图：见交付总结 §4.5。

## 8 文件清单

修改（代码与工具）：`apps/web/src/ui/panels/drone-detail/DroneDetailPanel.tsx`、`apps/web/src/ui/views/ShortcutHelp.tsx`、`apps/web/src/ui/views/JobsPage.tsx`、`apps/web/src/ui/panels/mission/MissionPanel.tsx`、`apps/web/src/ui/layout/TimelineBar.tsx`、`apps/web/src/ui/panels/world/WorldPanel.tsx`、`apps/web/src/viewport/testHooks.ts`、`apps/web/src/styles/motion/tokens.css`（生成物）、`apps/web/perf/thresholds.json`、`tools/shadcn/gen-motion-tokens.py`、`tools/lint/_common.mjs`、`tools/lint/no-emoji.mjs`、`tools/lint/selftest.mjs`、`mk/lint.mk`、`mk/m06.mk`。

新增：`tools/lint/check-release.mjs`、`docs/impl/D1-交付总结.md`、本报告。

替换：`docs/media/demo-flight.webp`（重新编码）。

修改（文档）：`docs/03-设计基线与决策记录.md`（ADR-079、ADR-080、§7.0、§8.4）、`docs/18-性能与测试方案.md`、`docs/15-视觉设计规范与色卡.md`、`docs/modules/M15-前端UI壳与设计体系组件PRD.md`、`docs/modules/M06-Web视口与渲染后端PRD.md`、`docs/README.md`、`README.md`、`README.zh-CN.md`。

不入库的过程文件：`.cache/final/`（门禁日志、Playwright 驱动 `pw.sh` 与逐例日志、测试构建、原动图备份、改动前的文件备份）；`.cache/showm/export.py`（动图上限）；会话 scratchpad 中的凭据比对、链接检查与动图试编码脚本。Playwright 用例的结果在 `runs/perf/p20261003-final/`。
