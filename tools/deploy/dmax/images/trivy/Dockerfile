# d3-trivy：提交镜像漏洞扫描器（ADR-204；D3-AC-59）。只含 Trivy v0.75.0 静态二进制（fetch.sh 校验 sha256 后放入构建上下文），
# 运行时由 subworker 以 --network none、只读根文件系统、非 root 用户启动：只读挂载离线库（/cache）与 `docker save` 导出的镜像归档
# （/in/image.tar），不挂载 docker 套接字，扫描输出 JSON 到标准输出。
FROM scratch
COPY trivy /usr/local/bin/trivy
ENV HOME=/tmp TMPDIR=/tmp TRIVY_NO_PROGRESS=true TRIVY_DISABLE_TELEMETRY=true
USER 65534:65534
WORKDIR /tmp
ENTRYPOINT ["/usr/local/bin/trivy"]
