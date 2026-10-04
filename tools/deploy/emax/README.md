# emax 部署手册：ANet Drone4D 公开演示站

本目录是把 ANet Drone4D 的公开演示站部署到服务器 emax（`https://drone4d.agentnetwork.org.cn`）的全部材料，对应
[AWR-19 §3.7 T4 公开演示站](../../../docs/19-部署与运维说明书.md)与 ADR-082 至 ADR-085。它是脚本目录，不是 Python 包，
不被任何代码 import。

| 文件 | 用途 |
|---|---|
| `build-local.sh` | 在开发机上构建演示前端（`VITE_AWR_DEMO=public`）并打部署包，可选附带 synthcity 世界包 |
| `install.sh` | 在 emax 上以 root 执行：建用户与目录、建 venv、预热、准备世界、安装 systemd 单元、切换发布、健康检查；`--rollback`、`--status` |
| `anet-drone4d.service` | systemd 单元（用户 awr，`/opt/anet-drone4d`，`/etc/anet-drone4d/env`，CPUQuota 200%，MemoryMax 2G） |
| `env.example` | `/etc/anet-drone4d/env` 的模板，列出全部可配置项；不含任何机密 |
| `drone4d.agentnetwork.org.cn.conf` | nginx 站点文件（80 端口 ACME 与 301，443 端口 TLS 与反向代理） |
| `drone4d.agentnetwork.org.cn.bootstrap.conf` | 首次申请证书用的只含 80 端口的引导站点文件 |

**硬性约束（每次操作前请再读一遍）：**

1. **只新增本站点，不得修改其他站点的配置。** emax 上已经运行 ANet 生产服务与 nginx 1.24：不改 `nginx.conf`、
   不改、不删、不重命名 `sites-available` 与 `sites-enabled` 中的其他文件，不动其他站点的证书。本站点文件中的
   `limit_req_zone`、`limit_conn_zone`、`upstream`、`map` 变量与 SSL 会话缓存都带 `drone4d` 前缀，与其他站点不冲突。
   `install.sh` 从不 reload nginx，也不调用 certbot：这两步一律由人工按下文逐条执行，执行前先 `nginx -t`。
2. **公开站只能提供合成城市 synthcity。** UrbanScene3D 六城的原始数据、世界包与渲染画面禁止再分发（ADR-034）：
   不要把开发机的 `worlds/` 整体拷到服务器，不要在服务器上 `make fetch-data`。api 的世界白名单只服务 synthcity，
   其余世界的 REST 与静态请求都返回 404。
3. **公开只读。** 匿名访客只拿得到 viewer；operator 与 admin 必须提供管理口令，缺省不生成口令，一律拒绝。
   不提供 GPU 重建、录制回放、智能体运行时等服务端功能（recorder、agent-runtime、job-worker 不启动）。
4. 机密（管理口令、每个 run 的签名密钥）只在服务器上随机生成，绝不入库、不写进 `env`、不出现在日志里。

## 1 前置条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| 服务器 | Ubuntu 24.04、x86-64、4 核、约 2.8 GB 可用内存、无 GPU（本站常驻约 0.6 GB、约 0.3 核） | `uname -m; nproc; free -h` |
| Python | 3.12（系统自带 3.12.3）与 venv 模块 | `python3.12 --version; sudo apt install python3.12-venv` |
| 工具 | `rsync`、`curl`、`systemd` | `command -v rsync curl systemctl` |
| 网络 | 首次安装与依赖变化时要能访问 PyPI（或镜像，见 5.3） | `curl -sI https://pypi.org/simple/ \| head -1` |
| DNS | `drone4d.agentnetwork.org.cn` 已解析到 emax | `dig +short drone4d.agentnetwork.org.cn` |
| nginx 与 certbot | nginx 1.24 已在运行，certbot 已安装，webroot `/var/www/certbot` 已存在（证书为 ECDSA） | `nginx -v; certbot --version; ls -d /var/www/certbot` |
| 端口 | 127.0.0.1:18640（api）与 127.0.0.1:18647（进程间总线）未被占用；不需要开放任何新的公网端口 | `ss -ltn \| grep -E ':1864[07]'` |
| 开发机 | 已 `make setup`（Node 22、`.venv`），synthcity 已生成（`make demo-world`，附带世界包时需要） | `make demo-build` |

## 2 首次部署

### 2.1 开发机：打包

```bash
# 演示构建 + 部署包；--with-world 另附 synthcity 世界包（约 60 MB，服务器上就不必再生成）
tools/deploy/emax/build-local.sh --with-world        # 或 make deploy-pack WORLD=1
```

产物在 `.cache/deploy/emax/`：`anet-drone4d-<release>.tar.gz`（代码、配置、演示前端，约 14 MB）与可选的
`synthcity-world-<版本>.tar.gz`。脚本会复核包内没有 `worlds/`、`data/`、`refs/`、`.cache/`、点云文件、超过 5 MB 的文件与
本机凭据，前端是演示构建且通过生产包扫描（无测试开关），最后打印上传命令。

