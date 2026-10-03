# d01 研究笔记：lieflat-charts 设计体系 → 《图表与表格实现规范》草案

> 研究单元：d01 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §28（DroneState）、§36–37（实时通信与刷新频率）、§38–40（UI、Timeline、Drone Interaction）、§43–50（MVP 与路线）
> 仓库快照：`refs/design/lieflat-charts` @ `eace082`（2026-09-05，5754 stars，PolyForm Noncommercial 1.0.0；本项目为科研用途，按用户要求忽略 license）。文中路径都相对仓库根目录。
> 本单元结论全部来自源码精读，另在本机 headless Chromium（SwiftShader，WebGL2 软件渲染）做了 SVG 与 Canvas 的流式图表微基准，脚本在 `/data/projs/anet-drone/.cache/research/d01/`（`www/bench.html`、`run.mjs`、`run2.mjs`、`palette.mjs`）。测量时机器负载较高（load average 4–10，8 核，同时有其他研究单元在跑），**绝对帧率不可信，只比较相对量级**。
> 相关单元：r11（three.js WebGPU，帧率控制器）、r12（Potree 加载器，HUD 指标与 4 Hz store）、r16（天气视觉，MOR 能见度）。本文定义的是它们共用的 HUD 与分析页的图表与表格层。
> 需要确认的假设：用户原话"产品色采用科技灰、黑色、色、红色"，本文把中间缺字的"色"按"白色"理解。

---

## 0. 结论速览

| 仓库 / 子模块 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **lieflat-charts**（整体） | 一套 Agent Skill：SKILL.md 规则 + 两份 token 文件 + 61 个图表模板（手写 SVG / ECharts / Chart.js）+ 12 套整页报告。**不是 npm 库**，没有构建，全部是单文件 HTML，用命令式 DOM 绘制 | **port**：视觉语言、token、几何与编码方式全部移植为 React + TS 组件；**不直接依赖** | V0.1–V1.0（MVP 在 V0.1 性能 HUD 与 V0.2 遥测面板） | 5/5 |
| `mono-tokens.js` | 风格的唯一正本：色板、字体、形状、动画、tooltip、`rnd`、`pol/sect/blob`、`obsReveal`、`CARD_CSS` | **port** → `lib/lf/tokens.ts` + `globals.css` | V0.1 | 5/5 |
| `color-presets.js` 的 **WIRE** | "黑灰阶 + 一个荧光橙主角"，和本项目"科技灰/黑/白 + 红"几乎同构 | **port**：以它为模板建一套 custom 色板 `ANet Graphite`，HERO 换成 `#E93024`，暖灰换成冷灰 | V0.1 | 5/5 |
| `templates/basics-gallery.html`（F1–F17） | 稀疏数据的 Lupi 语法：梯级柱、发丝折线/面积、刻度环、刻度行、刻度仪表、直方、箱线、河流、K 线 | **port**：本项目主力，约 12 个图型进 MVP | V0.1–V0.6 | 5/5 |
| `templates/glance-gallery.html`（G3–G22） | 快读型，粗笔画；G17 动态流、G18 画线加计数器最适合 HUD | **port** G17/G18/G10/G15/G20/G21（改写为 Canvas/SVG，不用 ECharts）；其余 reference | V0.1–V0.6 | 4/5 |
| `templates/lupi-gallery.html`（L1–L20） | 细读型，逐记录：L3 条码、L11 生命史、L12 柱廊、L13 沙漏、L16/L17 热力、L19 山脊、L20 平行坐标 | **port**（分析页、报告）；海报类 reference | V0.2–V1.0 | 4/5 |
| `templates/big-*.html`（B1–B3） | 交互大图：环形、力导向、Threads（9 px 透明孪生热区 + hover/pin 状态机） | **port** 交互模式；**reference** 图本身（V1.0 ANet 网络） | V1.0 | 3/5 |
| `templates/reports/`（R01–R12） | 整页报告骨架；R09 仪表盘、R12 周报、R10 的 `table.log` 是全库**唯一的表格规范** | **port** `table.log` 与 KPI 卡；**reference** 报告版式（性能测试报告、任务复盘） | V0.1（测试报告）/ V0.6 | 4/5 |
| `templates/maps-gallery.html`（M1–M2） | 美国与世界 choropleth，依赖在线 GeoJSON | **skip**：本项目的地图是 3D 世界本身 | — | 1/5 |
| `scripts/validate.mjs`、`smoke-new-charts.mjs` | 静态检查（禁 `Math.random`、色值越界、重复 id、脚本语法）与 Playwright 烟测 | **port** 成本项目的 `scripts/lint-lf.mjs` 和 e2e 用例 | V0.1 | 4/5 |

**实现者先读这 12 条（每条都有源码或实测依据）：**

1. **lieflat 是"规则 + 模板"，没有可 import 的代码库。** 每张图是一个 IIFE，用 `el()/txt()/tip()` 命令式创建 SVG 节点，入场动画靠 CSS 类（`.pop/.fade/.draw`）加内联 `animation-delay`（`templates/basics-gallery.html` 第 201–218 行）。复用方式只能是 **port**：保留几何、编码、比例和动画节奏，重写成 React 组件。ECharts 和 Chart.js 的图（G3–G18、F13）统一改写成手写 SVG 或 Canvas，**不引入 ECharts、Chart.js 或 Recharts**。
2. **品牌色直接落在 WIRE 预设的逻辑上。** WIRE 的 `logic: 'mono+accent'`：灰阶承载全部数据，强调色每张图只给一个主角（`color-presets.js` 的 WIRE 注释）。本项目建一套 custom 色板 **ANet Graphite**：冷调科技灰阶 + 品牌红 `#E93024`。整个产品只用这一套色系（SKILL.md §6.5 "同一交付只用一种色彩系统"），不出现青瓷蓝、椰林绿或任何第三色相。
3. **红色的对比度是硬约束（实测，§3.2）。** `#E93024` 在暗面板 `#111214` 上是 4.38:1，画数据标记够用（≥3:1），画小字不够（<4.5:1）。暗色主题里红色文字改用 `#FF5242`（5.83:1）；浅色纸面 `#F2F3F5` 上红色标记是 3.85:1，红色文字改用 `#B4241B`（5.89:1）。色觉异常模拟下，红与中灰 `#81868F` 的 ΔE 为 10.6（protan，暗）和 13.9（deutan，亮），都高于 8 的目标。
4. **lieflat 的重绘方式不能用于遥测。** 它的做法是 `innerHTML=''` 后重建几百个节点（`obsReveal` 的 `go()`），同样的做法在 6 张图 × 600 点、10 Hz 刷新时，脚本耗时 34–36 ms/次，6 秒内出现约 1 s 的 long task，页面只剩 2.3 fps（§3.6 基准表）。
5. **SwiftShader 下真正拖垮帧率的是 GPU 光栅化，不是 JS。** 默认（GPU 加速）的 2D canvas 在 10 Hz 下只有 5.6–7.4 fps；**CPU canvas（`getContext('2d', {willReadFrequently: true})`）在 12 张图 × 1200 点、10 Hz 下保持 60 fps**。叠加一个持续渲染的 WebGL2 场景（基线 28–30 fps）后：CPU canvas 18–24 fps，GPU canvas 5.5 fps，SVG 10 Hz 更新 4.7 fps，SVG 放进独立合成层（`contain: strict; will-change: transform`）后是 17 fps。
6. **两条渲染路径：** 静态或低频（≤2 Hz）的图用 **SVG**，必须放进独立合成层；流式图（sparkline、实时曲线、长条码）用 **CPU canvas**。全部图共用**一个** rAF 调度器：HUD 4 Hz（和 r12 的 4 Hz 统计 store 对齐），当前聚焦的遥测图最高 10 Hz，不可见时暂停。
7. **选型遵守 SKILL.md 的优先级，同时用足它的例外条款。** HUD 属于"监控 / dashboard"，SKILL.md §0 第 4 条允许直接用 Glance（G17 动态流、G18 计数器）；分析页和报告按"Lupi Editorial → Lupi Basics → Glance"走。MVP 需要移植的图型约 16 个（§4.4）。
8. **字号要换算。** lieflat 的字号写在 `viewBox="0 0 400 320"` 坐标系里；图放进 300 px 宽的侧栏时整体缩到 0.75，7 px 的轴标签只剩 5.25 px。本项目的 HUD 图**按像素布局**（ResizeObserver 取宽度，不用 viewBox 缩放），CSS 最小字号 10 px；中文标签不做全大写和加字距。
9. **表格：lieflat 没有表格 gallery，唯一的规范是 R10 的 `table.log`**（`templates/reports/report-10.zh.html` 第 77–88 行）：数字右对齐，表头下 1 px 实线，行间点线，**不用斑马纹**，合计行上方 1 px 实线，只有一个 hot 单元格用强调色，全局 `font-variant-numeric: tabular-nums lining-nums`。本项目把它映射到 shadcn `Table` 的 className 上（§3.9）。
10. **源码里的坑：** ① `glance-gallery.html`、`big-*.html` 和对应彩色版里的 `rnd` 少了 `Math.abs`，约 44% 的取值为负（实测 1651/3781），会让抖动和随机游走偏向一侧，移植时必须用 `mono-tokens.js` 的版本；② catalog 表头写"63 张"，实际只有 61 条，L18、G1、G2 不存在，但 SKILL.md 仍引用 G1；③ basics gallery 的代码块名是 `B1…B4 / C1…C9`，和 catalog 的 F1–F13 对不上（对照表见 §2.4）；④ 图例和标注里直接用了 `U+25B2 U+25BC U+25CF U+25CB U+25C9 ↑ ←`（三角、圆点、箭头）等 Unicode 字符，本项目禁用 emoji，这类字符也统一换成 SVG 形状或 morphicons；⑤ 依赖 Google Fonts 和 jsDelivr CDN，离线环境下字体和 ECharts 图全部失效；⑥ `FAINT`（Mono `#C6C5BF`，WIRE 为 TXT 的 .32–.40 透明度）用来写来源行，对比度只有 1.5–2.4:1；连副标题用的 Mono `MUTED #8F8E88` 也只有 2.86:1，都达不到 WCAG 对小字 4.5:1 的要求。
11. **和 shadcn 的冲突要显式解决。** shadcn `Card` 默认 `rounded-xl border shadow-sm py-6 gap-6`（`refs/design/ui/apps/v4/registry/new-york-v4/ui/card.tsx`），lieflat 要求"圆角 24、无边框、无阴影、靠留白分卡"。做法：`LfChartCard` 仍然使用 shadcn `Card`（结构和语义不变），通过 className 覆盖样式。浮在 3D 场景上的 HUD 面板保留 1 px hairline 边框，因为它必须和 3D 背景分开；**不用 `backdrop-filter`**，在软件渲染下它非常贵。
12. **动效分两套节奏。** lieflat 的入场是 900 ms `quarticOut`、点阵 12 ms 错峰、条形 100 ms 错峰（`mono-tokens.js` 的 `MOTION`），这套只用在分析页和报告。HUD 里的实时图**没有入场动画**，数据更新不做补间，或只做 0–260 ms 线性过渡（G17 的 `animationDurationUpdate: 260`）；数值变化用 G18 的 cubicOut 计数器，并带代次保护。所有动画都响应 `prefers-reduced-motion`。

---

## 1. 仓库概览

| 项 | 内容 |
|---|---|
| 地址 | https://github.com/larashero3-dotcom/lieflat-charts（作者"躺在废墟里"，在 moxt.ai 上制作） |
| 快照 | `eace082`，2026-09-05（Merge PR #15），5754 stars，2026 年仍在活跃开发 |
| 形态 | Agent Skill：`SKILL.md` 带 frontmatter（`name`、`description`），`agents/openai.yaml` 供 Codex 使用；安装方式为 `npx skills add …` 或 clone 到 `~/.claude/skills/` |
| 规模 | 约 2.57 万行（HTML、JS、MD）。仓库 39 MB，其中 `docs/` 占 19 MB（PNG 与 GIF 预览），`templates/` 1.3 MB |
| 依赖 | 纯 SVG 的图零依赖；G3 用 Chart.js 4（CDN）；G5–G18 和 F13 用 ECharts 6（CDN，`echarts@6/dist/echarts.min.js`）；字体 Inter（Google Fonts）；地图用在线 GeoJSON |
| 许可 | PolyForm Noncommercial 1.0.0；Chart.js（MIT）、ECharts（Apache-2.0）、Inter（OFL）见 `THIRD_PARTY_NOTICES.md` |
| 构建与测试 | 无构建。`node scripts/validate.mjs` 做静态检查（本机实测"检查通过：52 个 HTML 文件，56 个文本文件"）；`scripts/smoke-new-charts.mjs` 用 Playwright 做运行期烟测 |

**设计哲学**（SKILL.md §2–§3、§5、§7）可以概括为六条，本项目全部继承：

1. **明度即数据**：最重要的最黑，暗卡上反转为最亮。多系列沿灰阶按重要性分配。
2. **一律实心**：不发光、不渐变、不加阴影。唯一的例外是叠加型图里的透明度，因为那时透明度本身在编码密度。
3. **单位诚实**：1 档 = 1 个真实单位，面积编码用 `Math.sqrt(v)` 换算半径；柱状图不断轴。
4. **卡片四件套**：结论式标题 + 副标题（图例和时间范围写在这里，用 `·` 分隔）+ 图 + 来源行（全大写、加字距）。
5. **交互三问**：这个元素背后有真实记录吗？不点能读出来吗？元素超过 50 个吗？装饰元素**禁止**加交互。
6. **敢说不**：拒绝断轴、发光、玻璃拟态、3D 图表，拒绝给单序列用多色相。

---

## 2. 源码结构与关键模块

### 2.1 目录树

