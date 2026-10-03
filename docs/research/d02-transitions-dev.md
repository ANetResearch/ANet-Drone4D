# D02 研究笔记：transitions.dev（全部动效与切换）与《动效规范》草案

> 研究单元：d02 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §4、§14、§28、§36–§40、§43；视觉约束见用户需求（动效/切换全部采用 transitions.dev，图标 morphicons，UI 全量 shadcn，产品色为科技灰、黑、白、红 #E93024）
>
> 仓库快照：`refs/design/transitions.dev` @ `e2d5551`（2026-09-21，4382 stars，作者 Jakub Antalik）。最后一次提交是 "fix(react): the five transitions with an empty React tab"，补齐了 5 个 React 模板，Co-Authored-By 为 Claude Opus 5。仓库在 2026 年 5–9 月持续活跃。下文路径均相对该仓库根目录。
>
> 交叉核对的旁证仓库（只读）：`refs/design/ui`（shadcn/ui @ 2026-09-28）、`refs/design/morphicons`，以及本机其他项目 node_modules 中的 `tw-animate-css@1.4.0`、`sonner@2.0.8`、`@radix-ui/react-presence`、`tailwindcss@4`（`theme.css`）。
>
> 实测产物在 `.cache/research/d02/`：`www/bench.html`（WebGL2 点云画布叠加 DOM 动效的压测页）、`bench.mjs`（playwright-core 驱动脚本）、`results.md`（原始结果）。环境是 headless Chromium 1234，使用 SwiftShader 软件渲染（本机无 GPU），分辨率 1280×720。

---

## 0. 结论速览

| 仓库 / 子模块 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **transitions.dev · `skills/transitions-dev/`**（32 个免费动效，`01-…32-*.md`） | 可移植的 CSS 过渡配方库。每个配方包含：`:root` 语义变量、`t-*` 命名空间类、状态钩子（`data-*`/`is-*`）、`prefers-reduced-motion` 守卫，部分附带一小段 JS 编排 | **adopt（vendor 源码）**：CSS 原样拷入 `apps/web/src/styles/motion/t/`。由 Radix 承载的 6 类浮层（Dialog/Sheet/Dropdown/Popover/Select/Tooltip）**改写为 `@keyframes` 版**（原因见 §4.2） | V0.1 起 | 5/5 |
| **`skills/transitions-polish/` + `_root.css` motion tokens** | 5 维动效 token（duration/easing/distance/scale/blur），外加"何时用哪个值"的规则：开合不对称、hover 进出、stagger 与 delay | **adopt**：作为全站**唯一**的动效 token 源（`tokens.css` 和 Tailwind `@theme`），规则写进本文 §4 的《动效规范》和 lint | V0.1 | 5/5 |
| index.html 中的 `<script data-react-key="pN|tok-*">` React 模板 | React 用法样板：`readMs()`、`closed/open/closing` 三态机、`useLayoutEffect` flush，以及自动注入 `<style id="transitions-pN">` | **port**：改写为 TS hooks（`usePresence`/`useReplay`/`useTextSwap`/`MotionNumber`…） | V0.1 | 4/5 |
| `refine/`（npm `transitions-refine` 0.3.34） | 开发期注入式时间线和 Refine 面板。后端是 LLM 或确定性算法 `refineTimings()`，把动效值对齐到 token | **reference**：借鉴 `server/motion-tokens.mjs` "用途优先、数值兜底"的映射，写我们自己的 motion-lint 脚本。工具本体不引入 | V0.2（lint） | 3/5 |
| `build/extract.mjs`、`scripts/transitions-data.json`、`transitions/*/index.html` | 生成链：从 showcase 抽取 skill；43 个条目的数据（字段为 css/react/pro） | **reference**：可作为"动效目录页 / Storybook"的数据源 | V0.2 | 2/5 |
| `cli/`（`npx transitions-dev`） | 免费配方从本地 md 拷贝；Pro 走设备码鉴权 API | **skip**（离线 vendor 即可，CI 无网络依赖） | — | 1/5 |
| **11 个 Pro 动效**（confetti-burst、spinner-to-check-morph、gooey-plus-menu 等） | 付费。仓库里只有 index.html 中**混淆压缩**的演示代码（`--pv*` 变量），没有配方源码 | **skip**。spinner→check 用 icon-swap 加 success-check 组合替代（官方 SKILL.md 就是这样建议的） | — | — |

**实现者先读这 10 条**

1. **token 是地基。** 全站动效只允许引用 `_root.css` 中的 7 个 duration、6 个 easing、5 个 distance、4 个 scale、3 个 blur（§3.1），外加 32 个配方的语义变量。matching 规则是**按用途匹配，不按最近数值**：300ms 的 modal close 应映射到 `--duration-quick`（150ms），而不是 `--duration-medium`（350ms）。见 `skills/transitions-polish/SKILL.md` 中的 "Core doctrine"。
2. **32 个免费配方可以覆盖本系统全部 13 类交互**（映射表见 §4.1）。Pro 不可用，也不需要。
3. **Radix Presence 只等 `animationend`，不等 `transitionend`**（`@radix-ui/react-presence/dist/index.mjs` 第 98–99 行只监听 `animationend/animationcancel`）。transitions.dev 的 modal/dropdown/tooltip/toast 是**基于 transition** 的，直接套在 shadcn 浮层上时关闭动画会被立即卸载吞掉。因此这 6 类浮层必须改写成 `@keyframes t-*-in/out`，并挂在 `[data-state]` 上（§4.2 给出完整 CSS）。
4. **实测（§4.7）：在无 GPU 的 SwiftShader 环境里，blur 是 UI 动效的头号成本。** 两块 320px 侧栏做 transform+opacity 开合，WebGL 帧率只降约 10%；加上 `filter: blur(2px)` 后降约 65%；静态 `backdrop-filter: blur(12px)` 玻璃面板也降约 63%。结论：
   - 设立 **motion tier（full / lite / reduced）**，并和点云 point-budget 调速器共用 FPS 信号。**先降 UI 动效，后降点云**。
   - shadcn 配置 `menuColor` 必须为 `"default"`：`*-translucent` 会内联 `backdrop-blur-2xl`，见 `packages/shadcn/src/utils/transformers/transform-menu.ts`。
   - 同时移除组件里的 `backdrop-blur-*`。
5. **遥测数字分 4 类处理**（§4.4.3）：
   - 连续量（高度、速度、姿态，5–50Hz）**不做动画**，只节流到 ≤10Hz，并使用 `tabular-nums`。
   - 离散量（电量整数%、卫星数、航点序号、告警数）用 **number pop-in**，只让变化的那几位数字动，频率 ≤2Hz。
   - 事件型 KPI 用 **spinning counter**，只在事件发生时播一次。
   - 状态文字用 **text-states-swap**。
6. **3D 画布永远全屏，面板是浮层。** 面板开合只动 transform/opacity，**禁止用动画改变 width 进而触发 canvas resize**（card-resize 只用于小型 HUD 部件）。
7. **图标切换以 morphicons 为主**，必须设 `reducedMotion="user"`，spring 取 `"snappy"`（ζ=0.73，t90≈133ms，稳定≈417ms，与 `--duration-fast` 同一量级）。transitions.dev 的 **icon-swap** 只用于无法形变的跨族替换（spinner<->check、状态徽标），同时也是 lite/reduced 档的回退方案。
8. **"spring" 的实现是 `cubic-bezier(0.34, B, 0.64, 1)` 这一族曲线**（prototypes.html 第 218–224 行注释）。实测超调量：B=1.36→4.3%，1.45→6.6%，1.96→23.5%，3.85→101%。其中 bounce(1.36) 约等于 morphicons snappy，bounce(1.96) 约等于 morphicons bouncy（23.5% 对 24.3%），两套体系可以对齐（§3.2）。
9. **告警 shake 是"一次性事件"，不是状态。** 危险状态靠持续的红色 `#E93024` 加图标加文字表达。shake 只在告警**进入**时播放一次（280ms），3D 画布和大面板永远不抖（§4.1 第 10 行）。
10. **Tailwind v4 的 `--ease-out` / `--ease-in-out` 与 transitions.dev 同名 token 冲突**：前者是 `cubic-bezier(0,0,.2,1)`，后者是关键字 `ease-out`。解决办法见 §4.3，在 `@theme` 中统一定义，并同时改写 `--default-transition-*`。

---

## 1. 仓库概览

### 1.1 基本信息

| 项 | 内容 |
|---|---|
| 仓库 | github.com/Jakubantalik/transitions.dev（网站 https://transitions.dev） |
| Star / 活跃度 | 4382 stars。最后提交 2026-09-21。`assets/proto-images/` 中有 2026-05-17 的截图；`refine/` 已发布到 npm 0.3.34 |
| 形态 | ① 静态展示站（`index.html` 684KB，所有 demo、配方模板、token 都内联在这一个文件里）；② agent skill（`skills/transitions-dev/`、`skills/transitions-polish/`）；③ CLI（`cli/`）；④ Refine 实时调参工具（`refine/`） |
| 构建 | 根目录 `package.json` 只有一个脚本 `npm run build`，即 `node build/extract.mjs`。**无运行时依赖**，配方是纯 CSS 加原生 JS |
| License | CLI 为 MIT；配方适用 `terms.html`（Transitions.dev license）。**本项目为科研用途，按要求忽略 license 限制**，但 Pro 源码本来就不在仓库里，所以不可用 |
| 与本项目的契合度 | 视觉语言是极简灰阶、小位移、2–3px 模糊、偏快的 smooth-out 曲线，和 shadcn 的中性风格、科技灰/黑/白/红产品色天然一致；所有参数都是 CSS 变量，便于做性能分档 |

### 1.2 免费 / Pro 清单（`scripts/transitions-data.json` 共 43 条，`transitions/` 下有 43 个子目录和 1 个 hub 页）

**免费 32 个**（skill 编号 / 站点 slug / 一句话说明）：

| # | skill 文件 | 站点 slug（与 skill 名不同时标出） | 动效 |
|---|---|---|---|
| 01 | card-resize | | width/height 补间 |
| 02 | number-pop-in | | 数字逐位带模糊从某方向弹入 |
| 03 | notification-badge | | 角标斜向滑入，圆点弹簧 pop |
| 04 | text-states-swap | | 文字三相切换（上出下入并模糊） |
| 05 | menu-dropdown | | 以触发点为原点缩放展开 |
| 06 | modal | modal-open-close | 居中缩放打开，关闭更快 |
| 07 | panel-reveal | | 面板位移、淡入淡出加交叉模糊 |
| 08 | page-side-by-side | | 两页左右切换（列表<->详情） |
| 09 | icon-swap | | 同槽位两个图标交叉缩放模糊 |
| 10 | success-check | | 淡入、旋转、模糊、Y 回弹并绘制路径 |
| 11 | avatar-group-hover | | 横排元素 hover 按距离衰减抬升，返回时回弹 |
| 12 | error-state-shake | | 分段 cubic-bezier 抖动，边框和提示自动恢复 |
| 13 | input-clear-dissolve | input-clear-with-dissolve | 清空输入时逐词光带溶解（逐帧 JS） |
| 14 | skeleton-reveal | skeleton-loader-and-reveal | 骨架脉冲后交叉淡入内容 |
| 15 | shimmer-text | | 纯 CSS 高光扫过文字 |
| 16 | tabs-sliding | | 分段控件的滑动胶囊 |
| 17 | tooltip | tooltip-open-close | 延迟出现、立即消失，同组气泡在触发器间移动 |
| 18 | texts-reveal | | 标题和副标题错峰模糊上升 |
| 19 | card-tilt | 3d-tilt | 指针驱动 3D 倾斜并带眩光 |
| 20 | plus-menu-morph | dropdown-menu-morph | 圆形按钮形变为菜单面板 |
| 21 | accordion | | grid-rows 0fr<->1fr 展开，箭头上下翻转 |
| 22 | toast | toast-open-close | 自下而上淡入，模糊并轻微缩放 |
| 23 | like-button | | 心形填充、pop 加粒子 |
| 24 | learn-more-hover | | 箭头 chevron 张开 |
| 25 | checkbox-check | | 先填充方框，再描画对勾 |
| 26 | spinning-counter | | 老虎机式数字滚轮 |
| 27 | toggle | | 开关滑块双回弹 |
| 28 | thinking-states | | 闪光状态行，定时切换到下一状态 |
| 29 | reasoning-stream | | 定高视窗内文本逐两行上滚 |
| 30 | streaming-text | | 逐词交叉模糊浮现 |
| 31 | matrix-loader | matrix-dot-loader | 4×4 点阵加载器（扫描/闪烁/环绕/脉冲） |
| 32 | banner-stacking | | Sonner 式横幅三层堆叠 |

