# d3-sensorsvc（M25 §9.1；WP-D3-12）：sensorsvc worker 与采集作业的运行镜像。只含依赖，代码以只读卷挂载到 /opt/awr（开发期），
# 发布时由 build.sh 以 --build-arg WITH_CODE=1 复制 python/ 进镜像。EGL 由 NVIDIA 容器工具包注入驱动库（graphics 能力），
# glvnd 以 10_nvidia.json 选择 NVIDIA 实现。
FROM python:3.12-slim
ARG PIP_INDEX_URL=https://mirrors.bfsu.edu.cn/pypi/web/simple
ARG PIP_TRUSTED_HOST=mirrors.bfsu.edu.cn
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NVIDIA_DRIVER_CAPABILITIES=graphics,compute,utility PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/awr/python
RUN apt-get update && apt-get install -y --no-install-recommends libegl1 libgl1 libgles2 libglvnd0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*
COPY 10_nvidia.json /usr/share/glvnd/egl_vendor.d/10_nvidia.json
COPY requirements.lock requirements-d3.lock requirements-gpu.lock /tmp/req/
RUN pip install --no-cache-dir --require-hashes -r /tmp/req/requirements.lock -r /tmp/req/requirements-d3.lock \
      -r /tmp/req/requirements-gpu.lock && rm -rf /tmp/req
RUN useradd --uid 1000 --create-home awr
WORKDIR /opt/awr
USER awr
