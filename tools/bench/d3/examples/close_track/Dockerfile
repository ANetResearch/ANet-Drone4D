# 示例内部提交镜像：以平台 Agent 基础镜像 d3-agent-py 为底，加入自己的包，入口经 drone4d.serve 提供线协议服务。
FROM d3-agent-py:dev
COPY close_track/ /opt/agent/close_track/
ENV PYTHONPATH=/opt/drone4d:/opt/agent
CMD ["python", "-m", "drone4d.serve", "close_track.agent:Agent"]