```text
lieflat-charts/
├── SKILL.md              # 规则法典：输出模式、选型硬约束、Mono 语法、两系分工、决策树、交互三问、库外翻译、彩色规则、说不清单、自检清单、单文件骨架
├── catalog.md            # 图型目录：61 条（表头写 63），每条 = 编号 · 名字 · 卡内标题 · 数据形状 · 场合 · 读者时间 · 引擎 · 姊妹
├── report-catalog.md     # 12 套报告模板索引（版心 / 密度 / 色系 / 依赖）
├── mono-tokens.js        # 风格唯一正本（挂在 window.MONO）
├── color-presets.js      # 三套彩色预设 + INK_BOOST + BY_WORDS（挂在 window.PRESETS）
├── templates/
│   ├── basics-gallery.html   # F1–F17（943 行）
│   ├── lupi-gallery.html     # L1–L20（缺 L18，1157 行）
│   ├── glance-gallery.html   # G3–G22（1070 行）
│   ├── maps-gallery.html     # M1–M2
│   ├── big-circular.html / big-force.html / big-threads.html   # B1–B3
│   ├── color/                # 12 个 gallery × {porcelain, palm, wire} 换肤样张 + 大图的 porcelain/palm 版
│   └── reports/              # report-01..12.{zh,en}.html + index.html
├── examples/             # lenny-2026-survey.html、reports/r04-financial-report.zh.html
├── scripts/validate.mjs、smoke-new-charts.mjs
└── agents/openai.yaml
```

### 2.2 `mono-tokens.js`：十个分区

| 分区 | 关键符号 | 取值（本项目移植时的依据） |
|---|---|---|
| 1 色板 | `INK #1C1C1A`、`PAPER #F0EFEB`、`MUTED #8F8E88`、`FAINT #C6C5BF`、`GRID #DEDDD6`；7 级 `L`、5 级 `LAD`；`DARK{bg, ink, muted, faint, grid, gridSoft, ladder}` | 暗卡：`bg #1C1C1A`，数据色反转为纸色 `#F0EFEB`，网格 `#2E2D29`，ladder 从亮到暗 |
| 2 字体 | `FONT.family='Inter'`；`title 16.5/700/-.02em`、`titleBig 19`、`sub 11.5/400`、`src 9.5/500/.08em`、`value 800`、`axis 9.5/600`；`minHalf 6.5`、`minWide 5.5` | 这些字号都是 **viewBox 坐标**（400×320），不是 CSS px |
| 3 形状 | `cardRadius 24`、`cardPad '28px 28px 20px'`、`barRadius 99`（胶囊端）、`tooltipRadius 12` | |
| 4 动画 | `enter 900`、`enterSlow 1200`、`easing 'quarticOut'`、`staggerDot 12`、`staggerBar 100`；CSS 三个类：`pop` 0.5 s `cubic-bezier(.2,.7,.3,1.3)`、`fade` 0.9 s `ease`、`draw` 1 s `cubic-bezier(.4,0,.2,1)`（配合 `pathLength=1` 和 `stroke-dasharray:1` 实现描线）；自带 `prefers-reduced-motion` 降级 | `pop` 的 y2=1.3 会轻微过冲，和 SKILL.md"不弹跳"的说法自相矛盾（§6） |
| 5 Tooltip | `tipLight`（墨底纸字，padding [10,14]，12 px）、`tipDark`（反转） | 恰好对应 shadcn `TooltipContent` 的 `bg-foreground text-background` |
| 6 伪随机 | `rnd(i,k) = abs(((i*73856093) ^ (k*19349663)) % 1000) / 1000` | 演示数据必须确定性，禁用 `Math.random()` |
| 7 几何 | `pol(cx,cy,r,deg)`、`sect(cx,cy,r0,r1,a0,a1)`（环形扇区 path）、`blob(x,y,r,seed)`（手绘感圆：两个慢波加噪声，二次中点平滑） | |
| 8 SVG 快捷 | `el(p,t,a)`、`txt(p,a,s)`、`tip(n,s)`（原生 `<title>`） | |
| 9 Reveal | `obsReveal(id, fn)`：IntersectionObserver threshold .3，**只触发一次**；点击重播；`keep(id,t)` 登记 timer，重播前 `clearInterval`；`eReveal` 是 ECharts 版 | React 里改写为 `useRevealOnce` 加 effect 清理 |
| 10 卡片骨架 | `CARD_CSS`：`.grid2`（1fr 1fr，gap 22）、`.card`、`.card.dark`、`.card.wide`、`h2`、`.sub`、`.src`、`.ch{height:320px}` | |

### 2.3 `color-presets.js`

- `INK_BOOST = { strokeScale: 1.8, opacityFloor: 0.85, exempt: ['dot heat'] }`：彩色版的发丝线在浅色下容易"消失"，所以线宽 ×1.8，透明度不低于 0.85。
- 三套预设共用一套**角色名**：`BG、TXT、MUT、LAB、FAINT、FLOOR、QUIET、TRACK、GRID、DATA、DATA2、HERO、HERODK、FAINTDATA、BEAD、HALO、CAT4、CAT3、HEAT、SER、RAMP、DRAMP4、RAMPDK`；porcelain 和 palm 另有 `DARK{CARDBG, DK8, HL, LABEL, SUB, SRCROW, RULE, LINK_*}`，给暗底大图用。
- **WIRE**：`BG #F0F0EE`、`TXT #1F1E1C`，结构灰一律是 TXT 的透明档（MUT .60、LAB .72、FAINT .32、FLOOR .24、QUIET .15、TRACK .12、GRID .16），`DATA #22211F`、`DATA2 #8F8E86`、`HERO #F5572F`，`RAMP` 的最后一档就是 HERO。
- WIRE 样张（`templates/color/basics-wire.html` 第 191–197 行）有一个 `HEROMODE=true`（"黑体红睛模式：每张图一个主角元素整体走 HERO"），实际效果是：F1 的第一根柱、F2 的峰值点、F3 的峰值发丝、F9 的 NET 柱、F12 改版后的点、F13 的一个叶子。这正是本项目"一处红"的规则来源。
- `templates/color/glance-wire.html` 第 221–227 行给了三个派生工具：`alpha(hex,a)`、`mix(a,b,t)`、`lum(hex)`（WCAG 相对亮度）。custom 色板的明暗派生可以直接照搬（SKILL.md §6.5 第 4 条："浅色由用户色与 `BG` 混合，深色由用户色与 `TXT` 混合"）。

### 2.4 gallery 结构与编号对照

每个 gallery 的结构都是：`<div class="card">`（h2 + `.sub` + `<svg id>` 或 `<div class="ch" id>` + `.src`），外加 `<script>` 里按 `// ════ 名字 ════` 分块的 IIFE。**catalog 编号和代码块名并不一致**，移植时按下表检索：

| catalog | 代码块（文件:行） | 容器 id | 引擎 | 本项目相关度 |
|---|---|---|---|---|
| F1 Rung Bars | `basics-gallery.html:220` "B1 · rung bars" | `rungs` | SVG | 高（LOD 层点数、类目比较） |
| F2 Hairline Line | `:250` "B2 · hairline line" | `dayline` | SVG | 高（≤60 点时序） |
| F3 Hairline Area | `:293` "B3 · hairline area" | `hairarea` | SVG | 高（飞行日志回放） |
| F4 Tick Donut | `:328` "B4 · tick donut" | `tickdonut` | SVG | 中（飞行模式时间占比；风玫瑰的母本） |
| F5 Tick Rows | `:370` "C1 · tick rows" | `tickrows` | SVG | 高（多机当前值对比） |
| F6 Paired Rungs | `:400` "C2" | `pairrungs` | SVG | 中（WebGL2 与 WebGPU 对比） |
| F7 Stacked Rungs | `:434` "C3" | `stackrungs` | SVG | 中 |
| F8 Plumb Scatter | `:470` "C4" | `plumb` | SVG | 中（漂移与风速）；3D 铅垂线的语汇来源 |
| F9 Rung Waterfall | `:509` "C5" | `fall` | SVG | 低（帧耗时分解） |
| F10 Dot Heat | `:561` "C6" | `dotheat` | SVG | 中 |
| F11 Tick Gauge | `:602` "C7 · tick gauge" | `gauge` | SVG | **极高**（电量、点预算、任务进度、加载进度） |
| F12 Dumbbell Queue | `:636` "C8" | `dumbbell` | SVG | 高（优化前后） |
| F13 Nested Treemap | `:675` "C9" | `treemap` | ECharts | 低（内存构成） |
| F14 Rung Histogram | `:720` | `histo` | SVG | 高（帧耗时、WS 延迟分布） |
| F15 Tick Box | `:766` | `boxplot` | SVG | 高（跨城市性能） |
| F16 Stream Ribbon | `:811` | `stream` | SVG | 中（多机任务份额） |
| F17 Candlestick | `:871` | `candle` | SVG | 低（风速区间的影线语法可借用） |
| L1 Launch Fan | `lupi-gallery.html:264` "1 · launch fan" | `fan` | SVG | 低 |
| L2 Dot Cascade | `:309` "4 · dot cascade on dark" | `cascade` | SVG | 低（中文类目名竖排受限） |
| L3 Barcode Lollipop | `:336` "2 · barcode lollipop" | `barcode` | SVG | 高（RTK 状态、逐秒峰值） |
| L4 Arc Matrix | `:374` | `arcmatrix` | SVG | 低 |
| L5 Radial Convergence | `:418` | `converge` | SVG | 中（V1.0） |
| L6 Cluster Field | `:465` | `clusters` | SVG | 低（海报） |
| L7 Brand Spectrum | `:510` "1 · brand spectrum" | `spectrum` | SVG | 低 |
| L8 Dotty Matrix | `:549` | `dotty` | SVG | 低 |
| L9 Bubble Almanac | `:583` | `almanac` | SVG | 低 |
| L10 Radial Patchwork | `:683` "4 · radial patchwork" | `patchwork` | SVG | 中（风玫瑰的母本） |
| L11 Trend Lineage | `:727` "6 · trend lineage" | `lineage` | SVG | 中（任务与 Agent 生命史） |
| L12 Type Colonnade | `:783` "8 · type colonnade" | `colonnade` | SVG | 中（ANet 任务归属） |
| L13 Hourglass Stream | `:816` "9 · hourglass stream" | `hourglass` | SVG | 中（任务漏斗） |
| L14 Hundred Field | `:858` | `hundredfield` | SVG | 低 |
| L15 Ballot Tally | `:896` | `ballottally` | SVG | 低 |
| L16 Matrix Heat | `:937` | `matheat` | SVG | 中（湍流 高度×时间） |
| L17 Calendar Heat | `:993` | `calheat` | SVG | 中（采集日历） |
| L19 Ridgeline | `:1046` | `ridge` | SVG | 中 |
| L20 Parallel Coordinates | `:1098` | `parallel` | SVG | 高（多维性能画像、多机画像） |
| G3 Chunky Bars | `glance-gallery.html:252` "mono-demo · 3" | `c3` | Chart.js | 低（F1/F5 替代） |
| G4 Dot Waffle | `:282` | `waffle` | SVG | 低 |
| G5–G9、G11–G14、G16 | `:310–:740` | `forest/circular/tree/rainfall/morph/force/wave/custompie/singleaxis/race` | ECharts | 低 |
| G10 Diverging Bar | `:502` | `negbar` | ECharts | 中（有正负的偏差：航迹误差） |
| G15 Jitter Strip | `:694` | `jitter` | ECharts | 中（编队间距逐条分布） |
| **G17 Dynamic Stream** | `:782` "mono-fancy4 · 2" | `stream` | ECharts | **极高**（全部实时遥测曲线） |
| **G18 Draw-in + Counter** | `:820` "mono-fancy4 · 3" | `stroke` | ECharts + rAF | 高（KPI 计数器） |
| G19 Violin | `:879` | `violin` | SVG | 低（后备） |
| G20 Matrix Heat (Glance) | `:945` | `gheat` | SVG | 中 |
| G21 Rank Strip | `:978` | `rankstrip` | SVG | 中（多机排名） |
| G22 Aggregate Sankey | `:1021` | `sankey` | SVG | 低 |
| B1 / B2 / B3 | `big-circular.html` / `big-force.html` / `big-threads.html` | `ch` | ECharts / ECharts / SVG | V1.0 |
| M1 / M2 | `maps-gallery.html:74/130` | `mapus/mapworld` | ECharts + GeoJSON | skip |

### 2.5 报告模板中可复用的部件

- **`table.log`**（`report-10.zh.html` 第 77–88 行，全库唯一的表格）：
  `th{font-size:8.5px; font-weight:700; letter-spacing:.12em; color:var(--mut); text-align:right; padding:0 0 7px; border-bottom:1px solid var(--txt)}`，
  `th:first-child{text-align:left}`，
  `td{font-size:10.5px; padding:6.5px 0; border-bottom:1px dotted var(--quiet); text-align:right; font-weight:600}`，
  `td:first-child{text-align:left; color:var(--lab); font-weight:700; font-size:9px; letter-spacing:.1em}`，
  `tr.total td{border-bottom:0; border-top:1px solid var(--txt); font-weight:800; color:var(--data)}`，
  `td.hot{color:var(--hero); font-weight:800}`；缺失值写 `—`。
- **KPI 卡**（`report-09.zh.html` 的 `.kpi`）：`.h`（14 px/800，上方 1 px 实线）+ `.d`（9.5 px 说明）+ `.box`（1.5 px 点线框，数值 22 px/800 右对齐，-.02em；下方 11 px 高的单条示意：`DATA` 段加 `FAINTDATA` 段）。报告注释里明确"KPI 卡是数据家具，不占图额度"。
- **速览条**（`report-12.zh.html` 的 `.miles .m`）：`tag` 8.5 px/700/.14em、`.v` 26 px/800/-.03em，单位用 `<small>` 13 px/700，说明 10 px。
- **页面级规则**：`body{font-variant-numeric: tabular-nums lining-nums}`（R09、R10、R12 都有）；节标题 14 px/800、字距 .2em、下方 2 px 实线（R12 的 `.sect`）；图下"来源行"写明图型编号和"REAL TEMPLATE"。

