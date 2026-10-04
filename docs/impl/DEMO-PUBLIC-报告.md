# DEMO-PUBLIC 公开演示模式与 emax 部署套件

| 项 | 内容 |
|---|---|
| 工作包 | DEMO-PUBLIC：前端公开演示构建（`VITE_AWR_DEMO=public`，落地页与只读沙盘）、后端公开访问模式（`AWR_ACCESS_MODE=public` 与 public 运行 profile）、剧本 S0 循环运行、emax 部署套件 `tools/deploy/emax/`、本机验证与文档 |
| 日期 | 2026-10-03 |
| 依据 | 任务书 DEMO-PUBLIC；D1 验收报告第 5 轮（§1、§4.2a、§4.3）；D1 交付总结；AWR-03（附录 E 至 ADR-081，§7.0 索引）；AWR-17 §3；AWR-19；M11、M15、M16 PRD 与实现报告；ADR-034、ADR-053、ADR-056、ADR-077、ADR-079 |
| 环境 | VMware 虚拟机 8 vCPU（Xeon E5-2603 v4 1.7 GHz）、无 GPU；Node 22.12；Python 3.12.3（`.venv`）；Chrome for Testing 151（SwiftShader，Tier S）；Playwright 1.63；`worlds/synthcity` 已生成。本阶段另有工作包 FX-TOAST 并行修改前端与 03 号文档 |
| 约束执行 | 没有安装依赖（模拟安装时 uv 只用本机缓存离线建 venv）；没有执行 git 写操作（本机工作树没有可用的 git 元数据，`git rev-parse` 失败，部署包版本号记为 `nogit`）；构建持共享性能锁；没有连接 emax（服务器上的部署由人工按 README 执行）；颜色只用 token、UI 只用 shadcn、表格用 lieflat 皮肤、动效用 transitions.dev token、图标用 morphicons；`make lint` 通过 |
| 规格变更 | 新增 AWR-03 ADR-082 至 ADR-085（附录 E 正文与 §7.0 索引；任务书写"从 ADR-079 起"，实际 ADR-079、080 已被 FINAL-REVIEW 占用、ADR-081 被并行的 FX-TOAST 占用，故顺延）、§8.5 运行形态表一行；AWR-17 §3.1–§3.4、§3.6、§4.3.1、§4.3.12、§5.1；AWR-19 新增 §3.7 T4 与 OPS-FR-050 至 054、OPS-AC-030 至 032，修订 §3.1、§5、§6.2、§6.3、§12.3；AWR-16 §12.1 `on_complete`；AWR-14 §2.2；M11 新增 FR-120 至 124、AC-060 至 062、§6.10 第 5 条；M15 新增 §4.17（FR-120 至 124）、§6.16、AC-070 至 073，补充 FR-003、037、110；M16 新增 FR-080、AC-050，§6.4.9 profile 行；M10 新增 FR-090、AC-050；M08-FR-008 补充 |

## 1 结论摘要

