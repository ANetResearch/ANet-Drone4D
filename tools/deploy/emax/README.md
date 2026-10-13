# emax 部署手册：ANet Drone4D 公开演示站 V0.2-demo

本目录是把 ANet Drone4D 的公开演示站部署到服务器 emax（`https://drone4d.agentnetwork.org.cn`）的全部材料，对应
[AWR-19 §3.7 T4 公开演示站](../../../docs/19-部署与运维说明书.md)、ADR-082 至 ADR-085 与 D2 的 ADR-091、ADR-092、ADR-109、
ADR-118 至 ADR-123（[AWR-04](../../../docs/04-D2-设计增补与决策记录.md) §4.4、§4.7、§11.5、§15）。它是脚本目录，不是 Python 包，
不被任何代码 import。

V0.2-demo 在 D1 的共享只读展示之外增加了多租户沙盒、七个世界、版本号与载入期容量、观看排队与节能、移动端和内部身份。服务
端由一个 systemd 单元承载两组进程：共享展示（supervisor、show-api、共享展示 sim-core，剧本 S7）与沙盒（sandbox-pool、
sandbox-api、每个沙盒一个 sim-core）；nginx 按路径把 `/api/sandbox/` 分给 sandbox-api，其余 `/api/` 给 show-api，
`/worlds/` 由 nginx 直接读世界包目录。

| 文件 | 用途 |
|---|---|
| `build-local.sh` | 开发机：演示构建（含构建信息）、部署包、七个世界的世界包与派生缓存包 |
| `worldpack.py` | 世界包的打包、校验与安装（内容寻址，build-local.sh 与 install.sh 调用） |
| `install.sh` | emax 上以 root 执行：用户与目录、venv、numba 预热、世界包、env、内部身份口令、systemd 单元与配额、切换发布、健康检查、cgroup 委派报告、沙盒冒烟；`--rollback`、`--status`、`--secret-only`；`--dry-run` 在任何机器上本机模拟 |
| `anet-drone4d.service` | systemd 单元（awr 用户，CPUQuota 250%、MemoryHigh 1.8G、MemoryMax 2G、Delegate=yes，经 `cgroup-exec.sh` 启动 supervisor） |
| `cgroup-exec.sh` | 单元的 ExecStart 包装：在委派的 cgroup 下建立 `core/` 与 `sandbox/{ctl,plan,sb}` 并写入限值（AWR-04 §4.4.4；会话父组 `sandbox/sb/` 的 `cpu.max` = 沙盒组上限 − 控制面预留 0.22 核） |
| `env.example` | `/etc/anet-drone4d/env` 的模板，列出全部可配置项；不含任何机密 |
| `drone4d.agentnetwork.org.cn.conf` | nginx 站点文件：80 端口 ACME 与 301；443 端口 TLS、路径分流、`/worlds/` 直出、前缀限流、容量接口 1 s 微缓存、`/sdk/` |
| `drone4d.agentnetwork.org.cn.bootstrap.conf` | 首次申请证书用的只含 80 端口的引导站点文件 |
| `smoke.py` | 安装后的沙盒冒烟（创建 → READY → 结束），install.sh 调用 |
| `sbctl.sh` | 沙盒池运维命令（状态、列表、结束、维护、接受开关、临时降容） |
| `observe.py` | 线上观测（D2-AC-27）：72 h 服务判据（OOM、非预期重启、共享展示 RTF）、S7 七天统计（被发现、间隙按原因）与 emax 实测成本，见 5.7 |
| `nginx-check.sh` | 开发机上用一次性 Ubuntu 24.04 容器 `nginx -t` 站点文件并跑 nginx 对拍与端到端用例（含 D2-AC-42 的容量微缓存），证据写到 `--out`（`make deploy-nginx-check`，见 2.4） |

**硬性约束（每次操作前请再读一遍）：**

1. **只新增本站点，不得修改其他站点的配置。** emax 上已经运行 ANet 生产服务与 nginx 1.24：不改 `nginx.conf`、
   不改、不删、不重命名 `sites-available` 与 `sites-enabled` 中的其他文件，不动其他站点的证书。本站点文件中的
   `limit_req_zone`、`limit_conn_zone`、`proxy_cache_path`、`upstream`、`map` 变量与 SSL 会话缓存都带 `drone4d` 前缀，与其他站点
   不冲突。`install.sh` 从不 reload nginx，也不调用 certbot：这两步一律由人工按下文逐条执行，执行前先 `nginx -t`。