### 2.6 `big-threads.html` 的交互状态机

- 可见线下面叠一条**同形透明孪生线**做热区：`stroke:'#000'`、`stroke-opacity:0`、`stroke-width:9`、`class:'hit'`（第 92–93 行）；标签热区是透明 rect（第 105、115 行）。
- CSS 状态：`.thread{transition: opacity .18s ease, stroke-width .18s ease}`；`svg.focused .thread:not(.hot){opacity:.035}`；`svg.focused .nodelab:not(.hot){opacity:.25}`。
- 行为：hover 单线时亮出整条路径；hover 标签时拉出整束；点击钉住（`pinned`，状态栏显示"PINNED · CLICK DARK SPACE TO RELEASE"）；点空白处释放；`mouseleave` 时如果没有钉住就清除。
- 底部状态栏 `#status`（34 px 高，上方 1 px 分隔线）用来显示读数，**不用浮动 tooltip**，所以没有遮挡。

### 2.7 `scripts/`

- `validate.mjs`：检查必备文件是否齐全；gallery 是否包含指定容器 id（`basics: treemap/histo/boxplot/stream/candle` 等）；catalog 是否有指定行；SKILL.md 是否保留"主力与后备"规则和 custom 色板角色；**可执行代码里出现 `Math.random(` 即失败**；彩色样张里出现不属于该预设的色值即失败；Mono 文件里出现"明显彩色"（RGB 通道极差 >18）即失败；同一文件内 id 重复即失败；`<script>` 用 `vm.Script` 做语法检查。
- `smoke-new-charts.mjs`：用 Playwright 打开每个 gallery，`scrollIntoView` 触发懒渲染，统计 `path,rect,circle,line,polygon,canvas,svg,text` 节点数（≥3 才算画出来），同时收集控制台错误。

---

## 3. 可复用算法与实现（含伪代码和参数）

### 3.1 视觉语言规范（从 lieflat 提炼，按本项目调整）

#### 3.1.1 字体与字号阶梯

lieflat 的字号是 viewBox 单位；本项目分两种密度，HUD 使用 CSS px。

| 角色 | lieflat 原值（viewBox 400×320） | **HUD 密度**（侧栏、状态栏，CSS px） | **Editorial 密度**（分析页、报告，CSS px） | 字重 / 字距 |
|---|---|---|---|---|
| 卡片标题 h2 | 16.5 | 13 | 16 | 700（HUD 600）/ -.02em |
| 大图标题 | 19 | — | 19 | 700 / -.02em |
| 副标题 sub | 11.5 | 11 | 12 | 400，颜色 MUT |
| 轴标签、类目名 | 7–9.5 | 10 | 10.5 | 600–700；拉丁字母全大写、字距 .08em；**中文不做全大写，字距 .02em** |
| 图内数值 | 9.5–11 | 11–12 | 11–13 | **800**，tabular-nums |
| 大数（KPI、仪表中心） | 22–34 | 22–28 | 28–34 | 800 / -.02em |
| 单位（跟在大数后） | — | 11 | 13 | 600–700，MUT |
| 来源行 src | 9.5 | 9.5–10 | 10 | 500 / .08em，全大写；**颜色用 MUT，不用 FAINT**（对比度要求） |
| 图内脚注 | 7 | 10 | 10 | 600 / .12em，全大写 |
| 最小字号 | 半宽 6.5 / 通栏 5.5 | **10** | **10**（报告导出的静态 SVG 允许 viewBox 8） | — |

- 字体：**Inter Variable 自托管**（`@fontsource-variable/inter`；本机 `fc-list` 里没有 Inter，Google Fonts 在离线环境下不可用）。中文回退栈为 `"PingFang SC","Microsoft YaHei","Noto Sans CJK SC","WenQuanYi Zen Hei",sans-serif`（本机实测有 WenQuanYi Zen Hei）。**不打包 Noto Sans SC**，体积是 MB 级。
- 数字：全局 `font-variant-numeric: tabular-nums lining-nums`（和 R09、R10、R12 一致）。Inter 的 tabular figures 就是等宽数字，**数字列不需要换成等宽字体**。真正的等宽字体（Geist Mono 或 JetBrains Mono，自托管）只用于 ID、十六进制、经纬度、日志原文，并开启 `"zero"`（带斜杠的零）。
- 装不下的信息改成 hover 显示，**不许缩小字号硬塞**（SKILL.md §2）。

#### 3.1.2 线宽、hairline 与间距

| 元素 | lieflat 值 | 本项目规则 |
|---|---|---|
| 发丝（Lupi 数据发丝、地板刻度） | 0.5–0.7（`stroke-width .55/.6`） | `max(0.5, 1/devicePixelRatio)` CSS px。DPR 为 1 时是 1 px 的低对比线，靠颜色（FLOOR/GRID）而不是线宽来弱化 |
| 网格线、基线 | 0.8，颜色 GRID | 1 px，GRID（暗色主题下为白色 8% 透明度） |
| 数据线（Basics 折线） | 1–1.2（彩色版 2–2.2） | HUD 1.5，分析页 1.25；强调线 2 |
| Glance 粗线 | 2.2–2.6 | 实时曲线 1.75（HUD 空间更紧） |
| 梯级、刻度 | 1（彩色版 1.8） | 1；ANet Graphite 属于 mono+accent，**不套用 INK_BOOST 的 ×1.8**，只在强调色元素上加粗到 1.5 |
| 虚线 | 点线引线 `1 3`、台阶 `2 3`、峰值圈 `2 3`、中位数旗 `2 4`、休眠段 `2 4` | 保留这些 dasharray 作为语义（§3.3） |
| 卡片内边距 | 28/28/20 | HUD 12–16；分析页 20–24 |
| 卡片间距 | 22 | HUD 8–12；分析页 20 |
| 卡片圆角 | 24 | HUD 面板 12（`--radius: .75rem`）；分析页图卡 20–24 |
| 柱端 | 胶囊，竖柱只圆上端，横柱只圆外端（`barRadius: 99`） | 保留 |

Canvas 取整规则：奇数像素宽的线坐标 +0.5，保证清晰；所有 hairline 用 `ctx.lineWidth = 1 / dpr`。

#### 3.1.3 网格、标注、来源

- **网格极少**：Lupi 和 Basics 的做法是一根基线加"日历地板"（每个时间单位一根 7 px 的短刻度，`FLOOR` 色），而不是整片网格。只有 F15、F17、G19 这类需要读数值的图才画 3–5 条水平网格（`#E3E2DB` 或 GRID，0.8 宽）。本项目的实时曲线只画 2 条网格，外加一条目标线（虚线 `2 4`）。
- **标注**：只标峰值或极值（F2 标 top-2 且间隔 ≥5 个单位，L3 标 top-3 且间隔 ≥6），**绝不在每个点上写数字**。数值文字带纸色光晕：`paint-order: stroke; stroke: BG; stroke-width: 3px`。
- **"沉默可见"**：值为 0 的格子画一个 r=0.8–0.9 的小点（F10、L4、L16、L17），而不是留空。
- **环境结构层（"家具"）**：小数据图的密度预算花在无数据的结构元素上，例如每 5 个单位一个小点、rim 刻度、虚线导轨（SKILL.md §3 "小数据走 Lupi 的路径"第 4 条）。本项目 HUD 的对应做法是：日历地板刻度（每秒或每 10 秒一根）加每 5 档一个小点。
- **来源行**：`图型名 · 系列 · 数据来源`，全大写。本项目的格式是 `TELEMETRY · P600-01 · SIM 10 HZ` 或 `PERF · NEW YORK · WEBGL2 SOFTWARE`。

#### 3.1.4 动效节奏

| 场景 | 参数（来源） | 本项目用法 |
|---|---|---|
| 入场 | 900 ms，quarticOut ≈ `cubic-bezier(.25,1,.5,1)`；大图 1200 ms | 分析页和报告卡片首次进入视口时播放一次 |
| 点阵错峰 | 8–15 ms/个（`staggerDot 12`） | 点数 >200 时把总错峰时长封顶在 600 ms（`delay = min(i*12, 600*i/N)`） |
| 条形错峰 | 80–130 ms/根（`staggerBar 100`） | 保留 |
| 描线 | 1 s `cubic-bezier(.4,0,.2,1)`，`pathLength=1` | 保留 |
| pop | 0.5 s `cubic-bezier(.2,.7,.3,1.3)`（有过冲） | HUD 里改为 `cubic-bezier(.2,.7,.3,1)`（不过冲） |
| 实时更新 | G17：每 300 ms 一个新读数，`animationDurationUpdate 260` 线性 | HUD 曲线：**不做补间**，每次调度直接重画 |
| 计数器 | G18：2600 ms cubicOut，`gen` 代次保护 | KPI 数值：|Δ| 超过阈值时做 400 ms cubicOut，否则直接赋值 |
| 重播 | 点击重播（`obsReveal`） | **HUD 禁用**（点击在 HUD 里代表选择）；分析页提供"重播"按钮 |
| 降级 | `prefers-reduced-motion` 时关闭 pop、fade、draw | 保留，并且计数器直接跳到终值 |

入场和界面过渡的时长 token 要和 transitions.dev 单元共用一张表：`--dur-instant 0`、`--dur-fast 120ms`、`--dur-base 240ms`、`--dur-chart-enter 900ms`、`--dur-chart-slow 1200ms`、`--ease-out-quart cubic-bezier(.25,1,.5,1)`。

### 3.2 色卡：ANet Graphite（custom 色板，全产品唯一色系）

这套色板按 SKILL.md §6.5 的 custom 规则建立："用户给出明确品牌色才建 custom；2 个颜色默认分为主数据色 + 强调色；允许派生明暗，不允许偷渡新色相；对比度是硬门；颜色不能成为唯一线索；一次只锁定一套。"逻辑沿用 WIRE 的 `mono+accent`。

**基础色阶**（冷调科技灰，OKLCH 色相约 262°，色度 ≤0.016；实测值来自 `.cache/research/d01/palette.mjs`）：

| token | hex | OKLCH L | 对暗面板 `g900` 对比度 | 对浅纸面 `g50` 对比度 | 角色 |
|---|---|---:|---:|---:|---|
| g950 | `#0A0B0D` | .149 | 1.05 | 17.73 | 暗色应用背景（"黑"） |
| g900 | `#111214` | .182 | 1.00 | 16.88 | 暗面板、图卡底；浅色主题的 TXT |
| g850 | `#16181B` | .208 | 1.05 | 16.02 | popover、悬浮层 |
| g800 | `#1D1F23` | .239 | 1.14 | 14.86 | muted、hover 底 |
| g700 | `#2A2D32` | .296 | 1.36 | 12.44 | 暗色强分隔线 |
| g600 | `#3E4249` | .378 | 1.86 | 9.09 | 暗色中只做 track 和空态，**不画数据** |
| g500 | `#5C616A` | .491 | 3.01 | 5.61 | 暗色最弱的一档数据；浅色的 MUT 文字 |
| g400 | `#81868F` | .619 | 5.12 | 3.30 | 中灰数据、focus ring |
| g300 | `#A7ABB3` | .740 | 8.14 | 2.07 | 暗色的次要文字；浅色最弱的一档数据 |
| g200 | `#CACDD3` | .848 | 11.77 | 1.43 | 暗色的次级数据 |
| g100 | `#E4E6E9` | .924 | 14.99 | 1.13 | 浅色 muted |
| g50 | `#F2F3F5` | .964 | 16.88 | 1.00 | 浅纸面；暗色的 TXT 和 DATA（"白"） |
| white | `#FBFBFC` | .988 | 18.12 | 1.07 | 浅色 popover |

**品牌红阶**（色相 29°，由 logo 红 `#E93024` 与 TXT 或 BG 混合派生）：

| token | hex | 对 g900 | 对 g50 | 用途 |
|---|---|---:|---:|---|
| r700 | `#B4241B` | 2.86 | **5.89** | 浅色主题的红色文字（HERO-TEXT） |
| r600 | `#D12A20` | 3.63 | 4.66 | 浅色主题的 destructive 按钮 |
| **r500** | **`#E93024`** | **4.38** | **3.85** | **品牌红**：logo 和所有数据标记（两种主题都用它） |
| r400 | `#FF5242` | **5.83** | 2.89 | 暗色主题的红色文字、destructive |
| r300 | `#FF8374` | 7.81 | 2.16 | 暗色的红色浅档（例如告警行文字的 hover） |

**校验结果**（dataviz 技能的 `validate_palette.js`）：
- 暗色数据阶 `g500→g50`（5 档）对 `#111214`：单调、相邻 ΔL ≥0.06，最浅一档 3.01:1，**PASS**；对 `#16181B` 为 2.86:1，PASS。
- 浅色数据阶 `g300→g900`（5 档）对 `#F2F3F5`：**PASS**，最浅一档 2.07:1。
- 7 档的做法（再加入暗色的 g600 或浅色的 g200）会失败：最浅一档只有 1.86:1 和 1.43:1。所以**数据阶只用 5 档**，g600 和 g200 只能作为 track、空态和"沉默点"。lieflat 的 7 级 `L` 在本项目里收缩为 5 级，和它的 `LAD` 5 级简版一致。
- 红色对中灰 `#81868F` 的 CVD 分离：暗色 protan ΔE 10.6，浅色 deutan ΔE 13.9，正常视觉 ΔE 约 23，**PASS**。另外红色和灰阶的明度也不同，去掉颜色后仍然可读。

**角色映射**（暗色是数字沙盘的默认主题；浅色用于分析页和报告导出）：

