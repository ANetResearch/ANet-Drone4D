#!/usr/bin/env node
// 轻量 awr.rt.v1 协议客户端（AWR-18 §8.7(2)、PERF-FR-014；M11-AC-020；M11 §9.3 r27 gw_client.mjs 迁移）。
// Node 22 内置 WebSocket（binaryType arraybuffer），不新增依赖。每个客户端：POST /api/auth/token（viewer）→ WS 握手 →
// hello（tier S）→ 订阅 swarm@10、fleet/roster@10、event、关注集 K 架 uav/{id}/state@30、选中 1 架 @60 → 按 --consume-hz
// 模拟消费，归还时以"≥ 3 帧或 ≥ 50 ms"发 ack，2 Hz ping 上报 srttMs。统计 swarm 实际频率、帧率、下行字节速率。
// 输出一行 JSON：{schema: "awr.bench.rtclient.v1", clients: [{swarm_hz, frames_per_s, bytes_per_s, ...}], ...}。
// 用法：node tools/bench/ipc/rt_client.mjs --base http://127.0.0.1:8000 --clients 10 --dur 60 [--focus 32] [--consume-hz 30]
//       [--warmup 3]
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
  if (a.startsWith('--')) acc.push([a.slice(2), all[i + 1] && !all[i + 1].startsWith('--') ? all[i + 1] : 'true'])
  return acc
}, []))
const BASE = args.base ?? 'http://127.0.0.1:8000'
const CLIENTS = Number(args.clients ?? 10)
const DUR = Number(args.dur ?? 60)
const FOCUS = Number(args.focus ?? 32)
const CONSUME_HZ = Number(args['consume-hz'] ?? 30)
const WARMUP = Number(args.warmup ?? 3)
const OP_BATCH = 0x10
const OP_TIME = 0x02
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function token () {
  const r = await fetch(`${BASE}/api/auth/token`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ role: 'viewer', client: 'rt_client.mjs' }),
  })
  if (!r.ok) throw new Error(`token ${r.status}`)
  return (await r.json()).token
}

function runClient (idx, tok) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(BASE.replace(/^http/, 'ws') + '/api/rt', ['awr.rt.v1', `bearer.${tok}`])
    ws.binaryType = 'arraybuffer'
    const topics = new Map()
    const st = { frames: 0, bytes: 0, swarm: 0, latest: 0, ctrl: 0, measuring: false, connId: '' }
    let swarmId = -1
    let hello = false
    let lastAck = 0
    let lastAckAt = performance.now()
    let timers = []
    ws.onerror = (e) => reject(new Error(`ws error ${e.message ?? ''}`))
    ws.onmessage = (ev) => {
      const d = ev.data
      if (typeof d === 'string') {
        const m = JSON.parse(d)
        if (st.measuring) { st.ctrl++; st.bytes += d.length }
        if (m.op === 'serverInfo') {
          st.connId = m.connId
          ws.send(JSON.stringify({ op: 'hello', client: 'rt_client.mjs/1', contracts: m.contracts, tier: 'S', deviceClass: 'software' }))
        } else if (m.op === 'advertise') {
          for (const c of m.channels) topics.set(c.topic, c.id)
          if (!hello) { hello = true; subscribe() }
        }
        return
      }
      const dv = new DataView(d)
      const op = dv.getUint8(0)
      if (op === OP_TIME || op !== OP_BATCH) return
      st.latest = dv.getUint32(4, true)
      if (!st.measuring) return
      st.frames++
      st.bytes += d.byteLength
      let off = 16
      while (off + 16 <= d.byteLength) {
        const ch = dv.getUint16(off, true)
        const len = dv.getUint32(off + 4, true)
        if (ch === swarmId) st.swarm++
        off += 16 + ((len + 7) & ~7)
      }
    }
    const subscribe = async () => {
      swarmId = topics.get('swarm/uav/state') ?? -1
      const ids = [...topics.keys()].filter((t) => t.startsWith('uav/') && t.endsWith('/state')).map((t) => t.split('/')[1]).sort()
      const focus = ids.slice(idx * FOCUS % Math.max(1, ids.length), idx * FOCUS % Math.max(1, ids.length) + FOCUS)
      const sel = ids.length ? ids[(idx * FOCUS + FOCUS) % ids.length] : null
      const subs = [{ topic: 'fleet/roster', rate: 10 }, { topic: 'swarm/state', rate: 10 }, { topic: 'event', rate: 0, mode: 'all' },
        ...focus.map((v) => ({ topic: `uav/${v}/state`, rate: 30 })), ...(sel ? [{ topic: `uav/${sel}/state`, rate: 60 }] : [])]
      for (let i = 0; i < subs.length; i += 20) {
        ws.send(JSON.stringify({ op: 'subscribe', subs: subs.slice(i, i + 20).map((s, j) => ({ id: i + j + 1, mode: 'latest', ...s })) }))
        await sleep(1050)
      }
      const period = 1000 / CONSUME_HZ
      let k = 0
      timers.push(setInterval(() => {
        k++
        const now = performance.now()
        if (st.latest > lastAck && (st.latest - lastAck >= 3 || now - lastAckAt >= 50)) {
          lastAck = st.latest
          lastAckAt = now
          ws.send(JSON.stringify({ op: 'ack', frame: lastAck, fps: CONSUME_HZ }))
        }
        if (k % Math.max(1, Math.round(CONSUME_HZ / 2)) === 0) ws.send(JSON.stringify({ op: 'ping', t: now, srttMs: 1 }))
      }, period))
      await sleep(WARMUP * 1000)
      st.measuring = true
      await sleep(DUR * 1000)
      st.measuring = false
      for (const t of timers) clearInterval(t)
      timers = []
      ws.close(1000)
      resolve({ conn_id: st.connId, swarm_hz: +(st.swarm / DUR).toFixed(2), frames_per_s: +(st.frames / DUR).toFixed(2),
        bytes_per_s: +(st.bytes / DUR).toFixed(1), ctrl_per_s: +(st.ctrl / DUR).toFixed(2), subs: subs.length })
    }
  })
}

const toks = []
for (let i = 0; i < CLIENTS; i++) toks.push(await token())
const results = await Promise.all(toks.map((t, i) => runClient(i, t)))
const swarm = results.map((r) => r.swarm_hz)
console.log(JSON.stringify({ schema: 'awr.bench.rtclient.v1', base: BASE, clients: results, dur_s: DUR,
  swarm_hz_min: Math.min(...swarm), bytes_per_s_max: Math.max(...results.map((r) => r.bytes_per_s)) }))
