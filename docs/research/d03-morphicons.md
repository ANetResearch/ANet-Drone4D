# D03 研究笔记：morphicons —— 全站图标体系与图标切换

> 研究单元：d03 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §34（Frontend 技术栈）、§37（更新频率）、§38（UI 设计）、§39（Timeline）、§40（Drone Interaction）
>
> 仓库快照：`refs/design/morphicons` @ `38d2a72`（2026-08-28，★2705，shallow clone）。master 就是已发布的 **`morphicons@1.7.1`**（npm latest，2026-08-28）。1.0.0 发布于 2026-08-01，4 周内出了 11 个版本，属于 2026 年的新仓库。MIT，零运行时依赖。下文路径均相对仓库根。
>
> 配套数据包：**`lucide@1.48.0`**（npm latest，2026-09-24），IconNode 数据包，共 1854 个图标文件、2115 个导出名（含别名）。对照组 `lucide-react@1.48.0`。三个包都用 `npm pack` 解到了 `.cache/research/d03/`。
>
> 本机实测产物，都在 `/data/projs/anet-drone/.cache/research/d03/`：
> - `inventory.mjs`：本系统图标清单（可直接当 `web/src/components/icons/registry.ts` 的种子），含 6 个自定义 stroke 图标
> - `verify.mjs` / `verify.out`：清单校验（202 个 lucide 图标全部存在且都是 canonical 名），自定义图标网格校验，48 个切换对的 Procrustes 指标
> - `bench-pairs.mjs` / `bench-split.mjs` / `bench-pairs.out`：67 个候选对的 plan 耗时、单帧耗时、θ/σ/residual
> - `dom-bench.{html,cjs}`：headless Chromium 下 N 个图标同时 morph 的真实帧时间，以及静止时 rAF 计数
> - `rbench/`：React 19.3 下挂载 300/1000/3000 个图标的对比（lucide-react / 纯 core 静态 / MorphIcon），以及 bundle 体积
> - `prewarm.mjs`：plan 缓存预热前后首次 `morphTo` 的延迟
> - `sheet.png`、`sheet2.png`：自定义图标在 16/20/24/48px 下的渲染，以及 35 个切换对在 t=0…1 的中间帧，用来肉眼验收
>
> 环境：Node 22.12，Chromium（playwright chromium-1234，SwiftShader 软件渲染），React 19.3.0 production build，esbuild 0.25。

---

## 0. 结论速览

| 仓库 / 制品 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **morphicons 1.7.1** `morphicons/dom`（`createMorph`、`canonicalD`） | 图标 morph 引擎：单例 rAF 调度、spring、中途打断、plan 缓存；`canonicalD` 输出静态 path | **adopt**。作为自研 `<StateIcon>` 和 `<Icon>` 的底层 | V0.1 起 | ★★★★★ |
| morphicons `morphicons/react`（`MorphIcon`） | 官方 React 绑定，支持 uncontrolled、controlled、imperative 三种模式 | **adopt（有限）**。只用于"拖拽进度驱动的图标"这类 controlled 场景，比如面板拖拽收起。一般场景用自研 `<StateIcon>`，因为 swap 回退需要在同一个 `<svg>` 里放两条 path | V0.1–V0.2 | ★★★★ |
| morphicons `morphicons`（纯 core：resample → Procrustes → polar interp → serialize） | 与 DOM 无关的形状对齐与插值数学 | **port / reference**。移植到 V0.6 编队变换预览（Procrustes + 极坐标插值），spring 移植到 HUD 数值动画 | V0.6 | ★★★★ |
| morphicons `morphicons/adapters` › `canvasTarget`，以及 `website/lib/icon-sprite.ts` 的模式 | 把图标变成像素：给 3D 场景用的 sprite 和 texture | **adopt**。选中机的状态 sprite 做 morph；多机图标用 atlas（自研，见 §3.7） | V0.2（选中机），V0.6（多机 atlas） | ★★★★ |
| morphicons vue / svelte / react-native / element / astro；`maskTarget`；`svgToIcon` | 其他框架绑定和格式适配 | **skip**（本项目是 React SPA，图标都是 IconNode 数据） | — | — |
| **lucide 1.48.0**（数据包，不是组件包） | 全站图标数据的唯一来源，已内置 `drone` 图标 | **adopt**，锁精确版本 | V0.1 | ★★★★★ |
| lucide-react 1.48.0 | 组件包 | **skip**。不安装，shadcn 的 import 由 codemod 改写到本项目的兼容层 | — | — |
| transitions.dev `icon-swap`（scale 0.25 + blur 2px + opacity，250ms ease-in-out） | 形变质量不达标的图标对，用 crossfade 回退 | **port**。改写成"同一个 `<svg>` 内两条 path"的 WAAPI 版本，保证 shadcn 的 `[&>svg]` 选择器仍然生效 | V0.1 | ★★★★ |

**核心结论**

1. **图标数据：`lucide@1.48.0` 加 6 个自定义 stroke 图标。** 清单共 208 个（202 个 lucide、6 个自定义），全部由脚本对照 lucide 1.48 导出表校验过：名字存在，而且都是 canonical 名。lucide 1.x 把 `Home`、`AlertTriangle`、`Loader2`、`Trash2`、`Building2` 这类旧名降成了别名，清单统一用 `House`、`TriangleAlert`、`LoaderCircle`、`Trash`、`BuildingComplex`。**lucide 已经有 `drone`**（四旋翼俯视，9 个子路径），P600 直接用它，不需要自绘。自定义图标只补 lucide 缺的语义：`PointCloud`、`SandDust`（沙尘）、`OctreeLod`、`DroneHexa`（异构六旋翼）、`Formation`（编队）、`CameraFrustum`。
2. **渲染：不装 lucide-react。** 静态 `<Icon>` 直接渲染 morphicons 纯 core 生成的 canonical `d`，只有一条 `<path>`，没有 hook 和运行时。动态 `<StateIcon>` 用 `morphicons/dom` 的 `createMorph`。两者在静止状态下像素一致，所以一个静态图标随时可以升级成 morph，不会跳变。实测 React 19 挂载 1000 个图标：**纯 core 静态 14.5 ms，MorphIcon 31.2 ms，lucide-react 61.5 ms**；DOM 节点数 2001 对 5051。bundle 方面，202 个图标用 lucide-react 是 19.2 KB gz；lucide 数据加 morphicons react/dom 是 18.9 KB gz，这个数已经包含了约 8 KB 的 morph 运行时。
3. **切换机制分三类。**
   - **morph**：同一对象的离散状态，形状同族，残差小，并且在中间帧目测过。例如 play↔pause、menu↔x、电量和信号分档、告警升级、loader→check/x、面板开合、天气。
   - **swap**：`*-Off` 这类"切口"变体，以及形状无关的对。例如 eye↔eye-off、bell↔bell-off、相机模式循环。在 t≈0.3–0.6 时它们会出现漩涡或团块，不合格，改用 transitions.dev 的 icon-swap。
   - **CSS rotate**：连续角度，例如航向、风向。**未登记的图标对默认走 swap**，只有白名单里的对才走 morph。
4. **性能。** rAF 不常驻：实测静止 500 ms 内 rAF 调用 0 次，settle 之后也是 0 次。单个图标在飞行中每帧花 **约 30–380 µs**（Node 实测，子路径越多越贵），其中 97% 以上是 `serialize` 拼字符串。并发 ≤10 个时无感；50 个同时 morph 时 p95 帧时间 22–29 ms；100 个 9 子路径的图标平均 29 ms/帧。和 Three.js 共用主线程，所以**必须加并发预算（K=8）**，遥测驱动的图标还要加迟滞，最短 1.5 s 才允许换一次图标。
5. **冷启动。** 首次 `morphTo` 要现算 plan 并经历 JIT，耗时 **2–28 ms**，第一次点击会掉帧。在 `requestIdleCallback` 里用"假 `PathEl` 加 `seek`"把白名单对的 plan 预热进 WeakMap 缓存，之后降到 **0.03–0.46 ms**。
6. **shadcn 集成。** shadcn v4 用 `[&_svg]`（45 处）、`[&>svg]`（32 处）、`has-[>svg]`（8 处）给图标定尺寸和间距，所以**图标组件的根节点必须是 `<svg>`**，swap 回退也必须在同一个 svg 里完成。shadcn 组件一共只 import 16 个 lucide-react 名字（`XIcon`、`CheckIcon`、`ChevronDownIcon` 等）。方案是在 `@/components/icons` 里提供同名兼容导出，由 codemod 把 import 路径改掉，再用 lint 禁止 `lucide-react` 和 emoji。
7. **色彩约束反过来强化了 morph 的价值。** 产品色只有灰、黑、白、红，没有绿色和黄色可以表示"正常"或"警告"，状态只能靠**形状**表达：CircleCheck、TriangleAlert、OctagonAlert，以及电量档位的格数。形状变化本身就在传达状态，morph 让这种变化连续、可被注意到。红色 `#E93024` 只留给 active 和告警。

---

## 1. 仓库概览

| 项 | 值 |
|---|---|
| 仓库 | github.com/guillermolg00/morphicons（单一维护者 Guillermo） |
| Star / 活跃度 | ★2705；1.0.0 于 2026-08-01 发布，1.7.1 于 2026-08-28 发布，4 周 11 个版本 |
| 定位 | 面向 stroke 图标（Lucide、Tabler、Heroicons outline、Iconoir、自定义 path）的通用 morph："any icon morphs into any other"。旋转不需要手工声明，由 2D Procrustes 加极坐标插值自动得出 |
| 运行时依赖 | **0**；react、vue、svelte、react-native 都是 optional peer |
| 入口（subpath exports） | `.`（纯 core）、`./dom`（driver）、`./adapters`、`./react`、`./react-native`、`./vue`、`./svelte`、`./element`（`<morph-icon>`）、`./astro`；ESM only，`sideEffects:false` |
| 体积（gzip，README 的 gate） | core 6.60 KB；core+dom 7.12；+react 8.01；adapters 合计 4.32（canvasTarget 单独 0.48）。本地 esbuild 复测 react+dom 为 7.99 KB，与 README 一致 |
| 测试 | CLAUDE.md 写 259 tests / ~13,941 asserts；我数到 `test()` 调用 256 处。README 里写的 156 是旧数字 |
| 工具链 | Bun（测试和包管理）、tsdown、Biome、TypeScript 7（tsgo）。使用方不受影响 |
| 代码量 | `src/` 共 3950 行，其中 core 1472 行、dom 520 行、react 272 行 |

设计哲学（`CLAUDE.md` 和 `docs/adr/0001` 的硬规则）：

