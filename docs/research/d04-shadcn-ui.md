# d04 研究笔记：shadcn/ui（2026 版）与《UI 组件体系与主题规范》草案

> 研究单元：d04 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §3–§4（浏览器职责）、§16（场景分层）、§28（DroneState）、§34（前端技术栈）、§36–§37（实时通信与刷新频率）、§38–§40（UI、Timeline、Drone Interaction）、§43–§50（MVP 与版本路线）
>
> 仓库快照：`refs/design/ui` @ `984f435`（2026-09-28，"feat(registry): add community registries (#12037)"），124743 stars。CLI 包 `packages/shadcn` 的版本为 **4.21.0**。下文路径都相对仓库根目录。
>
> 相关单元：d01（lieflat-charts，定义了 ANet Graphite 色板、图表与表格规范）、r14（R3F 宿主 + 命令式引擎）、r11/r12（WebGPU 与点云 HUD 指标）。本文的主题 token **直接沿用 d01 §3.2 的 ANet Graphite 色阶**，只补充 shadcn 需要的变量（sidebar-\*、brand-\* 等），不另起一套色板。
>
> 本机实测（产物在 `/data/projs/anet-drone/.cache/research/d04/`）：
> - `trial/`：执行 `shadcn init -t vite -b base -p b1D0dv96` 后再 `add --all`，得到 61 个组件文件，耗时 39 s；`vite build` 通过（JS 423 KB，gzip 后 137 KB）。`tsc -b` 只报 1 个错误，是 `scroll-area.tsx` 里未使用的 `React` import。
> - `trial2/`、`trial3/`：用自定义的 `registry:base` 文件 `anet-base.json` 执行 `init`，同时 `REGISTRY_URL` 指向本地镜像。init、add 11 个组件、`vite build` 全部成功，品牌 token 也自动注册进了 `@theme inline`。
> - `mirror.sh` 和 `mirror/`：base-mira 全部 63 个 registry item、颜色表和 `/init` 载荷的静态镜像，共 676 KB。
> - 环境：Node 22，npm registry 为 `registry.npmmirror.com`，`ui.shadcn.com` 可以访问。实测时安装到的版本是 `@base-ui/react 1.8.0`、`lucide-react 1.48.0`、`cn 0.4.0`、`react-resizable-panels 4.14.1`、`recharts 3.8.0`、`react 19.2`、`vite 8`、`typescript 6`、`tailwindcss 4`。

---

## 0. 结论速览

| 仓库 / 子模块 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **shadcn CLI** `packages/shadcn`（v4.21.0） | 代码分发器：`init/add/apply/preset/migrate/eject/view/search/docs/info/mcp/build/registry` | **adopt**，作为 devDependency。只在搭建和升级时运行，生成的组件源码提交进仓库 | V0.1 | 5/5 |
| **registry `bases/base`**（Base UI 1.x） | 63 个 UI item（其中 61 个有源码文件）、16 个 sidebar block、dashboard-01、hooks 和 lib | **adopt**，组件源码复制进 `apps/web/src/components/ui/`，此后由本项目维护 | V0.1 起 | 5/5 |
| style **`mira`**（Made for compact interfaces） | 8 种视觉风格之一：按钮 `h-7`、字号 `text-xs/relaxed`，适合高密度控制台 | **adopt**；备选是 `nova`（Reduced padding） | V0.1 | 5/5 |
| registry `bases/radix` | Radix 版本，API 用 `asChild` | **reference**（社区 registry 的组件多数基于 Radix，需要移植时参考） | — | 3/5 |
| registry `bases/aria`（React Aria） | 缺少 `menubar`、`navigation-menu`、`toast` | **skip** | — | 2/5 |
| `packages/shadcn/src/tailwind.css` | `data-open/closed/checked/...` 自定义 variant，外加 `scroll-fade`、`shimmer` 工具类 | **adopt**，可以通过 `@import "shadcn/tailwind.css"` 引入，也可以 `eject` 内联 | V0.1 | 5/5 |
| `styles/style-*.css` 加 `create-style-map.ts`、`transform-style-map.ts` | 把作者源码里的 `cn-*` 占位类展开成 Tailwind 类的构建管线 | **reference**；离线第三级方案下 **port**（§3.9） | 仅离线兜底 | 3/5 |
| blocks `sidebar-07/15/16`、`dashboard-01` | 图标折叠侧栏、左右双侧栏、带顶栏的布局、仪表盘 | **port** 布局骨架，页面内容重写 | V0.1 | 4/5 |
| `chart`（Recharts 3.8.0） | shadcn 图表容器 | **skip**：图表以 lieflat 为准，d01 已决定用 SVG 与 CPU canvas | — | 0/5 |
| Chat 原语：`message-scroller/message/bubble/marker/attachment/questionnaire` 与 `@shadcn/react` | 2026 年新增的对话 UI 原语 | **adopt**（V1.0 ANet 协作日志和任务协商），V0.x 不安装 | V1.0 | 4/5 |
| `@shadcn/helpers`（`createChat`） | 用脚本编排 AI SDK 流式对话的 mock | **reference**：用来 mock ANet 协商流程、写 E2E 用例 | V1.0 | 2/5 |
| `skills/shadcn/`（SKILL.md 与 rules） | 官方编码规范：组合方式、表单、图标、base 与 radix 的差异 | **adopt** 为本项目的 UI lint 规则 | V0.1 | 4/5 |
| `templates/vite-app` | Vite 8 + React 19 + TS 6 模板，带 `ThemeProvider` | **port**：`ThemeProvider` 必须改（§6 第 1 条） | V0.1 | 4/5 |

**实现者先读这 12 条（每条都有源码或实测依据）：**

1. **2026 版 shadcn 的基本面已经变了。** 旧的"new-york / default 两种风格加 Radix"已经成为遗留，现在是 **3 种 base（`base` / `radix` / `aria`）乘以 8 种 style（`nova/vega/maia/lyra/mira/luma/sera/rhea`）**，再加 preset code（把设计系统参数压缩成一个 base62 串）。从 4.13.0 起默认 base 是 **Base UI**（CHANGELOG："base-ui is now default"）。4.21.0 起 `lib/utils.ts` 只剩 `export { cn } from "cn"`，由 `cn` 包取代 `clsx` 加 `tailwind-merge`。主题采用 Tailwind v4 的 `@theme inline` 加 OKLCH 格式的 CSS 变量。
2. **本项目的选型定为 `base-mira`，图标库 lucide，字体 Inter 加 JetBrains Mono，圆角 small。** 对应 preset code 是 **`b1D0dv96`**，本机用 `shadcn preset decode` 验证过：style=mira、baseColor=neutral、iconLibrary=lucide、font=inter、radius=small、menuColor=default、menuAccent=subtle。颜色随后整体替换为 ANet Graphite（§3.3）。推荐做法是直接用本文 §3.3 的 `anet-base.json`（类型为 `registry:base`）一步完成 init，已实测可用。
3. **Vite 安装全链路已在本机跑通**（§3.1）。有三个坑：① 模板的 `ThemeProvider` 把**裸 `d` 键**绑定为切换明暗，会和 WASD 飞行控制冲突；② `scroll-area.tsx` 在 `noUnusedLocals` 下编译报错；③ 对 Vite 项目，block 的 `page.tsx` 不会写入，布局要自己在 `App.tsx` 里组装。
4. **离线分三级**（§3.9）。L1：联网装一次，把组件源码提交进仓库，以后不再依赖 registry（shadcn 的设计初衷就是如此）。L2：本地静态镜像加 `REGISTRY_URL=http://127.0.0.1:8765/r`（已实测；需要镜像 `r/colors/neutral.json`，`/init` 保存为无扩展名的文件）。L3：直接 vendoring 作者源码加 style CSS，在运行时用 `.style-mira` 作用域（官网自己的 `apps/v4/app/style-registry.css` 就是这么做的）。npm 依赖需要另外准备离线缓存。
5. **动效用 Base UI 的 `data-starting-style` / `data-ending-style` 对接 transitions.dev。** Base UI 官方推荐 CSS transition（可以中途平滑取消），transitions.dev 的 `.t-*` 前置态、`.is-open`、`.is-closing` 正好一一对应（§3.6）。弹层里 tw-animate-css 的 keyframe 类（`data-open:animate-in zoom-in-95 …`）要用 codemod 去掉，换成 `motion.css` 适配层。
6. **图标：`components.json` 的 `iconLibrary` 保持 `lucide`，morphicons 使用 `lucide` 数据包。** 两者来自同一套 Lucide 图形，版本要对齐（`lucide-react@1.48` 对应 `lucide@1.48`）。应用层统一用 `<AppIcon>`（内部是 MorphIcon），shadcn 组件内部的静态 chevron 和 check 保留 `lucide-react`。严禁 emoji，也严禁把 `U+25B2 U+25CF ↑` 之类 Unicode 字形当图标用。
7. **图表：不安装 shadcn `chart`（它会引入 Recharts）。** 图表外壳用 shadcn `Card`，里面是 d01 的 LfChart（SVG 或 CPU canvas）。表格用 shadcn `Table` 加 lieflat `table.log` 皮肤（d01 §3.9）。Data Table 用 **TanStack Table v9 的新 API**（`tableFeatures()`、`useTable`、`createColumnHelper<typeof features, T>()`，见 `dashboard-01/components/data-table.tsx`）。
8. **布局组合 sidebar-16、sidebar-07、sidebar-15 三个 block，中间用 Resizable v4。** 左侧 `Sidebar collapsible="icon"`，右侧 `Sidebar collapsible="none"`，宽度由自己控制，中间用 `ResizablePanelGroup orientation="vertical"` 分成视口和底部 Dock。两侧栏共用一个 `SidebarProvider`，也共用 `--sidebar-width`（§6 第 6 条）。
9. **UI 层绝不拖累 3D 帧率。** 浮在 canvas 上方的元素一律不用 `backdrop-blur`，需要从 dialog、sheet 遮罩里删掉 `supports-backdrop-filter:backdrop-blur-xs`，menuColor 也不用 translucent。遥测经过 store 节流到 4–10 Hz 再给 React。侧栏折叠时有 200 ms 的 `transition-[left,right,width]`，其间对 canvas resize 做防抖（§3.10）。
10. **Base 与 Radix 的 API 差异是移植社区代码时的主要坑。** 自定义 trigger 用 `render={<Button/>}`，不用 `asChild`；`ToggleGroup` 与 `Accordion` 的值一律是数组；`Select` 要传 `items`；`Slider` 推荐一律传数组 `[x]`，因为 shadcn 包装层在传标量时按 `[min,max]` 渲染两个 thumb（`bases/base/ui/slider.tsx` 第 12–16 行）。`react-resizable-panels` v4 中**数字表示像素，字符串表示百分比**。
11. **Toast 用 Base UI 的 `toast` 组件，不用 sonner。** base 下 sonner 被隐藏（`registry/constants.ts` 的 `COMPONENTS_HIDDEN_FROM_SELECTION`），而且 sonner 依赖 `next-themes`。Base UI 的 `createToastManager()` 返回一个全局 manager，WebSocket 回调、命令 ACK 之类的非 React 代码可以直接调用 `toast.add()`。
12. **红色的使用沿用 d01 的"一处红"规则。** 暗色是默认主题；`--primary` 是近白色，不是红色；红色只通过 `--brand` / `--brand-solid` / `--brand-text` 和 `--destructive` 出现。列表里的选中态不用红，改为 `bg-muted` 加左侧 2 px 前景色竖条。

---

## 1. 仓库概览

