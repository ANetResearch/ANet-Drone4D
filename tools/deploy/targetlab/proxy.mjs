// Loopback-only review gateway. The API serves the built UI/worlds; sandbox has its own REST/WS service.
// Node built-ins only: production review does not need npm/node_modules on the host.
import http from 'node:http'
import net from 'node:net'

const targetPort = (url) => url.startsWith('/api/sandbox/') ? 8272 : 8270
const server = http.createServer((req, res) => {
  const upstream = http.request({ host: '127.0.0.1', port: targetPort(req.url ?? '/'),
    method: req.method, path: req.url, headers: req.headers }, (reply) => {
    res.writeHead(reply.statusCode ?? 502, reply.headers)
    reply.pipe(res)
    reply.on('error', () => res.destroy())
  })
  upstream.on('error', () => {
    if (res.headersSent) return res.destroy()
    res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' })
    res.end('Target lab backend is starting or unavailable. Please retry shortly.')
  })
  req.on('aborted', () => upstream.destroy())
  res.on('close', () => upstream.destroy())
  req.pipe(upstream)
})
server.on('upgrade', (req, socket, head) => {
  const upstream = net.connect(targetPort(req.url ?? '/'), '127.0.0.1', () => {
    const lines = [`${req.method} ${req.url} HTTP/${req.httpVersion}`]
    for (let i = 0; i < req.rawHeaders.length; i += 2) lines.push(`${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}`)
    upstream.write(`${lines.join('\r\n')}\r\n\r\n`)
    if (head.length) upstream.write(head)
    socket.pipe(upstream).pipe(socket)
  })
  socket.on('error', () => upstream.destroy())
  upstream.on('error', () => socket.destroy())
  socket.on('close', () => upstream.destroy())
  upstream.on('close', () => socket.destroy())
})
server.listen(5443, '127.0.0.1', () => console.log('Target lab: http://127.0.0.1:5443'))