| 任务 | 结果 | 证据 |
|---|---|---|
| 1 前端演示构建 | 完成。`VITE_AWR_DEMO=public` 编译期开关（与测试开关互斥）；`/` 为落地页，首屏只加载运行时、react、landing 三个分块，不加载 three；演示构建只注册沙盘、世界列表与设置路由，隐藏重建任务、录制、回放与 GPU 自检入口；访客为 viewer，只读原因统一为"公开演示为只读模式"（shadcn Tooltip），不出现申请控制；标题与关于注明演示站；不发布 `dist/bench` | §2；Playwright 27 项断言全部通过（§6.3） |
| 2 后端公开模式 | 完成。访问模式 `public`：匿名只签 viewer，operator 与 admin 只认 `AWR_ADMIN_SECRET_FILE`（缺省一律 403 `115`）；Origin 只认站点域名；Host 白名单含域名与 127.0.0.1；可信代理之后按 `X-Forwarded-For` 计客户端地址（只用于限流与日志）；世界白名单（静态 `/worlds` 同样 404）；功能族 404、viewer 写入 403；按地址限流、WS ≤ 64、单地址 ≤ 4；public profile 只起 sim-core 与 api（recorder、agent-runtime、job-worker 关闭） | §3；`tests/rt/test_public_mode.py` 11 例；curl 21 项（§6.2） |
| 2a 剧本 S0 循环 | 完成。实现契约已有的 `on_complete = reset`：结果发出 8 s【仿真】后在步边界外重置并从头重开，每轮天气序列循环左移一位；S0 新增 `public` 剧本 profile。顺带修复 `sim/reset` 后 RTF 统计为负使 sim-core 崩溃的既有缺陷 | §4；`test_loop_reset_rotates_weather`；本机实跑（§6.4） |
| 2b 资源 | 机群 7 架；本机稳态 sim-core 0.26 核（目标 ≤ 0.5）、api 0.04 核、常驻内存合计约 0.6 GB；systemd 上限 CPUQuota 200%、MemoryMax 2G | §6.5 |
| 3 部署套件 | 完成。`tools/deploy/emax/`：中文 README、nginx 站点文件与 80 端口引导版本、systemd 单元、`env.example`、幂等 `install.sh`（含失败自动回滚、`--rollback`、`--status`，不碰 nginx）、`build-local.sh`（含合规复核）；`make demo-build`、`make deploy-pack` | §5；`shellcheck` 无告警；本机模拟安装（§6.1） |
| 4 本地验证 | 完成。按部署包在本机模拟安装并以单元的 ExecStart 启动（127.0.0.1:18640）：curl 21 项、WS 写入拒绝、Playwright 落地页到沙盘全流程通过；截图 `.cache/impl/shots/demo-*.png` 已逐张查看 | §6 |
| 5 文档 | 完成。19 §3.7 T4、17 §3 访问模式表、M11/M15/M16/M10/M08 相应小节、03 ADR-082 至 085 | 规格变更行 |
| 测试与门禁 | `make lint` 通过；前端 `tsc --noEmit`、`oxlint --type-aware` 无报错，Vitest unit 121 个文件 961 例通过；后端相关套件见 §7 | §7 |

## 2 前端公开演示构建（ADR-083）

### 2.1 开关与入口

- `apps/web/src/lib/demo.ts`：`DEMO_PUBLIC = import.meta.env.VITE_AWR_DEMO === 'public'`（常量折叠），以及演示世界、入口、路由与隐藏动作表、仓库与文档链接、`demoWorlds()`。
- `apps/web/vite.config.ts`：`VITE_AWR_DEMO` 只允许 `public`，与 `VITE_AWR_TEST_SWITCHES=1` 同用直接抛错；演示构建启用 `awr-demo-public` 插件：把 `index.html` 的入口换成 `src/app/demo/entry.tsx`、标题换成 "ANet Drone4D · Public Demo"，在 `<head>` 内联一行脚本给 `/` 打 `data-landing`（启动遮罩在首帧前就不绘制）；`closeBundle` 删除 `dist/bench`（UrbanScene3D 城市的 flight60 夹具），把 6 张合成城市截图与 `demo-flight.webp` 从 `docs/media` 复制到 `dist/demo`。头图按任务书复制到 `apps/web/public/demo/drone4d-banner.jpg`（512 KB，默认构建也会带上这一张，但不引用）。
- 分块：演示构建在 `ui` 组之前插入 `landing` 组，只捕获落地页实际用到的模块（含 Vite 预加载辅助模块）。第一次构建时落地页经 `ui` 分块静态导入 three（`index.html` 预加载了 three、r3f、ui）；先把组扩到整个 `ui/components` 与 `ui/icons`（`ui-kit` 1.07 MB），再收窄到精确模块并捕获预加载辅助模块后，落地页的静态依赖只剩运行时、react 与 landing（脚本约 156 KB gzip、样式约 30 KB gzip）。
- `src/app/demo/entry.tsx`：`/` 调用 `renderLanding()`，其余路径 `import('../../main')`。

