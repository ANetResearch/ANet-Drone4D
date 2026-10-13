// Open interface page, /sandbox/api (AWR-04 §11.4; ADR-107; M15-FR-221; UX-FR-145; AWR-14 §20.14): the sandbox REST v1
// rendered from GET /api/sandbox/v1/openapi.json with shadcn parts only (no Swagger UI or Redoc): a section list on the
// left (sessions, catalogue, vehicles, perception, targets, nests and tasks, environment, realtime), the operation on the
// right (method as text, path, summary, x-awr auth / idempotency / rate class / SB id, the parameter table in the
// lieflat table.log skin, body and response schemas, curl and Python SDK examples with copy). Tokens never appear on the
// page: curl uses the placeholder $AWR_SANDBOX_TOKEN, "copy my token" writes the clipboard only on click; the session id
// is filled in when this browser holds a sandbox. The realtime section lists the WS endpoint, the subprotocol and the
// topic table of the contracts. A page-layer route in its own chunk (no three, no engine).
import { TOPICS } from '@awr/contracts/topics'
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { apiOrigin } from '@/net/endpoint'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/ui/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/ui/components/ui/collapsible'
import { Spinner } from '@/ui/components/ui/spinner'
import { Tabs, TabsContent, TabsList, TabsPanels, TabsTrigger } from '@/ui/components/ui/tabs'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { BrandLockup } from '@/ui/brand'
import { storedSession, useSandbox } from '@/ui/sandbox/session'
import { useFetched } from '@/ui/sandbox/useFetched'
import { lifetimeText } from '@/ui/sandbox/format'
import { remainingS, sandboxStore } from '@/ui/sandbox/session'
import { curlOf, fillPath, groupOperations, listOperations, sdkOf, type ApiOp, type ApiParam } from './apiDocsModel'

function CopyButton({ text, label }: { text: () => string; label: string }) {
  const [ok, setOk] = React.useState(false)
  return (
    <Button size="xs" variant="ghost" aria-label={label} onClick={() => void navigator.clipboard?.writeText(text()).then(() => {
      setOk(true)
      setTimeout(() => setOk(false), 1500)
    }, () => null)}>
      <StateIcon icon="copy" alt={ok} data-icon="inline-start" />
      {label}
    </Button>
  )
}

function Code({ text }: { text: string }) {
  return <pre className="overflow-x-auto rounded-md bg-muted p-2 font-mono text-hud-sub leading-relaxed whitespace-pre">{text}</pre>
}