| 角色 | 暗色（HUD 默认） | 浅色（纸面） | 说明 |
|---|---|---|---|
| `BG` | `#111214` | `#F2F3F5` | 图所在面，也用作光晕色 |
| `TXT` | `#F2F3F5` | `#111214` | |
| `LAB` | `rgba(255,255,255,.72)` ≈ 9.9:1 | `rgba(10,11,13,.72)` ≈ 7.7:1 | 类目标签 |
| `MUT` | `rgba(255,255,255,.56)` ≈ 6.4:1 | `rgba(10,11,13,.60)` ≈ 5.0:1 | 副标题、来源行、轴标签 |
| `FAINT` | `rgba(255,255,255,.24)` | `rgba(10,11,13,.32)` | **只用于非文字**（地板刻度、"每 5 档一点"） |
| `FLOOR` | `rgba(255,255,255,.16)` | `rgba(10,11,13,.24)` | 日历地板 |
| `GRID` | `rgba(255,255,255,.08)` | `rgba(10,11,13,.12)` | 网格、基线 |
| `TRACK` | `rgba(255,255,255,.06)` | `rgba(10,11,13,.08)` | 未填充的轨道、进度底 |
| `DATA` | `#F2F3F5` | `#111214` | 主数据（明度最强） |
| `DATA2` | `#A7ABB3` | `#5C616A` | 次数据、对比系列（"去年""改版前"） |
| `FAINTDATA` | `#5C616A` | `#A7ABB3` | 背景数据、上下文发丝 |
| `RAMP` | `#5C616A #81868F #A7ABB3 #CACDD3 #F2F3F5` | `#A7ABB3 #81868F #5C616A #3E4249 #111214` | 序数阶（低→高） |
| `HERO` | `#E93024` | `#E93024` | **每张图唯一的主角标记** |
| `HERO_TEXT` | `#FF5242` | `#B4241B` | 红色文字 |
| `HALO` | `rgba(17,18,20,.92)` | `rgba(242,243,245,.92)` | 数值光晕 |

**红色使用规则**（"一处红"，由 WIRE 的"橙永远只给一个元素，第二处上橙就等于没有主角"推出）：

1. 一张图最多一个红色元素，按优先级取：**严重告警 > 当前选中的无人机 > 数据主角**（峰值、违例阈值、LIVE 点）。
2. 列表和表格里的"选中"**不用红**：用 `bg-muted` 加左侧 2 px 前景色竖条。红色留给告警和唯一的 hot 单元格。
3. 3D 场景里选中的无人机、它的轨迹和标签圈用红；其余无人机一律灰阶。这样 3D、图表和列表里的红色始终指向同一个对象。
4. 状态色**不引入新色相**（不用琥珀色，不用绿色）：

| 状态 | 视觉 | 必带 |
|---|---|---|
| critical | 红色实心 | morphicons 图标 + 文字标签 |
| warning | 红色描边、空心（沿用 lieflat "空心 = 次一级"的语义） | 图标 + 标签 |
| nominal | 墨色（DATA）实心 | 仅在需要时显示图标 |
| stale / offline | FAINTDATA 加虚线 `2 4`（沿用 lieflat "虚线 = 休眠"） | 标签加时长，例如 `STALE 3.2 S` |
| planned / predicted | 虚线 | 图例说明 |
| simulated vs real（V0.5） | 真实 = 实心，仿真 = 空心 | 图例说明 |

5. shadcn 的 `--chart-1…5` 映射为 `DATA、RAMP[3]、RAMP[2]、RAMP[1]、HERO`，这样沿用 shadcn chart 约定的组件也拿不到第三种色相。

**多系列（多机）配色**：≤4 条序列时按**实体固定**分配灰阶（`DATA、DATA2(g300)、g400、g500`），一次会话内不因排序而变化，同时必须直接标注名称；被选中的那一条改为 HERO。超过 4 条时改用 small multiples，或者用"一条主角 + 其余 FAINTDATA 发丝"（L20 平行坐标的做法：主角线宽 2，其余 0.65、透明度 0.5–0.8）。**绝不给无人机分配彩虹色。**

### 3.3 标记语法：lieflat 词汇表 → 本项目语义

| lieflat 标记 | 源码出处 | 原语义 | 本项目沿用为 |
|---|---|---|---|
| 梯级（rung） | F1 `B1 · rung bars` | 1 档 = 1 个单位 | 1 档 = 1 个诚实单位（50k 点、1 m/s、1 个节点）；单位写进副标题 |
| 刻度（tick） | F4/F5/F11/L15 | 1 tick = 1% 或 1 人 | 电量、预算、进度的 1% |
| 日历地板（barcode floor） | F2/F16/F17/L3 | 每天一根短刻度，无论当天有没有数据 | 每个采样周期（1 s 或 10 s）一根；**数据缺失时仍然画地板**，缺失一眼可见 |
| 空心点 | F2 周末、F12 改版前、F17 收涨、L11 重做 | 另一类或之前 | 仿真值、计划值、RTK float 解 |
| 虚线段 | L11 休眠、F9 减项、R09 理想线 | 休眠、参考 | stale、预测、目标线、计划航线 |
| 虚线圈 | F10/L16/L17 峰值 | 最强的一个 | 峰值格，或选中的实体（此时同时着 HERO） |
| 铅垂线（plumb） | F8 | 读 x 看线脚 | 3D 中航迹到地面的铅垂发丝（每 N 米一根），和 F8 同一套语汇 |
| 纸色中位横档 | F15/G19 | 中位数 | 中位数（暗色主题下就是 BG 色） |
| 串珠（bead） | F12 | 1 珠 = 省下的 1 分钟 | 1 珠 = 优化省下的 1 ms |
| 光晕文字 | 各处 `paint-order:stroke` | 数值压在数据上仍然可读 | 同样用法；canvas 版先 `strokeText` 再 `fillText` |
| 每 5 或 10 档一个小点 | F1/F4/F5/F14/L15 | 方便数格子 | 同样用法 |

### 3.4 标注与防碰撞算法

```ts
// 1) top-k 峰值，要求彼此间隔 ≥ gap（F2: k=2,gap=5；L3: k=3,gap=6）
function pickPeaks(vs: ArrayLike<number>, k: number, gap: number): number[] {
  const idx = Array.from({ length: vs.length }, (_, i) => i).sort((a, b) => vs[b] - vs[a]);
  const out: number[] = [];
  for (const i of idx) {
    if (out.every(j => Math.abs(j - i) >= gap)) out.push(i);
    if (out.length === k) break;
  }
  return out;
}
// 2) 环形标签锚点（F4）：按中角余弦决定对齐方式
const anchorOf = (deg: number) => { const c = Math.cos(deg * D2R); return c > .3 ? 'start' : c < -.3 ? 'end' : 'middle'; };
// 引线：点线 '1 3'，从 R0+20 连到 R0+38
// 3) 带状图标签（F16）：标在各系列最宽处，但宽处索引夹在 [4, N-6] 内，避免纸色文字溢出带外
// 4) 相邻数值标签强制最小间距（SKILL.md §8 第 3 条"barcode 教训"）：
function spreadLabels(ys: number[], minGap = 12): number[] { /* 排序后自上而下贪心推挤，再自下而上回推一次 */ }
```

### 3.5 核心图型算法（端口参数都来自源码）

**F1 梯级柱**（`basics-gallery.html:220`）
```text
x0(i) = 56 + i*56；base = 266；step = 5.6/单位；半宽 HW = 14，每档半宽 w = HW-1.5+rnd(k+1,i+2)*3
每档：line(x-w, y) → (x+w, y)，y = base - k*step，stroke DATA，宽 1，opacity .5+rnd*.5（mono+accent 下改为 .85+rnd*.15）
每 5 档（k%5==4）：在 (x+HW+4.5, y) 画 r=.8 的 FAINT 小点
数值标签：(x, base-(v-1)*step-10)，11/800；类目标签：(x, base+18)，7.5/700，字距 .08em；基线：GRID .8
本项目新增：自动单位 unit = nice(max/40)，从 {1,2,5}×10^k 中取；单根柱最多 40 档；副标题写"1 RUNG = 50K POINTS"
```

**F2 发丝折线 / F3 发丝面积**（`:250`、`:293`）
```text
F2：地板刻度 x(d) 高 7 px，FLOOR .6；折线 path 宽 1（HUD 1.5），用 .draw 描线 1.2 s；逐点圆 r=2.1，峰值 r=4.2；
    周末（本项目：仿真值 / 缺失值）画空心：fill=BG，stroke 1
F3：每个样本一根发丝，从 base 立到 map(v)，灰色 .55 宽，opacity .5+rnd*.45；峰值那一根用 DATA、宽 1.1；
    顶边用 1.2 宽的轮廓线；峰值画点并加数值标签
长序列（>宽度/3 个样本）：先分桶（§3.6.4），每根发丝代表一个桶，副标题写"ONE HAIRLINE = 5 S"
```

**F11 刻度仪表**（`:602`；用于电量、点预算、任务进度、加载进度）
```text
cx=200, cy=190, R0=104, A0=-195°, SW=210°（viewBox 坐标，按比例缩放）
for k in 0..99:
  a = A0 + k/100*SW；inked = k < value
  len = inked ? 13+rnd(k+1,3)*6 : 5+rnd(k+1,7)*2.5
  line(pol(R0,a) → pol(R0+len,a))，inked ? (DATA, 1) : (FLOOR, .6)
里程碑 25/50/75/100：pol(R0-7) 处 r=1 的小点，pol(R0-19) 处 7 px 数字
尖端珠：pol(R0+20, A0+value/100*SW)，r=2.4
中心：数值 34/800 + 剩余说明 8/600/.1em（例如 "27 TICKS TO GO"）
电量改编：保留线（reserve=20%）画成径向虚线；value ≤ reserve 时全部已上墨刻度改为 HERO（告警），
中心改为 "≈ 14 MIN LEFT"；value > reserve 时整张图没有红色
```

**F14 梯级直方图 + 中位旗**（`:720`）
```text
每个箱一把梯子（step 5.4，HW 10.5）；箱界刻度画在梯脚之间（直方图的箱是区间，不是类目）；
峰值箱标数；中位数：累计 ≥ 50% 的箱 mb，在 X0+mb*PW+PW*.7 处画虚线 '2 4'，标注 "HALF RESOLVED BY HERE"
帧耗时直方图的箱界有业务含义：[0, 8.3, 16.7, 33.3, 50, ∞) ms，对应 ≥120 / 60 / 30 / 20 / <20 fps
```

**F15 刻度箱线**（`:766`）
```text
五数概括 [min, q1, med, q3, max]：分位数用线性插值（type 7）；离群值用 Tukey 围栏 1.5·IQR
须线：宽 .8，MUT，用 draw 描线 .6 s；两端横帽 ±7
箱体：rect 宽 24、rx 9，fill 按中位数快慢取阶（越快越深）
中位数：BG 色横档，宽 2.2；右侧数值 9.5/800；离群值：空心圆 r 2.6，水平方向抖动 (rnd-.5)*8
```

**G17 动态流 → LfLiveLine**（`glance-gallery.html:782`）
```text
原版：窗口 50 个点，每 300 ms 追加一个；y 轴固定 [20,130]；线宽 2.4，smooth .45；
      endLabel 14/800；面积从 16% 透明度渐变到 0；左上角 LIVE 圆点 r4 加 "LIVE" 800/11 px；grid.right=58 给末端标签留位
本项目：窗口按时间计（默认 60 s），不按点数；y 轴按字段给固定域（高度 0–120 m，速度 0–15 m/s），
      超出时以 10% 的滞回扩展，避免抖动；不做 smooth（遥测要诚实，平滑会过冲）；
      末端点用 HERO 表示"现在"（WIRE 版 LIVE 圆点就是 HERO，glance-wire.html:838）；
      末端数值 12/800 带光晕；面积渐变 12% → 0；可选目标线（虚线）
```

**G18 计数器**（`:820`）
```ts
let gen = 0;
function countTo(el: HTMLElement, from: number, to: number, dur = 400, fmt = String) {
  const my = ++gen, t0 = performance.now();
  const tick = () => {
    if (my !== gen) return;                                   // 代次保护：只有最新一轮能写
    const p = Math.min(1, (performance.now() - t0) / dur), e = 1 - (1 - p) ** 3;  // cubicOut
    el.textContent = fmt(from + (to - from) * e);
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
```

**G19 / L19 的 KDE**
```text
bw = max(.9, s*.72)（原版 s 取的是造数据时的离散度参数 G[g][2]，移植时用样本标准差）；定义域两端各外扩 1.6·bw（让密度自然衰减，不出现平切端）；取 44 个网格点
dens(h) = Σ exp(-(h-v)²/(2bw²))；轮廓 = 镜像点列，用二次中点平滑（Q p, mid(p,q)）
L19：72 个采样，LIFT 54，行距 44；用 fill-opacity .96 的纸色 slab 遮住后排；每隔一个采样画一根竖发丝（.5 宽，opacity .28–.6）
```

**F16 河流（silhouette 基线）**
```text
tot[t] = Σ_s v_s[t]；y0[t] = CY - tot[t]*SC/2；逐系列堆叠：top = run，bot = run + v*SC
边界用二次中点平滑；系列之间用 2 px 的纸色缝分开；明度按当前份额排序（当前领先者最深）
```

**L20 平行坐标**
```text
轴 x = [64,156,248,340]，TOP 64，BOT 252；mapY = BOT - (v-lo)/(hi-lo)*(BOT-TOP)
相邻两轴之间用三次贝塞尔，控制点取两轴中点的 x：C(mx, y_k)(mx, y_{k+1})
主角：线宽 2，DATA（本项目用 HERO 或 DATA），点 r=3；其余：线宽 .65，FAINTDATA，opacity .5+rnd*.3，点 r=1.4
主角评分 score 要说明依据（原版："price is a fact, not a virtue"，价格维度不计入评分）
```

**G21 名次条**：行按最终名次排序；每格 32×32、rx 8，颜色按名次取 5 档 ladder，格内写名次数字；行尾的升降标记**改为 SVG 三角形**（原版用 `U+25B2`/`U+25BC` 三角字符）。