**Pro 11 个（源码不可用）**：confetti-burst、gooey-plus-menu、card-stack-hover、organic-shimmer、drag-drop-with-physics、image-open-tilt、spinner-to-check-morph、pro-gradient-text、delete-with-smoky-dissolve、image-generation-placeholder、get-pro-button。`transitions-data.json` 中这些条目的 `css`/`react` 字段为空；`transitions/<slug>/index.html` 页面写明 "part of Transitions Pro… full source ships… in the Pro skill"；`cli/README.md` 声明本包 "contains no premium source"。index.html 第 5799 行起确实有 Pro demo 的压缩混淆代码（例如 `:root{--pv1o:120;…}`），但它不是配方，**不做逆向移植**。

### 1.3 文档与数据的不一致（使用时要注意）

- `README.md` 仍写 "12 transitions / eighteen reference files"，已经过时。以 `skills/transitions-dev/SKILL.md`（32 个）和 `transitions-data.json`（43 个）为准。
- 同一动效在 skill 与站点中 slug 不同（见上表第三列）。本项目统一采用 **skill 文件名**作为内部 id。
- `cli/free/tooltip.md` 是旧版（单 trigger 的 `.t-tt-wrap`，纯 CSS）；`skills/transitions-dev/17-tooltip.md` 是新版（`.t-tt-group` 共享气泡，可在触发器间移动，需要 JS）。`cli/free/banner-stacking.md` 中 `--stack-rise: 80px`，skill 版为 `60px`。**以 skills/ 为准。**

---

## 2. 源码结构与关键模块

### 2.1 目录与职责

```text
transitions.dev/
├── index.html              # 真正的"源"：:root 设计 token（第147–260行 motion tokens + --pX-*）、
│                           #   32 个 demo 的 CSS/HTML/JS、var PROTO_TEMPLATES = {...}（第9896行）、
│                           #   <script type="text/plain" data-react-key="p1…p34 | tok-*">（第7618–9330行）
├── prototypes.html         # 可调参 playground（含 bounce 参数说明，第218–224行）
├── detail.html             # 单个动效详情页（客户端生成代码片段，第10729行也有 PROTO_TEMPLATES）
├── build/
│   ├── extract.mjs         # 用 vm 沙箱 eval PROTO_TEMPLATES，解析 :root，按模板渲染 skills/
│   └── templates/{skill.md.tmpl, reference.md.tmpl}
├── skills/
│   ├── transitions-dev/    # SKILL.md、01…32-*.md、_root.css（motion tokens + 32 组语义变量）
│   └── transitions-polish/ # SKILL.md（规则）、_refine-rules.md（精简规则）、_root.css（只有 5 维 token）
├── scripts/
│   ├── transitions-data.json        # 43 条 {slug,name,sub,pro,css,react}，从运行中的站点抓取
│   └── build-transition-pages.py    # 生成 transitions/<slug>/index.html（SEO 静态页）与 sitemap
├── transitions/<43 slugs>/index.html + index.html(hub)
├── cli/  (bin/transitions-dev.mjs, free/*.md, free-manifest.json)
├── refine/ (bin/cli.mjs, server/{relay,motion-tokens,group-deterministic,inject,…}.mjs)
└── assets/ (图标、字体 Saans、OG 图)
```

### 2.2 生成链路

```text
index.html :root
  ├─ /* ---- Motion tokens */  --duration-* --ease-* --distance-* --scale-* --blur-*   (共享刻度)
  └─ /* P1 … P34 */            --p1-pos-open-dur: 260ms; --p2-open-dur: var(--duration-fast); …
                                   │
PROTO_TEMPLATES[pN] = { css: "...var(--p2-open-dur)...", vars: [["--dropdown-open-dur","--p2-open-dur"],…] }
                                   │  build/extract.mjs
                                   v
 ① 把 --pX-* 重命名为语义名（--dropdown-open-dur）  ② 把 var(--duration-*) 解析回字面量（skill 自包含）
 ③ 渲染 SKILL.md 的 Motion tokens 表 + _root.css + 01…32-*.md
```

关键实现（`build/extract.mjs`）：

- `extractObjectLiteral(source, marker)`：按字符扫描做括号配对，能跳过字符串中的 `{}`。
- `declRe = /(--p\d{1,2}-[a-z0-9-]+)\s*:\s*([^;]+);/gi`：抽取默认值。
- `tokenRe`：抽取 5 维 token。
- `familyRe`/`mtLineRe`：解析 token 的家族分组和行尾 `/* usage */` 注释，生成 Motion tokens 表。

**启示：** 我们的 `tokens.css` 也应在每个 token 行尾写 `/* usage */` 注释，由脚本生成文档和 lint 字典，保持"单一事实源"。

### 2.3 配方的解剖（以 `05-menu-dropdown.md` 为例）

每个配方由 5 部分组成，这也是我们在 vendor 时必须保留的结构：

1. **`:root` 语义变量**：`--dropdown-open-dur: 250ms; --dropdown-close-dur: 150ms; --dropdown-pre-scale: .97; --dropdown-closing-scale: .99; --dropdown-ease: cubic-bezier(.22,1,.36,1)`。
2. **`t-*` 选择器**，默认态即"未显示的预备态"：`.t-dropdown { transform: scale(var(--dropdown-pre-scale)); opacity:0; pointer-events:none; transition: transform …, opacity …; will-change: … }`。
3. **状态钩子**：`.is-open`、`.is-closing`、`[data-origin="top-right"]`。
4. **`@media (prefers-reduced-motion: reduce)` 守卫**，SKILL.md 规定必须保留。
5. **JS 编排（可选）**：通过 `getComputedStyle(...).getPropertyValue("--dropdown-close-dur")` 读取时长，保证 `setTimeout` 与 CSS 同步。

**状态钩子命名约定**（SKILL.md "Output format" 第 3 条）：`data-open`、`data-state`、`data-page`、`data-origin`、`aria-selected`、`aria-expanded`、`.is-open`、`.is-closing`、`.is-error`、`.is-shaking`、`.has-value`、`.is-clearing`、`.is-pulsing`、`.is-revealed`、`.is-shown`、`.is-hiding`、`.is-hover`、`.is-tilting`。

### 2.4 JS 编排的 7 种模式（后续 hooks 的抽象来源）

| 模式 | 代表配方 | 机制 | 我们的 hook |
|---|---|---|---|
| A 纯状态切换 | card-resize、badge、panel、page、icon-swap、toast、accordion、toggle、checkbox、shimmer | 只改 attribute/class，CSS 负责 transition | 无需 hook，由 React 直接渲染属性 |
| B 开合三态 | dropdown、modal | `open → closing →（closeDur 后）closed`；关闭期保留 `.is-closing`，防止下次打开从关闭态缩放起跳 | `usePresence` |
| C 重放 | number pop-in、text swap、success check、shake、texts reveal | 移除类 → `void el.offsetWidth` 强制 reflow → 重新添加 | `useReplay`（WAAPI 版本，避免强制 layout，见 §3.4-b） |
| D 测量并写几何 | tabs pill、tooltip 移动 | 测 `offsetLeft/offsetWidth`，写 transform/width；首帧和 resize 时 `transition:none`，reflow 后再恢复 | `useSlidingPill`、`useTooltipTravel` |
| E 逐帧驱动 | input-clear（光带包络）、spinning counter（SVG 竖向模糊衰减） | rAF 循环，用 JS 版 cubic-bezier 采样器与 CSS 曲线保持一致 | `bezier()` 采样器，也用于 3D 相机 |
| F 延迟表 | matrix loader、stagger | 为每个元素写 `--d` / `transition-delay` | `staggerDelay()` |
| G 堆栈管理 | banner stacking、thinking states | 维护 newest-first 数组和 `data-depth`；同一 task 内 reflow 后释放（**不用 rAF**，节流时 rAF 会被跳过） | `useStack`、`useThinkingStates` |

### 2.5 React 模板（`index.html` 第 7618–9330 行）

- 每个模板都带 `readMs(name, fallback)` 辅助函数（`parseFloat(getComputedStyle(documentElement).getPropertyValue(name))`）。
- 开合组件使用三态 state：`"closed" | "open" | "closing"`，在 `useEffect` 里对 `"closing"` 设定 `setTimeout(() => setState("closed"), readMs("--modal-close-dur",150))`（p2/p7）。
- `transitions-data.json` 的 react 字段是"自包含"版本：模块顶部有 `__TRANSITION_STYLES` 字符串，首次 import 时以 `document.getElementById("transitions-pN")` 做幂等检查并注入 `<style>`。**我们不采用这种运行时注入**，改为静态 CSS 文件，便于 tier 覆盖和 lint。
- p28（thinking states）演示了 React 下两份副本并存的切换写法：出场副本带 `is-exit` 渲染，入场副本以 `key={current}` 重新挂载，`useLayoutEffect` 中执行 `void el.offsetWidth` 后延迟 `--think-gap` 再移除 `is-enter-start`。
- `tok-duration/easing/distance/blur/scale` 这 5 个模板把 token 导出为 JS 常量（`duration.fast = 250` 等），我们照此生成 `motion/tokens.ts`，供 Three.js 侧使用。

### 2.6 transitions-polish 的规则（直接纳入《动效规范》）

- **开合不对称**：打开是邀请，关闭要让路。dropdown/modal 打开 250ms、关闭 150ms；panel 打开 400ms、关闭 350ms。**对称例外**（同时长、同曲线、不拆分）：page slide 250ms、tabs 250ms、accordion 250ms、icon swap 250ms、text swap 150ms。
- **进入携带距离和模糊**；退出可以缩短或省去。**超调（bounce）只用于进入**，不能让关闭回弹。
- **hover-in 快而直接**（≤250ms，smooth-out）；**hover-out 可以更慢、更弹**（`--ease-bounce-strong`）。这是全体系里唯一"退出比进入更复杂"的场景。
- **stagger**：单项偏移 40ms（大元素可用 80ms），**总错峰时长 ≤300ms**，长列表需要限制参与错峰的元素数量或缩小偏移。
- **delay**：只用于意图过滤（tooltip 80ms）和编排（success check 的 path 80ms）；**不要用 delay 来掩盖慢动画，永远不要延迟关闭或 hover-out**。
- **距离**："频率越高位移越小，仪式感越强位移越大"。原地文字 4px，页面 8px，庆祝 30px；除整块面板或抽屉外，位移大于约 40px 就会显得拖沓。
- **缩放**：预缩放不低于约 0.9，否则会读成 zoom。
- **模糊**：只用于软化切换或滑动，**不要用在纯淡入淡出或颜色/主题切换上**。

### 2.7 refine 工具的确定性算法（`refine/server/motion-tokens.mjs`）

- `DURATION_TOKENS/SCALE_TOKENS/BLUR_TOKENS/DISTANCE_TOKENS` 四张表，与 skill 一致。
- `pickScaleByUsage(hint)`：用正则从 label、selector、phase 中识别用途。例如 `/\bmodal\b|\bdialog\b/` 对应 0.96；`/\btooltip\b|\bpopover\b/` 对应 0.98；dropdown/menu/select 按是否含 `/clos/` 分别对应 0.99 或 0.97。无法识别时返回 null，退回按数值取最近。
- `refineTimings(timings, ctx)`：duration 偏离最近 token 超过 10ms 时出建议；easing 若不在 `TOKEN_EASINGS` 集合里，且是 `ease`/`ease-in`/自定义 bezier/`linear(`，就建议改为 smooth-out。
- 这套逻辑可以移植成我们 CI 里的 **motion-lint**：扫描 `*.tsx/*.css` 中的 `duration-[…]`、`transition:`、`cubic-bezier(`，按组件名推断用途后给出 token 建议。

### 2.8 源码中发现的问题 / 需要修正之处

