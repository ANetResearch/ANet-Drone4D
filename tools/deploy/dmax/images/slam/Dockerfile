# d3-livo（M27 §4.9、§6.6.2；ADR-143；D3-AC-14、D3-AC-47）：slam_eval 作业的 FAST-LIVO2 执行镜像。
# 基础镜像为 r1 研究镜像 d3-r1-fast-livo2:noetic（ros:noetic-perception-focal + Sophus a621ff 补丁 + xuankuzcr/rpg_vikit
# + hku-mars/FAST-LIVO2 main@0d2c034，catkin Release；构建上下文见 docs/research/d3/r1-fast-livo2.md §3.1）。
# FAST-LIVO2 为 GPL-2.0，只以独立容器进程运行、经 bag 与文件交互；本仓库不含其源码或链接产物（r1 §7-1）。
# 本层只加入无 rviz 启动文件、容器入口（回放、资源采样、排空、存图、status.json）与真机回归的 RTK 抽取脚本。
ARG BASE=d3-r1-fast-livo2:noetic
FROM ${BASE}
ARG D3_LIVO_BUILD=dev
COPY run_bag.launch /ws/src/FAST-LIVO2/launch/run_bag.launch
COPY run_livo.sh extract_rtk.py /opt/d3-livo/
RUN chmod +x /opt/d3-livo/run_livo.sh /opt/d3-livo/extract_rtk.py \
 && test -x /ws/devel/lib/fast_livo/fastlivo_mapping \
 && echo "${D3_LIVO_BUILD}" > /opt/d3-livo/BUILD
LABEL org.drone4d.component="d3-livo" org.drone4d.upstream="hku-mars/FAST-LIVO2@0d2c034" org.drone4d.license.upstream="GPL-2.0" \
      org.drone4d.build="${D3_LIVO_BUILD}"
WORKDIR /ws
ENTRYPOINT ["bash", "/opt/d3-livo/run_livo.sh"]