2. **七个世界经授权开放。** synthcity 与 UrbanScene3D 六城（深圳、苏州、纽约、芝加哥、上海、旧金山）在公开站全部开放：六城经
   数据集权利方授权使用（ADR-109，R-D2-28）。世界包与派生缓存只经部署包上传（`build-local.sh` 的世界包目录），**不进入 git
   仓库**，也不要在服务器上 `make fetch-data` 或从开发机整体拷贝 `worlds/`。flight60 测试夹具（`dist/bench`）在 WP-13 评估前仍不随
   演示构建发布。
3. **匿名访客的权限边界不变。** 匿名访客在共享展示上只有 viewer；沙盒只对领取者本人可写，资源受 N_max、世界内存准入与前缀
   配额约束。内部身份（全功能，ADR-123）只认服务器上的口令文件，入口在设置页底部折叠的"高级"区。不提供 GPU 重建、录制回放、
   智能体运行时等服务端重计算功能（扩展组按需启用为 P1）。
4. **机密不入库、不进包、不进日志。** 内部身份口令只由用户本人或其授权的运维在 emax 上输入（`--admin-secret-file`，见 5.4）；
   每个 run 的签名密钥与沙盒池的 `token.key`、`pool.secret` 都在服务器上自动生成。

## 1 前置条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| 服务器 | Ubuntu 24.04、x86-64、4 核、约 2.6 GB 可用内存、无 GPU；本站峰值约 1.75 GB（含可回收文件页，MemoryHigh 1.8G、MemoryMax 2G）、CPU 上限 2.5 核（CPUQuota 250%） | `uname -m; nproc; free -h` |
| 配额审批 | CPUQuota 250% 与 MemoryHigh 1.8G 需运维确认（Q-D2-01、Q-D2-09）；未获批时安装时加 `--cpu-quota 200% --memory-high 1.6G`（N_max 2） | — |
| 磁盘 | `/opt` 所在分区 ≥ 5 GB 可用（七个世界约 1.07 GB、派生缓存约 1.0 GB、保留 3 个发布、runs 配额 2 GB）；上传的世界包目录另需约 1 GB | `df -h /opt ~` |
| cgroup | cgroup v2（Ubuntu 24.04 缺省），systemd 支持 `Delegate=yes`；否则沙盒按 N_max − 1 运行 | `stat -fc %T /sys/fs/cgroup`（应为 `cgroup2fs`） |
| Python | 3.12（系统自带 3.12.3）与 venv 模块 | `python3.12 --version; sudo apt install python3.12-venv` |
| 工具 | `rsync`、`curl`、`systemd` | `command -v rsync curl systemctl` |
| 网络 | 首次安装与依赖变化时要能访问 PyPI（或镜像，见 6.3） | `curl -sI https://pypi.org/simple/ \| head -1` |
| DNS | `drone4d.agentnetwork.org.cn` 已解析到 emax | `dig +short drone4d.agentnetwork.org.cn` |
| nginx 与 certbot | nginx 1.24 已在运行，certbot 已安装，webroot `/var/www/certbot` 已存在（证书为 ECDSA）；`/var/lib/nginx` 存在（容量接口微缓存目录建在其下） | `nginx -v; certbot --version; ls -d /var/www/certbot /var/lib/nginx` |
| 端口 | 127.0.0.1:18640（show-api）、18641（sandbox-api）与 18647（进程间总线）未被占用；不开放新的公网端口 | `ss -ltn \| grep -E ':1864[017]'` |
| 开发机 | 已 `make setup`（Node 22、`.venv`）；七个世界已构建且派生缓存可用（`make worlds`、`make demo-world`、`make geo-warm`、`make geo-check`） | `make deploy-pack` |

## 2 首次部署

### 2.1 开发机：打包

```bash
make worlds-dataset              # 只重写六城 world.json 的数据集说明（ADR-109；contentVersion 与派生缓存不变；make worlds 之后会自动执行）
make deploy-pack                 # = tools/deploy/emax/build-local.sh --worlds all
make deploy-dry-run              # 本机模拟 install.sh：校验部署包、构建信息、世界包与派生缓存（deep），不需要 root
make deploy-nginx-check          # 可选：站点文件有改动时，在一次性容器里 nginx -t 并跑 nginx 对拍用例（需要 docker）
```

产物在 `.cache/deploy/emax/`：

