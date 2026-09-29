# G7 补充深挖：设计体系在 Base UI（shadcn base-mira）上的落地细节——卸载时序、32 个配方的状态映射、Tailwind v4 token 统一、mira 实际使用的 lucide 图标、LfChartCard 高密度规格，附可运行 UI 样板页

> 缺口编号：G7（见 `00-index.md` §9、§8 C6）｜ 日期：2026-09-28 ｜ 相关单元：d02（transitions.dev）、d04（shadcn/ui）、d03（morphicons）、d01（lieflat-charts）
> 对应设计：`docs/01-design.md` §34（前端技术栈）、§38–40（UI、Timeline、Drone Interaction）
>
> 核对的源码：
> - `@base-ui/react@1.8.0`（npm latest），位于 `.cache/research/d04/trial/node_modules/@base-ui/react/`：`internals/useTransitionStatus.mjs`、`internals/useAnimationsFinished.mjs`、`internals/useOpenChangeComplete.mjs`、`internals/getDisabledMountTransitionStyles.mjs`、`utils/popups/popupStoreUtils.mjs`、`menu/root/MenuRoot.mjs`、`popover/store/PopoverStore.mjs`、`toast/root/ToastRoot.mjs`、`tabs/panel/TabsPanel.mjs`、`checkbox/indicator/CheckboxIndicator.mjs`，以及 `docs/react/handbook/animation.md` 和各组件的 `docs/react/components/*.md`
> - `refs/design/ui/apps/v4/registry/bases/base/ui/*.tsx`（IconPlaceholder）、`refs/design/ui/apps/v4/registry/styles/style-mira.css`、`refs/design/ui/packages/shadcn/src/tailwind.css`
> - base-mira 的实际产物：`.cache/research/d04/trial/src/components/ui/*.tsx`（由 `shadcn init -b base` 加 `add --all` 生成，61 个文件）
> - `refs/design/transitions.dev/skills/transitions-dev/`（`_root.css` 与 `01…32-*.md`）
> - `tailwindcss@4.3.3`（`theme.css`、`dist/lib.js` 中的 utility 定义）、`cn@0.4.0`（shadcn 新的 class 合并引擎）、`morphicons@1.7.1`、`lucide@1.48.0`
>
> 本次产物，全部在 `/data/projs/anet-drone/.cache/research/g07/`：
> - `sample/`：**可运行的 UI 样板页**（React 19.2、Vite 8、Tailwind 4.3.3、@base-ui/react 1.8.0、base-mira 组件、morphicons 1.7.1、lucide 1.48.0）。可以直接作为 `apps/web` 的骨架。
> - `verify.mjs`：用 playwright-core 驱动 headless Chromium，对 full、lite、reduced、OS reduced 四种条件逐项实测。结果写在 `verify.json`，截图在 `shots/`。
> - `fps.mjs`：空闲帧率探针。
>
> 测试环境：本机 load average 约 15（8 核，其他 agent 在并发跑任务），渲染用 SwiftShader 软件光栅，帧间隔经常被拉长到 100–350 ms，**所有绝对时长都不可信**。因此判据一律采用**与帧率无关**的量，例如"全部动画 `finished` 之后到卸载的间隔"和"被等待的动画数量"。

---

## 0. 结论速览

| # | 问题 | 结论 | 依据 |
|---|---|---|---|
| 1 | Base UI 卸载时是否等待 transition 结束 | **会等待**。关闭时 Base UI 对 **Popup（Toast 为 Root）元素本身**执行 `Promise.all(el.getAnimations().map(a => a.finished))`，全部结束后才用 `flushSync` 卸载。CSS transition 和 CSS animation 都会被等待，但**不包括子树和 `::before`/`::after`**。如果元素上没有动画，下一帧就卸载 | 源码链路：`useOpenStateTransitions` → `useOpenChangeComplete` → `useAnimationsFinished`。实测（full/lite 档）：Dialog、Sheet、Dropdown、Tooltip 从"全部 finished"到卸载只隔 **2–13 ms**，即 1 帧以内，被等待的动画数分别是 2、3（lite 为 2）、2、2。reduced 档下动画数为 0，卸载在 ≤1 帧内完成（§1） |
| 1′ | 对 d02 的影响 | d02 以"Radix Presence 只等 `animationend`"为前提，把 6 类浮层改写成 keyframes（`radix.css`），**这一层整体作废**，改为 transition 映射（`styles/motion/base-ui.css`，§2）。Base UI 部件在 reduced 档**直接用 0s**，不再需要 0.01ms 的技巧；0.01ms 只保留给依赖 `transitionend` 的 JS 配方。Sonner 换成 Base UI Toast；`SlidingTabsList` 改用 `Tabs.Indicator`；`useTooltipTravel` 改用 `Tooltip.createHandle` | 实测：中途打断时 transition 会平滑反向，Tooltip 关闭的实际时长是 46 ms 而不是 50 ms，这是规范规定的反向缩短（reversing shortening） |
| 2 | 32 个配方的前置态和关闭态怎么映射 | 通用规则见 §2.1：**配方的前置态对应 `[data-starting-style]`，`.is-open` 对应默认规则，`.is-closing` 对应 `[data-ending-style]`**。时序上**打开时长必须写在默认规则里，关闭时长写在 `[data-ending-style]` 里**（因为 CSS 取 after-change style 的 transition 值）。部分配方的写法相反，把关闭时长放在基础规则里（07/17/21/22/03），迁移时要把它们挪过来。32 个配方的逐项落点见 §2.3 | 样板页已落地 05、06、07、08、16、17、21、22、25、27、32，并在三档下实测 |
| 3 | Tailwind v4 的 `--ease-*` 与 transitions.dev 同名 token 怎么统一 | ① `--ease-*`、`--blur-*` 放进 **`@theme static`**（非 inline）。同名的 `--ease-out` 和 `--ease-in-out` 按 transitions.dev 改写为 CSS 关键字；base-mira 里只有 sheet 用了 `ease-in-out`，而且会被 codemod 删掉，所以无副作用。② Tailwind 的时长命名空间是 `--transition-duration-*`，不是 `--duration-*`，所以需要建别名。③ **必须写 `static`**：Tailwind 4.3 默认只输出用到的主题变量，实测不写时 `--ease-out` 和 `--ease-bounce` 都不存在，JS 读出来是空串。④ 动效 CSS 放进 `@layer motion`，声明在 `utilities` 之后。⑤ shadcn 组件里的 `cn` 必须登记这些自定义名，否则 `text-hud-sub` 会被当成文字颜色，并把 `text-muted-foreground` 吞掉（§3） | 以上 5 点都有实测或源码依据 |
| 4 | base-mira 组件实际 import 了哪些 lucide 名字 | **同样是 16 个，但集合不同**：`ArrowDownIcon CheckIcon ChevronDownIcon ChevronLeftIcon ChevronRightIcon ChevronUpIcon CircleCheckIcon InfoIcon Loader2Icon MinusIcon MoreHorizontalIcon OctagonXIcon PanelLeftIcon SearchIcon TriangleAlertIcon XIcon`。与 d03（new-york-v4）相比，新增 6 个：ChevronLeftIcon、CircleCheckIcon、InfoIcon、OctagonXIcon、TriangleAlertIcon、MoreHorizontalIcon；减少 6 个：ArrowLeft、ArrowRight、ChevronRight（无后缀）、CircleIcon、GripVerticalIcon、MoreHorizontal。两个是别名：Loader2 对应 canonical LoaderCircle，MoreHorizontal 对应 Ellipsis（§4） | 对 registry 源码 23 个文件的 IconPlaceholder 取并集，再与 `shadcn add --all` 的实际产物交叉核对，两者一致 |
| 5 | LfChartCard 在 mira 高密度下的字号与间距 | HUD 密度：`<Card size="sm">`（12 px 内边距），`gap-2 rounded-xl`（约 10 px）。**不要再加 border**，mira 已用 `ring-1` 描边，再加会出现双线。标题 13/18/600，副标题 11/16，KPI 22/28/800，标签和来源 10/12，全大写、字距 .08em。这些字阶作为 `--text-hud-*` 写入 `@theme`。300 px 宽的侧栏里，单卡实测高 130 px（§5） | 在样板页中用 `getComputedStyle` 实测得到 |
| + | 其他新发现 | ① Base UI 的 `Menu.GroupLabel`（shadcn 的 `DropdownMenuLabel`）必须放在 `DropdownMenuGroup` 内，否则运行时报 **Base UI error #31**，整个 React 树卸载。Radix 没有这条约束。② Positioner 的定位方式是 `transform: translate()`，共享气泡的移动过渡必须包含 `transform`。③ Tailwind v4 的 `translate-*`、`scale-*`、`rotate-*` 写的是独立的 `translate`/`scale`/`rotate` 属性，Switch thumb 的过渡要列 `translate` 而不是 `transform`（首版写错，实测 thumb 直接跳位）。④ lite 档必须让前置态和关闭态的 `filter` 回到 `none`：`blur(0px)` 与 `none` 是不同的计算值，仍会生成一条 filter 过渡。⑤ 用 Playwright 瞬移点击 Base UI 触发器时，约 1/3 概率打不开，先 hover 再 click 就稳定了（e2e 规范）。⑥ 带 `defaultOpen` 首次挂载的浮层默认**不播**进场动画（`animateInitialOpen=false`） | §6 |