- **core 永不触碰 DOM**：纯函数，输入 icon 数据，输出 `d` 字符串和数字。
- **冻结两条契约**：输入契约 `IconInput = IconNode | d string`；写入契约 `PathEl = { setAttribute }`。任何外来格式都只能通过 `morphicons/adapters` 转换成这两条契约之一，并且每个 adapter 有独立的 size gate。
- **五个绑定共享一份生命周期契约**：lazy driver；controlled 模式优先；退出 controlled 后重新定基（clean re-entry）。
- **形变质量靠肉眼验收**：playground 用 t=0.25/0.5/0.75 冻结帧，发现问题后把它编码成度量或 tie-break（λ 就是这么来的），再补测试钉住。

---

## 2. 源码结构与关键模块

### 2.1 文件地图

| 文件 | 行数 | 职责 / 关键符号 |
|---|---|---|
| `src/core/types.ts` | 34 | `IconNode = ReadonlyArray<[tag, attrs]>`，`IconInput`，`CubicPath {pts: Float64Array, closed}`（打包为 `[p0,c1,c2,p1,…]`），`Sampled` |
| `src/core/parse.ts` | 249 | `parsePath(d)`：处理绝对/相对命令、H/V/S/T 简写（控制点反射）、紧凑 arc flag、科学计数法，输出 `RawSubpath[]` |
| `src/core/normalize.ts` | 356 | `iconToCubics`：把所有 primitive 统一转成 cubic。`builder()` 包含 line、quad、arc（SVG F.6，切成 ≤90° 的片段，每片 α=4/3·tan(Δθ/4)）；`ellipsePath`（KAPPA=0.5523）；`rectPath`（含圆角）；`fitIcon(input, viewBox, grid=24)` |
| `src/core/resample.ts` | 259 | `resamplePath(path, N=64)`：弧长等距采样，8 点 Gauss-Legendre 积分 `|B′|`，`detectCorners`（阈值 22.5°）把角点锚定为精确采样点，最大余数法分配点数，Newton 加二分做弧长反演 |
| `src/core/plan.ts` | 413 | `procrustes()`（闭式解）、`alignPair()`（2 个方向 × N 个环形偏移，score=res+λ|θ|/π）、`costMatrix`、`bestPermutation`（≤8 时穷举）、`bestSurjection`（满射）、`applyGlobal`（全局 hybrid 和 block transport）、`buildPlan()` |
| `src/core/interpolate.ts` | 59 | `interpPolar(plan, t, out)`：零分配，写入预分配的 `Float64Array`；`interpLinear` 仅作对照 |
| `src/core/serialize.ts` | 54 | `serialize()`：飞行中输出 `M/L` 折线，保留 2 位小数；`cubicsToPathD()`：静止时输出 canonical，保留 4 位小数，保证不同 JS 引擎输出字节一致，避免 SSR hydration mismatch |
| `src/core/spring.ts` | 48 | `Spring`：阻尼谐振子，半隐式 Euler，1/240 s 子步；`SPRING_PRESETS` |
| `src/dom/index.ts` | 300 | `createMorph(el, icon, {reducedMotion})` 返回 `Morph{morphTo,set,seek,progress,reducedMotion,destroy}`；**单例 rAF 调度器**；三个 WeakMap 缓存 `samples/canon/plans`；`canonicalD()` |
| `src/dom/controller.ts` | 220 | 框架无关的绑定控制器（Svelte 和 element 直接复用），`computeInitialD`、`frozenD` |
| `src/react/index.tsx` | 272 | `MorphIcon`（forwardRef）、`MorphHandle{morphTo,set}`；`useState` 固定 initialD，React 永远不会改写 `d`，改写都由 driver 在 vdom 之外完成 |
| `src/adapters/canvas.ts` | 138 | `canvasTarget(canvas|ctx, {viewBox, strokeWidth, color, clear, onWrite})`：每帧执行 `stroke(new Path2D(d))`；按短边等比缩放并居中（letterbox）；颜色优先级为 color 选项 > 计算后的 CSS color > 现有 strokeStyle |
| `src/adapters/mask.ts` | 155 | `maskTarget`：给 CSS mask 图标用，两个引用型 `<mask>` 做双缓冲（ADR 0002） |
| `src/adapters/svg.ts` | 96 | `svgToIcon(markup)`：SVG 字符串转 IconInput，拒绝 fill 图标和 transform，按 viewBox 自动 fit 到 24 网格 |
| `website/lib/icon-sprite.ts`、`website/components/canvas-map.tsx` | — | **宿主有自己渲染循环时的范式**（这里是 Mapbox）：每个图标一块独立小 canvas，`onWrite` 置 dirty，宿主在自己的帧里按需上传。可以直接迁移到 Three.js |

### 2.2 数据流（plan 是核心产物）

```
icon A ─┐
        ├→ iconToCubics → resamplePath(N=64) → match(subpaths) → alignPair(Procrustes) → applyGlobal
icon B ─┘                                                                     ↓
                                                         MorphPlan{items: PlanItem[], n}   ← 按 (A,B) 引用缓存
                                                                              ↓
                         spring.step(dt) → x → interpPolar(plan, x, out) → serialize(out) → el.setAttribute("d")
                                                                              ↓ settle
                                                           el.setAttribute("d", canonicalD(target))
```

`PlanItem` 的字段：`a`（A 按对应关系重排后的点）、`aC`（A 去质心）、`bT`（B 变换到 A 坐标系：R(−θ)(b−c_B)/σ）、`bO`（B 的原始朝向）、`ca/cb`（两端质心）、`theta`、`lnSigma`、`res`、`closed`，以及 `block{off, drift}|null`。

### 2.3 DOM driver（`createMorph`）的状态机

- 状态：`target`、`rest`（为 true 表示 `d` 就是 target 的 canonical）、`plan/out/closed`、`t`、`flying`、`dead`。
- `morphTo(icon, spring)`：
  - 如果 `icon === target` 并且当前处于静止或飞行中，直接 **no-op**（同一目标不会重启 spring）；
  - 如果 `motionOff()`（reducedMotion 为 always，或为 user 且 `matchMedia('(prefers-reduced-motion: reduce)')`），退化为 `set`；
  - 否则 `retarget`：静止时用缓存的 `planBetween(target, icon)`；飞行中用 `buildPlan(snapshot(), sampled(icon))`，以当前渲染缓冲作为起点，**不缓存**。然后执行 `spring.start()`，**速度保留**并钳制在 ±14。
- `tick(dt)`：`spring.step(dt)` → `render(spring.x)`；settle 条件为 `|1−x|<0.001 ∧ |v|<0.02`，满足后 `stop()` 并 `settle()`，settle 会把 `d` 吸附回 canonical（<0.02 px 的跳变，肉眼不可见）。
- `seek(icon, t)`：controlled 模式的原语，不启动调度器，速度清零。`progress = t` 是它的语法糖。
- **调度器**：`tickers: Set<Ticker>` 加一个 rAF。`dt=(ts−last)/1000`，钳制在 [0, 0.1]；刚启动的第一帧 dt=0。集合为空时 `cancelAnimationFrame` 并复位。**没有 morph 在飞时不存在任何 rAF 或定时器**，本机已实测验证。
- **缓存**：`samples`、`canon`、`plans` 都是以 IconNode 引用为 key 的 WeakMap。**字符串 d 不缓存**，每次重算。所以本项目的图标一律用模块级 IconNode 常量，并保证引用稳定。

### 2.4 React 绑定（`MorphIcon`）要点

- Props：`icon | from+to+progress`、`spring`（`"smooth"|"snappy"|"bouncy"` 或 `{stiffness,damping}`）、`reducedMotion`（默认 `"never"`）、`size=24`、`color=currentColor`、`strokeWidth=2`、`absoluteStrokeWidth`、`label`，其余 SVG props 透传。`viewBox` 固定为 `0 0 24 24`，但可以被 rest 覆盖。
- 只渲染**一个 `<path>`**：所有子路径合并到同一个 `d`。结果是 DOM 节点更少，但不能单独给某个子元素着色（没有 duotone）。
- 可访问性：没有 `label` 时设 `aria-hidden`；有 `label` 时设 `role="img"` 并加 `<title>`。
- 生命周期：用 `useIsoLayoutEffect`（`typeof document==="undefined"` 时退回 `useEffect`）在 mount 时创建 driver。effect 的声明顺序是 reducedMotion → icon → controlled，所以"同一次 commit 里同时改 policy 和 icon"时，新 policy 会作用于这一次 morph。
- MorphIcon 的 JSX 显式写了 children，调用方**无法往里面注入第二条 path**。这就是本项目 swap 回退不能基于 MorphIcon、要自研 `<StateIcon>` 的原因（§3.4）。
- SSR：initialD 由纯 core 计算，只算一次，服务端和客户端字节一致。本项目是 Vite SPA，用不到 SSR，但如果以后文档站用 Next，可以直接复用。

---

## 3. 可复用算法与实现（伪代码与参数）

### 3.1 形状管线公式（port 时照抄即可）

| 步骤 | 公式 / 参数 |
|---|---|
| 归一化 | Line：`C1=P0+⅓(P3−P0)`，`C2=P0+⅔(P3−P0)`。Quad：`C1=Q0+⅔(Q1−Q0)`，`C2=Q2+⅔(Q1−Q2)`。Circle：4 段 cubic，`k=0.5523·r`。Arc：按 F.6 转成中心参数化，切成 ≤90° 的片段，每片 `α=4/3·tan(Δθ/4)` |
| 重采样 | 每个子路径采 N=64 个点；用 8 点 Gauss-Legendre 求弧长；切向不连续超过 22.5° 的点视为角点并锚定；角点之间按弧长比例分配区间，采用最大余数法、每段至少 1 个区间、总数严格等于 N−1（闭合路径为 N）；闭合路径**只锚定角点**，起点 M 不算，保证采样与形状本身绑定 |
| 子路径匹配 | 代价为 `dist(质心)+0.35·|ΔL|`。子路径数相等时，≤8 条穷举全排列（带剪枝），更多时贪心；数量不等时做**满射**，多出的一侧复制最近的子路径（"细胞分裂"），不会凭空出现或塌缩成一个点 |
| Procrustes | `θ*=atan2(Sxy−Syx, Sxx+Syy)`；`σ*=[cosθ*(Sxx+Syy)+sinθ*(Sxy−Syx)]/Σ‖a‖²`；`res=√(Σ|σRa−b|²/Σ‖b‖²)` |
| 平局裁决 | `score=res+0.05·|θ|/π`。两个遍历方向都要试；闭合路径还要试 N 个环形偏移，O(N²)，约 4K 次运算 |
| 全局 hybrid | 所有子路径拼接后再做一次 Procrustes，若 `res<5e-3`，所有 item 共享同一组 (θ,σ)，并启用 block transport：`c_k(t)=c_Ak+t·d_k+(σᵗR(tθ)−I)(c_Ak−g_A)` |
| 极坐标插值 | `P(t)=c(t)+σ*ᵗ·R(t·θ*)·[(1−t)·aᶜ+t·b̃]`，其中 `σᵗ=exp(t·lnσ)`。t=0 和 t=1 两端精确；spring 过冲（t>1）时自然外推 |
| 序列化 | 飞行中 `M/L` 保留 2 位小数；静止时输出 canonical cubic，保留 4 位小数 |