| 产物 | 内容 |
|---|---|
| `anet-drone4d-<release>.tar.gz` | 代码、配置、剧本、机型、演示前端（含 `build-info.json`）、`tools/deploy/`、`tools/sdk/`、许可与通知文件；顶层 `RELEASE`（构建信息：release、version、channel、commit、dirty、built_at）与 `MANIFEST.sha256`（约 15 MB） |
| `worlds/` | 每个世界一个世界包 `world-<id>-<contentVersion>-<world.json 摘要>.tar.gz`（含构建状态）与一个派生缓存包 `geo-<id>-<cv>-<derive_sha8>.tar.gz`（含 Grid25），共享环境资产 `shared-env-<sha8>.tar.gz`，索引 `WORLDS.json` 与 `WORLDS.sha256`（七个世界合计约 1 GB，派生缓存压缩率高） |

**D2.2-W2 六城模拟监测展示**：`tools/sim/gen_city_watch.py` 与 `tools/sim/capture_city_watch.py` 生成并离线录制的
`worlds/<六城之一>/show/show.awrrt`（各约 3–4 MB）随世界包一并打包（`world-<id>-...tar.gz` 含 `<id>/**`）。录制不计入
`contentVersion`，因此 `WORLDS.json` 为每个世界另记录录制摘要 `show_sha256`，包名也带上它
（`world-<id>-<cv>-<wj8>-s<show8>.tar.gz`）：只换录制时会生成新包，`install.sh` 在已安装录制的摘要与索引不一致时重装该世界，
不需要手工删除旧包。

`<release>` 形如 `20261012-083000-3f9a1c2d4e`（UTC 时刻与提交；没有 git 时为 `nogit`，工作区有改动时带 `-dirty`），启动屏显示
`V0.2.0-demo · 3f9a1c2 · 2026-10-12`。世界包按内容版本与派生键命名：世界没变就复用同一个文件，rsync 只传新增的包。脚本会复核部署
包内没有 `worlds/`、`data/`、`refs/`、`.cache/`、点云文件、超过 5 MB 的文件、本机凭据与部署口令，前端是演示构建且通过生产包
扫描，`build-info.json` 与 `RELEASE` 是同一次构建，最后打印上传命令。只打部分世界：`make deploy-pack WORLDS=synthcity,shenzhen`；
不打世界包：`WORLDS=none`。ADR-109 之前构建的六城 world.json 仍写"科研用途"（公开站直出 world.json，世界面板照此显示），
`worldpack.py` 打包时会拒绝并提示先 `make worlds-dataset`。

### 2.2 上传并安装

```bash
rsync -avP .cache/deploy/emax/anet-drone4d-<release>.tar.gz emax:/tmp/
rsync -avP --delete .cache/deploy/emax/worlds/ emax:anet-drone4d-worlds/      # 首次约 1 GB；以后只传变化的包
ssh emax 'tar -C /tmp -xzf /tmp/anet-drone4d-<release>.tar.gz'
ssh -t emax 'sudo bash /tmp/anet-drone4d-<release>/tools/deploy/emax/install.sh \
  --bundle /tmp/anet-drone4d-<release> --worlds "$HOME/anet-drone4d-worlds"'
```

运维尚未批准 250% 与 1.8G 时，在最后一条命令后加 `--cpu-quota 200% --memory-high 1.6G`（写入 drop-in，以后的安装保留，
`--reset-limits` 删除）。启用内部身份见 5.4。

`install.sh` 依次：校验部署包（`MANIFEST.sha256`、演示构建、不含 `dist/bench`、`build-info.json` 与 `RELEASE` 一致、单元文件委派）→
创建系统用户 `awr` 与目录（`var/sandbox`、`var/identity` 为 0700）→ 自举 uv 0.12.19 → 复制到 `/opt/anet-drone4d/releases/<release>`
（root 所有，awr 只读）→ 按 `requirements.lock` 建该发布的 `.venv` → 预编译字节码，以共享展示与沙盒两套插件预热 numba 缓存
（约 2–3 min）→ 装入世界包（逐包 sha256 与成员校验，解到同一文件系统的临时目录后原子改名；内容版本与派生键相同者跳过；每个
世界保留当前与上一个派生缓存条目）→ **按新发布的代码 deep 校验**七个世界的派生缓存（键与 contentVersion、coordinate.sha256、
GeoParams、DERIVE_VERSION 一致，逐数组 sha256）与世界状态，失败即停止且不触碰正在运行的服务 → 首次安装时由 `env.example` 生成
`/etc/anet-drone4d/env`（0600）→ 安装 systemd 单元与配额 drop-in，切换 `current`，重启并等待 show-api 就绪（90 s 内不就绪自动
回滚到上一发布）→ 报告 cgroup 委派结论 → 沙盒冒烟（经回环创建一个 synthcity 沙盒、等 READY、结束；满员、需要工作量证明或前缀
配额用完时为"跳过"）→ 保留最近 3 个发布 → 打印 nginx 与 certbot 命令。