---

## 1. Base UI 卸载时序（源码与实测）

### 1.1 源码链路（@base-ui/react 1.8.0）

```text
Popup 类部件（Dialog/AlertDialog/Popover/Menu/ContextMenu/Menubar/Select/Combobox/Tooltip/PreviewCard/NavigationMenu）
  useOpenStateTransitions(open, store)                      utils/popups/popupStoreUtils.mjs L405–L440
    └ useTransitionStatus(open)                              internals/useTransitionStatus.mjs
        open 由 false 变 true：mounted=true，status='starting' → 渲染 [data-starting-style]
                               下一帧 AnimationFrame.request → status=undefined（属性移除，开始进场过渡）
        open 由 true 变 false：status='ending' → 渲染 [data-ending-style]（同时 [data-closed]）
    └ useOpenChangeComplete({ enabled: mounted && !open, ref: popupRef, onComplete: forceUnmount })
        └ useAnimationsFinished(popupRef)                    internals/useAnimationsFinished.mjs
            frame.request(exec)                              // 等一帧，让新样式生成动画对象
            exec = Promise.all(el.getAnimations().map(a => a.finished))
                     .then(done)                             // done = ReactDOM.flushSync(forceUnmount)
                     .catch(() => 某动画被取消 → 若仍有 pending/running 的，则重新 exec，否则 done)
            if (!el.getAnimations || globalThis.BASE_UI_ANIMATIONS_DISABLED) → 立即执行
Toast：ToastRoot.mjs L103，对 rootRef 做同样处理，完成后执行 store.removeToast(id)
TabsPanel / CheckboxIndicator / Collapsible(Accordion)Panel / *ItemIndicator：同样的 useOpenChangeComplete，作用于自身元素
```

要点：

1. **`getAnimations()` 是按元素查询的**（调用时没有传 `{subtree:true}`），所以只等 Popup 或 Root **自身**的 CSS transition、CSS animation 和 WAAPI 动画。子元素和伪元素上的动画不会被等待。
   **规则**：一个部件里**最长的关闭动效必须放在被等待的那个元素上**。遮罩 Backdrop 不会被等待，它的关闭时长必须小于或等于 Popup 的关闭时长；本方案是 150 ≤ 150（Dialog）和 150 ≤ 350（Sheet）。
2. transition 与 keyframes **都会被等待**。base-mira 自带的写法 `data-open:animate-in … data-closed:animate-out`（tw-animate keyframes）也能正确卸载，但 keyframes 在中途打断时会跳变。Base UI 官方推荐使用 transition，本项目也采用 transition（实测 Tooltip 打开到一半就关闭时，关闭时长会按反向缩短规则变为 46 ms，视觉上连续）。
3. **没有动画就立即卸载**：`transition-duration: 0s`，或者关闭态的计算值与当前值相同（例如打开过渡还没开始、opacity 仍然是 0），`getAnimations()` 都会返回空，元素在下一帧卸载。所以 reduced 档对 Base UI 部件**直接写 0s**。d02 为 Radix 设计的"0.01ms 保证 animationend 触发"在这里不需要。
4. 进场的第一帧：Popover、Menu、Combobox 的 Popup 以及所有 Positioner 带有**内联的 `style="transition:none"`**（`getDisabledMountTransitionStyles`），用于"关闭途中又重新打开"时让起点立即生效。**动效层禁止对 transition 使用 `!important`**，否则会压过这条内联样式。
5. 带 `defaultOpen` 且在首屏就挂载的浮层，默认**不会**进入 `'starting'`（`animateInitialOpen=false`），因此页面加载时不会播放进场动画。子菜单这类由用户操作触发挂载的子树会自动开启进场动画。
6. 可用的生命周期钩子：Root 的 `onOpenChange(open, details)` 在状态改变时立即触发，`onOpenChangeComplete(open)` 在动画结束后触发（例如"模态打开期间 3D 降到 ≤15 fps，关闭动画结束后恢复"，见 d02 §4.6）。另外可以用 `actionsRef.current.unmount()` 手动卸载。

### 1.2 `data-instant`：Base UI 规定"应该瞬切"的场景

| 部件 | 取值（源码或文档） | 含义 | 本项目处理 |
|---|---|---|---|
| Menu、ContextMenu、Menubar | `click`（**键盘**激活，`event.detail===0`）、`dismiss`（Esc 或点击外部）、`group`（menubar 组内切换）、`trigger-change` | 键盘用户和组内切换不需要过渡 | `[data-instant]{transition-duration:0s}`，实测键盘 Enter 打开、Esc 关闭时动画数为 0 |
| Popover | `click`、`dismiss`、`focus`、`trigger-change` | 同上 | 同上 |
| Tooltip | `delay`（Provider 分组 timeout 内切换到相邻 trigger）、`dismiss`、`focus` | 等价于 transitions.dev 17 的"已显示时换目标不带延迟" | 同上 |
| PreviewCard | `dismiss`、`focus` | — | 同上 |

鼠标点击打开时 `data-instant` 为空，正常播放 05 的 250 ms 动效。

### 1.3 实测（`verify.json`；判据与帧率无关）

| 部件 | 关闭方式 | full：被等待的动画 | full：finished 到卸载 | lite：被等待的动画 | lite：finished 到卸载 | reduced / OS reduced |
|---|---|---|---|---|---|---|
| Dialog | 取消按钮 | opacity 150、transform 150 | 8.2 ms | 同左 | 12.2 ms | 动画数 0，6.0 / 5.9 ms |
| Sheet（right） | Esc | filter 350、opacity 350、transform 350 | 4.5 ms | opacity 350、transform 350（**filter 已被 lite 规则剔除**） | 5.1 ms | 0，7.3 / 5.4 ms |
| DropdownMenu | 点击外部 | opacity 150、transform 150 | 4.2 ms | 同左 | 3.8 ms | 0，13.0 / 9.2 ms |
| DropdownMenu | 键盘打开，Esc 关闭 | 0（`data-instant="dismiss"`） | 5.6 ms | 0 | 5.9 ms | 0 |
| Tooltip | 指针离开 | opacity 46、transform 46（反向缩短） | 4.5 ms | 同左 | 2.4 ms | 0，3.3 / 9.3 ms |
| TabsPanel（退出面板） | 切换 tab | filter、opacity、transform 各 250 | 4.4 ms | opacity、transform 各 250 | 3.3 ms | 0 |

空闲帧率（`fps.mjs`，按 lite、full、reduced、full、lite 交替顺序）：21.5（冷启动）、60、59、60、60.5。full 档空闲时没有额外开销；`verify.json` 里 full 档的 idleFps 只有 1–2，是因为它是冷启动后的第一页，不能据此下结论。

---

## 2. transitions.dev 配方到 Base UI 部件的映射

### 2.1 通用规则