| 项 | 内容 |
|---|---|
| 地址 | https://github.com/shadcn-ui/ui（作者 shadcn，现属 Vercel 生态） |
| 快照 | `984f435`，2026-09-28；CLI 4.21.0；`@shadcn/react` 0.3.1；124743 stars。2026 年全年高频发版：CHANGELOG 中 4.7→4.21 连续 minor，包括 GitHub registry、preset、apply、eject、`migrate cn`、React Aria base、Base UI Toast、scroll-fade 与 shimmer 工具类、SOCKS 代理 |
| 形态 | pnpm + turbo monorepo：`apps/v4`（Next 16 官网，也是 registry 源），`packages/shadcn`（CLI），`packages/react`（`@shadcn/react`，无样式原语），`packages/helpers`（`@shadcn/helpers`，AI SDK 对话 mock），`packages/tests`，`templates/*`（10 个脚手架模板），`skills/shadcn`（Agent Skill） |
| 分发模型 | **不是 npm 组件库**：CLI 从 registry（`https://ui.shadcn.com/r/styles/{style}/{name}.json`）拉取 JSON，里面带源码字符串，经 transformer 处理后写进用户项目。运行时依赖只有 `@base-ui/react`、`cn`、`class-variance-authority`、`lucide-react`、`tw-animate-css`，以及个别组件自己的依赖 |
| 许可 | MIT |
| 官网依赖版本（`apps/v4/package.json`） | `@base-ui/react 1.6.0`、`react 19.2.3`、`next 16.3.3`、`tailwindcss ^4.3`、`recharts 3.8.0`、`react-resizable-panels ^4`、`@tanstack/react-table ^9`、`cmdk ^1.1.1`、`sonner ^2`、`tw-animate-css ^1.4`、`cn ^0.2.2` |
| 实测安装版本（2026-09-28 的 npm latest） | `@base-ui/react 1.8.0`、`lucide-react 1.48.0`、`cn 0.4.0`、`react-day-picker 10.0.1`、`react-resizable-panels 4.14.1`、`date-fns 4.4`、`vite 8`、`typescript ~6` |

---

## 2. 源码结构与关键模块

### 2.1 目录结构（只列与本项目相关的部分）

```text
ui/
├── packages/shadcn/src/
│   ├── index.ts                     # commander 注册 14 个命令
│   ├── commands/{init,add,apply,diff,docs,view,search,migrate,eject,info,build,mcp,preset,registry/*}.ts
│   ├── preset/{preset.ts,defaults.ts,presets.ts,resolve.ts}   # preset 编解码、默认 8 套、init URL 拼装
│   ├── registry/{constants,schema,resolver,fetcher,address,github*,proxy,api,loader,...}.ts
│   ├── templates/{vite,next,start,react-router,astro,laravel,monorepo,create-template}.ts
│   ├── utils/updaters/{update-css-vars,update-css,update-fonts,update-dependencies,update-files}.ts
│   ├── utils/transformers/{transform-icons,transform-render,transform-aschild,transform-menu,transform-rtl,transform-rsc,transform-css-vars,transform-font,...}.ts
│   ├── styles/{create-style-map,transform-style-map,transform}.ts
│   ├── migrations/{migrate-cn,migrate-icons,migrate-base-color,migrate-radix,migrate-rtl}.ts
│   ├── icons/libraries.ts           # lucide/tabler/hugeicons/phosphor/remixicon 的导入与用法模板
│   └── tailwind.css                 # 以 "shadcn/tailwind.css" 导出
├── apps/v4/
│   ├── registry/bases/{base,radix,aria}/{ui,blocks,hooks,lib,examples,components,internal}/  # 手写源码
│   ├── registry/styles/style-{nova,vega,maia,lyra,mira,luma,sera,rhea}.css                   # style token（cn-* → @apply）
│   ├── registry/{bases.ts,themes.ts,base-colors.ts,styles.tsx,fonts.ts,config.ts}
│   ├── app/(app)/(create)/init/route.ts   # /init?base=&style=&... → 动态生成 registry:base JSON
│   ├── app/style-registry.css             # 运行时 @import 各 style CSS 到 layer(base)
│   └── public/r/                          # 仓库只提交了遗留的 styles/default 与 styles/new-york，base-* 需要构建生成
├── packages/react/src/{message-scroller,questionnaire,use-render}/
├── packages/helpers/                      # createChat（AI SDK UI 流 mock）
├── templates/vite-app/                    # Vite 8 + React 19 + TS 6 + @tailwindcss/vite
└── skills/shadcn/{SKILL.md,cli.md,customization.md,registry.md,rules/*.md}
```

### 2.2 CLI 关键流程

**`init`**（`packages/shadcn/src/commands/init.ts`）

- `initOptionsSchema` 的主要选项：`--template (next|start|vite|react-router|laravel|astro)`、`--base (base|radix|aria)`、`--preset [name|code|url]`、`--monorepo`、`--css-variables`、`--rtl`、`--pointer`、`--reinstall`、`-y`、`-d` 和 `-n`。
- 执行流程：
  1. 解析 preset。名字走 `DEFAULT_PRESETS`；code 走 `decodePreset`；URL 直接使用。三种形式最终都通过 `resolveInitUrl()` 拼成 `${SHADCN_URL}/init?base=&style=&baseColor=&theme=&iconLibrary=&font=&rtl=&menuAccent=&menuColor=&radius=[&chartColor][&fontHeading]&track=1`。
  2. `resolveRegistryBaseConfig()` 拉取这个 URL，得到一个 `registry:base` item，其 `config` 会被 deepmerge 进 `components.json`。如果 `extends` 为 `"none"`，就不再安装默认的 `index` 样式。
  3. `runInit()`：执行 `preFlightInit`；空目录时调用 `createProject` → `templates[x].scaffold`。有 `SHADCN_TEMPLATE_DIR` 时从本地复制，否则用 `git clone --depth 1 --filter=blob:none --sparse` 从 GitHub 只拉 `templates/<dir>`（`templates/create-template.ts` 的 `defaultScaffold`）。
  4. 写入 `components.json`，然后 `addComponents(["index"?, initUrl, ...components, "button"(新模板)])`。
- `confirmBaseSwitch()`：重新 init 时如果切换了 base，会提示 ui 目录外依赖旧原语的代码需要手动改。

**`add`**（`commands/add.ts`）

- 选项：`-y -o(--overwrite) -a(--all) -p(--path) --dry-run --diff [path] --view [path]`。
- `promptForRegistryComponents()` 在 `--all` 时从 `index.json` 取全部 item，然后过滤掉 `DEPRECATED_COMPONENTS`（toast 只在 base 下可用、toaster 已废弃）和 `COMPONENTS_HIDDEN_FROM_SELECTION`（sonner 在 base 下隐藏）。
- 没有 `components.json` 时会自动走一次 init。

**registry 解析**（`registry/resolver.ts` 的 `fetchRegistryItems()`）按以下优先级分派：

1. `github:` 或 `owner/repo` 形式 → `fetchGitHubRegistryItem`（4.19 起支持用 GH_TOKEN 访问私有仓库）；
2. `isLocalFile(item)`，即 `path.endsWith(".json") && !isUrl` → `fetchRegistryLocal()`，读本地 JSON 并用 zod 校验；
3. URL → `fetchRegistry`；
4. `@namespace/name` → 按 `components.json` 或 `package.json` 的 `registries` 模板替换；
5. 裸名称 → `styles/${config.style}/${name}.json`，相对 `REGISTRY_URL`。

`REGISTRY_URL` 默认是 `https://ui.shadcn.com/r`，可以用环境变量覆盖（`registry/constants.ts`）；`SHADCN_URL` 由它去掉 `/r` 得到，`/init` 也从这里取。`registry/proxy.ts` 支持 `HTTPS_PROXY/NO_PROXY`（undici `EnvHttpProxyAgent`）和 `ALL_PROXY=socks5://`。

**preset 编码**（`preset/preset.ts`）：把各参数的枚举下标按位打包成一个整数（不超过 53 bit），再做 base62 编码，前面加版本字符。v2 前缀为 `"b"`，共 51 bit。

| 字段（按顺序） | 位宽 | 取值（下标 0 为默认值） |
|---|---|---|
| menuColor | 3 | default, inverted, default-translucent, inverted-translucent |
| menuAccent | 3 | subtle, bold |
| radius | 4 | default, none, small, medium, large |
| font | 6 | inter, noto-sans, …, jetbrains-mono(9), geist(10), geist-mono(11), …（共 26 种） |
| iconLibrary | 6 | lucide, hugeicons, tabler, phosphor, remixicon |
| theme | 6 | neutral, stone, zinc, gray, amber, …, red(15), …（共 25 种） |
| baseColor | 6 | neutral, stone, zinc, gray, mauve, olive, mist, taupe |
| style | 6 | nova, vega, maia, lyra, mira(4), luma, sera, rhea |
| chartColor | 6 | 与 theme 相同 |
| fontHeading | 5 | inherit, 加上各字体 |

```ts
// encodePreset 的核心（JS 位运算会截断到 32 bit，因此用乘法）
let bits = 0, offset = 0
for (const f of PRESET_FIELDS_V2) { bits += indexOf(f.values, cfg[f.key]) * 2 ** offset; offset += f.bits }
return "b" + toBase62(bits)
// 本项目：radius=small(2)→2·2^6，style=mira(4)→4·2^34，其余字段为 0
//        → 68719476864 → "b1D0dv96"（已用 `shadcn preset decode b1D0dv96` 验证）
```

**CSS 变量写入**（`utils/updaters/update-css-vars.ts`）

- 把 `cssVars.light` 写到 `:root`，`cssVars.dark` 写到 `.dark`，`cssVars.theme` 写到 `@theme inline`。
- `updateThemePlugin()` 对每个变量调用 `isColorValue()` 判断：是颜色就生成 `--color-<name>: var(--<name>)`，否则生成 `--<name>`。**所以自定义的 `brand` 之类颜色变量会被自动注册成 `bg-brand` 这样的工具类**（trial3 实测，出现了 `--color-brand-text`）。
- 圆角的派生公式：`--radius-sm = r×0.6`，`md = r×0.8`，`lg = r`，`xl = r×1.4`，`2xl = r×1.8`，`3xl = r×2.2`，`4xl = r×2.6`。
- 同时插入 `@custom-variant dark (&:is(.dark *));`。

**字体**（`utils/updaters/update-fonts.ts` 的 `massageTreeForFonts()`）：非 Next 框架会安装 `@fontsource-variable/<font>` 并写入 `@import`，所以 Vite 下字体可以离线使用，不依赖 Google Fonts CDN。

**图标 transformer**（`utils/transformers/transform-icons.ts`）：作者源码中写的是 `<IconPlaceholder lucide="CheckIcon" tabler="IconCheck" hugeicons=… phosphor=… remixicon=…/>`。安装时按 `config.iconLibrary` 选取对应属性，替换成 `<CheckIcon/>`（hugeicons 替换成 `<HugeiconsIcon icon={…}/>`），并补上 import。各图标库的导入与用法模板在 `icons/libraries.ts`。

**其他命令**：
- `apply <preset>`：对已有项目换 preset，支持 `--only theme,font`。
- `migrate {cn|icons|base-color|radix|rtl}`：代码迁移。
- `eject`：把 `shadcn/tailwind.css` 内联进项目，并移除 `shadcn` 依赖。
- `view/search/docs`：浏览 registry。
- `info --json`：输出项目上下文。
- `mcp`：MCP server，供 Agent 调用 registry。
- `build` / `registry validate`：构建或校验自建 registry。

### 2.3 registry 的组织：authored base、style CSS 与生成

- **作者源码不带样式细节。** 以 `apps/v4/registry/bases/base/ui/button.tsx` 为例：`cva("cn-button group/button inline-flex …", {variants:{variant:{default:"cn-button-variant-default", …}}})`。视觉细节全部放在 `styles/style-mira.css` 这类 style 文件里：

  ```css
  .style-mira { .cn-button-size-default { @apply h-7 gap-1 px-2 text-xs/relaxed …; } … }
  ```

  每个 style 文件约 1740 行，其中 nova 有 422 个 `cn-*` 规则。