服务器上的布局：

| 路径 | 所有者 | 内容 |
|---|---|---|
| `/opt/anet-drone4d/releases/<release>/` | root | 代码、配置、演示前端、SDK、`RELEASE` 与该发布的 `.venv` |
| `/opt/anet-drone4d/current` | root | 指向当前发布的符号链接 |
| `/opt/anet-drone4d/var/worlds/` | awr，0751（与 `var/` 同为只可穿越，nginx 以 alias 直出 `/worlds/`） | 七个世界、`_shared/env/`、`.status/`（构建状态）、`.geo-cache/`（派生缓存，含 Grid25） |
| `/opt/anet-drone4d/var/sandbox/` | awr，0700 | 沙盒池：`token.key`、`pool.secret`、`state.json`、`audit.jsonl`、`runs/<sid>/`、`snapshots/`、`parked/<sid>/` |
| `/opt/anet-drone4d/var/identity/` | awr，0700 | 内部身份的锁定表与撤销表 |
| `/opt/anet-drone4d/var/runs/` | awr | 共享展示每次启动一个 run：子进程日志、审计、生效配置（配额 2 GB） |
| `/opt/anet-drone4d/var/cache/numba/` | awr | numba 编译缓存 |
| `/etc/anet-drone4d/env` | root，0600 | 运行环境（`env.example` 生成，之后不覆盖） |
| `/etc/anet-drone4d/admin.secret` | awr，0600 | 内部身份口令（`--admin-secret-file`）或随机管理口令（`--admin-secret`） |
| `/etc/systemd/system/anet-drone4d.service` | root | systemd 单元（每次安装覆盖） |
| `/etc/systemd/system/anet-drone4d.service.d/limits.conf` | root | 可选的配额 drop-in（`--cpu-quota`、`--memory-high`；安装时保留） |
| `/var/lib/nginx/drone4d-micro/` | www-data | 容量接口 1 s 微缓存（nginx 自动创建） |

### 2.3 nginx 与证书（人工执行）

```bash
# 1) 首次部署：引导站点（只有 80 端口：ACME 校验，其余 503），申请 ECDSA 证书
sudo install -m 0644 /opt/anet-drone4d/current/tools/deploy/emax/drone4d.agentnetwork.org.cn.bootstrap.conf \
  /etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf
sudo ln -sfn /etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf /etc/nginx/sites-enabled/drone4d.agentnetwork.org.cn.conf
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d drone4d.agentnetwork.org.cn --key-type ecdsa --elliptic-curve secp384r1

# 2) 正式站点（证书就绪后；从 V0.1 升级时同样执行这一步）
sudo diff /etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf /opt/anet-drone4d/current/tools/deploy/emax/drone4d.agentnetwork.org.cn.conf
sudo install -m 0644 /opt/anet-drone4d/current/tools/deploy/emax/drone4d.agentnetwork.org.cn.conf /etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf
sudo nginx -t && sudo systemctl reload nginx
```

站点文件的要点：

| 路径 | 去向 | 说明 |
|---|---|---|
| `/api/sandbox/**`（REST 与会话 WS `…/sessions/sb-…/rt`） | sandbox-api 127.0.0.1:18641 | 创建沙盒每地址前缀 2 次/min、突发 4；会话 WS 每前缀 4 条 |
| `/api/sandbox/v1/capacity` | sandbox-api，1 s 微缓存 | 无论访客多少、上游回复什么（含池不可达的 503），到达 sandbox-api ≤ 1 次/s：任何回复缓存 1 s、HEAD 与 GET 共用一份、冷缓存时其余访客等锁；副本由 nginx 以自己的身份回源（不带访客的查询参数与请求头，不带 X-Request-Id 与 Set-Cookie）；GET、HEAD 以外 405；响应头 `X-Drone4d-Cache` |
| 其余 `/api/**`、`/api/rt`、`/api/viewer/queue`、`/api/auth/identity` | show-api 127.0.0.1:18640 | 内部身份每前缀 0.5 次/s；观看排队与沙盒排队长轮询 ≤ 25 s |
| `/worlds/<七个世界>/**`、`/worlds/_shared/env/**` | nginx 直出 `var/worlds` | Range、ETag、304、缓存头（`?v=` 时 immutable，否则 no-cache）与安全头同 show-api；以点开头的路径（`.geo-cache`、`.status` 等）404 |
| `/sdk/**` | nginx 直出当前发布的 `tools/sdk/` | Python SDK 与示例 |
| `/build-info.json`、`/manifest.webmanifest` | nginx 直出当前发布的前端，no-cache | 构建信息与 PWA 清单 |
| 其余（前端、落地页媒体、SPA 回退） | show-api | — |

