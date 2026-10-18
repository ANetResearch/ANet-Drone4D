# d3-agent-py：容器 Agent 基础镜像（AWR-17 §18.9.1；M26 §7.2）。Python 3.12 + drone4d SDK（线协议 serve 与参考基线），
# 运行时由 Env 以 --network none、只读根文件系统、非 root 用户启动，经挂载的 /run/drone4d/agent.sock 与 Env 通信。
# 内部提交可以此为基础镜像：COPY 自己的包，CMD ["python", "-m", "drone4d.serve", "my_pkg.agent:Agent"]。
# L2 参考检测器（drone4d.perception.ref_det，numpy 推理）需 numpy 与 pillow；BLAS 单线程（容器 1 CPU，且逐字节确定）。
FROM python:3.12-slim
COPY wheels/ /tmp/wheels/
RUN pip install --no-index --no-cache-dir /tmp/wheels/msgpack-*.whl /tmp/wheels/numpy-*.whl /tmp/wheels/pillow-*.whl && rm -rf /tmp/wheels
COPY drone4d/ /opt/drone4d/drone4d/
ENV PYTHONPATH=/opt/drone4d PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DRONE4D_AGENT_SOCK=/run/drone4d/agent.sock \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
USER 65534:65534
WORKDIR /tmp
CMD ["python", "-m", "drone4d.serve", "--baseline", "track_share"]
