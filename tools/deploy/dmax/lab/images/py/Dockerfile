# d3-lab-py：内部工作区服务镜像（AWR-05 §7.11、ADR-166；D3-AC-51、D3-AC-25）。同一镜像以不同命令运行 dmax `d3` profile 的
# bench-api（`python -m awr.api.bench_main`）、lab-gateway（`python -m awr.bench.api.gateway`）、rerun-proxy
# （`python -m awr.bench.api.rerun_proxy`）与两个 Rerun catalog（`python -m awr.bench.catalog serve`），以及运维脚本 labctl。
# 代码与契约在构建时复制（镜像标签为提交号），依赖只从离线 wheel 安装（容器内无 DNS）；运行用户 10001，Rerun 使用统计关闭。
FROM python:3.12-slim
RUN groupadd -g 10001 lab && useradd -u 10001 -g 10001 -m -d /home/lab -s /usr/sbin/nologin lab
COPY wheels/ /tmp/wheels/
COPY requirements-lab.txt /tmp/requirements-lab.txt
RUN pip install --no-index --no-cache-dir --find-links /tmp/wheels -r /tmp/requirements-lab.txt && rm -rf /tmp/wheels \
 && su -s /bin/sh lab -c 'rerun analytics disable >/dev/null 2>&1 || true'
COPY python/ /opt/awr/python/
COPY contracts/ /opt/awr/contracts/
COPY labctl.py /opt/awr/tools/labctl.py
ENV PYTHONPATH=/opt/awr/python AWR_CONTRACTS_DIR=/opt/awr/contracts PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/lab \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MALLOC_ARENA_MAX=2
USER 10001:10001
WORKDIR /home/lab
CMD ["python", "-m", "awr.api.bench_main"]