### 2.2 落地页（`src/app/demo/Landing.tsx`）

顶栏（头像、产品名、"公开演示"徽标、中英 `ToggleGroup`、GitHub、"进入演示"）→ 头图与"从真实世界，到无人机集群。"、一句话定位、主按钮"进入演示 / Launch demo"（`/world/synthcity`，整页跳转进入带遮罩的正常启动）、GitHub 仓库与设计文档外链、提示行 → 当前版本的真实画面（S0 动图与 6 张截图，懒加载）→ 四个功能要点（渐进加载与疏密自动调节、4D 环境场、多机仿真与安全、ANet 协作）→ 演示说明（公开只读、synthcity 程序生成、未含 GPU 重建等服务端重计算功能、性能取决于访客 GPU）与 lieflat 皮肤的"本站运行的内容"表 → 页脚（仓库、文档、ANet、许可与版权行）。全部文案在 zh-CN 与 en 词典（`landing.*`、`demo.*` 共 58 键，两份都有译文）；语言选择记在 `localStorage`（`awr.demo.lang`），缺省按浏览器语言。颜色只用 ANet Graphite token（顶栏原用 `backdrop-blur`，被 VIS-L-03 拦下后改为实色），入场用 `brand-enter` 配方并按 `--duration-stagger` 错开。

### 2.3 沙盘的演示改动

| 方面 | 改动 | 文件 |
|---|---|---|
| 路由 | 只注册 `root`、`world`、`worlds`、`settings`；`/world/:id` 只接受 synthcity；应用内 `/` 进入 `/world/synthcity`；设置路由固定到 synthcity | `app/router/table.ts`、`app/routes/{index,world,settings}.tsx` |
| 菜单与动作 | 去掉重建任务、录制列表、回放录制、GPU 自检；帮助增加"演示首页"；面包屑 run 不链接；不登记 `jobs.open`、`replay.runs`；书签动作禁用并给出只读原因 | `ui/layout/AppHeader.tsx`、`ui/actions/builtin.ts` |
| 只读 | token 一律按 viewer；i18n 别名把 4 个只读原因键与 `role.viewer`、`hint.liveNoRewind` 改指演示文案（键不变，守卫与 Tooltip 无需分支）；顶栏只读徽标带 Tooltip；详情页、设置账户页、顶栏都没有"申请控制"；环境面板的预设与滑块、时间轴"添加书签"在只读时以 Tooltip 说明 | `app/i18n/index.ts`、`net/api.ts`、`ui/panels/{drone-detail,settings,env,timeline}/*` |
| 数据 | 世界列表只保留 synthcity；时间轴不探测录制、不读服务端书签（此前每次进入沙盘有 2 个 404） | `app/query/options.ts`、`stores/timeline.ts` |
| 标识 | 标题"<视图> · ANet Drone4D 公开演示"；关于区块最前增加"演示站"一行 | `app/App.tsx`、`ui/views/AboutDialog.tsx` |

### 2.4 顺带修复：设计样例页的无主分块

`app/routes/design.tsx` 在 `TEST_SWITCHES` 守卫之后懒加载 `DesignSample`，普通生产构建仍产出一个无人引用的 `DesignSample-*.js`（与 ADR-079 第 6 条所述 `featMatrix` 同类；该文件名不在 `prod-bundle-scan` 的禁用标识表中，所以此前没被发现）。改为内联 `import.meta.env` 条件后，默认构建与演示构建都不再产出该分块。默认构建的其余分块结构不变（私有目录对比构建：index、engine、r3f、react、three、ui），且不含任何演示标识。

## 3 后端公开访问模式（ADR-082）