```text
transitions.dev（三态类名）                          Base UI（数据属性）
────────────────────────────────────────────────────────────────────────────────────────
.t-x 基础规则里的"隐藏值"(opacity:0 / scale / translate / blur)  →  [data-starting-style]  以及（对称配方）[data-ending-style]
.t-x.is-open / [data-open="true"] 的"显示值"                     →  部件默认规则（不带状态属性）
.t-x.is-closing / [data-open="false"] 的覆盖值                    →  [data-ending-style]
打开时长/曲线（配方写在 .is-open 或基础规则上）                   →  **默认规则**的 transition（离开 starting 时，after-change style 为默认规则）
关闭时长/曲线（配方写在 .is-closing 或基础规则上）                →  **[data-ending-style]** 规则里的 transition-duration / -timing-function
data-origin="top-left|…"                                          →  transform-origin: var(--transform-origin)（Positioner 按实际 side/align 计算）
JS 三态机（closed → open → closing → 移除）                       →  删除，由 Base UI 的 transitionStatus 接管
.is-init（防止挂载时播放）                                        →  删除；新挂载元素没有 before-change style，不会产生过渡
@media (prefers-reduced-motion) { transition:none!important }    →  保留为首帧兜底（不影响 Base UI 内联的 transition:none）；运行期由 html[data-motion] 接管
```

**两种写法要注意区分**：05 和 06 把打开时长写在基础规则、关闭时长写在 `.is-closing` 上，可以直接搬。07、17、21、22 以及 03 的 dot，把**关闭时长写在基础规则**、打开时长写在激活态上。迁移这几个时必须对调：Base UI 的"打开终态"是默认规则，"关闭"是 `[data-ending-style]`。

### 2.2 落地代码（`sample/src/styles/motion/base-ui.css` 节选，完整文件共 191 行）

```css
@layer motion {                       /* index.css 头部声明：@layer theme, base, components, utilities, motion; */
  /* 05 Menu dropdown：所有锚定弹层 */
  :is([data-slot="dropdown-menu-content"], [data-slot="dropdown-menu-sub-content"], [data-slot="context-menu-content"],
      [data-slot="context-menu-sub-content"], [data-slot="menubar-content"], [data-slot="menubar-sub-content"],
      [data-slot="popover-content"], [data-slot="hover-card-content"], [data-slot="select-content"], [data-slot="combobox-content"]) {
    animation: none;                                   /* 兜底：codemod 漏删 tw-animate 类时也不会叠加 keyframes */
    transform-origin: var(--transform-origin);
    transition-property: transform, opacity;
    transition-duration: var(--dropdown-open-dur);   transition-timing-function: var(--dropdown-ease);
    &[data-starting-style] { opacity: 0; transform: scale(var(--dropdown-pre-scale)); }
    &[data-ending-style]   { opacity: 0; transform: scale(var(--dropdown-closing-scale)); transition-duration: var(--dropdown-close-dur); }
    &[data-instant]        { transition-duration: 0s; }
  }
  [data-slot="select-content"][data-side="none"]:is([data-starting-style], [data-ending-style]) { transform: none; } /* alignItemWithTrigger */

  /* 06 Modal */
  :is([data-slot="dialog-content"], [data-slot="alert-dialog-content"]) {
    animation: none; transform-origin: center;
    transition-property: transform, opacity;          /* 居中用的是 Tailwind v4 的 translate 属性，与 transform 互不干扰 */
    transition-duration: var(--modal-open-dur); transition-timing-function: var(--modal-ease);
    &[data-starting-style] { opacity: 0; transform: scale(var(--modal-scale)); }
    &[data-ending-style]   { opacity: 0; transform: scale(var(--modal-scale-close)); transition-duration: var(--modal-close-dur); }
  }
  :is([data-slot="dialog-overlay"], [data-slot="alert-dialog-overlay"], [data-slot="sheet-overlay"]) {   /* 不被等待：≤ Popup 关闭时长 */
    animation: none; backdrop-filter: none; background-color: rgb(0 0 0 / 0.6);
    transition: opacity var(--duration-quick) var(--ease-smooth-out);
    &:is([data-starting-style], [data-ending-style]) { opacity: 0; }
  }

  /* 07 Panel reveal：Sheet，按 data-side 侧向平移 40px */
  [data-slot="sheet-content"] {
    --_tx: 0px; --_ty: 0px;
    &[data-side="right"] { --_tx: var(--distance-drawer); }  &[data-side="left"] { --_tx: calc(var(--distance-drawer) * -1); }
    &[data-side="bottom"] { --_ty: var(--distance-drawer); } &[data-side="top"]  { --_ty: calc(var(--distance-drawer) * -1); }
    transition-property: transform, opacity, filter;
    transition-duration: var(--panel-open-dur); transition-timing-function: var(--panel-ease);
    &:is([data-starting-style], [data-ending-style]) { opacity: 0; transform: translate3d(var(--_tx), var(--_ty), 0); filter: blur(var(--panel-blur)); }
    &[data-ending-style] { transition-duration: var(--panel-close-dur); }
  }

  /* 17 Tooltip（意图延迟用 <TooltipProvider delay={80} closeDelay={0} timeout={400}>，不用 CSS transition-delay） */
  [data-slot="tooltip-content"] {
    animation: none; transform-origin: var(--transform-origin);
    transition-property: transform, opacity; transition-duration: var(--tt-in-dur); transition-timing-function: var(--tt-in-ease);
    &:is([data-starting-style], [data-ending-style]) { opacity: 0; transform: scale(var(--tt-scale)); }
    &[data-ending-style] { transition-duration: var(--tt-out-dur); transition-timing-function: var(--tt-out-ease); }
    &[data-instant] { transition-duration: 0s; }
  }
  /* 17 的共享气泡移动：Tooltip.createHandle()；Positioner 用 top:0;left:0 + transform:translate() 定位，所以必须过渡 transform */
  [data-slot="tooltip-positioner"][data-shared] {
    transition: transform var(--tt-move-dur) var(--tt-move-ease), top var(--tt-move-dur) var(--tt-move-ease), left var(--tt-move-dur) var(--tt-move-ease);
    &[data-instant] { transition: none; }
  }
  [data-slot="tooltip-positioner"][data-shared] > [data-slot="tooltip-content"] {
    width: var(--popup-width, auto); height: var(--popup-height, auto); transition-property: transform, opacity, width, height;
  }

  /* 22 Toast + 32 Banner stacking（Base UI Toast，limit=3） */
  [data-slot="toast"] {
    --peek: var(--stack-peek);
    --scale: calc(max(0, 1 - (var(--toast-index) * var(--stack-depth-scale))));   /* 每层缩 .06（mira 为 .1） */
    transition: transform var(--toast-open) var(--toast-ease), opacity var(--toast-open) var(--toast-ease),
                filter var(--toast-open) var(--toast-ease), height var(--duration-quick) var(--toast-ease);
    &:not([data-expanded], [data-starting-style], [data-ending-style], [data-limited]) {
      opacity: calc(1 - min(var(--toast-index), 2) * var(--stack-depth-fade) * 0.5); }        /* 深度 1、2 层分别为 .8、.6 */
    &[data-limited] { opacity: 0; }        /* mira 的 data-limited:opacity-0 在 utilities 层，会被本层覆盖，因此重申；上一行的 :not 必须排除它 */
    &[data-starting-style] { opacity: 0; filter: blur(var(--toast-blur)); transform: translateY(var(--toast-distance)) scale(var(--toast-scale)); }
    &[data-ending-style] { transition-duration: var(--toast-close); }
    &[data-ending-style]:not([data-limited]):not([data-swipe-direction]) {        /* 超时或点关闭：原位下沉 16px，缩到 .97，模糊淡出 */
      opacity: 0; filter: blur(var(--toast-blur));
      transform: translateY(calc(var(--toast-distance) - (var(--toast-index) * var(--peek)) - (var(--shrink) * var(--height))))
                 scale(calc(var(--scale) * var(--toast-scale))); }
    &[data-expanded][data-ending-style]:not([data-limited]):not([data-swipe-direction]) {
      transform: translateY(calc(var(--offset-y) + var(--toast-distance))) scale(var(--toast-scale)); }
  }   /* 滑动关闭（data-swipe-direction）沿用 mira 的 swipe 位移 */

  /* 21 Accordion：高度变量 + 内容淡入模糊 + chevron 纵向翻转 */
  [data-slot="accordion-content"] {
    animation: none; height: var(--accordion-panel-height); transition: height var(--acc-expand) var(--acc-ease);
    &:is([data-starting-style], [data-ending-style]) { height: 0; }
    &[data-ending-style] { transition-duration: var(--acc-collapse); }
    & > [data-slot="accordion-content-inner"] { transition: opacity var(--acc-expand) var(--acc-ease), filter var(--acc-expand) var(--acc-ease); }
    &:is([data-starting-style], [data-ending-style]) > [data-slot="accordion-content-inner"] { opacity: 0; filter: blur(var(--blur-small)); }
  }
  [data-slot="accordion-trigger-icon"] { transition: transform var(--acc-chevron) var(--acc-ease); transform-origin: center; }
  [data-slot="accordion-trigger"][data-panel-open] > [data-slot="accordion-trigger-icon"] { transform: scaleY(-1); }

  /* 16 Tabs sliding：Base UI Tabs.Indicator 提供的 --active-tab-* 变量 */
  [data-slot="tabs-indicator"] {
    position: absolute; left: 0; top: 0; z-index: 0; border-radius: var(--radius-md); background: var(--background);
    width: var(--active-tab-width); height: var(--active-tab-height); translate: var(--active-tab-left) var(--active-tab-top);
    transition: translate var(--tabs-dur) var(--tabs-ease), width var(--tabs-dur) var(--tabs-ease);
  }
  [data-slot="tabs-list"]:has(> [data-slot="tabs-indicator"]) > [data-slot="tabs-trigger"] {
    z-index: 1; transition: color var(--tabs-dur) var(--tabs-ease); &[data-active] { background: transparent; border-color: transparent; } }

  /* 08 Page side-by-side：Tabs.Panel（data-activation-direction），外层 TabsPanels 为单格 grid */
  [data-slot="tabs-panels"] { display: grid; }  [data-slot="tabs-panels"] > [data-slot="tabs-content"] { grid-area: 1 / 1; }
  [data-slot="tabs-content"] {
    transition: opacity var(--page-fade-dur) var(--page-fade-ease), transform var(--page-slide-dur) var(--page-slide-ease), filter var(--page-slide-dur) var(--page-slide-ease);
    &:is([data-starting-style], [data-ending-style]) { opacity: 0; filter: blur(var(--page-blur)); }
    &[data-starting-style][data-activation-direction="right"] { transform: translateX(var(--page-slide-distance)); }
    &[data-starting-style][data-activation-direction="left"]  { transform: translateX(calc(var(--page-slide-distance) * -1)); }
    &[data-ending-style][data-activation-direction="right"]   { transform: translateX(calc(var(--page-slide-distance) * -1)); }
    &[data-ending-style][data-activation-direction="left"]    { transform: translateX(var(--page-slide-distance)); }
  }

  /* 27 Toggle：Tailwind v4 的 translate-x-* 写的是 translate 属性 */
  [data-slot="switch-thumb"] { transition: translate var(--toggle-dur) var(--toggle-ease), background-color var(--duration-quick) var(--ease-smooth-out); }

  /* 25 Checkbox：Indicator 挂载即描画；取消勾选时由 Indicator 自身的 opacity 过渡让 Base UI 等待 150ms */
  [data-slot="checkbox-indicator"] {
    transition: opacity var(--check-uncheck) var(--check-ease);
    &[data-ending-style] { opacity: 0; }
    & svg path { stroke-dasharray: var(--check-len) var(--check-len); stroke-dashoffset: 0;
                 transition: stroke-dashoffset var(--check-draw) var(--check-ease) var(--check-delay); }
    &[data-starting-style] svg path { stroke-dashoffset: var(--check-len); }   /* CheckIcon 带 pathLength=1，--check-len:1 */
  }
}
```

