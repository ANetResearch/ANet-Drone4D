// /lab/submissions, internal submissions (BN-85, BN-95, BN-96, BN-98, BN-99; AWR-17 §18.23). Lists the team's submissions
// (researchers see all) with pipeline state, queue position, board visibility and the per-lane metrics once scored; the form
// registers an agent image (digest of an image already on dmax) with its entry command and environment. Withdrawal is
// offered before EVALUATING; visibility switches between private and the internal board.
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { sanitizeText } from '@/lib/sanitize'
import { benchDelete, BenchError, benchGet, benchPost, type BoardIndex, type Submission } from '@/net/bench'
import { PanelEmpty } from '@/ui/brand'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader,
  AlertDialogTitle,
} from '@/ui/components/ui/alert-dialog'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/ui/components/ui/field'
import { Input } from '@/ui/components/ui/input'
import { NativeSelect, NativeSelectOption } from '@/ui/components/ui/native-select'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Textarea } from '@/ui/components/ui/textarea'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { fmtValue, METRICS } from './board'
import { canToggleVisibility, canWithdraw, DIGEST_RE, parseCmd, parseEnv } from './submissions'
import { useBenchSession } from './useBenchSession'

function errText(t: ReturnType<typeof useT>, x: unknown): string {
  if (!(x instanceof BenchError)) return t('lab.unreachable')
  const why = x.detail && typeof x.detail === 'object' && 'why' in x.detail ? String((x.detail as { why: unknown }).why) : ''
  return t('lab.sub.failed', { code: x.code ?? x.status, name: x.reasonName ?? '', why })
}

