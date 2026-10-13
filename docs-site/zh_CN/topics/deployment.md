# 部署

本页概述公开演示站 [drone4d.agentnetwork.org.cn](https://drone4d.agentnetwork.org.cn) 的部署套件与本文档站的发布方式。逐条命令、参数与故障处理
以仓库中的 [tools/deploy/emax/README.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/tools/deploy/emax/README.md) 为准。

## 拓扑

一台 4 核、无 GPU 的 Ubuntu 24.04 服务器，与其他生产服务共用。一个 systemd 单元 `anet-drone4d`（系统用户 `awr`）承载两组进程：

| 组 | 进程 | 说明 |
|---|---|---|
| 共享展示 | supervisor、show-api、共享展示的 sim-core | 剧本 S7，show-api 只监听回环 18640 |
| 沙盒 | sandbox-pool、sandbox-api、每个沙盒一个 sim-core | sandbox-api 只监听回环 18641；进程间总线 18647 |

nginx 终止 TLS，并按路径分流：`/api/sandbox/` 转发给 sandbox-api，其余 `/api/` 转发给 show-api，`/worlds/` 由 nginx 直接读取世界包目录，
`/sdk/` 提供 Python SDK 的下载。容量接口有 1 s 的微缓存，各路径按地址前缀限流。

## 资源上限

| 项 | 取值 |
|---|---|
| CPU | `CPUQuota=250%`；cgroup 委派后分为 `core/` 与 `sandbox/{ctl,plan,sb}` 子组 |
| 内存 | `MemoryHigh=1.8G`，`MemoryMax=2G`；本站峰值约 1.75 GB（含可回收文件页） |
| 沙盒并发 | 容量模型按实测会话成本计算，当前公开上限为 2，其余访客排队 |
| 实时观看 | 24 个席位，每个地址前缀 4 条 WebSocket |
| 磁盘 | `/opt` 所在分区至少 5 GB：七个世界约 1.07 GB、派生缓存约 1.0 GB、保留 3 个发布、runs 配额 2 GB |

## 部署流程

1. **开发机打包。** `make deploy-pack` 生成部署包（代码、配置、剧本、机型、演示前端与构建信息、SDK、许可文件，约 15 MB）与七个世界的世界包和派生缓存包。
   世界包按内容版本命名，世界未变化时复用同一文件。脚本复核部署包中不含世界源数据、点云文件、超过 5 MB 的文件、本机凭据与部署口令。
   `make deploy-dry-run` 在本机模拟安装并深度校验。
2. **上传。** 部署包与世界包目录经 rsync 上传；首次约 1 GB，之后只传变化的包。
3. **安装。** 以 root 执行 `install.sh`：校验部署包，创建用户与目录，按锁文件建立该发布的虚拟环境并预热 numba 缓存，装入并深度校验世界包，安装 systemd
   单元与配额，切换 `current` 并等待 show-api 就绪（90 s 内未就绪自动回滚到上一发布），最后执行沙盒冒烟（创建、就绪、结束）并保留最近 3 个发布。
4. **nginx 与证书。** 由运维人工执行：先 `nginx -t`，再重载；证书经 certbot 的 webroot 方式签发。`install.sh` 从不重载 nginx，也不调用 certbot。
5. **验证。** 健康接口、容量接口、首页与沙盒冒烟。

更新与首次部署相同（只传变化的世界包）；`install.sh --rollback` 回到上一发布，`--status` 查看状态。

## 约束

- **只新增本站点。** 服务器上的其他站点、`nginx.conf` 与证书一律不改动；本站点的限流区、缓存区、upstream 与 map 变量都带 `drone4d` 前缀。
- **世界包不入库。** 七个世界经授权开放（见 {doc}`worlds`），世界包只经部署包上传，不进入 git 仓库，也不在服务器上下载原始数据。
- **机密不入库、不进包、不进日志。** 内部身份口令只由授权运维在服务器上以文件方式写入（`install.sh --admin-secret-file -` 从标准输入读取）；
  每次运行的签名密钥与沙盒池的密钥在服务器上自动生成。
- **匿名访客的权限边界。** 匿名访客在共享展示上只有观看权限；沙盒只对领取者本人可写，资源受并发上限、世界内存准入与前缀配额约束。
  公开实例不提供 GPU 重建、录制回放与智能体运行时等服务端重计算功能。

## 运维工具

| 工具 | 用途 |
|---|---|
| `sbctl.sh` | 沙盒池运维：状态、会话列表、结束会话、维护模式、接受开关、临时降容 |
| `observe.py` | 线上观测：72 h 服务判据（内存溢出、非预期重启、共享展示实时因子）与 S7 七天统计 |
| `nginx-check.sh` | 在一次性容器中对站点文件执行 `nginx -t` 并运行 nginx 对拍与端到端用例 |
| `smoke.py` | 安装后的沙盒冒烟 |

## 发布本文档站

本文档站采用 Sphinx、sphinx-rtd-theme 与 Read the Docs 的目录布局，发布在 `https://docs.agentnetwork.org.cn/drone4d/` 下：

| 路径 | 内容 |
|---|---|
| `/drone4d/` | 根页：按浏览器语言跳转，中文到 `zh-cn`，其余到 `en` |
| `/drone4d/en/latest/` | 英文文档 |
| `/drone4d/zh-cn/latest/` | 中文文档 |

仓库根目录的 `.readthedocs.yaml` 使同一套源也可以由 Read the Docs 构建。自托管发布由两步完成：

```sh
make docs-site                        # 构建到 docs-site/_build/html/
tools/deploy/docs/publish.sh          # 生成待发布目录与校验清单，并打印 rsync 命令
```

`publish.sh` 只在本机准备 `.cache/deploy/docs/drone4d/` 并打印上传命令，不执行上传。上传目标为文档服务器上的
`/var/www/docs.agentnetwork/public/drone4d/`（属主 `www-data`）；同步只作用于 `drone4d/` 子目录，不删除或改动该根目录中的任何其他内容，
也不需要修改 nginx 配置。