**L3 条码棒棒糖**：每天一根满高发丝（GRID .7），当天的值画点，下方接一段随机长度的茎（`y+14+rnd*26`）；top-3 峰值间隔 ≥6；周末空心。**RTK 改编**：每秒一根发丝，fixed 为实心点，float 为空心点，没有解时只画地板（缺口一眼可见）。

**库外翻译 1：LfWindRose（风玫瑰）**。按 SKILL.md §6 的四步走：
1. 本体：方向（角度）× 风速档（明度）× 频率（刻度数）。
2. 最近的亲戚：F4 Tick Donut（1 tick = 1%，灰阶分段，点线引线）和 L10 Radial Patchwork（24 h rim 刻度，角度 = 时刻）。
3. 用 token 造句：16 个扇区，每 22.5° 一个；每个扇区沿中角向外堆叠径向短刻度，1 tick = 1% 样本；刻度颜色按风速档取 RAMP（静风最浅，强风最深）；rim 用 L10 的 96 根刻度，4 个正方向写 N/E/S/W；中心写静风占比大数；**盛行风向**那个扇区的最外端刻度和标签用 HERO（全图唯一的红）。
4. 副标题：`one tick = 1% of samples · shade = speed band · last 10 min`。

**库外翻译 2：LfRangeHairline（区间发丝，用于阵风和帧耗时 min/max）**：由 F3（每个桶一根发丝）和 F17（影线表示全程范围）合成。每个桶画一根从 min 到 max 的发丝，均值处画一个点；最大阵风所在的桶用 HERO 点加数值标签；下方画地板刻度。

### 3.6 渲染引擎移植：SVG 还是 Canvas（实测）

#### 3.6.1 微基准（`.cache/research/d01/www/bench.html`）

设置：6 张图，每张 300×120 px，每张 600 个点（10 Hz 下即 60 s 窗口）；Chromium headless，SwiftShader；统计 6 s 内的数据。

| 方案 | 刷新 | 脚本耗时/次（均值） | 页面 fps | 备注 |
|---|---|---:|---:|---|
| 空页面基线 | — | — | 51 | |
| **lieflat 原法**：`innerHTML=''`，重建 path + 600 个 circle | 10 Hz | 34–36 ms | 2.3 | 6 s 内 long task 约 1.0–1.1 s |
| SVG，600 根 `<line>` 逐个改属性（F3 式） | 10 Hz | 24 ms | 3.0 | |
| SVG，只改一个 path 的 `d` | 10 Hz | 3.9–4.2 ms | 5.0–5.3 | 瓶颈是光栅化，不是 JS |
| SVG path | 1 Hz | 4.8 ms | 51 | 低频时没问题 |
| Canvas 2D（默认，GPU 加速） | 10 Hz | 0.9–1.3 ms | 5.6–7.4 | SwiftShader 下 GPU 光栅化极慢 |
| **Canvas 2D，`willReadFrequently:true`（CPU）** | 10 Hz | 0.86 ms | **49–59** | |
| 同上 | 30 Hz | 0.55 ms | 59 | |
| 同上，12 张 × 1200 点 | 10 Hz | 1.33 ms | **60** | |
| 3000 点：SVG path 与 GPU canvas | 10 Hz | 19 ms / 3 ms | 2.0 / 2.5 | |

叠加一个持续渲染 2 万点的 WebGL2 画布（640×360，基线 28–30 fps；20 万点时只剩 3.9 fps，不具区分度）：

| 方案 | 刷新 | fps |
|---|---|---:|
| 只有 WebGL | — | 28–30 |
| + CPU canvas 图表 | 10 Hz | 18–24 |
| + GPU canvas 图表 | 10 Hz | 5.5 |
| + SVG path | 10 Hz | 4.7 |
| + SVG path，独立合成层（`will-change:transform; contain:strict`） | 10 Hz | 17 |
| + SVG path | 2 Hz | 9.8–14.3 |
| + SVG path，独立合成层 | 2 Hz | 20.3 |

**结论（在真实 GPU 上同样成立，只是差距更小）：**
1. 流式图一律用 **CPU canvas**：`canvas.getContext('2d', { willReadFrequently: true })`。它不和 WebGL 争 GPU，SwiftShader 下的收益最大；在真实 GPU 上，300×120 的 CPU 光栅化也只需微秒级。
2. SVG 只用于**静态或 ≤2 Hz** 的图，外层容器必须加 `contain: strict; will-change: transform`（独立合成层，重绘不会波及 3D 画布所在的层）。
3. **禁止**在任何刷新路径里重建节点（`innerHTML=''`、React key 变化导致整棵 SVG 重挂）。入场动画只在首次挂载时由 CSS 类驱动。
4. 全部图表在一帧内的总绘制预算 ≤2 ms：调度器按轮转方式分配，每帧最多重画 2 张图。

#### 3.6.2 调度器 `LfScheduler`（整个应用只有一个）

```ts
type Job = { el: Element; hz: number; last: number; visible: boolean;
             version: () => number; seen: number; draw: (now: number) => void };
class LfScheduler {
  private jobs: Job[] = []; private rr = 0; private raf = 0;
  private io = new IntersectionObserver(es => es.forEach(e => {
    const j = this.jobs.find(j => j.el === e.target); if (j) j.visible = e.isIntersecting; }));
  add(j: Job) { this.jobs.push(j); this.io.observe(j.el); this.kick(); return () => this.remove(j); }
  remove(j: Job) { this.io.unobserve(j.el); this.jobs = this.jobs.filter(x => x !== j); }
  private kick() { if (!this.raf) this.raf = requestAnimationFrame(this.tick); }
  private tick = (now: number) => {
    this.raf = 0;
    if (!document.hidden) {
      let drawn = 0; const n = this.jobs.length; const t0 = performance.now();
      for (let c = 0; c < n && drawn < 2 && performance.now() - t0 < 2; c++) {
        const j = this.jobs[(this.rr + c) % n];
        if (!j.visible || now - j.last < 1000 / j.hz) continue;
        const v = j.version(); if (v === j.seen) continue;        // 数据没变就不画
        j.seen = v; j.last = now; j.draw(now); drawn++;
      }
      this.rr = (this.rr + 1) % Math.max(1, n);
    }
    if (this.jobs.length) this.kick();
  };
}
export const lfScheduler = new LfScheduler();
```

#### 3.6.3 遥测环形缓冲（不进 React state）

```ts
export class Ring {                     // 每架无人机、每个字段一个
  t: Float64Array; v: Float32Array; head = 0; len = 0; version = 0;
  constructor(public cap = 1200) { this.t = new Float64Array(cap); this.v = new Float32Array(cap); } // 120 s @ 10 Hz
  push(t: number, v: number) { this.t[this.head] = t; this.v[this.head] = v;
    this.head = (this.head + 1) % this.cap; this.len = Math.min(this.len + 1, this.cap); this.version++; }
  at(i: number) { const k = (this.head - this.len + i + this.cap) % this.cap; return [this.t[k], this.v[k]] as const; }
}
// WS 消息（10–50 Hz）→ telemetryStore.rings.get(droneId).get(field).push()；React 只订阅 4 Hz 的快照（Zustand）
```

#### 3.6.4 降采样：Canvas 用逐像素列 min/max（M4 式），SVG 用分桶

```ts
// 在 CPU canvas 上画窗口内的曲线：每个像素列保留 first/min/max/last，画出的包络和原数据完全一致
function drawEnvelope(ctx, ring, t0, t1, x, y, W) {
  let col = -1, mn = 0, mx = 0, first = 0, last = 0; ctx.beginPath();
  for (let i = 0; i < ring.len; i++) {
    const [t, v] = ring.at(i); if (t < t0) continue;
    const c = Math.floor(x(t));
    if (c !== col) { if (col >= 0) flush(); col = c; mn = mx = first = last = v; }
    else { mn = Math.min(mn, v); mx = Math.max(mx, v); last = v; }
  }
  if (col >= 0) flush(); ctx.stroke();
  function flush() { ctx.lineTo(col + .5, y(first)); ctx.lineTo(col + .5, y(mn)); ctx.lineTo(col + .5, y(mx)); ctx.lineTo(col + .5, y(last)); }
}
// 回放和分析（SVG）：服务端或前端按桶聚合 {t, min, mean, max, n}，每个桶一根发丝（LfRangeHairline / F3）
```

#### 3.6.5 CPU canvas 的基本设置

```ts
function setupCanvas(cv: HTMLCanvasElement, cssW: number, cssH: number) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);            // 上限 2，控制 CPU 光栅化成本
  cv.width = Math.round(cssW * dpr); cv.height = Math.round(cssH * dpr);
  const ctx = cv.getContext('2d', { willReadFrequently: true, alpha: true })!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, dpr, hair: 1 / dpr };
}
// 带光晕的文字：ctx.lineJoin='round'; ctx.lineWidth=3; ctx.strokeStyle=tok.halo; ctx.strokeText(s,x,y); ctx.fillStyle=tok.txt; ctx.fillText(s,x,y)
// 字体：ctx.font = `800 12px "Inter Variable", "PingFang SC", sans-serif`；ctx.fontVariantNumeric 不可用，Inter 默认数字已是 lining，
//      tabular 需要在 @font-face 上设置 font-feature-settings:"tnum"，或者在 canvas 里右对齐数字，避免视觉跳动
```

CSS 变量无法直接给 canvas 用：`useLfTokens()` 在挂载时和主题切换时（用 MutationObserver 监听 `<html class>`）读一次 `getComputedStyle(document.documentElement)`，把结果缓存成对象。

### 3.7 交互规范

- **交互三问**（SKILL.md §5）是硬规则：地板刻度、rim 刻度、"每 5 档一点"这类装饰元素一律 `pointer-events: none`；元素少于 50 个且两端有标注的图可以只做静态；超过 50 个元素或多段路径的图必须有 hover。
- **tooltip**：整个应用共用一个浮层（portal），样式直接复用 shadcn `TooltipContent` 的 className（`bg-foreground text-background rounded-md px-3 py-1.5 text-xs`），这正好就是 lieflat 的 `tipLight`（墨底纸字）。内容格式：`标签 — 数值 单位 · 时间`（lieflat 统一用 em dash `—`）。**不用 SVG 原生 `<title>`**：样式不可控，而且延迟大约 1 s。
- **热区**：线的热区用 9 px 透明孪生线（B3）；点和格子的热区至少 12 px；触屏至少 24 px。Canvas 图做一次二分查找找最近的时间点，画一条纵向 hairline 光标（DATA 40% 透明度）。
- **聚焦**：hover 某个系列时其余降到 0.25（标签）或 0.035（发丝束）；分析页可以点击钉住，底部状态栏显示读数（B3 的 `#status` 模式）。**HUD 里的点击等于全局选中无人机**，不做钉住。
- 键盘：图卡可以获得焦点，左右方向键在时间点之间移动光标，Esc 清除；焦点环用 `--ring`（g400），不用红色。

### 3.8 数字格式规范

| 量 | 单位 | 精度 | 示例 | 备注 |
|---|---|---|---|---|
| 高度（AGL/AMSL） | m | 1 位小数 | `82.3 m` | 标明 AGL 或 AMSL |
| 水平/垂直速度 | m/s | 1 位 | `7.2 m/s`、`−1.4 m/s` | 负号用 U+2212 `−`（和 F9 一致） |
| 航向、风向 | ° | 整数，补足 3 位 | `045°`、`NW 315°` | |
| 电量 | % | 整数 | `78%` | 另加剩余时间 `≈ 14 MIN` |
| 电压、电流 | V、A | 1 位 | `22.4 V` | |
| 风速、阵风 | m/s | 1 位 | `8.2 m/s · G 11.6` | |
| 能见度（雾） | m | 3 位有效数字 | `850 m`、`12.0 km` | 用 MOR，不用无单位的 0.21（与 r16 对齐） |
| 降水 | mm/h | 1 位 | `22.0 mm/h` | |
| FPS | fps | 整数 | `58 FPS` | |
| 帧耗时 | ms | 1 位 | `16.7 ms` | |
| 点数 | pts | SI，3 位有效数字 | `4.82M`、`350K` | |
| 内存 | MB/GB | 3 位有效数字 | `512 MB`、`1.25 GB` | |
| 经纬度 | ° | 7 位小数，等宽字体 | `31.8206523` | |
| 本地 ENU 坐标 | m | 2 位 | `E 120.45 · N −33.10 · U 82.30` | |
| 时间 | — | `HH:MM:SS`；任务时间 `T+03:12` | `14:32:05` | 仿真时间和真实时间分开标注（`SIM` / `UTC`） |
| 缺失 | — | — | `—` | |

实现方式：每种量一个缓存好的 `Intl.NumberFormat` 实例，封装为 `fmt.alt(v)` 这样的函数；所有会实时变化的数字都用 tabular-nums，右对齐（或者固定宽度），避免抖动。

### 3.9 表格规范（R10 `table.log` 移植到 shadcn Table）

