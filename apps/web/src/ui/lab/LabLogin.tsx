// /lab/login, workspace sign-in (AWR-14 §21.2.2; AWR-17 §18.3.1 BN-01): the bench token is exchanged for the HttpOnly
// session cookie and is not kept by the page. `next` only accepts relative /lab paths.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate, type RouteProps } from '@/app/router/router'
import { BenchError, benchPost } from '@/net/bench'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/ui/components/ui/field'
import { Input } from '@/ui/components/ui/input'
import { Icon } from '@/ui/icons/Icon'

export function safeNext(raw: string | null): string {
  return raw && /^\/lab(\/[A-Za-z0-9._~\-/]*)?(\?[^\s]*)?$/.test(raw) && !raw.startsWith('//') ? raw : '/lab/runs'
}

export function LabLogin({ search }: RouteProps) {
  const t = useT()
  const [token, setToken] = React.useState('')
  const [err, setErr] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState(false)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      await benchPost('/session', { token: token.trim() })
      setToken('')
      navigate(safeNext(search.get('next')))
    } catch (x) {
      setErr(x instanceof BenchError ? t('lab.login.failed', { code: x.code ?? x.status, name: x.reasonName ?? '' }) : t('lab.unreachable'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section data-view="lab-login" className="app-layer-overlay-page flex items-center justify-center bg-background p-6">
      <form onSubmit={(e) => void submit(e)} className="flex w-full max-w-sm flex-col gap-4">
        <div className="flex items-center gap-2">
          <Icon icon="lease.held" />
          <h1 className="text-ed-title font-bold">{t('lab.login.title')}</h1>
        </div>
        <Field>
          <FieldLabel htmlFor="lab-token">{t('lab.login.token')}</FieldLabel>
          <Input id="lab-token" type="password" autoComplete="off" spellCheck={false} value={token} onChange={(e) => setToken(e.target.value)} />
          <FieldDescription>{t('lab.login.hint')}</FieldDescription>
          {err ? <FieldError>{err}</FieldError> : null}
        </Field>
        <Button type="submit" disabled={busy || token.trim().length < 8}>{t('lab.login.submit')}</Button>
      </form>
    </section>
  )
}
