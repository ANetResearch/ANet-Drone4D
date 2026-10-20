# d3-compile（W09 遗留 4、K06；ADR-196 决策 (4)、(5)）：dmax 上世界包构建（M22 navmesh 阶段）与事件编译（M23，Detour 后端）的镜像。
# 以 d3-sensorsvc 为基础（Python 依赖与 open3d 相同），只增加 Node 22.12 与 navsvc 的 recast-navigation 依赖：
#   - Node 单文件可执行 /opt/node/bin/node（版本由构建参数 NODE_VERSION 核对）；
#   - tools/navsvc/node_modules 装在 /node_modules：代码以卷挂载在任意目录（/repo、/opt/awr）时，Node ESM 裸模块解析逐级向上查到此处，
#     Python 侧 `navmesh.navsvc_modules()` 与 navsvc `recast.mjs` 按同一次序取到同一目录并核对版本与 WASM sha256；
#   - AWR_EVENT_NAV=detour：本镜像内的编译只接受 Detour 后端，Node 或 navsvc 不可用时报 ConfigError，不退回栅格（W12 遗留 3）。
# 构建不访问网络：上下文由 build.sh 从本机同步（Node 取 NODE_DIR，依赖取 `npm ci --prefix tools/navsvc` 的结果）。
ARG BASE=d3-sensorsvc:dev
FROM ${BASE}
ARG NODE_VERSION=v22.12.0
ARG RECAST_VERSION=0.43.1
ARG WASM_SHA256
USER root
COPY node /opt/node/bin/node
COPY node_modules/ /node_modules/
ENV PATH=/opt/node/bin:${PATH} AWR_EVENT_NAV=detour
RUN test -n "$WASM_SHA256" && test "$(node --version)" = "$NODE_VERSION" \
 && for p in core generators wasm; do test "$(node -p "require('/node_modules/@recast-navigation/$p/package.json').version")" = "$RECAST_VERSION"; done \
 && echo "$WASM_SHA256  /node_modules/@recast-navigation/wasm/dist/recast-navigation.wasm.wasm" | sha256sum -c - \
 && chmod -R a+rX /node_modules /opt/node
LABEL org.opencontainers.image.title="d3-compile" awr.node.version="${NODE_VERSION}" awr.recast.version="${RECAST_VERSION}" \
      awr.navsvc.wasm_sha256="${WASM_SHA256}"
USER awr
