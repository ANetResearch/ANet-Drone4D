# shadcn 组件补丁（codemod）清单（M15-FR-083；M15-AC-045；g07 §3-§5；d04 §6）

`postadd.mjs` 按下表顺序执行 `codemods/*.mjs`；每个 codemod 幂等（对已处理的源码不产生改动），锚点失配时报错
（M15 §11 K3）。`node tools/shadcn/postadd.mjs --check` 只检查不写入（`make lint-m15`）。全量重装
（`node tools/shadcn/install.mjs`）后 `ui/components/ui/**` 与 `ui/hooks/**` 与重装前逐字节相同（2026-09-29 验证）。

| # | codemod | 作用 | 依据 |
|---|---|---|---|
| 1 | `icons` | `lucide-react` 导入改为 `@/ui/icons/lucide-compat`（ICON-01；ADR-030） | g07 §3.1 |
| 2 | `cn` | `cn` 统一来自 `@/lib/utils`（createCn 登记项目 @theme 键，CN-01） | g07 §3.4 |
| 3 | `motion` | 删除 18 个 slot 的内置 `animate-in/out`、`data-[state]` 动画类、`backdrop-blur`、`supports-backdrop-filter:*`、任意 `[transition:...]`；`duration-N` 映射为时长 token，`ease-[cubic-bezier]` 映射为 `ease-smooth-out`；sheet 去掉 `shadow-lg`。动效改由 `styles/motion/base-ui.css` 的 transitions.dev 配方驱动 | g07 §4；ADR-029 |
| 4 | `colors` | Tailwind 命名色（`bg-black/NN`、`bg-white`、`text-white`）改为语义 token（VIS-L-02） | 15 §13 |
| 5 | `z-layers` | `z-50` 改为 `z-(--z-dialog)`、`z-(--z-popover)`、`z-(--z-toast)`（14 §2.1 层级表） | 14 §2.1 |
| 6 | `destructive-alpha` | destructive 底色透明度统一为 /10（交互态 /15），一红原则 | 15 §3.7 |
| 7 | `sidebar-overlay` | Sidebar 改为浮层：去掉 cookie 与内置 Mod+B 监听（热键由 `ui/hotkeys` 独占）、gap 宽度为 0、容器绝对定位，画布不随侧栏改变尺寸 | 14 §3.3；M15-FR-013 |
| 8 | `accordion-single-chevron` | Accordion 只保留一个 chevron，展开时旋转 | g07 §5 |
| 9 | `tabs-indicator` | Tabs 两种 variant 都用 Base UI Indicator（line 变体去掉 after 下划线），新增 `TabsPanels` | g07 §5；transitions.dev 16 |
| 10 | `toggle-group-indicator` | ToggleGroup 滑块指示器（`indicator` 属性，默认开启；首次定位 `data-instant`） | g07 §5 |
| 11 | `popover-anchor` | Popover 支持 `anchor`（画布内锚点弹层） | g07 §5 |
| 12 | `scroll-area-ts6133` | 删除未使用的 `React` 命名空间导入（TS 7 `noUnusedLocals`） | d04 §6 第 2 条 |
| 13 | `use-mobile` | `useIsMobile` 改为 `useSyncExternalStore`（上游在 effect 内同步 setState，oxlint react/set-state-in-effect；首帧值错误） | 本包新增 |
| 14 | `font-heading` | 删除 `font-heading` 类：CLI 只在 tailwind.css 自身含 `--font-heading:` 时写入该类，init 与 add 两条路径结果不同；AWR 只有一个无衬线字体族，删除后两条路径一致 | 本包新增 |

生成物（不是 codemod，但同属安装链）：`gen-motion-tokens.py`（transitions.dev token）、`inline-shadcn-tailwind.mjs`
（`#000` 改为 `oklch(0 0 0)`、`1ms` 改为 `var(--duration-epsilon)`、去掉 shimmer 段）、`gen-theme-tokens.mjs`
（`lib/tokens/*.gen.ts`）、`icons/gen-registry.mjs`（图标注册表与 morph 白名单）、`gen-reasons-i18n.mjs`（原因码文案）。
全部支持 `--check`。