| # | 位置 | 问题 | 本项目处理 |
|---|---|---|---|
| 1 | `22-toast.md` | HTML 示例写 `data-open="false"`，但 CSS 用的是 `.is-open` | 统一改为 `[data-state]` / keyframes（§4.2） |
| 2 | polish 规则与 `_root.css` | 规则写 "toast close 350ms (--duration-medium)"，但配方 `--toast-close: 250ms`，`--stack-close: 250ms` | 取 **250ms**，符合"关闭快于打开 350ms"的上位规则 |
| 3 | 04/05 的 JS | `readMs` 兜底值为 200，而 token 为 150 | 兜底值统一从 `tokens.ts` 常量读取 |
| 4 | `02-number-pop-in.md` | 只支持 `data-stagger="1"/"2"` 两级 | 泛化为 `--i` 乘以 stagger，并设总时长上限（§3.4-c） |
| 5 | `26-spinning-counter.md` | "JavaScript orchestration: None — pure CSS"，实际上必须用 JS 构建 reel，竖向模糊也要 rAF 驱动（index.html 第 9446–9530 行） | 参照 p26 模板移植 |
| 6 | 全部配方 | `will-change` 写在常驻选择器上 | 只在动画期间通过 class 挂上 `will-change`（§6 风险 3） |
| 7 | 13/15/16/17/28/31 | 暗色覆盖基于 `html[data-theme="dark"]`，而 shadcn 用 `.dark` | 统一写到 `.dark` 与我们的色卡变量 |
| 8 | `21-accordion.md` | inner 的 `filter: blur(2px)` 写成字面量，不走变量 | 改为 `var(--blur-small)`，以便 lite 档置零 |
| 9 | `12-error-state-shake.md` | 关键帧百分比写死（28.57/57.14/78.57%），调整 A/B 时长后会错位 | 由脚本按公式生成（§3.4-f） |
| 10 | prototypes.html 第 218 行注释 | 称 bounce 是 "second control point" 的 Y，实际是第一个控制点的 y1 | 仅文档瑕疵 |

---

## 3. 可复用算法与实现

### 3.1 Motion token 全表（源：`skills/transitions-dev/_root.css` 第 1–38 行，与 `index.html` 第 209–239 行一致）

**时长**

| token | 值 | 用途（原文） | 本系统用途 |
|---|---|---|---|
| `--duration-stagger` | 40ms | 每项错峰 | 列表进场、数字位错峰 |
| `--duration-micro` | 80ms | tooltip/path 延迟、shake 段、大元素错峰 | tooltip 意图延迟、shake 分段 |
| `--duration-quick` | 150ms | modal/dropdown 关闭、文字切换、tooltip 出现 | 所有关闭、状态文字、颜色阈值变化 |
| `--duration-fast` | 250ms | icon swap、dropdown/modal 打开、tabs、页面滑动 | 浮层打开、Tabs、列表<->详情 |
| `--duration-medium` | 350ms | panel 关闭、toast 关闭 | 侧栏关闭、toast/横幅打开 |
| `--duration-slow` | 400ms | panel 打开、骨架揭示、输入清空 | 侧栏/Sheet 打开、骨架→内容 |
| `--duration-very-slow` | 500ms | 强调、badge 出现、文字揭示、success check | 成功确认、数字 pop-in、空状态标题 |

**曲线**

| token | 值 | 用途 |
|---|---|---|
| `--ease-smooth-out` | `cubic-bezier(0.22, 1, 0.36, 1)` | **默认曲线**：浮层开合、页面滑动、尺寸/位置变化 |
| `--ease-in-out` | `ease-in-out` | icon swap、text swap、文字揭示、骨架揭示 |
| `--ease-out` | `ease-out` | tooltip 开合 |
| `--ease-linear` | `linear` | shimmer、骨架脉冲、spinner |
| `--ease-bounce` | `cubic-bezier(0.34, 1.36, 0.64, 1)` | badge pop 进入 |
| `--ease-bounce-strong` | `cubic-bezier(0.34, 3.85, 0.64, 1)` | hover-out 回弹 |

**距离 / 缩放 / 模糊**

| 距离 | 值 | 缩放（起始预缩放） | 值 | 模糊 | 值 |
|---|---|---|---|---|---|
| `--distance-micro` | 4px（text swap） | `--scale-large` | .96（modal） | `--blur-small` | 2px（面板/图标/文字/骨架/数字） |
| `--distance-small` | 6px（shake 小段） | `--scale-medium` | .97（dropdown 开） | `--blur-medium` | 3px（页面滑动/文字揭示） |
| `--distance-base` | 8px（badge/页面/shake 大段） | `--scale-small` | .98（tooltip） | `--blur-large` | 8px（success check） |
| `--distance-medium` | 12px（文字揭示） | `--scale-tiny` | .99（dropdown 关） | | |
| `--distance-large` | 30px（check badge） | | | | |

**本项目扩展 token**（写入 `tokens.css`，行尾同样加 usage 注释）：

```css
:root {
  --distance-drawer: 40px;            /* Sheet/侧栏进出位移（与 shadcn base-ui sheet 的 2.5rem 一致；polish 规则"整块面板可超过 40px"的上限） */
  --stagger-cap: 300ms;               /* 错峰总时长上限（polish 规则） */
  --duration-camera-min: 400ms;       /* 3D 相机飞行最短时长 */
  --duration-camera-max: 1200ms;      /* 3D 相机飞行最长时长 */
  --duration-lod-fade: 250ms;         /* 点云八叉树节点渐入（= --duration-fast） */
  --telemetry-anim-interval: 500ms;   /* 离散遥测字段动画最小间隔（≤2Hz） */
  --telemetry-text-interval: 100ms;   /* 连续遥测字段文本刷新间隔（≤10Hz） */
  --ease-spring-snappy: cubic-bezier(0.34, 1.36, 0.64, 1);  /* ≈ morphicons snappy（见 §3.2） */
  --ease-spring-bouncy: cubic-bezier(0.34, 1.96, 0.64, 1);  /* ≈ morphicons bouncy，仅用于点赞级庆祝，本项目基本不用 */
}
```

### 3.2 Spring 近似：`cubic-bezier(0.34, B, 0.64, 1)` 族

transitions.dev 不使用物理弹簧，而是用"单次超调"的三次贝塞尔曲线模拟弹簧。prototypes.html 的 "Bounce" 旋钮就是按模板 `cubic-bezier(0.34, {bounce}, 0.64, 1)` 改写 y1：1.0 表示无超调，约 1.35 表示轻微回弹，3.8 以上表示强烈回弹。本单元用二分采样实测（脚本见 `.cache/research/d02/`，`bez.py` 逻辑已写进本节）：

| 曲线 | 出处 | 峰值（超调） | 峰值时刻 | t50 | t90 | 等效阻尼比 ζ* |
|---|---|---|---|---|---|---|
| smooth-out (0.22,1,0.36,1) | 默认 | 1.000 | — | 0.13 | 0.37 | ≥1（无超调） |
| (0.34,1.25,0.64,1) | plus-menu morph 打开 | 1.020 | 0.70 | 0.16 | 0.40 | 0.78 |
| (0.34,1.35/1.36,0.64,1) | toggle、check bob、badge pop | 1.041–1.043 | 0.64 | 0.15 | 0.36 | 0.71 |
| (0.34,1.45,0.64,1) | number pop-in | 1.066 | 0.61 | 0.14 | 0.33 | 0.65 |
| (0.34,1.96,0.64,1) | like pop | 1.235 | 0.50 | 0.10 | 0.22 | 0.42 |
| (0.34,3.85,0.64,1) | avatar hover-out | **2.015** | 0.40 | 0.05 | 0.09 | 非物理（超调超过 100%） |
| (0.16,1,0.3,1) | spinning counter reel | 1.000 | — | 0.10 | 0.33 | — |
| (0.4,0,0.2,1) | badge close | 1.000 | — | 0.35 | 0.63 | — |

\* ζ 由超调量反推：`ζ = −ln(OS) / √(π² + ln²(OS))`。

**与 morphicons 对齐**（`refs/design/morphicons/src/core/spring.ts`，模型为 `ẍ = k(1−x) − cẋ`，以 1/240s 子步长做半隐式 Euler 积分，重启时保留速度，速度截断在 ±14）：

| morphicons 预设 | k / c | ζ | 超调 | t90 | 稳定时间（|1−x|<.001） | 对应 transitions.dev |
|---|---|---|---|---|---|---|
| smooth | 170 / 26 | 1.00 | 0 | 300ms | 737ms | smooth-out（400–500ms） |
| **snappy** | 420 / 30 | 0.73 | 2.7% | 133ms | 417ms | **bounce(1.36) × 350–500ms** |
| bouncy | 300 / 14 | 0.40 | 24.3% | 112ms | 925ms | bounce(1.96) × 350ms |

规范：**图标形变统一用 `spring="snappy"`**，与 `--ease-bounce` 的超调级别（约 3–4%）一致。bouncy 只允许用在"庆祝"类场景，本系统默认不用。

**需要真实多次振荡时，用 `linear()` 生成器**（Chromium 113+、Safari 17.2+、Firefox 112+ 支持）：

```ts
// scripts/gen-spring-linear.ts —— 由 morphicons 同款 ODE 生成 CSS linear() 缓动
function springLinear(k: number, c: number, samples = 48) {
  const h = 1 / 240; let x = 0, v = 0, t = 0; const xs: number[] = [];
  const pts: [number, number][] = [];
  while (t < 3) {                               // 模拟到稳定
    const a = k * (1 - x) - c * v; v += a * h; x += v * h; t += h;
    pts.push([t, x]);
    if (Math.abs(1 - x) < 1e-3 && Math.abs(v) < 2e-2) break;
  }
  const T = t;                                  // 稳定时间即 CSS 时长
  for (let i = 0; i <= samples; i++) {          // 等时间间隔采样
    const ti = (i / samples) * T;
    const p = pts.find(([tt]) => tt >= ti) ?? pts[pts.length - 1];
    xs.push(p[1]);
  }
  return { duration: Math.round(T * 1000), easing: `linear(${xs.map(n => n.toFixed(4)).join(", ")})` };
}
// 产出例如：--ease-spring-snappy-linear: linear(0, 0.0x, …, 1.027, …, 1); --duration-spring-snappy: 417ms;
```

### 3.3 32 个配方参数速查（本项目取舍）

"采用"一列：A 表示 MVP 必用，B 表示 V0.2 以后使用，R 表示仅参考，S 表示不用。