### 3.2 Spring 与 motion token（和 transitions.dev 对齐）

`ẍ = k(1−x) − c·ẋ`，半隐式 Euler，子步 h=1/240 s，每帧最多 16 个子步；`ζ=c/(2√k)`。下表是本机仿真结果（`spring.mjs`，60 Hz）：

| preset | k | c | ζ | t90 | settle | 过冲 | 本项目用途 |
|---|---|---|---|---|---|---|---|
| snappy（库默认） | 420 | 30 | 0.73 | 133 ms | 417 ms | 2.6% | **toggle**：用户点击触发的开关（menu、play、panel、lock） |
| smooth | 170 | 26 | 1.00 | 300 ms | 750 ms | 0 | **state**：表达"状态变化过程"的切换，例如天气、主题、loader→check |
| bouncy | 300 | 14 | 0.40 | 117 ms | 933 ms | 24% | **禁用**，和科技风格调性不符 |
| hud（自定义） | 900 | 60 | 1.00 | 133 ms | 350 ms | 0 | 密集 HUD 或列表内的状态图标，settle 最短，占用预算时间最少 |

transitions.dev 的 token（`skills/transitions-polish/_root.css`）：`--duration-fast: 250ms`，对应 icon swap、dropdown、tabs；`--duration-very-slow: 500ms`，对应 success check；主缓动 `cubic-bezier(0.22,1,0.36,1)`（在仓库里出现 44 次）。对应关系：swap 固定用 250 ms；morph 的 snappy 在感知上约等于 250 ms（t90=133 ms）；smooth 约等于 500 ms，和 "success check" 的节奏一致。

### 3.3 图标注册表与静态渲染（V0.1）

```ts
// web/src/components/icons/custom.ts —— 自定义图标：24 网格、只用 stroke、不带 transform（见 §4.1 末尾）
export const PointCloud: IconNode = [...]; export const SandDust: IconNode = [...]; // …

// web/src/components/icons/registry.ts
import { Menu, X, Drone, Play, Pause, /* … 只写具名 import，禁止 import { icons } */ } from "lucide";
import type { IconNode } from "morphicons";
import * as C from "./custom";
export const ICONS = {
  "nav.menu": Menu, "close": X, "drone.quad": Drone, "tl.play": Play, "tl.pause": Pause,
  "layer.pointcloud": C.PointCloud, "env.sand": C.SandDust, /* … 共 208 项，见 §4.1 */
} as const satisfies Record<string, IconNode>;
export type IconKey = keyof typeof ICONS;
// 注意：morphicons 的 IconInput 也接受字符串 d。本项目的 <Icon> 只接受 IconKey | IconNode，
// 不接受裸 d 字符串，避免 key 和 d 之间产生歧义。
export const resolveIcon = (i: IconKey | IconNode): IconNode => (typeof i === "string" ? ICONS[i] : i);
```

```tsx
// web/src/components/icons/icon.tsx —— 静态图标：没有 hook，也没有运行时，只有一条 <path>
import { canonicalD } from "morphicons/dom";           // 按 IconNode 引用做 WeakMap 缓存
export const Icon = memo(forwardRef<SVGSVGElement, IconProps>(function Icon(
  { icon, size, label, className, ...rest }, ref) {
  return (
    <svg ref={ref} data-icon="" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
         width={size} height={size} fill="none" stroke="currentColor"
         strokeLinecap="round" strokeLinejoin="round"
         role={label ? "img" : undefined} aria-hidden={label ? undefined : true}
         className={cn("icon", className)} {...rest}>
      {label ? <title>{label}</title> : null}
      <path d={canonicalD(resolveIcon(icon))} />
    </svg>
  );
}));
```

```css
/* 全局 CSS，用 :where 把优先级压到 0，shadcn 的 [&_svg]:size-4 等规则总能覆盖 */
:where(svg[data-icon]) { width: 1rem; height: 1rem; flex-shrink: 0; }
svg[data-icon] { stroke-width: var(--icon-stroke, 1.5px); }
svg[data-icon] path { vector-effect: non-scaling-stroke;   /* 描边宽度按屏幕像素算，不随 size 缩放 */
                      transform-box: view-box; transform-origin: 50% 50%; }  /* 给 swap 的 scale 用 */
```

为什么用 `vector-effect` 而不用 `absoluteStrokeWidth`：MorphIcon 和 lucide-react 的 `absoluteStrokeWidth` 按 **size prop** 计算，而 shadcn 用 CSS（`size-4`）控制尺寸，size prop 仍然是默认的 24。两者一错位，16px 图标的描边会被错误地算成 1px。`non-scaling-stroke` 让描边在任意尺寸下都等于 `--icon-stroke`（默认 1.5 px，比 lucide 默认值在 16px 下的 1.33 px 略粗，在 24px 下的 2 px 更细，整体更"工程感"）。48px 以上的大图标局部设 `--icon-stroke: 2px`。

### 3.4 `<StateIcon>`：morph 与 swap 二选一，加预算和策略（V0.1）

```tsx
// web/src/components/icons/state-icon.tsx
import { createMorph, canonicalD, type Morph } from "morphicons/dom";
type SpringName = "snappy" | "smooth" | "hud";
const SPRINGS = { snappy: "snappy", smooth: "smooth", hud: { stiffness: 900, damping: 60 } } as const;
const SETTLE_MS = { snappy: 450, smooth: 800, hud: 380 };

export const StateIcon = memo(function StateIcon({ icon, spring = "snappy", label, className, ...rest }: StateIconProps) {
  const node = resolveIcon(icon);
  const [initialD] = useState(() => canonicalD(node));          // React 永不改写这个 d
  const main = useRef<SVGPathElement>(null), ghost = useRef<SVGPathElement>(null);
  const drv = useRef<Morph | null>(null), last = useRef(node);
  const policy = useUiMotion((s) => s.iconPolicy);               // zustand 状态："full" | "swap" | "off"

  useLayoutEffect(() => {                                        // mount：在自有 <path> 上创建 driver
    drv.current = createMorph(main.current!, node, { reducedMotion: "user" });
    return () => { drv.current?.destroy(); drv.current = null; };
  }, []);

  useLayoutEffect(() => {
    const from = last.current, m = drv.current;
    if (!m || from === node) return;
    last.current = node;
    const mode = policy === "off" ? "none" : policy === "swap" ? "swap" : pairMode(from, node); // 查白名单
    if (mode === "morph" && morphBudget.acquire(SETTLE_MS[spring])) { m.morphTo(node, SPRINGS[spring]); return; }
    m.set(node);                                                  // 主 path 直接跳到目标
    if (mode === "swap") playSwap(ghost.current!, main.current!, canonicalD(from));
  }, [node, policy, spring]);

  return (
    <svg data-icon="" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round"
         strokeLinejoin="round" role={label ? "img" : undefined} aria-hidden={label ? undefined : true}
         className={cn("icon", className)} {...rest}>
      {label ? <title>{label}</title> : null}
      <path ref={ghost} d="" />                                    {/* swap 时的"旧图标"幽灵层 */}
      <path ref={main} d={initialD} />
    </svg>
  );
});

// transitions.dev icon-swap 的参数（250ms、ease-in-out、scale .25、blur 2px），改用 WAAPI，放在同一个 svg 内
const SWAP_T = { duration: 250, easing: "ease-in-out" };
const OUT = [{ opacity: 1, transform: "scale(1)", filter: "blur(0)" }, { opacity: 0, transform: "scale(.25)", filter: "blur(2px)" }];
function playSwap(ghost: SVGPathElement, main: SVGPathElement, fromD: string) {
  ghost.setAttribute("d", fromD);
  ghost.animate(OUT, { ...SWAP_T, fill: "forwards" }).finished.then(() => ghost.setAttribute("d", ""));
  main.animate([...OUT].reverse(), SWAP_T);
}

// 并发预算：同时最多 K 个 morph，超出的直接 set（瞬切），不会让主线程被图标拖垮
const K = 8; let active = 0;
export const morphBudget = { acquire(ms: number) { if (active >= K) return false; active++; setTimeout(() => active--, ms); return true; } };

// 白名单：用 IconNode 引用做 key，两个方向都要登记；未登记的对一律返回 "swap"
const MORPH_OK = new WeakMap<IconNode, WeakSet<IconNode>>();
export function allowMorph(a: IconNode, b: IconNode) { for (const [x, y] of [[a, b], [b, a]]) (MORPH_OK.get(x) ?? MORPH_OK.set(x, new WeakSet()).get(x)!).add(y); }
const pairMode = (a: IconNode, b: IconNode) => (MORPH_OK.get(a)?.has(b) ? "morph" : "swap");
```

`reduced-motion` 的处理：`StateIcon` 固定传 `reducedMotion:"user"`，所以系统开启减少动态后 morph 自动变成瞬切。swap 由 WAAPI 执行，需要自己判断 `matchMedia('(prefers-reduced-motion: reduce)')`，为 true 时 `playSwap` 直接返回。E2E 截图测试时，把全局 `iconPolicy` 设为 `"off"`，保证截图确定。

### 3.5 plan 预热（消除首次点击卡顿）

```ts
// app 启动后，在 idle 时分片执行。只用公开 API：假 PathEl 加 seek，会在 rest 状态下触发 planBetween，结果写入 WeakMap
import { createMorph } from "morphicons/dom";
const sink = { setAttribute() {} };
export function prewarm(pairs: [IconNode, IconNode][]) {
  let i = 0;
  const run = (dl: IdleDeadline) => {
    while (i < pairs.length && dl.timeRemaining() > 3) {
      const [a, b] = pairs[i++];
      for (const [x, y] of [[a, b], [b, a]]) { const m = createMorph(sink, x); m.seek(y, 0.5); m.destroy(); }
    }
    if (i < pairs.length) requestIdleCallback(run);
  };
  requestIdleCallback(run);
}
```

实测（`prewarm.mjs`，每次都是全新进程）：冷启动时首次 `morphTo` 分别为 Sun→CloudRain 27.98 ms、Eye→EyeOff 3.70 ms、PanelLeft 2.56 ms、Play→Pause 2.27 ms。预热之后是 0.46、0.05、0.03、0.03 ms。4 对预热共耗时 43.6 ms，在 idle 里分片执行，用户无感。

### 3.6 遥测驱动图标：分桶、迟滞、驻留时间（V0.2）

