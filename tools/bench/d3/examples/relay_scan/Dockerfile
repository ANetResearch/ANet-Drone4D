# 示例内部提交镜像 relay_scan：以平台 Agent 基础镜像 d3-agent-py 为底（BASE 缺省 d3-agent-py:dev，须含 numpy 与 pillow 以运行 L2
# 参考检测器），加入自己的包，入口经 drone4d.serve 提供线协议服务。wheels/ 中的离线 wheel（可为空）覆盖安装到镜像，用于修复基础镜像中
# 已有修复版本的 HIGH 及以上漏洞（提交扫描门，ADR-204），例如 pillow ≥ 12.3.0。
ARG BASE=d3-agent-py:dev
FROM ${BASE}
USER root
COPY wheels/ /tmp/wheels/
RUN set -e; if ls /tmp/wheels/*.whl >/dev/null 2>&1; then pip install --no-index --no-deps --no-cache-dir /tmp/wheels/*.whl; fi; rm -rf /tmp/wheels
COPY relay_scan/ /opt/agent/relay_scan/
ENV PYTHONPATH=/opt/drone4d:/opt/agent
USER 65534:65534
CMD ["python", "-m", "drone4d.serve", "relay_scan.agent:Agent"]