| # | 类 / 钩子 | 关键参数（默认） | JS | 本项目用途 | 采用 |
|---|---|---|---|---|---|
| 01 | `.t-resize` | 300ms smooth-out，补间 width/height | 否 | HUD 小部件（小地图 240<->480）尺寸切换 | A |
| 02 | `.t-digit-group.is-animating .t-digit[data-stagger]` | 500ms，8px，stagger 70ms，blur 2px，bounce 1.45，dir(0,1) | 重放 | 离散遥测数字 | A |
| 03 | `.t-badge[data-open] > .t-badge-dot` | slide 260ms (−8.2,12.4)px；pop 500ms bounce 1.36；关闭 180ms (0.4,0,0.2,1) | 否 | 告警数角标、无人机芯片角标 | A |
| 04 | `.t-text-swap.is-exit/.is-enter-start` | 150ms，4px，blur 2px，ease-in-out | 三相 | 飞行模式、链路、任务状态 | A |
| 05 | `.t-dropdown.is-open/.is-closing[data-origin]` | 开 250ms 自 .97；关 150ms 至 .99 | 三态 | DropdownMenu/Popover/Select/ContextMenu（改写为 keyframes） | A |
| 06 | `.t-modal.is-open/.is-closing` | 开 250ms 自 .96；关 150ms 至 .96 | 三态 | Dialog/AlertDialog（改写为 keyframes） | A |
| 07 | `.t-panel-slide[data-open]` | 开 400ms，关 350ms，Y 100px，blur 2px | 否 | Sheet（改写为 X 轴 40px keyframes）、浮动侧栏 | A |
| 08 | `.t-page-slide[data-page] .t-page[data-page-id]` | 250ms，8px，blur 3px，stagger 0 | 否 | 右栏 机群列表<->单机详情；向导步骤 | A |
| 09 | `.t-icon-swap[data-state] .t-icon[data-icon]` | 250ms，blur 2px，起始缩放 .25，ease-in-out | 否 | 不可形变的图标替换，以及 morphicons 在 lite/reduced 档的回退 | A |
| 10 | `.t-success-check[data-state=in]` | 500ms；旋转自 80°；Y 40px（bob 1.35）；blur 10px；path 延迟 80ms | 重放 | 任务上传成功、起飞完成、世界加载完成 | A |
| 11 | `.t-avatar` + `--shift/--scale-active` | lift −4px，衰减 .45，scale 1.05，320ms，回弹 3.85 | 是 | 顶栏机群芯片组 hover | B |
| 12 | `.t-input(.is-error/.is-shaking)` `.t-error-msg` | 6px/4px，A=80ms，B=60ms，保持 3000ms，恢复 280ms | 是 | 表单校验、告警芯片单次抖动 | A |
| 13 | `.t-clear…` | 1000ms 逐帧光带 | 逐帧 | 世界列表搜索框清空：开销大，只在 full 档启用 | R |
| 14 | `.t-skel[.is-revealed] .t-skel-skeleton.is-pulsing` | 脉冲 1000ms×1 至 .5；揭示 400ms blur 2px | 少量 | 面板等待 WS 首帧、世界卡片加载 | A |
| 15 | `.t-shimmer[data-text]::before` | 2000ms linear，渐变带宽 400% | 否 | "连接仿真中…""流式加载中…" | A（同屏 ≤2 个） |
| 16 | `.t-tabs .t-tabs-pill .t-tab[aria-selected]` | 250ms smooth-out | 测量 | 视角切换、倍速 ×1/×2/×5/×10、图层分组 | A |
| 17 | `.t-tt-group .t-tt[data-show]` | 入 150ms，延迟 80ms，出 50ms，缩放 .98，移动 160ms | 测量 | 3D 主工具栏共享气泡（其余用 shadcn Tooltip） | B |
| 18 | `.t-stagger.is-shown .t-stagger-line--N` | 500ms，12px，stagger 40ms，blur 3px；退出 200ms 纯淡出 | 重放 | 空状态、欢迎页、世界详情标题；列表项进场（改写） | A |
| 19 | `.t-tilt .t-tilt-card .t-tilt-glare` | 透视 1000px，回复 1000ms，跟随 400ms | 是 | 世界画廊卡片（可选） | S/R |
| 20 | `.t-morph[data-open]` | 开 350ms bounce 1.25；关 250ms；圆角 40→20 | 少量 | 3D 视图中"添加航点/无人机"FAB | B |
| 21 | `.t-acc[data-open] .t-acc-panel/.t-acc-panel-inner/.t-acc-chevron` | 250ms，grid-rows 0fr<->1fr，箭头 scaleY(−1) | 否 | 左栏 Layers/Environment 分组（覆盖 shadcn Accordion） | A |
| 22 | `.t-toast.is-open` | 开 350ms，关 250ms，16px，blur 2px，缩放 .97 | 否 | Sonner 主题化 | A |
| 23 | `.t-like…` | 心形与粒子 | 否 | — | S |
| 24 | `.t-learn…` | chevron 张开 | 否 | 文档/帮助链接 | R |
| 25 | `.t-check[aria-checked]` | 方框 150ms，描画 350ms，取消 150ms | 否 | shadcn Checkbox 对勾描画（lucide check 长度 22.63，取 `--check-len: 23`） | A |
| 26 | `.t-reel .t-reel-col .t-reel-strip` | 1400ms (0.16,1,0.3,1)，单格 30px，stagger 90ms，竖向模糊 3px | 是 | 事件型 KPI（任务完成数、加载点数汇总） | B |
| 27 | `.t-toggle[data-on].is-init .t-toggle-thumb` | 350ms bounce 1.35，双回弹 | 否 | shadcn Switch（图层开关） | A（transition 简化版） |
| 28 | `.t-think .t-think-sizer .t-think-text` | 保持 2000ms，切换 150ms，间隔 50ms，8px，blur 2px | 是 | 重建/SITL 启动等多步骤状态行 | B |
| 29 | `.t-reason-viewport/scroll` | 保持 840ms，步进 500ms，2 行，渐隐 28px | 是 | ANet 协商日志滚动条 | B（V1.0） |
| 30 | `.t-stream-w.is-in` | 间隔 60ms，淡入 350ms，blur 1px | 是 | ANet Agent 消息流式输出 | B（V1.0） |
| 31 | `.t-matrix[data-variant]` | 周期 1200ms；scan/twinkle/orbit/pulse | 构建 | 状态栏"节点流式中"、按钮内小加载器 | A |
| 32 | `.t-stack .t-stack-banner[data-depth]` | 开 350ms，关 250ms，上升 60px，露出 12px，深度缩放 .06，深度淡出 .4 | 是 | HUD 告警横幅三层堆叠 | B |

### 3.4 核心算法与伪代码

#### a. 开合三态机（非 Radix 浮层，例如浮动 HUD 面板）

```ts
type Phase = "closed" | "open" | "closing";
function usePresence(open: boolean, closeVar = "--modal-close-dur") {
  const [phase, setPhase] = useState<Phase>(open ? "open" : "closed");
  useEffect(() => {
    if (open) { setPhase("open"); return; }
    if (phase === "closed") return;
    setPhase("closing");
    const ms = motion.ms(closeVar);                 // 读缓存的 token（见 h），reduced 档为 0
    const id = setTimeout(() => setPhase("closed"), ms);
    return () => clearTimeout(id);
  }, [open]);
  return { mounted: phase !== "closed", phase };    // className = t-xxx + (phase==="open"?" is-open": phase==="closing"?" is-closing":"")
}
```

建议：更稳妥的做法是监听元素的 `transitionend`（过滤 `e.target===el && e.propertyName==="opacity"`），`setTimeout(ms + 50)` 只做兜底。后台标签页中定时器会被节流到 1s，可能导致 `is-closing` 滞留。

#### b. 重放（避免强制 layout 的 WAAPI 版本）

```ts
// 原版：el.classList.remove(c); void el.offsetWidth; el.classList.add(c);  —— 每次都会强制同步 layout
// 新元素插入时 CSS animation 会自动启动，所以"替换节点"比"重放同一节点"更好（见 c）。
// 必须重放同一节点时：
function replay(el: Element) {
  const anims = el.getAnimations({ subtree: true });
  if (anims.length) { for (const a of anims) { a.cancel(); a.play(); } return; }   // CSSAnimation 在 cancel 之后 play 会从 0 开始
  el.classList.remove("is-animating"); void (el as HTMLElement).offsetWidth; el.classList.add("is-animating"); // 兜底
}
```

#### c. Number pop-in 泛化版（遥测离散量）

```text
输入：newValue, format(fixed=1, unit), prevText, lastAnimAt, tier
1. text = format(newValue)；若 text == prevText 则返回
2. 若 now - lastAnimAt < --telemetry-anim-interval（500ms）：
       记下 pending=text，并在 interval 到期时再处理（合并中间值，只展示最新值）；返回
3. dir = sign(newValue - prevValue)  → --digit-dir-y = (dir>0 ? +1 : -1)   // 上涨：从下方升入；下跌：从上方落入
4. 右对齐逐位 diff（数字位数变化时整体视为变化）：
       changed = { i | text[i] != prevText[i] }
5. 对每个 i ∈ changed，按从左到右的序号 k：
       用新 <span class="t-digit"> 替换第 i 位（新节点插入即自动触发 animation，无需 reflow）
       style.animationDelay = min(k * --digit-stagger, --stagger-cap/2)   // 70ms/位，总计 ≤150ms
   未变化的位保持原节点，不动画
6. tier == "reduced"：直接 textContent = text；tier == "lite"：--digit-blur 已被置 0（CSS 层处理）
7. 容器：display:inline-flex; font-variant-numeric: tabular-nums;（防止宽度抖动）
   group 始终带 .is-animating，动画只作用于新插入的 .t-digit.ch
```

CSS 在原配方基础上的改动：

```css
.t-digit-group .t-digit.ch { animation: t-digit-pop-in var(--digit-dur) var(--digit-ease) both; }
.t-digit-group { font-variant-numeric: tabular-nums; }
.t-digit-group .t-digit.ch { will-change: transform, opacity, filter; }   /* 只在新节点上声明 will-change */
```

#### d. 文字三相切换，以及 thinking states 双层并行

```text
swap(next):                                  // 04-text-states-swap
  el.add("is-exit")                          // 向上 4px，blur 2px，opacity 0（150ms）
  after(--text-swap-dur):
     el.text = next; el.remove("is-exit"); el.add("is-enter-start")   // 瞬间跳到下方 4px（transition:none）
     reflow; el.remove("is-enter-start")    // 回到 0（150ms）
  总计 2 × 150ms。连续切换时加 busy 锁，并排队只保留最后一次。

thinking(states, hold=2000):                 // 28-thinking-states，双层并行，一次切换只花一个 --think-swap
  leaving = live; leaving.add("is-exit")
  next = new span(.t-think-text.is-enter-start, text, data-text=text)；append
  after(--think-gap 50ms): reflow; next.remove("is-enter-start")
  after(swap+gap): leaving.remove()；再等 hold 后执行下一轮
  外层 .t-think-sizer 放最长的文案（宽度恒定，避免抖动）
```

#### e. 滑动胶囊与 tooltip 移动

```text
moveTo(tab, animate):                        // 16-tabs-sliding
  x = tab.offsetLeft; w = tab.offsetWidth
  if !animate: pill.style.transition="none"; set(x,w); reflow; pill.style.transition=""
  else set(x,w)                              // transform: translateX(x); width: w（250ms smooth-out）
  触发时机：首帧（rAF）、ResizeObserver(list)、字体加载完成（document.fonts.ready）、active 变化
place(trigger):                              // 17-tooltip，同组共享一个气泡
  width = ceil(text.scrollWidth + padL + padR)
  x = r.left - g.left + r.width/2 - width/2
  若未显示：snap(width, --tt-x)，然后 data-show=true（出现有 80ms 延迟）；已显示：直接写入，按 --tt-move-dur 160ms 移动过去
```

#### f. Shake 分段关键帧（按公式生成，不手写百分比）

```text
legs = [A, A, B, B]  (A=--shake-dur-a 80ms, B=--shake-dur-b 60ms)，total = 2A+2B = 280ms
stops = cumsum(legs)/total → [0, 28.57%, 57.14%, 78.57%, 100%]
x     = [0, +distance(6px), −distance, +overshoot(4px), 0]
每个 stop 带 animation-timing-function: var(--shake-ease)（smooth-out），整体 animation: … linear
编排：wrap.add(is-error); input.add(is-error); replay(input, "is-shaking");
      定时 total+hold(3000ms) 后移除 is-error（边框和提示在 280ms 内恢复）。用户输入时立即清除。
```

#### g. 距离衰减抬升（avatar group hover）

```text
shift(i) = lift × falloff^|i − active|     (lift=−4px, falloff=.45)  → 0：−4，1：−1.8，2：−0.81，3：−0.36
scale(i) = (i == active) ? 1.05 : 1
关键技巧：在写 CSS 变量之前，先以内联方式设置 transitionTimingFunction：
  hover-in 用 smooth-out，mouseleave 用 bounce-strong(3.85)。这样同一条 transition 就有方向感知的曲线。
```

#### h. Token 读取缓存（避免反复 getComputedStyle）

```ts
// motion/runtime.ts
const cache = new Map<string, number>();
export const motion = {
  tier: "full" as "full" | "lite" | "reduced",
  ms(name: string, fb = 0) {
    if (motion.tier === "reduced") return 0;
    if (!cache.has(name)) cache.set(name, parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || fb);
    return cache.get(name)!;
  },
  invalidate() { cache.clear(); },           // tier 或主题切换时调用
};
```

#### i. Spinning counter（事件型 KPI）

```text
build(str): 对每个数字位建 col>strip，内含 (spins+1)*10 + rows 个单元格（k%10）；分隔符（, .）原样放置
spin(target): strips 先置 translateY(center(0))，transition:none → reflow
  对第 i 列：transition = transform dur(1400ms) ease(0.16,1,0.3,1) delay(i*90ms)
             translateY = −(spins*10 + digit) × cell
  竖向模糊（SVG feGaussianBlur stdDeviation="0 Y"，每列一个 filter，最多 10 个）：
     rAF：local = clamp((t − i*stagger)/dur, 0, 1)；Y = blurMax × (1 − local)
  aria-label = 目标字符串；reduced 档直接显示结果
```

#### j. 横幅堆叠深度模型（HUD 告警）

```text
depth d=0：translateY(0) scale(1) opacity 1 blur 0
depth 1：transform-origin 50% 100%；translateY(−peek) scale(1−.06) opacity .6 blur 1px
depth 2：translateY(−2·peek) scale(1−.12) opacity .36 blur 2px
新横幅：.is-enter（从下方 60px、缩放 .97、blur 2px、transition:none）→ 所有旧横幅 depth+1 → reflow → 在同一 task 内移除 .is-enter
第 4 个：.is-leaving（depth 3 位置并淡出，250ms）后移除
展开：指针在折叠框内时加 .is-spread；离开"(高度+gap)×2"范围才移除；旧横幅上移 (100%+8px)×d
```