- **构建时展开。** `packages/shadcn/src/styles/create-style-map.ts` 用 postcss 与 postcss-selector-parser 把 `.cn-x { @apply … }` 解析成 `Record<"cn-x", "tailwind classes">`（以选择器中最后一个 `cn-*` 类为主体）。`transform-style-map.ts` 再用 ts-morph 遍历 `cva()` 调用的 base 与 variants、JSX `className`、`cn()` 调用和 `mergeProps`，把 `cn-*` 替换成展开后的类并用 `twMerge` 去重。`ALLOWLIST` 中的 `cn-menu-target`、`cn-menu-translucent`、`cn-logical-sides`、`cn-rtl-flip`、`cn-font-heading` 保留到安装时再处理。
- **官网运行时也可以不展开。** `apps/v4/app/style-registry.css` 用 `@import "../registry/styles/style-mira.css" layer(base);`，在容器上加 `.style-mira` 就能直接渲染未展开的作者源码。这是离线 L3 方案的依据（§3.9）。
- **`registry:base` 由服务端动态生成。** `apps/v4/registry/config.ts` 的 `buildRegistryBase(config)` 生成 `{name:"${base}-${style}", type:"registry:base", extends:"none", config:{style, iconLibrary, rtl, menuColor, menuAccent, tailwind:{baseColor}}, dependencies:[shadcn, class-variance-authority, cn, tw-animate-css, <base 依赖>, <图标包>], registryDependencies:["utils","font-<font>"], cssVars, css:{"@import tw-animate-css", "@import shadcn/tailwind.css", "@layer base":{…}}}`。本项目的 `anet-base.json` 就是按这个结构手写的。
- **base 之间的差异。** `apps/v4/registry/bases.ts` 中 `base` 依赖 `@base-ui/react`，`radix` 依赖 `radix-ui`，`aria` 依赖 `react-aria-components`。三个 base 的 ui 数量分别是 base 63、radix 62（没有 toast）、aria 60（没有 menubar、navigation-menu、toast）。

### 2.4 `shadcn/tailwind.css`（`packages/shadcn/src/tailwind.css`）

- `@custom-variant data-open/closed/checked/unchecked/selected/disabled/active/horizontal/vertical`：同时兼容 Radix 的 `data-state="open"` 和 Base UI 的 `data-open` 属性，两套 base 因此可以共用 style CSS。
- `accordion-down/up` keyframes：读取 `--radix-accordion-content-height` 或 `--accordion-panel-height`。
- `scroll-fade(-x/-y/-t/-b/-s/-e)`：用 `animation-timeline: scroll()` 驱动的滚动边缘渐隐遮罩；不支持时退化为静态遮罩。适合右侧无人机列表和事件日志的 ScrollArea。
- `shimmer`：文字流光，用于"连接中 / 重建中"等状态文本，带 `prefers-reduced-motion` 降级。它和 transitions.dev 的 shimmer-text 是同一类效果，二选一即可。**本项目采用 transitions.dev 版本**，以保证动效来源统一。

### 2.5 2026 年新增的组件与包

- **Field**：`FieldSet / FieldLegend / FieldGroup / Field / FieldLabel / FieldContent / FieldTitle / FieldDescription / FieldError / FieldSeparator`，取代旧 `form`。base 下的 `form` 已经没有文件，校验用 `data-invalid` 加 `aria-invalid`。
- **InputGroup**（前后缀、单位、内嵌按钮）、**ButtonGroup**、**Item**（列表行：Media、Content、Title、Description、Actions）、**Empty**、**Kbd / KbdGroup**、**Spinner**、**NativeSelect**、**Combobox**（Base UI 原生）、**Direction**。
- **Chat 原语**：`MessageScroller`（依赖 `@shadcn/react/message-scroller`，自带流式跟随、锚定和"跳到最新"）、`Message`、`Bubble`、`Attachment`、`Marker`（系统提示与分隔线）、`Questionnaire`（结构化提问）。
- **`@shadcn/helpers` 的 `createChat()`**：链式脚本 `.user().assistant(({writer}) => writer.tool(...).output(...))`，支持 `needsApproval` 这类人在回路的审批流，用来 mock AI SDK 流。

### 2.6 `skills/shadcn`（可以直接作为本项目的 UI 编码规范）

- **styling**：`className` 只管布局，不改组件颜色；用 `gap-*`，不用 `space-*`；等宽高用 `size-*`；不手写 `dark:` 颜色；不给弹层手动设 `z-index`。
- **forms**：表单用 `FieldGroup + Field`；`InputGroup` 内部用 `InputGroupInput`；2–7 个选项用 `ToggleGroup`。
- **composition**：Item 必须放在对应的 Group 里；`Dialog`、`Sheet`、`Drawer` 必须有 Title（可以用 `sr-only`）；Button 没有 `isLoading`，用 `Spinner + data-icon + disabled` 组合。
- **icons**：图标用 `data-icon="inline-start|inline-end"`，组件内的图标不加尺寸类，以组件对象传入。
- **base-vs-radix.md**：`render` 与 `asChild`、`nativeButton={false}`、Select 的 `items`、ToggleGroup 的 `multiple`、Slider 的标量、Accordion 的数组。

---

## 3. 可复用算法与实现（实现者只看这一节即可动手）

### 3.1 Vite 项目安装步骤（在线路径，本机已跑通）

```bash
# 0) 在 monorepo 的 apps/ 下新建 web（也可以在已有 Vite 项目中直接 init）
cd /data/projs/anet-drone/apps
# 方式 A（推荐）：用本项目的 registry:base 一步生成（主题、字体、依赖一次到位）
npx shadcn@latest init ../design/anet-base.json -t vite -n web --no-monorepo -y
# 方式 B：先用 preset 初始化，再手动替换 index.css 里的变量
npx shadcn@latest init -t vite -b base -p b1D0dv96 -n web --no-monorepo -y

cd web
# 1) MVP 组件（不要用 --all：会装上 recharts/embla/react-day-picker/input-otp/@shadcn/react 等 MVP 用不到的依赖）
npx shadcn@latest add sidebar resizable card separator scroll-area collapsible accordion tabs \
  sheet dialog alert-dialog popover hover-card tooltip menubar dropdown-menu context-menu breadcrumb \
  button button-group input input-group textarea field label checkbox radio-group switch slider \
  select native-select combobox command toggle toggle-group table badge kbd item empty progress \
  skeleton spinner alert toast avatar -y
# 2) 修正已知编译错误：scroll-area.tsx 第 1 行的未使用 import（TS6133）
sed -i '1{/^import \* as React from "react"$/d}' src/components/ui/scroll-area.tsx
# 3) 可选：把 shadcn/tailwind.css 内联进来，并移除 shadcn 依赖（它会带来约 45 MB 的 ts-morph 和 typescript）
npx shadcn@latest eject -y
```

模板本身已经配置好 `vite.config.ts`（`@tailwindcss/vite`、`@` → `src`）、`tsconfig.json` 与 `tsconfig.app.json` 的 `paths`、`index.css`（`@import "tailwindcss"`）。**已有 Vite 项目**要手动补这几项：`npm i tailwindcss @tailwindcss/vite`，在 `vite.config.ts` 里加 `plugins:[react(), tailwindcss()]` 和 `resolve.alias['@']`，在两个 tsconfig 里加 `"paths":{"@/*":["./src/*"]}`，然后执行 `npx shadcn@latest init`。

生成的 `components.json`（trial3 实测）：

```json
{ "$schema":"https://ui.shadcn.com/schema.json", "style":"base-mira", "rsc":false, "tsx":true,
  "tailwind":{"config":"","css":"src/index.css","baseColor":"neutral","cssVariables":true,"prefix":""},
  "iconLibrary":"lucide", "rtl":false,
  "aliases":{"components":"@/components","utils":"@/lib/utils","ui":"@/components/ui","lib":"@/lib","hooks":"@/hooks"},
  "menuColor":"default", "menuAccent":"subtle", "registries":{} }
```

### 3.2 base、style 与 base color 的选择依据

| 维度 | 选择 | 理由 |
|---|---|---|
| base | **Base UI**（`base`） | 2026 年起为默认；官方推荐用 CSS transition（`data-starting-style` / `data-ending-style`）做动效，和 transitions.dev 同构；Toast manager 可以在 React 外调用；Popover 和 PreviewCard 的 Positioner 支持 `anchor: VirtualElement`，可以锚定到 3D 物体投影出的屏幕坐标；Select 支持对象值和多选；Slider 支持标量；Drawer 原生实现，不依赖 vaul |
| style | **mira**（紧凑） | 按钮 `h-7`、`text-xs/relaxed`、Card 间距 `--spacing(4)`（sm 为 3）。数字沙盘里 3D 视口应占最大面积，侧栏信息密度要高。备选 **nova**（按钮 h-8、text-sm）用于分析页；**lyra**（直角、适合等宽字体）风格太硬，不采用 |
| baseColor | neutral（占位） | 所有颜色最终由 ANet Graphite 覆盖（§3.3）。baseColor 只影响 CLI 的 `colors/neutral.json`，因此离线镜像里必须有这个文件 |
| iconLibrary | lucide | morphicons 以 Lucide 数据为一等公民（`import { Menu, X } from "lucide"`），同源才能保证静态图标与 morph 图标形状一致 |
| font | Inter Variable 加 JetBrains Mono Variable | Inter 与 lieflat 的 `FONT.family` 一致；数字使用 `tabular-nums`；坐标、ID、日志用等宽字体。中文回退为 `PingFang SC / Microsoft YaHei / Noto Sans SC` |
| radius | small（`--radius: .45rem`） | 控件偏硬朗，突出工程感。lieflat 图卡的圆角由 `LfChartCard` 单独覆盖：HUD 为 12–16 px，报告为 24 px。d01 草案里写的是 `--radius: .75rem`，**建议统一改为 .45rem**，图卡的大圆角通过 className 实现 |
| menuColor / menuAccent | default / subtle | translucent 会给菜单加 `backdrop-blur-2xl`（`.cn-menu-translucent`），在 3D 场景上方代价很高，不采用 |

### 3.3 主题定制：ANet Graphite 映射到 shadcn 变量（暗色为默认）

规则：色值**完全沿用 d01 §3.2**（色相约 262° 的冷调科技灰阶，加上色相 29° 的品牌红阶），这里只写 shadcn 变量的取值，并补上 d01 没有定义的 `sidebar-*`、`brand-*` 和视口相关变量。对比度数据来自本机计算（WCAG 2.x）。