| 组件 | 改动 |
|---|---|
| `awr/api/public.py`（新增） | 纯函数：`client_ip()`（可信代理与 `X-Forwarded-For`）、`world_of_path()`、`public_policy()`（功能族 404、只读写入 403）、`public_categories()`（按地址限流类别） |
| `awr/api/settings.py` | `access_mode` 增加 `public`；字段 `trusted_proxies`、`worlds_allow`、`max_conns_per_ip`；`origin_allowed()` 在公开模式只认 `AWR_ORIGINS`；`from_env()` 读 `AWR_TRUSTED_PROXIES`、`AWR_WORLDS_ALLOW`、`AWR_WS_MAX`、`AWR_WS_MAX_PER_IP`，公开模式的管理口令只取 `AWR_ADMIN_SECRET_FILE`（不读 `admin.token`） |
| `awr/api/middleware.py` | 客户端地址写入 `state.client_ip` 并用于审计与限流；世界白名单（Host 与 Origin 之后）；公开模式策略与三个按地址的令牌桶 |
| `awr/api/rest/auth.py` | 公开模式未配置口令时 operator 与 admin 403 `115`（`PUBLIC_READONLY`）；`access_mode` 枚举增加 `public` |
| `awr/api/rt/ws.py`、`gateway.py`、`session.py` | WS 按客户端地址计数（`conn_count_ip`）与上限；审计来源为客户端地址 |
| `awr/api/rest/worlds.py`、`sys.py`、`main.py`、`static.py` | 世界列表与详情按白名单过滤；公开模式下 `/api/sys/info` 细节只给 admin、不暴露 OpenAPI；静态前缀 `/demo/**` |
| `awr/api/ratelimit.py` | 类别 public_api（20/s，突发 120）、auth（0.5/s，突发 10）、world_query（20/s，突发 40） |
| `awr/runtime/config.py`、`supervisor.py` | `net.access_mode` 增加 `public`（必须回环、必须有 origins）；`net.trusted_proxies`、`worlds_allow`、`ws_max`、`ws_max_per_ip`；`defaults.standby` 总开关；对应环境变量；supervisor 注入新键（含此前只靠继承的 `AWR_ALLOWED_HOSTS`），公开模式不生成随机口令、READY 行不打印口令 |
| `configs/runtime.yaml` | 新键与 profile `public`（core 进程、无热备用与钉核、synthcity 与 S0 的 public 剧本 profile、`fallback_world: null`、18640 与 18647、可信代理回环、世界白名单 synthcity、WS 64 与 4、runs 配额 2 GB） |
| `packages/contracts/rest/openapi.snapshot.json` | 重新生成（`TokenResponse.access_mode` 增加 `public`） |

回环与局域网模式行为不变：新键的缺省值等于原值，新分支只在公开模式或显式配置时生效（`tests/rt/test_access.py`、`test_limits.py` 等照常通过）。

## 4 剧本循环（ADR-084）

- `awr/sim/mission/director.py`：`_check_end()` 在 `on_complete = reset` 时记下 `loop_at_ns = t + 8 s`；`stage()` 在 `done` 阶段到期后只调用一次 `M10Runtime.request_loop_reset()`。
- `awr/sim/mission/runtime.py`：`request_loop_reset()` 调 `SimCore.request_reset("scenario_loop", {"loop_index": k + 1})`；`on_reset()` 记下轮次（人工重置归零），`load_scenario()` 对循环剧本按轮次 `rotate_weather`。
- `awr/sim/mission/scenario_loader.py`：`rotate_weather(doc, k)`（初值加按时刻排序的 `env.preset` 事件，预设名循环左移 k 位，深拷贝）。
- `awr/sim/runtime/main.py`：`request_reset()` 把请求放入步边界 inbox，`_dispatch` 走与 `sim/reset` 相同的 `reset()`；**缺陷修复**：`reset()` 清零 RTF 窗口起点，`_write_step_stats()` 的 RTF 不小于 0。修复前重置后第一次 1 Hz 统计为负，`set_step_stats` 写 u32 抛 OverflowError，主循环崩溃并被 supervisor 重启（任何 `sim/reset` 都会触发；首个循环用例在重置后 1 s 复现）。
- `awr/datasets/scenarios/authoring.py` → `scenarios/s0-synthcity-showcase.json`：新增 `public` profile `{rate: 1, record: false, on_complete: reset}`（`python -m awr.datasets.scenarios generate` 重写，只改这一个文件）。