### 2.2 上传并安装

```bash
rsync -avP .cache/deploy/emax/anet-drone4d-<release>.tar.gz .cache/deploy/emax/synthcity-world-<版本>.tar.gz emax:/tmp/
ssh emax 'tar -C /tmp -xzf /tmp/anet-drone4d-<release>.tar.gz'
ssh -t emax 'sudo bash /tmp/anet-drone4d-<release>/tools/deploy/emax/install.sh \
  --bundle /tmp/anet-drone4d-<release> --world-pack /tmp/synthcity-world-<版本>.tar.gz'
```

`install.sh` 依次：校验包（`MANIFEST.sha256`、演示构建标记、不含 `dist/bench`）→ 创建系统用户 `awr` 与目录 →
自举 uv 0.12.19 → 复制到 `/opt/anet-drone4d/releases/<release>`（root 所有，awr 只读）→ 按 `requirements.lock` 建该发布的
`.venv` → 预编译字节码与 numba 缓存（约 1–2 min）→ 解开世界包（没给 `--world-pack` 时在服务器上生成 synthcity，约 30 s、
峰值约 1.3 GB 内存；共享环境资产随世界包提供，否则 sim-core 首次启动时生成，约 1 s）→ 首次安装时由 `env.example` 生成 `/etc/anet-drone4d/env`（0600）→ 安装 systemd 单元，
切换 `current`，重启并等待 `/api/health/ready`（90 s 内不就绪自动回滚到上一发布）→ 保留最近 3 个发布 → 打印 nginx 与
certbot 命令。

服务器上的布局：

| 路径 | 所有者 | 内容 |
|---|---|---|
| `/opt/anet-drone4d/releases/<release>/` | root | 代码、配置、演示前端与该发布的 `.venv` |
| `/opt/anet-drone4d/current` | root | 指向当前发布的符号链接 |
| `/opt/anet-drone4d/var/worlds/` | awr | `synthcity/` 与 `_shared/env/`（只有这两项） |
| `/opt/anet-drone4d/var/runs/` | awr | 每次启动一个 run：子进程日志、审计、生效配置（配额 2 GB） |
| `/opt/anet-drone4d/var/cache/numba/` | awr | numba 编译缓存 |
| `/etc/anet-drone4d/env` | root，0600 | 运行环境（`env.example` 生成，之后不覆盖） |
| `/etc/anet-drone4d/admin.secret` | awr，0600 | 可选的管理口令（`--admin-secret` 生成） |
| `/etc/systemd/system/anet-drone4d.service` | root | systemd 单元（每次安装覆盖） |

### 2.3 nginx 与证书（人工执行）

```bash
# 1) 引导站点（只有 80 端口：ACME 校验，其余 503），申请 ECDSA 证书
sudo install -m 0644 /opt/anet-drone4d/current/tools/deploy/emax/drone4d.agentnetwork.org.cn.bootstrap.conf \
  /etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf
sudo ln -sfn /etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf /etc/nginx/sites-enabled/drone4d.agentnetwork.org.cn.conf
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d drone4d.agentnetwork.org.cn --key-type ecdsa --elliptic-curve secp384r1

# 2) 正式站点（443：TLS 1.2/1.3、HSTS、反向代理到 127.0.0.1:18640、/api/rt WebSocket）
sudo install -m 0644 /opt/anet-drone4d/current/tools/deploy/emax/drone4d.agentnetwork.org.cn.conf \
  /etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf
sudo nginx -t && sudo systemctl reload nginx
```

`nginx -t` 失败时不要 reload，先看报错：最常见的是 `limit_req_zone` 等名字与其他站点重复（本文件已全部加 `drone4d`
前缀）或证书路径尚不存在（还在用引导站点之前就换了正式站点）。证书续期沿用 certbot 已有的定时任务（webroot 方式，
续期后 certbot 的 deploy hook 或定时 reload 生效）；本站点不需要额外的续期配置。

### 2.4 验证

```bash
curl -sI https://drone4d.agentnetwork.org.cn/ | head -1                                              # HTTP/2 200（落地页）
curl -s -o /dev/null -w '%{http_code}\n' https://drone4d.agentnetwork.org.cn/world/synthcity           # 200
curl -s -o /dev/null -w '%{http_code}\n' https://drone4d.agentnetwork.org.cn/worlds/shenzhen/world.json # 404
curl -s -H 'content-type: application/json' -d '{"role":"operator"}' \
  -o /dev/null -w '%{http_code}\n' https://drone4d.agentnetwork.org.cn/api/auth/token                   # 403（公开只读）
curl -sI http://drone4d.agentnetwork.org.cn/ | head -1                                               # 301
```