### 2.3 32 个配方逐项落点

"宿主"一列：**BU** 表示由 Base UI 管理挂载和卸载，按 §2.1 映射；**BU-常驻** 表示 Base UI 部件一直挂载，只切换状态属性；**自研** 表示不经过 Base UI，沿用 d02 的 hooks 和三态机。"样板页"一列标"已验证"的，表示已在 `sample/` 落地并实测。

| # | 配方 | 宿主与部件 | 前置态 → | 关闭态 → | 时长放置 | 样板页 |
|---|---|---|---|---|---|---|
| 01 | card-resize | BU-常驻：Sidebar 的 `sidebar-gap`、`sidebar-container`（mira 为 `duration-200 ease-linear`），HUD 小部件 | — | — | 在 motion 层把两者覆盖为 `var(--resize-dur) var(--resize-ease)`（300 ms，smooth-out） | 规则给出 |
| 02 | number-pop-in | 自研 `MotionNumber`（WAAPI 重放） | — | — | 按 d02 §3.4-c；reduced 档用 0.01ms 变量 | — |
| 03 | notification-badge | 自研 Badge dot，常驻，只切换 `data-open` | 原配方 `[data-open=false]` | 同左 | 关闭值在 false 规则上，保持原样（不经过 Base UI） | — |
| 04 | text-states-swap | 自研 `SwapText`（依赖 transitionend） | — | — | reduced 用 0.01ms | — |
| **05** | menu-dropdown | **BU**：Menu、ContextMenu、Menubar 及其 Sub、Popover、PreviewCard(hover-card)、Select、Combobox 的 Popup | `[data-starting-style]` scale .97、opacity 0 | `[data-ending-style]` scale .99、150 ms | 默认规则 250 ms；`[data-instant]` 为 0s | 已验证 |
| **06** | modal | **BU**：Dialog、AlertDialog 的 Popup，Backdrop 只做 opacity | scale .96 | scale .96、150 ms | 默认规则 250 ms | 已验证 |
| **07** | panel-reveal | **BU**：Sheet（Dialog）Popup；右侧 DroneRail 用 Collapsible 实现时同理 | translate 40 px（按 side）+ blur 2 | 同左、350 ms | **需对调**：默认规则 400 ms | 已验证 |
| **08** | page-side-by-side | **BU**：Tabs.Panel 放在 `TabsPanels` 单格 grid 内；右栏机群列表与单机详情用同一结构 | ±8 px（`data-activation-direction`）+ blur 3 | 反向 ±8 px | 两侧都是 250 ms | 已验证 |
| 09 | icon-swap | 自研 `StateIcon`（同一个 svg 内的 ghost path，WAAPI） | — | — | lite 档不加 blur；reduced 档直接 `set` | 已验证 |
| 10 | success-check | 自研 `SuccessCheck` | — | — | reduced 0.01ms | — |
| 11 | avatar-group-hover | 自研，挂在 ToggleGroup 的 item 上 | — | — | — | — |
| 12 | error-shake | 自研 `ShakeOnce`；`.is-error` 对应 Base UI Field 的 `[data-invalid]`；`.is-shaking` 仍由 JS 驱动 | — | — | — | — |
| 13 | input-clear | 自研（只在 full 档启用） | — | — | — | — |
| 14 | skeleton-reveal | 自研 `SkeletonReveal` 包住 `Skeleton` | — | — | — | — |
| 15 | shimmer-text | 自研 `.t-shimmer`。**注意** shadcn 的 `tailwind.css` 自带 `shimmer` 工具类（目前只有 attachment 在用），为避免两套写法，lint 禁止使用它 | — | — | — | — |
| **16** | tabs-sliding | **BU-常驻**：`Tabs.Indicator`（`--active-tab-left/top/width/height`），替代 d02 的 MutationObserver 方案 | — | — | 250 ms，translate 与 width | 已验证 |
| **17** | tooltip | **BU**：Tooltip Popup；延迟交给 `Provider delay`；移动效果用 handle 加 Positioner 过渡 | scale .98 | 50 ms | **需对调**：默认规则 150 ms；共享气泡移动 160 ms | 已验证 |
| 18 | texts-reveal | 自研 | — | — | — | — |
| 19 | card-tilt | 不采用 | — | — | — | — |
| 20 | plus-menu-morph | **BU**：Popover Popup。推荐用 `clip-path: inset(calc(100% - 40px) 0 0 calc(100% - 40px) round var(--morph-r-closed))` 作为前置态，打开后为 `inset(0 round var(--morph-r-open))`，模拟"从 FAB 长出来" | clip-path + opacity | 同左、250 ms | 默认规则 350 ms bounce 1.25（V0.6，**未实测**） | — |
| **21** | accordion | **BU**：Accordion 和 Collapsible 的 Panel（`--accordion-panel-height`） | height 0；内层 opacity 0 + blur 2 | 同左 | 两侧都是 250 ms | 已验证 |
| **22** | toast | **BU**：Toast Root | 16 px、scale .97、blur 2 | 非滑动关闭时原位加 16 px；滑动关闭保留 mira 的位移 | **需对调**：默认规则 350 ms，ending 250 ms | 已验证 |
| 23 | like-button | 不采用 | — | — | — | — |
| 24 | learn-more | 仅参考 | — | — | — | — |
| **25** | checkbox-check | **BU**：Checkbox.Indicator（勾选时挂载）与 Root 的 `data-checked` | path 的 `dashoffset=1`（`pathLength=1`） | Indicator opacity 0、150 ms（被等待） | 描画 350 ms 写在 path 上 | 已验证 |
| 26 | spinning-counter | 自研 | — | — | — | — |
| **27** | toggle | **BU-常驻**：Switch.Thumb 的 `data-checked` | — | — | 350 ms bounce 1.35，属性为 `translate`；不再需要 `.is-init` | 已验证 |
| 28 | thinking-states | 自研 | — | — | — | — |
| 29 | reasoning-stream | 自研 | — | — | — | — |
| 30 | streaming-text | 自研 | — | — | — | — |
| 31 | matrix-loader | 自研 | — | — | — | — |
| **32** | banner-stacking | **BU**：Toast（`--toast-index` 表示深度、`data-expanded` 对应原配方的 `.is-spread`、`data-limited` 对应第 4 条被挤出、`data-behind` 隐藏后层正文） | 同 22 | 同 22 | peek 12 px，深度缩放 .06，深度淡出 | 已验证 |