## 5 部署套件 `tools/deploy/emax/`（ADR-085）

| 文件 | 要点 |
|---|---|
| `README.md` | 中文部署手册：硬性约束（只新增本站点、只提供 synthcity、公开只读、机密只在服务器生成）、前置条件表、首次部署（打包、上传安装、nginx 与证书、验证）、更新、回滚、运维（日志位置、常用操作、PyPI 镜像）、卸载 |
| `drone4d.agentnetwork.org.cn.conf` | 80：ACME（`/var/www/certbot`）+ 301；443：`listen 443 ssl http2`（nginx 1.24 写法）、证书 `/etc/letsencrypt/live/drone4d.agentnetwork.org.cn/`、TLS 1.2/1.3、HSTS（不带 includeSubDomains）、upstream `127.0.0.1:18640`、`/api/rt` 升级与 3600 s 超时、`proxy_buffering off`、gzip 只压文本类、`.bin` 等二进制不压缩、缓存头透传、`client_max_body_size 1m`、`limit_req_zone`/`limit_conn_zone`/upstream/map/SSL 会话缓存都带 `drone4d` 前缀；请求头只在 server 级设置（location 内一旦出现 `proxy_set_header` 就不再继承） |
| `drone4d.agentnetwork.org.cn.bootstrap.conf` | 只含 80 端口：ACME 校验，其余 503 |
| `anet-drone4d.service` | 用户 awr、`WorkingDirectory=/opt/anet-drone4d`、`EnvironmentFile=/etc/anet-drone4d/env`、`Restart=always`、`CPUQuota=200%`、`MemoryMax=2G`、`Nice=5`、`NoNewPrivileges`、`ProtectSystem=full`、`ProtectHome`、`PrivateTmp`、journal、`KillMode=mixed` |
| `env.example` | 全部可配置项与说明，不含机密（管理口令由 `install.sh --admin-secret` 生成到 `/etc/anet-drone4d/admin.secret`） |
| `install.sh` | root 执行、幂等：包校验（`MANIFEST.sha256`、演示构建标记、无 `dist/bench`）→ 用户与目录 → uv 0.12.19 自举 → `releases/<release>`（root 所有）→ uv 按 `requirements.lock` 建 `.venv`，awr 源码经 `.pth` 加入 `sys.path`（不在只读源码树写 egg-info）→ 字节码与 numba 预热 → 世界（解开世界包，只允许 synthcity、`_shared/env` 与 `.status/synthcity.json`；否则在服务器生成）→ 首次生成 `env` → 可选管理口令 → 单元、切换、健康检查（90 s 不就绪自动回滚）→ 保留 3 个发布 → 打印 nginx 与 certbot 命令；`--rollback [release]`、`--status` |
| `build-local.sh` | 演示构建（持共享性能锁）与生产包扫描；复制 `python/`、`packages/contracts/`、`configs/`、`scenarios/`、`vehicles/`、`tools/deploy/` 与许可文件；复核无 `worlds/`、`data/`、`refs/`、`.cache/`、`runs/`、点云文件、超过 5 MB 的文件与 `~/.config/awr/*.env` 中的凭据；`RELEASE` 与 `MANIFEST.sha256`；`--with-world` 附带校验过 `dataset.redistribution` 与构建状态的 synthcity 世界包 |
| `mk/deploy.mk` | `make demo-build`（演示构建到 `.cache/deploy/emax/web-dist` 并扫描）、`make deploy-pack [WORLD=1]` |