#### k. Matrix loader 延迟表（4×4 点阵，每点 2px，间隔 2px）

```text
scan：    d = col × cycle/10
twinkle： d = [7,2,11,5,14,9,0,12,3,15,6,10,13,1,8,4][idx] × cycle/16
orbit：   RING=[1,2,7,11,14,13,8,4]，d = RING.indexOf(idx) × cycle/8；中心 4 点不动画
pulse：   INNER=[5,6,9,10] 先亮，其余晚 cycle×.16
rounded： 角点 [0,3,12,15] 隐藏（.is-gap）
keyframes：0%,45%,100% → base；15% → active（只动 background-color，开销极低）
```

#### l. Stagger 预算

```text
staggerDelay(i, n, offset=40ms, cap=300ms):
  eff = (n ≤ 1) ? 0 : min(offset, cap/(n−1))
  return min(i, maxItems=8) × eff             // 超过 8 项的元素与第 8 项同时进入
```

#### m. JS cubic-bezier 采样器（Three.js 相机与 LOD 渐入共用同一套曲线）

直接移植自 `13-input-clear-dissolve.md` 的 `bezier(str)`：对 x(s)−t 做 8 次 Newton 迭代求 s，再返回 y(s)。

```ts
export function bezier(x1: number, y1: number, x2: number, y2: number) {
  const cx = 3*x1, bx = 3*(x2-x1)-cx, ax = 1-cx-bx, cy = 3*y1, by = 3*(y2-y1)-cy, ay = 1-cy-by;
  return (t: number) => {
    if (t <= 0) return 0; if (t >= 1) return 1;
    let s = t;
    for (let i = 0; i < 8; i++) { const dx = ((ax*s+bx)*s+cx)*s - t; const d = (3*ax*s+2*bx)*s+cx; if (Math.abs(dx) < 1e-6 || d === 0) break; s -= dx/d; }
    return ((ay*s+by)*s+cy)*s;
  };
}
export const easeSmoothOut = bezier(0.22, 1, 0.36, 1);
```

注意：Newton 法在 x 导数接近 0 时（例如 (0.34,3.85,…) 这类端点附近的强超调曲线）可能发散，需要加二分兜底（`bez.py` 的做法）。

#### n. Motion tier 调速器（本项目设计，与点云 point budget 共用 FPS 信号）

```text
输入：frameTime p95（来自渲染循环，2s 滑窗）、targetFrame（16.7ms）、OS reduced、用户设置 motionPref ∈ {system, full, lite, reduced}
effective = min(OS 约束, 用户设置, 性能档)             // 顺序 full > lite > reduced，只能更弱
性能档：
  full → lite：p95 > 1.25 × target 持续 2s
  lite → full：p95 < 0.9 × target 持续 5s，且点云 budget 已回到上限（滞回，避免抖动）
降级顺序（同一个 QualityManager 统一调度）：
  ① UI motion lite（模糊/背景模糊清零，暂停屏外循环）
  ② DPR 降低
  ③ 点云 point budget 下调
  ④ EDL/粒子数下调
写入：document.documentElement.dataset.motion = effective; motion.tier = effective; motion.invalidate()
```

---

## 4. 在本项目中的落点与复用方式（《动效规范》草案 v0.1）

### 4.1 交互映射总表

| # | 本系统交互 | transitions.dev 配方 | shadcn 组件 / 承载 | 关键 token | JS / hook | 版本 |
|---|---|---|---|---|---|---|
| 1 | **面板展开/收起**（左 WORLD 栏、右 DRONES 栏） | 07 panel-reveal 改为 **X 轴**，位移 `--distance-drawer` 40px | 自定义浮动面板（Card 容器），加 `usePresence` | 开 400 / 关 350，smooth-out，blur 2px（lite 档为 0） | `usePresence` | V0.1 |
| 1b | 面板内分组（Layers、Environment、Sensors） | 21 accordion | `Accordion`/`Collapsible` | 250ms 对称，箭头 scaleY 翻转 | 无 | V0.1 |
| 2 | **Sheet 打开/关闭**（任务编辑、无人机详情抽屉） | 07 panel-reveal 的 keyframes 版 | `Sheet`（data-side） | 同 1；遮罩 fade 150ms，**不用 backdrop-blur** | 无（Radix Presence） | V0.1 |
| 2b | **Dialog/AlertDialog**（确认起飞、删除世界） | 06 modal 的 keyframes 版 | `Dialog`、`AlertDialog` | 开 250ms 自 .96，关 150ms 至 .96 | 无 | V0.1 |
| 3 | **Dropdown / Popover / Select / ContextMenu** | 05 menu-dropdown 的 keyframes 版，原点取 Radix 变量 | `DropdownMenu`、`Popover`、`Select`、`ContextMenu`、`Menubar` | 开 250ms 自 .97，关 150ms 至 .99 | 无 | V0.1 |
| 4 | **Tabs 切换**（视角：第三人称/FPV/鸟瞰/自由；倍速；面板页签） | 16 tabs-sliding（胶囊）；内容区用 08 的"仅进入"版 | `Tabs`（外包一层 `SlidingTabsList`）、`ToggleGroup` | 250ms smooth-out；内容进入 8px 加 blur 3px | `useSlidingPill` | V0.1 |
| 5 | **页面/视图切换**：机群列表<->单机详情 | 08 page-side-by-side | 右栏容器 | 250ms，8px，blur 3px | 无（设置 `data-page`） | V0.1 |
| 5b | 路由切换（世界画廊→沙盘） | 14 skeleton-reveal 加 18 texts-reveal；**3D 画布常驻不卸载** | `Skeleton`、`Card` | 400ms / 500ms | `useReplay` | V0.1 |
| 5c | 3D 相机模式切换 | 同一 smooth-out 曲线的 JS 版（§4.6） | — | 400–1200ms，按距离取值 | `bezier()`、`useCameraTween` | V0.1 |
| 6 | **遥测数字跳变** | 02 number-pop-in（泛化版）；事件型用 26 | 自定义 `MotionNumber` | 500ms bounce 1.45，≤2Hz | `MotionNumber`、`useTelemetryBinding` | V0.1 / V0.2 |
| 7 | **图标切换**（播放/暂停、图层可见、锁定、菜单/关闭） | morphicons 为主；09 icon-swap 作回退 | `Button` 内放 `MorphIcon` | snappy spring；icon-swap 250ms | 无 | V0.1 |
| 8 | **状态文字切换**（Ready→Taking off→Flying；Connecting→Live） | 04 text-states-swap；多步骤长任务用 28 thinking-states | `Badge` 内放 `SwapText` | 150ms，4px，blur 2px | `useTextSwap` | V0.1 / V0.2 |
| 9 | 加载/等待（WS 首帧、节点流式、SITL 启动） | 14 skeleton、15 shimmer、31 matrix-loader | `Skeleton`、`Spinner` 的替代 | 脉冲 1000ms；shimmer 2000ms；matrix 1200ms | `SkeletonReveal`、`MatrixLoader` | V0.1 |
| 10 | **告警 shake** | 12 error-state-shake | 表单：`Input`/`Field`；告警：机群卡片上的状态 `Badge` | 6/4px，80/60ms（共 280ms），恢复 280ms | `useShake` | V0.1（表单）/ V0.2（告警） |
| 11 | **成功确认** | 10 success-check；复选框用 25 | `Button`/`Toast` 内嵌；`Checkbox` | 500ms；path 延迟 80ms | `SuccessCheck` | V0.2 |
| 12 | **卡片尺寸变化** | 01 card-resize（HUD 小部件）；高度变化用 21 | `Card` | 300ms smooth-out | 无 | V0.1 |
| 13 | **列表进出**（机群、事件日志、航点列表） | 进入借 18（8px、blur 2px、400ms、错峰 40ms、上限 300ms）；退出 200ms 淡出后 grid-rows 收起 250ms | `ScrollArea` 内的列表项 | §3.4-l | `useListPresence` | V0.2 |
| 14 | **Toast** | 22 toast 的 token，加到 Sonner 上 | `Sonner` | 开 350 / 关 250，16px，blur 2px，缩放 .97 | 无 | V0.1 |
| 15 | HUD 告警横幅堆叠 | 32 banner-stacking | `Alert` 放入 `AlarmStack` | §3.4-j | `useStack` | V0.2 |
| 16 | 角标（告警数、无人机未读事件） | 03 notification-badge | `Badge` | slide 260ms，pop 500ms bounce | 无 | V0.1 |
| 17 | 工具栏 tooltip | 17 tooltip（主工具栏用共享气泡）；其他用 Radix Tooltip 映射（§4.2） | `Tooltip` | 入 150ms，延迟 80ms，出 50ms | `useTooltipTravel`（B） | V0.1 / V0.2 |
| 18 | 开关、复选（图层开关） | 27 toggle（transition 简化版）、25 checkbox | `Switch`、`Checkbox` | 350ms bounce 1.35；描画 350ms | 无 | V0.1 |
| 19 | 机群芯片 hover | 11 avatar-group-hover | 顶栏 `ToggleGroup`/`Badge` 组 | §3.4-g | `useFalloffHover` | V0.2 |
| 20 | 添加航点/无人机 FAB | 20 plus-menu-morph | `Button` 加菜单内容 | 开 350 / 关 250 | 无 | V0.6 |
| 21 | ANet Agent 消息、协商日志 | 30 streaming-text、29 reasoning-stream、28 thinking-states | `Card`、`ScrollArea` | §3.3 | `useStream` | V1.0 |

**第 10 行（告警）的语义细则**：

- 告警分级：info 对应 Toast；warning 对应横幅加角标；critical 对应横幅、角标、状态 Badge 单次 shake，外加持续红色 `#E93024` 的描边或底色，并配图标与文字。
- shake 只在告警**进入**时触发一次，同一告警 10s 内不重复触发；告警持续期间**不自动恢复**。表单校验沿用 `--revert-hold` 3000ms 自动恢复。
- 禁止抖动 3D 画布、整块面板、Dialog（Dialog 校验失败时只抖动出错字段）。
- `role="alert"` 与文字是主要信息通道，动效只起辅助作用（WCAG 2.2 的 1.4.1 与 2.3.3）。

### 4.2 与 shadcn 结合的实现方案

**核心事实**（已在源码中核对）：

1. 新版 shadcn（`refs/design/ui/apps/v4/registry/bases/radix/ui/*.tsx`）用 `data-slot` 标识部件（`dialog-content`、`sheet-content`、`dropdown-menu-content`、`popover-content`、`tooltip-content`、`select-content`、`accordion-content`、`tabs-trigger`、`switch-thumb`…），动效写在 `styles/style-*.css` 的 `cn-*` 类中。例如 `style-vega.css` 第 467 行：
   ```text
   .cn-dialog-content { @apply data-open:animate-in data-closed:animate-out data-closed:fade-out-0 data-open:fade-in-0 data-closed:zoom-out-95 data-open:zoom-in-95 … duration-100 }
   ```
   其中 `data-open`/`data-closed` 是 `packages/shadcn/src/tailwind.css` 第 28–40 行定义的 `@custom-variant`，同时匹配 Radix 的 `[data-state=open]` 和 Base UI 的 `[data-open]`。
2. `animate-in` 展开为 `enter var(--tw-animation-duration, var(--tw-duration, .15s)) var(--tw-ease, ease) …`；`@keyframes enter { from { opacity: var(--tw-enter-opacity,1); transform: translate3d(…) scale3d(var(--tw-enter-scale,1)…) rotate(…); filter: blur(var(--tw-enter-blur,0)) } }`（tw-animate-css 1.4.0）。
3. Radix Presence 只在 `animationend/animationcancel` 之后卸载，所以 **transition 版的关闭动画无效**。
4. Radix 在浮层上提供原点变量：`--radix-dropdown-menu-content-transform-origin`、`--radix-popover-content-transform-origin`、`--radix-select-content-transform-origin`、`--radix-tooltip-content-transform-origin`、`--radix-context-menu-content-transform-origin`。shadcn 已经写了 `origin-(--radix-…)`，这正好替代 transitions.dev 的 `data-origin`。
5. Radix Tooltip 的 `data-state` 有三值：`delayed-open`、`instant-open`、`closed`。`instant-open` 表示在 skipDelay 窗口内从相邻触发器切换过来，语义上正好对应 transitions.dev tooltip 的"已显示时切换目标不带延迟"。

