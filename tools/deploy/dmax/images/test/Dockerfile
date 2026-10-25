# d3-test（D3 封板 I01、K14；docs/05 F-113）：D3 测试统一入口（tools/deploy/dmax/run_d3_tests.sh）主阶段的运行镜像。
# 以 d3-compile 为基础：Python 依赖同 d3-sensorsvc（requirements.lock + requirements-d3.lock + GPU 层，含 datafusion），另有
# Node 22.12 与 navsvc 的 recast-navigation 依赖（合成世界构建经 M22 navmesh 阶段、事件编译用 Detour 后端）。本镜像只增加测试与
# 入口脚本调用的命令行工具：rsync（部署与发布脚本的用例）、git（快照与标签）、make。只含依赖，代码以卷挂载到 /repo。
# 依赖 docker 守护进程或本仓库 git 目录的用例（tests/d3/deploy、submit/test_redteam.py）不在容器内运行，由入口的主机阶段承担。
# 构建：tools/deploy/dmax/images/test/build.sh（先按锁文件重建 d3-sensorsvc，再重建 d3-compile，最后本镜像）。
ARG BASE=d3-compile:dev
FROM ${BASE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends rsync git make \
 && rm -rf /var/lib/apt/lists/* \
 && python -c "import datafusion, open3d, mcap, warp" \
 && test "$(node -p "require('/node_modules/@recast-navigation/core/package.json').version")" = "$(node -p "require('/node_modules/@recast-navigation/wasm/package.json').version")" \
 && rsync --version >/dev/null && git --version >/dev/null && make --version >/dev/null
LABEL org.opencontainers.image.title="d3-test"
USER awr