模拟安装中发现并修正两处：①`install.sh` 原先另以种子 0 调用 `ensure_turb_box`、`ensure_weather_map` 生成共享环境资产，而 sim-core 用自己的派生种子（文件名 `vk_s1095877177_*`），种子 0 的文件永远不会被读取；已删去这一步（资产随世界包提供，没有时 sim-core 首次启动生成，本机实测约 1.3 s）。②世界包最初只含 `synthcity/` 与 `_shared/env`，服务器上的目录服务因为没有 `.status/synthcity.json` 把世界报为"待重建"（世界列表的"打开"按钮禁用）；现在世界包带上该状态记录，`build-local.sh` 校验它为 ready 且版本一致，`install.sh` 解到 `var/worlds/.status/`，随后的 `worldpkg build synthcity --missing` 判定 up to date（0.0 s）。

## 6 本地验证

### 6.1 按部署包模拟安装

`build-local.sh --with-world`：20 s，部署包 14 MB、世界包 63 MB，合规复核通过。按 `install.sh` 的布局在 `.cache/deploy/emax/sim-opt/` 模拟（共做了两次，第二次用最终代码重新打包、全新目录；最终部署包与第二次模拟的发布只差 `tools/deploy/emax/README.md` 与 `install.sh` 两个文件的文字与 §5 末段所述的一处删减）：解包到 `releases/<release>` 并核对 `MANIFEST.sha256`、`current` 符号链接、uv 离线建 `.venv`（本机缓存缺 uv 自身的 wheel，模拟时从锁中去掉 uv 一项；服务器上联网安装不受影响）、`.pth`、字节码、numba 预热 95 s、解开世界包、按 `env.example`（路径换成模拟目录，`AWR_ORIGINS` 追加 `http://127.0.0.1:18640` 供本机浏览器用）与单元的 ExecStart 启动。READY 后 1 s 内 `/api/health/ready` 就绪。

### 6.2 curl（站点 Host 与 Origin，`.cache/impl/shots/demo-curl.txt`）

| 请求 | 结果 |
|---|---|
| `GET /`（落地页）、`GET /world/synthcity`、`GET /worlds/synthcity/world.json`、`GET /demo/drone4d-banner.jpg` | 200 |
| `GET /worlds/shenzhen/world.json`、`GET /api/worlds/shenzhen`、`GET /bench/flight60/shenzhen.json`、`GET /api/openapi.json` | 404 |
| `GET /api/health/live`（Host attacker.example） | 400 |
| `GET /api/sys/info`（Origin https://evil.example） | 403 |
| `POST /api/auth/token` viewer / operator（无口令）/ admin（猜测口令） | 200 / 403 / 403 |
| viewer：`POST /api/commands`（land）、`/api/env/preset`、`/api/fleet/vehicles` | 403 |
| viewer：`POST /api/sys/perf-report`、`GET /api/runs`、`GET /api/jobs` | 404 |
| viewer：`POST /api/env/query`（只读查询） | 200 |
| viewer：`GET /api/worlds` | `['synthcity']` |

WS（Origin 为站点域名）：serverInfo 角色 viewer、席位 none；`uav/p600-h1/cmd/land`、`env/preset`、`sim/pause`、`seat/takeover` 全部 `rejected 115`；Origin `http://127.0.0.1:9999` 与 `https://evil.example` 的升级 403。

### 6.3 Playwright（Chrome 151 SwiftShader，1600 × 900，`.cache/demo/pw-demo.mjs`）

全部 27 项断言通过（其余读数记在 `demo-pw-log.json`），无 pageerror、无 console.error：落地页只请求 4 个脚本分块（入口、运行时、react、landing），启动遮罩不绘制；标题、按钮、演示说明的 4 个要点；英文切换后按钮为 "Launch demo"、`<html lang="en">`；点击"进入演示"约 17 s 揭开，加载了 three；点云上屏（Tier S 1–2 万点）、机群列表 7 架、无人机图层有绘制；标题"synthcity · ANet Drone4D 公开演示"；顶栏"公开演示 · 只读"与 Tooltip、环境面板 Tooltip、详情页说明都是"公开演示为只读模式"；页面中没有"申请控制"；工具（性能面板、事件日志、图表、时间轴）、帮助（快捷键、演示首页、项目仓库、关于）、仿真菜单中没有重建、录制与 GPU 自检；`/jobs` 回到世界列表且只有 synthcity，卡片为就绪（"打开"可用）。