function OperationCard({ op, sid }: { op: ApiOp; sid: string | null }) {
  const t = useT()
  const origin = apiOrigin() || (typeof location !== 'undefined' ? location.origin : '')
  const curl = curlOf(op, origin, sid)
  const sdk = op.pySample ?? sdkOf(op, origin)
  const columns: LfColumn<ApiParam>[] = [
    { key: 'name', label: t('api.col.name'), format: (p) => <span className="font-mono">{p.name}</span> },
    { key: 'in', label: t('api.col.in'), width: '5rem' },
    { key: 'type', label: t('api.col.type'), width: '7rem', format: (p) => <span className="font-mono">{p.type || '—'}</span> },
    { key: 'required', label: t('api.col.required'), width: '4rem', format: (p) => (p.required ? t('common.yes') : t('common.no')) },
    { key: 'description', label: t('api.col.desc'), format: (p) => p.description || '—' },
  ]
  return (
    <Card className="shrink-0" data-api-op={op.key}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Badge variant="outline" className="font-mono">{op.method.toUpperCase()}</Badge>
          <span className="font-mono">{`/api/sandbox/v1${fillPath(op.path, sid)}`}</span>
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-2">
          <span>{op.summary || '—'}</span>
          {op.sb ? <Badge variant="secondary">{op.sb}</Badge> : null}
          <Badge variant="outline">{t(`api.auth.${op.auth === 'public' ? 'public' : 'token'}`)}</Badge>
          {op.idempotent && op.idempotent !== 'no' ? <Badge variant="outline">{t('api.idempotent', { how: op.idempotent })}</Badge> : null}
          {op.rate ? <Badge variant="outline">{t('api.rate', { cls: op.rate })}</Badge> : null}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {op.description ? <p className="text-hud-sub text-muted-foreground">{op.description}</p> : null}
        {op.params.length ? <LfTable columns={columns} rows={op.params} rowKey={(p) => `${p.in}:${p.name}`} ariaLabel={t('api.params')} height={200} /> : null}
        {op.body ? <p className="text-hud-sub"><span className="text-muted-foreground">{t('api.body')}</span> <span className="font-mono">{op.body}</span></p> : null}
        <div className="flex flex-col gap-1 text-hud-sub" data-api-responses="">
          <span className="text-muted-foreground">{t('api.responses')}</span>
          {op.responses.map((r) => <span key={r.code} className="font-mono">{`${r.code} · ${r.description}${r.schema ? ` · ${r.schema}` : ''}`}</span>)}
        </div>
        {op.reasons.length ? (
          <div className="flex flex-col gap-1 text-hud-sub" data-api-reasons={op.reasons.length}>
            <span className="text-muted-foreground">{t('api.reasons')}</span>
            {op.reasons.map((c) => <span key={c}><span className="font-mono">{c}</span>{` · ${reasonText(c).short}`}</span>)}
          </div>
        ) : null}
        {op.realtime ? <p className="text-hud-sub"><span className="text-muted-foreground">{t('api.realtime')}</span> <span className="font-mono">{op.realtime}</span></p> : null}
        <Tabs defaultValue="curl">
          <TabsList variant="line">
            <TabsTrigger value="curl">curl</TabsTrigger>
            <TabsTrigger value="sdk">Python SDK</TabsTrigger>
          </TabsList>
          <TabsPanels>
            <TabsContent value="curl" className="flex flex-col gap-1">
              <Code text={curl} />
              <CopyButton text={() => curl} label={t('api.copy')} />
            </TabsContent>
            <TabsContent value="sdk" className="flex flex-col gap-1">
              <Code text={sdk} />
              <CopyButton text={() => sdk} label={t('api.copy')} />
            </TabsContent>
          </TabsPanels>
        </Tabs>
      </CardContent>
    </Card>
  )
}

function RealtimeSection({ sid }: { sid: string | null }) {
  const t = useT()
  const rows = TOPICS.filter((x) => x.kind !== 'client')
  const columns: LfColumn<(typeof TOPICS)[number]>[] = [
    { key: 'pattern', label: t('api.topic'), format: (r) => <span className="font-mono">{r.pattern}</span> },
    { key: 'encoding', label: t('api.encoding'), width: '5rem' },
    { key: 'schemaName', label: t('api.schema'), format: (r) => <span className="font-mono">{r.schemaName}</span> },
    { key: 'nativeHz', label: 'Hz', align: 'right', width: '3rem' },
  ]
  return (
    <Card className="shrink-0" data-api-realtime="">
      <CardHeader>
        <CardTitle>{t('api.group.realtime')}</CardTitle>
        <CardDescription className="font-mono">{`WS /api/sandbox/v1/sessions/${sid ?? '{sid}'}/rt · Sec-WebSocket-Protocol: awr.rt.v1`}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <p className="text-hud-sub text-muted-foreground">{t('api.realtimeHint')}</p>
        <LfTable columns={columns} rows={rows} rowKey={(r) => r.pattern} ariaLabel={t('api.topics')} height={360} />
      </CardContent>
    </Card>
  )
}