```ts
// 电量分档：0 为 warn（<20%），1 为 low（20–40），2 为 medium（40–70），3 为 full（≥70）
const EDGES = [20, 40, 70], HYST = 3, DWELL_MS = 1500;
function batteryLevel(pct: number, prev: number): number {
  let l = prev;
  while (l < 3 && pct >= EDGES[l] + HYST) l++;      // 升档必须越过边界 +3%
  while (l > 0 && pct < EDGES[l - 1]) l--;          // 降档一越过边界立即生效（安全优先）
  return l;
}
// 每个 drone、每个图标位维护一份 {level, since}；now - since < DWELL_MS 时不刷新图标（告警升级不受此限制）
// 输出 BatteryFull / BatteryMedium / BatteryLow / BatteryWarning，这三组相邻对在白名单里，res=0 或已目测通过
```

同样的做法适用于：链路（SignalHigh/Medium/Low/Zero，按 RSSI 或丢包率分档）；GNSS（LocateFixed 或 Satellite，由 fix type 决定：NoFix/2D/3D/RTK-float/RTK-fixed，RTK 用 shadcn `Badge` 的文字标注，不单独造图标）。**原则：图标只反映离散档位，不反映连续值**。原始值 10–50 Hz 更新，图标变化不超过 0.7 Hz，否则 spring 会被反复重启，并发预算也会被耗光。

航向和风向这类连续角度：`<Icon icon="heading" style={{ transform: \`rotate(${unwrapped}deg)\` }} />`，其中 `unwrapped = prev + ((next − prev + 540) % 360 − 180)` 用来避免 359°→1° 时绕一整圈，配 CSS `transition: transform 120ms linear`。**不使用 morph**。

### 3.7 3D 场景内的图标（V0.2 / V0.6）

**选中机状态 sprite（morph）**：沿用 `website/lib/icon-sprite.ts` 的范式。

```ts
const sprite = iconSprite({ size: 64, icon: Drone, color: "#ffffff", pad: 3,   // 形状放在 alpha 通道，颜色交给 shader
  onWrite: () => { tex.needsUpdate = true; invalidate(); } });                  // R3F 的 demand 模式
const tex = new THREE.CanvasTexture(sprite.canvas);                             // WebGPURenderer 和 WebGL2 都支持
// 状态变化时：sprite.morph.morphTo(TriangleAlert, "smooth")。morph driver 跑在自己的 rAF 上，Three 只在 dirty 时上传纹理
```

要点：**不要把 Three 的 context 交给 morph**（`canvasTarget` 的注释和 README 都强调，两个时钟会互相争抢）。每个 sprite 用独立的小 canvas，由 onWrite 通知宿主。

**多机静态图标 atlas（V0.6，同屏 100 架以上）**：

```ts
function buildIconAtlas(nodes: IconNode[], cell = 64, pad = 3, strokePx = 4) {
  const cols = Math.ceil(Math.sqrt(nodes.length)), rows = Math.ceil(nodes.length / cols);
  const cv = new OffscreenCanvas(cols * cell, rows * cell), ctx = cv.getContext("2d")!;
  const s = cell / (24 + 2 * pad), index = new Map<IconNode, number>();
  ctx.strokeStyle = "#fff"; ctx.lineCap = "round"; ctx.lineJoin = "round";
  nodes.forEach((n, i) => {
    ctx.setTransform(s, 0, 0, s, (i % cols) * cell + pad * s, Math.floor(i / cols) * cell + pad * s);
    ctx.lineWidth = strokePx / s;                    // 在 grid 单位下换算，得到恒定的 strokePx 像素
    ctx.stroke(new Path2D(canonicalD(n)));           // 和 DOM 图标同源、同几何
    index.set(n, i);
  });
  return { tex: new THREE.CanvasTexture(cv), index, cols, rows };
}
// InstancedMesh(Plane) 做 billboard，per-instance 属性为 aIcon（atlas 下标）和 aTint（来自色卡 token）
// fragment：uv' = (uv + vec2(col, row)) / vec2(cols, rows)；color = aTint；alpha = texture(atlas, uv').a
// 生成时按 DPR 放大 2 倍，开启 mipmap；只有选中的那架机切换到上面的 morph sprite
```

### 3.8 core 数学的二次利用：编队变换预览（port，V0.6）

编队从 A 队形变到 B 队形（例如一字形变 V 形，同时整体转向）时，如果对 slot 坐标直接做线性插值，会出现"弦塌缩"：队形在中途缩小、切变，这和图标旋转时的问题完全一样。morphicons 的解法可以直接移植：

```
输入：slots A[N]、B[N]（水平面 xy，高度单独处理）
1. 对应关系：用 Hungarian 求 min Σ|a_i − b_π(i)|²（N≤20 时约 1 ms）；或者按任务约束固定分配
2. Procrustes（§3.1 的闭式解）：得到 θ*、σ*、c_A、c_B、res
3. 对每个 t：P_i(t) = c(t) + σ*ᵗ·R(t·θ*)·[(1−t)·(a_i−c_A) + t·R(−θ*)(b_π(i)−c_B)/σ*]
   z_i(t) = lerp(z_a, z_b, t)；c(t) = lerp(c_A, c_B, t)
4. 输出用于 UI 预览，以及给规划器（EGO-Swarm 等）的初始参考轨迹；避碰由规划器负责
```

res≈0 时，这是纯刚体旋转加缩放，队形在整个过程中都保持完整。落在 `swarm/formation.ts`，定位为 reference 级：只做可视化和初值，不参与安全关键控制。

### 3.9 实测性能汇总

| 测试 | 结果 |
|---|---|
| plan（预热后，缓存采样） | Menu→X 0.15 ms，Play→Pause 1.2 ms，Sun→CloudRain 0.68 ms，Chevron 0.06 ms；resample 两个图标 0.15–1.1 ms |
| 单帧 core 开销（Node） | `interpPolar` 0.6–5.8 µs；**`serialize` 40–265 µs**（约每子路径 30 µs，N=64，2 位小数的字符串拼接）；67 个候选对合计为 32–382 µs/帧（`bench-pairs.out`） |
| DOM 并发（Chromium，飞行期间的帧间隔） | 1 或 10 个：16.7 ms（满帧）；50 个 Menu→X：p95 22 ms；50 个 Sun→CloudRain：均值 18.8 ms，p95 29 ms；100 个 Sun→CloudRain：均值 29 ms；200 个：55 ms |
| 静止时 | 500 ms 内 rAF 调用 0 次；settle 后 0 次，调度器已停止 |
| settle 时间 | snappy 约 450 ms（DOM 实测 451–501 ms） |
| React 19 挂载 1000 个图标 | lucide-react 61.5 ms，5051 个节点；MorphIcon 31.2 ms，2001 个节点；**core 静态 14.5 ms**，2001 个节点 |
| React 19 挂载 3000 个图标 | 155.8 / 76.4 / **54.8** ms |
| bundle（202 个图标，gzip，react 作 external） | lucide-react 19.2 KB；lucide 数据 + morphicons react+dom 18.9 KB；仅 morphicons react+dom 8.0 KB |

---

## 4. 在本项目中的落点与复用方式

| 能力 | 落点（模块 / 文件） | 版本 | 复用方式 | 说明 |
|---|---|---|---|---|
| 图标数据 | `web/src/components/icons/registry.ts` 和 `custom.ts` | V0.1 | adopt `lucide@1.48.0`（精确锁版本） | 语义 key 到 IconNode 的映射。业务代码只写语义 key，不写 lucide 名 |
| 静态图标 | `icons/icon.tsx` | V0.1 | adopt `morphicons/dom` › `canonicalD` | shadcn 组件、菜单、列表、表格等 90% 以上的场景 |
| 状态图标 | `icons/state-icon.tsx` | V0.1 | adopt `createMorph`，自研绑定，port transitions.dev 的 swap | morph/swap 白名单、并发预算、全局 motion 策略 |
| 拖拽驱动图标 | `layout/resizable-panel.tsx` | V0.2 | adopt `MorphIcon` 的 controlled 模式 | 面板拖拽宽度映射为 `progress`，`PanelLeftClose→PanelLeftOpen` 跟手变化 |
| shadcn 兼容层 | `icons/lucide-compat.ts` 和 `scripts/shadcn-icons-codemod.mjs` | V0.1 | 自研 | 导出 `XIcon`、`CheckIcon` 等 16 个名字 |
| 质量门禁 | `scripts/check-icons.mjs`（CI） | V0.1 | 自研 | 禁止 `lucide-react`、禁止 `import { icons } from "lucide"`、禁止 `\p{Extended_Pictographic}`，校验 registry 名字 |
| 预热 | `icons/prewarm.ts` | V0.1 | 公开 API 的组合用法 | 白名单对 idle 分片预热 |
| 遥测图标 | `fleet/derive-ui-state.ts` | V0.2 | 自研 | 分桶、迟滞、驻留时间 |
| 3D 选中机 sprite | `viewport/overlays/drone-badge.ts` | V0.2 | adopt `canvasTarget`，port `icon-sprite.ts` | CanvasTexture 加 invalidate |
| 3D 多机 atlas | `viewport/overlays/icon-atlas.ts` | V0.6 | 自研（Path2D + canonicalD） | InstancedMesh billboard |
| 编队变换预览 | `swarm/formation-preview.ts` | V0.6 | port core 的 Procrustes 和极坐标插值 | 仅做可视化和初值 |
| 性能联动 | `perf/quality-controller.ts` | V0.1 | 自研 | 帧时间持续超标时先把 `iconPolicy` 降到 `swap` 再到 `off`，然后才降点预算 |

### 4.1 本系统图标清单（按模块，lucide@1.48.0 已逐个校验）

说明：
- `static` 表示不切换，用 `<Icon>`。
- `morph → X` 表示登记进白名单、用 spring 形变的对，括号里的 `smooth` 表示使用 state 节奏。
- `swap → X` 表示走 transitions.dev crossfade。
- `CSS rotate(θ)` 表示连续角度。
- 括号里是 lucide 的 kebab 文件名。
- **custom:** 开头的是本项目自定义图标，定义见本节末尾。

校验结果（`verify.out`）：215 行，被引用的唯一图标 207 个（202 个 lucide、5 个 custom），另有 1 个备选自定义图标 CameraFrustum（`Cone` 的替代，未被引用），合计 208 个；48 个 morph/swap 对；**缺失 0 个，非 canonical 名 0 个**。