| 变量 | 暗色 `.dark`（默认） | 浅色 `:root`（分析页和报告导出） | 说明与对比度 |
|---|---|---|---|
| `--background` | `#0A0B0D` oklch(0.149 0.005 264.5) g950 | `#F2F3F5` oklch(0.964 0.003 264.5) g50 | 应用底色，也就是"黑" |
| `--foreground` | `#F2F3F5` g50 | `#111214` oklch(0.182 0.004 264.5) g900 | 暗色下 16.9:1 |
| `--card` / `-foreground` | `#111214` g900 / g50 | `#F2F3F5` / g900 | 面板、HUD 卡，lieflat 的 BG |
| `--popover` / `-foreground` | `#16181B` oklch(0.208 0.007 258.4) g850 / g50 | `#FBFBFC` / g900 | 菜单、弹层 |
| `--primary` / `-foreground` | `#F2F3F5` / `#111214` | `#111214` / `#F2F3F5` | **主操作是白（或黑），不是红** |
| `--secondary` / `--muted` / `--accent` | `#1D1F23` oklch(0.239 0.008 264.4) g800 | `#E4E6E9` oklch(0.924 0.005 258.3) g100 | hover 底色、选中底色 |
| `--muted-foreground` | `#A7ABB3` oklch(0.740 0.012 264.5) g300 | `#5C616A` oklch(0.491 0.016 262.3) g500 | 暗色对 g900 为 8.1:1，浅色对 g50 为 5.6:1 |
| `--destructive` | `#FF5242` oklch(0.677 0.212 29.1) r400 | `#D12A20` oklch(0.559 0.203 29.0) r600 | 暗色对 g900 为 5.8:1 |
| `--border` | `oklch(1 0 0 / 8%)` | `oklch(0.149 0.005 264.5 / 12%)` | 发丝分隔线 |
| `--input` | `oklch(1 0 0 / 12%)` | `oklch(0.149 0.005 264.5 / 16%)` | |
| `--ring` | `#81868F` oklch(0.619 0.015 262.4) g400 | 同左 | 焦点环保持中性，红色不作为焦点色 |
| `--chart-1…5` | DATA g50、g300、g400、g500、**HERO #E93024** | g900、g500、g400、g300、HERO | 与 d01 相同；`--chart-5` 是唯一的红 |
| `--sidebar` / `-foreground` | `#111214` / `#CACDD3` g200 | `#F2F3F5` / g900 | 侧栏与卡片同色，靠 1 px 分隔线区分 |
| `--sidebar-primary` / `-foreground` | g50 / g900 | g900 / g50 | TeamSwitcher 的 logo 底块等。**不用红**，因为导航选中不用红 |
| `--sidebar-accent` / `-foreground` | g800 / g50 | g100 / g900 | 菜单 hover 与 active |
| `--sidebar-border` / `--sidebar-ring` | `oklch(1 0 0 / 8%)` / g400 | 12% 黑 / g400 | |
| **`--brand`**（新增） | `#E93024` oklch(0.607 0.221 29.2) | 同左 | logo、选中无人机、LIVE 点、数据主角。**仅用于图形标记**：对 g900 为 4.38:1，不够用于小字 |
| **`--brand-foreground`**（新增） | `#FFFFFF` | 同左 | |
| **`--brand-solid`**（新增） | `#D12A20` r600 | 同左 | 唯一的品牌 CTA 按钮底色（例如"开始仿真"），白字对比 5.17:1 |
| **`--brand-text`**（新增） | `#FF5242` r400（5.83:1） | `#B4241B` r700（5.89:1） | 红色文字，即 lieflat 的 HERO_TEXT |
| `--radius` | `.45rem` | 同左 | |
| 视口清屏色（新增，不进入 shadcn） | `--viewport: #060708` oklch(0.128 0.003 245.8) | `#0A0B0D` | Three.js 的 `renderer.setClearColor`，启动时读取 computed style。红色 `#E93024` 在它上面的对比度是 4.72:1 |
| HUD 浮层（新增） | `--hud: oklch(0.182 0.004 264.5 / 88%)`，加 1 px `--border` | — | **不加 blur**；alpha 至少 0.85，保证文字对比 |

**状态语义**：沿用 d01 规则，不引入琥珀色或绿色。critical 为 `bg-destructive` 实心加图标加文字；warning 为红色描边空心；nominal 用前景色；stale 或 offline 用 g500 加虚线再加时长。Badge 的映射如下：`variant="destructive"` 表示 critical；`variant="outline"` 加 `border-destructive text-brand-text` 表示 warning；`variant="secondary"` 表示 nominal；`variant="ghost"` 加 `text-muted-foreground` 表示 offline。

**可复现的 init 载荷 `design/anet-base.json`**。完整文件见 `.cache/research/d04/anet-base.json`，已用它完成 init 加 build 验证。结构摘要：

```json
{ "$schema":"https://ui.shadcn.com/schema/registry-item.json",
  "name":"anet-base", "type":"registry:base", "extends":"none",
  "config":{"style":"base-mira","iconLibrary":"lucide","rtl":false,"menuColor":"default","menuAccent":"subtle","tailwind":{"baseColor":"neutral"}},
  "dependencies":["shadcn","class-variance-authority","cn","tw-animate-css","@base-ui/react","lucide-react",
                  "@fontsource-variable/inter","@fontsource-variable/jetbrains-mono"],
  "registryDependencies":["utils"],          // 故意不写 "font-inter"：它要访问 registry，改为直接依赖 fontsource
  "cssVars":{
    "theme":{"--font-sans":"'Inter Variable', 'PingFang SC', 'Microsoft YaHei', 'Noto Sans SC', sans-serif",
             "--font-mono":"'JetBrains Mono Variable', ui-monospace, monospace","--font-heading":"var(--font-sans)"},
    "light":{ "background":"oklch(0.964 0.003 264.5)", "brand":"oklch(0.607 0.221 29.2)", "...":"见上表" },
    "dark": { "background":"oklch(0.149 0.005 264.5)", "brand-text":"oklch(0.677 0.212 29.1)", "...":"见上表" } },
  "css":{"@import \"tw-animate-css\"":{},"@import \"shadcn/tailwind.css\"":{},
         "@import \"@fontsource-variable/inter\"":{},"@import \"@fontsource-variable/jetbrains-mono\"":{},
         "@layer base":{"*":{"@apply border-border outline-ring/50":{}},
                        "body":{"@apply bg-background text-foreground font-sans antialiased":{}}}} }
```

init 之后，在 `src/index.css` 末尾手动追加以下内容（CLI 不负责这些）：

```css
@source not "../public/worlds";            /* 点云瓦片目录若未被 .gitignore，就排除，避免 Tailwind 扫描大文件 */
@import "./styles/motion.css";             /* §3.6 transitions.dev 适配层 */
@import "./styles/lf.css";                 /* d01 的 --lf-* 角色变量与 .lf-* 标记类 */
:root { --viewport:#0A0B0D; }
.dark { --viewport:#060708; --hud:oklch(0.182 0.004 264.5 / 88%); }
@layer base {
  html { color-scheme: dark; }             /* 默认暗色，滚动条和表单控件也跟随暗色 */
  .tabular, [data-numeric] { font-variant-numeric: tabular-nums lining-nums; }
}
```

`index.html` 里写死 `<html class="dark">`；`ThemeProvider` 的 `defaultTheme` 设为 `"dark"`（§6 第 1 条）。

### 3.4 全部组件清单与本项目的用途（apps/v4 registry，base 共 63 项）

| 组件 | 适用场景（官方） | 本项目用途 | 版本 | MVP 安装 |
|---|---|---|---|---|
| **sidebar**（与 sheet、tooltip、skeleton、input、separator、use-mobile 一起安装） | 应用导航侧栏：`collapsible offcanvas/icon/none`、`variant sidebar/floating/inset`、`side left/right`，⌘/Ctrl+B 切换，状态写入 cookie | 左侧 World/Layers/Environment 栏（icon 折叠）；右侧 Drones 栏（`collapsible="none"`） | V0.1 | 是 |
| **resizable**（react-resizable-panels v4：Group/Panel/Separator） | 可拖拽分栏 | 视口与底部 Dock 的上下分割；FPV 画中画或并排分割；任务编辑器 | V0.1 | 是 |
| **card** | 内容容器（Header/Title/Description/Action/Content/Footer） | HUD 卡、LfChartCard 外壳、无人机详情卡 | V0.1 | 是 |
| **tabs**（variant default/line） | 分页 | 右栏无人机详情：遥测、任务、传感器、Agent；底部 Dock：时间线、图表、事件、日志 | V0.1 | 是 |
| **scroll-area** | 自定义滚动条 | 无人机列表、事件日志、图层列表，可叠加 `scroll-fade-y` | V0.1 | 是（需修 TS6133） |
| **collapsible / accordion** | 折叠 | 左栏分组（Wind、Rain、Fog…）；传感器参数分组 | V0.1 / V0.3 | 是 |
| **separator** | 分隔 | 全局 | V0.1 | 是 |
| **sheet** | 侧拉面板 | 任务编辑器（右侧宽 Sheet）；设置；图层详情 | V0.2 | 是 |
| **dialog** | 模态 | 世界导入、新建场景、快捷键帮助、关于 | V0.1 | 是 |
| **alert-dialog** | 确认 | 危险指令：降落、返航、急停、删除任务、覆盖世界 | V0.2 | 是 |
| **drawer**（Base UI 原生） | 底部抽屉，适合移动端 | 移动端或平板的简化控制；桌面不用 | V1.0 | 否 |
| **popover** | 浮层 | 性能 HUD 详情、风向选择、坐标拾取结果（锚定到 VirtualElement） | V0.1 | 是 |
| **hover-card** | 悬停卡 | 连接状态详情（延迟、消息率、丢包）；列表中悬停无人机时显示摘要 | V0.2 | 是 |
| **tooltip**（Provider 默认 `delay=0`） | 提示 | 所有图标按钮，内容内嵌 `Kbd` 显示快捷键 | V0.1 | 是 |
| **menubar** | 桌面应用菜单栏 | 顶栏 World / View / Simulation / Mission / Tools / Help | V0.1 | 是 |
| **dropdown-menu** | 下拉菜单 | 无人机行的"更多"菜单、用户菜单、导出菜单 | V0.1 | 是 |
| **context-menu** | 右键菜单 | 3D 视口右键（飞到此处、加航点、设为 Home、量测、复制坐标）；列表行右键 | V0.2 | 是（注意与相机控制冲突，§6） |
| **command**（cmdk，放在 Dialog 里） | 命令面板 | Ctrl+K：跳到无人机、切换图层、切换世界、执行场景脚本、切换相机 | V0.1 | 是 |
| **navigation-menu** | 网站导航 | 不需要 | — | 否 |
| **breadcrumb** | 面包屑 | 顶栏"World › Scene › Drone" | V0.1 | 是 |
| **pagination** | 分页 | 用虚拟滚动代替 | — | 否 |
| **button / button-group** | 按钮、按钮组 | 飞行指令组（起飞、悬停、降落、返航）；视图工具组 | V0.1 | 是 |
| **toggle / toggle-group** | 2–7 选 1 或多选 | 相机模式（Orbit、Follow、FPV、Bird、Free）；倍速 ×1/×2/×5/×10；天气预设；图层渲染模式（RGB、Height、Intensity、Classification） | V0.1 | 是 |
| **slider** | 连续值 | 风速、风向、雨强、雾浓度、云量、点预算、时间轴 seek、图层透明度 | V0.1 | 是 |
| **input / input-group / textarea** | 输入 | 数值带单位（`InputGroupAddon` 写 "m/s"、"m"、"°"）；坐标输入；搜索 | V0.1 | 是 |
| **field / label** | 表单布局 | 环境参数、任务参数、设置 | V0.1 | 是 |
| **checkbox / radio-group / switch** | 选择 | 图层开关用 Switch；多选导出用 Checkbox；坐标系选择用 RadioGroup | V0.1 | 是 |
| **select / native-select / combobox** | 选择 | 航点动作类型用 Select；世界切换（带搜索）用 Combobox；表格内的小选择器用 NativeSelect | V0.1 | 是 |
| **input-otp** | 验证码 | 不需要 | — | 否 |
| **calendar** | 日期选择 | 回放日期选择、任务排期（react-day-picker 10） | V0.6 | 否 |
| **table** | 表格 | 遥测数值表、航点表、多机总览、点云节点统计（套用 lieflat 皮肤） | V0.1 | 是 |
| **badge** | 标签 | 飞行模式（OFFBOARD、AUTO.MISSION）、状态、LOD 等级、SIM 与 REAL 标记 | V0.1 | 是 |
| **kbd** | 快捷键 | Tooltip、命令面板、帮助面板 | V0.1 | 是 |
| **item** | 列表行 | 无人机列表行（Media 放机型图标，Content 放名称和状态，Actions 放菜单）；图层行；世界列表 | V0.1 | 是 |
| **empty** | 空状态 | 未加载世界、无无人机、无任务、无遥测 | V0.1 | 是 |
| **progress** | 进度 | 电量、点预算占用、瓦片加载、任务完成度 | V0.1 | 是 |
| **skeleton / spinner** | 加载 | 世界加载时的骨架屏；按钮的等待态 | V0.1 | 是 |
| **alert** | 提示条 | 断线横幅、仿真器未连接、WebGPU 不可用已降级到 WebGL2 | V0.1 | 是 |
| **toast**（Base UI） | 消息 | 指令 ACK、告警、导入完成 | V0.1 | 是 |
| **sonner** | 消息（Radix 与 Aria 用） | base 下隐藏 | — | 否 |
| **avatar** | 头像 | V1.0 中 ANet agent 与操作员；V0.x 不用 | V1.0 | 可选 |
| **aspect-ratio** | 固定比例 | FPV 或吊舱视频窗 16:9 | V0.5 | 否 |
| **carousel** | 轮播 | 不需要 | — | 否 |
| **chart**（recharts@3.8.0） | 图表 | **不装**，图表由 d01 的 LfChart 实现 | — | 否 |
| **form** | 旧版表单 | base 下已无文件，用 Field | — | 否 |
| **direction** | RTL | 不需要 | — | 否 |
| **message-scroller / message / bubble / marker / attachment / questionnaire** | Chat 与 Agent UI | V1.0 ANet：协作日志（Marker 作为系统事件分隔）、Agent 间任务协商（Message 加 Bubble）、传感器截图附件（Attachment）、操作员审批（Questionnaire） | V1.0 | 否 |

