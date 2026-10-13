// Settings › Advanced › Identity (R-D2-39; ADR-123; AWR-04 §15.9.1; AWR-14 §20.26.6; M15-FR-235, NFR-204; UX-FR-157).
// Owner: M15. A Collapsible at the very bottom of the settings dialog, closed by default, titled like ordinary description
// text (no icon, no badge); it is registered nowhere else (no palette action, hotkey, help entry or deep link; `?settings=`
// has no identity section). Inside: "Identity · for internal staff", a password Input and "Verify".
// The pass phrase lives only in this component's local state: submit reads it, clears the field at once and hands the
// value to identity.verify (one request body), nothing else sees it (no store, storage, log, telemetry or error text).
// Results: ok (badge "internal · full access", expiry, "remember this device for 12 h", "keep running", "leave the internal
// identity" with a confirmation), wrong, locked or rate limited (the button waits for Retry-After), disabled (this
// deployment has no secret file: the input is disabled).
import * as React from 'react'
import { useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { identity, useIdentity } from '@/stores/identity'
import { power, usePower } from '@/app/power/PowerController'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/ui/components/ui/alert-dialog'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/ui/components/ui/collapsible'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/ui/components/ui/field'
import { Input } from '@/ui/components/ui/input'
import { Spinner } from '@/ui/components/ui/spinner'
import { Switch } from '@/ui/components/ui/switch'
import { Icon } from '@/ui/icons/Icon'

function untilText(ms: number): string {
  const d = new Date(ms)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function IdentitySection() {
  const t = useT()
  const tier = useIdentity((s) => s.tier)
  const phase = useIdentity((s) => s.phase)
  const exp = useIdentity((s) => s.expUnixMs)
  const remember = useIdentity((s) => s.remember)
  const retryUntil = useIdentity((s) => s.retryUntilMs)
  const keepRunning = usePower((s) => s.keepRunning)
  // the pass phrase: local component state only, cleared before the request is sent
  const [value, setValue] = React.useState('')
  const [now, setNow] = React.useState(() => Date.now())
  const [confirm, setConfirm] = React.useState(false)
  const waiting = retryUntil > now
  React.useEffect(() => {
    if (!waiting) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [waiting])
  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (!value || phase === 'checking' || waiting) return
    const secret = value
    setValue('')
    const r = await identity.verify(secret)
    setNow(Date.now())
    if (r === 'ok') notify('identity:unlock', 'info', t('identity.unlocked'))
  }
  const internal = tier === 'internal'
  const leftMin = Math.max(1, Math.ceil((retryUntil - now) / 60_000))
  const error = phase === 'wrong' ? t('identity.wrong') : phase === 'locked' || phase === 'rate' ? t('identity.locked', { n: leftMin })
    : phase === 'error' ? t('identity.error') : null
  return (
    <Collapsible className="flex flex-col gap-2" data-settings-advanced="">
      <CollapsibleTrigger render={<Button size="sm" variant="ghost" className="justify-start px-0 font-normal text-muted-foreground" data-advanced-toggle="" />}>
        {t('settings.advanced')}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <FieldGroup data-identity-section={internal ? 'internal' : phase}>
          <Field>
            <FieldLabel>{t('identity.section')}</FieldLabel>
            <FieldDescription>{t('identity.hint')}</FieldDescription>
          </Field>
          {internal ? (
            <>
              <Field orientation="horizontal" className="flex-wrap items-center gap-2">
                <Badge variant="outline" className="gap-1" data-identity-badge="">
                  <Icon icon="identity.shield" />
                  {t('identity.badge')}
                </Badge>
                <span className="text-hud-sub text-muted-foreground tabular-nums" data-identity-until="">{t('identity.until', { time: untilText(exp) })}</span>
                <Button size="sm" variant="outline" className="ml-auto" onClick={() => setConfirm(true)} data-identity-logout="">{t('identity.logout')}</Button>
              </Field>
              <Field orientation="horizontal" className="justify-between">
                <FieldLabel htmlFor="identity-remember" className="font-normal">{t('identity.remember')}</FieldLabel>
                <Switch id="identity-remember" checked={remember} onCheckedChange={(on: boolean) => identity.setRemember(on)} />
              </Field>
              <Field orientation="horizontal" className="justify-between">
                <FieldLabel htmlFor="identity-keep" className="font-normal">{t('power.keepRunning')}</FieldLabel>
                <Switch id="identity-keep" checked={keepRunning} onCheckedChange={(on: boolean) => power.setKeepRunning(on)} data-keep-running={keepRunning ? 'on' : 'off'} />
              </Field>
            </>
          ) : (
            <form className="flex flex-col gap-2" onSubmit={(e) => void submit(e)} autoComplete="off" data-identity-form="">
              <Field orientation="horizontal" className="gap-2" data-invalid={error ? true : undefined}>
                <Input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoComplete="off" spellCheck={false} autoCapitalize="none"
                  autoCorrect="off" disabled={phase === 'disabled'} aria-label={t('identity.section')} aria-invalid={error ? true : undefined}
                  data-identity-input="" className="min-w-0 flex-1" />
                <Button type="submit" size="sm" disabled={!value || phase === 'checking' || waiting || phase === 'disabled'} data-identity-verify="">
                  {phase === 'checking' ? <Spinner data-icon="inline-start" /> : null}
                  {t('identity.verify')}
                </Button>
              </Field>
              {phase === 'disabled' ? <FieldDescription data-identity-error="disabled">{t('identity.disabled')}</FieldDescription> : null}
              {error ? <FieldError data-identity-error={phase}>{error}</FieldError> : null}
            </form>
          )}
        </FieldGroup>
      </CollapsibleContent>
      <AlertDialog open={confirm} onOpenChange={setConfirm}>
        <AlertDialogContent data-identity-logout-dialog="">
          <AlertDialogHeader>
            <AlertDialogTitle>{t('identity.logoutTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('identity.logoutBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction data-identity-logout-confirm="" onClick={() => {
              power.setKeepRunning(false)
              void identity.logout()
            }}>{t('identity.logout')}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Collapsible>
  )
}
