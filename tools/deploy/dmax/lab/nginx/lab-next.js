// d3-labweb 登录跳转参数（K12；AWR-14 §21.2.2"未登录访问跳转 /lab/login?next=<原路径>"）：next 为原始请求路径加查询串
// （$request_uri）经 encodeURIComponent 编码，与前端 ui/lab/useBenchSession.ts 的
// `encodeURIComponent(location.pathname + location.search)` 同一口径；此前以 $uri 拼接，查询串（如 ?view=eval、?a=&b=）在登录后丢失。
function next(r) {
  return encodeURIComponent(r.variables.request_uri || '/lab/runs')
}

export default { next }