限流与连接数都按地址前缀计（IPv4 /32、IPv6 /64，`$drone4d_prefix`），精确的配额与聚合前缀由池与两个 api 执行。`nginx -t` 失败时
不要 reload，先看报错：最常见的是名字与其他站点重复（本文件已全部加 `drone4d` 前缀）、证书路径尚不存在（还在用引导站点之前
就换了正式站点）、`/var/lib/nginx` 不存在（`proxy_cache_path` 的父目录）。证书续期沿用 certbot 已有的定时任务。

### 2.4 验证

```bash
D=https://drone4d.agentnetwork.org.cn
curl -sI $D/ | head -1                                                          # HTTP/2 200（落地页）
curl -s $D/build-info.json                                                      # 与 /opt/anet-drone4d/current/RELEASE 同一 release
curl -s $D/api/sandbox/v1/capacity                                              # awr.capacity.v1；连续两次时第二次 X-Drone4d-Cache: HIT
curl -sI $D/api/sandbox/v1/capacity | grep -ci '^x-request-id'                  # 0（共享副本不带单个请求的标识）
curl -s -o /dev/null -w '%{http_code}\n' -X POST $D/api/sandbox/v1/capacity     # 405（不回源）
curl -s -o /dev/null -w '%{http_code}\n' $D/worlds/shenzhen/world.json          # 200（ADR-109）
curl -s -o /dev/null -w '%{http_code}\n' -H 'Range: bytes=0-99' $D/worlds/synthcity/visual/pointcloud/octree.bin   # 206
curl -s -o /dev/null -w '%{http_code}\n' $D/worlds/.geo-cache/                  # 404
curl -s -o /dev/null -w '%{http_code}\n' $D/sdk/awr_sandbox.py                  # 200
curl -s -H 'content-type: application/json' -d '{"role":"operator"}' \
  -o /dev/null -w '%{http_code}\n' $D/api/auth/token                            # 403（匿名只签 viewer）
curl -sI http://drone4d.agentnetwork.org.cn/ | head -1                          # 301
sudo bash /opt/anet-drone4d/current/tools/deploy/emax/install.sh --status         # 发布、cgroup 委派、两个 api 与容量
```

再用桌面 Chrome 或 Edge 打开站点：启动屏显示版本号与五段进度条及容量行；落地页 → 「进入演示」→ 共享展示 S7；世界列表有七个
世界；「开启我的沙盒」可在 synthcity 与六城创建。手机与平板浏览器打开同一地址，进入对应的移动布局。

站点文件的 nginx 语义（`nginx -t`、`/worlds/` 直出与 show-api 的对拍、路径分流、创建限流、容量微缓存）在开发机上以
`make deploy-nginx-check` 验证（一次性 Ubuntu 24.04 容器，nginx 1.24，与 emax 相同；本机不装 nginx，需要 docker、本地
`ubuntu:24.04` 镜像与可访问的 Ubuntu 软件源）。容量微缓存即 D2-AC-42 的 nginx 子项：100 个并发客户端（各自的地址前缀，
GET 与 HEAD 混合）持续 8 s，每个自然秒到达 sandbox-api ≤ 1 次；上游 503、500、慢（2.5 s，冷缓存）、卡住（> 读超时）与
不可达五种故障下同样 ≤ 1 次/s。证据写到 `NGINX_CHECK_OUT`（缺省 `.cache/deploy/emax/nginx-check/<UTC 时刻>/`，汇总
`nginx-check.json`）；验收经 M16 harness 用例 `d21.nginx-site` 执行同一脚本。退出码：0 通过、1 用例失败、2 `nginx -t` 失败、
8 缺少 docker、镜像或软件源。

## 3 更新

在开发机重新打包（2.1），上传部署包与世界包目录并执行同一条 `install.sh`（2.2）。新发布装在新的 `releases/<release>`，依赖没变时
uv 直接复用缓存，numba 按新源码路径重新预热；世界包与派生缓存只在内容版本或派生键变化时才重新解包（`DERIVE_VERSION` 或几何参数
变化时派生键变化，新条目随世界包目录上传）；切换后 90 s 内健康检查不过会自动切回上一发布。`/etc/anet-drone4d/env` 不会被覆盖：
若安装时提示 `env.example` 有新增的键，按提示 `diff` 后手工合并；D1 时期若在 env 中写过 `AWR_WORLDS_ALLOW=synthcity`，安装时会
提示删除。nginx 站点文件只有在本目录的站点文件变化时才需要按 2.3 第 2 步重新安装并 reload（从 V0.1 升级到 V0.2-demo 必须执行）。

