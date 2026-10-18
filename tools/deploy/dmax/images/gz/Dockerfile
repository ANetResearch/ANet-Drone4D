# d3-gz（M24 §6.1；WP-D3-20）：gz-sim 8（Harmonic，ROS 2 Jazzy vendor 包）+ libDrone4DGz.so（Drone4DClock、Drone4DVelocityControl）
# + x500_d4d 物理模型。physics 导出档只用 CPU，不加载渲染。基础镜像为 r9 研究镜像（ubuntu 24.04、ros-jazzy gz vendor 包、
# g++、libzmq3-dev、cppzmq-dev、libprotobuf-dev、protobuf-compiler、libeigen3-dev；构建方式见 r5-gazebo/docker/Dockerfile）。
ARG BASE=d3-r9-gz-sim:dev
FROM ${BASE}
ARG D4D_PLUGIN_BUILD=dev
COPY gz /src/d4d-gz
RUN bash -c "source /opt/ros/jazzy/setup.bash \
 && cmake -S /src/d4d-gz -B /tmp/b -DCMAKE_BUILD_TYPE=Release -DD4D_PLUGIN_BUILD=${D4D_PLUGIN_BUILD} -DCMAKE_INSTALL_PREFIX=/opt/d4d \
 && cmake --build /tmp/b -j8 && cmake --install /tmp/b && rm -rf /tmp/b" \
 && mkdir -p /opt/d4d/models && cp -r /src/d4d-gz/models/* /opt/d4d/models/ \
 && printf '#!/bin/bash\nsource /opt/ros/jazzy/setup.bash\nexec "$@"\n' > /opt/d4d/entry.sh && chmod +x /opt/d4d/entry.sh
ENV GZ_SIM_SYSTEM_PLUGIN_PATH=/opt/d4d/lib GZ_SIM_RESOURCE_PATH=/opt/d4d/models
ENTRYPOINT ["/opt/d4d/entry.sh"]
