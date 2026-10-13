// "Open my sandbox" Sheet (AWR-04 §10.2, §10.4; M15-FR-204, FR-209; UX-FR-106, UX-FR-107; AWR-14 §20.4.1, §20.13.1),
// shared by the landing page (lazy, app/demo/SandboxLaunchSheet.tsx) and the application (shared show, static browsing,
// World Hub). City Select with availability (GET /worlds: available, admit_now), scenario template RadioGroup with the
// first-feedback line (GET /templates?world=, default T5), simulation period ToggleGroup (day 10:00, dusk 18:30, night
// 23:00 in the world's zone; T6 suggests night), "restore my last configuration" when this browser keeps a snapshot of
// that world, the pool summary (GET /meta). Start: POST /sessions through ui/sandbox/session.ts, with the proof of work
// solved in a worker ("verifying the browser", shown after 300 ms); 201 enters the sandbox, 202 queues (the queue card),
// 508 lists the worlds that can start now and offers queuing, 504 shows the quota text with the Retry-After countdown,
// 200 without a token says the sandbox runs in another tab and offers a second one. D2.1 (ADR-122): only windows narrower
// than 360 CSS px cannot start; the public capacity (M17 stores/capacity, polled every 10 s while the Sheet is open) shows
// the same line as the boot mask and, when the pool is full, "needs queue · position y + 1 · about t" (AWR-04 §15.6.4 item 1).
// No react-query here: the landing page mounts this without the application providers.
import type { SessionMeta, SessionTemplateItem, SessionWorldItem, Snapshot } from '@awr/contracts/types'
import * as React from 'react'
import { getLocale, reasonText, useT } from '@/app/i18n'
import { sanitizeText } from '@/lib/sanitize'
import { Alert, AlertAction, AlertDescription } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Checkbox } from '@/ui/components/ui/checkbox'
import { Field, FieldDescription, FieldLabel } from '@/ui/components/ui/field'
import { RadioGroup, RadioGroupItem } from '@/ui/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { Spinner } from '@/ui/components/ui/spinner'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { DEFAULT_TZ, periodCalendar, type DayPeriod } from './calendar'
import { localSnapshot, sandbox, useSandbox, windowLargeEnough, type CreateResult } from './session'
import { acquireCapacityPolling, useCapacity } from '@/stores/capacity'
import { capacityLine, sandboxQueueHint } from '@/app/i18n/capacity'
import { useFetched, useSecond } from './useFetched'

export const DEFAULT_WORLD = 'synthcity'
export const DEFAULT_TEMPLATE = 't5_area_guard'
export const MANUAL_TEMPLATE = 't6_manual'
const PERIODS: readonly DayPeriod[] = ['day', 'dusk', 'night']

/** template label: the server's Chinese name under the zh-CN UI, else the i18n name of the known ids (the server has
 *  no English name, so the English UI always uses the i18n dictionary, never the zh_ fields: ANET-PERM-landing-02) */
export function templateLabel(t: (k: string) => string, tpl: { template_id: string; name_zh?: string }): string {
  return getLocale() === 'zh-CN' && tpl.name_zh ? sanitizeText(tpl.name_zh, 40) : t(`sandbox.template.${tpl.template_id}`)
}
/** the template's one-line description (when it says more than the name) and its first feedback (AWR-14 §20.13.1);
 *  the server's zh_ fields are only used under the zh-CN UI (no English equivalent exists yet) */
export function TemplateLines({ tpl }: { tpl: { template_id: string; name_zh?: string; summary_zh?: string; first_feedback_zh?: string } }) {
  const t = useT()
  const zh = getLocale() === 'zh-CN'
  const summary = zh && tpl.summary_zh && tpl.summary_zh !== tpl.name_zh ? sanitizeText(tpl.summary_zh, 120) : null
  const first = zh && tpl.first_feedback_zh ? sanitizeText(tpl.first_feedback_zh, 60) : t(`sandbox.template.${tpl.template_id}.first`)
  return (
    <>
      {summary ? <span className="text-muted-foreground">{summary}</span> : null}
      <span className="text-hud-sub text-muted-foreground">{t('sandbox.template.firstFeedback', { text: first })}</span>
    </>
  )
}
export function worldLabel(w: { world_id: string; name_zh?: string }): string {
  return w.name_zh ? `${sanitizeText(w.name_zh, 24)} · ${w.world_id}` : w.world_id
}
/** availability key of a world: start now, capacity (queue), unavailable */
export function availabilityOf(w: Pick<SessionWorldItem, 'available' | 'admit_now'>): 'now' | 'queue' | 'off' {
  return !w.available ? 'off' : w.admit_now ? 'now' : 'queue'
}