#### A. 外壳/导航/全局

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| nav.world | `Earth` (earth) | 世界/场景 | static |
| nav.layers | `Layers` (layers) | 图层 | static |
| nav.environment | `CloudSunRain` (cloud-sun-rain) | 环境 | static |
| nav.fleet | `Drone` (drone) | 机队 | static |
| nav.mission | `Route` (route) | 任务 | static |
| nav.agents | `Bot` (bot) | 智能体 ANet | static |
| nav.data | `Database` (database) | 数据/World Package | static |
| nav.perf | `Gauge` (gauge) | 性能面板 | static |
| nav.settings | `Settings` (settings) | 设置 | static |
| nav.menu | `Menu` (menu) | 菜单 | morph → `X` |
| panel.left | `PanelLeftClose` (panel-left-close) | 收起左栏 | morph → `PanelLeftOpen` |
| panel.right | `PanelRightClose` (panel-right-close) | 收起右栏 | morph → `PanelRightOpen` |
| panel.bottom | `PanelBottomClose` (panel-bottom-close) | 收起时间轴 | morph → `PanelBottomOpen` |
| cmd.search | `Search` (search) | 搜索/命令面板 | static |
| cmd.command | `Command` (command) | 快捷键提示 | static |
| theme.light | `Sun` (sun) | 浅色 | morph(smooth) → `Moon` |
| theme.dark | `Moon` (moon) | 深色 | morph(smooth) → `Sun` |
| theme.system | `Monitor` (monitor) | 跟随系统 | static |
| view.fullscreen | `Maximize` (maximize) | 全屏 | morph → `Minimize` |
| help | `CircleQuestionMark` (circle-question-mark) | 帮助 | static |
| shortcuts | `Keyboard` (keyboard) | 快捷键 | static |
| notify | `Bell` (bell) | 通知 | swap → `BellOff` |
| notify.ring | `BellRing` (bell-ring) | 新告警 | static |
| user | `CircleUser` (circle-user) | 用户 | static |
| logout | `LogOut` (log-out) | 退出 | static |
| conn.online | `Wifi` (wifi) | 后端连接 | morph → `WifiOff` |

#### B. 图层与点云工具

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| layer.pointcloud | **custom:PointCloud** | 点云 | static |
| layer.terrain | `Mountain` (mountain) | 地形 | static |
| layer.building | `BuildingComplex` (building-complex) | 建筑 | static |
| layer.semantic | `Tags` (tags) | 语义 | static |
| layer.lidar | `Radar` (radar) | LiDAR 实时扫描 | static |
| layer.mesh | `Box` (box) | Mesh | static |
| layer.splat | `Sparkles` (sparkles) | 3DGS/Visual World | static |
| layer.trajectory | `Spline` (spline) | 轨迹 | static |
| layer.occupancy | `Grid3x3` (grid-3x3) | 占据栅格/体素 | static |
| layer.bbox | `SquareDashed` (square-dashed) | 包围盒/Octree 节点框 | static |
| layer.lod | **custom:OctreeLod** | LOD/点预算 | static |
| layer.visible | `Eye` (eye) | 显示 | swap → `EyeOff` |
| layer.lock | `Lock` (lock) | 锁定 | morph → `LockOpen` |
| layer.opacity | `Blend` (blend) | 透明度 | static |
| layer.pointsize | `CircleDot` (circle-dot) | 点大小 | static |
| layer.colormode | `Palette` (palette) | 着色模式(RGB/高程/强度/分类) | static |
| layer.loading | `LoaderCircle` (loader-circle) | 渐进加载中 | morph → `CircleCheck` |
| layer.stream | `CloudDownload` (cloud-download) | 流式下载 | static |
| tool.measure | `Ruler` (ruler) | 测量 | static |
| tool.clip | `Scissors` (scissors) | 裁剪/剖切 | static |
| tool.slice | `Slice` (slice) | 切片 | static |
| tool.axis | `Axis3d` (axis-3d) | 坐标轴 | static |

#### C. 环境 Environment

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| env.clear | `Sun` (sun) | 晴 | morph(smooth) → `CloudSun` |
| env.partly | `CloudSun` (cloud-sun) | 少云 | morph(smooth) → `Cloud` |
| env.cloud | `Cloud` (cloud) | 云 | morph(smooth) → `CloudRain` |
| env.overcast | `Cloudy` (cloudy) | 阴 | static |
| env.rain | `CloudRain` (cloud-rain) | 雨 | morph(smooth) → `CloudFog` |
| env.storm | `CloudRainWind` (cloud-rain-wind) | 风雨 | static |
| env.drizzle | `CloudDrizzle` (cloud-drizzle) | 毛毛雨 | static |
| env.thunder | `CloudLightning` (cloud-lightning) | 雷暴 | static |
| env.snow | `CloudSnow` (cloud-snow) | 雪 | static |
| env.fog | `CloudFog` (cloud-fog) | 雾 | morph(smooth) → `Haze` |
| env.haze | `Haze` (haze) | 霾/能见度 | static |
| env.sand | **custom:SandDust** | 沙尘 | static |
| env.wind | `Wind` (wind) | 风 | static |
| env.wind.dir | `Navigation2` (navigation-2) | 风向(CSS rotate，不 morph) | CSS rotate(θ) |
| env.gust | `WindArrowDown` (wind-arrow-down) | 阵风/下沉气流 | static |
| env.turbulence | `Tornado` (tornado) | 湍流 | static |
| env.temperature | `Thermometer` (thermometer) | 温度 | static |
| env.humidity | `Droplets` (droplets) | 湿度/降水量 | static |
| env.visibility | `Binoculars` (binoculars) | 能见度 | static |
| env.sunrise | `Sunrise` (sunrise) | 日出 | morph(smooth) → `Sunset` |
| env.time | `SunMoon` (sun-moon) | 昼夜 | static |
| env.field | `WavesHorizontal` (waves-horizontal) | 环境场 E(x,y,z,t) | static |

#### D. 无人机状态

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| drone.quad | `Drone` (drone) | P600/四旋翼 | static |
| drone.hexa | **custom:DroneHexa** | 六旋翼(异构) | static |
| drone.takeoff | `PlaneTakeoff` (plane-takeoff) | 起飞 | morph → `PlaneLanding` |
| drone.land | `PlaneLanding` (plane-landing) | 降落 | static |
| drone.armed | `ShieldCheck` (shield-check) | 已解锁/健康 | morph → `ShieldAlert` |
| drone.power | `Power` (power) | 上电 | morph → `PowerOff` |
| bat.full | `BatteryFull` (battery-full) | 电量高 | morph → `BatteryMedium` |
| bat.medium | `BatteryMedium` (battery-medium) | 电量中 | morph → `BatteryLow` |
| bat.low | `BatteryLow` (battery-low) | 电量低 | morph → `BatteryWarning` |
| bat.warn | `BatteryWarning` (battery-warning) | 电量告警 | static |
| bat.charging | `BatteryCharging` (battery-charging) | 充电 | static |
| gnss.rtk | `Satellite` (satellite) | RTK/GNSS | static |
| gnss.fix | `LocateFixed` (locate-fixed) | 定位锁定 | swap → `LocateOff` |
| gnss.nofix | `LocateOff` (locate-off) | 无定位 | static |
| link.high | `SignalHigh` (signal-high) | 链路强 | morph → `SignalMedium` |
| link.medium | `SignalMedium` (signal-medium) | 链路中 | morph → `SignalLow` |
| link.low | `SignalLow` (signal-low) | 链路弱 | morph → `SignalZero` |
| link.lost | `SignalZero` (signal-zero) | 链路丢失 | static |
| link.radio | `RadioTower` (radio-tower) | 数传/地面站 | static |
| alt | `MoveVertical` (move-vertical) | 高度 | static |
| speed | `Gauge` (gauge) | 速度 | static |
| heading | `Compass` (compass) | 航向(CSS rotate) | CSS rotate(θ) |
| health | `HeartPulse` (heart-pulse) | 健康 | static |
| sensor.camera | `Camera` (camera) | 相机/吊舱 | static |
| sensor.video | `Video` (video) | 视频流 | swap → `VideoOff` |
| sensor.thermal | `ThermometerSun` (thermometer-sun) | 热成像 | static |
| sensor.lidar | `Radar` (radar) | MID-360 | static |
| sensor.compute | `Cpu` (cpu) | Jetson Orin NX | static |
| mode.manual | `Hand` (hand) | 手动 | static |
| mode.offboard | `Bot` (bot) | Offboard/自主 | static |
| mode.hold | `Anchor` (anchor) | 悬停保持 | static |
| mode.rtl | `House` (house) | 返航 | static |

#### E. 任务/控制模式

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| cmd.takeoff | `PlaneTakeoff` (plane-takeoff) | Takeoff | static |
| cmd.land | `PlaneLanding` (plane-landing) | Land | static |
| cmd.goto | `MapPin` (map-pin) | GoTo | static |
| cmd.followpath | `Waypoints` (waypoints) | FollowPath | static |
| cmd.orbit | `Orbit` (orbit) | Orbit | static |
| cmd.hover | `Crosshair` (crosshair) | Hover | static |
| cmd.rth | `House` (house) | ReturnHome | static |
| cmd.coverage | `Scan` (scan) | Area Coverage | static |
| cmd.search | `ScanSearch` (scan-search) | Search | static |
| cmd.track | `Focus` (focus) | Tracking | static |
| cmd.formation | **custom:Formation** | Formation/Swarm | static |
| cmd.avoid | `ShieldHalf` (shield-half) | Collision Avoidance | static |
| wp.add | `MapPinPlus` (map-pin-plus) | 添加航点 | static |
| wp.remove | `MapPinX` (map-pin-x) | 删除航点 | static |
| mission.list | `ListChecks` (list-checks) | 任务列表 | static |
| mission.pending | `CircleDashed` (circle-dashed) | 待执行 | swap → `LoaderCircle` |
| mission.running | `LoaderCircle` (loader-circle) | 执行中 | morph → `CircleCheck` |
| mission.done | `CircleCheck` (circle-check) | 完成 | static |
| mission.failed | `CircleX` (circle-x) | 失败 | static |
| mission.paused | `CirclePause` (circle-pause) | 暂停 | morph → `CirclePlay` |
| mission.start | `Play` (play) | 开始 | morph → `Square` |
| mission.abort | `OctagonX` (octagon-x) | 中止/急停 | static |
| mission.delete | `Trash` (trash) | 删除 | static |
| mission.edit | `Pencil` (pencil) | 编辑 | static |

