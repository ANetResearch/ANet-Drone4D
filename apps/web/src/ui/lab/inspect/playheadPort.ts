// lab 播放头（stores/lab/playhead.ts，工作区唯一时间权威，AWR-14 §21.13）到 RerunSync 播放头端口的适配。stores/lab/playhead.ts
// 不修改：其 seek 把时刻夹在 [0, durationS]，Viewer 回写的时刻超出当前时长时先扩展时长，避免回写被截断而产生 > 1 帧的偏差。
import { usePlayhead } from '@/stores/lab/playhead'
import type { PlayheadPort } from './rerunSync'

export const labPlayheadPort: PlayheadPort = {
  get: () => {
    const s = usePlayhead.getState()
    return { tS: s.tS, playing: s.playing }
  },
  seek: (tS) => {
    const s = usePlayhead.getState()
    if (tS > s.durationS) s.setDuration(tS)
    usePlayhead.getState().seek(tS)
  },
  pause: () => usePlayhead.getState().pause(),
  subscribe: (fn) => usePlayhead.subscribe((s, prev) => fn({ tS: s.tS, playing: s.playing }, { tS: prev.tS, playing: prev.playing })),
}