**方案：把 shadcn 组件源码中的动画类整体替换为 `t-*` 工具类**（shadcn 的组件源码归我们所有，这是它的设计初衷）。tw-animate-css 保留安装，供未改动的组件兜底，但 lint 禁止在我们的代码中出现 `zoom-in-95`、`duration-100` 等动画类。

```css
/* apps/web/src/styles/motion/radix.css —— transitions.dev 语义 → Radix [data-state] 关键帧
   只有 from（进入）/ to（退出）：进入时动画结束于元素的计算样式，Tailwind v4 用于居中的 `translate` 属性不受影响 */
@keyframes t-modal-in    { from { opacity: 0; transform: scale(var(--modal-scale)); } }
@keyframes t-modal-out   { to   { opacity: 0; transform: scale(var(--modal-scale-close)); } }
@keyframes t-dropdown-in { from { opacity: 0; transform: scale(var(--dropdown-pre-scale)); } }
@keyframes t-dropdown-out{ to   { opacity: 0; transform: scale(var(--dropdown-closing-scale)); } }
@keyframes t-tt-in       { from { opacity: 0; transform: scale(var(--tt-scale)); } }
@keyframes t-tt-out      { to   { opacity: 0; transform: scale(var(--tt-scale)); } }
@keyframes t-sheet-in    { from { opacity: 0; transform: translate3d(var(--t-sheet-x,0), var(--t-sheet-y,0), 0); filter: blur(var(--panel-blur)); } }
@keyframes t-sheet-out   { to   { opacity: 0; transform: translate3d(var(--t-sheet-x,0), var(--t-sheet-y,0), 0); filter: blur(var(--panel-blur)); } }
@keyframes t-fade-in     { from { opacity: 0; } }
@keyframes t-fade-out    { to   { opacity: 0; } }

@utility t-modal {                                   /* DialogContent / AlertDialogContent */
  &[data-state="open"]   { animation: t-modal-in  var(--modal-open-dur)  var(--modal-ease) both; }
  &[data-state="closed"] { animation: t-modal-out var(--modal-close-dur) var(--modal-ease) both; }
}
@utility t-overlay {                                 /* DialogOverlay / SheetOverlay：纯淡入淡出，不模糊 */
  &[data-state="open"]   { animation: t-fade-in  var(--duration-quick) var(--ease-smooth-out) both; }
  &[data-state="closed"] { animation: t-fade-out var(--duration-quick) var(--ease-smooth-out) both; }
}
@utility t-dropdown {                                /* DropdownMenu/Popover/Select/ContextMenu/Menubar/HoverCard Content（原点沿用 shadcn 的 origin-(--radix-…)） */
  &[data-state="open"]   { animation: t-dropdown-in  var(--dropdown-open-dur)  var(--dropdown-ease) both; }
  &[data-state="closed"] { animation: t-dropdown-out var(--dropdown-close-dur) var(--dropdown-ease) both; }
}
@utility t-sheet {                                   /* SheetContent：panel-reveal 改为侧向 40px */
  &[data-side="right"]  { --t-sheet-x: var(--distance-drawer); }
  &[data-side="left"]   { --t-sheet-x: calc(var(--distance-drawer) * -1); }
  &[data-side="bottom"] { --t-sheet-y: var(--distance-drawer); }
  &[data-side="top"]    { --t-sheet-y: calc(var(--distance-drawer) * -1); }
  &[data-state="open"]   { animation: t-sheet-in  var(--panel-open-dur)  var(--panel-ease) both; }
  &[data-state="closed"] { animation: t-sheet-out var(--panel-close-dur) var(--panel-ease) both; }
}
@utility t-tooltip {                                 /* TooltipContent */
  &[data-state="delayed-open"] { animation: t-tt-in  var(--tt-in-dur)   var(--tt-in-ease) var(--tt-delay) both; }
  &[data-state="instant-open"] { animation: t-tt-in  var(--tt-move-dur) var(--tt-move-ease) both; }
  &[data-state="closed"]       { animation: t-tt-out var(--tt-out-dur)  var(--tt-out-ease) both; }
}
@media (prefers-reduced-motion: reduce) {
  :where(.t-modal, .t-overlay, .t-dropdown, .t-sheet, .t-tooltip) { animation: none !important; }  /* Presence 看到 none，会立即卸载 */
}
```

组件改法示例（`components/ui/dialog.tsx`）：

```text
- className="… data-open:animate-in data-closed:animate-out data-closed:fade-out-0 data-open:fade-in-0 data-closed:zoom-out-95 data-open:zoom-in-95 … duration-100"
+ className="… t-modal"
```

**Accordion**（Radix 用 `--radix-accordion-content-height` 做 height 关键帧）：保留 shadcn 的 `animate-accordion-down/up`，追加 `duration-(--acc-expand) ease-(--acc-ease)`（Tailwind v4 标准语法，会写入 `--tw-duration` 与 `--tw-ease`）。inner 追加：

```css
[data-slot="accordion-content"] > * { transition: opacity var(--acc-expand) var(--acc-ease), filter var(--acc-expand) var(--acc-ease); }
[data-slot="accordion-content"][data-state="closed"] > * { opacity: 0; filter: blur(var(--blur-small)); }
[data-slot="accordion-trigger-icon"] { transition: transform var(--acc-chevron) var(--acc-ease); }
[data-state="open"] > [data-slot="accordion-trigger-icon"] { transform: scaleY(-1); }   /* 替换 shadcn 的 rotate-180 */
[data-slot="accordion-trigger-icon"] path { vector-effect: non-scaling-stroke; }
```

**Tabs 滑动胶囊**：shadcn 的 `tabs-trigger` 用 `data-active:bg-background` 为每个触发器单独画背景，没有 indicator。做法是在 `TabsList` 外包 `SlidingTabsList`：

```tsx
// components/ui/sliding-tabs.tsx（基于 shadcn Tabs）
export function SlidingTabsList(props: React.ComponentProps<typeof TabsList>) {
  const listRef = useRef<HTMLDivElement>(null);
  const pillRef = useRef<HTMLSpanElement>(null);
  useSlidingPill(listRef, pillRef);   // 观察 [data-slot=tabs-trigger][data-state=active]：MutationObserver(attributeFilter:["data-state"]) + ResizeObserver
  return (
    <TabsList ref={listRef} {...props} className={cn("relative", props.className)}>
      <span ref={pillRef} data-slot="tabs-pill" aria-hidden className="t-tabs-pill absolute inset-y-[3px] left-0 rounded-md bg-background" />
      {props.children}
    </TabsList>
  );
}
/* CSS：[data-slot=tabs-list]:has([data-slot=tabs-pill]) [data-slot=tabs-trigger][data-state=active] { background: transparent; box-shadow: none; }  并让 trigger 的 z-index 高于 pill */
```

**TabsContent**：只做进入动画，避免强制挂载两份内容。`[data-slot=tabs-content][data-state=active] { animation: t-page-enter var(--page-slide-dur) var(--page-slide-ease) both }`，其中 `t-page-enter` 从 `translateX(±8px)`、`blur(3px)`、`opacity 0` 开始，方向由 `data-direction` 决定（在 `onValueChange` 中比较新旧 index 得出）。

**Switch**：shadcn `switch-thumb` 用 `data-[state=checked]:translate-x-…` 加 `transition-transform`。把缓动换成 `--toggle-ease`（bounce 1.35，单次超调约 4%），时长用 `--toggle-dur` 350ms。**不采用双回弹 keyframes**：它需要 `.is-init` 才能避免挂载时播放，与 Radix 受控组件配合容易出错。

**Checkbox**：Radix `CheckboxIndicator` 只在选中时挂载（Presence）。在其内部的 path 上：

```css
[data-slot=checkbox-indicator] svg path { stroke-dasharray: var(--check-len, 23); stroke-dashoffset: var(--check-len, 23); }
[data-slot=checkbox-indicator][data-state=checked] svg path { animation: t-check-draw var(--check-draw) var(--check-ease) var(--check-delay) forwards; }
[data-slot=checkbox-indicator][data-state=unchecked] { animation: t-fade-out var(--check-uncheck) var(--check-ease) both; }
```

lucide `check` 的路径 `M20 6 9 17l-5-5` 长度为 15.556+7.071=22.63，取 23。若由 morphicons 渲染，路径可能被归一化，应在挂载时动态测量 `getTotalLength()`。

**Sonner（shadcn toast）**：Sonner 2.0.8 内置动画（`transition: transform 400ms, opacity 400ms, height 400ms…`，状态属性有 `[data-sonner-toast][data-mounted=true|data-removed=true|data-expanded]`）。做法：

1. 在 `toastOptions.className` 上挂 `t-sonner`；
2. 用 CSS 覆盖 `[data-sonner-toast]` 的 `transition-duration` 与 `transition-timing-function`：mounted 时 `--toast-open` 350ms，removed 时 `--toast-close` 250ms，曲线用 `--toast-ease`；
3. 不重写它的堆叠几何（Sonner 自己的 stacking 与 banner-stacking 同源），只统一时间和曲线；
4. lite 档不加 blur。

**Tooltip 延迟设置**：`TooltipProvider delayDuration={0} skipDelayDuration={300}`，意图延迟改由 CSS `--tt-delay` 80ms 承担，与原配方一致。3D 画布边缘等密集区域的 tooltip 单独设 `delayDuration={400}`，以免指针路过时闪烁。

**必须禁用的 shadcn 配置与类**：

- `components.json` 中 `menuColor` 只能取 `"default"`（或 `"inverted"`）。`default-translucent`/`inverted-translucent` 会通过 `transform-menu.ts` 内联 `animate-none! … before:backdrop-blur-2xl before:backdrop-saturate-150`，既关闭动画又引入昂贵的背景模糊。
- 所有 `backdrop-blur-*`、`supports-backdrop-filter:backdrop-blur-*`（vega 风格在 dialog overlay 上默认带 `backdrop-blur-xs`）一律删除。遮罩只用 `bg-black/60` 做纯色半透明。
- 禁止使用 `transition-all`（vega 的 popover 用了），改为枚举具体属性，这也是 transitions.dev 的 "Common mistakes"。

### 4.3 CSS 文件组织与 Tailwind v4 接入

```text
apps/web/src/styles/
├── globals.css                 # @import "tailwindcss"; @import "tw-animate-css"; @import "./motion/index.css"; shadcn 主题色卡
└── motion/
    ├── index.css               # 按顺序 @import 下列文件
    ├── tokens.css              # @theme {...}（5 维 token 及扩展）+ :root {...}（32 组语义变量，逐字取自 _root.css）
    ├── tiers.css               # html[data-motion="lite"|"reduced"] 的变量覆盖（§4.5）
    ├── radix.css               # §4.2 的 keyframes 与 @utility t-modal/t-dropdown/t-sheet/t-tooltip/t-overlay
    ├── shadcn-overrides.css    # accordion/tabs-pill/switch/checkbox/sonner 的 data-slot 覆盖
    └── t/                      # 原样 vendor 的配方（只做三处改动：will-change 挪到激活态、字面量 blur 改为变量、暗色改为 .dark）
        ├── digit.css text-swap.css icon-swap.css badge.css success-check.css shake.css
        ├── skeleton.css shimmer.css tabs.css tooltip.css stagger.css accordion.css
        ├── page-slide.css panel-slide.css resize.css matrix.css think.css stack.css
        └── reel.css stream.css reason.css morph.css falloff.css
```

**Tailwind v4 冲突处理**（`tailwindcss/theme.css` 第 434–436 行默认 `--ease-in: cubic-bezier(.4,0,1,1)`、`--ease-out: cubic-bezier(0,0,.2,1)`、`--ease-in-out: cubic-bezier(.4,0,.2,1)`，第 476–482 行 `--blur-xs…3xl`，第 492–493 行 `--default-transition-*`）：