## 4 回滚

```bash
sudo bash /opt/anet-drone4d/current/tools/deploy/emax/install.sh --status              # 当前发布、全部发布、cgroup、两个 api
sudo bash /opt/anet-drone4d/current/tools/deploy/emax/install.sh --rollback            # 切回上一个发布并重启
sudo bash /opt/anet-drone4d/current/tools/deploy/emax/install.sh --rollback <release>  # 切回指定发布
```

回滚只切换 `current`、重新安装该发布自带的 systemd 单元并重启服务，不动 `var/`（世界、派生缓存、沙盒状态、运行目录、缓存）、
`env` 与配额 drop-in。每个世界保留当前与上一个派生缓存条目，因此回滚到上一发布时派生键仍可用；更早的发布若派生键已被清理，
对应世界在沙盒中标为不可用（507 `cache_missing`），用该发布的世界包目录重新 `--worlds` 安装即可。从 V0.2-demo 回滚到 V0.1 时，
nginx 站点文件也要换回 V0.1 的版本（`/opt/anet-drone4d/releases/<V0.1 发布>/tools/deploy/emax/`）。

## 5 运维

### 5.1 日志

| 位置 | 内容 | 命令 |
|---|---|---|
| journal | supervisor 的 READY 行、进程状态转移、重启与熔断、告警（JSON 行）；`cgroup-exec` 的一行布局结论 | `journalctl -u anet-drone4d -f` |
| `var/runs/current/logs/*.log` | 共享展示 sim-core、show-api、sandbox-pool、sandbox-api 的进程日志（10 MB × 5 轮转） | `sudo -u awr tail -f /opt/anet-drone4d/var/runs/current/logs/sandbox-pool.log` |
| `var/sandbox/runs/<sid>/` | 每个沙盒 sim-core 的日志 | `sudo -u awr ls /opt/anet-drone4d/var/sandbox/runs/` |
| `var/runs/current/audit.jsonl` | 共享展示审计：token 签发、被拒的写请求、内部身份解锁与失败（只记前缀与 jti，从不记口令） | `sudo -u awr tail /opt/anet-drone4d/var/runs/current/audit.jsonl` |
| `var/sandbox/audit.jsonl` | 沙盒审计：创建、结束、抢占、回收、排队、配额拒绝、驻留 | `sudo -u awr tail /opt/anet-drone4d/var/sandbox/audit.jsonl` |
| `var/runs/current/crash/` | 子进程异常退出或挂死时的栈 | `ls /opt/anet-drone4d/var/runs/current/crash/` |
| nginx | 本站点的访问与错误沿用 nginx 的全局日志（缺省格式不含请求体与认证头） | `sudo tail -f /var/log/nginx/access.log` |

### 5.2 常用操作

```bash
systemctl status anet-drone4d                    # 状态（CGroup 中可见 core/ 与 sandbox/ 两组子进程）
sudo bash /opt/anet-drone4d/current/tools/deploy/emax/install.sh --status
sudo systemctl restart anet-drone4d              # 重启：共享展示剧本从头开始；沙盒池重新接管仍在运行的沙盒
systemd-cgtop -1 | grep anet-drone4d             # CPU 与内存（单元上限 CPUQuota 250%、MemoryHigh 1.8G、MemoryMax 2G）
cat /sys/fs/cgroup/system.slice/anet-drone4d.service/sandbox/cpu.max     # 沙盒组 CPU 上限（Q = 2.5 时 136000 100000）
cat /sys/fs/cgroup/system.slice/anet-drone4d.service/sandbox/sb/cpu.max  # 会话父组 sandbox/sb/：沙盒组上限减控制面预留 0.22 核（AWR_CG_CTL_CPU_RESERVE；控制面不随会话限流停顿，D2-FX-R2）
curl -s -H 'Host: drone4d.agentnetwork.org.cn' http://127.0.0.1:18640/api/health/ready
curl -s -H 'Host: drone4d.agentnetwork.org.cn' http://127.0.0.1:18641/api/sandbox/v1/health
```

需要排查时可以临时在 `env` 的 `AWR_ORIGINS` 追加 `http://127.0.0.1:18640` 并重启，再经 `ssh -N -L 18640:127.0.0.1:18640 emax` 在
本机浏览器打开 `http://127.0.0.1:18640/`，排查完删掉并重启。

### 5.3 沙盒池（sbctl.sh）