**blocks**：`dashboard-01`（侧栏、图表、Data Table）、`sidebar-01…16`、`login-01…05`、`signup-01…05`。本项目只参考 **sidebar-07**（icon 折叠加 TeamSwitcher）、**sidebar-15**（左右双栏，右栏 `collapsible="none"`）和 **sidebar-16**（`--header-height` 顶栏，侧栏用 `top-(--header-height) h-[calc(100svh-var(--header-height))]!` 让出顶栏）。

### 3.5 UI 区域到 shadcn 组件的映射（交付物 1）

```text
┌──────────────────────── AppHeader (h=44px, sidebar-16 模式) ────────────────────────────────┐
│ Logo│Menubar(World View Sim Mission Tools Help)│Breadcrumb│   Badge(SIM 14:32:07 ×2) Badge(WS 38ms) Kbd(Ctrl K) ThemeBtn │
├──────────┬───────────────────────────────────────────────────────────────┬──────────────────┤
│ Sidebar  │ SidebarInset                                                  │ Sidebar(right,   │
│ (left,   │ ┌ ResizablePanelGroup(vertical) ──────────────────────────────┐│ collapsible=none)│
│ icon折叠) │ │ Panel "viewport" (canvas + HUD overlays)                    ││ DRONES           │
│ WORLD    │ │  ToggleGroup(camera) ButtonGroup(view)   Card(HUD: FPS/pts)  ││ ScrollArea       │
│ LAYERS   │ │  ContextMenu(canvas)  Popover(virtual anchor)               ││  Item×N          │
│ ENV      │ ├ ResizableHandle ─────────────────────────────────────────────┤│ Tabs(detail)     │
│          │ │ Panel "dock" Tabs(Timeline | Charts | Events | Log)         ││  Table/LfChart   │
│          │ └──────────────────────────────────────────────────────────────┘│ ButtonGroup(cmd) │
└──────────┴───────────────────────────────────────────────────────────────┴──────────────────┘
```

| 区域 | 元素 | 组件组合 | 要点 | 版本 |
|---|---|---|---|---|
| **顶栏** | Logo | `<img src="/brand/anet-logo.svg">`（黑色胶囊、红色描边、白字，在暗底上显示正常）；小尺寸或 favicon 用 GitHub 头像 PNG，**下载到 `public/brand/` 本地托管** | 不从外链加载 | V0.1 |
| | 主菜单 | `Menubar > MenubarMenu > MenubarTrigger + MenubarContent > MenubarGroup > MenubarItem + MenubarShortcut(Kbd)`，`MenubarCheckboxItem` 用于图层显隐，`MenubarRadioGroup` 用于相机模式 | Item 必须放在 Group 里 | V0.1 |
| | 世界切换 | `Combobox`（带搜索）；世界数少于 7 个时用 `Select items={…}` | Base Select 必须传 `items` | V0.1 |
| | 仿真时钟与连接 | `Badge variant="secondary"` 加 `HoverCard`（WS 延迟、消息率、丢包、服务端 tick）；断线时切换为 `Badge variant="destructive"`，并在顶部显示 `Alert` | 数字用 tabular，4 Hz 刷新 | V0.2 |
| | 命令面板 | `Button variant="outline" size="sm"` 加 `Kbd`，打开 `CommandDialog`（其中 `CommandInput`、`CommandList`、`CommandGroup`、`CommandItem`、`CommandShortcut`） | 全局快捷键 Ctrl+K | V0.1 |
| **左栏 WORLD/LAYERS/ENV** | 容器 | `Sidebar side="left" collapsible="icon"` > `SidebarHeader`（世界名）> `SidebarContent` > `SidebarGroup` + `SidebarGroupLabel`（WORLD、LAYERS、ENVIRONMENT）> `SidebarMenu` + `SidebarMenuButton tooltip="…"` | icon 折叠后 `tooltip` 自动生效；折叠时触发 canvas 防抖 resize | V0.1 |
| | 图层 | `SidebarGroupContent` 里放 `Field orientation="horizontal"`（`FieldLabel` 加 `Switch`），外加一个 `Slider` 调透明度；点云渲染模式用 `ToggleGroup`（RGB、Height、Intensity） | 值变化直接写 engine store，不经过 React 状态树 | V0.1 |
| | 环境参数 | `Collapsible`（Wind、Rain、Fog、Sand、Cloud）> `FieldGroup` > `Field`（`FieldLabel`、`Slider`、`InputGroup`：`InputGroupInput type=number` 加 `InputGroupAddon` 单位）；风向用 `Slider` 0–360°，旁边放 lieflat 迷你罗盘（SVG） | 滑块与数值框双向绑定；拖动时 rAF 合并，松开后提交到后端（REST 或 WS） | V0.3 |
| | 天气预设 | `ToggleGroup`（晴、雨、雾、沙尘、自定义），图标为 `AppIcon` | 切换预设时 morph 图标 | V0.3 |
| **3D 视口** | 相机模式 | `ToggleGroup`（Orbit、Follow、FPV、BirdEye、Free）加 `Tooltip` + `Kbd`（1–5） | base 下 `value={[mode]}` | V0.1 |
| | 视图工具 | `ButtonGroup`（适配视野、正北、测距、截图），`Button size="icon-sm" variant="ghost"` | 图标用 `data-icon`，不加尺寸类 | V0.1 |
| | 性能 HUD | `Card size="sm"`：FPS（G18 计数器）、点预算 `Progress`、可见节点数、LOD `Badge`；点击后用 `Popover` 展开 `Table`（节点统计）、`Switch`（自适应 LOD）、`Slider`（点预算，0.5M–5M） | 4 Hz；`contain: strict` | V0.1 |
| | 右键 | `ContextMenu > ContextMenuTrigger(render={<div className="absolute inset-0"/>})`；菜单项：飞到此处、添加航点、设为 Home、量测、复制坐标 | 只有"右键按下后移动小于 4 px"才打开菜单（§6） | V0.2 |
| | 3D 锚定浮层 | `Popover`（受控 `open`），`Positioner` 的 `anchor` 设为 VirtualElement（投影后的屏幕坐标） | shadcn 的 `PopoverContent` 只透传了 align、side 等参数，需要在 `popover.tsx` 的 Pick 列表里加上 `anchor`；相机移动时关闭浮层 | V0.2 |
| | 加载与空态 | `Empty`（没有世界时：导入按钮加示例世界列表）；加载中用 `Skeleton` 加 `Progress`（瓦片队列） | | V0.1 |
| **右栏 DRONES** | 容器 | `Sidebar side="right" collapsible="none" style={{width:"20rem"}}`；显隐由应用状态控制，外层包一个 panel reveal 过渡 | sidebar-15 模式 | V0.1 |
| | 列表 | `ScrollArea className="scroll-fade-y"` > `ItemGroup` > `Item size="sm"`（`ItemMedia` 机型图标、`ItemContent` 放 `ItemTitle` P600-01 和 `ItemDescription` 高度与速度、`ItemActions` 放 `Badge` 状态和电量 `Progress`）；选中时 `data-selected` 加左侧 2 px 竖条 | 选中态不用红；只有 3D 场景中选中的无人机本体用红 | V0.1 |
| | 详情 | `Tabs variant="line"`（遥测、任务、传感器、Agent）> 遥测页：lieflat `Table`（ALT M、SPD M/S、BAT %、MODE）加 LfChart sparkline；任务页：航点 `Table`；Agent 页（V1.0）：`MessageScroller` | 遥测 10 Hz 只驱动当前可见的 tab | V0.2 |
| | 指令 | `ButtonGroup`（起飞、悬停、降落、返航）；降落、返航和急停经过 `AlertDialog` 确认；执行中显示 `Spinner data-icon`；ACK 或失败用 `toast.add()` | 急停要求按住 1 s 确认，按钮上显示进度 | V0.2 |
| **底部 Dock** | 时间线 | `Slider`（seek，事件点用绝对定位的刻度，悬停显示 `Tooltip`）+ `ButtonGroup`（上一事件、播放/暂停、下一事件；播放与暂停用 MorphIcon 切换）+ `ToggleGroup`（×1 ×2 ×5 ×10）+ 时间文本（tabular） | LIVE 与 REPLAY 用 `Badge` 区分；LIVE 状态显示红点 | V0.2 |
| | 图表页 | 多个 `Card`（LfChartCard）排成网格，内容是 d01 的 LfChart | 只有可见时才绘制 | V0.2 |
| | 事件与日志 | `Table`（时间、来源、级别、消息，虚拟滚动）；级别用 Badge；V1.0 协作日志改用 `MessageScroller` 加 `Marker` | | V0.2 |
| **任务编辑器** | 容器 | `Sheet side="right"`（宽 `sm:max-w-xl`）> `SheetHeader`/`SheetTitle` > `Tabs`（航点、区域、参数） | Sheet 必须有 Title | V0.2 |
| | 航点表 | Data Table（TanStack v9：`tableFeatures({rowSortingFeature,rowSelectionFeature,...})` 加 dnd-kit 排序）；单元格内编辑用 `InputGroup` 和 `NativeSelect` | 拖动排序后同步 3D 航线 | V0.2 |
| **设置与帮助** | | `Dialog` > `Tabs`（通用、渲染、网络、快捷键）> `FieldGroup`；快捷键表用 `Table` + `KbdGroup` | | V0.1 |
| **全局** | | `TooltipProvider delay={400} closeDelay={80}`（默认 0 延迟会在高密度按钮间频繁闪现）；Toast 的 `ToastProvider` 和 `ToastViewport` 放在根节点 | | V0.1 |

布局骨架（`App.tsx`，只写结构）：

```tsx
<ThemeProvider defaultTheme="dark" hotkey={false}>
 <TooltipProvider delay={400}>
  <ToastProvider>
   <div className="[--header-height:2.75rem] h-svh overflow-hidden">
    <SidebarProvider className="flex h-full min-h-0 flex-col" defaultOpen>
      <AppHeader />                                    {/* h-(--header-height) border-b */}
      <div className="flex min-h-0 flex-1">
        <WorldSidebar collapsible="icon"
          className="top-(--header-height) h-[calc(100svh-var(--header-height))]!" />
        <SidebarInset className="min-w-0">
          <ResizablePanelGroup orientation="vertical" id="main">
            <ResizablePanel id="viewport" defaultSize="72%" minSize="40%">
              <Viewport3D />                           {/* canvas + HUD overlay, contain:strict */}
            </ResizablePanel>
            <ResizableHandle withHandle />
            <ResizablePanel id="dock" defaultSize="28%" minSize={120} collapsible collapsedSize={36}>
              <BottomDock />
            </ResizablePanel>
          </ResizablePanelGroup>
        </SidebarInset>
        {droneRailOpen && <DroneRail />}               {/* <Sidebar side="right" collapsible="none" style={{width:"20rem"}}/> */}
      </div>
    </SidebarProvider>
    <ToastViewport />
   </div>
  </ToastProvider>
 </TooltipProvider>
</ThemeProvider>
```

