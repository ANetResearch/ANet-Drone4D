# 示例内部提交镜像 close_track：以平台 Agent 基础镜像 d3-agent-py 为底（BASE 缺省 d3-agent-py:dev，须含 numpy 与 pillow ≥ 12.3.0，
# 由 tools/deploy/dmax/images/agent/build.sh 构建），加入自己的包，入口经 anet4d.serve 提供线协议服务。wheels/ 中的离线 wheel（可为空）
# 覆盖安装到镜像，只在基础镜像尚未修复已有修复版本的 HIGH 及以上漏洞时使用（提交扫描门，ADR-204、D3-AC-59）；基础镜像已修复时为空。
# 本文件替代 B21 在 dmax 上的临时修补（build/example-close-track-b21：在 :b10 镜像上覆盖安装 pillow 12.3.0；K18）。
ARG BASE=d3-agent-py:dev
FROM ${BASE}
USER root
COPY wheels/ /tmp/wheels/
RUN set -e; if ls /tmp/wheels/*.whl >/dev/null 2>&1; then pip install --no-index --no-deps --no-cache-dir /tmp/wheels/*.whl; fi; rm -rf /tmp/wheels
COPY close_track/ /opt/agent/close_track/
ENV PYTHONPATH=/opt/drone4d:/opt/agent
USER 65534:65534
CMD ["python", "-m", "anet4d.serve", "close_track.agent:Agent"]