### 2.4 shadcn 组件改动（codemod 与补丁，写入 `docs/ui-patches.md`）

1. `scripts/shadcn-motion-codemod.mjs`：只处理由 motion 层接管的 18 个 `data-slot` 所在元素的 className，删除以下内容：`animate-(in|out|none|accordion-*)`、`fade-*-0`、`zoom-*-95`、`slide-in-from-*-2`、`duration-\d+`、`ease-*`、`transition(-opacity|-all)?`、`opacity-0`、`translate-[xy]-[…]`、`backdrop-blur-*`，也包括带任意 `data-*:` 变体前缀的写法。脚本幂等。
   d04 原来的正则漏了 Sheet 上的 `data-starting-style:translate-x-[2.5rem]` 和 `transition duration-200 ease-in-out`；另外只有用"不跨越下一个 data-slot"的写法 `(?:(?!data-slot=)[\s\S]){0,400}?` 才能命中 `dialog-overlay`，初版就漏掉了它。实测样板页有 6 个文件被改动。
2. `@layer motion` 在 utilities 之后声明，即使 codemod 漏掉某个类，或者以后 `shadcn add` 重新引入，也照样被覆盖。实测它覆盖了 `data-active:bg-background`、`transition-transform`、`data-limited:opacity-0`，所以最后这条需要在 motion 层重申。
3. `accordion.tsx`：mira 原来用两个图标 `ChevronDownIcon`/`ChevronUpIcon`，通过 `group-aria-expanded:hidden` 瞬间切换。改为**一个** `ChevronDownIcon` 加 `scaleY(-1)`，对应配方 21。给内层 div 加上 `data-slot="accordion-content-inner"`，并删掉它上面不会生效的 `data-starting-style:h-0`：内层不是 Base UI 部件，这个属性永远不会出现。
4. `tabs.tsx`：当 `variant="default"` 时，在 `TabsList` 末尾渲染 `<TabsPrimitive.Indicator data-slot="tabs-indicator"/>`；新增 `TabsPanels`，即单格 grid 容器。
5. 所有 `components/ui/*.tsx` 中的 `import { cn } from "cn"` 都改为 `from "@/lib/utils"`（原因见 §3.4）。
6. `lucide-react` 改为从 `@/components/icons/lucide-compat` 导入（§4）。

---

## 3. Tailwind v4 与 transitions.dev token 的统一写法

### 3.1 命名空间事实（tailwindcss@4.3.3 源码）

| Tailwind 工具类 | 读取的主题键 | 与 transitions.dev 的关系 |
|---|---|---|
| `ease-*` | `--ease-*`（外加静态的 `ease-linear`、`ease-initial`） | **同一命名空间**。默认值 `--ease-in: (.4,0,1,1)`、`--ease-out: (0,0,.2,1)`、`--ease-in-out: (.4,0,.2,1)` 与 transitions.dev 的 `--ease-out: ease-out`、`--ease-in-out: ease-in-out`（CSS 关键字，分别为 (0,0,.58,1) 和 (.42,0,.58,1)）**同名但值不同** |
| `duration-*` | `--transition-duration-*`（裸数字按 ms 处理） | transitions.dev 的 `--duration-*` **不在** Tailwind 的命名空间里，写 `duration-fast` 不会生效，需要建别名 |
| `blur-*` | `--blur-*`（默认 xs…3xl） | `--blur-small/medium/large` 不重名，可以直接并入 |
| `default transition` | `--default-transition-duration`、`--default-transition-timing-function` | 映射到 `--duration-quick`（150 ms）和 `--ease-smooth-out`，影响 mira 所有 `transition-colors`/`transition-all` 的缺省值 |

base-mira 中实际使用 `ease-in-out`/`ease-out`/`ease-in` 工具类的只有 `sheet.tsx`（`ease-in-out`，codemod 会删），`ease-linear` 只有 `sidebar.tsx`。**因此在 `@theme` 中覆盖 `--ease-out`/`--ease-in-out` 对 mira 没有副作用**，以 transitions.dev 为准。

### 3.2 统一写法（`sample/src/styles/motion/tokens.css`，由 `scripts/gen-motion-tokens.py` 从 `_root.css` 生成，不要手改）

```css
@theme static {                      /* static：见 3.3 */
  --ease-smooth-out: cubic-bezier(0.22, 1, 0.36, 1);
  --ease-in-out: ease-in-out;         /* 覆盖 Tailwind 默认 (.4,0,.2,1)，与 transitions.dev 一致 */
  --ease-out: ease-out;               /* 覆盖 Tailwind 默认 (0,0,.2,1) */
  --ease-bounce: cubic-bezier(0.34, 1.36, 0.64, 1);
  --ease-bounce-strong: cubic-bezier(0.34, 3.85, 0.64, 1);
  --ease-spring-snappy: cubic-bezier(0.34, 1.36, 0.64, 1);
  --blur-small: 2px; --blur-medium: 3px; --blur-large: 8px;
  --transition-duration-stagger: var(--duration-stagger);  /* … micro/quick/fast/medium/slow/very-slow，共 7 个别名 */
  --default-transition-duration: var(--duration-quick);
  --default-transition-timing-function: var(--ease-smooth-out);
}
@layer theme {
  :root { --duration-stagger: 40ms; …; --distance-*: …; --scale-*: …; /* 32 组配方语义变量逐字复制 */ }
  .dark { --tt-bg: var(--popover); --matrix-base: color-mix(…); … }   /* d02 §4.3 */
}
```

用法：组件里写 `ease-smooth-out duration-fast blur-small`，或者等价的 `duration-(--duration-fast)`。lint 禁止 `duration-[\d+ms]`、`ease-[cubic-bezier…]` 这类任意值。

注意 `@theme` **不能加 inline**。inline 会把值直接写进工具类，`html[data-motion="lite"]{--blur-small:0px}` 这类运行期覆盖就不起作用了。