布局持久化：用 `useDefaultLayout({ id:"main", storage: localStorage })`（react-resizable-panels v4 导出）保存分栏比例；侧栏的展开状态由 `SidebarProvider` 写入 cookie（`sidebar_state`，7 天）。

### 3.6 transitions.dev 动效整合（交付物 3 之一）

**原理**：Base UI 的弹层在挂载后的第一帧带 `[data-starting-style]`，开始关闭时带 `[data-ending-style]`，并通过 `element.getAnimations()`（包括 CSS transition）等待动画结束后才卸载（`@base-ui/react/docs/react/handbook/animation.md`）。transitions.dev 的每个片段都是"前置态 → `.is-open` → `.is-closing`"三态模型，而且只用 transform、opacity 和 filter。对应关系如下：

| transitions.dev 片段（`skills/transitions-dev/NN-*.md`） | 三态映射 | 应用到的 shadcn 部件（`data-slot`） |
|---|---|---|
| 05 Menu dropdown（`--dropdown-open-dur 250ms / close 150ms / pre-scale .97 / closing-scale .99 / ease cubic-bezier(.22,1,.36,1)`） | 前置态对应 `[data-starting-style]`，`.is-open` 对应默认态，`.is-closing` 对应 `[data-ending-style]`；`transform-origin: var(--transform-origin)` 由 Base UI 提供，天然满足 origin-aware | `dropdown-menu-content`、`context-menu-content`、`menubar-content`、`*-sub-content`、`popover-content`、`hover-card-content`、`select-content`、`combobox-content` |
| 06 Modal（250/150 ms，scale .96） | 同上 | `dialog-content`、`alert-dialog-content`（遮罩只做 opacity） |
| 07 Panel reveal（开 400 ms、关 350 ms、translateY 100px、blur 2px） | 改为按 `data-[side]` 平移 | `sheet-content`；右栏 DroneRail 的显隐 |
| 17 Tooltip（进 150 ms、出 50 ms、scale .98、组内切换延迟 80 ms） | Base UI Tooltip 在"已有 tooltip 打开时切换到相邻 trigger"会立即显示，与 `--tt-delay` 的语义一致 | `tooltip-content` |
| 22 Toast（开 350 ms、关 250 ms、16px、blur 2px） | Base UI Toast 已经内置 `[transition:transform_500ms_cubic-bezier(0.22,1,0.36,1)]`，**同一条曲线**，只需把时长换成 token | `toast` |
| 21 Accordion（250 ms） | `h-(--accordion-panel-height)` 加 `data-starting-style:h-0` | `accordion-content`、`collapsible-content` |
| 16 Tabs sliding | shadcn 的 tabs 没有 Indicator，需要在 `tabs.tsx` 的 `TabsList` 里加 `<TabsPrimitive.Indicator className="… w-(--active-tab-width) translate-x-(--active-tab-left) transition-[translate,width]"/>` | `tabs-list` |
| 27 Toggle / 25 Checkbox | 调整 Switch thumb 的 transition 曲线；Checkbox 的对勾做路径描边动画 | `switch`、`checkbox-indicator` |
| 02 Number pop-in / 26 Spinning counter | 数值变化 | HUD 大数字、时间线时钟（**超过 2 Hz 的数值禁用**，改为直接赋值） |
| 14 Skeleton reveal | 从骨架切换到内容 | 世界加载、列表首屏 |
| 12 Error shake | 校验失败 | `Field data-invalid` 的输入框 |
| 09 Icon swap | 无法 morph 的图标（填充型或位图） | logo 与状态图 |
| 10 Success check | 指令 ACK 成功 | toast 图标 |

**适配层 `src/styles/motion.css`（结构示例）：**

```css
:root{ /* 从 transitions.dev skills/_root.css 复制所需 token；d01 的 --dur-* 作为别名 */
  --ease-smooth-out:cubic-bezier(0.22,1,0.36,1);
  --dropdown-open-dur:250ms; --dropdown-close-dur:150ms; --dropdown-pre-scale:.97; --dropdown-closing-scale:.99;
  --modal-open-dur:250ms; --modal-close-dur:150ms; --modal-scale:.96;
  --panel-open-dur:400ms; --panel-close-dur:350ms; --panel-blur:2px;
  --tt-in-dur:150ms; --tt-out-dur:50ms; --tt-scale:.98;
  --dur-fast:var(--duration-quick,150ms); --dur-base:var(--duration-fast,250ms);   /* 与 d01 对齐 */
}
:where([data-slot=dropdown-menu-content],[data-slot=context-menu-content],[data-slot=menubar-content],
       [data-slot$=-sub-content],[data-slot=popover-content],[data-slot=hover-card-content],
       [data-slot=select-content],[data-slot=combobox-content]){
  transform-origin:var(--transform-origin);
  transition:transform var(--dropdown-open-dur) var(--ease-smooth-out),opacity var(--dropdown-open-dur) var(--ease-smooth-out);
  &[data-starting-style]{opacity:0;transform:scale(var(--dropdown-pre-scale))}
  &[data-ending-style]{opacity:0;transform:scale(var(--dropdown-closing-scale));transition-duration:var(--dropdown-close-dur)}
}
:where([data-slot=dialog-content],[data-slot=alert-dialog-content]){
  transition:transform var(--modal-open-dur) var(--ease-smooth-out),opacity var(--modal-open-dur) var(--ease-smooth-out);
  &[data-starting-style],&[data-ending-style]{opacity:0;transform:scale(var(--modal-scale))}
  &[data-ending-style]{transition-duration:var(--modal-close-dur)}
}
:where([data-slot=tooltip-content]){
  transition:transform var(--tt-in-dur) ease-out,opacity var(--tt-in-dur) ease-out;
  &[data-starting-style],&[data-ending-style]{opacity:0;transform:scale(var(--tt-scale))}
  &[data-ending-style]{transition-duration:var(--tt-out-dur)}
}
@media (prefers-reduced-motion:reduce){ :where([data-slot]){transition-duration:0s!important} }
```

**codemod（去掉 tw-animate 的 keyframe 类，避免与 transition 叠加）**：只作用于 `src/components/ui/{dialog,alert-dialog,dropdown-menu,context-menu,menubar,popover,hover-card,select,combobox,tooltip}.tsx`，在 className 字符串中删除匹配下面正则的 token：

```text
/\b(?:data-(?:open|closed)|data-\[state=delayed-open\]):(?:animate-(?:in|out)|fade-(?:in|out)-0|zoom-(?:in|out)-95)\b|\bdata-\[side=[a-z-]+\]:slide-in-from-[a-z-]+-2\b|\bduration-100\b/g
```

同时删除遮罩上的 `supports-backdrop-filter:backdrop-blur-xs`。改完后执行 `shadcn add <x> --diff` 对照上游变化，把修改记录在 `docs/ui-patches.md`，方便以后升级时重放。

### 3.7 morphicons 图标整合（交付物 3 之二）

- **依赖**：`morphicons`、`lucide`（数据包）、`lucide-react`（shadcn 组件内部使用）。三者版本锁定一致：`lucide` 与 `lucide-react` 用同一版本号，例如都为 `1.48.0`。
- **统一入口 `src/components/app-icon.tsx`**：

```tsx
import { MorphIcon, type MorphIconProps } from "morphicons/react"
import type { IconNode } from "lucide"
/** 所有应用层图标都走这里：静态时渲染普通 SVG；icon 属性变化时自动 morph */
export function AppIcon({ icon, label, ...p }: { icon: IconNode; label?: string } & Omit<MorphIconProps,"icon">) {
  return <MorphIcon icon={icon} label={label} spring="snappy" reducedMotion="user" strokeWidth={1.75} {...p} />
}
// 用法：<Button size="icon-sm" variant="ghost"><AppIcon data-icon="inline-start" icon={playing ? Pause : Play}/></Button>
```

- **必须 morph 的状态切换**：播放和暂停；侧栏展开和折叠（`PanelLeftOpen` 与 `PanelLeftClose`）；图层可见（`Eye` 与 `EyeOff`）；锁定跟随（`Lock` 与 `LockOpen`）；连接状态（`Wifi` 与 `WifiOff`）；主题（`Sun` 与 `Moon`）；排序（`ArrowUp` 与 `ArrowDown`）；天气预设（`Sun`、`CloudRain`、`CloudFog`、`Wind`）；Dock 折叠（`ChevronDown` 与 `ChevronUp`）。
- **shadcn 组件内部的图标**（chevron、check、X、PanelLeft 等，由安装时的 transformer 写入的 `lucide-react` 组件）保持静态，不需要改动。如果要求 100% 统一走 morphicons，可以写一个 ts-morph 或正则 codemod，把 `import { XIcon } from "lucide-react"` 改为 `import { X } from "lucide"`，再把 `<XIcon …/>` 改为 `<AppIcon icon={X} …/>`。这个操作放到 V0.3，收益不大。
- **禁止项**：emoji；Unicode 字形图标（`U+25B2 U+25BC U+25CF U+25CB ↑ ←`，lieflat 模板里有，d01 已经列出）；把图标名字符串映射到组件（违反 `rules/icons.md`，还会破坏 tree-shaking）。
- **图标尺寸**：Button、MenuItem、SidebarMenuButton 内部的图标不加 `size-*`，由组件 CSS 控制（mira 下为 `size-4` 或 `size-3.5`）。独立使用时用 `size` 属性，统一为 14 或 16 px。

### 3.8 lieflat 图表与表格整合（交付物 3 之三）

- **冲突处理原则：视觉以 lieflat 为准，结构与交互以 shadcn 为准。**
  - `Card` 作为 LfChartCard 的外壳：`CardHeader` 放结论式标题，`CardDescription` 放"图例 · 时间范围"，`CardAction` 放 `ToggleGroup` 时间窗，`CardFooter` 放来源行（全大写、加字距）。
  - 图表主体是 d01 的 SVG 或 CPU canvas 组件；tooltip 复用 `TooltipContent` 的类（`bg-foreground text-background`），这恰好就是 lieflat 的 tipDark 反转样式。
- **不安装 `chart`**。原因有三：① 它把 `recharts@3.8.0` 锁成硬依赖，而 lieflat 规定"禁止退回图表库默认样式"；② d01 实测 SVG 在 10 Hz 更新时会拖到 4.7 fps，Recharts 的 React 组件树开销更大；③ `ChartStyle` 用 `dangerouslySetInnerHTML` 为每个图注入 `--color-<key>`，主题切换依赖 `.dark` 选择器。唯一借鉴的是 **`ChartConfig` 的形状**（`{label, icon, color|theme}`），d01 的 `LfConfig` 已经照此设计，但把颜色限定为角色（data、data2、faint、hero）。
- **表格**：shadcn `Table` 的结构（`Table/TableHeader/TableBody/TableFooter/TableRow/TableHead/TableCell/TableCaption`）加上 d01 §3.9 的 `lfTable` className 映射：表头 10 px、700、字距 .12em、大写；行间点线；数字右对齐并使用 tabular-nums；不用斑马纹；合计行上方实线；每表最多一个 hot 单元格。
- **Data Table**：TanStack Table v9。

```ts
const features = tableFeatures({ rowSortingFeature, rowSelectionFeature, columnVisibilityFeature,
  sortedRowModel: createSortedRowModel() })          // 未注册的功能会被 tree-shake 掉
const col = createColumnHelper<typeof features, Waypoint>()
const table = useTable({ features, data, columns, state:{sorting,rowSelection}, getRowId:r=>r.id, onSortingChange:setSorting })
// 渲染：<FlexRender …/>；超过 200 行时加 @tanstack/react-virtual；拖动排序用 @dnd-kit/sortable（与 dashboard-01 相同）
```

### 3.9 离线与无网络安装（交付物 4）