```bash
S=/opt/anet-drone4d/current/tools/deploy/emax/sbctl.sh
sudo bash $S status                       # N_max、Q、委派、各会话的世界、状态、剩余寿命、倍速、匿名页、降级级别、队列
sudo bash $S list
sudo bash $S stop sb-xxxxxxxxxx           # 结束一个会话（快照保留 10 min）
sudo bash $S drain --notice 60            # 维护：60 s 前告知后结束全部会话；之后 systemctl restart anet-drone4d
sudo bash $S accept off                   # 拒绝新沙盒（容量接口 closed，落地页按钮置灰）；accept on 恢复
sudo bash $S limits --max-sessions 1      # 临时降容（重启保留）；--max-sessions auto 复原
sudo bash $S --json status                # 机器可读
```

彻底关闭沙盒：在 env 中设 `AWR_SANDBOX_ENABLED=0` 并重启（共享展示照常）。并发上限 N_max 按 AWR-04 §4.4.1 公式由单元 CPUQuota
推出（250% 时 4、200% 时 2，硬上限 6；无 cgroup 委派时再减 1）；世界加权内存准入额度由 `cgroup-exec.sh` 按沙盒组
`memory.high` 自动导出（1.8G 时 1100 MB、1.6G 时 950 MB）。

### 5.4 内部身份口令（ADR-123）

内部身份（设置页底部"高级 › 身份"，全功能，给项目内部人员使用）的口令只保存在 emax 的 `/etc/anet-drone4d/admin.secret`
（0600 awr），由用户本人或其授权的运维在服务器上输入；口令不得出现在仓库、部署包、文档、聊天记录与日志中。

```bash
ssh -t emax
umask 077
read -rs -p '内部身份口令：' P && printf '%s\n' "$P" > ~/.awr-admin-secret && unset P && echo
sudo bash /opt/anet-drone4d/current/tools/deploy/emax/install.sh --secret-only --admin-secret-file ~/.awr-admin-secret
shred -u ~/.awr-admin-secret
```

`--secret-only` 只写口令并重启服务，不重装发布；安装或更新时也可以把 `--admin-secret-file` 直接加在 2.2 的命令后。
`--admin-secret-file -` 从标准输入读取（终端上不回显）。口令须为单行、可打印 ASCII、不含空白、长度 ≥ 8；脚本校验后以 0600 awr
写入 `/etc/anet-drone4d/admin.secret`，在 env 中设置 `AWR_ADMIN_SECRET_FILE`，然后重启服务生效。轮换：用新口令再执行一遍，全部
身份令牌随之失效。口令错误按地址前缀 10 min 内 5 次锁定 30 min（另有聚合前缀与全站阈值；回环来源不受全站锁定），锁定状态
保存在 `var/identity/`。开发机的 `make lint`（REL-04）会在发现本机存在部署口令文件（`~/.config/awr/admin.secret` 或
`AWR_ADMIN_SECRET_FILE`）时检查发布集合中没有它的内容，`build-local.sh` 对部署包做同样的检查。
停用内部身份：删除 `admin.secret` 与 env 中的 `AWR_ADMIN_SECRET_FILE` 行并重启（入口返回"此部署未启用内部身份"）。

### 5.5 配额与降级

| 现象 | 处理 |
|---|---|
| 运维要求降回 200% 与 1.6G | 以 2.2 的安装命令加 `--cpu-quota 200% --memory-high 1.6G` 重新执行（幂等；发布与世界包都复用，写入 drop-in 后重启；cgroup-exec.sh 与池按新配额重算，N_max 降为 2） |
| 沙盒组 CPU 吃紧（日志 `ladder.enter{level: L2}`） | 全部快进会话自动降为 ×1；持续时 `sbctl.sh limits --max-sessions` 临时降容 |
| 内存紧张（`ladder.enter{level: L4}`） | 池按超额分驻留会话；检查 `systemd-cgtop`，必要时降容 |
| `cgroup.undelegated` 告警 | 非 cgroup v2 或 systemd 未委派：沙盒以 N_max − 1、nice +5 与 RSS 看门狗运行，功能不受影响 |
| 世界列表中某城市 `available: false` | 派生缓存缺失或与代码版本不一致：用开发机当前版本的世界包目录重新 `--worlds` 安装 |

### 5.6 PyPI 镜像

服务器到 PyPI 慢或不通时，在执行 `install.sh` 前导出镜像地址（依赖仍按 `requirements.lock` 的 hash 校验）：

```bash
sudo PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  bash /tmp/anet-drone4d-<release>/tools/deploy/emax/install.sh --bundle /tmp/anet-drone4d-<release> --worlds "$HOME/anet-drone4d-worlds"
```

### 5.7 线上观测（D2-AC-27）

