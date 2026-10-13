// Sandbox dialogs (AWR-14 §20.4.1, §20.4.6, §20.12.2, §20.13.1; M15-FR-204, FR-208 to FR-210; UX-FR-110, UX-FR-141,
// UX-FR-142): one host for the in-app "open my sandbox" Sheet, "load scenario" (template picker), "switch world" and the
// "sandbox ended" dialog, driven by a small UI store so the session bar, menus, the World Hub and the command palette open
// them the same way. The world switch keeps sid and token (POST /sessions/{sid}/world); the canvas follows the new world
// once the session is ready (interim: the page reloads into the same sid until the point cloud layer reads the sandbox
// world, request to M05).
import type { SessionTemplateItem, SessionWorldItem, Snapshot } from '@awr/contracts/types'
import * as React from 'react'
import { useStore } from 'zustand'
import { reasonText, useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { createAwrStore } from '@/lib/createStore'
import { DEMO_ENTRY } from '@/lib/demo'
import { prefs } from '@/stores/prefs'
import { viewport } from '@/viewport/facade'
import { Alert, AlertDescription } from '@/ui/components/ui/alert'
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader,
  AlertDialogTitle,
} from '@/ui/components/ui/alert-dialog'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/ui/components/ui/dialog'
import { Field, FieldDescription, FieldLabel } from '@/ui/components/ui/field'
import { RadioGroup, RadioGroupItem } from '@/ui/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Spinner } from '@/ui/components/ui/spinner'
import { Icon } from '@/ui/icons/Icon'
import { useModalFrameCap } from '@/ui/views/useModalFrameCap'
import { availabilityOf, StartSheet, templateLabel, TemplateLines, worldLabel } from './StartSheet'
import { localSnapshot, sandbox, useSandbox, type CreateResult, type SandboxError } from './session'
import { useFetched } from './useFetched'
import { PairDialog } from './PairDialog'
import { power } from '@/app/power/PowerController'

type DialogId = 'start' | 'template' | 'world' | 'pair' | null
interface DialogsState { open: DialogId; world: string | null; snapshot: Snapshot | null; seq: number }
const dialogsStore = createAwrStore<DialogsState>('ui.sandbox.dialogs', () => ({ open: null, world: null, snapshot: null, seq: 0 }))