```css
/* tokens.css */
@theme {
  /* 与 transitions.dev 同名：在 @theme 中显式重定义，让 Tailwind 的 ease-out/ease-in-out 工具类与 token 语义一致 */
  --ease-smooth-out: cubic-bezier(0.22, 1, 0.36, 1);
  --ease-out: ease-out;               /* 原 Tailwind 为 cubic-bezier(0,0,.2,1)。此处按 transitions.dev 统一（仅 tooltip 等使用） */
  --ease-in-out: ease-in-out;
  --ease-bounce: cubic-bezier(0.34, 1.36, 0.64, 1);
  --ease-bounce-strong: cubic-bezier(0.34, 3.85, 0.64, 1);
  --blur-small: 2px; --blur-medium: 3px; --blur-large: 8px;   /* 与默认 xs..3xl 不重名，并生成 blur-small 等工具类 */
  --default-transition-duration: 150ms;                        /* = --duration-quick：hover/focus 的颜色过渡 */
  --default-transition-timing-function: cubic-bezier(0.22, 1, 0.36, 1);
}
:root {
  --duration-stagger: 40ms; --duration-micro: 80ms; --duration-quick: 150ms; --duration-fast: 250ms;
  --duration-medium: 350ms; --duration-slow: 400ms; --duration-very-slow: 500ms;
  --distance-micro: 4px; --distance-small: 6px; --distance-base: 8px; --distance-medium: 12px; --distance-large: 30px;
  --scale-large: .96; --scale-medium: .97; --scale-small: .98; --scale-tiny: .99;
  /* + _root.css 中 32 组语义变量，逐字复制 */
}
```

在组件中引用时长统一写 `duration-(--duration-fast)`，缓动写 `ease-smooth-out`。**lint 规则**：禁止 `duration-[\d+ms]`、`ease-\[cubic-bezier` 这类任意值，禁止 `transition-all`，禁止 `backdrop-blur`。

**暗色（产品默认暗色主题）**：transitions.dev 的颜色 token 用 shadcn 语义变量重写，不再引入独立的 hex：

```css
.dark {
  --tabs-bar-bg: var(--muted);            --tabs-pill-bg: var(--accent);        /* 原暗色值 #202020 / #454545 */
  --tabs-text-muted: var(--muted-foreground); --tabs-text-active: var(--foreground);
  --tt-bg: var(--popover);                --tt-fg: var(--popover-foreground);   /* 原 #222 / #f0f0f0 */
  --shimmer-base: var(--muted-foreground); --shimmer-highlight: var(--foreground); /* 原 #6e6e6e / #ededed */
  --think-base: var(--muted-foreground);  --think-highlight: var(--foreground);
  --matrix-base: color-mix(in oklab, var(--foreground) 18%, transparent);       /* 原 #3a3a3e */
  --matrix-active: color-mix(in oklab, var(--foreground) 72%, transparent);     /* 原 #b8b8c2 */
  --like-color: var(--brand-red);         /* #E93024，用于告警/强调的红 */
}
```

### 4.4 React hooks 与组件 API（`apps/web/src/motion/`）

#### 4.4.1 通用

```ts
export const motion: { tier: MotionTier; ms(name: string, fb?: number): number; invalidate(): void };   // §3.4-h
export function useMotionTier(): MotionTier;                            // 订阅 QualityManager
export function usePresence(open: boolean, closeVar?: string): { mounted: boolean; phase: "open" | "closing" | "closed" };
export function useReplay<T extends HTMLElement>(): [React.RefObject<T>, () => void];   // WAAPI 重放
export function staggerDelay(i: number, n: number, offset?: number, cap?: number): number;
export function bezier(x1: number, y1: number, x2: number, y2: number): (t: number) => number;
```

#### 4.4.2 组件

```tsx
<SwapText value={status} />                                // 04：单行状态，150ms 三相切换
<StatusLine states={["启动 PX4 SITL…","连接 MAVLink…","等待 GPS…"]} hold={2000} />   // 28：多步骤状态
<MotionNumber value={battery} format={v => `${v.toFixed(0)}%`} minInterval={500} />  // 02 泛化版
<ReelCounter value={missionsDone} trigger={missionCompletedEventId} />                 // 26
<SuccessCheck show={uploaded} />                           // 10：动态测量 path 长度
<SkeletonReveal ready={!!firstFrame} skeleton={<Skeleton …/>}>{content}</SkeletonReveal> // 14
<ShimmerText>连接仿真服务中…</ShimmerText>                  // 15：同屏最多 2 个，屏外暂停（IntersectionObserver）
<MatrixLoader variant="scan" rounded />                   // 31
<SlidingTabsList>…</SlidingTabsList>                       // 16
<AlarmStack items={alarms} />                              // 32
<ShakeOnce trigger={alarmEnterSeq}><Badge variant="destructive">低电量</Badge></ShakeOnce>  // 12
```

#### 4.4.3 遥测显示策略与 `useTelemetryBinding`

遥测以 10–50Hz 经 WS 进入 zustand 的 transient store。**遥测显示不走 React 重渲染**：用 `store.subscribe(selector, cb)` 直接写 DOM。

| 字段类 | 例子 | 刷新 | 动效 |
|---|---|---|---|
| C 连续量 | 高度、速度、航向、姿态、坐标、机体处风速、点云"已加载点数" | ≤10Hz（`--telemetry-text-interval`） | **无**。`tabular-nums`、固定小数位；跨阈值时颜色做 150ms 过渡 |
| D 离散量 | 电量整数%、卫星数、航点序号 k/N、在线架数、告警数、任务进度% | 变化即更新，动画 ≤2Hz | number pop-in，只动变化的位；方向由涨跌决定 |
| E 事件 KPI | 完成任务数、本次加载点数汇总、累计飞行里程（任务结束时） | 事件触发 | spinning counter，只播一次 |
| S 状态文字 | 飞行模式、链路状态、任务阶段 | 变化即更新 | text-states-swap；多步骤长任务用 thinking-states |

```ts
function useTelemetryBinding(el: RefObject<HTMLElement>, droneId: string, field: FieldSpec) {
  useEffect(() => store.subscribe(s => s.drones[droneId]?.[field.key], (v) => {
    if (field.cls === "C") throttleWrite(el.current!, field.format(v), field.intervalMs ?? 100);   // rAF 合批写 textContent
    else if (field.cls === "D") popIn(el.current!, v, field.format);                                // §3.4-c
  }), [droneId, field]);
}
```

### 4.5 Reduced motion 与 motion tier 规范

| 档位 | 触发 | 行为 |
|---|---|---|
| **full** | 默认 | 全部配方原样执行 |
| **lite** | FPS 调速器（§3.4-n），或用户选择"流畅优先" | 所有 blur 变量置 0；`backdrop-filter:none`；屏外或超过配额的 shimmer/matrix/reasoning 循环暂停；错峰上限降为 150ms；spinning counter 降级为 number pop-in；input-clear 降级为直接清空；morphicons 改为 icon-swap（无 blur） |
| **reduced** | OS `prefers-reduced-motion: reduce`，或用户选择"减少动效" | 无位移、缩放、旋转、模糊、抖动、循环；状态直接切换，终态必须可见（success check `opacity:1; stroke-dashoffset:0`，stream 文字 `opacity:1`）；3D 相机飞行改为硬切或 150ms 淡切；关闭自动环绕；告警改用颜色、图标和文字 |

```css
/* tiers.css */
html[data-motion="lite"] {
  --blur-small: 0px; --blur-medium: 0px; --blur-large: 0px;
  --digit-blur: 0px; --badge-blur: 0px; --text-swap-blur: 0px; --panel-blur: 0px; --page-blur: 0px;
  --icon-swap-blur: 0px; --check-blur-from: 0px; --clear-blur: 0px; --reveal-blur: 0px; --stagger-blur: 0px;
  --morph-blur: 0px; --toast-blur: 0px; --reel-spin-blur: 0px; --think-blur: 0px; --stream-blur: 0px;
  --stack-blur: 0px; --stack-depth1-blur: 0px; --stack-depth2-blur: 0px;
  --stagger-cap: 150ms;
}
html[data-motion="lite"] *, html[data-motion="lite"] *::before { backdrop-filter: none !important; }
html[data-motion="lite"] .t-shimmer:not(.is-priority)::before,
html[data-motion="lite"] .t-think-text::before { animation-play-state: paused; }

html[data-motion="reduced"] {           /* 用户手动选择时，复用各配方 @media 守卫的效果 */
  --digit-dur: 0.01ms; --text-swap-dur: 0.01ms; --modal-open-dur: 0.01ms; --modal-close-dur: 0.01ms;
  --dropdown-open-dur: 0.01ms; --dropdown-close-dur: 0.01ms; --panel-open-dur: 0.01ms; --panel-close-dur: 0.01ms;
  --page-slide-dur: 0.01ms; --page-fade-dur: 0.01ms; --icon-swap-dur: 0.01ms; --tabs-dur: 0.01ms; --acc-expand: 0.01ms;
  --acc-collapse: 0.01ms; --toast-open: 0.01ms; --toast-close: 0.01ms; --tt-in-dur: 0.01ms; --tt-out-dur: 0.01ms; --tt-delay: 0ms;
  /* …其余 *-dur 同理；用 0.01ms 而不是 0，保证 animationend 仍会触发（Radix Presence、JS 编排依赖这个事件） */
  --digit-distance: 0px; --distance-drawer: 0px; --shake-distance: 0px; --shake-overshoot: 0px;
}
```

规则：

1. **各配方自带的 `@media (prefers-reduced-motion: reduce)` 守卫一律保留**（SKILL.md "Output format" 第 4 条），`tiers.css` 只负责"用户手动选择"与"性能降级"两种来源。
2. 生效档位 = min(OS, 用户, 性能)，用户选择只能让动效更弱，不能强于 OS 设置。
3. morphicons 必须传 `reducedMotion="user"`（其 README 说明默认**不**遵循 OS 设置）。
4. e2e 测试一律以 `page.emulateMedia({ reducedMotion: "reduce" })` 截图，保证结果确定；专门的动效用例再以 full 档配合 `document.getAnimations()` 等待动画结束。

### 4.6 3D 视口的动效语言（与 DOM 共用 token）

| 场景 | 规范 |
|---|---|
| 相机飞行（点击无人机"Follow/聚焦"、双击点云定位） | `T = clamp(0.40 + 0.15·ln(1 + dist/20m), 0.40, 1.20) s`；位置与 target 用 `easeSmoothOut`，FOV 同步插值；**可中断**：新目标从当前位姿重新开始；用户拖拽立即取消。reduced 档改为硬切 |
| 视角模式切换（第三人称/FPV/鸟瞰/自由） | 相机插值 450ms smooth-out；HUD 模式名用 text-swap；切换按钮用 tabs 胶囊；进入 FPV 时 HUD 用 texts-reveal（仅 full 档） |
| **点云节点渐进加载** | 新节点以 **screen-door 渐入**（`hash(gl_VertexID) < uFade` 时 discard；也可让点大小从 0.6× 过渡到 1×），`uFade` 在 `--duration-lod-fade`（250ms）内按 smooth-out 从 0 到 1。不用 alpha 混合，避免排序和深度问题。父节点在子节点渐入完成后才隐藏，防止出现空洞 |
| 选中高亮 | 选择环缩放 0→1，500ms，`--ease-bounce`（与 badge pop 同一语义）。**不做常驻呼吸**；只有告警状态的无人机才有 1Hz ease-in-out 呼吸，reduced 档为静态 |
| 航点添加/删除 | 添加：缩放 0→1，500ms bounce；删除：150ms 淡出（关闭总是更快） |
| 轨迹 | 回放时按时间推进绘制（stroke-draw 的 3D 类比），不做额外缓动 |
| 模态打开期间 | 3D 渲染降频到 ≤15fps 或按需渲染（`invalidate`），把帧预算留给模态动画 |

### 4.7 实测：WebGL 画布上叠加 DOM 动效的开销（性能预算依据）

测试条件：`.cache/research/d02/www/bench.html`，WebGL2 全屏绘制 3 万个 `GL_POINTS`（旋转），两侧各有一块 320px 浮动面板，每块 30 行数值；每个用例跑 8s，取 rAF 平均 fps。headless Chromium 1234 加 SwiftShader，8 核，load 3–6。两轮结果：

