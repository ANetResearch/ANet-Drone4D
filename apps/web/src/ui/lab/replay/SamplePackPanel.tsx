// 样例包浏览（D3-AC-56"样例包在内部工作区可浏览"；数据形状见 samplePack.ts）。lieflat 表格：观测档组、基线策略、代表运行、
// 四项指标（EC、EU、AO、CE，三位小数）、样例文件（私有视角视频、机队轨迹、MCAP、指标等，经 BN-54 下载）与操作（设为运行 A、设为运行 B、打开观测墙）。
// 多个样例包时按生成时刻由新到旧，可切换；无样例包（接口未提供或尚未生成）时显示空状态，页面其余部分照常可用。
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { sanitizeText } from '@/lib/sanitize'
import { PanelEmpty } from '@/ui/brand'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger,
} from '@/ui/components/ui/dropdown-menu'
import { NativeSelect, NativeSelectOption } from '@/ui/components/ui/native-select'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { fmtNum, METRICS } from '@/ui/lab/board'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { artifactHref, bytesText, type SampleEntry, type SampleFile, type SamplePack } from './samplePack'
import type { RunSide } from './sync'

const FILE_ICON: Readonly<Record<SampleFile['kind'], IconKey>> = {
  video: 'sensor.video', mcap: 'data.package', metrics: 'perf.chart', tape: 'tl.replay', manifest: 'data.json', slice: 'data.file',
  tracks: 'layer.trajectory',
}

function fileLabel(t: ReturnType<typeof useT>, f: SampleFile): string {
  if (f.kind === 'video' && f.uav) return t('lab.replay.pack.fileVideo', { uav: f.uav, cam: f.cam ?? '' })
  return t(`lab.replay.pack.file.${f.kind}`)
}

function FilesMenu({ s }: { s: SampleEntry }) {
  const t = useT()
  if (!s.files.length) return <span className="text-muted-foreground">—</span>
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="xs" variant="outline" data-pack-files={s.runId} />}>
        <Icon icon="data.folder" data-icon="inline-start" />
        {t('lab.replay.pack.files', { n: s.files.length })}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="font-mono">{s.runId}</DropdownMenuLabel>
          {s.files.map((f) => (
            <DropdownMenuItem key={f.path} render={<a href={artifactHref(s.runId, f.path)} download={f.path.split('/').at(-1)} data-pack-file={f.path} />}>
              <Icon icon={FILE_ICON[f.kind]} />
              <span>{fileLabel(t, f)}</span>
              <span className="ml-auto pl-3 font-mono text-muted-foreground tabular-nums">{bytesText(f.bytes)}</span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export interface SamplePackPanelProps {
  packs: readonly SamplePack[] | undefined
  pending: boolean
  /** 样例包接口不可用（未部署或角色不足） */
  unavailable: boolean
  a: string | null
  b: string | null
  onAssign: (side: RunSide, rid: string) => void
}

export function SamplePackPanel({ packs, pending, unavailable, a, b, onAssign }: SamplePackPanelProps) {
  const t = useT()
  const [packId, setPackId] = React.useState<string | null>(null)
  const list = packs ?? []
  const pack = list.find((p) => p.packId === packId) ?? list[0]
  const cols: LfColumn<SampleEntry>[] = [
    { key: 'group', label: t('lab.replay.pack.col.group'), format: (s) => <span className="font-mono">{sanitizeText(s.group, 16).toUpperCase()}</span> },
    { key: 'policy', label: t('lab.replay.pack.col.policy'), format: (s) => <span className="font-mono">{sanitizeText(s.policy, 48)}</span> },
    { key: 'run', label: t('lab.replay.pack.col.run'), format: (s) => (
      <span className="inline-flex items-center gap-1.5 font-mono">
        {sanitizeText(s.runId, 64)}
        {s.runId === a ? <Badge variant="outline" data-pack-side="a">A</Badge> : null}
        {s.runId === b ? <Badge variant="outline" data-pack-side="b">B</Badge> : null}
      </span>
    ) },
    ...METRICS.map((k): LfColumn<SampleEntry> => ({
      key: k, label: t(`lab.board.metric.${k}`), align: 'right', format: (s) => <span className="font-mono">{fmtNum(s.metrics[k])}</span>,
    })),
    { key: 'sim', label: t('lab.replay.pack.col.sim'), unit: 's', align: 'right', format: (s) => <span className="font-mono">{fmtNum(s.simS, 1)}</span> },
    { key: 'files', label: t('lab.replay.pack.col.files'), format: (s) => <FilesMenu s={s} /> },
    { key: 'actions', label: t('lab.replay.pack.col.actions'), format: (s) => (
      <span className="inline-flex gap-1" onClick={(e) => e.stopPropagation()}>
        <Button size="xs" variant={s.runId === a ? 'secondary' : 'outline'} data-pack-assign={`a/${s.runId}`} onClick={() => onAssign('a', s.runId)}>{t('lab.replay.pack.setA')}</Button>
        <Button size="xs" variant={s.runId === b ? 'secondary' : 'outline'} data-pack-assign={`b/${s.runId}`} onClick={() => onAssign('b', s.runId)}>{t('lab.replay.pack.setB')}</Button>
        <Button size="xs" variant="ghost" aria-label={t('lab.replay.pack.openWall', { id: s.runId })} onClick={() => navigate(`/lab/runs/${encodeURIComponent(s.runId)}`)}>
          <Icon icon="sensor.camera" />
        </Button>
      </span>
    ) },
  ]
  return (
    <section data-figure="lab-replay-pack" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Icon icon="data.package" />
        <h2 className="text-ed-sub font-bold">{t('lab.replay.pack.title')}</h2>
        {pack ? (
          <span className="text-hud-sub text-muted-foreground" data-pack-meta={pack.packId}>
            {t('lab.replay.pack.meta', {
              event: pack.eventId, v: pack.eventVersion ?? '—', world: pack.worldId ?? '—', board: pack.boardVersion ?? '—',
              at: pack.generatedUtc ? pack.generatedUtc.replace('T', ' ').slice(0, 16) : '—',
            })}
          </span>
        ) : null}
        {list.length > 1 ? (
          <NativeSelect size="sm" className="ml-auto" value={pack?.packId ?? ''} aria-label={t('lab.replay.pack.select')} data-pack-select=""
            onChange={(e) => setPackId(e.target.value)}>
            {list.map((p) => <NativeSelectOption key={p.packId} value={p.packId}>{p.packId}</NativeSelectOption>)}
          </NativeSelect>
        ) : null}
      </div>
      {pending ? (
        <div className="flex flex-col gap-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-7 rounded-md" />)}</div>
      ) : !pack ? (
        <PanelEmpty title={unavailable ? t('lab.replay.pack.unavailable') : t('lab.replay.pack.empty')} description={t('lab.replay.pack.emptyHint')} />
      ) : (
        <>
          <LfTable columns={cols} rows={pack.samples} rowKey={(s) => `${s.group}/${s.policy}/${s.runId}`} ariaLabel={t('lab.replay.pack.title')}
            figureId="lab-replay-pack-table" height={Math.min(320, 40 + pack.samples.length * 28)} />
          {pack.dropped ? <p className="text-hud-sub text-muted-foreground" data-pack-dropped={pack.dropped}>{t('lab.replay.pack.dropped', { n: pack.dropped })}</p> : null}
        </>
      )}
    </section>
  )
}
