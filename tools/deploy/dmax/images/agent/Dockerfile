# d3-agent-py：容器 Agent 基础镜像（AWR-17 §18.9.1；M26 §7.2）。Python 3.12 + anet4d SDK（线协议 serve 与参考基线；另含已弃用的兼容别名包 drone4d，V0.3.2 后移除），
# 运行时由 Env 以 --network none、只读根文件系统、非 root 用户启动，经挂载的 /run/drone4d/agent.sock 与 Env 通信。
# 内部提交可以此为基础镜像：COPY 自己的包，CMD ["python", "-m", "anet4d.serve", "my_pkg.agent:Agent"]。
# L2 参考检测器（anet4d.perception.ref_det，numpy 推理）需 numpy 与 pillow；BLAS 单线程（容器 1 CPU，且逐字节确定）。
# pillow ≥ 12.3.0（build.sh 钉定版本并校验 sha256）：基础镜像须通过提交扫描门（ADR-204、D3-AC-59；K18）。
FROM python:3.12-slim
COPY wheels/ /tmp/wheels/
RUN pip install --no-index --no-cache-dir /tmp/wheels/msgpack-*.whl /tmp/wheels/numpy-*.whl /tmp/wheels/pillow-*.whl && rm -rf /tmp/wheels
# /opt/drone4d 为遗留安装目录（示例提交镜像的 PYTHONPATH 引用该路径），其下为 anet4d 与 drone4d 两个包
COPY sdk/ /opt/drone4d/
ENV PYTHONPATH=/opt/drone4d PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DRONE4D_AGENT_SOCK=/run/drone4d/agent.sock \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
USER 65534:65534
WORKDIR /tmp
CMD ["python", "-m", "anet4d.serve", "--baseline", "track_share"]