| 规则 | 取值 | 依据 |
|---|---|---|
| 对齐 | 第一列（名称）左对齐；**数字列右对齐**；状态列左对齐（图标 + 文字）；单位写在表头，不写在单元格里（`ALT M`、`SPD M/S`） | R10 `th{text-align:right}`、`th:first-child{left}` |
| 表头 | 10 px（报告 8.5）/700，字距 .12em，拉丁字母全大写，MUT 色；下方 **1 px 实线，颜色为 TXT** | R10 `th` |
| 行分隔 | 1 px **点线**，GRID（lieflat 的 `--quiet`） | R10 `td{border-bottom:1px dotted}` |
| 斑马纹 | **不用**。行分隔靠点线；斑马纹会让明度承担非数据的含义，违背"明度即数据" | lieflat 全库都没有斑马纹 |
| 数字字体 | Inter tabular-nums，600；ID、经纬度用等宽字体 | R09、R10、R12 的 `body` |
| 首列 | 700，LAB 色；拉丁字母全大写、字距 .1em；中文不加字距 | R10 `td:first-child` |
| 合计行 | 上方 1 px TXT 实线，800 | R10 `tr.total` |
| hot 单元格 | 每张表最多 1 个，HERO_TEXT 色、800 | R10 `td.hot` |
| 行高 | HUD 28 px；分析页 32 px；报告约 26 px（6.5 px 上下内边距） | |
| hover | `bg-muted/40`，不改变文字色 | shadcn 默认 `hover:bg-muted/50` |
| 选中 | `bg-muted` + 左侧 2 px 前景色竖条；**不用红** | §3.2 红色规则 |
| 缺失、负数 | `—`；`−`（U+2212） | R10；F9 |
| 行内微图 | 64×16 的 sparkline（CPU canvas）；或"刻度条"（F5 的 mini 版：1 tick = 1 个单位，≤20 ticks）；或 3 档灰度的点 | |
| 排序 | 表头按钮，图标用 morphicons（`ArrowUp` 与 `ArrowDown` 互相 morph） | |
| 大表 | 超过 200 行时虚拟滚动（TanStack Virtual）；表头 sticky；列宽固定，数字列 `min-width` 按最大格式化宽度计算 | |
| 空状态 | 画一行 FAINT 地板刻度，下方写 `NO DATA · WAITING FOR TELEMETRY` | lieflat "沉默可见" |

```tsx
// components/lf/LfTable.tsx —— 基于 shadcn Table 的 className 覆盖
export const lfTable = {
  table: "text-[11px] [font-variant-numeric:tabular-nums_lining-nums]",
  headerRow: "border-b border-foreground hover:bg-transparent",
  head: "h-8 px-2 text-[10px] font-bold uppercase tracking-[.12em] text-muted-foreground text-right first:text-left",
  row: "border-b border-dotted border-[color:var(--lf-grid)] hover:bg-muted/40 data-[state=selected]:bg-muted data-[state=selected]:shadow-[inset_2px_0_0_var(--foreground)]",
  cell: "px-2 py-1.5 text-right font-semibold first:text-left first:font-bold first:text-[color:var(--lf-lab)]",
  totalRow: "border-t border-foreground font-extrabold",
  hot: "text-[color:var(--lf-hero-text)] font-extrabold",
};
// <Table className={lfTable.table}><TableHeader><TableRow className={lfTable.headerRow}>…
```

---

## 4. 在本项目中的落点与复用方式

### 4.1 模块落点

```text
apps/web/src/
├── styles/globals.css              # shadcn 变量 + --lf-* 角色变量（亮 / 暗）+ .lf-* 标记类 + 动画类
├── lib/lf/
│   ├── tokens.ts                   # ANET_DARK / ANET_LIGHT（角色对象），FONT、MOTION、SHAPE（移植自 mono-tokens.js）
│   ├── rnd.ts                      # rnd（带 abs）+ mulberry32（mock 数据用）
│   ├── geom.ts                     # pol、sect、blob、贝塞尔工具
│   ├── scale.ts                    # linear、band、time、nice、niceUnit（1/2/5×10^k）
│   ├── stats.ts                    # quantile（type 7）、fiveNum + Tukey、kde、histogram、pickPeaks、spreadLabels
│   ├── fmt.ts                      # §3.8 数字格式
│   ├── ring.ts                     # 环形缓冲
│   └── scheduler.ts                # LfScheduler（单例）
├── components/lf/
│   ├── LfChartCard.tsx             # shadcn Card 包装：卡片四件套
│   ├── LfStat.tsx                  # KPI 卡（R09 .kpi + G18 计数器 + 可选 sparkline）
│   ├── LfSparkline.tsx  LfLiveLine.tsx               # Canvas（G17）
│   ├── LfHairlineLine.tsx  LfHairlineArea.tsx  LfRangeHairline.tsx  LfBarcode.tsx   # F2 F3 译 L3
│   ├── LfRungBars.tsx  LfTickRows.tsx  LfPairedRungs.tsx  LfStackedRungs.tsx         # F1 F5 F6 F7
│   ├── LfTickGauge.tsx  LfTickDonut.tsx  LfDumbbell.tsx  LfPlumbScatter.tsx          # F11 F4 F12 F8
│   ├── LfHistogram.tsx  LfTickBox.tsx  LfStreamRibbon.tsx  LfDotHeat.tsx             # F14 F15 F16 F10
│   ├── LfMatrixHeat.tsx  LfCalendarHeat.tsx  LfRidgeline.tsx  LfParallel.tsx         # L16/G20 L17 L19 L20
│   ├── LfLineage.tsx  LfColonnade.tsx  LfHourglass.tsx  LfRankStrip.tsx  LfJitter.tsx  LfDiverging.tsx
│   ├── LfWindRose.tsx                                 # 库外翻译
│   ├── LfTable.tsx                                    # §3.9
│   └── primitives/ LfSvg.tsx LfCanvas.tsx Floor.tsx ValueLabel.tsx Legend.tsx Tooltip.tsx useReveal.ts useWidth.ts useLfTokens.ts
└── routes/analytics/perf-report.tsx               # R09 结构的性能测试报告页（可打印为 PDF）
scripts/lint-lf.mjs                                # 移植自 validate.mjs
e2e/lf-smoke.spec.ts                               # 移植自 smoke-new-charts.mjs
```

### 4.2 CSS 变量（shadcn 变量与 lieflat 角色变量并存）

```css
/* styles/globals.css —— ANet Graphite；全产品唯一色系 */
:root {                                   /* 浅色 = 纸面（分析页、报告导出） */
  --radius: .75rem;
  --background:#F2F3F5; --foreground:#111214;
  --card:#F2F3F5; --card-foreground:#111214;              /* lieflat：卡片与页面同色，靠留白分卡 */
  --popover:#FBFBFC; --popover-foreground:#111214;
  --primary:#111214; --primary-foreground:#F2F3F5;
  --secondary:#E4E6E9; --secondary-foreground:#111214;
  --muted:#E4E6E9; --muted-foreground:#5C616A;
  --accent:#E4E6E9; --accent-foreground:#111214;
  --destructive:#D12A20;
  --border:rgba(10,11,13,.12); --input:rgba(10,11,13,.16); --ring:#81868F;
  --brand:#E93024;
  --lf-bg:var(--card); --lf-txt:#111214;
  --lf-lab:rgba(10,11,13,.72); --lf-mut:rgba(10,11,13,.60); --lf-faint:rgba(10,11,13,.32);
  --lf-floor:rgba(10,11,13,.24); --lf-grid:rgba(10,11,13,.12); --lf-track:rgba(10,11,13,.08);
  --lf-data:#111214; --lf-data2:#5C616A; --lf-faintdata:#A7ABB3;
  --lf-hero:#E93024; --lf-hero-text:#B4241B; --lf-halo:rgba(242,243,245,.92);
  --lf-ramp-1:#A7ABB3; --lf-ramp-2:#81868F; --lf-ramp-3:#5C616A; --lf-ramp-4:#3E4249; --lf-ramp-5:#111214;
  --chart-1:var(--lf-data); --chart-2:var(--lf-ramp-3); --chart-3:var(--lf-ramp-2); --chart-4:var(--lf-ramp-1); --chart-5:var(--lf-hero);
}
.dark {                                   /* 暗色 = 数字沙盘默认 */
  --background:#0A0B0D; --foreground:#F2F3F5;
  --card:#111214; --card-foreground:#F2F3F5;
  --popover:#16181B; --popover-foreground:#F2F3F5;
  --primary:#F2F3F5; --primary-foreground:#111214;
  --secondary:#1D1F23; --secondary-foreground:#F2F3F5;
  --muted:#1D1F23; --muted-foreground:#A7ABB3;
  --accent:#1D1F23; --accent-foreground:#F2F3F5;
  --destructive:#FF5242;
  --border:rgba(255,255,255,.08); --input:rgba(255,255,255,.12); --ring:#81868F;
  --lf-txt:#F2F3F5;
  --lf-lab:rgba(255,255,255,.72); --lf-mut:rgba(255,255,255,.56); --lf-faint:rgba(255,255,255,.24);
  --lf-floor:rgba(255,255,255,.16); --lf-grid:rgba(255,255,255,.08); --lf-track:rgba(255,255,255,.06);
  --lf-data:#F2F3F5; --lf-data2:#A7ABB3; --lf-faintdata:#5C616A;
  --lf-hero:#E93024; --lf-hero-text:#FF5242; --lf-halo:rgba(17,18,20,.92);
  --lf-ramp-1:#5C616A; --lf-ramp-2:#81868F; --lf-ramp-3:#A7ABB3; --lf-ramp-4:#CACDD3; --lf-ramp-5:#F2F3F5;
}
@theme inline { --color-lf-hero: var(--lf-hero); --color-lf-data: var(--lf-data); --color-lf-grid: var(--lf-grid); /* …其余同理 */ }

/* 标记类（SVG） */
.lf-svg{contain:strict;will-change:transform}              /* 独立合成层（§3.6） */
.lf-svg text{font-family:var(--font-sans);font-variant-numeric:tabular-nums lining-nums}
.lf-floor{stroke:var(--lf-floor);stroke-width:.6} .lf-grid{stroke:var(--lf-grid);stroke-width:1}
.lf-line{fill:none;stroke:var(--lf-data);stroke-width:1.5;stroke-linejoin:round}
.lf-solid{fill:var(--lf-data)} .lf-hollow{fill:var(--lf-bg);stroke:var(--lf-data);stroke-width:1}
.lf-hero{fill:var(--lf-hero);stroke:var(--lf-hero)}
.lf-value{font-weight:800;fill:var(--lf-txt)} .lf-halo{paint-order:stroke;stroke:var(--lf-halo);stroke-width:3px;stroke-linejoin:round}
.lf-cap{font-weight:600;letter-spacing:.12em;text-transform:uppercase;fill:var(--lf-mut)}
.lf-pop{transform-box:fill-box;transform-origin:center;animation:lf-pop .5s cubic-bezier(.2,.7,.3,1) both}
.lf-fade{animation:lf-fade .9s ease both}
.lf-draw{stroke-dasharray:1;stroke-dashoffset:1;animation:lf-draw 1s cubic-bezier(.4,0,.2,1) both}
@keyframes lf-pop{from{transform:scale(0)}} @keyframes lf-fade{from{opacity:0}} @keyframes lf-draw{to{stroke-dashoffset:0}}
[data-lf-play="false"] :is(.lf-pop,.lf-fade,.lf-draw){animation:none;stroke-dasharray:none;stroke-dashoffset:0}
@media (prefers-reduced-motion:reduce){:is(.lf-pop,.lf-fade,.lf-draw){animation:none;stroke-dasharray:none;stroke-dashoffset:0}}
```

### 4.3 组件 API

```ts
// 公共类型
export type LfRole = 'data' | 'data2' | 'faint' | 'hero';
export type LfDensity = 'hud' | 'editorial';
export interface LfConfig { [key: string]: { label: string; unit?: string; role?: LfRole; icon?: IconNode; format?: (v: number) => string } }
// 借用 shadcn ChartConfig 的形状（label/icon + 颜色），但颜色只能取角色，不能给任意 hex
export interface LfBase { ariaLabel: string; height?: number; density?: LfDensity; animate?: boolean; className?: string }
```

| 组件 | 关键 props | 引擎 | 母本 |
|---|---|---|---|
| `LfChartCard` | `title`（结论式；HUD 可用指标名）、`sub`（图例 · 时间范围）、`src`、`action?`（shadcn `CardAction`，放 ToggleGroup 时间范围）、`density`、`wide?` | shadcn Card | 卡片四件套 |
| `LfStat` | `label, value, unit, format, delta?, status?, spark?: () => Ring, countUp?` | DOM + Canvas | R09 `.kpi`、R12 `.miles`、G18 |
| `LfSparkline` | `source: () => Ring, windowSec, width=64, height=16, hero?: 'last'` | CPU Canvas | G17 精简版 |
| `LfLiveLine` | `source, windowSec=60, hz=4, domain:[lo,hi], target?, unit, format, height=80, stale=2000` | CPU Canvas | G17 |
| `LfHairlineLine` | `data: {t,v,hollow?}[], peaks=2, peakGap=5, xLabels, hero:'peak'\|'last'\|'none', floorNote` | SVG | F2 |
| `LfHairlineArea` / `LfRangeHairline` | `buckets: {t,min?,mean,max?,n}[], bucketLabel:'5 S'` | SVG | F3 / 译自 F3+F17 |
| `LfBarcode` | `data: {t,v,state:'solid'\|'hollow'\|'none'}[], peaks=3` | SVG（>300 点改 Canvas） | L3 |
| `LfRungBars` / `LfTickRows` | `data:{label,value}[], unit?:number (auto), unitLabel, hero?: key\|'max'` | SVG | F1 / F5 |
| `LfPairedRungs` / `LfDumbbell` | `data:{label,a,b}[], aLabel, bLabel, beadUnit?` | SVG | F6 / F12 |
| `LfTickGauge` | `value, max=100, reserve?, center, remainder, milestones=[25,50,75,100]` | SVG | F11 |
| `LfTickDonut` | `data:{label,value}[]`（和为 100） | SVG | F4 |
| `LfHistogram` | `values \| bins, edges, edgeLabels, medianFlag=true, unitLabel` | SVG | F14 |
| `LfTickBox` | `groups:{label, values \| five, outliers?}[], domain, unit` | SVG | F15 |
| `LfStreamRibbon` | `series:{key,values}[], times` | SVG | F16 |
| `LfMatrixHeat` / `LfDotHeat` / `LfCalendarHeat` | `rows, cols, value(r,c), buckets, peakRing=true` | SVG | L16/G20、F10、L17 |
| `LfParallel` | `dims:{key,label,lo,hi}[], items:{id,values}[], hero?: id, score?` | SVG | L20 |
| `LfRankStrip` | `rows:{id,ranks[]}[], periods` | SVG | G21 |
| `LfLineage` / `LfColonnade` / `LfHourglass` | 事件序列 / 多对一 / 漏斗阶段 | SVG | L11 / L12 / L13 |
| `LfWindRose` | `samples:{dirDeg,speed}[] \| hist, sectors=16, bands=[2,4,6,8,10]` | SVG | 译自 F4+L10 |
| `LfTable` | `columns:{key,label,unit?,align?,format?,mini?:'spark'\|'ticks'}[], rows, total?, hotKey?, virtual?` | shadcn Table | R10 `table.log` |