截图（逐张查看过）：`demo-landing.png`（首屏）、`demo-landing-media.png`、`demo-landing-features.png`、`demo-landing-full.png`、`demo-landing-en.png`、`demo-sandbox.png`（只读徽标 Tooltip）、`demo-readonly-env.png`（环境面板 Tooltip）、`demo-select.png`（详情页只读说明）、`demo-menu-tools.png`、`demo-worldhub.png`，均在 `.cache/impl/shots/`。

WS 写入与 Origin（`.cache/demo/ws-checks.py`，结果附在 `demo-curl.txt` 末尾）：见 §6.2 末段，最终部署布局上复测结果相同。

### 6.4 S0 循环实跑

第一次本机启动（开发目录、public profile）：`scenario.result` SUCCEEDED 于 285.8 s【仿真】，293.8 s 发 `sim.reset{reason: scenario_loop}`、epoch 2、时钟 PLAYING；第二轮环境关键帧在 45 s 的过渡目标为 `fog`（初值小雨），即左移一位；crash 目录为空，sim-core 没有重启。

### 6.5 资源（本机 1.7 GHz，30 s 窗口）

| 进程 | 无访客 CPU（开发目录 / 部署布局） | 1 个浏览器访客 CPU | 常驻内存（开发目录 / 部署布局） |
|---|---|---|---|
| sim-core | 0.259 / 0.260 核 | 0.245 核 | 235 / 252 MB |
| api | 0.036 / 0.043 核 | 0.052 核 | 93 / 94 MB |
| supervisor | 0.018 / 0.017 核 | — | 67 / 71 MB |
| 规划进程（plan-pool） | 0 / 0 | — | 192 / 193 MB |

合计约 0.32 核、0.59–0.61 GB（热备用关闭省下一个约 240 MB 的 sim-core 进程）。按每个访客约 0.02 核估算，64 个 WS 连接时 api 约 1.3 核，仍在 CPUQuota 200% 之内；emax 的 CPU 主频若高于本机，实际占用更低。

## 7 测试与门禁

| 项 | 结果 |
|---|---|
| `make lint` | 通过（含 ruff、oxlint、no-emoji、no-hex、lint-lf、motion-lint、check-icons、no-raw-controls、check-release 等；本机无 git 元数据，check-release 按设计提示后通过） |
| 前端 | `tsc --noEmit` 与 `oxlint --type-aware` 无报错；Vitest unit 121 个文件 961 例通过（1 例既有 skip），含新增 `tests/m15/demo.test.ts` 2 例与 `i18n.test.ts` 的键集对齐 |
| 后端（第一批） | `tests/mission`、`tests/e2e/test_scenarios_static.py`、`tests/scenarios`、`tests/rt/test_access.py`、`test_limits.py`、`tests/runtime/test_config.py`、`test_supervisor.py`：312 例通过（519 s） |
| 后端（第二批） | `tests/rt`、`tests/sim`、`tests/contracts`、`tests/environment`、`tests/e2e/test_synthcity_showcase.py`：838 例通过、2 例失败（`tests/environment/test_env_e2e.py` 的两例：WS 关键帧等待超时、`GET /api/env/state` 503；当时本机同时在跑部署包构建、numba 预热与 Playwright，load 约 4）；单独重跑该文件 2 例通过，收尾时与 `test_public_mode.py`、`test_config.py`、`test_director.py`、OpenAPI 快照一起再跑 44 例全部通过 |
| 新增用例 | `tests/rt/test_public_mode.py` 11 例；`tests/runtime/test_config.py::test_public_profile_for_demo_site`；`tests/mission/test_director.py::test_loop_reset_rotates_weather`；`tests/m15/demo.test.ts` 2 例 |
| 生产包扫描 | 演示构建与默认构建（私有目录）`prod-bundle-scan` 均 clean；默认构建不含演示标识 |
| 脚本 | `shellcheck`（本机已有的 koalaman/shellcheck 镜像）对 `install.sh`、`build-local.sh` 全部级别无告警 |
| OpenAPI 快照 | `gen_openapi.py` 重新生成后 `tests/contracts/test_openapi_snapshot.py` 通过 |

