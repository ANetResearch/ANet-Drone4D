// The landing page's sandbox entry (M15-FR-204; AWR-14 §20.4.1): the shared StartSheet (ui/sandbox/StartSheet.tsx) in
// its own lazily loaded chunk, so the first screen of the landing page still loads only the runtime, react, landing and
// entry chunks (M15-FR-121, AC-203). A created session is entered with a full page load (the application boots with
// its mask and progress bar, the session is recovered from storage); a queue ticket goes to the shared show, where the
// queue card continues the long poll.
import type { CreateResult } from '@/ui/sandbox/session'
import { StartSheet } from '@/ui/sandbox/StartSheet'
import { DEMO_ENTRY } from '@/lib/demo'

export default function SandboxLaunchSheet({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const onResult = (r: CreateResult) => {
    if (r.kind === 'created') location.assign(`/sandbox/${r.sid}`)
    else if (r.kind === 'queued') location.assign(DEMO_ENTRY)
  }
  return <StartSheet open={open} onOpenChange={onOpenChange} onResult={onResult} />
}