**`LfChartCard` 的组合方式**（shadcn Card 原样使用，只覆盖 className）：

```tsx
export function LfChartCard({ title, sub, src, action, density = 'hud', wide, children }: Props) {
  const hud = density === 'hud';
  return (
    <Card data-lf-card className={cn(
      'gap-2 shadow-none', hud ? 'rounded-xl border border-border bg-card py-3' : 'rounded-3xl border-0 bg-card py-6 gap-3',
      wide && 'col-span-full')}>
      <CardHeader className={hud ? 'px-3 gap-0.5' : 'px-6 gap-1'}>
        <CardTitle className={hud ? 'text-[13px] font-semibold tracking-[-.01em]' : 'text-base font-bold tracking-[-.02em]'}>{title}</CardTitle>
        {sub && <CardDescription className="text-[11px] leading-snug">{sub}</CardDescription>}
        {action && <CardAction>{action}</CardAction>}
      </CardHeader>
      <CardContent className={hud ? 'px-3' : 'px-6'}>{children}</CardContent>
      {src && <CardFooter className={hud ? 'px-3' : 'px-6'}>
        <span className="text-[10px] font-medium uppercase tracking-[.08em] text-[color:var(--lf-mut)]">{src}</span>
      </CardFooter>}
    </Card>
  );
}
```

**移植示例：F2 → `LfHairlineLine`**（命令式 `el()` 改为 JSX；几何与动画节奏逐项对应 `basics-gallery.html:250–291`）：