#### F. 时间轴

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| tl.play | `Play` (play) | 播放 | morph → `Pause` |
| tl.pause | `Pause` (pause) | 暂停 | morph → `Play` |
| tl.stop | `Square` (square) | 停止 | static |
| tl.skipback | `SkipBack` (skip-back) | 跳到开头 | static |
| tl.skipfwd | `SkipForward` (skip-forward) | 跳到结尾 | static |
| tl.rewind | `Rewind` (rewind) | 快退 | morph → `FastForward` |
| tl.ff | `FastForward` (fast-forward) | 快进 | static |
| tl.stepback | `StepBack` (step-back) | 单步后退 | static |
| tl.stepfwd | `StepForward` (step-forward) | 单步前进 | static |
| tl.loop | `Repeat` (repeat) | 循环 | morph → `Repeat1` |
| tl.replay | `RotateCcw` (rotate-ccw) | 回放 | static |
| tl.history | `RotateCcwClock` (rotate-ccw-clock) | 历史记录 | static |
| tl.clock | `Clock` (clock) | 仿真时间 | static |
| tl.speed | `Gauge` (gauge) | 倍速 x1/x2/x5/x10 | static |
| tl.marker | `Flag` (flag) | 事件标记 | static |
| tl.bookmark | `Bookmark` (bookmark) | 书签 | static |
| tl.live | `Radio` (radio) | 实时(Live) | static |
| tl.record | `CircleDot` (circle-dot) | 录制 | static |

#### G. 相机模式

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| cam.free | `Move3d` (move-3d) | Free Camera | static |
| cam.orbit | `Orbit` (orbit) | 环绕 | static |
| cam.third | `Video` (video) | Third Person | static |
| cam.fpv | `ScanEye` (scan-eye) | FPV | static |
| cam.bird | `Map` (map) | Bird Eye/俯视 | static |
| cam.follow | `Locate` (locate) | 跟随 | morph → `LocateFixed` |
| cam.fov | `Cone` (cone) | 相机视锥 FOV | static |
| cam.reset | `Fullscreen` (fullscreen) | 重置视角 | static |
| cam.zoomin | `ZoomIn` (zoom-in) | 放大 | static |
| cam.zoomout | `ZoomOut` (zoom-out) | 缩小 | static |
| cam.screenshot | `ImageDown` (image-down) | 截图 | static |
| cam.switch | `SwitchCamera` (switch-camera) | 切换相机 | static |

#### H. 告警

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| alert.info | `Info` (info) | 信息 | static |
| alert.success | `CircleCheck` (circle-check) | 成功 | static |
| alert.warning | `TriangleAlert` (triangle-alert) | 警告 | morph → `OctagonAlert` |
| alert.error | `CircleAlert` (circle-alert) | 错误 | static |
| alert.critical | `OctagonAlert` (octagon-alert) | 严重 | static |
| alert.emergency | `Siren` (siren) | 紧急 | static |
| alert.geofence | `ShieldAlert` (shield-alert) | 地理围栏 | static |
| alert.linklost | `WifiOff` (wifi-off) | 链路中断 | static |
| alert.battery | `BatteryWarning` (battery-warning) | 低电量 | static |
| alert.wind | `Wind` (wind) | 大风 | static |
| alert.weather | `CloudLightning` (cloud-lightning) | 恶劣天气 | static |
| alert.collision | `ShieldHalf` (shield-half) | 碰撞风险 | static |

#### I. Agent/ANet (V1.0)

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| agent | `Bot` (bot) | Physical Agent | morph → `BotOff` |
| agent.offline | `BotOff` (bot-off) | 离线 | static |
| agent.brain | `BrainCircuit` (brain-circuit) | 规划/推理 | static |
| agent.network | `Network` (network) | ANet | static |
| agent.capability | `ScanSearch` (scan-search) | Capability Discovery | static |
| agent.task | `Workflow` (workflow) | Task Assignment | static |
| agent.collab | `Handshake` (handshake) | 协作 | static |
| agent.msg | `MessagesSquare` (messages-square) | 消息 | static |
| agent.share | `Share2` (share-2) | 共享/广播 | static |

#### J. 性能/调试

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| perf.fps | `Activity` (activity) | FPS | static |
| perf.cpu | `Cpu` (cpu) | CPU | static |
| perf.mem | `MemoryStick` (memory-stick) | 内存/显存 | static |
| perf.gpu | `Zap` (zap) | GPU/WebGPU | static |
| perf.budget | **custom:OctreeLod** | 点预算 | static |
| perf.chart | `ChartLine` (chart-line) | 曲线 | static |
| perf.bench | `FlaskConical` (flask-conical) | 基准测试 | static |
| perf.debug | `Bug` (bug) | 调试 | static |
| perf.timer | `Timer` (timer) | 帧时间 | static |

#### K. 数据/World Package

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| data.import | `Upload` (upload) | 导入 | static |
| data.export | `Download` (download) | 导出 | static |
| data.folder | `FolderOpen` (folder-open) | 打开 World | static |
| data.package | `Package` (package) | World Package | static |
| data.file | `FileBox` (file-box) | 点云文件 | static |
| data.json | `FileBraces` (file-braces) | 配置 JSON | static |
| data.server | `Server` (server) | 后端服务 | static |
| data.drive | `HardDrive` (hard-drive) | 本地缓存 | static |

#### L. 通用控件

| key | 图标 (lucide@1.48 / custom) | 含义 | 切换方式 |
|---|---|---|---|
| chev.down | `ChevronDown` (chevron-down) | - | morph → `ChevronUp` |
| chev.up | `ChevronUp` (chevron-up) | - | static |
| chev.left | `ChevronLeft` (chevron-left) | - | morph → `ChevronRight` |
| chev.right | `ChevronRight` (chevron-right) | - | morph → `ChevronDown` |
| chev.updown | `ChevronsUpDown` (chevrons-up-down) | - | morph → `ChevronsDownUp` |
| check | `Check` (check) | - | morph → `X` |
| close | `X` (x) | - | static |
| plus | `Plus` (plus) | - | morph → `X` |
| minus | `Minus` (minus) | - | static |
| more | `Ellipsis` (ellipsis) | - | static |
| more.v | `EllipsisVertical` (ellipsis-vertical) | - | static |
| grip | `GripVertical` (grip-vertical) | - | static |
| copy | `Copy` (copy) | - | morph → `Check` |
| refresh | `RefreshCw` (refresh-cw) | - | static |
| external | `ExternalLink` (external-link) | - | static |
| filter | `Funnel` (funnel) | - | static |
| sort | `ArrowDownUp` (arrow-down-up) | - | static |
| sliders | `SlidersHorizontal` (sliders-horizontal) | - | static |
| pin | `Pin` (pin) | - | swap → `PinOff` |
| star | `Star` (star) | - | static |
| link | `Link` (link) | - | swap → `Link2Off` |

**lucide 已有 `drone`**：9 个子路径，四旋翼 X 构型俯视，旋翼用弧线表示，1.48 版可用。P600 属于四旋翼，直接使用这个图标。下面 6 个自定义图标都满足 morphicons 和 lucide 的约束：24 网格、只用 stroke、不带 transform/`<g>`、只使用 path/line/circle/rect、描边 2 时留出至少 1 grid 的边距。已在 16/20/24/48px 下渲染，见 `sheet.png`。

```ts
// web/src/components/icons/custom.ts
export const PointCloud: IconNode = [            // 点云：四角扫描框加 5 个采样点
  ["path", { d: "M3 7V5a2 2 0 0 1 2-2h2" }], ["path", { d: "M17 3h2a2 2 0 0 1 2 2v2" }],
  ["path", { d: "M21 17v2a2 2 0 0 1-2 2h-2" }], ["path", { d: "M7 21H5a2 2 0 0 1-2-2v-2" }],
  ["circle", { cx: "8", cy: "10", r: "1" }], ["circle", { cx: "12", cy: "8", r: "1" }],
  ["circle", { cx: "16", cy: "11", r: "1" }], ["circle", { cx: "10", cy: "15", r: "1" }],
  ["circle", { cx: "15", cy: "16", r: "1" }],
];
export const SandDust: IconNode = [              // 沙尘：三条流线加 5 个悬浮颗粒
  ["path", { d: "M2 8h9" }], ["path", { d: "M2 12h13" }], ["path", { d: "M2 16h8" }],
  ["circle", { cx: "15", cy: "7", r: "1" }], ["circle", { cx: "20", cy: "9", r: "1" }],
  ["circle", { cx: "19", cy: "14", r: "1" }], ["circle", { cx: "13", cy: "17", r: "1" }],
  ["circle", { cx: "20", cy: "19", r: "1" }],
];
export const OctreeLod: IconNode = [             // LOD / 点预算：四叉划分，其中一格再细分
  ["rect", { x: "3", y: "3", width: "18", height: "18", rx: "2" }],
  ["path", { d: "M12 3v18" }], ["path", { d: "M3 12h18" }], ["path", { d: "M16.5 3v9" }], ["path", { d: "M12 7.5h9" }],
];
export const DroneHexa: IconNode = [             // 六旋翼（V1.0 异构机型）；16px 下偏密，建议 ≥20px 使用
  ["circle", { cx: "12", cy: "12", r: "2" }],
  ["path", { d: "M12 10V7" }], ["path", { d: "M13.73 11l2.6-1.5" }], ["path", { d: "M13.73 13l2.6 1.5" }],
  ["path", { d: "M12 14v3" }], ["path", { d: "M10.27 13l-2.6 1.5" }], ["path", { d: "M10.27 11l-2.6-1.5" }],
  ["circle", { cx: "12", cy: "4.5", r: "2.5" }], ["circle", { cx: "18.5", cy: "8.25", r: "2.5" }],
  ["circle", { cx: "18.5", cy: "15.75", r: "2.5" }], ["circle", { cx: "12", cy: "19.5", r: "2.5" }],
  ["circle", { cx: "5.5", cy: "15.75", r: "2.5" }], ["circle", { cx: "5.5", cy: "8.25", r: "2.5" }],
];
export const Formation: IconNode = [             // 编队 / Swarm：三个节点加三条连边
  ["circle", { cx: "12", cy: "5", r: "2.5" }], ["circle", { cx: "5", cy: "18", r: "2.5" }],
  ["circle", { cx: "19", cy: "18", r: "2.5" }],
  ["path", { d: "M10.8 7.2 6.2 15.8" }], ["path", { d: "M13.2 7.2l4.6 8.6" }], ["path", { d: "M7.5 18h9" }],
];
export const CameraFrustum: IconNode = [         // 相机视锥（lucide `Cone` 的备选）
  ["circle", { cx: "5", cy: "12", r: "2" }], ["path", { d: "M7 11 19 5" }], ["path", { d: "M7 13l12 6" }], ["path", { d: "M19 5v14" }],
];
```

新增自定义图标的流程：画在 24 网格上，用 stroke 2 和 round cap/join，**不用 fill 和 transform**。然后运行 `node verify.mjs` 检查网格边界和解析，运行 `sheet.cjs` 在 16/24px 下目测。如果要参与 morph，还要检查 t=.25/.5/.75 的中间帧。

### 4.2 morph / swap 切换对（指标来自 `verify.out` 和 `bench-pairs.out`，目测来自 `sheet*.png`）

