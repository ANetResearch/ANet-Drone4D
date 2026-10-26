# d3-labweb：内部工作区 lab-web 与鉴权反向代理（AWR-05 §7.11、ADR-166；D3-AC-51）。nginx 只监听 ZeroTier 地址（LAB_BIND:LAB_PORT，
# TLS），静态内容为 apps/web 的内部构建（`main`，VITE_AWR_EXT_ROUTES 含 lab 组）；/api/bench/、/api/lab/rt、/rerun/ 与
# /rerun.cloud.v1alpha1.RerunCloudService/ 转发到回环上的 bench-api、lab-gateway 与 rerun-proxy。配置模板在启动时由官方镜像的
# envsubst 入口展开（只替换 LAB_ 前缀变量）。
FROM docker.m.daocloud.io/library/nginx:1.27-alpine
COPY nginx.conf lab-headers.conf lab-next.js /etc/nginx/
COPY lab.conf.template /etc/nginx/templates/lab.conf.template
COPY site/ /usr/share/nginx/lab/
RUN rm -f /etc/nginx/conf.d/default.conf \
 && find /usr/share/nginx/lab -type f \( -name '*.wasm' -o -name '*.js' -o -name '*.mjs' -o -name '*.css' -o -name '*.svg' -o -name '*.json' -o -name '*.html' \) \
      -size +1k -exec sh -c 'gzip -9 -c "$1" > "$1.gz"' _ {} \; \
 && find /usr/share/nginx/lab -type d -exec chmod 0755 {} + && find /usr/share/nginx/lab -type f -exec chmod 0644 {} +
ENV NGINX_ENVSUBST_FILTER=^LAB_