### 3.3 为什么必须写 `static`（实测）

Tailwind 4.3 默认只输出**被用到的**主题变量。不写 static 时，产物 CSS 里**没有** `--ease-out`、`--ease-bounce`，`getComputedStyle(root).getPropertyValue('--ease-out')` 返回空串。写上 `@theme static` 之后，全部都会输出，`verify.json` 的 tokens 一节中 `--ease-out` 读到 `"ease-out"`。

d02 的 `motion.ms()` 和 bezier 采样器（Three.js 相机与 LOD 渐入共用曲线）都依赖 JS 读取这些 token，所以 static 是必需的。

### 3.4 shadcn 新的 `cn` 必须登记自定义名（实测发现的缺陷）

base 系列组件直接写 `import { cn } from "cn"`（cn@0.4.0，语义与 tailwind-merge 相同，使用默认表）。默认表**不认识**本项目在 `@theme` 中新增的名字：

| 输入 | 默认 `cn` 的输出 | 问题 | 登记后的输出 |
|---|---|---|---|
| `"font-heading text-sm font-medium"` + `"text-hud-title font-semibold"` | `font-heading text-sm text-hud-title font-semibold` | 两个字号都保留，按样式表顺序 `text-sm` 胜出，**实测为 14px** | `font-heading text-hud-title font-semibold`（实测 13px） |
| `"text-xs/relaxed text-muted-foreground"` + `"text-hud-sub"` | `text-xs/relaxed text-hud-sub` | `text-hud-sub` 被当成**文字颜色**，结果**吞掉了 `text-muted-foreground`** | `text-muted-foreground text-hud-sub` |
| `"ease-in-out duration-200"` + `"ease-smooth-out duration-fast"` | 四个类全部保留 | 冲突没有消解 | `ease-smooth-out duration-fast` |
| `"blur-sm"` + `"blur-small"` | 两个都保留 | 同上 | `blur-small` |

修正方法（`sample/src/lib/utils.ts`，同时把所有组件的 `from "cn"` 改为 `from "@/lib/utils"`）：

```ts
import { createCn } from "cn/config"
export const cn = createCn({ extend: {
  theme: { text: ["hud-kpi", "hud-title", "hud-sub", "hud-cap"], ease: ["smooth-out", "bounce", "bounce-strong", "spring-snappy"], blur: ["small", "medium", "large"] },
  classGroups: { duration: [{ duration: ["stagger", "micro", "quick", "fast", "medium", "slow", "very-slow"] }] },
} })
```

规则：**以后每在 `@theme` 新增一个命名空间键，都要同步登记到这里**。CI 可以对 `@theme` 与 `createCn` 的键集合做差集检查。

### 3.5 层序与优先级

```css
@import "tailwindcss";
@layer theme, base, components, utilities, motion;   /* motion 最后声明，因此优先级最高（在普通声明中） */
@import "./styles/motion/tokens.css";  @import "./styles/motion/base-ui.css";  @import "./styles/motion/tiers.css";
```

- motion 层覆盖 utilities 层靠的是**层序**，不需要 `!important`，因此不会压过 Base UI 在 starting 帧写的内联 `transition:none`。
- 应用自己的非分层 CSS 仍然能覆盖 motion 层；按规范，业务代码不应该这样做。

### 3.6 档位（`tiers.css`，在 d02 §4.5 基础上按 Base UI 修订）

```css
@layer motion {
  html[data-motion="lite"] { --blur-small:0px; …(21 个 *-blur 置 0); --stagger-cap:150ms; }
  /* blur(0px) 与 none 是两个不同的计算值，仍会生成一条 filter 过渡。lite 档让前置态和关闭态的 filter 直接回到 none */
  html[data-motion="lite"] :is([data-starting-style],[data-ending-style],[data-starting-style]>*,[data-ending-style]>*) { filter:none; }
  html[data-motion="lite"] *, html[data-motion="lite"] *::before, html[data-motion="lite"] *::after { backdrop-filter:none; }
  /* reduced：Base UI 部件直接 0s；依赖 transitionend 的 JS 配方继续用 0.01ms 变量 */
  html[data-motion="reduced"] { --digit-dur:.01ms; --text-swap-dur:.01ms; …; --distance-drawer:0px; --shake-distance:0px; }
  html[data-motion="reduced"] :is([data-slot$="-content"],[data-slot$="-overlay"],[data-slot="toast"],[data-slot="tabs-indicator"],
      [data-slot="switch-thumb"],[data-slot="checkbox-indicator"] path,[data-slot="accordion-trigger-icon"],…) { transition-duration:0s; transition-delay:0s; }
}
```

实测：lite 档下 Sheet 被等待的动画从 3 条（filter、opacity、transform）降到 2 条；reduced 与 OS reduced 下所有部件的动画数都是 0，卸载都在 ≤1 帧内完成。

`motion/tier.ts` 将 `min(OS matchMedia, 用户, 性能调速器)` 写到 `<html data-motion>`，并提供 `useMotionTier()`（基于 `useSyncExternalStore`）给 StateIcon 等 JS 读取。

---

## 4. base-mira 实际使用的 lucide 名字与兼容层

### 4.1 逐文件清单（registry 源码中 IconPlaceholder 的 `lucide=` 属性，与 `shadcn add --all` 产物一致）

| 组件文件 | lucide-react 名字 |
|---|---|
| accordion | ChevronDownIcon、ChevronUpIcon（G7 补丁后只剩 ChevronDownIcon） |
| breadcrumb | ChevronRightIcon、MoreHorizontalIcon |
| calendar | ChevronDownIcon、ChevronLeftIcon、ChevronRightIcon |
| carousel | ChevronLeftIcon、ChevronRightIcon |
| checkbox、menubar、questionnaire | CheckIcon |
| combobox | CheckIcon、ChevronDownIcon、XIcon |
| command | CheckIcon、SearchIcon |
| context-menu、dropdown-menu | CheckIcon、ChevronRightIcon |
| dialog、sheet | XIcon |
| input-otp | MinusIcon |
| message-scroller | ArrowDownIcon |
| native-select、navigation-menu | ChevronDownIcon |
| pagination | ChevronLeftIcon、ChevronRightIcon、MoreHorizontalIcon |
| select | CheckIcon、ChevronDownIcon、ChevronUpIcon |
| sidebar | PanelLeftIcon |
| spinner | Loader2Icon |
| toast | XIcon、CircleCheckIcon、InfoIcon、TriangleAlertIcon、OctagonXIcon、Loader2Icon |
| sonner（base 下仍保留） | CircleCheckIcon、InfoIcon、TriangleAlertIcon、OctagonXIcon、Loader2Icon |

**并集共 16 个**：ArrowDownIcon、CheckIcon、ChevronDownIcon、ChevronLeftIcon、ChevronRightIcon、ChevronUpIcon、CircleCheckIcon、InfoIcon、Loader2Icon、MinusIcon、MoreHorizontalIcon、OctagonXIcon、PanelLeftIcon、SearchIcon、TriangleAlertIcon、XIcon。

与 d03 §4.3 的对比：

- **新增**：ChevronLeftIcon（calendar、carousel、pagination）、CircleCheckIcon、InfoIcon、TriangleAlertIcon、OctagonXIcon（这 4 个来自 toast 的类型图标）、MoreHorizontalIcon（带 Icon 后缀）。
- **不再需要**：ArrowLeft、ArrowRight（new-york 的 carousel 用）、ChevronRight（无后缀）、CircleIcon（new-york 中 radio 和菜单单选的指示器；base 版改用 CSS 圆点）、GripVerticalIcon（base 的 resizable 不用图标）、MoreHorizontal（无后缀）。
- 别名：lucide 1.48.0 中 `Loader2` 的 canonical 名是 `LoaderCircle`，`MoreHorizontal` 的 canonical 名是 `Ellipsis`（`.cache/research/d03/lucide-alias-map.json`）。兼容层按 canonical 名导入，以满足 d03 `check-icons` 的"只用 canonical 名"规则。

