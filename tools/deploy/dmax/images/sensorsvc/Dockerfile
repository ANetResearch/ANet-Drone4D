# d3-sensorsvc（M25 §9.1；WP-D3-12）：sensorsvc worker 与采集作业的运行镜像。只含依赖，代码以只读卷挂载到 /opt/awr（开发期），
# 发布时由 build.sh 以 --build-arg WITH_CODE=1 复制 python/ 进镜像。EGL 由 NVIDIA 容器工具包注入驱动库（graphics 能力），
# glvnd 以 10_nvidia.json 选择 NVIDIA 实现。GPU JPEG（nvImageCodec + nvJPEG，M25-FR-060、183）以离线 wheel 单独一层安装
# （容器与构建环境不依赖外网；wheel 由 build.sh 按 requirements-nvjpeg.lock 的哈希预先下载到 wheels-nvjpeg/），并删除
# nvImageCodec 的 CPU 编解码扩展（libjpeg-turbo、OpenCV、TIFF 等），只保留 libnvjpeg_ext：在线路径的 JPEG 只能由 nvJPEG 产生。
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
COPY requirements-nvjpeg.lock /tmp/req/
COPY wheels-nvjpeg/ /tmp/wh/
RUN pip install --no-cache-dir --no-index --find-links /tmp/wh --require-hashes --no-deps -r /tmp/req/requirements-nvjpeg.lock \
 && rm -rf /tmp/wh /tmp/req \
 && X=$(python -c "import importlib.util as u, os; print(os.path.join(os.path.dirname(u.find_spec('nvidia.nvimgcodec').origin), 'extensions'))") \
 && find "$X" -name 'lib*_ext.so*' ! -name 'libnvjpeg_ext.so*' -delete && ls "$X" | grep -qx 'libnvjpeg_ext.so.*' \
 && test "$(ls "$X" | wc -l)" = 1
RUN useradd --uid 1000 --create-home awr
WORKDIR /opt/awr
USER awr