```tsx
export function LfHairlineLine({ data, height = 180, peaks = 2, peakGap = 5, xLabels = [], hero = 'peak',
  floorNote, format = String, animate = true, ariaLabel }: LfHairlineLineProps) {
  const [ref, w] = useWidth<HTMLDivElement>();                 // ResizeObserver，像素布局
  const play = useRevealOnce(ref) && animate;                  // 已包含 prefers-reduced-motion 判断
  const P = { l: 24, r: 24, t: 22, b: 36 }, base = height - P.b;
  const vs = data.map(d => d.v), hi = Math.max(...vs) * 1.12, lo = Math.min(0, ...vs);
  const x = (i: number) => P.l + i * (w - P.l - P.r) / Math.max(1, data.length - 1);
  const y = (v: number) => base - (v - lo) / (hi - lo) * (base - P.t);
  const top = pickPeaks(vs, peaks, peakGap);
  const d = 'M' + data.map((p, i) => `${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`).join(' L ');
  return (
    <div ref={ref} className="lf-svg" style={{ height }}>
      {w > 0 && <svg width={w} height={height} role="img" aria-label={ariaLabel} data-lf-play={String(play)}>
        {data.map((_, i) => <line key={`f${i}`} x1={x(i)} x2={x(i)} y1={base} y2={base - 7}
          className="lf-floor lf-fade" style={{ animationDelay: `${i * 8}ms` }} />)}
        <line x1={P.l - 6} x2={w - P.r + 6} y1={base} y2={base} className="lf-grid lf-fade" />
        <path d={d} pathLength={1} className="lf-line lf-draw" style={{ animationDuration: '1.2s' }} />
        {data.map((p, i) => {
          const big = top.includes(i), isHero = hero === 'peak' ? i === top[0] : hero === 'last' && i === data.length - 1;
          return <circle key={i} cx={x(i)} cy={y(p.v)} r={big ? 4.2 : 2.1}
            className={cn(p.hollow ? 'lf-hollow' : 'lf-solid', isHero && 'lf-hero', 'lf-pop')}
            style={{ animationDelay: `${200 + i * 30}ms` }} data-tip={`${p.t} — ${format(p.v)}`} />;
        })}
        {top.map(i => <text key={`v${i}`} x={x(i)} y={y(vs[i]) - 11} textAnchor="middle" fontSize={11}
          className="lf-value lf-halo lf-fade" style={{ animationDelay: `${1000 + i * 10}ms` }}>{format(vs[i])}</text>)}
        {xLabels.map(([i, s]) => <text key={`x${i}`} x={x(i)} y={base + 18} textAnchor="middle" fontSize={10} className="lf-cap">{s}</text>)}
        {floorNote && <text x={w / 2} y={height - 4} textAnchor="middle" fontSize={10} className="lf-cap">{floorNote}</text>}
      </svg>}
    </div>
  );
}
```

**移植示例：G17 → `LfLiveLine`**（CPU canvas，交给调度器驱动，完全绕开 React 渲染）：

```tsx
export function LfLiveLine({ source, windowSec = 60, hz = 4, domain, target, format, height = 80, stale = 2000, ariaLabel }: Props) {
  const ref = useRef<HTMLCanvasElement>(null), tok = useLfTokens(), [wrap, w] = useWidth<HTMLDivElement>();
  useEffect(() => {
    const cv = ref.current; if (!cv || !w) return;
    const { ctx, hair } = setupCanvas(cv, w, height), R = 56;          // 右侧留给末端标签（G17 的 grid.right=58）
    let [lo, hi] = domain;
    return lfScheduler.add({ el: cv, hz, last: 0, visible: true, seen: -1,
      version: () => source()?.version ?? -1,
      draw: () => {
        const ring = source(); if (!ring || !ring.len) return;
        const [tEnd, vEnd] = ring.at(ring.len - 1), t0 = tEnd - windowSec * 1000;
        if (vEnd > hi) hi = vEnd + (hi - lo) * .1; if (vEnd < lo) lo = vEnd - (hi - lo) * .1;   // 10% 滞回扩展
        const x = (t: number) => (t - t0) / (tEnd - t0) * (w - R), y = (v: number) => height - 6 - (v - lo) / (hi - lo) * (height - 16);
        ctx.clearRect(0, 0, w, height);
        ctx.lineWidth = hair; ctx.strokeStyle = tok.grid; ctx.beginPath();
        for (const g of [lo, hi]) { const yy = Math.round(y(g)) + .5; ctx.moveTo(0, yy); ctx.lineTo(w - R, yy); } ctx.stroke();
        if (target != null) { ctx.setLineDash([2, 4]); ctx.strokeStyle = tok.mut; ctx.beginPath();
          const yy = Math.round(y(target)) + .5; ctx.moveTo(0, yy); ctx.lineTo(w - R, yy); ctx.stroke(); ctx.setLineDash([]); }
        const isStale = performance.now() - tEnd > stale;   // 这里假设 ring 的时间戳与 performance.now() 同源；若是 SIM 时间则换成本地接收时间
        ctx.lineWidth = 1.75; ctx.strokeStyle = isStale ? tok.faintdata : tok.data;
        if (isStale) ctx.setLineDash([2, 4]);
        drawEnvelope(ctx, ring, t0, tEnd, x, y, w - R); ctx.setLineDash([]);
        const ex = x(tEnd), ey = y(vEnd);
        ctx.fillStyle = isStale ? tok.faintdata : tok.hero; ctx.beginPath(); ctx.arc(ex, ey, 3, 0, 7); ctx.fill();   // "现在" = HERO
        ctx.font = '800 12px "Inter Variable", "PingFang SC", sans-serif'; ctx.textBaseline = 'middle';
        ctx.lineWidth = 3; ctx.strokeStyle = tok.halo; ctx.strokeText(format(vEnd), ex + 6, ey);
        ctx.fillStyle = tok.txt; ctx.fillText(format(vEnd), ex + 6, ey);
      } });
  }, [w, height, hz, windowSec, source, tok]);
  return <div ref={wrap} style={{ height }}><canvas ref={ref} role="img" aria-label={ariaLabel} style={{ width: '100%', height }} /></div>;
}
```

### 4.4 场景 → 选型对照（MVP 及后续）

选型按 SKILL.md 的顺序执行："主力 L1–L15、F1–F13 → 后备 → Glance"。HUD 引用 §0 第 4 条例外（"用户明确要求 Glance / dashboard / 监控"）。"直用"指 §0 第 3.2 条允许直接使用的五种后备图。

| # | 场景 | 数据形状 | 选型 | 引擎 / 刷新 | 版本 |
|---|---|---|---|---|---|
| 1 | FPS、GPU ms 实时 | 实时单序列 | **G17 → LfLiveLine**，目标线 16.7 ms 或 50 ms（软件档 20 fps） | Canvas / 4 Hz | V0.1 |
| 2 | 可见点数 / point budget | 单值进度 | **F11 → LfTickGauge**（1 tick = 1% 预算） | SVG / 2 Hz | V0.1 |
| 3 | 各 LOD 层点数 | 少类目比较 | **F1 LfRungBars**（1 档 = 50K 点，自动单位） | SVG / 1 Hz | V0.1 |
| 4 | 在途请求、已加载节点、JS/GPU 内存 | KPI + 趋势 | **LfStat + LfSparkline** | Canvas / 4 Hz | V0.1 |
| 5 | World Package 加载进度 | 单值进度 | F11 | SVG | V0.1 |
| 6 | 本次会话的帧耗时分布 | 单变量分箱（箱界有业务含义） | **F14 LfHistogram**（箱界 8.3/16.7/33.3/50 ms） | SVG / 按需 | V0.1 |
| 7 | 6 个城市的帧耗时对比 | 分组五数概括 | **F15 LfTickBox**（直用） | SVG / 报告 | V0.1 测试报告 |
| 8 | 优化前后（每个城市） | 类目级两时点 | **F12 LfDumbbell**（1 珠 = 1 ms） | SVG / 报告 | V0.1 |
| 9 | WebGL2 与 WebGPU 对比 | 分组对比 | F6 LfPairedRungs | SVG / 报告 | V0.1 |
| 10 | 城市多维性能画像（fps、p95、首帧、内存、点数） | 3–6 个连续维度 | **L20 LfParallel**（直用） | SVG / 报告 | V0.2 |
| 11 | 高度、速度、电量、链路实时 | 实时单序列 ×3–4 | **G17 LfLiveLine**，small multiples | Canvas / 4 Hz，聚焦时 10 Hz | V0.2 |
| 12 | 电量 | 单值进度 | **F11 LfTickGauge**（保留线，低于它才出现红色） | SVG / 1 Hz | V0.2 |
| 13 | 无人机 KPI 行（ALT/SPD/BAT/MODE） | 单值 | **LfStat** | DOM / 4 Hz | V0.2 |
| 14 | 飞行日志回放 | 长时序 | **F3 LfHairlineArea**（分桶，1 根发丝 = N s）/ LfRangeHairline | SVG / 静态 | V0.2 |
| 15 | 飞行模式时间占比 | 100% 构成 ≤6 段 | F4 LfTickDonut | SVG | V0.2 |
| 16 | WebSocket 延迟分布 | 分箱 | F14 | SVG | V0.2 |
| 17 | 无人机列表、事件日志、航点表 | 表 | **LfTable**（日志用虚拟滚动） | DOM | V0.2 |
| 18 | 风速与阵风 | 区间时序 | LfRangeHairline（库外翻译） | SVG / 1 Hz | V0.3 |
| 19 | 风向分布 | 方向 × 风速 | LfWindRose（库外翻译） | SVG / 0.5 Hz | V0.3 |
| 20 | 环境参数（MOR、mm/h、云量） | KPI | LfStat + 迷你 F11 | DOM | V0.3 |
| 21 | 风致漂移与风速 | 二维散点 ≤20 点 | F8 LfPlumbScatter；点多时改 G15 | SVG | V0.4 |
| 22 | 湍流强度（高度层 × 时间） | 两个离散维度 × 数值 | L16（分析页）/ G20（HUD） | SVG | V0.4 |
| 23 | RTK 解算状态时序 | 逐秒状态 | **L3 LfBarcode**（fixed 实心，float 空心，无解只剩地板） | SVG/Canvas | V0.5 |
| 24 | 配准残差 | 分组分布 | F15 | SVG | V0.5 |
| 25 | 采集日历 | 全年日期 | L17 LfCalendarHeat（直用） | SVG | V0.5 |
| 26 | 多机当前值（≤8 架） | 横向排名 | **F5 LfTickRows**（选中的那架用 HERO） | SVG / 2 Hz | V0.6 |
| 27 | 多机（>8 架） | 表 | LfTable + 行内刻度条或 sparkline | DOM + Canvas | V0.6 |
| 28 | 多机随任务阶段的排名 | 离散时间上的排名 | G21 LfRankStrip | SVG | V0.6 |
| 29 | 编队间距误差 | 逐条分布 | G15 LfJitter / F15 | SVG | V0.6 |
| 30 | 多机任务份额随时间 | 构成随时间 + 总量 | F16 LfStreamRibbon（直用） | SVG | V0.6 |
| 31 | 航迹偏差（有正负） | 带正负的分类数值 | G10 LfDiverging | SVG | V0.6 |
| 32 | 任务归属到无人机（ANet） | 多对一 + 名单 | L12 LfColonnade | SVG | V1.0 |
| 33 | 任务漏斗（发布 → 接受 → 执行 → 完成） | 分阶段递减 | L13 LfHourglass | SVG | V1.0 |
| 34 | Agent 任务生命史 | 事件序列 | L11 LfLineage | SVG | V1.0 |
| 35 | 能力网络 / 发现路径 | 网络、多段路径 | B2 / B3（交互模式 port） | SVG/Canvas | V1.0 |
| 36 | 性能测试报告、任务复盘 | 整页 | R09（AT A GLANCE 2×2 + KPI 栏）/ R12 / R04 的结构 | React 路由 + 打印 | V0.1 / V0.6 |

**HUD 布局参考**（侧栏宽 320–360 px，图宽 288–328 px）：sparkline 高 24，实时曲线 72–96，仪表 120，直方图 140，刻度行每行 28。一个面板内最多 4 张图（SKILL.md "每屏最多一张暗卡"的规则在暗色主题下失去意义；对应的约束改为"每个面板内最多一个红色元素"）。

### 4.5 可复用条目清单

| 条目 | 来源 | 目标模块 | 方式 | MVP |
|---|---|---|---|---|
| Mono 角色体系与 WIRE 的 mono+accent 逻辑 | `mono-tokens.js`、`color-presets.js` WIRE | `lib/lf/tokens.ts`、`globals.css` | port（新建 ANet Graphite 色板） | 是 |
| 字号、线宽、圆角、动画参数 | `mono-tokens.js` FONT/SHAPE/MOTION | `tokens.ts` | port（按 HUD 密度换算） | 是 |
| `rnd`（带 abs 的版本） | `mono-tokens.js` §6 | `lib/lf/rnd.ts` | adopt 原样 | 是 |
| `pol/sect/blob` | `mono-tokens.js` §7 | `lib/lf/geom.ts` | adopt 原样 | 是 |
| reveal（只触发一次 + timer 清理） | `obsReveal`、`keep` | `useRevealOnce` | port | 是 |
| 卡片四件套 | `CARD_CSS` | `LfChartCard`（shadcn Card） | port | 是 |
| F1、F2、F3、F5、F11、F14 | `basics-gallery.html` | `components/lf/*` | port | 是 |
| G17 动态流、G18 计数器 | `glance-gallery.html:782/820` | `LfLiveLine`、`LfStat` | port（改为 CPU canvas） | 是 |
| `table.log` 表格规范 | `report-10.zh.html:77–88` | `LfTable` | port | 是 |
| KPI 卡 | `report-09.zh.html` `.kpi` | `LfStat` | port | 是 |
| pickPeaks、光晕文字、沉默点 | F2/L3、`paint-order` | `stats.ts`、`ValueLabel` | port | 是 |
| 静态检查脚本 | `scripts/validate.mjs` | `scripts/lint-lf.mjs` | port（禁 Math.random、禁 token 外的 hex、禁 emoji 和 U+25B2/U+25BC 三角字符、重复 id） | 是 |
| Playwright 烟测 | `scripts/smoke-new-charts.mjs` | `e2e/lf-smoke.spec.ts` | port | 是 |
| F4、F6、F8、F12、F15、F16 | `basics-gallery.html` | `components/lf/*` | port | 否（V0.1 报告 / V0.2+） |
| L3、L11、L12、L13、L16、L17、L19、L20 | `lupi-gallery.html` | `components/lf/*` | port | 否 |
| G10、G15、G20、G21 | `glance-gallery.html` | `components/lf/*` | port | 否 |
| Threads 热区与 hover/pin 状态机 | `big-threads.html` | `primitives/interaction.ts` | port | 否 |
| 报告版式 R09 / R12 | `templates/reports/` | `routes/analytics/*` | reference | 否 |
| ECharts / Chart.js 的实现 | G3–G18、F13 | — | skip（改写为 SVG/Canvas） | — |
| 地图 M1/M2 | `maps-gallery.html` | — | skip | — |

### 4.6 版本节奏

- **V0.1**：token、色板、`LfChartCard`、`LfStat`、`LfSparkline`、`LfLiveLine`、`LfTickGauge`、`LfRungBars`、`LfTickRows`、`LfHistogram`、`LfTable`，加上调度器、环形缓冲、`lint-lf`、e2e 烟测。性能 HUD 上线；性能测试报告页用 F15、F12、F6。
- **V0.2**：遥测面板（高度/速度/电量/链路）、电量仪表、飞行日志回放（F3 + LfRangeHairline）、事件日志表、WS 延迟直方图、L20。
- **V0.3–V0.4**：风玫瑰、阵风区间、环境 KPI、F8、L16/G20。
- **V0.5**：L3（RTK）、F15（配准）、L17。
- **V0.6**：F5 多机、G21、G15、F16、G10；任务复盘报告。
- **V1.0**：L12、L13、L11、B2/B3 的交互模式。

---

## 5. 对比与推荐

本单元只有一个仓库，比较的是它内部各子系统与本项目的契合度，以及可替代方案。

| 子系统 | 契合度 | 理由 | 排序 |
|---|---|---|---|
| Basics（F1–F17） | 最高 | 本项目大部分数据是"几个类目 / 一条序列 / 一个进度"，F 系的剪影一眼可认，单位可数，全部是纯 SVG | 1 |
| Glance 的 G17、G18 | 最高（HUD） | 实时曲线和计数器在其他系里没有对应物 | 2 |
| token + WIRE 预设 | 最高 | 直接决定色卡 | 2（并列） |
| Lupi（L3、L11–L13、L16、L17、L19、L20） | 高（分析页、报告、V1.0） | 逐记录的语法适合回放分析和 ANet 叙事 | 3 |
| `table.log` + KPI 卡 | 高 | 全库唯一的表格规范 | 4 |
| big-threads 的交互 | 中 | V1.0 才需要 | 5 |
| 其余 Glance（ECharts） | 低 | 引入 ECharts（约 1 MB）不划算，而且大多是演示型图 | 6 |
| Maps | 无 | 本项目的"地图"是 3D 世界 | — |

**可替代方案**（没有 clone，只说明取舍理由）：shadcn `chart.tsx` 基于 Recharts（`refs/design/ui/apps/v4/registry/new-york-v4/ui/chart.tsx` 引入 `recharts` 的 `ResponsiveContainer`）。它每次数据更新都要走 React 渲染整棵 SVG，流式场景下代价高，默认样式也和 lieflat 语法冲突。**结论：不用 Recharts；只借用 shadcn `ChartConfig` 的形状（series key → label/icon/颜色）作为 `LfConfig` 的 API 约定**。ECharts 和 Chart.js 同样不引入。流式曲线的核心就是 §3.6 的 CPU canvas 加 min/max 包络，约 100 行代码，不值得为它引入 uPlot 一类的依赖。

---

## 6. 风险与注意事项

1. **性能（最高风险）**：lieflat 所有模板都假设图"画一次就不动"。照原样移植到 HUD 会把 3D 帧率从 28 fps 拖到 2–5 fps（§3.6）。必须执行：流式图用 CPU canvas、SVG 进独立合成层、单一调度器、不可见时暂停、每帧 ≤2 ms。**`backdrop-filter`、`box-shadow` 大面积模糊、CSS filter 在软件渲染下同样昂贵，HUD 里禁用。**
2. **CSS 动画数量**：lieflat 给每个节点单独挂 CSS 动画（600 个点就是 600 个动画）。分析页首屏如果有 6 张这样的图，入场期间会卡。错峰总时长要封顶，动画结束后移除 `lf-*` 动画类（`animationend` 冒泡计数），或者只给 ≤200 个节点加动画。
3. **字号**：viewBox 缩放会把 7 px 变成 5 px。HUD 必须像素布局，CSS 最小 10 px；中文比英文更需要字号，中文的类目名还需要更宽的标签位（L2 的竖排类目名只允许 ≤4 个字，SKILL.md §4 已警告）。
4. **离线**：lieflat 依赖 Google Fonts 和 jsDelivr。本项目的 Inter 必须自托管；中文回退到系统字体，headless 环境里靠 WenQuanYi（本机已有）。截图基线要在同一套字体下生成。
5. **无障碍**：lieflat 的 FAINT 文字只有 1.5–2.4:1，Mono 的副标题色 `#8F8E88` 也只有 2.86:1。本项目的文字最低用 MUT（暗色 6.4:1，浅色 5.0:1）；红色文字按主题换用 r400 或 r700。色彩永远不能是唯一线索：状态必须带图标和标签，系列必须直接标注。
6. **红色过载**：品牌红同时承担"主角"和"严重告警"两种语义。如果同一张图里两者都出现，按 §3.2 的优先级处理；PRD 评审时要逐页检查"每张图最多一处红"。
7. **暗色主题是新增的**：lieflat 以浅色纸面为主，暗卡只是例外，没有"全暗 dashboard"的先例。明度即数据在暗底上反转（最亮 = 最重要），纸色中位横档变成 BG 色横档，光晕色也要反转。这些都需要逐个组件在两种主题下截图验收。
8. **源码缺陷**：`rnd` 的负值问题（§0 第 10 条）；`pop` 的缓动有过冲，和"不弹跳"的说法矛盾；catalog 数量与编号不一致；G1 被引用但不存在。移植时以本文 §2.4 的对照表为准。
9. **Unicode 字符与 emoji 规定**：`U+25B2 U+25BC U+25CF U+25CB U+25C9 ↑ ← │` 虽然不是 emoji，但部分字体会用彩色字形渲染它们（本机装了 Noto Color Emoji）。统一改为 SVG 形状或 morphicons，由 `lint-lf` 检查。
10. **规则的适用边界**：SKILL.md 的"默认先 Lupi、一张图一个结论、单页 ≤6 张图"是面向对外发布的编辑物制定的，HUD 是监控场景，适用例外条款。但"标题写结论"在分析页和报告里要坚持（例如"New York 的 p95 帧耗时比 Shenzhen 高 41%"，而不是"帧耗时箱线图"）。
11. **许可**：PolyForm Noncommercial 不允许商用。本项目是科研用途，用户已说明忽略；如果将来商用，需要重写而不是移植（视觉语言本身不受版权保护，但代码受保护）。
12. **mock 数据**：lieflat 要求演示数据确定性（`rnd`），这和本项目的 mock 仿真、Timeline seek、截图回归是一致的。前端 mock 用 `mulberry32(seed)`，不用 `rnd`（`rnd` 的散列质量只够做视觉抖动）；后端 mock 同样必须固定种子。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§38 UI 设计缺少"数据可视化与遥测显示"规范。** 建议新增一节，内容包括：ANet Graphite 色卡（§3.2）、"一处红"规则、状态色不引入新色相、图表清单（§4.4）、HUD 与 Editorial 两种密度、数字格式（§3.8）、表格规范（§3.9）。§38 草图里的 `Fog 0.21` 没有单位，建议改为能见度 `MOR 850 m`（与 r16 一致）；`Battery 78%` 旁边加剩余时间。
2. **§28 DroneState 要为图表补字段和单位。** 建议加入：`battery{percent, voltage_v, current_a, remaining_s}`、`link{rssi_dbm, latency_ms, loss_pct}`、`gps{fix_type, sats, hdop}`、`wind_est{u,v,w}`、`mode_history`（带时间戳，供 F4 和 L11 使用）。所有字段用 SI 单位；每个字段在 schema 里声明"显示精度"和"合理域"（LfLiveLine 的固定 y 域和格式化都依赖它）。
3. **§36–37 刷新频率补充前端图表节拍。** WS 10–50 Hz 的数据只写入环形缓冲（不触发 React）；Zustand 快照 4 Hz；HUD 图 4 Hz，聚焦图 10 Hz；静态 SVG 图 ≤1 Hz；不可见时 0 Hz。回放和历史数据走 REST，服务端按桶聚合 `{t,min,mean,max,n}`，前端画 F3 或 LfRangeHairline。
4. **§39 Timeline 本身就应该是一张图。** 刻度尺用 L3/F2 的日历地板（每秒或每 10 秒一根刻度，缺数据的时段一眼可见）；背景叠一条选中无人机的高度发丝面积（F3，FAINTDATA）；事件标记用实心点（起飞、降落）、空心点（航线变更），告警用红点；拖动时显示 G18 式的时间计数。
5. **§40 Drone Interaction：选中态全局统一为红色。** 3D 里的机体标记、轨迹、标签圈，图表里的主角系列，列表里的选中行（这里用前景色竖条而不是红色），三者指向同一个对象。3D 标注沿用 lieflat 语汇：航迹到地面的铅垂发丝（F8）、选中目标的虚线圈、数值文字带光晕。
6. **缺少告警与状态设计。** 建议增加告警分级（critical / warning / nominal / stale），以及颜色 + morphicons 图标 + 文字的三重编码规则，并规定告警在 HUD、3D、Timeline 三处的呈现方式。
7. **§43 MVP 应把"性能埋点 + HUD + 流畅性测试报告"列为交付物。** 用户明确要求"流畅性测试"，r11/r12 已定义 `window.__perf` 和相对指标断言；本单元补上它们的可视化：性能 HUD（V0.1）和 R09 结构的测试报告页（F15 城市箱线、F14 帧耗时直方、F12 优化前后、L20 多维画像）。
8. **§42 仓库结构里加入设计系统目录。** 例如 `apps/web/src/styles`、`lib/lf`、`components/lf`，另加 `scripts/lint-lf.mjs`（禁 `Math.random`、禁 token 之外的 hex、禁 emoji 和 U+25B2/U+25BC 三角字符、重复 id）和 `e2e/lf-smoke`。
9. **时间语义。** 文档里没有区分仿真时间和墙钟时间。图表的时间轴一律使用仿真时间，界面上用 `SIM` / `UTC` 标注；stale 判断用本地接收时间。回放和倍速播放时，图表窗口跟随仿真时间推进。
10. **"浏览器负责看世界"的原则需要补充"浏览器内的预算分配"。** 3D 渲染、环境粒子、图表、DOM 共用一个主线程和一块 GPU。建议在 §4 或 §37 明确每帧的预算：图表 ≤2 ms，DOM 更新 ≤2 ms，其余留给 3D，并由 r11 的质量控制器统一降级（降级顺序里加一步"图表降到 1 Hz"）。
11. **色卡与品牌资产的落地。** logo 使用 `refs/design/ANet/docs/media/anet-logo.svg`（黑底圆角平行四边形，描边为 `#E93024`），其红色与 HERO 完全一致。建议在设计文档中固定：品牌红只有 `#E93024` 一个值，其余红色阶只是为满足文字对比度派生出来的，不作为独立的品牌色使用。