### 4.2 兼容层（`sample/src/components/icons/lucide-compat.tsx`）

```tsx
import { ArrowDown, Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, CircleCheck, Ellipsis, Info,
         LoaderCircle, Minus, OctagonX, PanelLeft, Search, TriangleAlert, X, type IconNode } from "lucide"
import { Icon } from "./icon"          // <svg data-icon> + 一条 canonicalD path（morphicons/dom）
function make(node: IconNode, name: string, extra?: { pathLength?: number } & Record<string, unknown>) {
  function C({ size, color, strokeWidth, absoluteStrokeWidth: _a, style, ...p }: CompatProps) {
    const s = { ...style } as React.CSSProperties & Record<string, string | number | undefined>
    if (color) s.color = color
    if (strokeWidth != null) s["--icon-stroke"] = `${Number(strokeWidth) * 0.75}px`   // 与 d03 一致：2 → 1.5px
    return <Icon icon={node} size={size} style={s} {...extra} {...p} />
  }
  C.displayName = name; return C
}
export const CheckIcon = make(Check, "CheckIcon", { "data-draw": "", pathLength: 1 })   // 对勾按 pathLength=1 做描边动画
export const Loader2Icon = make(LoaderCircle, "Loader2Icon"), MoreHorizontalIcon = make(Ellipsis, "MoreHorizontalIcon") /* …共 16 个 */
```

- 对勾描边动画不按几何长度 23 硬编码，而是 **`pathLength=1`** 加 `--check-len:1`。带 `data-draw` 的图标关闭 `vector-effect: non-scaling-stroke`，否则 dash 会按屏幕坐标计算。实测 `stroke-dashoffset` 过渡 350 ms 正常。
- base-mira 的组件都没有传 `strokeWidth` 或 `size`，全部靠 `[&_svg:not([class*='size-'])]:size-*` 控制。实测 mira 按钮（h-7）内图标为 **14 px**（`size-3.5`），描边 1.5 px。
- 语义约定：Toast 的 `error` 类型沿用 base 的 `OctagonXIcon`，表示"操作失败"；HUD 告警的"严重"级按 d03 §4.4 用 `OctagonAlert`。两者分开，不混用。

---

## 5. LfChartCard 在 mira 高密度下的规格（实测）

mira 的基线（实测）：正文 `text-xs/relaxed` 为 12/19.5；按钮高 28、字号 12；`CardTitle` 默认 14/500（`text-sm font-medium`）；`Card` 默认 `--card-spacing: 16px`，`size="sm"` 时为 12px；卡片描边是 `ring-1 ring-foreground/10`，没有 border 和 shadow；`rounded-lg` = `--radius` = 0.45rem ≈ 7.2 px。

### 5.1 HUD 密度（侧栏与状态栏）

| 部位 | className（`sample/src/components/lf/lf.tsx`） | 实测计算值 | 与 d01 草案的差异 |
|---|---|---|---|
| Card | `size="sm"` 加 `gap-2 rounded-xl` | padding 12，gap 8，圆角 10.08 px（`--radius×1.4`），ring 1 px，**border 0** | d01 草案写的 `border border-border shadow-none py-3 px-3` 在 mira 下会**双线**（ring 与 border 叠加），而且与 `--card-spacing` 冲突。改用 `size="sm"`；圆角由 12 px 改为 token 化的 `rounded-xl` |
| CardHeader | `gap-0.5` | 左右 padding 12 | — |
| 标题 | `text-hud-title font-semibold` | **13 / 18 / 600 / −0.01em** | 与 d01 相同；需要 `cn` 登记才能生效（§3.4） |
| 副标题（图例和时间范围） | `text-hud-sub`，颜色继承 muted | **11 / 16 / 400** | 同上 |
| CardAction | `ToggleGroup size="sm" variant="outline"` | 高 24、字号 12 | — |
| 指标标签 | `text-hud-cap font-semibold uppercase text-muted-foreground` | **10 / 12 / 600 / .08em** | — |
| KPI 大数 | `text-hud-kpi font-extrabold`，加 `data-numeric`（tabular-nums） | **22 / 28 / 800 / −0.02em** | — |
| 单位 | `text-hud-sub font-semibold text-muted-foreground` | 11 / 600 | — |
| 来源行 | `text-hud-cap font-medium uppercase text-muted-foreground` | **10 / 12 / 500 / .08em** | — |
| Sparkline | canvas 64×16，线宽 1.25，末点 r=1.75，颜色 `--lf-hero` | 4 Hz，走调度器，不触发 React 渲染 | — |
| 整卡 | 300 px 宽的侧栏 | **高 130 px**；3 张卡加 2 个 8 px 间距共 406 px | — |

字阶定义（`@theme`，已写入 `index.css`）：

```css
--text-hud-kpi: 1.375rem;    --text-hud-kpi--line-height: 1.75rem;   --text-hud-kpi--letter-spacing: -0.02em;
--text-hud-title: 0.8125rem; --text-hud-title--line-height: 1.125rem; --text-hud-title--letter-spacing: -0.01em;
--text-hud-sub: 0.6875rem;   --text-hud-sub--line-height: 1rem;
--text-hud-cap: 0.625rem;    --text-hud-cap--line-height: 0.75rem;    --text-hud-cap--letter-spacing: 0.08em;
```

这样 lint 可以禁止 `text-[13px]` 这类任意值。10 px 是最小字号，mira 自己的 xs 按钮用的也是 `text-[0.625rem]`，与 d01 的"最小 10"一致。

### 5.2 editorial 密度（分析页与报告）

`Card` 默认尺寸（16 px 间距），`gap-3 rounded-4xl`（`--radius×2.6` ≈ 18.7 px，最接近 d01 的 20–24），标题 `text-base font-bold tracking-[-0.02em]`，副标题 `text-xs`。d01 要求的 `--radius: .75rem` 不采用，统一使用 d04 的 `.45rem`，大圆角通过 token 倍数实现。

---

## 6. 其他新发现（均会影响实现）

1. **`DropdownMenuLabel` 必须放在 `DropdownMenuGroup` 内**。Base UI 的 `Menu.GroupLabel` 依赖 `MenuGroupContext`，缺少时在生产构建中抛出 `Base UI error #31`（开发模式的提示是 "MenuGroupContext is missing"），整个 React 树都会卸载。Radix 版没有这条约束，从 Radix 示例或 AI 生成的代码迁移过来时最容易踩到。建议加 ESLint 规则或单测：检查 `DropdownMenuLabel` 的祖先中有没有 `DropdownMenuGroup`。
2. **Positioner 用 `transform: translate(x,y)` 定位**（外加 `top:0; left:0`）。所以锚点移动的动画（共享 tooltip、以后 3D 物体投影锚定的 Popover）必须过渡 `transform`。实测只过渡 top 和 left 时没有任何动画。加上 transform 后，Positioner 上出现一条 160 ms 的 transform 过渡；其中一次运行在中途采样到 x=29.5（起点 5.5，终点 64）。其余几次运行的中途值没有变化，因为帧被饿死，属于环境噪声。
3. **Tailwind v4 的独立变换属性**：`translate-*`、`scale-*`、`rotate-*` 写入的是 `translate`、`scale`、`rotate` 属性。mira 的 Switch thumb、Dialog 居中（`-translate-x-1/2`）都属于这种情况。因此 Switch 的过渡要列 `translate`（实测修正后为 translate 350 ms、bounce 1.35）。Dialog 的缩放写在 `transform` 上，与居中用的 `translate` 互不干扰。
4. **Tooltip 意图延迟用 `Provider delay`，不用 CSS `transition-delay`**。没到延迟时不挂载 DOM，指针扫过 3D 工具栏零开销。组内切换由 `timeout` 触发 `data-instant="delay"` 瞬显。样板页的配置是 `delay={80} closeDelay={0} timeout={400}`；3D 画布边缘等密集区域设 `delay={400}`。
5. **e2e 规范**：Playwright 的 `page.click()` 会让指针从 (0,0) 瞬移后立即按下，Base UI 触发器约有 1/3 概率打不开（实测 3 次中 1 次失败；先 hover 或放慢按下后 9/9 成功）。统一用 `press = hover → 50 ms → click`。动效断言要用与帧率无关的判据：取 `el.getAnimations()` 的属性、时长和缓动，再比较 `finished` 与卸载的先后，**不要断言毫秒数**。
6. **Toast 的 mira 细节**：Root 用 `--toast-index`、`--toast-offset-y`、`--toast-height`、`--toast-frontmost-height`、`--toast-swipe-movement-x/y` 自己计算堆叠，`ToastContent` 在后层通过 `data-behind` 隐藏正文。本方案只替换深度系数（.1 改为 .06）、进出场和时长，**不改堆叠几何**。Provider 默认 `limit=3`、`timeout=5000`，第 4 条会带 `data-limited`。
7. **mira 中 `transition-all` 出现在 10 个组件里**（button、badge、toggle、tabs-trigger、switch 根、accordion-trigger、progress、sidebar、input-otp、navigation-menu）。这些控件都很小，改动的只有颜色和 1 px 位移，而且缺省时长已由 `--default-transition-*` 统一为 150 ms 加 smooth-out，所以保留不改。d02 禁止 `transition-all` 的 lint 规则**只作用于 `src/**` 下除 `components/ui/**` 以外的代码**。
8. **Accordion 的 mira 原样实现**：Panel 上用 keyframes（`animate-accordion-down/up`），内层 div 上的 `data-starting-style:h-0` 永远不会生效。首屏展开的项会在加载时播放 keyframes，transition 则不会，这也是改用 transition 的原因之一。
9. **logo**：`anet-logo.svg` 的主体是黑色填充，在暗色主题下只能看到红色外框（见 `shots/page-full.png` 左上角）。暗色需要一个白色填充的变体（`fill="currentColor"`），交给色卡和品牌单元处理。