export function ApiDocsPage({ onClose }: { onClose?: () => void; params?: unknown; search?: unknown } = {}) {
  const t = useT()
  const spec = useFetched<Record<string, unknown>>('/openapi.json')
  const doc = spec.data
  const error = spec.error
  const [open, setOpen] = React.useState<string | null>(null)
  // the session of this browser: the store's, else the stored one (the page may open on its own, without the app)
  const stored = React.useMemo(() => storedSession(), [])
  const sid = useSandbox((s) => (s.token ? s.sid : null)) ?? stored?.sid ?? null
  const token = useSandbox((s) => s.token) ?? stored?.token ?? null
  React.useEffect(() => {
    document.title = t('title.view', { view: t('title.sandboxApi'), product: t('demo.product') })
  }, [t])
  const ops = React.useMemo(() => (doc ? listOperations(doc) : []), [doc])
  const groups = React.useMemo(() => groupOperations(ops), [ops])
  const current = ops.find((o) => o.key === open) ?? ops[0] ?? null
  const left = sid && sandboxStore.getState().session ? lifetimeText(remainingS(sandboxStore.getState())) : null
  return (
    <section data-view="api-docs" className="fixed inset-0 z-(--z-overlay-page) flex flex-col overflow-hidden bg-background text-foreground">
      <header className="flex h-11 shrink-0 items-center gap-3 border-b bg-card px-3">
        <BrandLockup compact />
        <span className="font-semibold">{t('api.title')}</span>
        <Button size="xs" variant="outline" nativeButton={false} render={<a href="/api/sandbox/v1/openapi.json" download="sandbox.openapi.json" />}>
          <Icon icon="data.export" data-icon="inline-start" />{t('api.downloadSpec')}
        </Button>
        <Button size="xs" variant="outline" nativeButton={false} render={<a href="/sdk/awr_sandbox.py" download="awr_sandbox.py" />}>
          <Icon icon="data.export" data-icon="inline-start" />{t('api.downloadSdk')}
        </Button>
        <span className="ml-auto flex items-center gap-2 text-hud-sub">
          {sid ? (
            <>
              <span className="text-muted-foreground">{t('api.mySession', { sid, left: left ?? '—' })}</span>
              <CopyButton text={() => token ?? ''} label={t('api.copyToken')} />
            </>
          ) : <span className="text-muted-foreground">{t('api.noSession')}</span>}
          <Button size="xs" variant="ghost" onClick={() => (onClose ? onClose() : history.length > 1 ? history.back() : navigate('/worlds'))}><Icon icon="close" data-icon="inline-start" />{t('common.close')}</Button>
        </span>
      </header>
      {sid ? <p className="border-b px-3 py-1 text-hud-sub text-muted-foreground">{t('api.tokenHint')}</p> : null}
      {error ? (
        <div className="flex flex-col items-center gap-2 p-8" data-api-error="">
          <p className="text-muted-foreground">{t('api.unavailable')}</p>
          <Button size="sm" variant="outline" onClick={spec.reload}>{t('common.retry')}</Button>
        </div>
      ) : !doc ? <div className="p-8"><Spinner /></div> : (
        <div className="flex min-h-0 flex-1">
          <nav className="w-64 shrink-0 overflow-y-auto border-r p-2" aria-label={t('api.sections')} data-api-nav={ops.length}>
            {groups.map((g) => (
              <Collapsible key={g.id} defaultOpen>
                <CollapsibleTrigger render={<Button size="xs" variant="ghost" className="w-full justify-between font-semibold" />}>
                  {t(`api.group.${g.id}`)}
                  <span className="text-muted-foreground">{g.ops.length}</span>
                </CollapsibleTrigger>
                <CollapsibleContent className="flex flex-col">
                  {g.ops.map((o) => (
                    <Button key={o.key} size="xs" variant={current?.key === o.key ? 'secondary' : 'ghost'} className="justify-start font-mono" onClick={() => setOpen(o.key)} data-api-link={o.key}>
                      <span className="w-12 shrink-0 text-left">{o.method.toUpperCase()}</span>
                      <span className="truncate">{o.path}</span>
                    </Button>
                  ))}
                </CollapsibleContent>
              </Collapsible>
            ))}
          </nav>
          <main className="flex min-w-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
            {current ? <OperationCard op={current} sid={sid} /> : null}
            <RealtimeSection sid={sid} />
          </main>
        </div>
      )}
    </section>
  )
}