再用桌面 Chrome 或 Edge 打开站点：落地页 → 「进入演示」→ 点云与七架 P600 出现，顶栏显示「公开演示 · 只读」。

## 3 更新

在开发机重新打包（2.1），上传并执行同一条 `install.sh`（2.2）。新发布装在新的 `releases/<release>`，依赖没变时 uv 直接
复用缓存，numba 按新源码路径重新预热；切换后 90 s 内健康检查不过会自动切回上一发布。`/etc/anet-drone4d/env` 不会被覆盖：
若安装时提示 `env.example` 有新增的键，按提示 `diff` 后手工合并。nginx 站点文件只有在本目录的站点文件变化时才需要按
2.3 第 2 步重新安装并 reload。世界包只在 synthcity 生成器版本变化时需要重新上传（`--world-pack`），否则服务器上的
`var/worlds/synthcity` 保持不变。

## 4 回滚

```bash
sudo bash /opt/anet-drone4d/current/tools/deploy/emax/install.sh --status              # 当前发布、全部发布、服务与健康
sudo bash /opt/anet-drone4d/current/tools/deploy/emax/install.sh --rollback            # 切回上一个发布并重启
sudo bash /opt/anet-drone4d/current/tools/deploy/emax/install.sh --rollback <release>  # 切回指定发布
```

回滚只切换 `current`、重新安装该发布自带的 systemd 单元并重启服务，不动 `var/`（世界、运行目录、缓存）与 `env`。

## 5 运维

### 5.1 日志

| 位置 | 内容 | 命令 |
|---|---|---|
| journal | supervisor 的 READY 行、进程状态转移、重启与熔断、告警（JSON 行） | `journalctl -u anet-drone4d -f` |
| `var/runs/current/logs/*.log` | sim-core 与 api 的进程日志（10 MB × 5 轮转） | `sudo -u awr tail -f /opt/anet-drone4d/var/runs/current/logs/api.log` |
| `var/runs/current/audit.jsonl` | 审计：token 签发、被拒的写请求（`auth.denied`，来源为客户端地址） | `sudo -u awr tail /opt/anet-drone4d/var/runs/current/audit.jsonl` |
| `var/runs/current/crash/` | 子进程异常退出或挂死时的栈 | `ls /opt/anet-drone4d/var/runs/current/crash/` |
| nginx | 本站点的访问与错误沿用 nginx 的全局日志 | `sudo tail -f /var/log/nginx/access.log` |

### 5.2 常用操作

```bash
systemctl status anet-drone4d          # 状态（supervisor 下的 sim-core、api 子进程一并显示在 CGroup 中）
sudo systemctl restart anet-drone4d    # 重启：剧本从头开始
curl -s -H 'Host: drone4d.agentnetwork.org.cn' http://127.0.0.1:18640/api/health/ready   # 本机健康检查
systemd-cgtop -1 | grep anet-drone4d   # CPU 与内存（上限 CPUQuota 200%、MemoryMax 2G）
```

剧本 S0 循环运行：每轮约 4.8 min（仿真时间），结束后停 8 s 自动从头重开，天气序列每轮循环左移一位（晴、小雨、雾）；
页面会提示「仿真已从剧本起点重开」。需要排查时可以临时在 `env` 的 `AWR_ORIGINS` 追加 `http://127.0.0.1:18640` 并重启，
再经 `ssh -N -L 18640:127.0.0.1:18640 emax` 在本机浏览器打开 `http://127.0.0.1:18640/`，排查完删掉并重启。
需要 operator 权限排查（例如手动下发命令）时：`sudo bash install.sh --bundle ... --admin-secret` 生成口令并写入
`AWR_ADMIN_SECRET_FILE`，重启后经 SSH 转发签发 operator token；排查完删除 `admin.secret` 与 `env` 中的该行并重启，恢复
「一律拒绝」。

### 5.3 PyPI 镜像

服务器到 PyPI 慢或不通时，在执行 `install.sh` 前导出镜像地址（依赖仍按 `requirements.lock` 的 hash 校验）：

```bash
sudo PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  bash /tmp/anet-drone4d-<release>/tools/deploy/emax/install.sh --bundle /tmp/anet-drone4d-<release>
```

## 6 卸载

只移除本站点与本服务，逐条执行；不涉及其他站点与 ANet 生产服务：

```bash
sudo systemctl disable --now anet-drone4d
sudo rm -f /etc/systemd/system/anet-drone4d.service && sudo systemctl daemon-reload
sudo rm -f /etc/nginx/sites-enabled/drone4d.agentnetwork.org.cn.conf /etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf
sudo nginx -t && sudo systemctl reload nginx
sudo certbot delete --cert-name drone4d.agentnetwork.org.cn        # 可选：删除本站点证书
sudo rm -rf /opt/anet-drone4d /etc/anet-drone4d /dev/shm/awr
sudo userdel awr; sudo groupdel awr 2>/dev/null || true             # 系统用户与同名组
```