---

## 7. UI 样板页（可运行）

```bash
cd /data/projs/anet-drone/.cache/research/g07/sample
npm install                         # 已装好；依赖见 package.json（@base-ui/react 1.8.0、morphicons 1.7.1、lucide 1.48.0、tailwindcss 4.3.3、vite 8）
npx tsc -b && npx vite build        # 类型检查通过；JS 510 KB（gzip 166 KB），CSS 91 KB（gzip 18 KB）
npx vite preview --port 4317 --host 127.0.0.1
node ../verify.mjs 4317             # 生成 ../verify.json 和 ../shots/*.png
```

页面内容：

- 顶栏：logo、motion tier 切换（full/lite/reduced，写 `html[data-motion]`）。
- 浮层区：Dialog（确认起飞）、Sheet（无人机详情）、DropdownMenu（图层，带 Group、CheckboxItem、图标）、Tooltip（聚焦按钮）、Toast（信息 / 告警，limit=3 堆叠）。
- 共享气泡工具栏（5 个视角按钮共用 1 个 Tooltip handle）。
- Tabs（Indicator 胶囊，面板左右切换）。
- Accordion（Layers 放 Switch，Environment 放 Checkbox）。
- StateIcon 三组（Play/Pause、Eye/EyeOff、Lock/LockOpen 在白名单内会 morph）。
- 右侧 HUD：3 张 LfChartCard，包含 LfStat 和 10 Hz mock 遥测驱动的 LfSparkline。

StateIcon 实测：full 档在 400 ms 内出现 5 个不同的中间 `d`（morphicons 弹簧 morph，不使用 WAAPI）；lite 档改为 swap（出现 ghost path，两条 WAAPI 动画，无 blur）；reduced 档直接 `set`。

截图（`/data/projs/anet-drone/.cache/research/g07/shots/`）：`page-{full,lite,reduced}.png`、`dialog-{full,lite,reduced}.png`、`menu-full.png`、`hud-full.png`。

文件清单（可以直接复制为 `apps/web` 的对应文件）：

| 文件 | 作用 |
|---|---|
| `src/index.css` | 导入顺序、`@layer … motion` 层序、ANet Graphite 主题（来自 d04 trial3）、HUD 字阶 `@theme` |
| `src/styles/motion/tokens.css` | 由 `scripts/gen-motion-tokens.py` 生成的 `@theme static` 和 32 组配方变量 |
| `src/styles/motion/base-ui.css` | §2.2 的全部映射 |
| `src/styles/motion/tiers.css` | §3.6 |
| `src/styles/icons.css`、`src/styles/lf.css`、`src/styles/shadcn-tailwind.css` | 图标基础样式、lieflat 角色变量、内联的 `shadcn/tailwind.css`（去掉 shadcn CLI 的运行时依赖） |
| `src/components/icons/{icon,state-icon,lucide-compat}.tsx` | §4 与 d03 §3.3–3.4 |
| `src/components/lf/lf.tsx` | `LfChartCard`、`LfStat`、`LfSparkline`、`Ring`、`lfScheduler` |
| `src/motion/tier.ts` | 档位存储 |
| `src/lib/utils.ts` | `createCn` 扩展（§3.4） |
| `scripts/shadcn-motion-codemod.mjs`、`scripts/shadcn-icons-codemod.mjs`、`scripts/gen-motion-tokens.py` | §2.4、§4 |
| `src/components/ui/{accordion,tabs}.tsx` | G7 补丁（§2.4 第 3、4 条） |

---

## 8. 需要同步修订的既有结论

| 位置 | 原结论 | 修订 |
|---|---|---|
| d02 §4.2 | `radix.css`：6 类浮层改写为 keyframes，`t-modal`/`t-dropdown`/`t-sheet`/`t-tooltip`/`t-overlay` 按 `[data-state]` 选择 | **删除**，改为 §2.2 的 `base-ui.css`，按 `data-slot` 加 starting/ending 属性选择，使用 transition |
| d02 §4.2 | Sonner：覆盖 `[data-sonner-toast]` 的 transition | 改为 Base UI Toast（§2.2 的 22 与 32） |
| d02 §4.2 与 §4.4 | `SlidingTabsList`（MutationObserver 加 ResizeObserver）、`useTooltipTravel` | 分别改为 `Tabs.Indicator` 和 `Tooltip.createHandle` 加 Positioner 的 transform 过渡 |
| d02 §4.2 | Radix Tooltip 的 `delayed-open`/`instant-open` | 改为 Provider `delay`/`timeout` 加 `[data-instant]` |
| d02 §4.5 | reduced 档一律用 0.01ms，理由是保证 `animationend` 触发 | Base UI 部件用 **0s**；0.01ms 只保留给 JS 配方 |
| d02 §4.3 | `@theme { --ease-* … }`，并且在组件里写 `duration-(--duration-fast)` | 改为 `@theme static`；增加 `--transition-duration-*` 别名，得到 `duration-fast` 工具类；加上 `@layer motion` 层序；`cn` 登记自定义名 |
| d04 §3.6 | "Base UI Toast 已内置同一条曲线，只需把时长换成 token" | 还需要替换进场和退场几何（150% 改为 16 px 加 .97 加 blur）、深度系数（.1 改为 .06），并重申 `data-limited` |
| d04 §3.6 | codemod 正则 | 改为 §2.4 第 1 条的 slot 限定版（覆盖 Sheet 的 starting/ending translate 与 transition，不跨越 data-slot） |
| d04 §3.2 与 §3.9 | `lib/utils` 为 `export { cn } from "cn"` | 改为 `createCn(extend…)`，并执行 `from "cn"` 到 `@/lib/utils` 的 codemod |
| d03 §4.3 | 16 个 lucide 名字（new-york-v4） | 换成 §4.1 的 16 个 base-mira 名字；CheckIcon 使用 `pathLength=1` |
| d01 §4.3 与 §3.1.2 | `LfChartCard` 用 `border border-border`、`py-3`/`px-3`、圆角 12 px、`--radius: .75rem`、`text-[13px]` 等任意值 | 改为 §5.1：`size="sm"`、`rounded-xl`，使用 `text-hud-*` 字阶，不加 border |
| 00-index §8 C6 | "Base UI 卸载时是否等待 transition、前置态映射需要验证（G7）" | 已验证：会等待，只等 Popup/Root 自身的动画，映射规则见 §2.1 |
