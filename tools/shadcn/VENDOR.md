# tools/shadcn 第三方来源登记（M15-FR-081..085；ADR-028..031、ADR-037）

本目录与 `apps/web/src/ui/components/ui/**` 中的第三方代码与数据来源。升级任一项时：更新本表，重跑
`node tools/shadcn/install.mjs`（离线）与 `make lint-m15`，并在 `PATCHES.md` 核对 codemod 锚点。

| 来源 | 版本或快照 | 用途 | 位置 |
|---|---|---|---|
| shadcn/ui 仓库 `refs/design/ui` | `984f435`（2026-09-28） | 组件源码与 base-mira 样式的参照 | 只读参考 |
| shadcn CLI | 4.21.0（`node_modules/shadcn`） | `init` 与 `add`，只由 `install.mjs` 调用 | `install.mjs` |
| registry 镜像（L2） | 由 `mirror.sh` 于 d04 调研时抓取，base-mira 59 项 + 4 张颜色表 + `/init` 载荷 | 离线安装（d04 §3.9） | `registry-mirror/` |
| @base-ui/react | 1.8.0 | 组件底座（`ui/components/ui/**` 以外禁止直接 import，TS-BND-01） | npm |
| cmdk | 1.1.1 | Command 组件 | npm |
| react-resizable-panels | 4.14.1 | Resizable（RailHost） | npm |
| lucide | 1.48.0（无 React 包装） | 图标几何（IconNode），经 `ui/icons` 语义注册表 | npm |
| morphicons | 1.7.1 | 白名单图标 morph（K = 8） | npm |
| cn | 0.4.0 | `createCn` 类名合并 | npm |
| tailwindcss | 4.3.3 | 样式；`shadcn/tailwind.css` 经 `inline-shadcn-tailwind.mjs` 内联为 `styles/shadcn-tailwind.css` | npm |
| transitions.dev `refs/design/transitions.dev` | `e2d5551`（2026-09-21） | 32 个配方的变量组与时长、缓动 token（`gen-motion-tokens.py` 生成 `styles/motion/tokens.css`） | 生成物 |
| lieflat-charts `refs/design/lieflat-charts` | `eace082`（2026-09-05） | 图表视觉语言（F1/F2/F5/F11、G17 画布流式线、table.log） | `ui/lf/**` 重写实现 |
| ANet 品牌 `refs/design/ANet` | 本地仓库快照 | `anet-logo.svg` 逐字节复制；头像 s=96、s=460 由 `brand/fetch-brand.sh` 下载，sha256 见 `apps/web/public/brand/brand.lock.json` | `apps/web/public/brand/` |

## 镜像裁剪

`registry-mirror/` 不包含：8 个禁用组件（chart、sonner、drawer、carousel、calendar、navigation-menu、pagination、
input-otp，M15 §6.1）、演示 block（dashboard-01、sidebar-07/15/16）与 `r/colors/index.json`。CLI 4.21 只按
`r/colors/<baseColor>.json` 取颜色表（`neutral.json` 必须保留，d04 §3.9），基色列表是 CLI 内置常量；被裁剪的文件
含十六进制颜色与 emoji，会被 D1-AC-20 的仓库扫描（VIS-L-01、EMOJI-01）拒绝。`mirror.sh` 已按同一规则跳过它们。

## 依赖规则

- CLI 运行期间向 `apps/web/package.json` 临时加入 `lucide-react`（让 CLI 的"已安装则跳过"生效），结束后逐字节恢复
  `package.json` 与根 `package-lock.json`；CLI 从不执行 npm install。组件需要的依赖全部已由 M00 声明。
- `tsconfig.json`、`vite.config.ts`（M00）以及 `styles/index.css`、`lib/utils.ts`、`components.json`（M15）在运行后恢复。