/** window size that follows resizes (the start button and its reason) */
function useWindowOk(): boolean {
  const [ok, setOk] = React.useState(() => windowLargeEnough())
  React.useEffect(() => {
    const on = () => setOk(windowLargeEnough())
    addEventListener('resize', on)
    return () => removeEventListener('resize', on)
  }, [])
  return ok
}

export interface StartSheetProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** preselected world (World Hub "open a sandbox in this city") */
  world?: string | null
  /** start from this snapshot ("new sandbox from the snapshot" of the ended dialog) */
  snapshot?: Snapshot | null
  /** what to do after the request: enter the sandbox, or show the queue card */
  onResult: (r: CreateResult) => void
}

export function StartSheet(props: StartSheetProps) {
  // the form starts afresh on every opening (initial state from the props, no state reset inside effects)
  const [seq, setSeq] = React.useState(0)
  const [wasOpen, setWasOpen] = React.useState(props.open)
  if (props.open !== wasOpen) {
    setWasOpen(props.open)
    if (props.open) setSeq((n) => n + 1)
  }
  return <StartSheetBody key={seq} {...props} />
}

function StartSheetBody({ open, onOpenChange, world: preset = null, snapshot = null, onResult }: StartSheetProps) {
  const t = useT()
  const winOk = useWindowOk()
  const phase = useSandbox((s) => s.phase)
  const error = useSandbox((s) => s.error)
  const anotherTab = useSandbox((s) => s.anotherTab)
  const showVerify = useSandbox((s) => s.verifyHint)
  const retryUntil = useSandbox((s) => s.retryUntilMs)
  const now = useSecond()
  const [world, setWorld] = React.useState<string>(snapshot?.world_id ?? preset ?? DEFAULT_WORLD)
  const [template, setTemplate] = React.useState<string>(snapshot?.template_id ?? DEFAULT_TEMPLATE)
  const [period, setPeriod] = React.useState<DayPeriod>('day')
  const [restore, setRestore] = React.useState(false)
  const [busy, setBusy] = React.useState(false)
  const worlds = useFetched<{ items: SessionWorldItem[] }>(open ? '/worlds' : null).data?.items ?? null
  const meta = useFetched<SessionMeta>(open ? '/meta' : null).data
  const templates = useFetched<{ items: SessionTemplateItem[] }>(open ? `/templates?world=${encodeURIComponent(world)}` : null).data?.items ?? null
  const retryLeft = Math.max(0, Math.ceil((retryUntil - now) / 1000))

  const info = worlds?.find((w) => w.world_id === world) ?? null
  const tz = info?.anchor?.tz ?? templates?.[0]?.calendar_default?.tz ?? DEFAULT_TZ
  const saved = React.useMemo(() => (open ? localSnapshot(world) : null), [open, world])
  const tplItems = templates ?? []
  const tplKnown = tplItems.some((x) => x.template_id === template)
  const unavailable = info !== null && !info.available
  const start = async (o: { forceNew?: boolean; queueOk?: boolean; worldOverride?: string } = {}) => {
    const w = o.worldOverride ?? world
    if (o.worldOverride) setWorld(o.worldOverride)
    setBusy(true)
    const snap = snapshot ?? (restore && saved ? saved.snapshot : null)
    const r = await sandbox.create(w, snap ? null : template, { calendar: periodCalendar(period, tz), configSnapshot: snap, forceNew: o.forceNew, queueOk: o.queueOk })
    setBusy(false)
    if (r.kind === 'created' || r.kind === 'queued') onResult(r)
  }
  const choosePeriodFor = (tpl: string) => {
    if (tpl === MANUAL_TEMPLATE) setPeriod('night')
  }
  const available = Array.isArray(error?.detail?.available_now) ? (error.detail.available_now as unknown[]).filter((x): x is string => typeof x === 'string') : []
  const creating = busy || phase === 'creating' || phase === 'challenge'
  const worldItems = (worlds ?? []).map((w) => ({ value: w.world_id, label: worldLabel(w) }))
  return (
    <Sheet open={open} onOpenChange={(o) => onOpenChange(o)}>
      <SheetContent side="right" className="w-120 max-w-full sm:max-w-120" data-start-sheet="">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2"><Icon icon="sandbox.mine" />{t('sandbox.start.title')}</SheetTitle>
          <SheetDescription>{t('sandbox.start.desc')}</SheetDescription>
        </SheetHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4">
          <Field>
            <FieldLabel>{t('sandbox.start.world')}</FieldLabel>
            <Select items={worldItems} value={world} onValueChange={(v) => {
              if (typeof v !== 'string') return
              setWorld(v)
              setRestore(false)
            }}>
              <SelectTrigger className="w-full" aria-label={t('sandbox.start.world')} data-start-world=""><SelectValue placeholder={t('sandbox.start.world')} /></SelectTrigger>
              <SelectContent>
                {(worlds ?? []).map((w) => (
                  <SelectItem key={w.world_id} value={w.world_id} disabled={!w.available}>
                    <span className="flex w-full items-center justify-between gap-2">
                      <span>{worldLabel(w)}</span>
                      <Badge variant="outline">{t(`sandbox.avail.${availabilityOf(w)}`)}</Badge>
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <FieldDescription data-start-world-state={info ? availabilityOf(info) : ''}>
              {info ? `${t(`sandbox.avail.${availabilityOf(info)}`)}${info.tier === 'L' ? ` · ${t('sandbox.start.large')}` : ''}` : worlds === null ? t('common.loading') : ''}
            </FieldDescription>
          </Field>
          {snapshot ? (
            <Alert data-start-from-snapshot="">
              <Icon icon="data.folder" />
              <AlertDescription>{t('sandbox.start.fromSnapshot', { world: snapshot.world_id })}</AlertDescription>
            </Alert>
          ) : (
            <Field>
              <FieldLabel>{t('sandbox.start.template')}</FieldLabel>
              {templates === null ? <Spinner /> : (
                <RadioGroup value={tplKnown ? template : ''} onValueChange={(v) => {
                  if (typeof v !== 'string') return
                  setTemplate(v)
                  choosePeriodFor(v)
                }} aria-label={t('sandbox.start.template')} data-start-templates="">
                  {tplItems.map((x) => (
                    <Field key={x.template_id} orientation="horizontal" className="items-start">
                      <RadioGroupItem value={x.template_id} id={`tpl-${x.template_id}`} data-template={x.template_id} />
                      <FieldLabel htmlFor={`tpl-${x.template_id}`} className="flex flex-col items-start gap-0.5 font-normal">
                        <span className="font-medium">
                          {templateLabel(t, x)}
                          {x.recommended || x.template_id === DEFAULT_TEMPLATE ? <Badge variant="secondary" className="ml-1.5">{t('sandbox.start.recommended')}</Badge> : null}
                        </span>
                        <TemplateLines tpl={x} />
                      </FieldLabel>
                    </Field>
                  ))}
                </RadioGroup>
              )}
              {template === MANUAL_TEMPLATE ? <FieldDescription>{t('sandbox.start.nightHint')}</FieldDescription> : null}
            </Field>
          )}
          <Field>
            <FieldLabel>{t('sandbox.start.period')}</FieldLabel>
            <ToggleGroup spacing={0} size="sm" variant="outline" value={[period]} aria-label={t('sandbox.start.period')} data-start-period=""
              onValueChange={(v: unknown[]) => PERIODS.includes(v[0] as DayPeriod) && setPeriod(v[0] as DayPeriod)}>
              {PERIODS.map((p) => <ToggleGroupItem key={p} value={p}>{t(`sandbox.period.${p}`)}</ToggleGroupItem>)}
            </ToggleGroup>
          </Field>
          {saved && !snapshot ? (
            <Field orientation="horizontal">
              <Checkbox id="start-restore" checked={restore} onCheckedChange={(c: boolean) => setRestore(c === true)} data-start-restore="" />
              <FieldLabel htmlFor="start-restore" className="font-normal">{t('sandbox.start.restore')}</FieldLabel>
            </Field>
          ) : null}
          {anotherTab ? (
            <Alert data-start-another-tab="">
              <Icon icon="alert.info" />
              <AlertDescription>{t('sandbox.start.anotherTab')}</AlertDescription>
              <AlertAction><Button size="xs" variant="outline" onClick={() => void start({ forceNew: true })}>{t('sandbox.start.forceNew')}</Button></AlertAction>
            </Alert>
          ) : null}
          {error && error.message !== 'small_window' ? (
            <Alert variant={error.code === 508 || error.code === 504 ? 'default' : 'destructive'} data-start-error={error.code ?? error.status}>
              <Icon icon="alert.warning" />
              <AlertDescription className="flex flex-col gap-2">
                <span>{error.code ? reasonText(error.code).short : t('sandbox.start.failed')}</span>
                {error.code === 504 && retryLeft > 0 ? <span data-start-retry={retryLeft}>{t('sandbox.start.retryIn', { s: retryLeft })}</span> : null}
                {error.code === 508 ? (
                  <span className="flex flex-wrap items-center gap-1.5" data-start-available="">
                    {available.map((w) => <Button key={w} size="xs" variant="outline" onClick={() => void start({ worldOverride: w })}>{w}</Button>)}
                    <Button size="xs" variant="ghost" onClick={() => void start({ queueOk: true })}>{t('sandbox.start.queue')}</Button>
                  </span>
                ) : null}
              </AlertDescription>
            </Alert>
          ) : null}
          {showVerify ? (
            <p className="flex items-center gap-2 text-muted-foreground" data-start-verifying="">
              <Spinner />
              {t('sandbox.start.verifying')}
            </p>
          ) : null}
          {meta ? <p className="text-muted-foreground" data-start-meta="">{t('sandbox.start.meta', { n: meta.active_sessions, max: meta.max_sessions, q: meta.queue_len })}</p> : null}
          <StartCapacity open={open} />
        </div>
        <SheetFooter className="flex-row justify-end gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button disabled={!winOk || creating || unavailable || (!snapshot && !tplKnown && templates !== null && tplItems.length > 0) || retryLeft > 0}
            onClick={() => void start()} data-start-go="">
            {creating ? <Spinner data-icon="inline-start" /> : <Icon icon="sandbox.mine" data-icon="inline-start" />}
            {t('sandbox.start.go')}
          </Button>
        </SheetFooter>
        {!winOk ? <p className="px-4 pb-3 text-muted-foreground" data-start-small="">{t('sandbox.smallWindow')}</p> : null}
      </SheetContent>
    </Sheet>
  )
}

/** the capacity line of the Sheet (and its "needs queue" hint) while it is open */
function StartCapacity({ open }: { open: boolean }) {
  const t = useT()
  const data = useCapacity((s) => s.data)
  const phase = useCapacity((s) => s.phase)
  React.useEffect(() => (open ? acquireCapacityPolling('start-sheet') : undefined), [open])
  if (phase === 'idle' || phase === 'loading') return null
  const line = capacityLine(phase === 'ok' ? data : null)
  const hint = sandboxQueueHint(data)
  return (
    <p className="flex flex-wrap items-center gap-2 text-muted-foreground tabular-nums" data-start-capacity={line.kind}>
      <span>{line.text}</span>
      {hint ? <Badge variant="outline" data-start-queue-hint="">{hint}</Badge> : null}
      {!hint && line.badge ? <Badge variant="outline">{line.badge}</Badge> : null}
      <span className="sr-only">{t('capacity.title')}</span>
    </p>
  )
}
