// /pair, the joining side of the commander pairing (R-D2-36 P1; AWR-04 §15.8.2 item 4; M15-FR-239; UX-FR-161). Owner: M15.
// The tablet enters the sandbox id (prefilled from ?sid=) and the 8-character code shown by the holder; SB-77 answers a
// sandbox token of the same principal, the session port adopts it and the page opens /sandbox/{sid} (both devices share
// the seat). Wrong, used or expired codes show the reason code text.
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { redeemPairCode } from '@/net/sandbox/client'
import { ApiError } from '@/net/api'
import { Button } from '@/ui/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/ui/components/ui/card'
import { Field, FieldError, FieldGroup, FieldLabel } from '@/ui/components/ui/field'
import { Input } from '@/ui/components/ui/input'
import { Spinner } from '@/ui/components/ui/spinner'
import { Icon } from '@/ui/icons/Icon'
import { sandbox, SID_RE } from '@/ui/sandbox/session'

const nsToMs = (ns: string | undefined): number => {
  try {
    return ns ? Number(BigInt(ns) / 1_000_000n) : 0
  } catch {
    return 0
  }
}

export function PairPage({ search }: { search: URLSearchParams }) {
  const t = useT()
  const [sid, setSid] = React.useState(() => search.get('sid') ?? '')
  const [code, setCode] = React.useState('')
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const ok = SID_RE.test(sid.trim()) && /^[A-Za-z0-9]{8}$/.test(code.trim())
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!ok || busy) return
    setBusy(true)
    setError(null)
    try {
      const r = await redeemPairCode(sid.trim(), code)
      sandbox.adopt(r.session.sid, r.token, nsToMs(r.token_exp_unix_ns) || nsToMs(r.session.expires_unix_ns), r.session.world_id)
      navigate(`/sandbox/${r.session.sid}`)
    } catch (err) {
      setError(err instanceof ApiError && err.reason ? `${err.reason} ${reasonText(err.reason).short}` : t('pair.failed'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="fixed inset-0 z-(--z-overlay-page) flex items-center justify-center bg-background p-4" data-view="pair">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-1.5"><Icon icon="device.pair" />{t('pair.enter')}</CardTitle>
          <CardDescription>{t('pair.enterHint')}</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={(e) => void submit(e)} autoComplete="off">
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="pair-sid">{t('pair.sid')}</FieldLabel>
                <Input id="pair-sid" value={sid} onChange={(e) => setSid(e.target.value)} spellCheck={false} autoCapitalize="none" className="font-mono" data-pair-sid="" />
              </Field>
              <Field data-invalid={error ? true : undefined}>
                <FieldLabel htmlFor="pair-code">{t('pair.codeLabel')}</FieldLabel>
                <Input id="pair-code" value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} maxLength={8} spellCheck={false} autoCapitalize="characters"
                  className="font-mono tracking-widest" data-pair-input="" />
                {error ? <FieldError data-pair-error="">{error}</FieldError> : null}
              </Field>
              <Button type="submit" disabled={!ok || busy} data-pair-join="">
                {busy ? <Spinner data-icon="inline-start" /> : null}
                {t('pair.join')}
              </Button>
            </FieldGroup>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
