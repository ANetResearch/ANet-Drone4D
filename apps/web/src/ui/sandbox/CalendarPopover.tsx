// Simulation calendar and acoustic background (UX-FR-114; AWR-14 §20.5.4 "ENVIRONMENT 组增量"; d2-WP-09-to-M15 items 1 to 4):
// the readout of M07's sky summary (local time in the world's zone, light band; sun elevation and illuminance in the
// popover) and, behind the write gate, the calendar input (`YYYY-MM-DD HH:mm` in the world's zone, converted to UTC
// before `call env/calendar {start_utc, tz}`; quick periods day 10:00, dusk 18:30, night 23:00) and the acoustic background
// (`call env/set {patch: {acoustic_bg}}`). Refusals (110 with detail.field start_utc or tz) show under the field. When the
// visibility drops below 3 km the popover suggests the long-focus thermal view (AWR-04 §9.2 conclusion 3). No date picker:
// shadcn `calendar` stays disabled before V0.6 (AWR-14 §4.7).
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { useEnv } from '@/stores/env'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/ui/components/ui/field'
import { InputGroup, InputGroupInput } from '@/ui/components/ui/input-group'
import { Popover, PopoverContent, PopoverDescription, PopoverHeader, PopoverTitle, PopoverTrigger } from '@/ui/components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Spinner } from '@/ui/components/ui/spinner'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { useRt } from '@/ui/shell/RtContext'
import { DEFAULT_TZ, localToUtc, periodText } from './calendar'
import { useD2Write } from './writeGate'

interface SkyView {
  localTime?: string
  tz?: string
  band?: 'day' | 'low' | 'night'
  sunElDeg?: number
  illumLx?: number
  acoustic?: { profile: 'rural' | 'suburban' | 'urban'; lBgDba: number } | null
}
const skyOf = (s: object): SkyView | null => ((s as { sky?: SkyView | null }).sky ?? null)
const BGS = ['rural', 'suburban', 'urban'] as const
export function CalendarPopover({ variant = 'bar' }: { variant?: 'bar' | 'panel' }) {
  const t = useT()
  const rt = useRt()
  const canWrite = useD2Write()
  const sky = useEnv((s) => skyOf(s))
  const morM = useEnv((s) => s.derived.morM)
  const [text, setText] = React.useState<string | null>(null)
  const [err, setErr] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState(false)
  if (!sky?.localTime) return null
  const tz = sky.tz || DEFAULT_TZ
  const draft = text ?? sky.localTime
  const utc = localToUtc(draft, tz)
  const apply = async () => {
    if (!utc) return
    setBusy(true)
    setErr(null)
    const r = await rt.call('env/calendar', { start_utc: utc, tz }).result
    setBusy(false)
    if (r.status === 'rejected' || r.status === 'failed') setErr(`${reasonText(r.code).short}${(r.detail as { why?: string } | undefined)?.why ? ` (${String((r.detail as { why?: string }).why)})` : ''}`)
    else setText(null)
  }
  const setBg = async (bg: (typeof BGS)[number]) => {
    const r = await rt.call('env/set', { patch: { acoustic_bg: bg } }).result
    if (r.status === 'rejected' || r.status === 'failed') setErr(reasonText(r.code).short)
  }
  return (
    <Popover>
      <PopoverTrigger render={<Button size="xs" variant="ghost" className={variant === 'bar' ? 'h-6 gap-1 px-1.5 text-muted-foreground' : 'w-full justify-start gap-1.5'}
        aria-label={t('calendar.title')} data-session-calendar="" />}>
        <Icon icon="env.time" />
        <span className="font-mono">{variant === 'bar' ? sky.localTime.slice(11) : sky.localTime}</span>
        {sky.band ? <span>{t(`sandbox.band.${sky.band}`)}</span> : null}
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80" data-calendar-popover="">
        <PopoverHeader>
          <PopoverTitle className="flex items-center gap-1.5"><Icon icon="env.time" />{t('calendar.title')}</PopoverTitle>
          <PopoverDescription className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono">{`${sky.localTime} · ${tz}`}</span>
            {sky.band ? <Badge variant="outline">{t(`sandbox.band.${sky.band}`)}</Badge> : null}
          </PopoverDescription>
        </PopoverHeader>
        <p className="font-mono text-hud-sub text-muted-foreground" data-calendar-sun="">
          {t('calendar.sun', { el: fmt.num(sky.sunElDeg ?? Number.NaN, 1), lx: fmt.num(sky.illumLx ?? Number.NaN, (sky.illumLx ?? 0) < 10 ? 3 : 0) })}
        </p>
        {canWrite ? (
          <>
            <Field data-invalid={!utc || err ? '' : undefined}>
              <FieldLabel htmlFor="cal-start" className="text-hud-sub text-muted-foreground">{t('calendar.start', { tz })}</FieldLabel>
              <InputGroup>
                <InputGroupInput id="cal-start" value={draft} placeholder="YYYY-MM-DD HH:mm" aria-invalid={!utc} onChange={(e) => setText(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && void apply()} data-calendar-input="" />
              </InputGroup>
              {!utc ? <FieldError>{t('calendar.format')}</FieldError> : err ? <FieldError>{err}</FieldError> : null}
            </Field>
            <ToggleGroup spacing={0} size="sm" variant="outline" value={[]} aria-label={t('calendar.periods')}
              onValueChange={(v: unknown[]) => (v[0] === 'day' || v[0] === 'dusk' || v[0] === 'night') && setText(periodText(draft, v[0]))}>
              {(['day', 'dusk', 'night'] as const).map((p) => <ToggleGroupItem key={p} value={p}>{t(`sandbox.period.${p}`)}</ToggleGroupItem>)}
            </ToggleGroup>
            <Button size="sm" disabled={!utc || busy || draft === sky.localTime} onClick={() => void apply()} data-calendar-apply="">
              {busy ? <Spinner data-icon="inline-start" /> : null}{t('calendar.apply')}
            </Button>
          </>
        ) : <FieldDescription>{t('calendar.readOnly')}</FieldDescription>}
        <Field>
          <FieldLabel className="text-hud-sub text-muted-foreground">{t('calendar.acoustic')}</FieldLabel>
          <Select items={BGS.map((b) => ({ value: b, label: t(`calendar.bg.${b}`) }))} value={sky.acoustic?.profile ?? 'suburban'} disabled={!canWrite}
            onValueChange={(v) => (v === 'rural' || v === 'suburban' || v === 'urban') && void setBg(v)}>
            <SelectTrigger size="sm" className="w-full" data-calendar-acoustic=""><SelectValue /></SelectTrigger>
            <SelectContent>{BGS.map((b) => <SelectItem key={b} value={b}>{t(`calendar.bg.${b}`)}</SelectItem>)}</SelectContent>
          </Select>
          {sky.acoustic ? <FieldDescription>{t('calendar.bgLevel', { db: fmt.num(sky.acoustic.lBgDba, 0) })}</FieldDescription> : null}
        </Field>
        {Number.isFinite(morM) && morM < 3000 ? <p className="flex items-center gap-1.5 text-hud-sub" data-calendar-haze=""><Icon icon="alert.info" />{t('calendar.hazeHint')}</p> : null}
      </PopoverContent>
    </Popover>
  )
}
