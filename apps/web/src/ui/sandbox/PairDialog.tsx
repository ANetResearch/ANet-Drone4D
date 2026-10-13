// Commander pairing, holder side (R-D2-36 P1; AWR-04 §15.8.2 item 4; AWR-14 §20.26.5; M15-FR-239; UX-FR-161). Owner: M15.
// The sandbox holder (a desktop or a TV) asks for a single-use 8-character pairing code (SB-76, valid 120 s, at most 5 per
// session in 10 min) and shows it with the sandbox id and the countdown; the tablet opens /pair, types the code and gets a
// sandbox token of the same principal (SB-77), so both screens share the seat. "New code" asks again.
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { createPairCode } from '@/net/sandbox/client'
import { ApiError } from '@/net/api'
import { Button } from '@/ui/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/ui/components/ui/dialog'
import { Icon } from '@/ui/icons/Icon'
import { useSandbox } from './session'

const nsToMs = (ns: string): number => {
  try {
    return Number(BigInt(ns) / 1_000_000n)
  } catch {
    return 0
  }
}

export function PairDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const t = useT()
  const sid = useSandbox((s) => s.sid)
  const token = useSandbox((s) => s.token)
  const [code, setCode] = React.useState<{ code: string; untilMs: number } | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [now, setNow] = React.useState(() => Date.now())
  // the request only (state is written from its answer); the dialog body is keyed per opening, so it starts clean
  const request = React.useCallback(() => {
    if (!sid || !token) return
    createPairCode(sid, token).then((r) => {
      setError(null)
      setCode({ code: r.code, untilMs: nsToMs(r.expires_unix_ns) || Date.now() + 120_000 })
    }, (e: unknown) => setError(e instanceof ApiError && e.reason ? `${e.reason} ${reasonText(e.reason).short}` : t('pair.failed')))
  }, [sid, token, t])
  React.useEffect(() => {
    if (open) request()
  }, [open, request])
  const ask = (): void => {
    setError(null)
    request()
  }
  React.useEffect(() => {
    if (!open) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [open])
  const left = code ? Math.min(120, Math.max(0, Math.ceil((code.untilMs - now) / 1000))) : 0
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm" data-pair-dialog="">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-1.5"><Icon icon="device.pair" />{t('pair.title')}</DialogTitle>
          <DialogDescription>{t('pair.hint', { path: '/pair' })}</DialogDescription>
        </DialogHeader>
        {code && left > 0 ? (
          <div className="flex flex-col items-center gap-2 py-2">
            <span className="font-mono text-3xl font-semibold tracking-widest tabular-nums" data-pair-code={code.code}>{code.code}</span>
            <span className="text-hud-sub text-muted-foreground tabular-nums">{t('pair.sidLine', { sid: sid ?? '', s: left })}</span>
          </div>
        ) : null}
        {code && left === 0 ? <p className="text-hud-sub text-muted-foreground">{t('pair.expired')}</p> : null}
        {error ? <p className="text-hud-sub text-muted-foreground" data-pair-error="">{error}</p> : null}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t('common.close')}</Button>
          <Button onClick={ask} data-pair-new="">{t('pair.new')}</Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