export const sandboxDialogs = {
  open(id: Exclude<DialogId, null>, o: { world?: string | null; snapshot?: Snapshot | null } = {}): void {
    if (id === 'start') sandbox.clearError()
    // every opening is a fresh form (the dialog bodies are keyed by seq)
    dialogsStore.setState({ open: id, world: o.world ?? null, snapshot: o.snapshot ?? null, seq: dialogsStore.getState().seq + 1 })
  },
  close(): void {
    dialogsStore.setState({ open: null })
  },
  /** export the configuration snapshot and save it as a JSON file (also kept in this browser) */
  async downloadSnapshot(snap?: Snapshot | null): Promise<boolean> {
    const s = snap ?? (await sandbox.exportSnapshot().catch(() => null))
    if (!s || typeof document === 'undefined') return false
    const url = URL.createObjectURL(new Blob([JSON.stringify(s, null, 1)], { type: 'application/json' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `sandbox-${s.world_id}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.json`
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    return true
  },
}

/** "load scenario": the templates of the session's world; loading replaces vehicles, targets, nests and tasks */
function TemplatePicker({ open }: { open: boolean }) {
  const t = useT()
  const world = useSandbox((s) => s.world)
  const current = useSandbox((s) => s.template)
  const items = useFetched<{ items: SessionTemplateItem[] }>(open && world ? `/templates?world=${encodeURIComponent(world)}` : null).data?.items ?? null
  const [pick, setPick] = React.useState<string | null>(current)
  const [busy, setBusy] = React.useState(false)
  const [err, setErr] = React.useState<SandboxError | null>(null)
  const cap = useModalFrameCap('modal:sandbox-template')
  const go = async () => {
    if (!pick) return
    setBusy(true)
    const e = await sandbox.loadTemplate(pick)
    setBusy(false)
    if (e) setErr(e)
    else sandboxDialogs.close()
  }
  return (
    <Dialog open={open} onOpenChange={(o) => {
      cap.onOpenChange(o)
      if (!o) sandboxDialogs.close()
    }} onOpenChangeComplete={cap.onOpenChangeComplete}>
      <DialogContent className="sm:max-w-md" data-template-picker="">
        <DialogHeader>
          <DialogTitle>{t('sandbox.bar.template')}</DialogTitle>
          <DialogDescription>{t('sandbox.template.replaceHint')}</DialogDescription>
        </DialogHeader>
        {items === null ? <Spinner /> : (
          <RadioGroup value={pick ?? ''} onValueChange={(v) => typeof v === 'string' && setPick(v)} aria-label={t('sandbox.start.template')}>
            {items.map((x) => (
              <Field key={x.template_id} orientation="horizontal" className="items-start">
                <RadioGroupItem value={x.template_id} id={`pick-${x.template_id}`} data-template={x.template_id} />
                <FieldLabel htmlFor={`pick-${x.template_id}`} className="flex flex-col items-start gap-0.5 font-normal">
                  <span className="font-medium">{templateLabel(t, x)}{x.template_id === current ? <Badge variant="outline" className="ml-1.5">{t('sandbox.template.current')}</Badge> : null}</span>
                  <TemplateLines tpl={x} />
                </FieldLabel>
              </Field>
            ))}
          </RadioGroup>
        )}
        {err ? <Alert variant="destructive"><Icon icon="alert.warning" /><AlertDescription>{err.code ? reasonText(err.code).short : err.message}</AlertDescription></Alert> : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => void sandboxDialogs.downloadSnapshot()}>{t('sandbox.exportSnapshot')}</Button>
          <Button disabled={!pick || busy} onClick={() => void go()} data-template-load="">{busy ? <Spinner data-icon="inline-start" /> : null}{t('sandbox.template.load')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** "switch world": the seven worlds with availability; the configuration is not carried over (same-name template) */
function WorldSwitch({ open }: { open: boolean }) {
  const t = useT()
  const current = useSandbox((s) => s.world)
  const template = useSandbox((s) => s.template)
  const worlds = useFetched<{ items: SessionWorldItem[] }>(open ? '/worlds' : null).data?.items ?? null
  const [pick, setPick] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [err, setErr] = React.useState<SandboxError | null>(null)
  const cap = useModalFrameCap('modal:sandbox-world')
  const go = async (w = pick) => {
    if (!w) return
    setBusy(true)
    const e = await sandbox.switchWorld(w, template)
    setBusy(false)
    if (e) setErr(e)
    else sandboxDialogs.close()
  }
  const items = (worlds ?? []).filter((w) => w.world_id !== current).map((w) => ({ value: w.world_id, label: worldLabel(w) }))
  const available = Array.isArray(err?.detail?.available_now) ? (err.detail.available_now as unknown[]).filter((x): x is string => typeof x === 'string') : []
  return (
    <Dialog open={open} onOpenChange={(o) => {
      cap.onOpenChange(o)
      if (!o) sandboxDialogs.close()
    }} onOpenChangeComplete={cap.onOpenChangeComplete}>
      <DialogContent className="sm:max-w-md" data-world-switch="">
        <DialogHeader>
          <DialogTitle>{t('sandbox.bar.world')}</DialogTitle>
          <DialogDescription>{t('sandbox.world.hint', { template: template ? t(`sandbox.template.${template}.short`) : '—' })}</DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel>{t('sandbox.start.world')}</FieldLabel>
          <Select items={items} value={pick ?? ''} onValueChange={(v) => typeof v === 'string' && setPick(v)}>
            <SelectTrigger className="w-full" aria-label={t('sandbox.start.world')} data-world-switch-select=""><SelectValue placeholder={t('sandbox.world.pick')} /></SelectTrigger>
            <SelectContent>
              {(worlds ?? []).filter((w) => w.world_id !== current).map((w) => (
                <SelectItem key={w.world_id} value={w.world_id} disabled={!w.available}>
                  <span className="flex w-full items-center justify-between gap-2"><span>{worldLabel(w)}</span><Badge variant="outline">{t(`sandbox.avail.${availabilityOf(w)}`)}</Badge></span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldDescription>{t('sandbox.world.noCarry')}</FieldDescription>
        </Field>
        {err ? (
          <Alert data-world-switch-error={err.code ?? err.status}>
            <Icon icon="alert.warning" />
            <AlertDescription className="flex flex-col gap-2">
              <span>{err.code === 508 ? t('sandbox.world.capacity') : err.code ? reasonText(err.code).short : err.message}</span>
              {available.length ? <span className="flex flex-wrap gap-1.5">{available.map((w) => <Button key={w} size="xs" variant="outline" onClick={() => void go(w)}>{w}</Button>)}</span> : null}
            </AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => void sandboxDialogs.downloadSnapshot()}>{t('sandbox.exportSnapshot')}</Button>
          <Button disabled={!pick || busy} onClick={() => void go()} data-world-switch-go="">{busy ? <Spinner data-icon="inline-start" /> : null}{t('sandbox.world.go')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** "the sandbox ended": the reason, the snapshot kept here, download, new sandbox from it, back to the shared show */
function SessionEndedDialog() {
  const t = useT()
  const phase = useSandbox((s) => s.phase)
  const reason = useSandbox((s) => s.endReason)
  const world = useSandbox((s) => s.world)
  const open = phase === 'ended' || phase === 'failed'
  const backRef = React.useRef<HTMLButtonElement>(null)
  const saved = React.useMemo(() => (open && world ? localSnapshot(world) : null), [open, world])
  const back = () => {
    sandbox.reset()
    navigate(DEMO_ENTRY)
  }
  return (
    <AlertDialog open={open} onOpenChange={(o) => {
      if (!o) back()
    }}>
      <AlertDialogContent initialFocus={backRef} data-session-ended={reason ?? ''}>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('sandbox.ended.title')}</AlertDialogTitle>
          <AlertDialogDescription>{t('sandbox.ended.reason', { reason: t(`sandbox.endReason.${reason ?? 'gone'}`) })}</AlertDialogDescription>
        </AlertDialogHeader>
        {saved ? (
          <p className="text-muted-foreground" data-session-ended-snapshot="">
            {t('sandbox.ended.saved', { v: saved.snapshot.vehicles.length, tg: saved.snapshot.targets.length, n: saved.snapshot.nests.length, k: saved.snapshot.tasks?.length ?? 0 })}
          </p>
        ) : null}
        <AlertDialogFooter>
          {saved ? <Button variant="outline" onClick={() => void sandboxDialogs.downloadSnapshot(saved.snapshot)}>{t('sandbox.ended.download')}</Button> : null}
          {saved ? <Button variant="outline" onClick={() => {
            const snap = saved.snapshot
            sandbox.reset()
            navigate(DEMO_ENTRY)
            sandboxDialogs.open('start', { snapshot: snap })
          }} data-session-ended-new="">{t('sandbox.ended.newFromSnapshot')}</Button> : null}
          <AlertDialogCancel ref={backRef} data-session-ended-back="">{t('sandbox.ended.back')}</AlertDialogCancel>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** the canvas follows a finished world switch (interim: reload into the same sid with the new last world) */
function useWorldFollow(): void {
  const world = useSandbox((s) => s.world)
  const phase = useSandbox((s) => s.phase)
  const switching = useSandbox((s) => s.switching)
  React.useEffect(() => {
    if (!world || switching || (phase !== 'ready' && phase !== 'active')) return
    const shown = viewport.worldId
    if (shown && shown !== world && location.pathname.startsWith('/sandbox/')) {
      prefs.setUi('lastWorld', world)
      power.expectNavigation()
      location.reload()
    }
  }, [world, phase, switching])
}

export function SandboxDialogsHost() {
  const open = useStore(dialogsStore, (s) => s.open)
  const world = useStore(dialogsStore, (s) => s.world)
  const snapshot = useStore(dialogsStore, (s) => s.snapshot)
  const seq = useStore(dialogsStore, (s) => s.seq)
  useWorldFollow()
  const onResult = (r: CreateResult) => {
    sandboxDialogs.close()
    if (r.kind === 'created') navigate(`/sandbox/${r.sid}`)
    // queued: the shared show with the queue card (AWR-04 §10.2)
    else if (r.kind === 'queued') navigate(DEMO_ENTRY)
  }
  return (
    <>
      <StartSheet open={open === 'start'} onOpenChange={(o) => (o ? null : sandboxDialogs.close())} world={world} snapshot={snapshot} onResult={onResult} />
      <TemplatePicker key={`tpl-${seq}`} open={open === 'template'} />
      <WorldSwitch key={`world-${seq}`} open={open === 'world'} />
      <PairDialog key={`pair-${seq}`} open={open === 'pair'} onOpenChange={(o) => (o ? null : sandboxDialogs.close())} />
      <SessionEndedDialog />
    </>
  )
}