D2-AC-27 只能在 emax 上测：部署后 72 h 内 OOM 0、非预期重启 0、共享展示 RTF ≥ 0.99；S7 连续 7 天被发现 0、间隙按原因分列；emax
实测 AWR-04 §4.4.1 各项成本与世界成本表（据此由文档总编以 ADR 冻结 N_max）。`observe.py` 与服务并排运行，只读取不改动；
以 root 执行（读取 0600 的 env 后降权为 awr 读 supervisor 状态，经 `sbctl.sh` 读沙盒池），自身约 0.02 核（本机实测 `awr status` 单次 0.79 s CPU，每 60 s 一次；`sbctl.sh` 每 120 s 一次；报告 `observer` 记录实际值）。

```bash
O=/opt/anet-drone4d/var/observe/$(date -u +%Y%m%dT%H%MZ)
PY=/opt/anet-drone4d/current/.venv/bin/python                       # 发布的 .venv：进程内读取共享展示 StateRing
OBS=/opt/anet-drone4d/current/tools/deploy/emax/observe.py
sudo systemd-run --unit=drone4d-observe --collect -p Nice=10 $PY $OBS run --hours 168 --out "$O"   # 7 天（前 72 h 同时给出服务判据）
sudo $PY $OBS mark "$O" --minutes 20 --note "手工重启"                # 维护窗口（窗口内的重启与 RTF 不计）
sudo $PY $OBS report "$O" --hours 72                                 # 72 h 服务判据
sudo $PY $OBS report "$O"                                            # 7 天（S7）
```

| 判据 | 口径 |
|---|---|
| OOM 0 | 单元根 `memory.events` 的 `oom_kill` 增量（层级计数，含 `core/` 与全部沙盒叶子组），且内核日志没有提到本单元的 OOM 行 |
| 非预期重启 0 | 单元 InvocationID 变化中既非发布切换（`current` 指向变化，即 install.sh 与 `--rollback`）也不在维护窗口内的次数，加上 supervisor 进程表 `restarts`（退避重启）的增量 |
| 共享展示 RTF ≥ 0.99 | 后台线程每 0.5 s 在进程内读共享展示 StateRing 头部（只映射，不占读者游标）：sim-core 逐秒自报 rtf 的最小值与相邻 10 s 样本间 Δt_sim / Δt_wall 的最小值，心跳 > 2 s 未更新按 0 计；跨 epoch、时钟停止或暂停（STOPPED、PAUSED、STEPPING、ENDED、RESTARTING；STALLED 照常计入）、非 ×1、维护窗口与重启或软重置后 120 s 内的窗口不计；不能导入 awr（用了系统 python3）时退回 `awr status` 的 ≥ 300 s 窗口，结论最多为未完成 |
| S7 七天 | 观测 ≥ 168 h 且事件游标未过期：带我方机体的 `target.discovered` 为 0；`relay.gap` 按 `cause` 分列次数、合计与最长 |
| 成本（不参与判据） | `core/`、`sandbox/`、`sandbox/plan`、`sandbox/ctl` 的核数均值与 p95；按世界的每会话核数；单元 `memory.peak` 与匿名页 + shmem 峰值；降级级别时长；生效 N_max |

退出码 0 通过、1 不通过、2 参数错误、3 未完成（时长不足、采样覆盖率 < 0.95 或事件游标过期）。观测中断后用同一 `--out` 再次
`run` 即续跑（起点与事件游标保留）。`report.json`（`awr.deploy.observe.v1`）连同 `samples.jsonl`、`events.jsonl` 拷回开发机，作为
D2-AC-27 的证据与 N_max 冻结 ADR 的输入；观测不修改 `configs/runtime.yaml`。

## 6 卸载

只移除本站点与本服务，逐条执行；不涉及其他站点与 ANet 生产服务：

```bash
sudo systemctl disable --now anet-drone4d
sudo rm -rf /etc/systemd/system/anet-drone4d.service /etc/systemd/system/anet-drone4d.service.d && sudo systemctl daemon-reload
sudo rm -f /etc/nginx/sites-enabled/drone4d.agentnetwork.org.cn.conf /etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf
sudo nginx -t && sudo systemctl reload nginx
sudo rm -rf /var/lib/nginx/drone4d-micro
sudo certbot delete --cert-name drone4d.agentnetwork.org.cn        # 可选：删除本站点证书
sudo rm -rf /opt/anet-drone4d /etc/anet-drone4d /dev/shm/awr ~/anet-drone4d-worlds
sudo userdel awr; sudo groupdel awr 2>/dev/null || true             # 系统用户与同名组
```