export function LabSubmissionsPage() {
  const t = useT()
  const qc = useQueryClient()
  const session = useBenchSession()
  const q = useQuery({ queryKey: ['lab', 'submissions'], queryFn: () => benchGet<{ items: Submission[] }>('/submissions'), enabled: session !== null,
    refetchInterval: 10_000 })
  const idx = useQuery({ queryKey: ['lab', 'board'], queryFn: () => benchGet<BoardIndex>('/board'), enabled: session !== null })
  const [withdrawing, setWithdrawing] = React.useState<Submission | null>(null)
  const [actionErr, setActionErr] = React.useState<string | null>(null)
  const rows = q.data?.items ?? []
  const refresh = () => void qc.invalidateQueries({ queryKey: ['lab', 'submissions'] })
  const toggle = async (s: Submission) => {
    setActionErr(null)
    try {
      await benchPost(`/submissions/${encodeURIComponent(s.submission_id)}/visibility`, { visibility: s.visibility === 'board' ? 'private' : 'board' })
      refresh()
      void qc.invalidateQueries({ queryKey: ['lab', 'leaderboard'] })
    } catch (x) {
      setActionErr(errText(t, x))
    }
  }
  const withdraw = async (s: Submission) => {
    setActionErr(null)
    try {
      await benchDelete(`/submissions/${encodeURIComponent(s.submission_id)}`)
      refresh()
    } catch (x) {
      setActionErr(errText(t, x))
    }
  }
  const cols: LfColumn<Submission>[] = [
    { key: 'id', label: t('lab.sub.col.id'), format: (s) => <span className="font-mono">{sanitizeText(s.submission_id, 32)}</span> },
    { key: 'track', label: t('lab.sub.col.track'), format: (s) => <span className="font-mono">{sanitizeText(s.track, 32)}</span> },
    { key: 'image', label: t('lab.sub.col.image'), format: (s) => <span className="font-mono" title={s.image_digest}>{s.image_digest.slice(0, 19)}</span> },
    { key: 'state', label: t('lab.sub.col.state'), format: (s) => (
      <span title={s.reason ? `${s.reason.code} ${s.reason.name}` : undefined}>
        {t(`lab.sub.state.${s.state}`)}{typeof s.reason?.detail?.why === 'string' ? ` · ${sanitizeText(s.reason.detail.why, 24)}` : ''}
      </span>
    ) },
    { key: 'position', label: t('lab.sub.col.position'), align: 'right', format: (s) => (s.position > 0 ? String(s.position) : '—') },
    { key: 'visibility', label: t('lab.sub.col.visibility'), format: (s) => t(`lab.sub.visibility.${s.visibility}`) },
    ...METRICS.map((k): LfColumn<Submission> => ({
      key: k, label: t(`lab.board.metric.${k}`), align: 'right', format: (s) => <span className="font-mono">{fmtValue(s.result?.lanes[0]?.metrics[k])}</span>,
    })),
    { key: 'actions', label: t('lab.sub.col.actions'), format: (s) => (
      <span className="flex gap-1">
        {canToggleVisibility(s.state) ? (
          <Button size="xs" variant="outline" onClick={(e) => { e.stopPropagation(); void toggle(s) }}>
            {s.visibility === 'board' ? t('lab.sub.hide') : t('lab.sub.show')}
          </Button>
        ) : null}
        {canWithdraw(s.state) ? (
          <Button size="xs" variant="ghost" onClick={(e) => { e.stopPropagation(); setWithdrawing(s) }}>{t('lab.sub.withdraw')}</Button>
        ) : null}
      </span>
    ) },
  ]
  const tracks = (idx.data?.versions.find((v) => v.current)?.tracks ?? []).map((x) => x.track)
  return (
    <section data-view="lab-submissions" data-figure="lab-submissions" className="app-layer-overlay-page overflow-auto bg-background p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Icon icon="data.package" />
        <h1 className="text-ed-title font-bold">{t('lab.sub.title')}</h1>
        <span className="text-hud-sub text-muted-foreground">{session ? t('lab.session', { name: session.principal, role: t(`lab.role.${session.role}`) }) : ''}</span>
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="ghost" onClick={() => navigate('/lab/board')}>
            <Icon icon="perf.bench" data-icon="inline-start" />
            {t('lab.board.title')}
          </Button>
          <Button size="sm" variant="ghost" onClick={refresh}>
            <Icon icon="refresh" data-icon="inline-start" />
            {t('lab.refresh')}
          </Button>
        </div>
      </div>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0">
          {actionErr ? <p className="mb-2 text-hud-sub text-muted-foreground" role="alert">{actionErr}</p> : null}
          {q.isPending ? (
            <div className="flex flex-col gap-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-7 rounded-md" />)}</div>
          ) : q.isError ? (
            <PanelEmpty title={t('lab.unreachable')} description={t('lab.unreachableHint')}
              action={<Button size="sm" variant="outline" onClick={refresh}>{t('common.retry')}</Button>} />
          ) : !rows.length ? (
            <PanelEmpty title={t('lab.sub.empty')} description={t('lab.sub.emptyHint')} />
          ) : (
            <LfTable columns={cols} rows={rows} rowKey={(s) => s.submission_id} ariaLabel={t('lab.sub.title')} figureId="lab-submissions-table"
              height={Math.max(240, typeof window !== 'undefined' ? window.innerHeight - 160 : 480)} />
          )}
        </div>
        <SubmitForm tracks={tracks} onDone={refresh} />
      </div>
      <AlertDialog open={withdrawing !== null} onOpenChange={(o) => (o ? null : setWithdrawing(null))}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('lab.sub.withdrawTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('lab.sub.withdrawBody', { id: withdrawing?.submission_id ?? '' })}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={() => { if (withdrawing) void withdraw(withdrawing); setWithdrawing(null) }}>{t('lab.sub.withdraw')}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}

function SubmitForm({ tracks, onDone }: { tracks: string[]; onDone: () => void }) {
  const t = useT()
  const [track, setTrack] = React.useState<string>('')
  const [digest, setDigest] = React.useState('')
  const [cmd, setCmd] = React.useState('python -m drone4d.serve my_pkg.agent:Agent')
  const [env, setEnv] = React.useState('')
  const [notes, setNotes] = React.useState('')
  const [busy, setBusy] = React.useState(false)
  const [err, setErr] = React.useState<string | null>(null)
  const [ok, setOk] = React.useState<string | null>(null)
  const tr = track || tracks[0] || ''
  const cmdV = parseCmd(cmd)
  const envV = parseEnv(env)
  const digestOk = DIGEST_RE.test(digest.trim())
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!cmdV || !envV || !digestOk || !tr) return
    setBusy(true)
    setErr(null)
    setOk(null)
    try {
      const r = await benchPost<{ submission_id: string; position: number }>('/submissions', {
        track: tr, image_digest: digest.trim(), entry: Object.keys(envV).length ? { cmd: cmdV, env: envV } : { cmd: cmdV }, notes,
      })
      setOk(r ? t('lab.sub.created', { id: r.submission_id, pos: r.position }) : null)
      onDone()
    } catch (x) {
      setErr(errText(t, x))
    } finally {
      setBusy(false)
    }
  }
  return (
    <form data-figure="lab-submit-form" onSubmit={(e) => void submit(e)} className="flex flex-col gap-3">
      <h2 className="text-hud-title font-semibold">{t('lab.sub.new')}</h2>
      <Field>
        <FieldLabel htmlFor="sub-track">{t('lab.sub.col.track')}</FieldLabel>
        <NativeSelect id="sub-track" size="sm" value={tr} onChange={(e) => setTrack(e.target.value)} disabled={!tracks.length}>
          {tracks.map((x) => <NativeSelectOption key={x} value={x}>{x}</NativeSelectOption>)}
        </NativeSelect>
      </Field>
      <Field>
        <FieldLabel htmlFor="sub-digest">{t('lab.sub.digest')}</FieldLabel>
        <Input id="sub-digest" className="font-mono" spellCheck={false} autoComplete="off" placeholder="sha256:…" value={digest} onChange={(e) => setDigest(e.target.value)} />
        <FieldDescription>{t('lab.sub.digestHint')}</FieldDescription>
        {digest && !digestOk ? <FieldError>{t('lab.sub.digestInvalid')}</FieldError> : null}
      </Field>
      <Field>
        <FieldLabel htmlFor="sub-cmd">{t('lab.sub.cmd')}</FieldLabel>
        <Input id="sub-cmd" className="font-mono" spellCheck={false} value={cmd} onChange={(e) => setCmd(e.target.value)} />
        {!cmdV ? <FieldError>{t('lab.sub.cmdInvalid')}</FieldError> : null}
      </Field>
      <Field>
        <FieldLabel htmlFor="sub-env">{t('lab.sub.env')}</FieldLabel>
        <Textarea id="sub-env" className="font-mono" rows={3} spellCheck={false} placeholder="MODE=eval" value={env} onChange={(e) => setEnv(e.target.value)} />
        {!envV ? <FieldError>{t('lab.sub.envInvalid')}</FieldError> : <FieldDescription>{t('lab.sub.envHint')}</FieldDescription>}
      </Field>
      <Field>
        <FieldLabel htmlFor="sub-notes">{t('lab.sub.notes')}</FieldLabel>
        <Textarea id="sub-notes" rows={2} maxLength={2000} value={notes} onChange={(e) => setNotes(e.target.value)} />
      </Field>
      {err ? <p className="text-hud-sub text-muted-foreground" role="alert">{err}</p> : null}
      {ok ? <p className="text-hud-sub" role="status">{ok}</p> : null}
      <Button type="submit" disabled={busy || !cmdV || !envV || !digestOk || !tr}>{t('lab.sub.submit')}</Button>
    </form>
  )
}