**会访问网络的环节**（源码依据）：
1. `init` 与 `add` 拉取 `${REGISTRY_URL}/styles/<style>/<name>.json`、`/r/colors/<baseColor>.json`、`/r/index.json`，以及 `${SHADCN_URL}/init?…`（`registry/resolver.ts`、`preset/presets.ts`）。
2. 新建项目时 `git clone` GitHub 上的 `templates/<dir>`（`templates/create-template.ts`）。
3. 安装 npm 依赖。
4. `registryDependencies` 中的 `font-inter` 这类 item 需要访问 registry。
5. `@namespace` 形式的社区 registry 需要 `registries.json`。

**L1（推荐，零运行时网络）**：在有网的机器上执行 §3.1，把 `src/components/ui/**`、`components.json`、`package-lock.json` 提交进仓库。之后开发与构建完全不需要 registry。升级时再联网执行 `add --diff`。这是 shadcn "代码归你所有"模型的本意。

**L2（本地 registry 镜像，已实测）**：

```bash
# 联网时执行一次：STYLE=base-mira ./mirror.sh，产出 mirror/r/{index.json,colors/*.json,styles/index.json,registries.json,styles/base-mira/*.json} 和 mirror/init
python3 -m http.server 8765 --directory mirror &              # 静态服务器忽略查询串，/init?base=… 会返回 mirror/init 文件
export REGISTRY_URL=http://127.0.0.1:8765/r                   # SHADCN_URL 自动推导为 http://127.0.0.1:8765
export SHADCN_TEMPLATE_DIR=/data/projs/anet-drone/refs/design/ui/templates   # 不再从 GitHub clone 模板
npx shadcn@latest init ./design/anet-base.json -t vite -n web --no-monorepo -y   # 本地 JSON，无需 /init
npx shadcn@latest add slider kbd sidebar-15 -y
```

实测日志中，除了 `new-york/*` 的三次 fallback 探测返回 404（无害），其余请求全部是 200。镜像必须包含 `r/colors/neutral.json`，否则 `add` 会报 "The item at …/colors/neutral.json was not found"。`mirror.sh` 在 `.cache/research/d04/`，建议移到 `tools/shadcn-mirror.sh`，镜像目录放在 `vendor/shadcn-registry/`（676 KB，可以提交）。

**npm 依赖离线**：
- 在有网环境执行 `npm ci`，然后用 `npm cache` 打包，或者运行 verdaccio 作为本地 npm 仓库。
- 在无网环境执行 `npm ci --offline --cache <dir>`。
- 本机当前 registry 是 `registry.npmmirror.com`，内网可以直接复用这个镜像。
- `@fontsource-variable/*` 自带 woff2 文件，没有 CDN 依赖。
- `shadcn` 包本身较重（6.5 MB，另外带上 typescript 24 MB、@ts-morph 12 MB）。执行 `eject` 后可以从运行依赖中移除，CLI 只在需要时用 `npx` 调用。

**L3（完全无 registry 的兜底）**：从 `refs/design/ui/apps/v4/registry/bases/base/ui/*.tsx` 直接复制作者源码，然后做三件事：
1. 把 import 路径 `@/registry/bases/base/ui/` 替换成 `@/components/ui/`。
2. 把 `IconPlaceholder` 转成 lucide。可以用正则，把 `<IconPlaceholder lucide="(\w+)"[^>]*?(className="[^"]*")?\s*/>` 改为 `<$1 $2/>` 并补 import；也可以移植 `transform-icons.ts`。
3. 在 `index.css` 中 `@import "./vendor/style-mira.css" layer(base);`（文件来自 `registry/styles/style-mira.css`），在 `<body>` 上加 `class="style-mira"`，这样 `cn-*` 类由 style CSS 在运行时展开。官网就是这么做的（`apps/v4/app/style-registry.css`）。

代价是：`cn-*` 类位于 base layer，`className` 覆盖会自动胜出（utilities layer 优先级更高），但 `twMerge` 无法对未展开的类去重。只在 L1 和 L2 都不可用时使用。

### 3.10 UI 与 3D 共存的性能约束（伪代码）

```ts
// 1) 遥测：WS 10–50 Hz → 引擎（每帧读取） ; → UI store 节流到 4 Hz（HUD）或 10 Hz（当前聚焦的图）
ws.onmessage = (m) => { telemetryRing.push(decode(m)); }            // 不触发 React 渲染
setInterval(() => uiStore.setState(snapshot(telemetryRing)), 250)     // HUD 4 Hz
// 组件只订阅自己需要的字段（zustand selector + shallow），避免整棵树重渲染
const alt = useUi(s => s.drones[id]?.alt)

// 2) 侧栏折叠会带来 200 ms 的宽度过渡，不要在过渡期间逐帧 setSize（会重新分配 drawing buffer）
const ro = new ResizeObserver(debounce(([e]) => engine.resize(e.contentRect), 120))
// 过渡期间 canvas 用 CSS 拉伸（width:100%;height:100%），结束后一次性 setSize；可以同时监听 transitionend

// 3) 覆盖在 canvas 上的 DOM：禁用 backdrop-filter；HUD 设置 contain:strict；避免 transition-all
// 4) Tooltip 和 Popover 的 Portal 挂在 body 上；打开弹层时不暂停渲染循环，但可以把点预算降到 70%（与 r12 的 FPS 反馈联动）
// 5) 相机拖动期间：closeAllPopovers(); pointer-events 交给 canvas；HUD 容器 pointer-events:none，子元素需要交互时单独设为 auto
```

### 3.11 全局快捷键表（避免冲突）

| 键 | 作用 | 来源 |
|---|---|---|
| Ctrl/⌘+B | 左栏折叠 | `sidebar.tsx` 的 `SIDEBAR_KEYBOARD_SHORTCUT="b"`（内置） |
| Ctrl/⌘+K | 命令面板 | 自行定义 |
| W/A/S/D/Q/E | FPV 或 Free 相机移动 | 自行定义；**要移除模板 ThemeProvider 的 `d` 键绑定** |
| 1–5 | 相机模式 | 自行定义 |
| Space | 播放或暂停 | 自行定义 |
| [ / ] | 倍速 | 自行定义 |
| Esc | 取消选择、关闭弹层 | Base UI 内置（弹层） |

统一用一个 `useHotkeys` 注册表，并复用模板中的 `isEditableTarget()`（在 input、textarea、select、contenteditable 中不触发）。弹层打开时（`document.querySelector('[data-open][role=dialog]')`）屏蔽飞行快捷键。

---

## 4. 在本项目中的落点与复用方式

| 条目 | 源 | 目标模块 | 方式 | 版本 | 理由 |
|---|---|---|---|---|---|
| CLI 与 `anet-base.json` | `packages/shadcn`、`apps/v4/registry/config.ts` 中的 `buildRegistryBase` | `apps/web/`、`design/anet-base.json` | adopt | V0.1 | 一次命令生成主题、字体和依赖，可重复执行 |
| Base UI 组件（MVP 约 45 个） | `bases/base/ui/*`（安装时已按 mira 展开） | `apps/web/src/components/ui/` | adopt（源码归本项目维护） | V0.1–V0.2 | 已实测可以构建 |
| ANet Graphite 主题变量 | d01 §3.2 加本文 §3.3 | `apps/web/src/index.css` | port | V0.1 | 全产品唯一色系 |
| `shadcn/tailwind.css` | `packages/shadcn/src/tailwind.css` | `@import` 或 eject 后内联 | adopt | V0.1 | data-\* 自定义 variant 是组件样式的前提 |
| 布局骨架 | `blocks/sidebar-07/15/16` | `src/app/layout/*` | port | V0.1 | 官方已验证的组合 |
| motion 适配层与 codemod | transitions.dev + Base UI 动画手册 | `src/styles/motion.css`、`tools/codemods/strip-tw-animate.mjs` | port | V0.1 | 动效来源统一 |
| AppIcon | morphicons + lucide | `src/components/app-icon.tsx` | adopt | V0.1 | 禁 emoji，状态切换统一 morph |
| lieflat Table 皮肤、LfChartCard | d01 | `src/components/lf/*` | port | V0.1–V0.2 | 图表与表格统一视觉 |
| Data Table（TanStack v9） | `dashboard-01/components/data-table.tsx` | `src/components/data-table/*` | port | V0.2 | 航点、多机、事件表 |
| Base UI Toast manager | `bases/base/ui/toast.tsx` | `src/lib/notify.ts` | adopt | V0.1 | 非 React 代码可以直接发通知 |
| Popover 虚拟锚点扩展 | Base UI `Positioner.anchor` | `src/components/ui/popover.tsx`（给 Pick 列表加 `anchor`） | 修改 | V0.2 | 3D 拾取后弹出菜单 |
| Tabs Indicator | Base UI `Tabs.Indicator` | `src/components/ui/tabs.tsx` | 修改 | V0.1 | 实现 transitions.dev 的 tabs sliding |
| 离线镜像 | `.cache/research/d04/mirror.sh` | `tools/shadcn-mirror.sh`、`vendor/shadcn-registry/` | adopt | V0.1 | 内网与断网环境 |
| Chat 原语与 `@shadcn/react` | `bases/base/ui/{message-scroller,message,bubble,marker,attachment,questionnaire}` | `src/components/ui/`（V1.0 再安装） | adopt | V1.0 | ANet 协作日志、协商、审批 |
| `createChat` | `packages/helpers` | `apps/web/e2e/mocks/anet-chat.ts` | reference | V1.0 | mock Agent 流 |
| shadcn 编码规范 | `skills/shadcn/rules/*.md` | `docs/ui-guidelines.md` 与 ESLint 规则 | adopt | V0.1 | 保持组合方式一致 |
| style-map 管线 | `packages/shadcn/src/styles/*` | `tools/`（仅 L3） | reference/port | 兜底 | 离线兜底 |
| `chart`、`carousel`、`input-otp`、`navigation-menu`、`pagination`、`sonner`、`form`、`direction` | registry | — | skip | — | 与本项目无关，或与 lieflat 冲突 |

---

## 5. 对比与推荐

### 5.1 base 对比

| 维度 | Base UI（`base`） | Radix（`radix`） | React Aria（`aria`） |
|---|---|---|---|
| 2026 状态 | **默认**（CLI 4.13.0 起），1.x 稳定（1.6 至 1.8） | 维护中，社区 registry 最多 | 4.13.1 新增 |
| 组件覆盖 | 63，包括 toast | 62（没有 toast，用 sonner） | 60（没有 menubar、navigation-menu、toast） |
| 动效 | `data-starting-style/ending-style` 做 CSS transition，可中途取消 | `data-state` 加 keyframes | `data-entering/exiting` |
| 组合方式 | `render` 属性加 `useRender` 和 `mergeProps` | `asChild` | render props |
| 特色 | Positioner `anchor`（支持 VirtualElement）、Select 多选与对象值、Slider 标量、Toast manager、NumberField 和 Meter 原语（shadcn 未封装） | 生态成熟 | 国际化和可访问性最强 |
| 本项目 | **选用** | 移植社区组件时参考 | 缺 menubar，不选 |

### 5.2 style 对比（`apps/v4/registry/styles.tsx`）

| style | 官方描述 | 按钮默认尺寸 | 本项目 |
|---|---|---|---|
| **mira** | Made for compact interfaces | h-7、text-xs | **主选**：控制台和 HUD |
| nova | Reduced padding and margins | h-8、text-sm | 备选：分析页和报告页（可以单独起一个 app） |
| vega | Clean, neutral, and familiar | h-9 | 不选：太松 |
| lyra | Boxy and sharp. For mono fonts | h-8、rounded-none | 不选：过硬，和 lieflat 的圆角冲突 |
| maia / luma / rhea | 圆润、柔和、发光感 | — | 不选：和"科技灰"的调性不符 |
| sera | Editorial and typographic | — | 不选（衬线字体） |

### 5.3 其余选项