## 8 未完成与注意事项

1. **服务器部署未执行**：本包没有连接 emax；nginx 站点文件没有在 nginx 上做 `nginx -t`（本机没有 nginx，也不安装），已按 nginx 1.24 的语法逐项复核（`listen ... http2`、请求头只在 server 级设置、区域名唯一）。首次部署请严格按 README 2.3 先 `nginx -t` 再 reload。
2. **Vitest browser 项目**：本包改动的组件（顶栏、环境面板、时间轴面板）在演示构建以外的分支不变；browser 项目未全量重跑，Playwright 已在真实构建上覆盖演示分支。
3. **局域网模式的既有现象**（未改，供后续参考）：`net/api.ts` 缺省申请 operator，局域网模式下无口令时服务端返回 401，`getToken()` 只对 409 回退 viewer，因此局域网观众可能拿不到 token。公开构建一律申请 viewer，不受影响。
4. **并行冲突**：FX-TOAST 同时修改了 `App.tsx`、`main.tsx` 与 03 号文档；本包的改动在这些文件中与之共存（逐处核对过），ADR 编号因此顺延到 082–085。
5. 落地页在本机 SwiftShader 下的沙盘帧间隔约 0.2–0.3 s（Tier S），真实访客取决于其 GPU（演示说明已写明）。

## 9 产出清单

- 前端：`apps/web/src/lib/demo.ts`、`apps/web/src/app/demo/{Landing,entry}.tsx`、`apps/web/public/demo/drone4d-banner.jpg`（新增）；`vite.config.ts`、`app/i18n/{index.ts,zh-CN.json,en.json}`、`app/router/table.ts`、`app/routes/{index,world,settings,design}.tsx`、`app/App.tsx`、`app/query/options.ts`、`net/api.ts`、`stores/timeline.ts`、`ui/views/AboutDialog.tsx`、`ui/layout/AppHeader.tsx`、`ui/actions/builtin.ts`、`ui/panels/{drone-detail,settings,timeline,env}/*.tsx`；`tests/m15/demo.test.ts`（新增）
- 后端：`python/awr/api/public.py`（新增）；`api/{settings,middleware,ratelimit,main,static}.py`、`api/rest/{auth,worlds,sys}.py`、`api/rt/{ws,gateway,session}.py`、`runtime/{config,supervisor}.py`、`sim/mission/{director,runtime,scenario_loader}.py`、`sim/runtime/main.py`、`datasets/scenarios/authoring.py`；`configs/runtime.yaml`；`scenarios/s0-synthcity-showcase.json`、`packages/contracts/rest/openapi.snapshot.json`（生成）；`tests/rt/test_public_mode.py`（新增）、`tests/runtime/test_config.py`、`tests/mission/test_director.py`
- 部署：`tools/deploy/emax/{README.md,build-local.sh,install.sh,anet-drone4d.service,env.example,drone4d.agentnetwork.org.cn.conf,drone4d.agentnetwork.org.cn.bootstrap.conf}`、`mk/deploy.mk`
- 文档：03（ADR-082 至 085、§7.0、§8.5）、14 §2.2、16 §12.1、17 §3 与 §4.3、§5.1、19（§2.1、§3.1、§3.7、§5、§6.2、§6.3、§12.3、§17）、M08、M10、M11、M15、M16 PRD
- 验证材料（不入库）：`.cache/impl/shots/demo-*.png`、`demo-curl.txt`、`demo-pw-log.json`；脚本 `.cache/demo/{run-public.sh,curl-checks.sh,pw-demo.mjs}`；部署包 `.cache/deploy/emax/`
