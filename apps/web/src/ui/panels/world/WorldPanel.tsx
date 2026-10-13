// WORLD group (M15-FR-021, FR-005, FR-027; AWR-14 §4.2, §6.13, §6.16): world selector (Select with items; switching the
// view world routes to /world/:id, the canvas and renderer stay), world facts from R04 and stores/world (M05): Chinese
// name and id, points, roots, highest building, the "schematic coordinates" badge for synthetic anchors (AWR-03 §5.1
// rule 3), loading phase and drawn points. When the view world differs from the session world (serverInfo), an info
// line says which world runs and offers to go back (static browsing, AWR-14 §6.16). "Add P600" enters the tool; it needs
// the seat and the session world.
// D2 (AWR-14 §20.5.1, §20.12): the world of a sandbox comes from the session (its route carries the sid): the panel shows
// it with the scenario template and the "load scenario" and "switch base map" entries instead of the selector (switching
// is a session operation, not a navigation); the D2 surfaces list the worlds of the sandbox catalogue and leave the D1
// "add P600" tool out (the fleet header adds vehicles).
import { useQuery } from '@tanstack/react-query'
import { useT } from '@/app/i18n'
import { navigate, useRoute } from '@/app/router/router'
import { worldDatasetQuery, worldsQuery } from '@/app/query/options'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { Alert, AlertAction, AlertDescription } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Item, ItemContent, ItemDescription, ItemTitle } from '@/ui/components/ui/item'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { useConnView } from '@/ui/shell/connView'
import { useSurface } from '@/app/shell/surface'
import { sbKeys, sbPublic } from '@/ui/sandbox/domain'
import { sandboxDialogs } from '@/ui/sandbox/dialogs'
import { useSandbox } from '@/ui/sandbox/session'
import { writeDeniedKey } from '@/ui/shell/guards'
import { toolMode, useTool } from '@/ui/tools/toolMode'
import { useWorld } from '@/stores/world'
import { datasetNoteKey } from '@/lib/demo'

/** loading phase and drawn points: follows the engine statistics without re-rendering the Select and the facts above it */
function WorldPoints() {
  const t = useT()
  const phase = useWorld((s) => s.phase)
  const B = useWorld((s) => s.B)
  return <ItemDescription>{t(`world.phase.${phase}`)} {'·'} {t('world.points', { n: fmt.pts(B) })}</ItemDescription>
}