| 切换对 | 子路径 | max θ | max res | 目测 | 机制 / spring |
|---|---|---|---|---|---|
| Menu↔X | 3→2 | 45° | 0.000 | 折叠干净 | morph / snappy |
| Plus↔X、Plus↔Minus、Check↔X | 2 | 45°/90°/73° | 0–0.34 | 好 | morph / snappy |
| Play↔Pause | 1→2 | 2° | 0.47 | 好（三角分裂成双竖条） | morph / snappy |
| CirclePlay↔CirclePause、Play→Square | 2–3 | 1–2° | 0.22–0.81 | 好 | morph / snappy |
| Rewind↔FastForward、Repeat↔Repeat1 | 2/5 | 178°/92° | 0.03/0.07 | 好 | morph / snappy |
| ChevronDown↔Up、ChevronRight↔Down、ChevronsUpDown↔DownUp | 1–2 | 90–180° | ≈0 | 纯旋转 | morph；列表或手风琴内保留 shadcn 自带的 CSS rotate，开销更低 |
| PanelLeft/Right/Bottom Close↔Open、Maximize↔Minimize | 3–4 | 180° | 0.000 | 好 | morph / snappy |
| Lock↔LockOpen | 2 | 27° | 0.17 | 好 | morph / snappy |
| Locate↔LocateFixed（跟随开关） | 5→6 | 0° | 0.000 | 好 | morph / snappy |
| BatteryFull→Medium→Low | 5→4→3 | 0° | 0.000 | 好（电量格收缩） | morph / hud |
| BatteryLow→BatteryWarning | 3→5 | 90° | 0.62 | 可接受 | morph / hud |
| SignalHigh→Medium→Low→Zero | 4→…→1 | 0–90° | 0.000 | 好 | morph / hud |
| ShieldCheck↔ShieldAlert、TriangleAlert↔OctagonAlert、CircleCheck↔CircleAlert | 2–3 | 2–65° | 0.20–0.36 | 好（告警升级） | morph / smooth |
| LoaderCircle→CircleCheck / CircleX | 1→2/3 | 78–81° | 0.49–0.68 | 好 | morph / smooth（旋转交接见 §6） |
| PlaneTakeoff↔PlaneLanding | 2 | 49° | 0.06 | 好 | morph / snappy |
| Wifi↔WifiOff、Power↔PowerOff、Bot↔BotOff | 4–7 | 45° | 0.20–0.79 | 可接受 | morph / snappy |
| Sun↔CloudSun↔Cloud↔CloudRain↔CloudFog↔Haze（天气预设） | 1–9 | 75–90° | 0–0.78 | 中间帧较乱，但能读出"天气在转变" | morph / **smooth**（用户明确要求 sun↔cloud-rain） |
| Sun↔Moon（主题）、Sunrise↔Sunset | 9→1 / 8 | 83° / 180° | 0.90 / 0 | 可接受，Sun↔Moon 也是库作者的验收对之一 | morph / smooth |
| **Eye↔EyeOff** | 2→4 | 42° | 0.89 | **不合格**：t≈0.2–0.6 外轮廓卷成漩涡 | **swap** |
| **Bell↔BellOff、Video↔VideoOff、Pin↔PinOff、Link↔Link2Off** | 2→3/4 | 44–82° | 0.66–0.88 | **不合格**：切口类变体发生扭曲 | **swap** |
| **CircleDashed→LoaderCircle** | 8→1 | 171° | 0.65 | **不合格**：8 段虚线挤成一团 | **swap** |
| **LocateFixed→LocateOff** | 6→7 | 3° | 0.83 | 一般 | **swap** |
| **相机模式循环**（Move3d/Video/ScanEye/Map） | 3–6 | 45–136° | 0.54–0.83 | **不合格**：形状无关，变成团块 | 用 ToggleGroup 并列展示 4 个静态图标，滑动指示器负责动效；单按钮循环时用 swap |

白名单准入规则（新增切换对时逐条检查）：
1. 必须是同一对象的状态变化；
2. 子路径数量差 ≤2，或者属于同族分档；
3. max res ≤0.5，或者是纯旋转（res≈0）；
4. 用 contact-sheet 脚本在 t=.25/.5/.75 目测通过。

四条都满足才能进白名单；不满足的**默认走 swap**。

### 4.3 在 shadcn 组件中替换 lucide-react（推荐方案）

**推荐：全站统一由 morphicons 负责渲染。静态图标用 `<Icon>`（纯 core 的 canonical d），状态图标用 `<StateIcon>`。不安装 `lucide-react`。** 理由：

1. **单一数据源、单一几何管线**：同一个 IconNode 引用同时服务静态渲染、morph、3D atlas，静态和动态像素一致。README 推荐的另一条路是 lucide-react 与 lucide 共存，那样要锁两个包的版本；morph 起点是 lucide-react 渲染出的多元素 SVG，终点是 morphicons 的单 path，settle 后 DOM 结构也不一样。
2. **更快**：1000 个图标挂载 14.5 ms 对 61.5 ms；DOM 节点少 60%。
3. **更小**：数据加引擎是 18.9 KB，lucide-react 单独就要 19.2 KB，两者共存估计约 30 KB。
4. **自定义图标零成本接入**：不需要 `createLucideIcon`。
5. **规范容易强制**：统一的 stroke、尺寸、色彩 token，禁止 emoji，都在一个组件里完成。

**shadcn 选择器约束**（shadcn/ui 仓库 `apps/v4/registry/new-york-v4/ui` 的统计）：`[&_svg]` 45 处，`[&_svg:not([class*='size-'])]` 33 处，`[&>svg]` 32 处，`has-[>svg]` 8 处，`*:[svg]` 3 处。因此：
- 根节点必须是 `<svg>`，不能是包裹 svg 的 `<span>`（transitions.dev 原版 icon-swap 就是 span 包裹，所以要改写）；
- 尺寸交给 CSS 决定（`:where` 的默认值加 shadcn 的 `size-*`）；
- 颜色用 `currentColor`，才能配合 `text-muted-foreground`。

**兼容层**：shadcn v4 的 `ui/*.tsx` 实际 import 的 lucide-react 名字只有 16 个：`ArrowDownIcon ArrowLeft ArrowRight CheckIcon ChevronDownIcon ChevronRight ChevronRightIcon ChevronUpIcon CircleIcon GripVerticalIcon Loader2Icon MinusIcon MoreHorizontal PanelLeftIcon SearchIcon XIcon`。新的 `bases/*` 用 `IconPlaceholder`，在 `shadcn add` 时按 `components.json` 的 `iconLibrary` 转换（`packages/shadcn/src/utils/transformers/transform-icons.ts`）。iconLibrary 只支持 lucide、tabler、hugeicons、phosphor、remixicon，**不支持自定义库**，所以保持 `iconLibrary:"lucide"`，再对产出做 import 改写。

```ts
// web/src/components/icons/lucide-compat.ts —— 与 lucide-react 同名同 props（size/color/strokeWidth/className）
import { X, Check, ChevronDown, ChevronRight, ChevronUp, Circle, GripVertical, LoaderCircle, Minus,
         Ellipsis, PanelLeft, Search, ArrowDown, ArrowLeft, ArrowRight } from "lucide";
const make = (node: IconNode, name: string) => { const C = forwardRef<SVGSVGElement, CompatProps>(
  ({ size, color, strokeWidth, style, ...p }, ref) => <Icon ref={ref} icon={node} size={size}
    style={{ color, ...(strokeWidth ? { "--icon-stroke": `${Number(strokeWidth) * 0.75}px` } : null), ...style }} {...p} />);
  C.displayName = name; return C; };
export const XIcon = make(X, "XIcon"), CheckIcon = make(Check, "CheckIcon"), /* … */
             Loader2Icon = make(LoaderCircle, "Loader2Icon"), MoreHorizontal = make(Ellipsis, "MoreHorizontal") /* … */;
```

```js
// scripts/shadcn-icons-codemod.mjs —— 在 package.json 的 "ui:add": "shadcn add $@ && node scripts/shadcn-icons-codemod.mjs" 中调用
for (const f of glob("src/components/ui/**/*.tsx")) {
  const s = read(f).replace(/from ["']lucide-react["']/g, 'from "@/components/icons/lucide-compat"');
  write(f, s);
}
// 如果兼容层缺少某个名字，tsc 会直接报错（fail loud），这时去 lucide-compat.ts 里补一行
```

```js
// scripts/check-icons.mjs（CI）
// 1) grep 'from "lucide-react"' → 直接失败
// 2) grep 'import { icons }' from "lucide" → 失败（它会引入全部 1854 个图标，破坏 tree-shaking）
// 3) 扫描 src/**/*.{ts,tsx,css,json,mdx} 中的 /\p{Extended_Pictographic}/u → 失败（执行"严禁 emoji"；这个范围比 \p{Emoji} 更严，★ 这类文本符号也会被拦下，UI 里一律改用图标）
// 4) 对 registry 做 verify.mjs 同款校验：名字存在、是 canonical 名、custom 不越界
```

**如何使用**：
- 菜单、按钮、表格、列表这类一般场景，用 `<Icon icon="layer.lidar" />`。
- 状态会变化的位置，用 `<StateIcon icon={playing ? "tl.pause" : "tl.play"} spring="snappy" />`。
- shadcn 组件内部通过兼容层自动走 `<Icon>`。
- 只有 `Accordion`、`Collapsible`、`Select` 的 chevron 保留 shadcn 自带的 `transition-transform rotate-180`：它在视觉上与纯旋转 morph 相同，但走 compositor，更省。

### 4.4 图标视觉 token（与色卡单元对齐）

| token | 暗色主题 | 亮色主题 | 用途 |
|---|---|---|---|
| `--icon-fg` | `#E6E7EA`（白） | `#0B0C0E`（黑） | 默认 |
| `--icon-muted` | `#8A8F98`（科技灰） | `#6B7079` | 次要、禁用、静态标签 |
| `--icon-active` | `#E93024`（ANet 红） | `#E93024` | 选中、开启、正在跟随 |
| `--icon-danger` | `#E93024`，配合形状（TriangleAlert/OctagonAlert）和 1.2 s 呼吸动画 | 同左 | 告警（仅红色一种） |
| `--icon-stroke` | 1.5px（≥48px 时为 2px） | 同左 | 配合 `vector-effect: non-scaling-stroke` |
| 尺寸档 | 12（Badge 内）、14（表格）、16（默认，`size-4`）、20（工具栏）、24（HUD 主按钮）、48（空状态） | — | — |

"状态由形状表达"的对照：
- 正常：CircleCheck，白色。
- 注意：TriangleAlert，白色，外加红点 Badge。
- 严重：OctagonAlert，红色。
- 失联：WifiOff 或 SignalZero，灰色。

这样即使只有灰、黑、白、红四种颜色，状态也可以区分，并且对色弱用户友好。

---

## 5. 对比与推荐