| 需求 | 选项 | 推荐 |
|---|---|---|
| 消息通知 | Base Toast / sonner | **Base Toast** |
| 图表 | shadcn Chart（Recharts）/ lieflat 移植 / ECharts | **lieflat 移植**（d01）；Recharts 与 ECharts 都不引入 |
| 表格 | shadcn Table / Data Table（TanStack v9） | 静态小表用 Table 加 lieflat 皮肤；交互表用 Data Table |
| 分栏 | Resizable / Sidebar | 两者组合（§3.5） |
| 命令面板 | Command（cmdk）/ Combobox | 全局入口用 Command，字段级选择用 Combobox |

**推荐排序**：本单元只有一个仓库，内部子模块按"对 MVP 的价值"排序为：CLI 与 base-mira 组件 > 主题变量机制（`@theme inline`） > sidebar-15/16 布局块 > `shadcn/tailwind.css` > skills 规则 > Chat 原语（V1.0） > style-map 管线（兜底）。

---

## 6. 风险与注意事项

1. **模板 ThemeProvider 的热键冲突**：`templates/vite-app/src/components/theme-provider.tsx` 的 `handleKeyDown` 在按下裸 `d` 键（无修饰键、非输入框）时切换明暗，会和 WASD 飞行控制直接冲突。另外它 `disableTransitionOnChange` 时注入的全局 `transition:none` 也会打断动效。处理：删除这段按键逻辑，或加一个 `hotkey={false}` 开关，默认主题设为 `"dark"`。
2. **编译错误**：`scroll-area.tsx` 第 1 行 `import * as React` 未使用，在 `noUnusedLocals` 下报 TS6133（实测）。升级组件后可能再次出现，建议写进 postinstall 修补脚本。
3. **依赖漂移**：registry 里写的是 `react-day-picker@latest`、`recharts@3.8.0`，官网锁定 `lucide-react 0.474.0`，而 init 实际装到的是 1.48.0。`cn` 的 0.x 版本变化很快（官网用 0.2.2，实测装到 0.4.0，`pnpm-workspace.yaml` 还专门为 cn 0.2.2 设了 `minimumReleaseAgeExclude`）。处理：`package-lock.json` 必须提交；`lucide`（morphicons 用）和 `lucide-react` 锁同一版本；Renovate 每周检查一次。
4. **shadcn 组件是快照，不会自动升级**：本项目对 popover（anchor）、tabs（Indicator）、tw-animate 类、backdrop-blur、scroll-area 做了修改，升级时要用 `add --diff` 手动合并。所有改动记录在 `docs/ui-patches.md`，写成可重放的 codemod。
5. **backdrop-filter 的性能**：dialog、alert-dialog 和 sheet 的遮罩带 `supports-backdrop-filter:backdrop-blur-xs`，menuColor translucent 带 `backdrop-blur-2xl`。在持续刷新的 WebGL canvas 上方，每一帧都要重新模糊合成，在 SwiftShader 下代价极高（d01 的实测也证实了这一点）。必须删除。
6. **两侧栏共用 `--sidebar-width`**：`SidebarProvider` 在 wrapper 上设置 `--sidebar-width: 16rem`，gap div 和 container 都读取它。给 `Sidebar` 传 `style` 只会影响 container，gap 仍按 16rem 计算，布局会错位。因此右栏只能用 `collapsible="none"`（普通 div，style 生效），或者改 sidebar.tsx 让它支持按侧设置宽度。另外，`collapsible="none"` 分支没有外层 `group` 元素，`group-data-[collapsible=icon]` 相关样式不会生效。
7. **Sidebar 桌面容器是 `fixed inset-y-0 h-svh`**：有顶栏时必须照 sidebar-16 的写法加 `top-(--header-height) h-[calc(100svh-var(--header-height))]!`，否则侧栏会盖住顶栏。wrapper 默认 `min-h-svh`，要改成 `h-full min-h-0`，否则整页会出现滚动。
8. **ContextMenu 与相机控制冲突**：OrbitControls 和 CameraControls 默认用右键平移，Base UI ContextMenu 会在 `contextmenu` 事件时打开。处理：在 trigger 上拦截事件，只有右键按下到抬起的位移小于 4 px 且时长小于 300 ms 时才放行，其余情况 `preventDefault`；或者把平移改到中键或 Shift+左键。
9. **Base 与 Radix 的 API 陷阱**：移植社区代码时注意以下几点，另外 **Slider 的包装层会在只传标量时渲染两个 thumb**（`_values` 回退到 `[min,max]`），所以一律传数组。
   - `asChild` 要改为 `render`；
   - `render` 渲染成非 button 元素时要加 `nativeButton={false}`；
   - `ToggleGroup` 的 `defaultValue` 必须是数组，受控时要自己包装和解包；
   - `Select` 必须传 `items`，placeholder 用 `{value:null}` 表示；
   - `Accordion` 没有 `type` 属性。
10. **react-resizable-panels v4 的单位**：`minSize={200}` 表示 200 **像素**，`defaultSize="30"` 表示 30%。旧版 v2 的 `direction` 改名为 `orientation`，`PanelGroup` 改为 `Group`，`PanelResizeHandle` 改为 `Separator`，网上的 v2 示例不能直接用。
11. **Tailwind v4 的内容扫描**：会自动扫描项目内所有非 gitignore 的文本文件。点云瓦片、JSON 元数据如果放在 `apps/web/public` 且没有被忽略，会拖慢 dev 启动和 HMR。用 `@source not` 排除，或者把世界数据放在 `data/` 目录，由后端提供。
12. **中文排版**：Inter 不含 CJK 字形，回退到系统字体后，中英文的字重和基线会不一致。lieflat 的"全大写加字距"规则不适用于中文（d01 已说明）。Noto Sans SC 的 fontsource 包体积很大（按 unicode-range 分片，按需加载），离线环境可以接受，但首屏会有字体切换，需要设置 `font-display: swap`。
13. **Tooltip 默认 0 延迟**：base 包装层的 `TooltipProvider` 默认 `delay = 0`（与 style 无关），在密集的图标工具栏上扫过时会频繁闪现。设置为 `delay={400}`。
14. **Popover 虚拟锚点不会跟随相机**：floating-ui 的 autoUpdate 只响应滚动、resize 和元素尺寸变化，感知不到相机运动，所以相机移动时浮层会漂移。相机移动时关闭浮层；需要持续跟随的标签由引擎直接驱动 DOM（r14 的方案）。
15. **`add --all` 的代价**：会装上 recharts、embla、react-day-picker、input-otp、@shadcn/react、cmdk 等依赖，并生成 61 个文件，CSS 达到 194 KB。只安装 §3.1 的清单。
16. **GPU 与平台**：shadcn 本身不依赖 GPU。本机 headless Chromium 使用 SwiftShader，所有 CSS 模糊、阴影、大面积半透明都会占用同一个软件光栅器，和 3D 渲染抢资源。UI 性能要和 3D 一起在 headless 下做基准测试（沿用 d01 与 r14 的 bench 方法）。

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§34 前端技术栈里"UI | shadcn/ui"写得太粗。** 建议改为：`shadcn/ui 4.x（CLI）+ Base UI 1.x（base）+ style mira + Tailwind CSS v4（@theme inline / OKLCH）+ tw-animate-css`，再单列四行：
   - Motion：transitions.dev 片段加 Base UI 的 `data-starting/ending-style`；
   - Icons：morphicons 加 `lucide` 数据包，禁止 emoji；
   - Charts & Tables：lieflat 移植（SVG 与 CPU canvas）加 shadcn Table 与 TanStack Table v9，**不用 Recharts**；
   - Fonts：Inter 与 JetBrains Mono（fontsource，离线）。

   同时注明"组件源码提交进仓库，registry 只在开发期使用，并提供离线镜像"。
2. **§38 UI 设计缺少设计系统的定义。** 应该补一节《设计系统》，包括：色卡（ANet Graphite，暗色为默认，给出完整 token 表，见 §3.3）、密度（mira）、排版（Inter 加 tabular-nums，等宽字体用于坐标和 ID）、状态语义（critical、warning、nominal、stale，不引入第三色相）、"一处红"规则、圆角（0.45rem；图卡单独规定）、动效 token 表。原文"产品色采用科技灰、黑色、色、红色"中间缺了一个字，按"白色"理解，d01 也采用同样理解，建议在原文中改正。
3. **§38 的布局只有左右两栏，缺少以下区域：**
   - 顶栏：菜单、世界切换、仿真时钟与连接状态、命令面板；
   - 底部 Dock：时间线、图表、事件和日志，与 §39 合并；
   - 3D 视口上的 HUD：FPS、点预算、LOD，这是验证"渐进加载与自适应密度"必需的可观测性入口；
   - 空、加载、错误状态：未加载世界、WS 断开、WebGPU 降级到 WebGL2 的提示；
   - 设置与快捷键帮助。

   建议把 §3.5 的区域到组件映射表作为 UI 交互 PRD 的骨架。
4. **§37 刷新频率缺少"UI 刷新"这一层。** 在 "WebSocket 10–50 Hz" 和 "Web Rendering 60 FPS" 之间补一行 **"React/DOM UI：HUD 4 Hz、聚焦图表不超过 10 Hz、列表 2 Hz"**，并写明遥测不直接进入 React 状态树（先写入环形缓冲，再由 selector 节流）。否则 50 Hz 的 setState 会拖垮 3D 帧率。
5. **§40 Drone Interaction 需要细化交互。**
   - 相机模式用 ToggleGroup 加数字快捷键；
   - FPV 用 Resizable 分屏或画中画，而不是整屏切换；
   - 右键菜单需要和相机右键平移做区分；
   - 危险指令（降落、返航、急停）必须经过 AlertDialog 或"按住确认"，并有 ACK 反馈（toast）和超时处理；
   - 为 SIM 与 REAL 数据源加显式标记（Badge），V0.5 接入真机后尤其重要。
6. **§39 Timeline 需要补充。**
   - 区分 LIVE 与 REPLAY 两种模式，切换时要确认；
   - 事件刻度（起飞、告警、任务节点）画在 seek 轨道上，并可以跳转；
   - 倍速用 ToggleGroup；
   - 快捷键：Space、[、]；
   - 回放的数据源（服务端录制或浏览器缓存）要在 §36 的 REST 与 WS 分工中写清楚。
7. **§16 场景分层应该补一层 "UIOverlayLayer"**（DOM）。标签和选中框的 DOM 化策略写在这里：由引擎驱动位置，shadcn 只负责交互浮层。同时说明 DebugLayer 的开关由性能 HUD 的 Popover 控制。
8. **§42 仓库结构需要补充。** 在 `apps/web` 下加 `src/components/ui`（shadcn，本项目维护）、`src/components/lf`（lieflat）、`src/styles/{index,motion,lf}.css`；再加 `design/anet-base.json`（主题的唯一来源）、`vendor/shadcn-registry/`（离线镜像）、`tools/codemods/`、`docs/ui-patches.md`。
9. **§43 MVP 应加入 UI 验收标准**：3D 视口在 SwiftShader 下的基线帧率不低于 X fps，打开任一弹层或侧栏时帧率下降不超过 10%；侧栏折叠期间不出现 WebGL 重分配卡顿；所有图标按钮都有 Tooltip 和 aria-label；键盘可以完成起飞、降落、相机切换；`prefers-reduced-motion` 生效。
10. **V1.0 的 ANet UI 可以直接用 shadcn 的 2026 Chat 原语**：MessageScroller 加 Message、Bubble 呈现 Agent 间的能力发现与任务协商；Marker 标记系统事件（例如"Drone B 接受任务"）；Attachment 放热成像截图；Questionnaire 或带审批的 tool call 做操作员确认。建议在 §31–§32 中写入这套交互形态。
11. **原文中的文字错误**（顺手修正）：
    - §2 "图像处理、数据预处理、知和部分在线推理"，漏了"感"字；
    - §12 "浏览器能够直接访 GPU"，漏了"问"字；
    - §31 "多机仿真成熟之再引入"，缺"后"字；
    - §41 目录树里 `── visual/` 缺少 `└` 前缀；
    - §51 "整个项最核心"，漏了"目"字；
    - 文中残留的 `:chatgpt-content-reference{index=…}` 标记应删除。