export function WorldPanel() {
  const t = useT()
  const route = useRoute()
  const surface = useSurface((s) => s.surface)
  const surfaceWorld = useSurface((s) => s.worldId)
  const d2 = surface !== 'local'
  const sandbox = surface === 'sandbox'
  const template = useSandbox((s) => s.template)
  const current = route?.params.id ?? (d2 ? surfaceWorld : null)
  const worlds = useQuery({ ...worldsQuery(), enabled: !d2 })
  const sbWorlds = useQuery({ queryKey: sbKeys.worlds, queryFn: sbPublic.worlds, staleTime: 60_000, retry: false, enabled: d2 && !sandbox })
  const dataset = useQuery({ ...worldDatasetQuery(current ?? 'shenzhen'), enabled: current !== null })
  const anchorKind = useWorld((s) => s.anchorKind)
  const north = useWorld((s) => s.northConfidence)
  const synthGround = useWorld((s) => s.syntheticGroundZ)
  const session = useConnView((s) => s.sessionWorldId)
  useConnView((s) => s.version)
  const toolActive = useTool((s) => s.mode.startsWith('ADD'))
  const ids = d2 ? (sbWorlds.data?.items.map((w) => w.world_id) ?? (current ? [current] : [])) : (worlds.data?.map((w) => w.id) ?? (current ? [current] : []))
  const items = ids.map((id) => {
    const w = worlds.data?.find((x) => x.id === id)
    const sw = sbWorlds.data?.items.find((x) => x.world_id === id)
    const name = w?.nameZh ?? (sw?.name_zh ? sanitizeText(sw.name_zh, 24) : null)
    return { value: id, label: name ? `${name} ${id}` : id }
  })
  const info = worlds.data?.find((w) => w.id === current)
  const schematic = anchorKind === 'synthetic' || info?.anchorKind === 'synthetic' || info?.georeferenced === false
  const mismatch = session !== null && current !== null && session !== current
  const denied = writeDeniedKey() ?? (mismatch ? 'hint.otherWorld' : null)
  return (
    <div className="flex flex-col gap-2" data-world-panel="">
      {sandbox ? (
        <div className="flex flex-col gap-1.5" data-world-sandbox={current ?? ''}>
          <span className="flex items-center gap-2 text-hud-sub">
            <span className="w-10 shrink-0 text-muted-foreground">{t('world.scenario')}</span>
            <span className="min-w-0 flex-1 truncate font-mono">{template ?? '-'}</span>
            <Button size="xs" variant="outline" onClick={() => sandboxDialogs.open('template')} data-world-template="">{t('sandbox.bar.template')}</Button>
          </span>
          <span className="flex items-center gap-2 text-hud-sub">
            <span className="w-10 shrink-0 text-muted-foreground">{t('world.basemap')}</span>
            <span className="min-w-0 flex-1 truncate font-mono">{current ?? '-'}</span>
            <Button size="xs" variant="outline" onClick={() => sandboxDialogs.open('world')} data-world-switch="">{t('sandbox.bar.world')}</Button>
          </span>
        </div>
      ) : (
        <Field>
          <FieldLabel className="text-hud-cap uppercase text-muted-foreground">{t('world.select')}</FieldLabel>
          <Select items={items} value={current} onValueChange={(v) => v && navigate(`/world/${String(v)}`)}>
            <SelectTrigger size="sm" className="w-full" aria-label={t('world.select')}><SelectValue placeholder={t('world.none')} /></SelectTrigger>
            <SelectContent>
              {items.map((i) => <SelectItem key={i.value} value={i.value}>{i.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </Field>
      )}
      <Item size="sm" variant="outline">
        <ItemContent className="min-w-0 gap-1">
          <ItemTitle className="w-full min-w-0 gap-1.5">
            <Icon icon="nav.world" className="shrink-0" />
            <span className="min-w-0 flex-1 truncate">{info?.nameZh ?? (dataset.data?.nameZh ? sanitizeText(dataset.data.nameZh, 40) : null) ?? current ?? '—'}</span>
            {dataset.data?.reconEngine === 'mock' || (dataset.data?.tags?.includes('recon') && dataset.data.tags.includes('synthetic')) ? <Badge variant="outline" className="shrink-0" data-world-simulated="">{t('jobs.simulatedData')}</Badge> : null}
            {schematic ? <Badge variant="outline" className="shrink-0" data-world-schematic="">{t('world.schematic')}</Badge> : null}
          </ItemTitle>
          <ItemDescription>
            <span className="font-mono">{current ?? '—'}</span>
            {info?.stats || !dataset.data?.stats
              ? ` · ${t('world.facts', { points: fmt.pts(info?.stats?.points), roots: fmt.count(info?.stats?.roots), top: fmt.num(info?.stats?.maxHeightM) })}`
              : ` · ${t('world.factsPkg', { points: fmt.pts(dataset.data.stats.points), km2: fmt.num(dataset.data.stats.areaKm2 ?? Number.NaN, 2), top: fmt.num(dataset.data.stats.maxHeightM) })}`}
          </ItemDescription>
          {/* honesty facts (AWR-03 §5.1 rule 3; M16-FR-006): north confidence and synthetic ground */}
          {north === 'assumed' || north === 'unknown' || synthGround !== null ? (
            <ItemDescription data-world-honesty="">
              {north === 'assumed' ? t('world.northAssumed') : north === 'unknown' ? t('world.northUnknown') : ''}
              {synthGround !== null ? `${north === 'assumed' || north === 'unknown' ? ' · ' : ''}${t('world.syntheticGround', { z: fmt.num(synthGround) })}` : ''}
            </ItemDescription>
          ) : null}
          {dataset.data?.name ? (
            // the citation is shown in a shadcn Tooltip (no native title attribute)
            <Tooltip>
              <TooltipTrigger render={<ItemDescription data-world-dataset="" data-world-dataset-note={datasetNoteKey(dataset.data)} />}>
                {/* generated worlds (synthcity, ADR-077) carry redistribution = true; the six UrbanScene3D cities are used under the
                    authorization of the dataset rights holders (ADR-109 rule 3, world.json dataset.terms) */}
                {t(datasetNoteKey(dataset.data), { name: sanitizeText(dataset.data.name ?? '—', 40), version: sanitizeText(/v\d+\.\d+\.\d+/.exec(dataset.data.version ?? '')?.[0] ?? '', 20) })}
              </TooltipTrigger>
              {dataset.data.citation || dataset.data.terms ? (
                <TooltipContent className="flex max-w-80 flex-col gap-1">
                  {dataset.data.citation ? <span>{sanitizeText(dataset.data.citation, 200)}</span> : null}
                  {dataset.data.terms ? <span data-world-terms="">{sanitizeText(dataset.data.terms, 320)}</span> : null}
                </TooltipContent>
              ) : null}
            </Tooltip>
          ) : null}
          <WorldPoints />
        </ItemContent>
      </Item>
      {!d2 && worlds.isError ? <p className="text-hud-sub text-muted-foreground">{t('world.listUnavailable')}</p> : null}
      {mismatch ? (
        <Alert data-world-mismatch="">
          <Icon icon="alert.info" />
          <AlertDescription>{t('world.mismatch', { world: session })}</AlertDescription>
          <AlertAction>
            <Button size="xs" variant="outline" onClick={() => navigate(`/world/${session}`)}>{t('world.backTo', { world: session })}</Button>
          </AlertAction>
        </Alert>
      ) : null}
      {d2 ? null : (
        <Tooltip>
          <TooltipTrigger render={
            <Button size="sm" variant="outline" disabled={denied !== null} focusableWhenDisabled aria-pressed={toolActive} data-add-p600=""
              className="aria-disabled:opacity-50" onClick={() => (toolActive ? toolMode.done() : toolMode.enterAdd())} />
          }>
            <Icon icon="plus" data-icon="inline-start" />
            {t('world.addP600')}
          </TooltipTrigger>
          <TooltipContent>{denied ? t(denied) : t('tool.add.hint')}</TooltipContent>
        </Tooltip>
      )}
    </div>
  )
}