本单元只有一个仓库，下面比较的是**集成策略**，以及未 clone 的同类思路（只做定性参考）。

| 方案 | 静态渲染 | 动态切换 | 1000 图标挂载 | DOM 节点 | bundle（202 图标，gz） | 版本对齐 | 结论 |
|---|---|---|---|---|---|---|---|
| A：lucide-react 加 morphicons（README 推荐的共存） | lucide-react | MorphIcon | 61.5 ms | 5051 | 约 30 KB（19.2 + 8.0 + morph 对的数据，估算） | 需同时锁两个包 | 可行但冗余，settle 前后 DOM 结构不同 |
| B：全部用 MorphIcon | MorphIcon | MorphIcon | 31.2 ms | 2001 | 18.9 KB | 单包 | 每个实例有 5 个以上 hook 加一个 driver，静态场景是浪费；也无法做 swap 回退 |
| **C（推荐）：core 静态 `<Icon>` 加自研 `<StateIcon>`（createMorph + WAAPI swap）** | canonicalD | morph 或 swap，带预算和策略 | **14.5 ms** | 2001 | 约 19 KB | 单包 | **最优** |

同类思路的定性对照（未 clone，不作为选型候选）：
- **通用 path 插值库**（例如 flubber 一类）：直接插值坐标，不做相似变换分解，旋转时会塌缩和切变；也没有角点锚定和 spring。morphicons 在这两点上都更好。
- **逐图标手工编排的动画图标集**（基于 Motion，给每个图标单独写动画）：动画质量高，但只能在预先编排的对之间切换，覆盖面小，每个图标都要单独维护。不满足 "any→any" 和自定义图标的需求。
- **Lottie 或设计师产出的动画**：运行时重，颜色和描边不跟随 token，与 shadcn 体系割裂。
- **纯 CSS crossfade**（transitions.dev icon-swap）：零数学、compositor 友好，但表达不了"同一对象在变形"。在本方案里作为 morph 的回退。

推荐排序：morphicons（adopt，★★★★★）> transitions.dev icon-swap（作为回退 port）> 其他（skip）。

---

## 6. 风险与注意事项

1. **主线程开销**：飞行中每帧都要拼 `d` 字符串（N=64 时每个子路径约 30 µs），然后 `setAttribute`，浏览器重新解析 path 并重绘。这些都在主线程，和 Three.js、遥测解析竞争。缓解手段：并发预算 K=8、遥测迟滞和驻留、hud spring（settle 350 ms）、`iconPolicy` 纳入性能降级链路。**不要**给表格中上百行的状态列都开 morph；多机列表只对"可见行"或"选中行"开 morph，其余行用 set。
2. **冷启动卡顿**：首次 plan 加 JIT 最多约 28 ms。必须做 §3.5 的预热。之后被打断时从中间形状重新 plan，这类 plan 不缓存，但 N=64 时约 0.2–1 ms，可以接受。
3. **引用稳定性**：WeakMap 缓存和"同目标 no-op"都依赖 IconNode 引用。**禁止在 JSX 里内联数组字面量当图标**，否则每次渲染都是新引用，会触发多余的 morph 并让缓存失效。自定义图标一律放模块级常量；registry 返回同一个引用。
4. ***-Off 类和异形对 morph 质量差**：已用白名单机制兜底。**lucide 升级后必须重跑 `verify.mjs` 和 contact sheet**，因为 lucide 1.x 仍在改名（旧名降级为别名），几何也可能调整。
5. **描边尺寸陷阱**：`absoluteStrokeWidth` 按 size prop 计算，与 CSS 控制的尺寸不一致。用 `vector-effect: non-scaling-stroke` 统一解决（§3.3）。副作用是 swap 的 scale 动画过程中描边不缩放，小尺寸时显得略粗，可以接受。
6. **单 path 的限制**：MorphIcon 和 `<Icon>` 都把所有子路径合并成一条 path，因此不支持 duotone 或局部着色，也不支持 fill 图标。需要局部红色强调的，用叠加 Badge 或红点实现，不要拆 path。
7. **Loader 旋转与 morph 的交接**：Spinner 是整个 svg 做 CSS `animate-spin`。结束时要切换到 CircleCheck，如果直接去掉 spin，图标会从当前角度跳回 0°。做法：读取当前 `getComputedStyle(svg).transform` 得到角度 φ，停止动画，把 transform 固定为 `rotate(φ)`；然后用 `transition: transform 450ms` 转到 `ceil(φ/360)·360°`，同时发起 `morphTo(CircleCheck, "smooth")`。点云"渐进加载中→完成"的图层状态正好用到这个交接。
8. **3D 场景的两个时钟**：morph driver 有自己的 rAF，Three 或 R3F 也有自己的循环。**不要把宿主的 context 交给 `canvasTarget`**；用独立小 canvas，靠 `onWrite` 标记 dirty 并调用 `invalidate()`。多机场景用 atlas，不要为每架机维护一个 morph sprite。
9. **项目成熟度**：仓库才两个月，只有一位维护者；但零依赖、MIT、源码约 4k 行，测试齐全（约 256 个）。对策：精确锁版本 `morphicons@1.7.1`；如果上游停更，可以把 `src/core` 和 `src/dom`（约 2k 行）vendor 进来自行维护。
10. **默认 `reducedMotion="never"`**：库的默认值会忽略系统"减少动态"设置。本项目统一传 `"user"`。E2E 和流畅性测试截图时全局设为 `off`，保证确定性。
11. **canonical d 并非原始 SVG**：arc 和 circle 被转换成 cubic，保留 4 位小数。几何误差小于 0.02 px，视觉上等价，但不能按字节比较 lucide 的原始 SVG。快照测试以 `canonicalD` 为准。
12. **ANet logo 不进图标系统**：`anet-logo.svg` 是 2846×493 的横版 wordmark，用黑底 fill、白字、红色描边，并带 `<mask>`，无法 morph，也不符合 24 网格。应当做成独立的 `<BrandLogo>`：内联 SVG，品牌红统一为 `#E93024`（原文件混用了 `#E93024`、`#ED2D28`、`#EE342E`）。在暗色主题下，黑色底牌要放在科技灰 surface 上，否则会融进背景。GitHub avatar（96px PNG）只用作 favicon 或 app icon。

---

## 7. 对设计文档 `01-design.md` 的优化建议

1. **§34 Frontend 技术栈表补三行**：
   - `Icons | lucide@1.48（数据包）+ morphicons@1.7（morph 与静态渲染），不引入 lucide-react`
   - `Motion | transitions.dev tokens（250ms/500ms，cubic-bezier(0.22,1,0.36,1)）+ morphicons spring（snappy/smooth/hud）`
   - `Charts | lieflat-charts`

   同时声明 "UI 全量 shadcn；图标统一经 `@/components/icons`"。
2. **§38 和 §39 的示意字符违反"严禁 emoji"**：图层列表里的 U+2611（BALLOT BOX WITH CHECK）和 Timeline 里的 U+25B6（BLACK RIGHT-POINTING TRIANGLE）都属于 `\p{Extended_Pictographic}`，同时也是 `\p{Emoji}`，在部分平台上会渲染成彩色 emoji。PRD 和 UI 中应当换成 shadcn `Checkbox`/`Switch` 加 `Eye` 图标，以及 `Play`/`Pause` 的 `StateIcon`。建议把 CI 的 emoji 检测写进 §42 仓库规范。
3. **新增 "Iconography / 图标规范" 小节**（放进 UI 交互 PRD）：语义 key 注册表（§4.1）；三类切换机制（morph/swap/rotate）和白名单准入规则（§4.2）；尺寸档与描边；"形状表达状态、红色只表示 active 和告警"的原则；并发预算和 motion 策略。
4. **§28 DroneState 增加派生 UI 状态**：`battery_level`、`link_level`、`gnss_fix`（NoFix/2D/3D/RTK_FLOAT/RTK_FIXED）、`alert_level`（OK/NOTICE/WARNING/CRITICAL）。明确分桶阈值、迟滞（+3%）和最短驻留时间（1.5 s，告警升级不受限制），由前端的 `derive-ui-state` 计算。业务逻辑设计说明书应把它们定义为状态机，而不是在组件里随手写 if/else。
5. **§37 更新频率补 UI 层**：遥测 10–50 Hz，3D 位姿每帧插值，**离散状态图标不超过 0.7 Hz**，数值读数 5–10 Hz 节流，并按 rAF 批量提交 React 状态。
6. **§38 右侧 DRONES 列表**：每行包含 `Drone` 图标、电量档、链路档、GNSS、告警，共 4 个状态图标。V0.6 多机时，同屏行数乘以 4 很快就会超过并发预算，需要写明"仅可见行和选中行 morph，其余行 set"。列表应虚拟化（shadcn 没有现成组件，可用 TanStack Virtual）。
7. **§39 Timeline 控件明确化**：播放和暂停合并为一个按钮（`StateIcon` Play↔Pause，快捷键 Space）；×1/×2/×5/×10 用 `ToggleGroup` 文本项，不用图标；增加 Live 指示（`Radio` 加红点）和 Loop（Repeat↔Repeat1）；Seek 拖动时图标不 morph。
8. **§40 相机模式用并列的 ToggleGroup**（Move3d/Video/ScanEye/Map），不用单按钮循环，因为这四个形状之间的 morph 不合格。Follow 开关用 Locate↔LocateFixed 的 morph。Trajectory、Camera FOV、Wind Force、Velocity 这些叠加层开关用 `Toggle` 加静态图标（Spline/Cone/Wind/MoveUpRight）。
9. **性能降级链路要纳入 UI 动效**：§14 的点云自适应（point budget、FPS 反馈）应当与 UI 共用一个 quality controller。帧时间持续超过 20 ms 时，按顺序降级：`iconPolicy: full → swap` → 面板过渡改为瞬切 → 下调点预算 → 下调 DPR。前两步几乎不影响数据可视化，应当最先执行。
10. **"流畅性测试"补 UI 维度**：复用本单元的 `dom-bench` 思路。在 3D 场景满载时，同时触发 K 个图标 morph，断言帧时间 p95 < 20 ms；并断言静止时 rAF 回调数只来自渲染循环本身，也就是 morph 调度器处于停止状态。
11. **§42 仓库结构**：在 `web/src/components/icons/` 下放 registry、custom、icon、state-icon、lucide-compat、prewarm；在 `scripts/` 下放 shadcn-icons-codemod 和 check-icons；在 `tools/icon-sheet/` 下放 contact sheet 生成器，供设计验收使用。
12. **V0.6 编队 UI**：在"Formation"功能中加入"队形变换预览"。用 §3.8 的 Procrustes 加极坐标插值生成不塌缩的过渡，作为规划器的参考初值，并在 PRD 中说明它不参与安全决策。