| 场景 | run1 fps | run2 fps | 相对基线 |
|---|---|---|---|
| 基线（静态面板） | 36.4 | 38.3（末尾复测 29.6） | 100% |
| 两侧面板 **transform+opacity** 循环开合（panel reveal 去掉 blur） | 31.2 | 34.9 | **约 −10%** |
| 同上，加 **filter: blur(2px)**（panel reveal 原样） | 12.6 | 13.2 | **约 −65%** |
| 两侧面板静态 **backdrop-filter: blur(12px)**（玻璃拟态） | 12.1 | 14.4 | **约 −63%** |
| 60 个数值字段 20Hz 纯文本写入（无动画） | 28.2 | 32.4 | 约 −15～−23% |
| 12 个数值 number pop-in（无 blur），2Hz | — | 31.3 | 约 −18% |
| 12 个数值 number pop-in（blur 2px），2Hz | — | 26.1 | 约 −32% |
| 60 个数值 number pop-in（无 blur），2Hz | 20.4 | — | 约 −44% |
| 60 个数值 number pop-in（blur 2px），2Hz | 11.5 | 15.6 | 约 −60% |
| 20 个 shimmer text 常驻循环 | 15.6 | — | 约 −57% |

说明：软件合成器下，任何 filter/backdrop-filter 都要在 CPU 上逐像素卷积，而且会和 WebGL 光栅化争抢同一批核。真实 GPU 上这些开销会低一个数量级以上。但**本机就是我们的流畅性测试环境**，现场也常见弱集显笔记本，所以预算按这组数据来定。

**性能预算（写入规范）**：

1. 大面积表面（面积大于 240×240px 或全高面板）**不做 blur 动画**：full 档允许 Sheet 打开时的 2px 模糊，lite 档强制为 0。**全站禁用 backdrop-filter**。
2. 同时进行的带 blur 动画元素不超过 12 个；数字 pop-in 每秒总次数不超过 24 次（例如 12 个字段 × 2Hz）。超出部分降级为无 blur。
3. 常驻循环动画（shimmer/matrix/reasoning）同屏不超过 2 个，屏外暂停。
4. 连续量遥测文本写入必须节流并在 rAF 中合批，禁止每条 WS 消息都触发 React setState。
5. CI 中加入动效性能回归：以本页为模板接入真实点云查看器，要求 lite 档下 UI 动效造成的 fps 损失不超过 15%。

### 4.8 版本落点

| 版本 | 动效交付 |
|---|---|
| **V0.1** | `tokens.css`、`tiers.css`、`radix.css`；shadcn 组件动画类替换（Dialog/Sheet/Dropdown/Popover/Select/Tooltip/Accordion/Tabs/Switch/Checkbox/Sonner）；`SwapText`、`MotionNumber`（C/D 类）、`SkeletonReveal`、`ShimmerText`、`MatrixLoader`、`SlidingTabsList`、`useShake`（表单）、badge；3D 相机 `bezier()` 与 LOD screen-door 渐入；QualityManager 接入 motion tier |
| V0.2 | `SuccessCheck`、`ReelCounter`、`StatusLine`（SITL 启动序列）、列表进出（`useListPresence`）、共享 tooltip 工具栏、motion-lint（移植 refine 算法） |
| V0.3–V0.4 | 环境面板参数联动（风速、雨量滑块值用 number pop-in；环境预设切换用 page slide）；天气图层开关 |
| V0.6 | `AlarmStack`（多机告警堆叠）、机群芯片 falloff hover、添加航点 FAB（plus-menu morph） |
| V1.0 | ANet：`StreamText`（Agent 消息）、`ReasonTicker`（协商日志）、`StatusLine`（Agent 状态） |

---

## 5. 对比与推荐

| 方案 | 优势 | 劣势 | 结论 |
|---|---|---|---|
| **transitions.dev（免费 32 个与 token）** | token 化、纯 CSS、每个配方都带 reduced-motion 守卫；视觉克制（小位移、2–3px 模糊、smooth-out），与 shadcn 中性风格和科技灰/黑/白/红产品色一致；变量化后容易分档；2026 年仍活跃（4.4k stars） | 不负责挂载/卸载（与 Radix Presence 不兼容，需要改写为 keyframes）；部分需要 JS；blur 较多，在软件渲染下很贵；Pro 不可用；文档有若干不一致 | **主规范，adopt** |
| tw-animate-css（shadcn 默认，1.4.0） | shadcn 原生；`enter/exit` 关键帧兼容 Radix Presence；变量化（`--tw-enter-*`） | 默认数值（150ms、ease、zoom-95、slide-2）与 token 不一致；`enter/exit` 关键帧**恒含 `filter: blur(var(--tw-enter-blur,0))`** | **承载层**：保留安装，我们的组件改用 `t-*` utility |
| Motion（framer-motion） | 物理 spring、layout 动画、AnimatePresence | 额外 JS 体积；与 CSS token 形成两套体系；transitions.dev SKILL.md 明言 "don't pull in a motion library" | **不引入** |
| morphicons | 任意 stroke 图标互相形变，Procrustes 对齐后旋转自然出现；零依赖 | 只管图标；默认不遵循 reduced-motion | 图标层 **adopt**（另一单元研究），spring 取 snappy |
| View Transitions API | 路由级转场几乎零代码 | 会对大尺寸 WebGL canvas 做快照，开销高；与常驻画布的架构冲突 | V0.x **不用** |
| Sonner（shadcn toast） | 成熟的堆叠、滑动关闭、可访问性 | 动画数值内置 | **adopt 并主题化**（只改时长和曲线） |

**推荐排序**：① transitions.dev skills 与 polish token（规范和配方）；② tw-animate-css（承载，数值被替换）；③ morphicons（图标，与 ① 对齐 spring）；④ refine（只借鉴其算法做 lint）。

---

## 6. 风险与注意事项

1. **Presence 与 transition 不匹配**：原配方的 `.is-closing` 属于 transition，放在 Radix 浮层上关闭动画会直接消失。必须使用 §4.2 的 keyframes 版本。新组件接入时要检查 "closed 时是否有 animation-name"。
2. **blur 与 backdrop-filter 的成本**（§4.7）。shadcn 若干风格默认带 `backdrop-blur-xs`，translucent 菜单带 `backdrop-blur-2xl`，一旦混入，全站帧率会腰斩。用 lint 拦截。
3. **will-change 层爆炸**：配方在常驻选择器上写 `will-change`。机群列表有 N 行、每行 6 个数字时，会产生数百个合成层，吃显存和内存。必须改为只在动画期间挂载。
4. **高频重放**：number pop-in 原版每次都强制 reflow（`void offsetHeight`），在 20Hz × 多字段下是 layout thrash。必须用 §3.4-c 的"替换变化节点"方案，并限频到 ≤2Hz。
5. **getComputedStyle 读取**：原版每次动画都要读若干变量，会触发样式重算。用 `motion.ms()` 缓存，在 tier 或主题切换时清空。
6. **定时器漂移**：后台标签页中 `setTimeout` 被节流，`is-closing`/`is-error` 可能滞留。以 `animationend`/`transitionend` 为主，定时器只做兜底。
7. **Tailwind v4 同名 token 冲突**（`--ease-out`/`--ease-in-out`）：不处理的话，全站 `ease-out` 工具类的实际曲线会随 CSS 层叠顺序变化。§4.3 已给出统一方案。
8. **暗色选择器不一致**：配方用 `html[data-theme=dark]`，shadcn 用 `.dark`。input-clear 在 JS 里读 `data-theme` 来决定白色光带，移植时要改成读 `.dark`，否则暗色下光带不可见。
9. **CJK 文案**：thinking states 的 sizer 必须和正文使用同一字体（中文宽度差异大）；shimmer 的 `background-clip:text` 对中文可用，但 `data-text` 必须与正文同步，i18n 切换时两处都要更新。
10. **初始化动画**：toggle 需要 `.is-init`，tabs 首帧要无过渡地定位，badge 以 `data-open=true` 挂载时会播 slide-in。凡是"首屏即为终态"的组件，都要确保挂载时不播放动画。
11. **Radix Tooltip 不能共享同一个气泡移动**：t-tt 的"气泡在触发器间移动"需要自研组件（仍用 shadcn 的样式 token），只用于 3D 主工具栏。其他位置使用 Radix 的 `instant-open` 近似效果。
12. **headless 测试不稳定**：带动画截图不确定，e2e 默认使用 reduced；动效用例用 `getAnimations()` 等待。SwiftShader 下 fps 绝对值低，只比较相对值。
13. **Pro 与 license**：Pro 源码不在仓库，不要从 index.html 的混淆 demo 逆向。配方 license 按项目约定忽略，但 vendor 文件头部保留出处注释，便于追溯。
14. **颜色硬编码**：`--like-color:#f40051`、`--tabs-*`、`--matrix-*`、`--shimmer-*` 等是配方自带的颜色，必须改为引用我们的色卡变量，品牌红统一为 `#E93024`。

---

## 7. 对设计文档 `01-design.md` 的优化建议

1. **缺少"设计体系 / 交互规范"层**：§34 只写了 "UI: shadcn/ui"。建议新增一章 "Design System"，内容包括色卡（科技灰/黑/白/红）、字体、间距、**动效 token（本文 §3.1）**、图标（morphicons）、图表（lieflat-charts），并在 §42 的 repo 结构中加入 `apps/web/src/styles/motion/` 与 `packages/tokens/`（token 单一事实源，同时导出 CSS 与 TS，供 Three.js 使用）。
2. **§38 的 UI 布局要改成"全屏画布加浮动面板"**：原稿是左/右固定栏。若固定栏占位，面板开合会改变画布尺寸，每帧触发 `renderer.setSize`，还会导致点云 LOD 重算。建议让 3D 画布永远全屏，面板作为浮层，开合只动 transform/opacity，并定义面板的折叠态（仅保留图标条）。
3. **§28/§37 需要补充"遥测显示刷新策略"**：WS 10–50Hz 不等于 UI 刷新频率。应按字段分为 C/D/E/S 四类（§4.4.3），连续量文本 ≤10Hz 且无动画，离散量动画 ≤2Hz。遥测渲染绕过 React，走 transient store 直接写 DOM。
4. **§14 自适应 LOD 应扩展为统一的 QualityManager**：原稿只讨论点云 LOD。建议统一调度点云 point budget、DPR、EDL、环境粒子数和 **UI motion tier**，降级顺序为"先 UI blur，后 DPR，最后点云预算"（§3.4-n）。依据是本文实测：UI blur 与 backdrop-filter 在软件渲染下会吃掉约 65% 的帧率，而它带来的体验收益远小于点云密度。
5. **§39 Timeline 需要补充交互细则**：拖动时关闭一切过渡，1:1 跟随；倍速用 tabs 胶囊；播放/暂停用 morphicons；事件标记用 badge pop；时间标签按 10Hz 纯文本、tabular-nums 显示。
6. **§40 相机模式需要定义转场规范**：时长公式、曲线、可中断性，以及 reduced 档改为硬切（§4.6）。否则各处相机动画会各写各的。
7. **缺少告警/通知模型**：多机场景下告警会并发出现。建议定义 severity→表现的映射：info 用 toast；warning 用横幅加角标；critical 用横幅、角标、单次 shake 加持续红色，外加可选声音。同时定义去重与节流（同一告警 10s 内不重复触发 shake，横幅最多堆叠 3 层）。
8. **缺少加载与流式状态规范**：V0.1 的核心体验是"渐进加载点云"。应当定义世界加载进度（texts-reveal 标题加进度）、节点流式指示（matrix loader 加"已加载/预算"数字）、面板骨架（skeleton-reveal），以及点云节点 screen-door 渐入，避免"跳出式"加载。
9. **缺少可访问性章节**：reduced-motion 三档、告警不能只靠动效或颜色（必须配文字和 `role=alert`）、键盘可操作（Radix 已提供）、对比度（红 #E93024 放在黑底上做文字时需要复核对比度）。
10. **§43 MVP 范围应纳入"设计体系地基"**：token、浮层动效替换、tier 机制的实现成本约 1–2 人日，但如果推迟到后期再统一，需要逐个组件返工。建议列为 V0.1 必做项，并在 §44 V0.1 功能清单中加入 "Design tokens & motion foundation"。
11. **§12/§21 的 WebGPU 可视化与 DOM 叠加的关系**：原稿只讨论 GPU 侧，没有提到 DOM 覆盖层会和画布争抢合成与 CPU（软件渲染时尤其明显）。建议在 §16 场景结构旁补一张"HUD/DOM 层规范"：DOM 覆盖层只动 transform/opacity，禁用 backdrop-filter，并规定常驻动画的数量上限。
12. **§35/§45 的 PX4 SITL 启动等长流程需要 UI 表达**：SITL 启动、MAVLink 连接、GPS 锁定是多步骤长任务。建议用 thinking-states 状态行，配合每步完成后的 success-check，替代无信息量的转圈 spinner。
