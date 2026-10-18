// 烘焙工作线程（bake.mjs）：每个线程装载同一份 WASM、持有整份顶点数组，按主线程给出的瓦片号逐个烘焙并回传瓦片字节。
import { parentPort, workerData } from 'node:worker_threads'

import { initRecast } from './recast.mjs'
import { TileBaker } from './tiles.mjs'

await initRecast()
const baker = new TileBaker(workerData)
parentPort.on('message', (i) => {
  if (i < 0) {
    baker.destroy()
    parentPort.close()
    return
  }
  try {
    const data = baker.build(i)
    if (data) parentPort.postMessage({ i, data }, [data.buffer])
    else parentPort.postMessage({ i, data: null })
  } catch (e) {
    parentPort.postMessage({ i, error: String(e && e.message ? e.message : e) })
  }
})
parentPort.postMessage({ ready: true })
